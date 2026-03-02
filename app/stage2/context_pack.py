"""Stage 2 context pack builder: seed + same-type neighbors + cross-type + optional canonicals."""
from __future__ import annotations

import asyncio
import json
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models.claim import Claim
from app.db.models.pipeline_run import PipelineRun
from app.db.repositories.chunk_repo import ChunkRepo
from app.stage1.cards import build_card_text, build_dedupe_key
from app.stage1.repo import Stage1ClaimRepo
from app.stage2.qdrant import (
    STAGE1_CANONICALS_COLLECTION,
    search_closest_canonical,
    search_similar_claims,
    seed_exists_in_collection,
)

# Unicode replacement character; some LLM APIs return empty when it appears in the prompt
_REPLACEMENT_CHAR = "\uFFFD"

# Unicode categories to replace with "?" so prompts don't trigger empty LLM responses
# Cc=control, Cf=format, Co=private use, Cn=not assigned, Cs=surrogate
_SANITIZE_CATEGORIES = frozenset(("Cc", "Cf", "Co", "Cn", "Cs"))


def _sanitize_string(s: str) -> str:
    """Replace invalid/problematic Unicode so the prompt does not trigger empty LLM responses."""
    if _REPLACEMENT_CHAR in s:
        s = s.replace(_REPLACEMENT_CHAR, "?")
    result = []
    for c in s:
        cat = unicodedata.category(c)
        if cat in _SANITIZE_CATEGORIES:
            result.append("?")
        else:
            result.append(c)
    return "".join(result)


def _sanitize_pack(obj: Any) -> Any:
    """Recursively sanitize all string values in the context pack (dict/list/str)."""
    if isinstance(obj, dict):
        return {k: _sanitize_pack(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_pack(v) for v in obj]
    if isinstance(obj, str):
        return _sanitize_string(obj)
    return obj


# Plan §04: cross-type counts per pass (max per type)
CROSS_TYPE_CAPS: dict[str, dict[str, int]] = {
    "ACTOR": {"ACTION": 3, "OBJECT": 1, "STATE": 1},
    "OBJECT": {"ACTION": 2, "ACTOR": 1, "STATE": 1},
    "STATE": {"OBJECT": 3, "ACTION": 3, "ACTOR": 0},
    "ACTION": {"ACTOR": 1, "OBJECT": 1, "STATE": 1},
}

SAME_TYPE_LIMIT = 3
CROSS_TYPE_SEARCH_LIMIT = 25

# Chunk excerpt: window around first evidence snippet (chars before, chars after)
CHUNK_EXCERPT_BEFORE = 100
CHUNK_EXCERPT_AFTER = 100
SAME_TYPE_CHUNK_BEFORE = 50
SAME_TYPE_CHUNK_AFTER = 50
CHUNK_FALLBACK_CHARS = 200

# Number of evidence items (with excerpts) to include for closest canonical claim
CANONICAL_EVIDENCE_COUNT = 5
# Max evidence items per neighbor block (each with its own excerpt)
NEIGHBOR_EVIDENCE_CAP = 5


def _parse_value(value_json: str) -> dict:
    if not value_json:
        return {}
    try:
        return json.loads(value_json) if isinstance(value_json, str) else value_json
    except (json.JSONDecodeError, TypeError):
        return {}


def _evidence_list_with_ids(claim: Claim) -> list[dict[str, Any]]:
    """Build evidence list with evidence_id, chunk_id, snippet for resolution and display."""
    out = []
    for ev in (claim.evidence or [])[:3]:
        out.append({
            "evidence_id": ev.id,
            "chunk_id": ev.chunk_id,
            "snippet": (ev.snippet_text or "").strip(),
        })
    return out


def _excerpt_around_evidence(
    chunk_repo: ChunkRepo,
    session: Session,
    ev: Any,
    before_chars: int,
    after_chars: int,
) -> str:
    """Get chunk excerpt centered on one evidence item. Same sanitization as _chunk_excerpt_around_first_evidence."""
    excerpt = chunk_repo.get_excerpt_around_snippet(
        session,
        ev.chunk_id,
        ev.snippet_text or "",
        before_chars=before_chars,
        after_chars=after_chars,
        fallback_chars=CHUNK_FALLBACK_CHARS,
    )
    if not excerpt:
        return ""
    excerpt = " ".join(excerpt.split())
    for q in ('"', '"', '"'):
        excerpt = excerpt.replace(q, " ")
    excerpt = " ".join(excerpt.split())
    return excerpt


def _chunk_excerpt_around_first_evidence(
    chunk_repo: ChunkRepo,
    session: Session,
    claim: Claim,
    before_chars: int,
    after_chars: int,
) -> str:
    """Get chunk excerpt centered on first evidence snippet; fallback if no evidence."""
    evidence = (claim.evidence or [])[:1]
    if evidence:
        ev = evidence[0]
        excerpt = chunk_repo.get_excerpt_around_snippet(
            session,
            ev.chunk_id,
            ev.snippet_text or "",
            before_chars=before_chars,
            after_chars=after_chars,
            fallback_chars=CHUNK_FALLBACK_CHARS,
        )
        if excerpt:
            excerpt = " ".join(excerpt.split())
            for q in ('"', '"', '"'):
                excerpt = excerpt.replace(q, " ")
            excerpt = " ".join(excerpt.split())
            return excerpt
    # No evidence or empty excerpt: fallback from claim's chunk
    if claim.chunk_id:
        mapping = chunk_repo.get_by_ids(
            session, [claim.chunk_id], max_text_chars=CHUNK_FALLBACK_CHARS
        )
        text = (mapping.get(claim.chunk_id) or "").strip()
        if text:
            return " ".join(text.split())
    return "(no evidence)"


def _evidence_list_with_excerpts(
    claim: Claim,
    chunk_repo: ChunkRepo,
    session: Session,
    limit: int,
    before_chars: int,
    after_chars: int,
) -> list[dict[str, Any]]:
    """Build evidence list with evidence_id, chunk_id, snippet, and chunk_excerpt per item."""
    out = []
    for ev in (claim.evidence or [])[:limit]:
        snippet = (ev.snippet_text or "").strip()
        chunk_excerpt = _excerpt_around_evidence(
            chunk_repo, session, ev, before_chars, after_chars
        )
        out.append({
            "evidence_id": ev.id,
            "chunk_id": ev.chunk_id,
            "snippet": snippet,
            "chunk_excerpt": chunk_excerpt,
        })
    return out


def _claim_to_seed_block(
    claim: Claim,
    chunk_repo: ChunkRepo,
    session: Session,
) -> dict[str, Any]:
    """Build seed block: value, evidence (with ids), chunk excerpt around first evidence."""
    value = _parse_value(claim.value_json)
    evidence_list = _evidence_list_with_ids(claim)
    chunk_excerpt = _chunk_excerpt_around_first_evidence(
        chunk_repo, session, claim,
        CHUNK_EXCERPT_BEFORE, CHUNK_EXCERPT_AFTER,
    )
    return {
        "claim_id": claim.id,
        "claim_type": claim.claim_type,
        "value": value,
        "evidence": evidence_list,
        "chunk_excerpt": chunk_excerpt,
    }


def _claim_to_canonical_block(
    claim: Claim,
    chunk_repo: ChunkRepo,
    session: Session,
    canonical_evidence_count: int,
) -> dict[str, Any]:
    """Build closest-canonical block: up to N evidence items, each with its own chunk excerpt."""
    value = _parse_value(claim.value_json)
    evidence_list = _evidence_list_with_excerpts(
        claim,
        chunk_repo,
        session,
        limit=canonical_evidence_count,
        before_chars=CHUNK_EXCERPT_BEFORE,
        after_chars=CHUNK_EXCERPT_AFTER,
    )
    return {
        "claim_id": claim.id,
        "claim_type": claim.claim_type,
        "value": value,
        "evidence": evidence_list,
    }


def _claim_to_neighbor_block(
    claim: Claim,
    chunk_repo: ChunkRepo,
    session: Session,
    same_type: bool,
) -> dict[str, Any]:
    """Build neighbor block: per-evidence chunk excerpts (no single block-level excerpt)."""
    value = _parse_value(claim.value_json)
    if same_type:
        before, after = SAME_TYPE_CHUNK_BEFORE, SAME_TYPE_CHUNK_AFTER
    else:
        before, after = CHUNK_EXCERPT_BEFORE, CHUNK_EXCERPT_AFTER
    evidence_list = _evidence_list_with_excerpts(
        claim,
        chunk_repo,
        session,
        limit=NEIGHBOR_EVIDENCE_CAP,
        before_chars=before,
        after_chars=after,
    )
    return {
        "claim_id": claim.id,
        "claim_type": claim.claim_type,
        "value": value,
        "evidence": evidence_list,
    }


def _build_resolution_map(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Build list of {claim_id, evidence_id, chunk_id, snippet_text} for all claims in pack."""
    resolution: list[dict[str, Any]] = []
    blocks = [pack.get("seed")] + (pack.get("same_type_neighbors") or []) + _cross_type_blocks(pack)
    closest = pack.get("closest_canonical")
    if closest:
        blocks = blocks + [closest]
    for block in blocks:
        if not block:
            continue
        claim_id = block.get("claim_id")
        for ev in block.get("evidence") or []:
            resolution.append({
                "claim_id": claim_id,
                "evidence_id": ev.get("evidence_id"),
                "chunk_id": ev.get("chunk_id"),
                "snippet_text": (ev.get("snippet") or "").strip(),
            })
    return resolution


def _cross_type_blocks(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten cross_type dict to list of blocks."""
    out = []
    for blocks in (pack.get("cross_type") or {}).values():
        out.extend(blocks or [])
    return out


class ContextPackBuilder:
    """Build a serializable context pack for one Stage-2 seed claim."""

    def __init__(
        self,
        session: Session,
        *,
        claim_repo: Stage1ClaimRepo | None = None,
        chunk_repo: ChunkRepo | None = None,
        collection_name: str = "stage1_cards",
        vector_size: int = 768,
        canonical_evidence_count: int = CANONICAL_EVIDENCE_COUNT,
    ):
        self._session = session
        self._claim_repo = claim_repo or Stage1ClaimRepo()
        self._chunk_repo = chunk_repo or ChunkRepo()
        self._collection_name = collection_name
        self._vector_size = vector_size
        self._canonical_evidence_count = canonical_evidence_count

    async def build(
        self,
        seed_claim_id: str,
        pass_kind: str,
        run_id: str,
        document_id: str,
    ) -> dict[str, Any] | None:
        """
        Build context pack for the seed claim. Returns serializable dict or None if seed not found.
        """
        claim_repo = self._claim_repo
        seed = claim_repo.get_claim_with_evidence(self._session, seed_claim_id)
        if not seed or seed.claim_type != pass_kind:
            return None

        run = self._session.get(PipelineRun, run_id)
        doc_id = document_id or (run.document_id if run else None)
        if not doc_id:
            return None

        # Optional safeguard: skip seed if not in Qdrant (e.g. stale DB state)
        if not await seed_exists_in_collection(
            seed_claim_id, collection_name=self._collection_name
        ):
            return None

        session = self._session
        chunk_repo = self._chunk_repo

        pack: dict[str, Any] = {
            "pass_kind": pass_kind,
            "seed": _claim_to_seed_block(seed, chunk_repo, session),
            "same_type_neighbors": [],
            "cross_type": {},
        }

        # Same-type neighbors (6-10)
        same_type_hits = await search_similar_claims(
            seed_claim_id,
            doc_id,
            pass_kind,
            collection_name=self._collection_name,
            vector_size=self._vector_size,
            same_type_limit=SAME_TYPE_LIMIT,
            same_type_only=True,
        )
        same_type_ids = [h.claim_id for h in same_type_hits]
        if same_type_ids:
            neighbors = self._load_claims_with_evidence(same_type_ids)
            pack["same_type_neighbors"] = [
                _claim_to_neighbor_block(c, chunk_repo, session, same_type=True)
                for c in neighbors
            ]

        # Closest canonical: search stage1_canonicals (ACCEPTED only) so LLM can choose MERGE_INTO
        closest_hit = await search_closest_canonical(
            seed_claim_id,
            doc_id,
            pass_kind,
            collection_name_cards=self._collection_name,
            collection_name_canonicals=STAGE1_CANONICALS_COLLECTION,
            vector_size=self._vector_size,
        )
        if closest_hit:
            canonical_claims = self._load_claims_with_evidence([closest_hit.claim_id])
            if canonical_claims:
                c = canonical_claims[0]
                pack["closest_canonical"] = _claim_to_canonical_block(
                    c,
                    chunk_repo,
                    session,
                    self._canonical_evidence_count,
                )

        # Cross-type: mixed search, then partition by type and cap
        cross_caps = CROSS_TYPE_CAPS.get(pass_kind, {})
        if cross_caps:
            mixed_hits = await search_similar_claims(
                seed_claim_id,
                doc_id,
                pass_kind,
                collection_name=self._collection_name,
                vector_size=self._vector_size,
                same_type_limit=CROSS_TYPE_SEARCH_LIMIT,
                same_type_only=False,
            )
            # Exclude same-type; group by claim_type
            by_type: dict[str, list[str]] = {}
            for h in mixed_hits:
                if h.claim_id == seed_claim_id:
                    continue
                payload = h.payload or {}
                ct = payload.get("claim_type")
                if not ct or ct == pass_kind:
                    continue
                cap = cross_caps.get(ct, 0)
                if cap <= 0:
                    continue
                if ct not in by_type:
                    by_type[ct] = []
                if len(by_type[ct]) < cap:
                    by_type[ct].append(h.claim_id)
            cross_ids = []
            for ct, ids in by_type.items():
                pack["cross_type"][ct] = []
                cross_ids.extend(ids)
            if cross_ids:
                cross_claims = self._load_claims_with_evidence(cross_ids)
                for c in cross_claims:
                    if c.claim_type in pack["cross_type"] and len(pack["cross_type"][c.claim_type]) < cross_caps.get(c.claim_type, 0):
                        pack["cross_type"][c.claim_type].append(
                            _claim_to_neighbor_block(c, chunk_repo, session, same_type=False)
                        )

        pack["_resolution"] = _build_resolution_map(pack)
        return _sanitize_pack(pack)

    def _load_claims_with_evidence(self, claim_ids: list[str]) -> list[Claim]:
        """Load claims by ids with evidence eager-loaded."""
        if not claim_ids:
            return []
        q = (
            select(Claim)
            .where(Claim.id.in_(claim_ids))
            .options(joinedload(Claim.evidence))
        )
        return list(self._session.execute(q).unique().scalars().all())


def build_context_pack_sync(
    session: Session,
    seed_claim_id: str,
    pass_kind: str,
    run_id: str,
    document_id: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Synchronous wrapper: run ContextPackBuilder.build in event loop."""
    builder = ContextPackBuilder(session, **kwargs)
    return asyncio.run(builder.build(seed_claim_id, pass_kind, run_id, document_id))
