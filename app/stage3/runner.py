"""Stage 3 runner: four passes (ACTOR, OBJECT, STATE, ACTION), cards + Qdrant + baked objects."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.db.repositories.chunk_repo import ChunkRepo
from app.db.repositories.stage3_repo import Stage3ObjectBakedRepo, Stage3ResolvedCardRepo
from app.db.session import session_scope
from app.embeddings.ollama import OllamaEmbedClient
from app.embeddings.settings import EmbedSettings
from app.llm.client_litellm import LiteLLMClient
from app.llm.providers import completion_kwargs_for_provider
from app.llm.settings import LLMSettings
from app.llm.types import LLMMessage, LLMRequest, LLMProvider
from app.stage1.repo import (
    Stage1ClaimRepo,
    Stage1LlmCallRepo,
    Stage1RunRepo,
    get_workspace_id_for_document,
)
from app.stage3.evidence_windows import build_evidence_windows
from app.stage3.prompt_builder import build_messages
from app.stage3.qdrant import (
    STAGE3_CARDS_COLLECTION,
    _stage3_point_id,
    ensure_collection as ensure_stage3_collection,
    search_candidate_actors_and_objects,
    search_candidate_objects,
    upsert_card as qdrant_upsert_card,
)
from app.stage3.schema import (
    parse_action_card,
    parse_actor_card,
    parse_object_card,
    parse_state_card,
)

logger = logging.getLogger(__name__)

STAGE3_RUN_KIND = "STAGE3_ASSEMBLE"
PASS_ORDER = ("ACTOR", "OBJECT", "STATE", "ACTION")

SUMMARY_MIN_LENGTH = 10
FALLBACK_SUMMARY_MAX_CHARS = 500


def _provider_from_model_id(model_id: str) -> LLMProvider:
    prefix = (model_id or "").split("/")[0].lower()
    if prefix == "gemini" or prefix.startswith("gemini/"):
        return LLMProvider.GEMINI
    return LLMProvider.OLLAMA


def _strip_json_block(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


def _repair_json(raw: str) -> dict[str, Any] | None:
    """Try direct parse, then strip fence and parse again."""
    cleaned = _strip_json_block(raw)
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    if len(cleaned) > 2000:
        cleaned = cleaned[:1000] + "\n..." + cleaned[-500:]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _build_resolved_with_canonical(
    parsed: dict[str, Any],
    kind: str,
    canonical_claim_id: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    """Add canonical id/name from claim to resolved card (actor/object)."""
    out = dict(parsed)
    out["canonical_claim_id"] = canonical_claim_id
    if kind in ("ACTOR", "OBJECT"):
        name = (value.get("name") or "").strip()
        if name:
            out["name"] = name
    return out


async def _process_actor_card(
    canonical_claim_id: str,
    document_id: str,
    stage3_run_id: str,
    model_id: str,
    prompt_version: str,
    *,
    claim_repo: Stage1ClaimRepo,
    llm_repo: Stage1LlmCallRepo,
    card_repo: Stage3ResolvedCardRepo,
    chunk_repo: ChunkRepo,
    collection_name: str,
    vector_size: int,
    embedding_model_id: str,
    timeout_s: float,
    max_tokens: int,
) -> tuple[bool, str | None]:
    """Build windows, call LLM, parse, store, embed+upsert. Returns (success, error_message)."""
    with session_scope() as session:
        claim = claim_repo.get_claim_with_evidence(session, canonical_claim_id)
        if not claim:
            return False, "Claim not found"
        if claim.claim_type != "ACTOR":
            return False, f"Claim type is {claim.claim_type}, not ACTOR"
        evidence_count = len(claim.evidence or [])
        claim_type = claim.claim_type
        value = json.loads(claim.value_json) if isinstance(claim.value_json, str) else {}
        windows = build_evidence_windows(session, claim, chunk_repo)

    if not windows:
        logger.warning(
            "Stage3 actor card skipped: claim %s evidence_count=%s windows_count=0 (no chunk fallback?)",
            canonical_claim_id,
            evidence_count,
        )
        return False, "No evidence windows"

    messages = build_messages(
        "ACTOR",
        evidence_windows=windows,
        canonical_claim_id=canonical_claim_id,
        claim_type=claim_type,
        value=value,
    )
    request_payload = {
        "kind": "ACTOR",
        "canonical_claim_id": canonical_claim_id,
        "system": messages[0]["content"],
        "user": messages[1]["content"],
        "user_len": len(messages[1]["content"]),
    }
    client = LiteLLMClient(concurrency_limit=4, max_retries=2)
    provider = _provider_from_model_id(model_id)
    req = LLMRequest(
        messages=[LLMMessage(role=m["role"], content=m["content"]) for m in messages],
        temperature=0.1,
        max_output_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    completion_kwargs = completion_kwargs_for_provider(provider, LLMSettings())

    try:
        resp = await client.acompletion(
            provider, model_id, req, timeout_s=timeout_s, **completion_kwargs
        )
    except Exception as e:
        logger.warning("Stage3 LLM failed for actor %s: %s", canonical_claim_id, e)
        with session_scope() as session:
            llm_repo.create(
                session,
                run_id=stage3_run_id,
                request_json=json.dumps(request_payload, ensure_ascii=False),
                response_text=None,
                status="FAILED",
                error_message=str(e),
            )
        return False, str(e)

    raw_text = resp.text or ""
    usage = resp.usage
    latency_ms = resp.latency_ms or 0

    data = _repair_json(raw_text)
    if not data:
        parse_status = "PARSE_FAILED"
        resolved = {"summary": (raw_text or "(no output)")[:FALLBACK_SUMMARY_MAX_CHARS]}
    else:
        resolved = parse_actor_card(data)
        if len((resolved.get("summary") or "").strip()) < SUMMARY_MIN_LENGTH:
            parse_status = "SUCCESS_WITH_WARNINGS"
        else:
            parse_status = "SUCCESS"
    resolved = _build_resolved_with_canonical(resolved, "ACTOR", canonical_claim_id, value)
    resolved_json_str = json.dumps(resolved, ensure_ascii=False)
    evidence_refs = json.dumps([{"chunk_id": w.get("chunk_id"), "snippet": w.get("snippet")} for w in windows])

    with session_scope() as session:
        card_repo.upsert(
            session,
            document_id=document_id,
            run_id=stage3_run_id,
            canonical_claim_id=canonical_claim_id,
            kind="ACTOR",
            resolved_json=resolved_json_str,
            raw_response=raw_text,
            parse_status=parse_status,
            prompt_version=prompt_version,
            model_id=model_id,
            embedding_model_id=embedding_model_id,
            evidence_refs=evidence_refs,
        )
        llm_repo.create(
            session,
            run_id=stage3_run_id,
            provider=provider.value,
            model=model_id,
            request_json=json.dumps(request_payload, ensure_ascii=False),
            response_text=raw_text,
            response_json=resolved_json_str,
            latency_ms=latency_ms,
            prompt_tokens=usage.input_tokens if usage else None,
            completion_tokens=usage.output_tokens if usage else None,
            status="SUCCESS" if parse_status != "PARSE_FAILED" else "PARSE_FAILED",
        )

    embed_client = OllamaEmbedClient()
    embed_settings = EmbedSettings()
    dims = vector_size or embed_settings.dims
    card_text = f"ACTOR: {value.get('name', '')}. Summary: {resolved.get('summary', '')}"
    try:
        await ensure_stage3_collection(collection_name=collection_name, vector_size=dims)
        vectors = await embed_client.embed_texts([card_text])
        if vectors and len(vectors[0]) == dims:
            await qdrant_upsert_card(
                _stage3_point_id(canonical_claim_id, "ACTOR"),
                vectors[0],
                document_id,
                canonical_claim_id,
                "ACTOR",
                collection_name=collection_name,
                prompt_version=prompt_version,
                model_id=model_id,
                embedding_model_id=embedding_model_id,
            )
    except Exception as e:
        logger.warning("Stage3 embed/upsert failed for actor %s: %s", canonical_claim_id, e)

    return True, None


async def _process_object_card(
    canonical_claim_id: str,
    document_id: str,
    stage3_run_id: str,
    model_id: str,
    prompt_version: str,
    *,
    claim_repo: Stage1ClaimRepo,
    llm_repo: Stage1LlmCallRepo,
    card_repo: Stage3ResolvedCardRepo,
    baked_repo: Stage3ObjectBakedRepo,
    chunk_repo: ChunkRepo,
    collection_name: str,
    vector_size: int,
    embedding_model_id: str,
    timeout_s: float,
    max_tokens: int,
) -> tuple[bool, str | None]:
    """Like actor; also create stage3_objects_baked with states=[]."""
    with session_scope() as session:
        claim = claim_repo.get_claim_with_evidence(session, canonical_claim_id)
        if not claim or claim.claim_type != "OBJECT":
            return False, "Claim not found or not OBJECT"
        claim_type = claim.claim_type
        value = json.loads(claim.value_json) if isinstance(claim.value_json, str) else {}
        windows = build_evidence_windows(session, claim, chunk_repo)

    if not windows:
        logger.warning(
            "Stage3 object card skipped: claim %s has no evidence and no chunk fallback",
            canonical_claim_id,
        )
        return False, "No evidence windows"

    messages = build_messages(
        "OBJECT",
        evidence_windows=windows,
        canonical_claim_id=canonical_claim_id,
        claim_type=claim_type,
        value=value,
    )
    request_payload = {
        "kind": "OBJECT",
        "canonical_claim_id": canonical_claim_id,
        "system": messages[0]["content"],
        "user": messages[1]["content"],
        "user_len": len(messages[1]["content"]),
    }
    client = LiteLLMClient(concurrency_limit=4, max_retries=2)
    provider = _provider_from_model_id(model_id)
    req = LLMRequest(
        messages=[LLMMessage(role=m["role"], content=m["content"]) for m in messages],
        temperature=0.1,
        max_output_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    completion_kwargs = completion_kwargs_for_provider(provider, LLMSettings())

    try:
        resp = await client.acompletion(
            provider, model_id, req, timeout_s=timeout_s, **completion_kwargs
        )
    except Exception as e:
        logger.warning("Stage3 LLM failed for object %s: %s", canonical_claim_id, e)
        with session_scope() as session:
            llm_repo.create(
                session,
                run_id=stage3_run_id,
                request_json=json.dumps(request_payload, ensure_ascii=False),
                response_text=None,
                status="FAILED",
                error_message=str(e),
            )
        return False, str(e)

    raw_text = resp.text or ""
    usage = resp.usage
    latency_ms = resp.latency_ms or 0

    data = _repair_json(raw_text)
    if not data:
        parse_status = "PARSE_FAILED"
        resolved = {"summary": (raw_text or "(no output)")[:FALLBACK_SUMMARY_MAX_CHARS]}
    else:
        resolved = parse_object_card(data)
        if len((resolved.get("summary") or "").strip()) < SUMMARY_MIN_LENGTH:
            parse_status = "SUCCESS_WITH_WARNINGS"
        else:
            parse_status = "SUCCESS"
    resolved = _build_resolved_with_canonical(resolved, "OBJECT", canonical_claim_id, value)
    resolved["states"] = []
    resolved_json_str = json.dumps(resolved, ensure_ascii=False)
    evidence_refs = json.dumps([{"chunk_id": w.get("chunk_id"), "snippet": w.get("snippet")} for w in windows])

    with session_scope() as session:
        card_repo.upsert(
            session,
            document_id=document_id,
            run_id=stage3_run_id,
            canonical_claim_id=canonical_claim_id,
            kind="OBJECT",
            resolved_json=resolved_json_str,
            raw_response=raw_text,
            parse_status=parse_status,
            prompt_version=prompt_version,
            model_id=model_id,
            embedding_model_id=embedding_model_id,
            evidence_refs=evidence_refs,
        )
        baked_repo.upsert(
            session,
            document_id=document_id,
            run_id=stage3_run_id,
            canonical_claim_id=canonical_claim_id,
            object_json=resolved_json_str,
        )
        llm_repo.create(
            session,
            run_id=stage3_run_id,
            provider=provider.value,
            model=model_id,
            request_json=json.dumps(request_payload, ensure_ascii=False),
            response_text=raw_text,
            response_json=resolved_json_str,
            latency_ms=latency_ms,
            prompt_tokens=usage.input_tokens if usage else None,
            completion_tokens=usage.output_tokens if usage else None,
            status="SUCCESS" if parse_status != "PARSE_FAILED" else "PARSE_FAILED",
        )

    embed_client = OllamaEmbedClient()
    embed_settings = EmbedSettings()
    dims = vector_size or embed_settings.dims
    card_text = f"OBJECT: {value.get('name', '')}. Summary: {resolved.get('summary', '')}"
    try:
        await ensure_stage3_collection(collection_name=collection_name, vector_size=dims)
        vectors = await embed_client.embed_texts([card_text])
        if vectors and len(vectors[0]) == dims:
            await qdrant_upsert_card(
                _stage3_point_id(canonical_claim_id, "OBJECT"),
                vectors[0],
                document_id,
                canonical_claim_id,
                "OBJECT",
                collection_name=collection_name,
                prompt_version=prompt_version,
                model_id=model_id,
                embedding_model_id=embedding_model_id,
            )
    except Exception as e:
        logger.warning("Stage3 embed/upsert failed for object %s: %s", canonical_claim_id, e)

    return True, None


async def _process_state_card(
    canonical_claim_id: str,
    document_id: str,
    stage3_run_id: str,
    model_id: str,
    prompt_version: str,
    *,
    claim_repo: Stage1ClaimRepo,
    llm_repo: Stage1LlmCallRepo,
    card_repo: Stage3ResolvedCardRepo,
    baked_repo: Stage3ObjectBakedRepo,
    chunk_repo: ChunkRepo,
    collection_name: str,
    vector_size: int,
    timeout_s: float,
    max_tokens: int,
) -> tuple[bool, str | None]:
    """Build windows, get candidate objects from Qdrant (embed state evidence), call LLM, store, append state to objects."""
    with session_scope() as session:
        claim = claim_repo.get_claim_with_evidence(session, canonical_claim_id)
        if not claim or claim.claim_type != "STATE":
            return False, "Claim not found or not STATE"
        windows = build_evidence_windows(session, claim, chunk_repo)

    if not windows:
        logger.warning(
            "Stage3 state card skipped: claim %s has no evidence and no chunk fallback",
            canonical_claim_id,
        )
        return False, "No evidence windows"

    query_text = " ".join((w.get("window_text") or "") for w in windows)[:2000]
    embed_client = OllamaEmbedClient()
    embed_settings = EmbedSettings()
    dims = vector_size or embed_settings.dims
    try:
        await ensure_stage3_collection(collection_name=collection_name, vector_size=dims)
        vectors = await embed_client.embed_texts([query_text])
        if not vectors or len(vectors[0]) != dims:
            return False, "Embed failed for state query"
        query_vector = vectors[0]
    except Exception as e:
        logger.warning("Stage3 embed for state query failed: %s", e)
        return False, str(e)

    hits = await search_candidate_objects(query_vector, document_id, collection_name=collection_name, limit=15)
    candidate_objects: list[dict[str, Any]] = []
    with session_scope() as session:
        for h in hits:
            row = card_repo.get(session, stage3_run_id, h.canonical_claim_id, "OBJECT")
            summary = ""
            if row and row.resolved_json:
                try:
                    data = json.loads(row.resolved_json)
                    summary = (data.get("summary") or "").strip()
                except Exception:
                    pass
            candidate_objects.append({
                "object_id": h.canonical_claim_id,
                "canonical_claim_id": h.canonical_claim_id,
                "summary": summary,
            })

    messages = build_messages(
        "STATE",
        evidence_windows=windows,
        candidate_objects=candidate_objects,
    )
    request_payload = {
        "kind": "STATE",
        "canonical_claim_id": canonical_claim_id,
        "system": messages[0]["content"],
        "user": messages[1]["content"],
        "user_len": len(messages[1]["content"]),
    }
    client = LiteLLMClient(concurrency_limit=4, max_retries=2)
    provider = _provider_from_model_id(model_id)
    req = LLMRequest(
        messages=[LLMMessage(role=m["role"], content=m["content"]) for m in messages],
        temperature=0.1,
        max_output_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    completion_kwargs = completion_kwargs_for_provider(provider, LLMSettings())

    try:
        resp = await client.acompletion(
            provider, model_id, req, timeout_s=timeout_s, **completion_kwargs
        )
    except Exception as e:
        logger.warning("Stage3 LLM failed for state %s: %s", canonical_claim_id, e)
        with session_scope() as session:
            llm_repo.create(
                session,
                run_id=stage3_run_id,
                request_json=json.dumps(request_payload, ensure_ascii=False),
                response_text=None,
                status="FAILED",
                error_message=str(e),
            )
        return False, str(e)

    raw_text = resp.text or ""
    usage = resp.usage
    latency_ms = resp.latency_ms or 0

    data = _repair_json(raw_text)
    if not data:
        parse_status = "PARSE_FAILED"
        resolved = {"summary": (raw_text or "(no output)")[:FALLBACK_SUMMARY_MAX_CHARS], "candidate_objects": []}
    else:
        resolved = parse_state_card(data)
        if len((resolved.get("summary") or "").strip()) < SUMMARY_MIN_LENGTH:
            parse_status = "SUCCESS_WITH_WARNINGS"
        else:
            parse_status = "SUCCESS"
    resolved["canonical_claim_id"] = canonical_claim_id
    resolved_json_str = json.dumps(resolved, ensure_ascii=False)
    evidence_refs = json.dumps([{"chunk_id": w.get("chunk_id"), "snippet": w.get("snippet")} for w in windows])

    with session_scope() as session:
        card_repo.upsert(
            session,
            document_id=document_id,
            run_id=stage3_run_id,
            canonical_claim_id=canonical_claim_id,
            kind="STATE",
            resolved_json=resolved_json_str,
            raw_response=raw_text,
            parse_status=parse_status,
            prompt_version=prompt_version,
            model_id=model_id,
            evidence_refs=evidence_refs,
        )
        llm_repo.create(
            session,
            run_id=stage3_run_id,
            provider=provider.value,
            model=model_id,
            request_json=json.dumps(request_payload, ensure_ascii=False),
            response_text=raw_text,
            response_json=resolved_json_str,
            latency_ms=latency_ms,
            prompt_tokens=usage.input_tokens if usage else None,
            completion_tokens=usage.output_tokens if usage else None,
            status="SUCCESS" if parse_status != "PARSE_FAILED" else "PARSE_FAILED",
        )
        state_summary = (resolved.get("summary") or "").strip() or "(state)"
        for oid in resolved.get("candidate_objects") or []:
            baked_repo.append_state_to_object(session, stage3_run_id, oid, state_summary)

    return True, None


async def _process_action_card(
    canonical_claim_id: str,
    document_id: str,
    stage3_run_id: str,
    model_id: str,
    prompt_version: str,
    *,
    claim_repo: Stage1ClaimRepo,
    llm_repo: Stage1LlmCallRepo,
    card_repo: Stage3ResolvedCardRepo,
    baked_repo: Stage3ObjectBakedRepo,
    chunk_repo: ChunkRepo,
    collection_name: str,
    vector_size: int,
    embedding_model_id: str,
    timeout_s: float,
    max_tokens: int,
) -> tuple[bool, str | None]:
    """Build windows, get candidate actors+objects (with baked states), call LLM, store, embed+upsert."""
    with session_scope() as session:
        claim = claim_repo.get_claim_with_evidence(session, canonical_claim_id)
        if not claim or claim.claim_type != "ACTION":
            return False, "Claim not found or not ACTION"
        windows = build_evidence_windows(session, claim, chunk_repo)

    if not windows:
        logger.warning(
            "Stage3 action card skipped: claim %s has no evidence and no chunk fallback",
            canonical_claim_id,
        )
        return False, "No evidence windows"

    query_text = " ".join((w.get("window_text") or "") for w in windows)[:2000]
    embed_client = OllamaEmbedClient()
    embed_settings = EmbedSettings()
    dims = vector_size or embed_settings.dims
    try:
        await ensure_stage3_collection(collection_name=collection_name, vector_size=dims)
        vectors = await embed_client.embed_texts([query_text])
        if not vectors or len(vectors[0]) != dims:
            return False, "Embed failed for action query"
        query_vector = vectors[0]
    except Exception as e:
        logger.warning("Stage3 embed for action query failed: %s", e)
        return False, str(e)

    actor_hits, object_hits = await search_candidate_actors_and_objects(
        query_vector, document_id, collection_name=collection_name, limit_per_kind=10
    )
    candidate_actors: list[dict[str, Any]] = []
    candidate_objects: list[dict[str, Any]] = []
    with session_scope() as session:
        for h in actor_hits:
            row = card_repo.get(session, stage3_run_id, h.canonical_claim_id, "ACTOR")
            summary = ""
            if row and row.resolved_json:
                try:
                    summary = (json.loads(row.resolved_json).get("summary") or "").strip()
                except Exception:
                    pass
            candidate_actors.append({"actor_id": h.canonical_claim_id, "canonical_claim_id": h.canonical_claim_id, "summary": summary})
        for h in object_hits:
            row = card_repo.get(session, stage3_run_id, h.canonical_claim_id, "OBJECT")
            baked = baked_repo.get(session, stage3_run_id, h.canonical_claim_id)
            summary = ""
            states: list[str] = []
            if row and row.resolved_json:
                try:
                    data = json.loads(row.resolved_json)
                    summary = (data.get("summary") or "").strip()
                except Exception:
                    pass
            if baked and baked.object_json:
                try:
                    data = json.loads(baked.object_json)
                    states = data.get("states") or []
                except Exception:
                    pass
            candidate_objects.append({
                "object_id": h.canonical_claim_id,
                "canonical_claim_id": h.canonical_claim_id,
                "summary": summary,
                "states": states,
            })

    messages = build_messages(
        "ACTION",
        evidence_windows=windows,
        candidate_actors=candidate_actors,
        candidate_objects=candidate_objects,
    )
    request_payload = {
        "kind": "ACTION",
        "canonical_claim_id": canonical_claim_id,
        "system": messages[0]["content"],
        "user": messages[1]["content"],
        "user_len": len(messages[1]["content"]),
    }
    client = LiteLLMClient(concurrency_limit=4, max_retries=2)
    provider = _provider_from_model_id(model_id)
    req = LLMRequest(
        messages=[LLMMessage(role=m["role"], content=m["content"]) for m in messages],
        temperature=0.1,
        max_output_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    completion_kwargs = completion_kwargs_for_provider(provider, LLMSettings())

    try:
        resp = await client.acompletion(
            provider, model_id, req, timeout_s=timeout_s, **completion_kwargs
        )
    except Exception as e:
        logger.warning("Stage3 LLM failed for action %s: %s", canonical_claim_id, e)
        with session_scope() as session:
            llm_repo.create(
                session,
                run_id=stage3_run_id,
                request_json=json.dumps(request_payload, ensure_ascii=False),
                response_text=None,
                status="FAILED",
                error_message=str(e),
            )
        return False, str(e)

    raw_text = resp.text or ""
    usage = resp.usage
    latency_ms = resp.latency_ms or 0

    data = _repair_json(raw_text)
    if not data:
        parse_status = "PARSE_FAILED"
        resolved = {"summary": (raw_text or "(no output)")[:FALLBACK_SUMMARY_MAX_CHARS]}
    else:
        resolved = parse_action_card(data)
        if len((resolved.get("summary") or "").strip()) < SUMMARY_MIN_LENGTH:
            parse_status = "SUCCESS_WITH_WARNINGS"
        else:
            parse_status = "SUCCESS"
    resolved["canonical_claim_id"] = canonical_claim_id
    resolved_json_str = json.dumps(resolved, ensure_ascii=False)
    evidence_refs = json.dumps([{"chunk_id": w.get("chunk_id"), "snippet": w.get("snippet")} for w in windows])

    with session_scope() as session:
        card_repo.upsert(
            session,
            document_id=document_id,
            run_id=stage3_run_id,
            canonical_claim_id=canonical_claim_id,
            kind="ACTION",
            resolved_json=resolved_json_str,
            raw_response=raw_text,
            parse_status=parse_status,
            prompt_version=prompt_version,
            model_id=model_id,
            embedding_model_id=embedding_model_id,
            evidence_refs=evidence_refs,
        )
        llm_repo.create(
            session,
            run_id=stage3_run_id,
            provider=provider.value,
            model=model_id,
            request_json=json.dumps(request_payload, ensure_ascii=False),
            response_text=raw_text,
            response_json=resolved_json_str,
            latency_ms=latency_ms,
            prompt_tokens=usage.input_tokens if usage else None,
            completion_tokens=usage.output_tokens if usage else None,
            status="SUCCESS" if parse_status != "PARSE_FAILED" else "PARSE_FAILED",
        )

    embed_client = OllamaEmbedClient()
    card_text = resolved.get("summary") or "(action)"
    try:
        await ensure_stage3_collection(collection_name=collection_name, vector_size=dims)
        vectors = await embed_client.embed_texts([card_text])
        if vectors and len(vectors[0]) == dims:
            await qdrant_upsert_card(
                _stage3_point_id(canonical_claim_id, "ACTION"),
                vectors[0],
                document_id,
                canonical_claim_id,
                "ACTION",
                collection_name=collection_name,
                prompt_version=prompt_version,
                model_id=model_id,
                embedding_model_id=embedding_model_id,
            )
    except Exception as e:
        logger.warning("Stage3 embed/upsert failed for action %s: %s", canonical_claim_id, e)

    return True, None


async def _run_pass_async(
    pass_kind: str,
    stage1_run_id: str,
    document_id: str,
    stage3_run_id: str,
    model_id: str,
    prompt_version: str,
    *,
    claim_repo: Stage1ClaimRepo,
    llm_repo: Stage1LlmCallRepo,
    card_repo: Stage3ResolvedCardRepo,
    baked_repo: Stage3ObjectBakedRepo,
    chunk_repo: ChunkRepo,
    collection_name: str,
    vector_size: int,
    embedding_model_id: str,
    timeout_s: float,
    max_tokens: int,
) -> dict[str, Any]:
    """Run one pass. Returns stats dict."""
    with session_scope() as session:
        ids = claim_repo.list_accepted_claim_ids(session, stage1_run_id, pass_kind)
    processed = 0
    failed = 0
    for cid in ids:
        if pass_kind == "ACTOR":
            ok, err = await _process_actor_card(
                cid, document_id, stage3_run_id, model_id, prompt_version,
                claim_repo=claim_repo, llm_repo=llm_repo, card_repo=card_repo, chunk_repo=chunk_repo,
                collection_name=collection_name, vector_size=vector_size,
                embedding_model_id=embedding_model_id,                 timeout_s=timeout_s, max_tokens=max_tokens,
            )
        elif pass_kind == "OBJECT":
            ok, err = await _process_object_card(
                cid, document_id, stage3_run_id, model_id, prompt_version,
                claim_repo=claim_repo, llm_repo=llm_repo, card_repo=card_repo, baked_repo=baked_repo,
                chunk_repo=chunk_repo, collection_name=collection_name, vector_size=vector_size,
                embedding_model_id=embedding_model_id,                 timeout_s=timeout_s, max_tokens=max_tokens,
            )
        elif pass_kind == "STATE":
            ok, err = await _process_state_card(
                cid, document_id, stage3_run_id, model_id, prompt_version,
                claim_repo=claim_repo, llm_repo=llm_repo, card_repo=card_repo, baked_repo=baked_repo,
                chunk_repo=chunk_repo, collection_name=collection_name, vector_size=vector_size,
                timeout_s=timeout_s, max_tokens=max_tokens,
            )
        else:
            ok, err = await _process_action_card(
                cid, document_id, stage3_run_id, model_id, prompt_version,
                claim_repo=claim_repo, llm_repo=llm_repo, card_repo=card_repo, baked_repo=baked_repo,
                chunk_repo=chunk_repo, collection_name=collection_name, vector_size=vector_size,
                embedding_model_id=embedding_model_id, timeout_s=timeout_s, max_tokens=max_tokens,
            )
        if ok:
            processed += 1
        else:
            failed += 1
            logger.warning("Stage3 %s failed for claim %s: %s", pass_kind, cid, err or "unknown")
    return {"pass": pass_kind, "total": len(ids), "processed": processed, "failed": failed}


def run_stage3(
    stage1_run_id: str,
    model_id: str,
    *,
    collection_name: str = STAGE3_CARDS_COLLECTION,
    vector_size: int = 768,
    timeout_s: float = 120.0,
    max_tokens: int = 2048,
    prompt_version: str = "stage3_cards",
    embedding_model_id: str | None = None,
) -> dict[str, Any]:
    """
    Run Stage-3 assembly on canonical (ACCEPTED) claims from the given Stage-1 run.
    Creates a new pipeline run with run_kind=STAGE3_ASSEMBLE and runs four passes in order.
    Returns dict with stage3_run_id, status, stats.
    """
    from app.db.models.pipeline_run import PipelineRun

    claim_repo = Stage1ClaimRepo()
    llm_repo = Stage1LlmCallRepo()
    run_repo = Stage1RunRepo()
    card_repo = Stage3ResolvedCardRepo()
    baked_repo = Stage3ObjectBakedRepo()
    chunk_repo = ChunkRepo()
    embed_settings = EmbedSettings()
    emb_id = embedding_model_id or embed_settings.ollama_model

    with session_scope() as session:
        run = session.get(PipelineRun, stage1_run_id)
        if not run:
            return {"error": "Stage-1 run not found", "stage3_run_id": None}
        document_id = run.document_id
        workspace_id = run.workspace_id or get_workspace_id_for_document(session, document_id)
        if not workspace_id:
            return {"error": "Workspace not found for document", "stage3_run_id": None}

        stage3_run = run_repo.create(
            session,
            workspace_id=workspace_id,
            document_id=document_id,
            config={"stage1_run_id": stage1_run_id},
            prompt_version=prompt_version,
            extractor_version="stage3",
            model_id=model_id,
            run_kind=STAGE3_RUN_KIND,
        )
        stage3_run_id = stage3_run.id

    kwargs = {
        "claim_repo": claim_repo,
        "llm_repo": llm_repo,
        "card_repo": card_repo,
        "baked_repo": baked_repo,
        "chunk_repo": chunk_repo,
        "collection_name": collection_name,
        "vector_size": vector_size,
        "embedding_model_id": emb_id,
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
                stage3_run_id,
                model_id,
                prompt_version,
                **kwargs,
            )
            stats_per_pass.append(s)
        total_processed = sum(x["processed"] for x in stats_per_pass)
        total_failed = sum(x["failed"] for x in stats_per_pass)
        return {
            "stage3_run_id": stage3_run_id,
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
        logger.exception("Stage3 run failed: %s", e)
        with session_scope() as session:
            run_repo.finalize(
                session,
                stage3_run_id,
                "FAILED",
                {},
                error_summary=str(e),
            )
        return {
            "stage3_run_id": stage3_run_id,
            "status": "FAILED",
            "error": str(e),
            "stats": None,
        }

    with session_scope() as session:
        run_repo.finalize(
            session,
            stage3_run_id,
            "COMPLETED",
            result["stats"],
        )

    return result
