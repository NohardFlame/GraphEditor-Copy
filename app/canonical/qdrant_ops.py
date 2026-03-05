"""Thin Qdrant operations for canonical collections (Phase 4)."""
from __future__ import annotations

from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_http


@dataclass
class CanonicalSearchHit:
    """One search result from a canonical collection."""

    point_id: str
    score: float
    payload: dict


def _serialize_payload(payload: dict) -> dict:
    """Ensure payload is JSON-serializable for Qdrant (e.g. datetime -> isoformat)."""
    out = {}
    for k, v in payload.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out


async def upsert_canonical_point(
    client: AsyncQdrantClient,
    collection_name: str,
    point_id: str,
    vector: list[float],
    payload: dict,
) -> None:
    """Upsert a single point into a canonical collection. point_id = canonical_id (or state id)."""
    payload_ser = _serialize_payload(payload)
    await client.upsert(
        collection_name=collection_name,
        points=[
            qdrant_http.PointStruct(
                id=point_id,
                vector=vector,
                payload=payload_ser,
            )
        ],
    )


async def search_canonical_collection(
    client: AsyncQdrantClient,
    collection_name: str,
    query_vector: list[float],
    top_k: int = 10,
    *,
    exclude_superseded: bool = True,
) -> list[CanonicalSearchHit]:
    """Search a canonical collection by vector. Optionally exclude points with superseded_by set."""
    # Exclude superseded: filter in Python after search (Qdrant MatchValue does not accept null).
    query_filter = None
    response = await client.query_points(
        collection_name=collection_name,
        query=query_vector,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    hits = []
    for r in response.points or []:
        pid = str(r.id) if r.id is not None else ""
        payload = dict(r.payload or {})
        if exclude_superseded and payload.get("superseded_by"):
            continue
        hits.append(
            CanonicalSearchHit(
                point_id=pid,
                score=float(r.score or 0.0),
                payload=payload,
            )
        )
    return hits


async def update_canonical_point_payload(
    client: AsyncQdrantClient,
    collection_name: str,
    point_id: str,
    payload_update: dict,
) -> None:
    """Set payload fields for an existing point (e.g. superseded_by). Overwrites only given keys."""
    await client.set_payload(
        collection_name=collection_name,
        payload=payload_update,
        points=[point_id],
    )


async def delete_canonical_point(
    client: AsyncQdrantClient,
    collection_name: str,
    point_id: str,
) -> None:
    """Delete one point by id."""
    await client.delete(
        collection_name=collection_name,
        points_selector=qdrant_http.PointIdsList(points=[point_id]),
    )
