"""Stage 2 runner: four passes (ACTOR, OBJECT, STATE, ACTION), seed loop, LLM + apply + audit."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.db.session import session_scope
from app.llm.client_litellm import LiteLLMClient
from app.llm.providers import completion_kwargs_for_provider
from app.llm.settings import LLMSettings
from app.llm.types import LLMMessage, LLMRequest, LLMProvider
from app.stage1.repo import Stage1ClaimRepo, Stage1LlmCallRepo, Stage1RunRepo, get_workspace_id_for_document
from app.stage2.context_pack import ContextPackBuilder
from app.stage2.decision_applier import apply_decision
from app.stage2.prompt_builder import build_messages
from app.stage2.qdrant import (
    STAGE1_CANONICALS_COLLECTION,
    delete_claim_points,
    upsert_claim_to_canonicals,
)
from app.stage2.schema import (
    DecisionBlock,
    EvidenceRef,
    Stage2DecisionOutput,
    Stage2DecisionOutputLLM,
)

logger = logging.getLogger(__name__)

STAGE2_RUN_KIND = "STAGE2_NORMALIZE"
PASS_ORDER = ("ACTOR", "OBJECT", "STATE", "ACTION")


def _provider_from_model_id(model_id: str) -> LLMProvider:
    prefix = (model_id or "").split("/")[0].lower()
    if prefix == "gemini" or prefix.startswith("gemini/"):
        return LLMProvider.GEMINI
    return LLMProvider.OLLAMA


def _strip_json_block(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def _is_empty_or_trivial_response(raw_text: str) -> bool:
    """True if the LLM returned nothing useful (empty, whitespace, or trivial JSON like {})."""
    cleaned = _strip_json_block(raw_text or "")
    if not cleaned:
        return True
    if cleaned in ("{}", "null", "[]"):
        return True
    try:
        data = json.loads(cleaned)
        return isinstance(data, dict) and len(data) == 0
    except Exception:
        return False


def _parse_decision_llm(raw_text: str) -> Stage2DecisionOutputLLM | None:
    """Parse LLM response into Stage2DecisionOutputLLM (no seed_claim_id, snippet-only evidence_refs)."""
    try:
        cleaned = _strip_json_block(raw_text)
        data = json.loads(cleaned)
        return Stage2DecisionOutputLLM.model_validate(data)
    except Exception:
        return None


def _resolve_evidence_refs(
    snippet_refs: list[dict[str, Any]],
    resolution_map: list[dict[str, Any]],
) -> list[EvidenceRef]:
    """Match each snippet to resolution map; return list of EvidenceRef (claim_id, evidence_id, chunk_id, snippet)."""
    out = []
    for ref in snippet_refs:
        snippet = (ref.get("snippet") or "").strip()
        matched = None
        for r in resolution_map:
            st = (r.get("snippet_text") or "").strip()
            if st == snippet:
                matched = r
                break
        if not matched:
            # Keep snippet for audit; IDs left None, claim_id empty
            out.append(
                EvidenceRef(claim_id="", evidence_id=None, chunk_id=None, snippet=snippet or None)
            )
            continue
        out.append(
            EvidenceRef(
                claim_id=matched.get("claim_id") or "",
                evidence_id=matched.get("evidence_id"),
                chunk_id=matched.get("chunk_id"),
                snippet=snippet or matched.get("snippet_text"),
            )
        )
    return out


def _llm_to_internal(
    llm: Stage2DecisionOutputLLM,
    seed_claim_id: str,
    pass_kind: str,
    resolution_map: list[dict[str, Any]],
    pack: dict[str, Any] | None = None,
) -> Stage2DecisionOutput:
    """Build internal decision: pass_kind and seed_claim_id from context; canonical_claim_id from pack when kind==MERGE_INTO."""
    resolved = _resolve_evidence_refs(
        [r.model_dump() for r in llm.decision.evidence_refs],
        resolution_map or [],
    )
    canonical_claim_id = None
    if llm.decision.kind == "MERGE_INTO" and pack:
        closest = pack.get("closest_canonical")
        if closest:
            canonical_claim_id = closest.get("claim_id")
    decision = DecisionBlock(
        kind=llm.decision.kind,
        canonical_claim_id=canonical_claim_id,
        evidence_refs=resolved,
    )
    return Stage2DecisionOutput(
        pass_kind=pass_kind,
        seed_claim_id=seed_claim_id,
        decision=decision,
        attachments=llm.attachments,
    )


async def _process_one_seed(
    seed_claim_id: str,
    pass_kind: str,
    stage1_run_id: str,
    document_id: str,
    stage2_run_id: str,
    model_id: str,
    *,
    claim_repo: Stage1ClaimRepo,
    llm_repo: Stage1LlmCallRepo,
    collection_name: str = "stage1_cards",
    vector_size: int = 768,
    timeout_s: float = 120.0,
    max_tokens: int = 2048,
) -> tuple[bool, str | None]:
    """
    Build context pack, call LLM, parse, apply, audit. Uses session_scope internally.
    Returns (success, error_message). On success, decision is applied and LlmCall created.
    """
    with session_scope() as session:
        builder = ContextPackBuilder(
            session,
            claim_repo=claim_repo,
            collection_name=collection_name,
            vector_size=vector_size,
        )
        pack = await builder.build(seed_claim_id, pass_kind, stage1_run_id, document_id)
        if not pack:
            return False, "Seed claim not found or wrong type"

    messages = build_messages(pass_kind, pack)
    request_payload = {
        "pass_kind": pass_kind,
        "seed_claim_id": seed_claim_id,
        "system_prompt": messages[0]["content"],
        "user_prompt": messages[1]["content"],
    }
    client = LiteLLMClient(concurrency_limit=4, max_retries=2)
    provider = _provider_from_model_id(model_id)
    req = LLMRequest(
        messages=[LLMMessage(role=m["role"], content=m["content"]) for m in messages],
        temperature=0.1,
        max_output_tokens=max_tokens,
        response_format={"type": "json_object"},
        cache_system_prompt=True,
    )
    completion_kwargs = completion_kwargs_for_provider(provider, LLMSettings())

    try:
        resp = await client.acompletion(
            provider, model_id, req, timeout_s=timeout_s, **completion_kwargs
        )
    except Exception as e:
        logger.warning("Stage2 LLM failed for seed %s (%s): %s", seed_claim_id, pass_kind, e)
        with session_scope() as session:
            llm_repo.create(
                session,
                run_id=stage2_run_id,
                request_json=json.dumps(request_payload, ensure_ascii=False),
                response_text=None,
                status="FAILED",
                error_message=str(e),
            )
        return False, str(e)

    raw_text = resp.text or ""
    usage = resp.usage
    latency_ms = resp.latency_ms or 0
    usage_json = json.dumps({
        "prompt_tokens": usage.input_tokens if usage else 0,
        "completion_tokens": usage.output_tokens if usage else 0,
        "total_tokens": usage.total_tokens if usage else 0,
        "latency_ms": latency_ms,
    }) if usage else None

    if _is_empty_or_trivial_response(raw_text):
        logger.warning(
            "Stage2 LLM returned empty or trivial JSON for seed %s (%s); check request_json for prompt.",
            seed_claim_id,
            pass_kind,
        )
        with session_scope() as session:
            llm_repo.create(
                session,
                run_id=stage2_run_id,
                chunk_id=None,
                request_json=json.dumps(request_payload, ensure_ascii=False),
                response_text=raw_text,
                response_json=None,
                status="PARSE_FAILED",
                latency_ms=latency_ms,
                prompt_tokens=usage.input_tokens if usage else None,
                completion_tokens=usage.output_tokens if usage else None,
                error_message="LLM returned empty or trivial JSON",
            )
        return False, "Empty or trivial response"

    llm_decision = _parse_decision_llm(raw_text)
    if not llm_decision:
        logger.warning("Stage2 parse failed for seed %s (%s)", seed_claim_id, pass_kind)
        with session_scope() as session:
            llm_repo.create(
                session,
                run_id=stage2_run_id,
                chunk_id=None,
                request_json=json.dumps(request_payload, ensure_ascii=False),
                response_text=raw_text,
                response_json=None,
                status="PARSE_FAILED",
                latency_ms=latency_ms,
                prompt_tokens=usage.input_tokens if usage else None,
                completion_tokens=usage.output_tokens if usage else None,
                error_message="Output did not match Stage2 decision schema",
            )
        return False, "Parse failed"

    # We set seed_claim_id, pass_kind, and canonical_claim_id (from pack when MERGE_INTO); evidence_refs resolved from pack.
    resolution_map = pack.get("_resolution") or []
    decision = _llm_to_internal(llm_decision, seed_claim_id, pass_kind, resolution_map, pack)

    with session_scope() as session:
        apply_decision(session, decision, claim_repo=claim_repo)
        llm_repo.create(
            session,
            run_id=stage2_run_id,
            chunk_id=None,
            provider=provider.value,
            model=model_id,
            request_json=json.dumps(request_payload, ensure_ascii=False),
            response_text=raw_text,
            response_json=decision.model_dump_json(),
            latency_ms=latency_ms,
            prompt_tokens=usage.input_tokens if usage else None,
            completion_tokens=usage.output_tokens if usage else None,
            status="SUCCESS",
        )

    # Push accepted claim to canonicals collection so it can be used as closest canonical later
    if decision.decision.kind == "ACCEPT_AS_CANONICAL":
        await upsert_claim_to_canonicals(
            seed_claim_id,
            collection_name_cards=collection_name,
            collection_name_canonicals=STAGE1_CANONICALS_COLLECTION,
            vector_size=vector_size,
        )
    elif decision.decision.kind == "MERGE_INTO":
        if decision.decision.canonical_claim_id:
            await upsert_claim_to_canonicals(
                decision.decision.canonical_claim_id,
                collection_name_cards=collection_name,
                collection_name_canonicals=STAGE1_CANONICALS_COLLECTION,
                vector_size=vector_size,
            )
        else:
            # Override: MERGE_INTO with no canonical → we accepted seed as canonical
            await upsert_claim_to_canonicals(
                seed_claim_id,
                collection_name_cards=collection_name,
                collection_name_canonicals=STAGE1_CANONICALS_COLLECTION,
                vector_size=vector_size,
            )

    # Remove from Qdrant claims that are no longer candidates (REJECT / MERGE_INTO)
    if decision.decision.kind in ("REJECT", "MERGE_INTO"):
        await delete_claim_points(collection_name, [seed_claim_id])

    return True, None


async def _run_pass_async(
    pass_kind: str,
    stage1_run_id: str,
    document_id: str,
    stage2_run_id: str,
    model_id: str,
    *,
    claim_repo: Stage1ClaimRepo,
    llm_repo: Stage1LlmCallRepo,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one pass (e.g. ACTOR). Returns stats dict."""
    with session_scope() as session:
        seeds = claim_repo.list_claims_unreviewed_by_type(
            session, stage1_run_id, pass_kind, embedded_only=True
        )
        seed_ids = [c.id for c in seeds]

    processed = 0
    failed = 0
    for seed_id in seed_ids:
        ok, _ = await _process_one_seed(
            seed_id,
            pass_kind,
            stage1_run_id,
            document_id,
            stage2_run_id,
            model_id,
            claim_repo=claim_repo,
            llm_repo=llm_repo,
            **kwargs,
        )
        if ok:
            processed += 1
        else:
            failed += 1

    return {"pass": pass_kind, "seeds": len(seed_ids), "processed": processed, "failed": failed}


def run_stage2(
    stage1_run_id: str,
    model_id: str,
    *,
    collection_name: str = "stage1_cards",
    vector_size: int = 768,
    timeout_s: float = 120.0,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """
    Run Stage-2 normalization on claims from the given Stage-1 run.
    Creates a new pipeline run with run_kind=STAGE2_NORMALIZE and runs four passes in order.
    Returns dict with stage2_run_id, status, stats (per-pass and total).
    """
    claim_repo = Stage1ClaimRepo()
    llm_repo = Stage1LlmCallRepo()
    run_repo = Stage1RunRepo()

    with session_scope() as session:
        from app.db.models.pipeline_run import PipelineRun
        run = session.get(PipelineRun, stage1_run_id)
        if not run:
            return {"error": "Stage-1 run not found", "stage2_run_id": None}
        document_id = run.document_id
        workspace_id = run.workspace_id or get_workspace_id_for_document(session, document_id)
        if not workspace_id:
            return {"error": "Workspace not found for document", "stage2_run_id": None}

        stage2_run = run_repo.create(
            session,
            workspace_id=workspace_id,
            document_id=document_id,
            config={"stage1_run_id": stage1_run_id},
            prompt_version="stage2_normalize",
            extractor_version="stage2",
            model_id=model_id,
            run_kind=STAGE2_RUN_KIND,
        )
        stage2_run_id = stage2_run.id

    kwargs = {
        "collection_name": collection_name,
        "vector_size": vector_size,
        "timeout_s": timeout_s,
        "max_tokens": max_tokens,
    }

    async def _run_all() -> dict[str, Any]:
        stats_per_pass = []
        for pass_kind in PASS_ORDER:
            s = await _run_pass_async(
                pass_kind,
                stage1_run_id,
                document_id,
                stage2_run_id,
                model_id,
                claim_repo=claim_repo,
                llm_repo=llm_repo,
                **kwargs,
            )
            stats_per_pass.append(s)

        total_processed = sum(s["processed"] for s in stats_per_pass)
        total_failed = sum(s["failed"] for s in stats_per_pass)
        return {
            "stage2_run_id": stage2_run_id,
            "status": "COMPLETED",
            "stats": {
                "per_pass": stats_per_pass,
                "total_processed": total_processed,
                "total_failed": total_failed,
            },
        }

    try:
        result = asyncio.run(_run_all())
    except Exception as e:
        logger.exception("Stage2 run failed: %s", e)
        with session_scope() as session:
            run_repo.finalize(
                session,
                stage2_run_id,
                "FAILED",
                {},
                error_summary=str(e),
            )
        return {
            "stage2_run_id": stage2_run_id,
            "status": "FAILED",
            "error": str(e),
            "stats": None,
        }

    with session_scope() as session:
        run_repo.finalize(
            session,
            stage2_run_id,
            "COMPLETED",
            result["stats"],
        )

    return result
