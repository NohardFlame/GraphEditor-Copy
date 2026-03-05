"""Phase 3 big-LLM extraction tests: API path (mocked), manual path, negative."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Chunk as ChunkORM, ExtractionRun, Frame, Mention, MentionEvidence
from app.extraction.big_extract_runner import (
    BIG_LLM_EXTRACT,
    ingest_big_llm_results,
    prepare_big_llm_export,
    run_big_llm_extract,
)


def _canned_big_extract_json(doc_id: str, chunk_id: str, chunk_text: str) -> dict:
    """One frame, one ACTOR mention, evidence.snippet = substring of chunk_text."""
    snippet = chunk_text[:10] if len(chunk_text) >= 10 else chunk_text
    return {
        "doc_id": doc_id,
        "prompt_version": "big_extract_v1",
        "frames": [
            {
                "frame_id": f"frame::{chunk_id}::0",
                "chunk_id": chunk_id,
                "frame_index": 0,
                "frame_text": chunk_text,
                "mentions": [
                    {
                        "mention_id": f"m::frame::{chunk_id}::0::0",
                        "type": "ACTOR",
                        "fields": {"name": "User"},
                        "epistemic": "EXPLICIT",
                        "evidence": {"snippet": snippet, "char_start": 0, "char_end": len(snippet)},
                        "confidence": 0.9,
                    }
                ],
                "relations": [],
            }
        ],
        "references": [],
        "errors": [],
    }


def _chunk_ids(session, doc_id: str) -> list[str]:
    rows = session.scalars(
        select(ChunkORM).where(ChunkORM.document_id == doc_id).order_by(ChunkORM.chunk_index)
    ).all()
    return [r.id for r in rows]


def test_prepare_big_llm_export_creates_run_and_files(big_extract_session, tmp_path):
    """prepare_big_llm_export creates extraction_run and writes part files + manifest."""
    run_id, paths, manifest_path = prepare_big_llm_export(
        "doc-big",
        "test-model",
        output_dir=tmp_path,
        session=big_extract_session,
    )
    assert run_id is not None
    assert len(paths) >= 1
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["doc_id"] == "doc-big"
    assert manifest["run_id"] == str(run_id)
    assert manifest["prompt_version"] == "big_extract_v1"
    assert len(manifest["parts"]) == len(paths)
    for p in paths:
        assert p.exists()
        assert "SYSTEM" in p.read_text() and "USER" in p.read_text()


def test_ingest_big_llm_results_success(big_extract_session, tmp_path):
    """Manual path: prepare export, then ingest canned JSON -> run SUCCEEDED, frames/mentions persisted."""
    run_id, _, _ = prepare_big_llm_export(
        "doc-big",
        "test-model",
        output_dir=tmp_path,
        session=big_extract_session,
    )
    chunk_ids = _chunk_ids(big_extract_session, "doc-big")
    assert len(chunk_ids) >= 1
    chunk_id = chunk_ids[0]
    chunk_row = big_extract_session.get(ChunkORM, chunk_id)
    chunk_text = (chunk_row.text or "")[:20]
    canned = _canned_big_extract_json("doc-big", chunk_id, chunk_row.text or "")
    result_file = tmp_path / "part_0.json"
    result_file.write_text(json.dumps(canned), encoding="utf-8")

    ingest_big_llm_results(
        run_id,
        [result_file],
        session=big_extract_session,
    )

    run_entity = big_extract_session.get(ExtractionRun, str(run_id))
    assert run_entity is not None
    assert run_entity.status == "SUCCEEDED"
    assert run_entity.stats_json is not None
    stats = json.loads(run_entity.stats_json)
    assert stats.get("frames_count") == 1
    assert stats.get("mentions_count_total") == 1
    assert "mentions_count_by_type" in stats
    assert stats.get("evidence_valid_count") == 1

    frames = big_extract_session.scalars(
        select(Frame).where(Frame.document_id == "doc-big")
    ).all()
    assert len(frames) == 1
    mentions = big_extract_session.scalars(
        select(Mention).where(Mention.document_id == "doc-big")
    ).all()
    assert len(mentions) == 1
    assert mentions[0].type == "ACTOR"
    evidence = big_extract_session.scalars(
        select(MentionEvidence).where(MentionEvidence.mention_id == mentions[0].id)
    ).all()
    assert len(evidence) == 1
    assert evidence[0].snippet_text in (chunk_row.text or "")


def test_ingest_big_llm_results_invalid_evidence_fails(big_extract_session, tmp_path):
    """Canned JSON with evidence.snippet not in chunk -> run FAILED, no frames/mentions."""
    run_id, _, _ = prepare_big_llm_export(
        "doc-big",
        "test-model",
        output_dir=tmp_path,
        session=big_extract_session,
    )
    chunk_ids = _chunk_ids(big_extract_session, "doc-big")
    chunk_id = chunk_ids[0]
    canned = _canned_big_extract_json("doc-big", chunk_id, "wrong snippet not in chunk")
    canned["frames"][0]["mentions"][0]["evidence"]["snippet"] = "NOT_IN_CHUNK"
    result_file = tmp_path / "part_0.json"
    result_file.write_text(json.dumps(canned), encoding="utf-8")

    ingest_big_llm_results(
        run_id,
        [result_file],
        session=big_extract_session,
    )

    run_entity = big_extract_session.get(ExtractionRun, str(run_id))
    assert run_entity.status == "FAILED"
    stats = json.loads(run_entity.stats_json or "{}")
    assert "evidence_errors" in stats

    frames = big_extract_session.scalars(
        select(Frame).where(Frame.document_id == "doc-big")
    ).all()
    assert len(frames) == 0
    mentions = big_extract_session.scalars(
        select(Mention).where(Mention.document_id == "doc-big")
    ).all()
    assert len(mentions) == 0


def test_run_big_llm_extract_with_mock_client(big_extract_session):
    """API path: mock llm_client returns canned JSON -> run SUCCEEDED, frames/mentions/relations."""
    chunk_ids = _chunk_ids(big_extract_session, "doc-big")
    chunk_id = chunk_ids[0]
    chunk_row = big_extract_session.get(ChunkORM, chunk_id)
    canned = _canned_big_extract_json("doc-big", chunk_id, chunk_row.text or "")

    class MockClient:
        def complete(self, system_prompt: str, user_prompt: str, model_id: str) -> str:
            return json.dumps(canned)

    run_id = run_big_llm_extract(
        "doc-big",
        "test-model",
        session=big_extract_session,
        llm_client=MockClient(),
    )

    run_entity = big_extract_session.get(ExtractionRun, str(run_id))
    assert run_entity.status == "SUCCEEDED"
    stats = json.loads(run_entity.stats_json or "{}")
    assert "frames_count" in stats
    assert "mentions_count_total" in stats
    assert "mentions_count_by_type" in stats
    assert "relations_count" in stats
    assert "evidence_valid_count" in stats
    assert "evidence_invalid_count" in stats
    frames = big_extract_session.scalars(
        select(Frame).where(Frame.document_id == "doc-big")
    ).all()
    assert len(frames) >= 1
    mentions = big_extract_session.scalars(
        select(Mention).where(Mention.document_id == "doc-big")
    ).all()
    assert len(mentions) >= 1


def test_run_big_llm_extract_without_client_raises(big_extract_session):
    """run_big_llm_extract with llm_client=None raises when it would call LLM."""
    with pytest.raises(RuntimeError, match="llm_client"):
        run_big_llm_extract(
            "doc-big",
            "test-model",
            session=big_extract_session,
            llm_client=None,
        )


@pytest.mark.skipif(
    not os.environ.get("E2E_LLM"),
    reason="Set E2E_LLM=1 to run real LLM extraction test (requires Ollama or Gemini)",
)
def test_run_big_llm_extract_real_llm(big_extract_session):
    """Real test: call actual LLM (Ollama/Gemini), parse response, persist frames/mentions."""
    from app.extraction.real_llm_client import make_real_client

    client = make_real_client()
    run_id = run_big_llm_extract(
        "doc-big",
        client.model_id,
        session=big_extract_session,
        llm_client=client,
    )
    run_entity = big_extract_session.get(ExtractionRun, str(run_id))
    assert run_entity is not None
    assert run_entity.status in ("SUCCEEDED", "FAILED")
    if run_entity.status == "SUCCEEDED":
        assert run_entity.stats_json
        stats = json.loads(run_entity.stats_json)
        assert "frames_count" in stats
        assert "mentions_count_total" in stats
