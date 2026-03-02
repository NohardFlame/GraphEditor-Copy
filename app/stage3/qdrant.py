"""Stage 3 Qdrant: ensure collection, upsert card points, search candidates for state/action."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models as qdrant_models

from app.vectorstore.client import build_qdrant_client
from app.vectorstore.collections import ensure_stage3_cards_collection

STAGE3_CARDS_COLLECTION = "stage3_cards"

# Namespace for deterministic UUIDs: one point id per (canonical_claim_id, kind).
_STAGE3_POINT_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-5789-0abc-def012345678")


def _stage3_point_id(canonical_claim_id: str, kind: str) -> str:
    """Unique point id per (canonical_claim_id, kind). Returns a valid Qdrant UUID (no suffix)."""
    name = f"{canonical_claim_id}_{kind}"
    return str(uuid.uuid5(_STAGE3_POINT_NAMESPACE, name))


@dataclass
class Stage3CardHit:
    """One card from Stage-3 collection search."""
    canonical_claim_id: str
    kind: str
    score: float
    payload: dict


async def ensure_collection(
    collection_name: str = STAGE3_CARDS_COLLECTION,
    vector_size: int = 768,
    client: AsyncQdrantClient | None = None,
) -> None:
    """Ensure Stage-3 cards collection exists."""
    c = client or build_qdrant_client()
    await ensure_stage3_cards_collection(c, collection_name, vector_size, "Cosine")


async def upsert_card(
    point_id: str,
    vector: list[float],
    doc_id: str,
    canonical_claim_id: str,
    kind: str,
    *,
    collection_name: str = STAGE3_CARDS_COLLECTION,
    prompt_version: str | None = None,
    model_id: str | None = None,
    embedding_model_id: str | None = None,
    payload_extra: dict | None = None,
    client: AsyncQdrantClient | None = None,
) -> None:
    """Upsert one card point into Stage-3 collection."""
    c = client or build_qdrant_client()
    payload = {
        "doc_id": doc_id,
        "canonical_claim_id": canonical_claim_id,
        "kind": kind,
        "prompt_version": prompt_version,
        "model_id": model_id,
        "embedding_model_id": embedding_model_id,
    }
    if payload_extra:
        payload.update(payload_extra)
    point = qdrant_models.PointStruct(
        id=point_id,
        vector=vector,
        payload=payload,
    )
    await c.upsert(collection_name=collection_name, points=[point])


async def search_candidate_objects(
    query_vector: list[float],
    doc_id: str,
    *,
    collection_name: str = STAGE3_CARDS_COLLECTION,
    limit: int = 15,
    client: AsyncQdrantClient | None = None,
) -> list[Stage3CardHit]:
    """Search Stage-3 collection for OBJECT cards in the same document (for state resolution)."""
    c = client or build_qdrant_client()
    query_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="doc_id",
                match=qdrant_models.MatchValue(value=doc_id),
            ),
            qdrant_models.FieldCondition(
                key="kind",
                match=qdrant_models.MatchValue(value="OBJECT"),
            ),
        ],
    )
    response = await c.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    out = []
    for p in response.points or []:
        payload = dict(p.payload or {})
        cid = payload.get("canonical_claim_id") or str(p.id)
        kind = payload.get("kind") or "OBJECT"
        out.append(
            Stage3CardHit(
                canonical_claim_id=cid,
                kind=kind,
                score=float(p.score or 0.0),
                payload=payload,
            )
        )
    return out


async def search_candidate_actors_and_objects(
    query_vector: list[float],
    doc_id: str,
    *,
    collection_name: str = STAGE3_CARDS_COLLECTION,
    limit_per_kind: int = 10,
    client: AsyncQdrantClient | None = None,
) -> tuple[list[Stage3CardHit], list[Stage3CardHit]]:
    """Search Stage-3 for ACTOR and OBJECT cards in the same document (for action resolution). Returns (actors, objects)."""
    c = client or build_qdrant_client()
    # Single query with filter kind in (ACTOR, OBJECT), then partition
    query_filter = qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="doc_id",
                match=qdrant_models.MatchValue(value=doc_id),
            ),
        ],
        should=[
            qdrant_models.FieldCondition(
                key="kind",
                match=qdrant_models.MatchValue(value="ACTOR"),
            ),
            qdrant_models.FieldCondition(
                key="kind",
                match=qdrant_models.MatchValue(value="OBJECT"),
            ),
        ],
    )
    response = await c.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit_per_kind * 2,
        with_payload=True,
        with_vectors=False,
    )
    actors: list[Stage3CardHit] = []
    objects: list[Stage3CardHit] = []
    for p in response.points or []:
        payload = dict(p.payload or {})
        cid = payload.get("canonical_claim_id") or str(p.id)
        kind = payload.get("kind") or "OBJECT"
        hit = Stage3CardHit(
            canonical_claim_id=cid,
            kind=kind,
            score=float(p.score or 0.0),
            payload=payload,
        )
        if kind == "ACTOR":
            actors.append(hit)
        else:
            objects.append(hit)
    return actors[:limit_per_kind], objects[:limit_per_kind]
