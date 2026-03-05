"""Canonical upsert and retrieval service (Phase 4)."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.canonical.embedding_text import (
    build_action_embedding_text,
    build_actor_embedding_text,
    build_object_embedding_text,
    build_state_embedding_text,
)
from app.canonical.models import CanonicalData, Candidate
from app.canonical.norm import norm
from app.canonical.qdrant_collections import (
    COLLECTION_ACTIONS,
    COLLECTION_ACTORS,
    COLLECTION_OBJECTS,
    COLLECTION_OBJECT_STATES,
)
from app.canonical.qdrant_ops import (
    search_canonical_collection,
    update_canonical_point_payload,
    upsert_canonical_point,
)
from app.db.models.extraction import Canonical, Frame, Mention
from app.db.repositories.canonical_repo import CanonicalRepo

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


class EmbedClient(Protocol):
    """Protocol for embedding client (e.g. OllamaEmbedClient)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    """Return one vector per input text."""


def _collection_for_type(canonical_type: str) -> str:
    t = canonical_type.upper()
    if t == "ACTOR":
        return COLLECTION_ACTORS
    if t == "OBJECT":
        return COLLECTION_OBJECTS
    if t == "ACTION":
        return COLLECTION_ACTIONS
    if t == "STATE":
        return COLLECTION_OBJECT_STATES
    raise ValueError(f"Unknown canonical_type: {canonical_type}")


def _build_canonical_payload(
    canonical_id: str,
    canonical_type: str,
    name: str,
    norm_name: str,
    aliases: list[str],
    created_at,
    superseded_by_id: str | None,
    *,
    actor_id: str | None = None,
    object_id: str | None = None,
    verb_norm: str | None = None,
    canonical_object_id: str | None = None,
    state_name: str | None = None,
    state_norm: str | None = None,
) -> dict:
    payload = {
        "canonical_id": canonical_id,
        "canonical_type": canonical_type,
        "name": name,
        "norm_name": norm_name,
        "aliases": aliases,
        "created_at": created_at,
        "superseded_by": superseded_by_id,
    }
    if actor_id is not None:
        payload["actor_id"] = actor_id
    if object_id is not None:
        payload["object_id"] = object_id
    if verb_norm is not None:
        payload["verb_norm"] = verb_norm
    if canonical_object_id is not None:
        payload["canonical_object_id"] = canonical_object_id
    if state_name is not None:
        payload["state_name"] = state_name
    if state_norm is not None:
        payload["state_norm"] = state_norm
    return payload


async def upsert_canonical_and_qdrant(
    session: Session,
    data: CanonicalData,
    *,
    embed_client: EmbedClient,
    qdrant_client: AsyncQdrantClient,
    created_run_id: str | None = None,
):
    """Create or update a canonical in SQL and sync to Qdrant. Idempotent by (type, norm_name) or canonical_id."""
    from app.db.models.extraction import Canonical

    run_id = data.created_run_id or created_run_id
    norm_name = data.norm_name if data.norm_name is not None and data.norm_name != "" else norm(data.name)
    repo = CanonicalRepo()
    canonical = repo.upsert_canonical(
        session,
        data.canonical_type,
        data.name,
        norm_name,
        created_run_id=run_id,
        canonical_id=data.canonical_id,
    )
    repo.upsert_aliases(session, canonical.id, data.aliases or [])
    session.refresh(canonical)
    aliases_list = [a.alias_text for a in canonical.aliases]

    # Build embedding text by type
    ctype = canonical.canonical_type.upper()
    if ctype == "ACTOR":
        text = build_actor_embedding_text(canonical, list(canonical.aliases))
    elif ctype == "OBJECT":
        text = build_object_embedding_text(canonical, list(canonical.aliases))
    elif ctype == "ACTION":
        actor_canonical = repo.get_by_id(session, data.actor_id) if data.actor_id else None
        object_canonical = repo.get_by_id(session, data.object_id) if data.object_id else None
        text = build_action_embedding_text(
            canonical,
            actor_canonical,
            object_canonical,
            list(canonical.aliases),
            verb_norm=data.verb_norm,
        )
    else:
        raise ValueError(f"upsert_canonical_and_qdrant does not handle type {data.canonical_type} (state use upsert_state_and_qdrant)")

    vectors = await embed_client.embed_texts([text])
    if not vectors:
        raise RuntimeError("embed_texts returned no vector")
    vector = vectors[0]

    collection = _collection_for_type(canonical.canonical_type)
    payload = _build_canonical_payload(
        canonical.id,
        canonical.canonical_type,
        canonical.name,
        canonical.norm_name,
        aliases_list,
        canonical.created_at,
        canonical.superseded_by_id,
        actor_id=data.actor_id,
        object_id=data.object_id,
        verb_norm=data.verb_norm or canonical.norm_name,
    )
    await upsert_canonical_point(qdrant_client, collection, canonical.id, vector, payload)
    return canonical


async def upsert_state_and_qdrant(
    session: Session,
    canonical_object_id: str,
    state_name: str,
    state_norm: str,
    *,
    embed_client: EmbedClient,
    qdrant_client: AsyncQdrantClient,
):
    """Ensure object_state exists and upsert its point into canonical_object_states collection."""
    from app.db.models.extraction import Canonical

    repo = CanonicalRepo()
    state = repo.get_or_create_object_state(
        session, canonical_object_id, state_name, state_norm
    )
    object_canonical = repo.get_by_id(session, canonical_object_id)
    if not object_canonical:
        raise ValueError(f"Object canonical {canonical_object_id} not found")
    text = build_state_embedding_text(object_canonical, state)
    vectors = await embed_client.embed_texts([text])
    if not vectors:
        raise RuntimeError("embed_texts returned no vector")
    vector = vectors[0]
    payload = _build_canonical_payload(
        state.id,
        "STATE",
        state.state_name,
        state.state_norm,
        [],
        None,
        None,
        canonical_object_id=canonical_object_id,
        state_name=state.state_name,
        state_norm=state.state_norm,
    )
    await upsert_canonical_point(
        qdrant_client,
        COLLECTION_OBJECT_STATES,
        state.id,
        vector,
        payload,
    )
    return state


def build_mention_card_text(mention: Mention, frame: Frame | None) -> str:
    """Build mention card text for embedding (for candidate retrieval)."""
    try:
        fields = json.loads(mention.fields_json) if mention.fields_json else {}
    except Exception:
        fields = {}
    snippet = ""
    if mention.evidence:
        snippet = (mention.evidence.snippet_text or "")[:300]
    elif frame and frame.frame_text:
        snippet = frame.frame_text[:300]
    context = f" Context: {snippet}." if snippet else ""

    mtype = (mention.type or "").upper()
    if mtype == "ACTOR":
        name = fields.get("name") or fields.get("actor_name") or ""
        return f"ACTOR MENTION: {name}.{context}"
    if mtype == "OBJECT":
        name = fields.get("name") or fields.get("object_name") or ""
        return f"OBJECT MENTION: {name}.{context}"
    if mtype == "ACTION":
        actor = fields.get("actor_name") or fields.get("actor") or ""
        verb = fields.get("verb") or ""
        obj = fields.get("object_name") or fields.get("object") or ""
        return f"ACTION MENTION: {actor} {verb} {obj}.{context}".strip()
    if mtype == "STATE":
        state_name = fields.get("state_name") or fields.get("state") or ""
        obj_hint = fields.get("object_name") or fields.get("object") or ""
        return f"STATE MENTION: {state_name}. Object hint: {obj_hint}.{context}".strip()
    return f"MENTION: {mention.fields_json or ''}.{context}"


async def embed_mention_card(
    mention: Mention,
    frame: Frame | None,
    embed_client: EmbedClient,
) -> list[float]:
    """Build mention card text and return its embedding vector."""
    text = build_mention_card_text(mention, frame)
    vectors = await embed_client.embed_texts([text])
    if not vectors:
        raise RuntimeError("embed_texts returned no vector")
    return vectors[0]


async def get_canonical_candidates_for_mention(
    mention: Mention,
    frame: Frame | None,
    canonical_type: str,
    k: int,
    *,
    embed_client: EmbedClient,
    qdrant_client: AsyncQdrantClient,
    max_candidates: int = 3,
    session: Session | None = None,
) -> list[Candidate]:
    """Retrieve top canonical candidates for a mention. Deduplicates by canonical_id and returns up to max_candidates distinct.
    If session is provided, only candidates whose canonical_id exists in the canonicals table are returned (avoids FK violations)."""
    collection = _collection_for_type(canonical_type)
    vector = await embed_mention_card(mention, frame, embed_client)
    hits = await search_canonical_collection(
        qdrant_client, collection, vector, top_k=k, exclude_superseded=True
    )
    seen: set[str] = set()
    candidates = []
    for h in hits:
        cid = h.payload.get("canonical_id") or h.point_id
        if cid in seen:
            continue
        seen.add(cid)
        candidates.append(
            Candidate(
                canonical_id=cid,
                name=h.payload.get("name") or "",
                norm_name=h.payload.get("norm_name") or "",
                aliases=list(h.payload.get("aliases") or []),
                score=h.score,
                payload=dict(h.payload),
            )
        )
        if len(candidates) >= max_candidates:
            break
    if session is not None and candidates:
        ids = [c.canonical_id for c in candidates]
        existing = set(
            session.execute(select(Canonical.id).where(Canonical.id.in_(ids)))
            .scalars()
            .all()
        )
        candidates = [c for c in candidates if c.canonical_id in existing]
    return candidates


async def record_canonical_merge(
    session: Session,
    from_canonical_id: str,
    to_canonical_id: str,
    qdrant_client: AsyncQdrantClient,
    *,
    run_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Record a merge: insert canonical_merges, set superseded_by_id on from-canonical, set superseded_by in Qdrant payload."""
    from app.db.models.extraction import Canonical, CanonicalMerge

    repo = CanonicalRepo()
    from_c = repo.get_by_id(session, from_canonical_id)
    if not from_c:
        raise ValueError(f"From canonical {from_canonical_id} not found")
    session.add(
        CanonicalMerge(
            from_canonical_id=from_canonical_id,
            to_canonical_id=to_canonical_id,
            run_id=run_id,
            reason=reason,
        )
    )
    from_c.superseded_by_id = to_canonical_id
    session.flush()

    collection = _collection_for_type(from_c.canonical_type)
    await update_canonical_point_payload(
        qdrant_client,
        collection,
        from_canonical_id,
        {"superseded_by": to_canonical_id},
    )
