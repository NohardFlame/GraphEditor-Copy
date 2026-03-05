"""Canonicalization runners: actors, objects, object_states, actions (Phase 5)."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select, func
from sqlalchemy.orm import Session, joinedload

from app.db.models.claim import LlmCall

from app.canonical.comparator import (
    PROMPT_VERSION_LOCAL_COMPARE,
    compare_mention_with_candidate,
)
from app.canonical.deterministic import (
    build_action_sig,
    build_state_dedupe_key,
    find_canonical_by_dedupe_key,
    find_object_state,
)
from app.canonical.models import CanonicalData, Candidate
from app.canonical.norm import norm
from app.canonical.service import (
    get_canonical_candidates_for_mention,
    record_canonical_merge,
    upsert_canonical_and_qdrant,
    upsert_state_and_qdrant,
)
from app.canonical.tournament import CreateNewCanonical, LinkToExisting, run_tournament
from app.db.document_helpers import workspace_id_for_document
from app.db.repositories.canonical_repo import CanonicalRepo
from app.db.repositories.canonical_settings_repo import (
    DEDUP_STRATEGY_ALGO_THRESHOLDS,
    get_or_create_for_workspace,
)
from app.db.models.extraction import (
    ExtractionRun,
    Frame,
    Mention,
    MentionToCanonical,
    Relation,
)
from app.extraction.constants import (
    CANONICALIZE_ACTIONS,
    CANONICALIZE_ACTORS,
    CANONICALIZE_OBJECT_STATES,
    CANONICALIZE_OBJECTS,
)

if TYPE_CHECKING:
    from app.canonical.service import EmbedClient
    from app.canonical.comparator import ComparatorLLMClient
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)

REL_ACTION_HAS_ACTOR = "ACTION_HAS_ACTOR"
REL_ACTION_HAS_OBJECT = "ACTION_HAS_OBJECT"
REL_OBJECT_HAS_STATE = "OBJECT_HAS_STATE"


def _get_mentions_without_accept_mapping(
    session: Session,
    document_id: str,
    mention_type: str,
) -> list[Mention]:
    """Mentions of given type for document that have no ACCEPT in mention_to_canonical."""
    return _get_mentions_for_canonicalization(
        session, document_id, mention_type, include_mapped=False
    )


def _get_mentions_for_canonicalization(
    session: Session,
    document_id: str,
    mention_type: str,
    *,
    include_mapped: bool = False,
) -> list[Mention]:
    """Mentions of given type for document. If include_mapped=False, only those without ACCEPT mapping."""
    stmt = (
        select(Mention)
        .where(
            Mention.document_id == document_id,
            Mention.type == mention_type,
        )
        .options(
            joinedload(Mention.frame),
            joinedload(Mention.evidence),
            joinedload(Mention.canonical_link),
        )
    )
    mentions = list(session.execute(stmt).scalars().unique().all())
    if include_mapped:
        return mentions
    out = []
    for m in mentions:
        if m.canonical_link is None or m.canonical_link.decision != "ACCEPT":
            out.append(m)
    return out


def _group_by_dedupe_key(mentions: list[Mention]) -> dict[str, list[Mention]]:
    """Group mentions by dedupe_key. Skips mentions with no dedupe_key."""
    groups: dict[str, list[Mention]] = defaultdict(list)
    for m in mentions:
        if m.dedupe_key:
            groups[m.dedupe_key].append(m)
    return dict(groups)


def _link_mentions_to_canonical(
    session: Session,
    mention_ids: list[str],
    canonical_id: str,
    run_id: str,
    decided_by: str,
) -> None:
    """Insert or update mention_to_canonical for each mention (ACCEPT, decided_by, run_id)."""
    for mid in mention_ids:
        existing = session.execute(
            select(MentionToCanonical).where(MentionToCanonical.mention_id == mid)
        ).scalar_one_or_none()
        if existing:
            existing.canonical_id = canonical_id
            existing.decision = "ACCEPT"
            existing.decided_by = decided_by
            existing.run_id = run_id
        else:
            session.add(
                MentionToCanonical(
                    mention_id=mid,
                    canonical_id=canonical_id,
                    decision="ACCEPT",
                    decided_by=decided_by,
                    run_id=run_id,
                )
            )
    session.flush()


def _mention_fields(mention: Mention) -> dict[str, Any]:
    try:
        return json.loads(mention.fields_json or "{}")
    except Exception:
        return {}


def _count_mentions_by_dedupe_key(session: Session, dedupe_key: str) -> int:
    """Global count of mentions with this dedupe_key."""
    n = session.scalar(
        select(func.count()).select_from(Mention).where(Mention.dedupe_key == dedupe_key)
    ) or 0
    return n


def _norm_name_from_dedupe_key(dedupe_key: str, canonical_type: str) -> str:
    """Extract norm_name from dedupe_key (e.g. actor::user -> user)."""
    t = (canonical_type or "").upper()
    if t == "ACTOR" and dedupe_key.startswith("actor::"):
        return dedupe_key[7:].strip()
    if t == "OBJECT" and dedupe_key.startswith("object::"):
        return dedupe_key[8:].strip()
    return norm(dedupe_key.split("::")[-1] if "::" in dedupe_key else dedupe_key)


async def _canonicalize_entities_algo(
    session: Session,
    run_id: str,
    groups: dict[str, list[Mention]],
    canonical_type: str,
    stats: dict[str, Any],
    embed_client: "EmbedClient",
    qdrant_client: "AsyncQdrantClient",
    settings: Any,
) -> None:
    """Algorithmic path: dedupe_key frequency + Qdrant cosine thresholds. Mutates stats."""
    name_key = "actor_name" if canonical_type == "ACTOR" else "object_name"
    for dk, group in groups.items():
        if not group:
            continue
        stats["processed"] += len(group)
        canonical = find_canonical_by_dedupe_key(session, canonical_type, dk)
        if canonical is not None:
            _link_mentions_to_canonical(
                session, [m.id for m in group], canonical.id, run_id, "RULE"
            )
            stats["by_rule"] += len(group)
            continue
        count = _count_mentions_by_dedupe_key(session, dk)
        if count >= settings.min_mentions_for_canonical:
            rep = group[0]
            fields = _mention_fields(rep)
            name = fields.get("name") or fields.get(name_key) or "Unknown"
            norm_name = _norm_name_from_dedupe_key(dk, canonical_type) or norm(name)
            data = CanonicalData(
                canonical_type=canonical_type,
                name=name,
                norm_name=norm_name,
                created_run_id=run_id,
            )
            canonical = await upsert_canonical_and_qdrant(
                session, data, embed_client=embed_client, qdrant_client=qdrant_client
            )
            _link_mentions_to_canonical(
                session, [m.id for m in group], canonical.id, run_id, "ALGO_FREQ"
            )
            stats["by_algo_freq"] = stats.get("by_algo_freq", 0) + len(group)
            stats["new_canonicals"] = stats.get("new_canonicals", 0) + 1
            continue
        rep = group[0]
        frame = rep.frame
        candidates = await get_canonical_candidates_for_mention(
            rep, frame, canonical_type, 10,
            embed_client=embed_client, qdrant_client=qdrant_client, session=session,
        )
        if not candidates:
            fields = _mention_fields(rep)
            name = fields.get("name") or fields.get(name_key) or "Unknown"
            norm_name = _norm_name_from_dedupe_key(dk, canonical_type) or norm(name)
            data = CanonicalData(
                canonical_type=canonical_type,
                name=name,
                norm_name=norm_name,
                created_run_id=run_id,
            )
            canonical = await upsert_canonical_and_qdrant(
                session, data, embed_client=embed_client, qdrant_client=qdrant_client
            )
            _link_mentions_to_canonical(
                session, [m.id for m in group], canonical.id, run_id, "ALGO_NEW"
            )
            stats["by_algo_new"] = stats.get("by_algo_new", 0) + len(group)
            stats["new_canonicals"] = stats.get("new_canonicals", 0) + 1
            continue
        best = candidates[0]
        if best.score >= settings.cosine_high_merge_threshold:
            _link_mentions_to_canonical(
                session, [m.id for m in group], best.canonical_id, run_id, "ALGO_HIGH"
            )
            stats["by_algo_high"] = stats.get("by_algo_high", 0) + len(group)
            continue
        if best.score <= settings.cosine_low_new_threshold:
            fields = _mention_fields(rep)
            name = fields.get("name") or fields.get(name_key) or "Unknown"
            norm_name = _norm_name_from_dedupe_key(dk, canonical_type) or norm(name)
            data = CanonicalData(
                canonical_type=canonical_type,
                name=name,
                norm_name=norm_name,
                created_run_id=run_id,
            )
            canonical = await upsert_canonical_and_qdrant(
                session, data, embed_client=embed_client, qdrant_client=qdrant_client
            )
            _link_mentions_to_canonical(
                session, [m.id for m in group], canonical.id, run_id, "ALGO_NEW"
            )
            stats["by_algo_new"] = stats.get("by_algo_new", 0) + len(group)
            stats["new_canonicals"] = stats.get("new_canonicals", 0) + 1
            continue
        fields = _mention_fields(rep)
        name = fields.get("name") or fields.get(name_key) or "Unknown"
        norm_name = _norm_name_from_dedupe_key(dk, canonical_type) or norm(name)
        data = CanonicalData(
            canonical_type=canonical_type,
            name=name,
            norm_name=norm_name,
            created_run_id=run_id,
        )
        new_canonical = await upsert_canonical_and_qdrant(
            session, data, embed_client=embed_client, qdrant_client=qdrant_client
        )
        await record_canonical_merge(
            session,
            new_canonical.id,
            best.canonical_id,
            qdrant_client,
            run_id=run_id,
            reason="DEBATED_COSINE_BAND",
        )
        _link_mentions_to_canonical(
            session, [m.id for m in group], new_canonical.id, run_id, "ALGO_DEBATED"
        )
        stats["by_algo_debated"] = stats.get("by_algo_debated", 0) + len(group)
        stats["new_canonicals"] = stats.get("new_canonicals", 0) + 1


async def canonicalize_actors(
    doc_id: UUID | str,
    *,
    session: Session,
    embed_client: "EmbedClient",
    qdrant_client: "AsyncQdrantClient",
    llm_client: "ComparatorLLMClient",
    workspace_id: str | None = None,
    force: bool = False,
) -> str:
    """Run actor canonicalization for document. Returns extraction_run id. If force=True, re-process all mentions."""
    doc_id_str = str(doc_id)
    wid = workspace_id or workspace_id_for_document(doc_id_str, session)
    run_entity = ExtractionRun(
        id=str(uuid4()),
        workspace_id=wid,
        document_id=doc_id_str,
        run_kind=CANONICALIZE_ACTORS,
        status="RUNNING",
        input_hash=None,
        prompt_version=PROMPT_VERSION_LOCAL_COMPARE,
        model_id=None,
    )
    session.add(run_entity)
    session.commit()
    run_id = run_entity.id

    stats = {"processed": 0, "by_rule": 0, "by_llm": 0, "new_canonicals": 0}
    try:
        mentions = _get_mentions_for_canonicalization(
            session, doc_id_str, "ACTOR", include_mapped=force
        )
        groups = _group_by_dedupe_key(mentions)
        settings = get_or_create_for_workspace(session, wid)
        session.flush()
        if settings.dedup_strategy == DEDUP_STRATEGY_ALGO_THRESHOLDS:
            await _canonicalize_entities_algo(
                session=session,
                run_id=run_id,
                groups=groups,
                canonical_type="ACTOR",
                stats=stats,
                embed_client=embed_client,
                qdrant_client=qdrant_client,
                settings=settings,
            )
        else:
            for dk, group in groups.items():
                if not group:
                    continue
                stats["processed"] += len(group)
                canonical = find_canonical_by_dedupe_key(session, "ACTOR", dk)
                if canonical is not None:
                    _link_mentions_to_canonical(
                        session, [m.id for m in group], canonical.id, run_id, "RULE"
                    )
                    stats["by_rule"] += len(group)
                    continue
                rep = group[0]
                frame = rep.frame
                candidates = await get_canonical_candidates_for_mention(
                    rep, frame, "ACTOR", 10, embed_client=embed_client, qdrant_client=qdrant_client, session=session
                )
                async def compare(m: Mention, c: Candidate):
                    return await compare_mention_with_candidate(
                        m, c, "ACTOR", llm_client=llm_client,
                        run_id=run_id, workspace_id=wid,
                        session=session, model_id=None,
                    )
                outcome = await run_tournament(rep, candidates, compare_fn=compare)
                if isinstance(outcome, LinkToExisting):
                    _canonical_repo = CanonicalRepo()
                    if _canonical_repo.get_by_id(session, outcome.canonical_id) is not None:
                        _link_mentions_to_canonical(
                            session, [m.id for m in group], outcome.canonical_id, run_id, "LLM"
                        )
                        stats["by_llm"] += len(group)
                    else:
                        logger.warning(
                            "LinkToExisting canonical_id=%s not in DB (e.g. stale Qdrant), creating new ACTOR",
                            outcome.canonical_id,
                        )
                        name = _mention_fields(rep).get("name") or _mention_fields(rep).get("actor_name") or "Unknown"
                        data = CanonicalData(canonical_type="ACTOR", name=name, created_run_id=run_id)
                        canonical = await upsert_canonical_and_qdrant(
                            session, data, embed_client=embed_client, qdrant_client=qdrant_client
                        )
                        _link_mentions_to_canonical(
                            session, [m.id for m in group], canonical.id, run_id, "LLM"
                        )
                        stats["by_llm"] += len(group)
                        stats["new_canonicals"] += 1
                else:
                    name = _mention_fields(rep).get("name") or _mention_fields(rep).get("actor_name") or "Unknown"
                    data = CanonicalData(canonical_type="ACTOR", name=name, created_run_id=run_id)
                    canonical = await upsert_canonical_and_qdrant(
                        session, data, embed_client=embed_client, qdrant_client=qdrant_client
                    )
                    _link_mentions_to_canonical(
                        session, [m.id for m in group], canonical.id, run_id, "LLM"
                    )
                    stats["by_llm"] += len(group)
                    stats["new_canonicals"] += 1
        llm_calls_count = session.scalar(
            select(func.count()).select_from(LlmCall).where(LlmCall.extraction_run_id == run_id)
        ) or 0
        stats["llm_calls_count"] = llm_calls_count
        run_entity.stats_json = json.dumps(stats)
        run_entity.status = "SUCCEEDED"
    except Exception as e:
        logger.exception("canonicalize_actors failed: %s", e)
        run_entity.stats_json = json.dumps({**stats, "error": str(e)})
        run_entity.status = "FAILED"
    session.add(run_entity)
    session.commit()
    return run_id


async def canonicalize_objects(
    doc_id: UUID | str,
    *,
    session: Session,
    embed_client: "EmbedClient",
    qdrant_client: "AsyncQdrantClient",
    llm_client: "ComparatorLLMClient",
    workspace_id: str | None = None,
    force: bool = False,
) -> str:
    """Run object canonicalization for document. Returns extraction_run id. If force=True, re-process all mentions."""
    doc_id_str = str(doc_id)
    wid = workspace_id or workspace_id_for_document(doc_id_str, session)
    run_entity = ExtractionRun(
        id=str(uuid4()),
        workspace_id=wid,
        document_id=doc_id_str,
        run_kind=CANONICALIZE_OBJECTS,
        status="RUNNING",
        input_hash=None,
        prompt_version=PROMPT_VERSION_LOCAL_COMPARE,
        model_id=None,
    )
    session.add(run_entity)
    session.commit()
    run_id = run_entity.id

    stats = {"processed": 0, "by_rule": 0, "by_llm": 0, "new_canonicals": 0}
    try:
        mentions = _get_mentions_for_canonicalization(
            session, doc_id_str, "OBJECT", include_mapped=force
        )
        groups = _group_by_dedupe_key(mentions)
        settings = get_or_create_for_workspace(session, wid)
        session.flush()
        if settings.dedup_strategy == DEDUP_STRATEGY_ALGO_THRESHOLDS:
            await _canonicalize_entities_algo(
                session=session,
                run_id=run_id,
                groups=groups,
                canonical_type="OBJECT",
                stats=stats,
                embed_client=embed_client,
                qdrant_client=qdrant_client,
                settings=settings,
            )
        else:
            for dk, group in groups.items():
                if not group:
                    continue
                stats["processed"] += len(group)
                canonical = find_canonical_by_dedupe_key(session, "OBJECT", dk)
                if canonical is not None:
                    _link_mentions_to_canonical(
                        session, [m.id for m in group], canonical.id, run_id, "RULE"
                    )
                    stats["by_rule"] += len(group)
                    continue
                rep = group[0]
                frame = rep.frame
                candidates = await get_canonical_candidates_for_mention(
                    rep, frame, "OBJECT", 10, embed_client=embed_client, qdrant_client=qdrant_client, session=session
                )
                async def compare(m: Mention, c: Candidate):
                    return await compare_mention_with_candidate(
                        m, c, "OBJECT", llm_client=llm_client,
                        run_id=run_id, workspace_id=wid,
                        session=session, model_id=None,
                    )
                outcome = await run_tournament(rep, candidates, compare_fn=compare)
                if isinstance(outcome, LinkToExisting):
                    _canonical_repo = CanonicalRepo()
                    if _canonical_repo.get_by_id(session, outcome.canonical_id) is not None:
                        _link_mentions_to_canonical(
                            session, [m.id for m in group], outcome.canonical_id, run_id, "LLM"
                        )
                        stats["by_llm"] += len(group)
                    else:
                        logger.warning(
                            "LinkToExisting canonical_id=%s not in DB (e.g. stale Qdrant), creating new OBJECT",
                            outcome.canonical_id,
                        )
                        name = _mention_fields(rep).get("name") or _mention_fields(rep).get("object_name") or "Unknown"
                        data = CanonicalData(canonical_type="OBJECT", name=name, created_run_id=run_id)
                        canonical = await upsert_canonical_and_qdrant(
                            session, data, embed_client=embed_client, qdrant_client=qdrant_client
                        )
                        _link_mentions_to_canonical(
                            session, [m.id for m in group], canonical.id, run_id, "LLM"
                        )
                        stats["by_llm"] += len(group)
                        stats["new_canonicals"] += 1
                else:
                    name = _mention_fields(rep).get("name") or _mention_fields(rep).get("object_name") or "Unknown"
                    data = CanonicalData(canonical_type="OBJECT", name=name, created_run_id=run_id)
                    canonical = await upsert_canonical_and_qdrant(
                        session, data, embed_client=embed_client, qdrant_client=qdrant_client
                    )
                    _link_mentions_to_canonical(
                        session, [m.id for m in group], canonical.id, run_id, "LLM"
                    )
                    stats["by_llm"] += len(group)
                    stats["new_canonicals"] += 1
        llm_calls_count = session.scalar(
            select(func.count()).select_from(LlmCall).where(LlmCall.extraction_run_id == run_id)
        ) or 0
        stats["llm_calls_count"] = llm_calls_count
        run_entity.stats_json = json.dumps(stats)
        run_entity.status = "SUCCEEDED"
    except Exception as e:
        logger.exception("canonicalize_objects failed: %s", e)
        run_entity.stats_json = json.dumps({**stats, "error": str(e)})
        run_entity.status = "FAILED"
    session.add(run_entity)
    session.commit()
    return run_id


def _resolve_object_id_for_state_mention(session: Session, state_mention_id: str) -> str | None:
    """For a STATE mention, get the canonical_object_id of its object (via OBJECT_HAS_STATE)."""
    stmt = select(Relation).where(
        Relation.dst_mention_id == state_mention_id,
        Relation.rel_type == REL_OBJECT_HAS_STATE,
    )
    rel = session.execute(stmt).scalar_one_or_none()
    if not rel:
        return None
    object_mention_id = rel.src_mention_id
    mtc = session.execute(
        select(MentionToCanonical).where(
            MentionToCanonical.mention_id == object_mention_id,
            MentionToCanonical.decision == "ACCEPT",
        )
    ).scalar_one_or_none()
    if not mtc:
        return None
    return mtc.canonical_id


async def canonicalize_object_states(
    doc_id: UUID | str,
    *,
    session: Session,
    embed_client: "EmbedClient",
    qdrant_client: "AsyncQdrantClient",
    llm_client: "ComparatorLLMClient",
    workspace_id: str | None = None,
    force: bool = False,
) -> str:
    """Run object-state canonicalization for document. Depends on objects being canonicalized. If force=True, re-process all."""
    doc_id_str = str(doc_id)
    wid = workspace_id or workspace_id_for_document(doc_id_str, session)
    run_entity = ExtractionRun(
        id=str(uuid4()),
        workspace_id=wid,
        document_id=doc_id_str,
        run_kind=CANONICALIZE_OBJECT_STATES,
        status="RUNNING",
        input_hash=None,
        prompt_version=PROMPT_VERSION_LOCAL_COMPARE,
        model_id=None,
    )
    session.add(run_entity)
    session.commit()
    run_id = run_entity.id

    stats = {"processed": 0, "by_rule": 0, "by_llm": 0, "new_canonicals": 0, "skipped_no_object": 0}
    try:
        mentions = _get_mentions_for_canonicalization(
            session, doc_id_str, "STATE", include_mapped=force
        )
        groups: dict[str, list[Mention]] = defaultdict(list)
        for m in mentions:
            obj_canonical_id = _resolve_object_id_for_state_mention(session, m.id)
            if obj_canonical_id is None:
                stats["skipped_no_object"] += 1
                continue
            state_norm = norm(_mention_fields(m).get("state_name") or _mention_fields(m).get("state") or "")
            if not state_norm:
                continue
            resolved_key = build_state_dedupe_key(obj_canonical_id, state_norm)
            groups[resolved_key].append(m)
        for resolved_key, group in groups.items():
            if not group:
                continue
            stats["processed"] += len(group)
            canonical = find_canonical_by_dedupe_key(session, "STATE", resolved_key)
            if canonical is not None:
                _link_mentions_to_canonical(
                    session, [m.id for m in group], canonical.id, run_id, "RULE"
                )
                stats["by_rule"] += len(group)
                continue
            parts = resolved_key.split("::", 2)
            if len(parts) != 3:
                continue
            canonical_object_id = parts[1]
            state_norm = parts[2]
            rep = group[0]
            state_name = _mention_fields(rep).get("state_name") or _mention_fields(rep).get("state") or state_norm
            state = await upsert_state_and_qdrant(
                session, canonical_object_id, state_name, state_norm,
                embed_client=embed_client, qdrant_client=qdrant_client,
            )
            _link_mentions_to_canonical(
                session, [m.id for m in group], state.id, run_id, "RULE"
            )
            stats["by_rule"] += len(group)
            stats["new_canonicals"] += 1
        llm_calls_count = session.scalar(
            select(func.count()).select_from(LlmCall).where(LlmCall.extraction_run_id == run_id)
        ) or 0
        stats["llm_calls_count"] = llm_calls_count
        run_entity.stats_json = json.dumps(stats)
        run_entity.status = "SUCCEEDED"
    except Exception as e:
        logger.exception("canonicalize_object_states failed: %s", e)
        run_entity.stats_json = json.dumps({**stats, "error": str(e)})
        run_entity.status = "FAILED"
    session.add(run_entity)
    session.commit()
    return run_id


def _resolve_actor_and_object_for_action(
    session: Session,
    action_mention_id: str,
) -> tuple[str | None, str | None]:
    """Return (canonical_actor_id, canonical_object_id) for an ACTION mention."""
    stmt = select(Relation).where(Relation.src_mention_id == action_mention_id)
    rels = list(session.execute(stmt).scalars().all())
    actor_id: str | None = None
    object_id: str | None = None
    for rel in rels:
        mtc_stmt = select(MentionToCanonical).where(
            MentionToCanonical.mention_id == rel.dst_mention_id,
            MentionToCanonical.decision == "ACCEPT",
        )
        mtc = session.execute(mtc_stmt).scalar_one_or_none()
        if not mtc:
            continue
        if rel.rel_type == REL_ACTION_HAS_ACTOR:
            actor_id = mtc.canonical_id
        elif rel.rel_type == REL_ACTION_HAS_OBJECT:
            object_id = mtc.canonical_id
    return actor_id, object_id


async def canonicalize_actions(
    doc_id: UUID | str,
    *,
    session: Session,
    embed_client: "EmbedClient",
    qdrant_client: "AsyncQdrantClient",
    llm_client: "ComparatorLLMClient",
    workspace_id: str | None = None,
    force: bool = False,
) -> str:
    """Run action canonicalization for document. Depends on actors and objects being canonicalized. If force=True, re-process all."""
    doc_id_str = str(doc_id)
    wid = workspace_id or workspace_id_for_document(doc_id_str, session)
    run_entity = ExtractionRun(
        id=str(uuid4()),
        workspace_id=wid,
        document_id=doc_id_str,
        run_kind=CANONICALIZE_ACTIONS,
        status="RUNNING",
        input_hash=None,
        prompt_version=PROMPT_VERSION_LOCAL_COMPARE,
        model_id=None,
    )
    session.add(run_entity)
    session.commit()
    run_id = run_entity.id

    stats = {"processed": 0, "by_rule": 0, "by_llm": 0, "new_canonicals": 0, "skipped_no_actor_or_object": 0}
    try:
        mentions = _get_mentions_for_canonicalization(
            session, doc_id_str, "ACTION", include_mapped=force
        )
        groups_by_sig: dict[str, list[Mention]] = defaultdict(list)
        for m in mentions:
            actor_id, object_id = _resolve_actor_and_object_for_action(session, m.id)
            if actor_id is None or object_id is None:
                stats["skipped_no_actor_or_object"] += 1
                continue
            verb_norm = norm(_mention_fields(m).get("verb") or "")
            if not verb_norm:
                continue
            action_sig = build_action_sig(actor_id, verb_norm, object_id)
            groups_by_sig[action_sig].append(m)
        for action_sig, group in groups_by_sig.items():
            if not group:
                continue
            stats["processed"] += len(group)
            canonical = find_canonical_by_dedupe_key(session, "ACTION", action_sig)
            if canonical is not None:
                _link_mentions_to_canonical(
                    session, [m.id for m in group], canonical.id, run_id, "RULE"
                )
                stats["by_rule"] += len(group)
                continue
            rep = group[0]
            actor_id, object_id = _resolve_actor_and_object_for_action(session, rep.id)
            if actor_id is None or object_id is None:
                continue
            verb_norm = norm(_mention_fields(rep).get("verb") or "")
            name = f"action {verb_norm}"
            data = CanonicalData(
                canonical_type="ACTION",
                name=name,
                norm_name=action_sig,
                actor_id=actor_id,
                object_id=object_id,
                verb_norm=verb_norm,
                created_run_id=run_id,
            )
            canonical = await upsert_canonical_and_qdrant(
                session, data, embed_client=embed_client, qdrant_client=qdrant_client
            )
            _link_mentions_to_canonical(
                session, [m.id for m in group], canonical.id, run_id, "RULE"
            )
            stats["by_rule"] += len(group)
            stats["new_canonicals"] += 1
        llm_calls_count = session.scalar(
            select(func.count()).select_from(LlmCall).where(LlmCall.extraction_run_id == run_id)
        ) or 0
        stats["llm_calls_count"] = llm_calls_count
        run_entity.stats_json = json.dumps(stats)
        run_entity.status = "SUCCEEDED"
    except Exception as e:
        logger.exception("canonicalize_actions failed: %s", e)
        run_entity.stats_json = json.dumps({**stats, "error": str(e)})
        run_entity.status = "FAILED"
    session.add(run_entity)
    session.commit()
    return run_id


async def rerun_canonicalization(
    doc_id: UUID | str,
    run_kind: str,
    *,
    session: Session,
    embed_client: "EmbedClient",
    qdrant_client: "AsyncQdrantClient",
    llm_client: "ComparatorLLMClient",
    workspace_id: str | None = None,
    force: bool = False,
) -> str:
    """Re-run one canonicalization step. If force=False, only unmapped mentions are processed; if force=True, all mentions are re-processed. Returns extraction_run id."""
    wid = workspace_id or workspace_id_for_document(str(doc_id), session)
    if run_kind == CANONICALIZE_ACTORS:
        return await canonicalize_actors(
            doc_id, session=session, embed_client=embed_client,
            qdrant_client=qdrant_client, llm_client=llm_client,
            workspace_id=wid, force=force,
        )
    if run_kind == CANONICALIZE_OBJECTS:
        return await canonicalize_objects(
            doc_id, session=session, embed_client=embed_client,
            qdrant_client=qdrant_client, llm_client=llm_client,
            workspace_id=wid, force=force,
        )
    if run_kind == CANONICALIZE_OBJECT_STATES:
        return await canonicalize_object_states(
            doc_id, session=session, embed_client=embed_client,
            qdrant_client=qdrant_client, llm_client=llm_client,
            workspace_id=wid, force=force,
        )
    if run_kind == CANONICALIZE_ACTIONS:
        return await canonicalize_actions(
            doc_id, session=session, embed_client=embed_client,
            qdrant_client=qdrant_client, llm_client=llm_client,
            workspace_id=wid, force=force,
        )
    raise ValueError(f"Unknown run_kind: {run_kind!r}")


async def run_canonicalization_for_document(
    doc_id: UUID | str,
    *,
    session: Session,
    embed_client: "EmbedClient",
    qdrant_client: "AsyncQdrantClient",
    llm_client: "ComparatorLLMClient",
    workspace_id: str | None = None,
) -> dict[str, str]:
    """Run all four canonicalization steps in order. Returns run_id by kind."""
    wid = workspace_id or workspace_id_for_document(str(doc_id), session)
    r1 = await canonicalize_actors(doc_id, session=session, embed_client=embed_client, qdrant_client=qdrant_client, llm_client=llm_client, workspace_id=wid)
    r2 = await canonicalize_objects(doc_id, session=session, embed_client=embed_client, qdrant_client=qdrant_client, llm_client=llm_client, workspace_id=wid)
    r3 = await canonicalize_object_states(doc_id, session=session, embed_client=embed_client, qdrant_client=qdrant_client, llm_client=llm_client, workspace_id=wid)
    r4 = await canonicalize_actions(doc_id, session=session, embed_client=embed_client, qdrant_client=qdrant_client, llm_client=llm_client, workspace_id=wid)
    return {
        CANONICALIZE_ACTORS: r1,
        CANONICALIZE_OBJECTS: r2,
        CANONICALIZE_OBJECT_STATES: r3,
        CANONICALIZE_ACTIONS: r4,
    }
