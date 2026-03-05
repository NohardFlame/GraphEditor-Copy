"""Big-LLM extraction runner: run_big_llm_extract (API path) and manual path helpers.

run_big_llm_extract: full flow with injectable llm_client (for when API key is available).
prepare_big_llm_export / ingest_big_llm_results: manual path (prepare files, user runs LLM, ingest).
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from app.db.document_helpers import workspace_id_for_document
from app.db.models.claim import LlmCall
from app.db.models.extraction import ExtractionRun
from app.extraction.big_extract_pipeline import run_downstream_pipeline
from app.extraction.constants import (
    BIG_LLM_EXTRACT,
    CANONICALIZE_ACTIONS,
    CANONICALIZE_ACTORS,
    CANONICALIZE_OBJECT_STATES,
    CANONICALIZE_OBJECTS,
)
from app.extraction.big_extract_prompts import (
    PROMPT_VERSION_BIG_EXTRACT,
    build_big_extract_prompts,
)
from app.extraction.marked_doc_builder import load_chunks_for_document
from app.extraction.service import prepare_marked_docs_for_extraction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# Re-export for backward compatibility
__all__ = [
    "BIG_LLM_EXTRACT",
    "CANONICALIZE_ACTIONS",
    "CANONICALIZE_ACTORS",
    "CANONICALIZE_OBJECT_STATES",
    "CANONICALIZE_OBJECTS",
    "rerun_big_extract",
    "run_big_llm_extract",
    "prepare_big_llm_export",
    "ingest_big_llm_results",
    "ingest_file_and_json",
]


def _workspace_id_for_document(doc_id: str, session: Session) -> str:
    return workspace_id_for_document(doc_id, session)


class BigLlmExtractClient(Protocol):
    """Protocol for LLM completion used by run_big_llm_extract."""

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model_id: str,
    ) -> str:
        """Return raw response text. May raise if no API key or on error."""
        ...


def _compute_input_hash(doc_id: str, prompt_version: str, model_id: str, session: Session) -> str:
    """Fingerprint for idempotency: document content + prompt_version + model_id."""
    doc_parts = prepare_marked_docs_for_extraction(doc_id, session=session)
    if not doc_parts:
        return hashlib.sha256(
            f"{doc_id}:{prompt_version}:{model_id}:empty".encode()
        ).hexdigest()
    parts = [f"{p.part_index}:{p.first_chunk_index}:{p.last_chunk_index}:{len(p.text or '')}" for p in doc_parts]
    content = "|".join(parts)
    return hashlib.sha256(
        f"{doc_id}:{prompt_version}:{model_id}:{content}".encode()
    ).hexdigest()


def rerun_big_extract(
    doc_id: UUID | str,
    model_id: str,
    *,
    session: Session,
    llm_client: BigLlmExtractClient | None = None,
    prompt_version: str | None = None,
    force: bool = False,
) -> UUID:
    """Re-run big-LLM extraction. If force=False and a SUCCEEDED run exists with same input_hash, returns it.

    Otherwise creates a new run and runs extraction. Use --force to bypass cache, --prompt-version/--model-id to try new config.
    """
    from sqlalchemy import select

    doc_id_str = str(doc_id)
    pv = prompt_version or PROMPT_VERSION_BIG_EXTRACT
    input_hash = _compute_input_hash(doc_id_str, pv, model_id, session)

    if not force:
        existing = session.execute(
            select(ExtractionRun).where(
                ExtractionRun.document_id == doc_id_str,
                ExtractionRun.run_kind == BIG_LLM_EXTRACT,
                ExtractionRun.input_hash == input_hash,
                ExtractionRun.status == "SUCCEEDED",
            ).limit(1)
        ).scalar_one_or_none()
        if existing:
            return UUID(existing.id)

    return run_big_llm_extract(
        doc_id,
        model_id,
        session=session,
        llm_client=llm_client,
        prompt_version_override=pv,
        force_new_run=True,
    )


def run_big_llm_extract(
    doc_id: UUID | str,
    model_id: str,
    *,
    session: Session,
    llm_client: BigLlmExtractClient | None = None,
    prompt_version_override: str | None = None,
    force_new_run: bool = False,
) -> UUID:
    """Run full big-LLM extraction: create run, build parts, call LLM per part, parse, validate, persist.

    Returns run_id. If llm_client is None or not configured, the client may raise (e.g. no API key).
    When force_new_run=True, input_hash is set to None so a new run can be created (for replay with force).
    """
    doc_id_str = str(doc_id)
    workspace_id = _workspace_id_for_document(doc_id_str, session)
    pv = prompt_version_override or PROMPT_VERSION_BIG_EXTRACT
    input_hash = None if force_new_run else _compute_input_hash(doc_id_str, pv, model_id, session)

    run_entity = ExtractionRun(
        id=str(uuid4()),
        workspace_id=workspace_id,
        document_id=doc_id_str,
        run_kind=BIG_LLM_EXTRACT,
        prompt_version=pv,
        model_id=model_id,
        status="RUNNING",
        input_hash=input_hash,
    )
    session.add(run_entity)
    session.commit()

    parts = prepare_marked_docs_for_extraction(doc_id_str, session=session)
    if not parts:
        run_entity.status = "FAILED"
        run_entity.stats_json = json.dumps({"error": "no chunks for document"})
        session.add(run_entity)
        session.commit()
        return UUID(run_entity.id)

    raw_responses: list[str] = []
    for i, part in enumerate(parts):
        system_prompt, user_prompt = build_big_extract_prompts(part, doc_id_str)
        if llm_client is None:
            raise RuntimeError(
                "run_big_llm_extract requires llm_client when calling LLM; "
                "use prepare_big_llm_export + ingest_big_llm_results for manual path."
            )
        response_text = llm_client.complete(system_prompt, user_prompt, model_id)
        raw_responses.append(response_text)
        llm_call = LlmCall(
            run_id=None,
            extraction_run_id=run_entity.id,
            chunk_id=None,
            model=model_id,
            prompt_version=pv,
            request_json=json.dumps({"part_index": i, "prompt_version": PROMPT_VERSION_BIG_EXTRACT}),
            response_text=response_text,
            status="SUCCESS",
        )
        session.add(llm_call)
    session.commit()

    run_downstream_pipeline(
        raw_responses,
        doc_id_str,
        run_entity.id,
        run_entity,
        session,
    )
    return UUID(run_entity.id)


def prepare_big_llm_export(
    doc_id: UUID | str,
    model_id: str,
    *,
    output_dir: Path,
    session: Session,
    input_hash_override: str | None = None,
) -> tuple[UUID, list[Path], Path]:
    """Prepare document for manual LLM run: create extraction_run, write prompt+doc files and manifest.

    Returns (run_id, list of exported part file paths, path to manifest.json).
    User runs the LLM with the provided prompt and document, then calls ingest_big_llm_results(run_id, result_paths).

    input_hash_override: if set, use this instead of computed input_hash (e.g. to avoid UNIQUE
    collision when re-running ingest-file-and-json for the same document).
    """
    doc_id_str = str(doc_id)
    workspace_id = _workspace_id_for_document(doc_id_str, session)
    input_hash = (
        input_hash_override
        if input_hash_override is not None
        else _compute_input_hash(doc_id_str, PROMPT_VERSION_BIG_EXTRACT, model_id, session)
    )

    run_entity = ExtractionRun(
        id=str(uuid4()),
        workspace_id=workspace_id,
        document_id=doc_id_str,
        run_kind=BIG_LLM_EXTRACT,
        prompt_version=PROMPT_VERSION_BIG_EXTRACT,
        model_id=model_id,
        status="RUNNING",
        input_hash=input_hash,
    )
    session.add(run_entity)
    session.commit()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    parts = prepare_marked_docs_for_extraction(doc_id_str, session=session)
    if not parts:
        run_entity.status = "FAILED"
        run_entity.stats_json = json.dumps({"error": "no chunks for document"})
        session.add(run_entity)
        session.commit()
        return UUID(run_entity.id), [], output_dir / "manifest.json"

    exported_paths: list[Path] = []
    manifest_parts: list[dict] = []
    for i, part in enumerate(parts):
        system_prompt, user_prompt = build_big_extract_prompts(part, doc_id_str)
        content = (
            "=== SYSTEM ===\n"
            + system_prompt
            + "\n\n=== USER ===\n"
            + user_prompt
            + "\n"
        )
        part_path = output_dir / f"part_{i}.txt"
        part_path.write_text(content, encoding="utf-8")
        exported_paths.append(part_path)
        manifest_parts.append({
            "part_index": i,
            "file": part_path.name,
        })
    manifest = {
        "run_id": run_entity.id,
        "doc_id": doc_id_str,
        "prompt_version": PROMPT_VERSION_BIG_EXTRACT,
        "model_id": model_id,
        "parts": manifest_parts,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return UUID(run_entity.id), exported_paths, manifest_path


def _remap_result_to_run_document(
    raw_json_str: str,
    target_doc_id: str,
    part_chunk_ids_ordered: list[str],
    part_chunk_texts: list[str] | None = None,
) -> str | None:
    """Rewrite doc_id and chunk_ids in LLM JSON to match the run's document.

    When part_chunk_texts is provided (same order as part_chunk_ids_ordered), each frame is
    assigned to the chunk whose text contains the frame's evidence snippet (content-based).
    Otherwise (or when no match), falls back to mapping by order of first appearance.
    Returns None only on parse error or empty part.
    """
    if not part_chunk_ids_ordered:
        return None
    try:
        data = json.loads(raw_json_str.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    frames = data.get("frames")
    if not isinstance(frames, list):
        return None
    # Fallback: order of first appearance -> part_chunk_ids by index
    seen: set[str] = set()
    old_chunk_ids: list[str] = []
    for f in frames:
        if not isinstance(f, dict):
            continue
        cid = f.get("chunk_id")
        if isinstance(cid, str) and cid not in seen:
            seen.add(cid)
            old_chunk_ids.append(cid)
    last_new = part_chunk_ids_ordered[-1]
    old_to_new_fallback: dict[str, str] = {}
    for i, old_id in enumerate(old_chunk_ids):
        old_to_new_fallback[old_id] = (
            part_chunk_ids_ordered[i]
            if i < len(part_chunk_ids_ordered)
            else last_new
        )

    use_content = (
        part_chunk_texts is not None
        and len(part_chunk_texts) == len(part_chunk_ids_ordered)
    )
    data["doc_id"] = target_doc_id

    for f in frames:
        if not isinstance(f, dict) or "chunk_id" not in f:
            continue
        new_cid: str | None = None
        if use_content:
            # Get a search string from this frame: first mention's evidence.snippet or frame_text
            search = None
            if isinstance(f.get("frame_text"), str) and f["frame_text"].strip():
                search = f["frame_text"].strip()[:500]
            for m in f.get("mentions") or []:
                if not isinstance(m, dict):
                    continue
                ev = m.get("evidence")
                if isinstance(ev, dict) and isinstance(ev.get("snippet"), str) and ev["snippet"].strip():
                    search = ev["snippet"].strip()
                    break
            if search:
                for j, chunk_text in enumerate(part_chunk_texts):
                    if search in (chunk_text or ""):
                        new_cid = part_chunk_ids_ordered[j]
                        break
        if new_cid is None:
            new_cid = old_to_new_fallback.get(f["chunk_id"], part_chunk_ids_ordered[0])
        f["chunk_id"] = new_cid

    # Renumber frame_index per chunk so (chunk_id, frame_index) is unique (validator requirement)
    chunk_next_index: dict[str, int] = {}
    for f in frames:
        if not isinstance(f, dict) or "chunk_id" not in f:
            continue
        cid = f.get("chunk_id")
        if cid not in chunk_next_index:
            chunk_next_index[cid] = 0
        f["frame_index"] = chunk_next_index[cid]
        chunk_next_index[cid] += 1

    for ref in data.get("references") or []:
        if isinstance(ref, dict) and "chunk_id" in ref and ref["chunk_id"] in old_to_new_fallback:
            ref["chunk_id"] = old_to_new_fallback[ref["chunk_id"]]
    return json.dumps(data, ensure_ascii=False)


def _read_result_paths_into_responses(
    result_paths: list[Path] | Path,
    expected_part_count: int,
) -> list[str]:
    """Resolve result_paths to list of raw response strings in part order."""
    if isinstance(result_paths, Path):
        single = result_paths
        if single.is_dir():
            paths: list[Path] = []
            for i in range(expected_part_count):
                p = single / f"part_{i}.json"
                if not p.exists():
                    p = single / f"part_{i}.txt"
                if not p.exists():
                    raise ValueError(
                        f"Expected part_{i}.json or part_{i}.txt in {single}"
                    )
                paths.append(p)
            return [p.read_text(encoding="utf-8") for p in paths]
        raw = single.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                if len(data) != expected_part_count:
                    raise ValueError(
                        f"JSON array length {len(data)} != expected {expected_part_count}"
                    )
                return [json.dumps(item) for item in data]
        except json.JSONDecodeError:
            pass
        return [raw]
    paths = list(result_paths)
    if len(paths) != expected_part_count:
        raise ValueError(
            f"Result file count {len(paths)} != expected {expected_part_count}"
        )
    return [Path(p).read_text(encoding="utf-8") for p in paths]


def ingest_big_llm_results(
    run_id: UUID | str,
    result_paths: list[Path] | Path | None = None,
    *,
    session: Session,
    remap_to_run_doc: bool = False,
    raw_responses: list[str] | None = None,
    skip_evidence_validation: bool = False,
) -> None:
    """Read result file(s) from manual LLM run, optionally store in llm_calls, run shared pipeline.

    Either result_paths or raw_responses must be provided (not both semantics: raw_responses wins).

    result_paths: list of paths in part order, or a directory with part_0.json, ..., or a single
    file (JSON array of part responses). Ignored if raw_responses is provided.

    raw_responses: pre-read list of JSON strings (one per part). Use with ingest_file_and_json
    when reusing saved JSON for the same file; doc_id/chunk_ids will have been remapped by caller.

    remap_to_run_doc: if True and result_paths used, rewrite doc_id and chunk_ids in the JSON
    to match the run's document (by part order).

    skip_evidence_validation: if True, do not require evidence.snippet to be a substring of chunk
    text (used by ingest-file-and-json for pre-produced JSON).
    """
    run_id_str = str(run_id)
    run_entity = session.get(ExtractionRun, run_id_str)
    if not run_entity:
        raise ValueError(f"ExtractionRun not found: {run_id_str}")
    if run_entity.run_kind != BIG_LLM_EXTRACT:
        raise ValueError(f"Run {run_id_str} is not BIG_LLM_EXTRACT")
    if run_entity.status not in ("RUNNING", "AWAITING_RESULTS"):
        if remap_to_run_doc and run_entity.status == "FAILED":
            run_entity.status = "RUNNING"
            run_entity.stats_json = None
            session.add(run_entity)
            session.commit()
        else:
            raise ValueError(
                f"Run {run_id_str} status is {run_entity.status}, "
                "expected RUNNING or AWAITING_RESULTS"
            )

    doc_id_str = run_entity.document_id
    parts = prepare_marked_docs_for_extraction(doc_id_str, session=session)
    if not parts:
        run_entity.status = "FAILED"
        run_entity.stats_json = json.dumps({"error": "no chunks for document"})
        session.add(run_entity)
        session.commit()
        return

    from_paths = raw_responses is None
    if raw_responses is not None:
        raw_responses = list(raw_responses)
    else:
        if result_paths is None:
            raise ValueError("Either result_paths or raw_responses must be provided")
        try:
            raw_responses = _read_result_paths_into_responses(
                result_paths, len(parts)
            )
        except ValueError as e:
            run_entity.status = "FAILED"
            run_entity.stats_json = json.dumps({"error": str(e)})
            session.add(run_entity)
            session.commit()
            return

    if len(raw_responses) != len(parts):
        run_entity.status = "FAILED"
        run_entity.stats_json = json.dumps({
            "error": "result count does not match part count",
            "expected": len(parts),
            "got": len(raw_responses),
        })
        session.add(run_entity)
        session.commit()
        return

    if remap_to_run_doc and from_paths:
        chunks = load_chunks_for_document(doc_id_str, session=session)
        remapped: list[str] = []
        for i, raw in enumerate(raw_responses):
            part = parts[i]
            part_chunk_ids = [
                chunks[k].chunk_id
                for k in range(part.first_chunk_index, part.last_chunk_index + 1)
            ]
            new_raw = _remap_result_to_run_document(raw, doc_id_str, part_chunk_ids)
            if new_raw is None:
                run_entity.status = "FAILED"
                run_entity.stats_json = json.dumps({
                    "error": "remap_to_run_doc failed",
                    "part_index": i,
                    "detail": "could not parse JSON or part has no chunks",
                })
                session.add(run_entity)
                session.commit()
                return
            remapped.append(new_raw)
        raw_responses = remapped

    for i, raw in enumerate(raw_responses):
        llm_call = LlmCall(
            run_id=None,
            extraction_run_id=run_entity.id,
            chunk_id=None,
            model=run_entity.model_id,
            prompt_version=run_entity.prompt_version or PROMPT_VERSION_BIG_EXTRACT,
            request_json=json.dumps({"source": "manual_run", "part_index": i}),
            response_text=raw,
            status="SUCCESS",
        )
        session.add(llm_call)
    session.commit()

    run_downstream_pipeline(
        raw_responses,
        doc_id_str,
        run_entity.id,
        run_entity,
        session,
        skip_evidence_validation=skip_evidence_validation,
    )


def _read_json_path_into_responses(json_path: Path, expected_part_count: int) -> list[str]:
    """Read JSON from file or directory into list of part response strings (one per part)."""
    if json_path.is_dir():
        paths = [json_path / f"part_{i}.json" for i in range(expected_part_count)]
        for p in paths:
            if not p.exists():
                raise ValueError(f"Missing {p.name} in {json_path}")
        return [p.read_text(encoding="utf-8") for p in paths]
    # Single file
    text = json_path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {json_path}: {e}") from e
    if isinstance(data, list):
        if len(data) != expected_part_count:
            raise ValueError(
                f"JSON array has {len(data)} parts, expected {expected_part_count}"
            )
        return [json.dumps(item, ensure_ascii=False) for item in data]
    # Single object = one part
    if expected_part_count != 1:
        raise ValueError(
            f"Single JSON object provided but document has {expected_part_count} parts; "
            "provide a directory with part_0.json, ... or a JSON array of part responses"
        )
    return [text]


def ingest_file_and_json(
    file_path: str | Path,
    json_path: str | Path,
    *,
    session: Session,
    workspace_name: str = "manual",
    model_id: str = "saved",
) -> tuple[str, UUID]:
    """Ingest file (Docling chunking), create run, remap saved JSON to new doc/chunk IDs, ingest.

    Use when you have a pre-produced big-extract JSON for a file and want to reuse it without
    re-running the LLM. Docling chunking is deterministic, so chunk order matches; we remap
    doc_id and chunk_ids by position. Returns (doc_id, run_id).

    json_path: path to a single JSON file (one part) or JSON array of part responses, or a
    directory containing part_0.json, part_1.json, ...
    """
    from app.extraction.ingest_file import ingest_file

    file_path = Path(file_path)
    json_path = Path(json_path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not json_path.exists():
        raise FileNotFoundError(f"JSON path not found: {json_path}")

    doc_id = ingest_file(session, file_path, workspace_name=workspace_name)
    parts = prepare_marked_docs_for_extraction(doc_id, session=session)
    if not parts:
        raise ValueError(f"Document {doc_id} has no chunks after ingest")

    # Use a unique input_hash so re-running ingest-file-and-json for the same doc doesn't hit
    # UNIQUE(workspace_id, document_id, run_kind, input_hash).
    base_hash = _compute_input_hash(
        str(doc_id), PROMPT_VERSION_BIG_EXTRACT, model_id, session
    )
    unique_hash = hashlib.sha256(
        f"{base_hash}:ingest_saved:{uuid4().hex}".encode()
    ).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        run_id, _, _ = prepare_big_llm_export(
            doc_id,
            model_id,
            output_dir=Path(tmp),
            session=session,
            input_hash_override=unique_hash,
        )

    raw_list = _read_json_path_into_responses(json_path, len(parts))
    chunks = load_chunks_for_document(doc_id, session=session)
    remapped: list[str] = []
    for i, raw in enumerate(raw_list):
        part = parts[i]
        part_chunk_ids = [
            chunks[k].chunk_id
            for k in range(part.first_chunk_index, part.last_chunk_index + 1)
        ]
        part_chunk_texts = [
            (chunks[k].text or "")
            for k in range(part.first_chunk_index, part.last_chunk_index + 1)
        ]
        new_raw = _remap_result_to_run_document(
            raw, doc_id, part_chunk_ids, part_chunk_texts=part_chunk_texts
        )
        if new_raw is None:
            raise ValueError(
                f"Could not remap part {i} JSON (parse error or invalid structure)"
            )
        remapped.append(new_raw)

    ingest_big_llm_results(
        run_id,
        session=session,
        raw_responses=remapped,
        skip_evidence_validation=True,
    )
    return doc_id, run_id
