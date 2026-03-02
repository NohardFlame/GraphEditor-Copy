"""Stage 2 Qdrant helper: retrieve seed vector, search similar, unique by dedupe_key; canonicals collection."""
from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models as qdrant_models

from app.vectorstore.client import build_qdrant_client
from app.vectorstore.collections import (
    ensure_stage1_canonicals_collection,
    ensure_stage1_cards_collection,
)

STAGE1_CANONICALS_COLLECTION = "stage1_canonicals"


async def seed_exists_in_collection(
    claim_id: str,
    *,
    collection_name: str = "stage1_cards",
    client: AsyncQdrantClient | None = None,
) -> bool:
    """Return True if the claim has a point with a vector in the collection (safety check for context pack)."""
    c = client or build_qdrant_client()
    try:
        records = await c.retrieve(
            collection_name=collection_name,
            ids=[claim_id],
            with_vectors=True,
            with_payload=False,
        )
    except Exception:
        return False
    if not records or not records[0].vector:
        return False
    return True


@dataclass(frozen=True)
class SimilarClaimHit:
    """One claim from similarity search after dedupe_key collapse."""
    claim_id: str
    score: float
    dedupe_key: str | None
    payload: dict


async def search_similar_claims(
    seed_claim_id: str,
    doc_id: str,
    pass_kind: str,
    *,
    collection_name: str = "stage1_cards",
    vector_size: int = 768,
    same_type_limit: int = 15,
    same_type_only: bool = False,
    client: AsyncQdrantClient | None = None,
) -> list[SimilarClaimHit]:
    """
    Retrieve seed claim vector from stage1_cards, search top-K similar, dedupe by dedupe_key.

    - same_type_only: if True, filter by claim_type == pass_kind (ACTOR/OBJECT/STATE/ACTION).
    - Returns at most same_type_limit results after keeping best score per dedupe_key, excluding seed.
    """
    c = client or build_qdrant_client()
    await ensure_stage1_cards_collection(c, collection_name, vector_size, "Cosine")

    # Retrieve seed point to get its vector (point id = claim_id in stage1_cards)
    try:
        records = await c.retrieve(
            collection_name=collection_name,
            ids=[seed_claim_id],
            with_vectors=True,
            with_payload=False,
        )
    except Exception:
        return []

    if not records or not records[0].vector:
        return []

    query_vector = records[0].vector
    if isinstance(query_vector, dict):
        # Named vectors: use default key
        query_vector = list(query_vector.values())[0] if query_vector else []
    query_vector = list(query_vector)

    if not query_vector:
        return []

    # Build filter: scope to same document (stage1_cards payload uses "doc_id"); exclude seed
    must: list[qdrant_models.Condition] = [
        qdrant_models.FieldCondition(
            key="doc_id",
            match=qdrant_models.MatchValue(value=doc_id),
        ),
    ]
    if same_type_only:
        must.append(
            qdrant_models.FieldCondition(
                key="claim_type",
                match=qdrant_models.MatchValue(value=pass_kind),
            ),
        )
    query_filter = qdrant_models.Filter(
        must=must,
        must_not=[qdrant_models.HasIdCondition(has_id=[seed_claim_id])],
    )

    limit = min(100, same_type_limit * 3)  # fetch extra for dedupe
    response = await c.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )

    points = response.points or []
    # Dedupe by dedupe_key: keep best score per key
    by_key: dict[str, SimilarClaimHit] = {}
    for p in points:
        pid = str(p.id) if p.id is not None else ""
        if pid == seed_claim_id:
            continue
        payload = dict(p.payload or {})
        dk = payload.get("dedupe_key") or ""
        score = float(p.score or 0.0)
        hit = SimilarClaimHit(claim_id=pid, score=score, dedupe_key=dk or None, payload=payload)
        if dk:
            if dk not in by_key or by_key[dk].score < score:
                by_key[dk] = hit
        else:
            by_key[pid] = hit  # no key: use claim_id as key so we keep one

    # Sort by score descending, take same_type_limit
    ordered = sorted(by_key.values(), key=lambda h: -h.score)
    return ordered[:same_type_limit]


async def search_closest_canonical(
    seed_claim_id: str,
    doc_id: str,
    pass_kind: str,
    *,
    collection_name_cards: str = "stage1_cards",
    collection_name_canonicals: str = STAGE1_CANONICALS_COLLECTION,
    vector_size: int = 768,
    client: AsyncQdrantClient | None = None,
) -> SimilarClaimHit | None:
    """
    Return the closest ACCEPTED (canonical) same-type claim for the seed from the canonicals collection.
    Uses seed vector from stage1_cards and queries stage1_canonicals with doc_id + claim_type filter.
    """
    c = client or build_qdrant_client()
    await ensure_stage1_cards_collection(c, collection_name_cards, vector_size, "Cosine")
    await ensure_stage1_canonicals_collection(c, collection_name_canonicals, vector_size, "Cosine")

    try:
        records = await c.retrieve(
            collection_name=collection_name_cards,
            ids=[seed_claim_id],
            with_vectors=True,
            with_payload=False,
        )
    except Exception:
        return None
    if not records or not records[0].vector:
        return None

    query_vector = records[0].vector
    if isinstance(query_vector, dict):
        query_vector = list(query_vector.values())[0] if query_vector else []
    query_vector = list(query_vector)
    if not query_vector:
        return None

    query_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="doc_id",
                match=qdrant_models.MatchValue(value=doc_id),
            ),
            qdrant_models.FieldCondition(
                key="claim_type",
                match=qdrant_models.MatchValue(value=pass_kind),
            ),
        ],
    )
    response = await c.query_points(
        collection_name=collection_name_canonicals,
        query=query_vector,
        query_filter=query_filter,
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    points = response.points or []
    if not points:
        return None
    p = points[0]
    pid = str(p.id) if p.id is not None else ""
    payload = dict(p.payload or {})
    score = float(p.score or 0.0)
    return SimilarClaimHit(
        claim_id=pid,
        score=score,
        dedupe_key=payload.get("dedupe_key"),
        payload=payload,
    )


async def upsert_claim_to_canonicals(
    claim_id: str,
    *,
    collection_name_cards: str = "stage1_cards",
    collection_name_canonicals: str = STAGE1_CANONICALS_COLLECTION,
    vector_size: int = 768,
    client: AsyncQdrantClient | None = None,
) -> None:
    """Copy the claim's point from stage1_cards to stage1_canonicals (e.g. after ACCEPT_AS_CANONICAL or MERGE_INTO)."""
    c = client or build_qdrant_client()
    await ensure_stage1_canonicals_collection(c, collection_name_canonicals, vector_size, "Cosine")

    try:
        records = await c.retrieve(
            collection_name=collection_name_cards,
            ids=[claim_id],
            with_vectors=True,
            with_payload=True,
        )
    except Exception:
        return
    if not records or not records[0].vector:
        return

    r = records[0]
    vector = r.vector
    if isinstance(vector, dict):
        vector = list(vector.values())[0] if vector else []
    vector = list(vector)
    if not vector:
        return

    point = qdrant_models.PointStruct(
        id=r.id,
        vector=vector,
        payload=dict(r.payload or {}),
    )
    await c.upsert(collection_name=collection_name_canonicals, points=[point])


async def delete_claim_points(
    collection_name: str,
    claim_ids: list[str],
    *,
    client: AsyncQdrantClient | None = None,
) -> None:
    """Delete points for the given claim ids from the collection (e.g. after REJECT or MERGE_INTO)."""
    if not claim_ids:
        return
    c = client or build_qdrant_client()
    await c.delete(
        collection_name=collection_name,
        points_selector=qdrant_models.PointIdsList(points=claim_ids),
    )
