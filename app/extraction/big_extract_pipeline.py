"""Shared downstream pipeline: parse -> validate -> evidence -> persist.

Used by both run_big_llm_extract (API path) and ingest_big_llm_results (manual path).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.extraction.big_extract_models import BigExtractResult, ParseError, ValidationError
from app.observability.stats import compute_big_extract_stats
from app.extraction.big_extract_parse import (
    parse_big_extract_response,
    validate_big_extract_result,
    validate_evidence_snippet,
)
from app.extraction.big_extract_persist import (
    load_chunk_text_by_id,
    to_frame_entity,
    to_mention_entity,
    to_mention_evidence_entity,
    to_relation_entity,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models.extraction import ExtractionRun


def _load_known_chunk_ids(doc_id: str, session: Session) -> set[str]:
    from sqlalchemy import select

    from app.db.models.chunk import Chunk

    rows = session.scalars(
        select(Chunk.id).where(Chunk.document_id == doc_id)
    ).all()
    return set(rows)


def run_downstream_pipeline(
    raw_responses: list[str],
    doc_id: str,
    run_id: str,
    run_entity: ExtractionRun,
    session: Session,
    known_chunk_ids: set[str] | None = None,
    skip_evidence_validation: bool = False,
) -> None:
    """Parse all responses, validate, run evidence checks, persist on success.

    On any parse/validation/evidence error: sets run status FAILED, stores errors in
    stats_json, does not persist frames/mentions/relations.

    skip_evidence_validation: if True, do not require evidence.snippet to be a substring
    of chunk text (e.g. for ingest-file-and-json with pre-produced JSON).
    """
    if known_chunk_ids is None:
        known_chunk_ids = _load_known_chunk_ids(doc_id, session)
    results: list[BigExtractResult] = []
    parse_errors: list[str] = []
    for i, raw in enumerate(raw_responses):
        parsed = parse_big_extract_response(raw)
        if isinstance(parsed, ParseError):
            parse_errors.append(f"Part {i}: {parsed.message}")
            continue
        results.append(parsed)
    if parse_errors:
        _fail_run(
            run_entity,
            session,
            {"parse_errors": parse_errors},
        )
        return
    validation_errors: list[str] = []
    for r in results:
        validation_errors.extend(
            e.message for e in validate_big_extract_result(r, doc_id, known_chunk_ids)
        )
    if validation_errors:
        _fail_run(
            run_entity,
            session,
            {"validation_errors": validation_errors},
        )
        return
    if not skip_evidence_validation:
        all_chunk_ids = set()
        for r in results:
            for f in r.frames:
                all_chunk_ids.add(f.chunk_id)
        chunk_text_by_id = load_chunk_text_by_id(all_chunk_ids, session)
        evidence_errors: list[str] = []
        for r in results:
            for frame in r.frames:
                for mention in frame.mentions:
                    err = validate_evidence_snippet(mention, frame, chunk_text_by_id)
                    if err is not None:
                        evidence_errors.append(err.message)
        if evidence_errors:
            _fail_run(
                run_entity,
                session,
                {"evidence_errors": evidence_errors},
            )
            return
    try:
        mention_id_to_db_id: dict[str, str] = {}
        for r in results:
            for frame_result in r.frames:
                frame_entity = to_frame_entity(frame_result, doc_id, run_id)
                session.add(frame_entity)
                session.flush()
                for mention_result in frame_result.mentions:
                    mention_entity = to_mention_entity(
                        mention_result,
                        frame_entity,
                        doc_id,
                        run_id,
                    )
                    session.add(mention_entity)
                    session.flush()
                    mention_id_to_db_id[mention_result.mention_id] = mention_entity.id
                    ev_entity = to_mention_evidence_entity(mention_result)
                    ev_entity.mention_id = mention_entity.id
                    session.add(ev_entity)
                for relation_result in frame_result.relations:
                    src_id = mention_id_to_db_id.get(relation_result.src_mention_id)
                    dst_id = mention_id_to_db_id.get(relation_result.dst_mention_id)
                    if not src_id or not dst_id:
                        continue
                    rel_entity = to_relation_entity(
                        relation_result,
                        frame_entity,
                        run_id,
                        src_id,
                        dst_id,
                    )
                    session.add(rel_entity)
        stats = compute_big_extract_stats(results, [])
        run_entity.status = "SUCCEEDED"
        run_entity.stats_json = json.dumps(stats)
        session.add(run_entity)
        session.commit()
    except Exception as e:
        session.rollback()
        _fail_run(
            run_entity,
            session,
            {"persist_error": str(e)},
        )


def _fail_run(
    run_entity: ExtractionRun,
    session: Session,
    payload: dict,
) -> None:
    run_entity.status = "FAILED"
    run_entity.stats_json = json.dumps(payload)
    session.add(run_entity)
    session.commit()
