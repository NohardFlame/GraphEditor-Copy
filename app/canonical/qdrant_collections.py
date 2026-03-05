"""Qdrant collection definitions and bootstrap for canonical entities (Phase 4)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_http

from app.vectorstore.errors import VectorSchemaMismatchError

logger = logging.getLogger(__name__)

COLLECTION_ACTORS = "canonical_actors"
COLLECTION_OBJECTS = "canonical_objects"
COLLECTION_ACTIONS = "canonical_actions"
COLLECTION_OBJECT_STATES = "canonical_object_states"

CANONICAL_COLLECTION_NAMES = (
    COLLECTION_ACTORS,
    COLLECTION_OBJECTS,
    COLLECTION_ACTIONS,
    COLLECTION_OBJECT_STATES,
)

DISTANCE = "Cosine"


@dataclass(frozen=True)
class CanonicalCollectionSpec:
    """Spec for one canonical Qdrant collection."""

    collection_name: str
    vector_size: int
    distance: str = DISTANCE


def _qdrant_distance(s: str) -> qdrant_http.Distance:
    m = {
        "Cosine": qdrant_http.Distance.COSINE,
        "cosine": qdrant_http.Distance.COSINE,
    }
    return m.get(s, qdrant_http.Distance.COSINE)


async def _ensure_payload_index(
    client: AsyncQdrantClient,
    collection_name: str,
    field_name: str,
    schema_type: qdrant_http.PayloadSchemaType,
) -> None:
    try:
        await client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=qdrant_http.PayloadSchemaType(schema_type),
        )
    except Exception:
        pass


async def _ensure_canonical_common_indexes(
    client: AsyncQdrantClient,
    collection_name: str,
) -> None:
    """Create payload indexes common to all canonical collections."""
    indexes = [
        ("norm_name", qdrant_http.PayloadSchemaType.TEXT),
        ("canonical_type", qdrant_http.PayloadSchemaType.KEYWORD),
        ("created_at", qdrant_http.PayloadSchemaType.DATETIME),
    ]
    for field, schema_type in indexes:
        await _ensure_payload_index(client, collection_name, field, schema_type)


async def _ensure_action_indexes(client: AsyncQdrantClient, collection_name: str) -> None:
    indexes = [
        ("actor_id", qdrant_http.PayloadSchemaType.KEYWORD),
        ("object_id", qdrant_http.PayloadSchemaType.KEYWORD),
        ("verb_norm", qdrant_http.PayloadSchemaType.TEXT),
    ]
    for field, schema_type in indexes:
        await _ensure_payload_index(client, collection_name, field, schema_type)


async def _ensure_state_indexes(client: AsyncQdrantClient, collection_name: str) -> None:
    indexes = [
        ("canonical_object_id", qdrant_http.PayloadSchemaType.KEYWORD),
        ("state_norm", qdrant_http.PayloadSchemaType.TEXT),
    ]
    for field, schema_type in indexes:
        await _ensure_payload_index(client, collection_name, field, schema_type)


async def ensure_canonical_collections(
    client: AsyncQdrantClient,
    vector_size: int,
) -> None:
    """Create or validate all four canonical collections. Validate vector size and Cosine distance.

    Raises VectorSchemaMismatchError if an existing collection has wrong size or distance.
    """
    distance = _qdrant_distance(DISTANCE)
    for name in CANONICAL_COLLECTION_NAMES:
        spec = CanonicalCollectionSpec(collection_name=name, vector_size=vector_size)
        exists = await client.collection_exists(name)
        if not exists:
            await client.create_collection(
                collection_name=name,
                vectors_config=qdrant_http.VectorParams(size=vector_size, distance=distance),
                hnsw_config=qdrant_http.HnswConfigDiff(m=16, ef_construct=128),
            )
            await _ensure_canonical_common_indexes(client, name)
            if name == COLLECTION_ACTIONS:
                await _ensure_action_indexes(client, name)
            elif name == COLLECTION_OBJECT_STATES:
                await _ensure_state_indexes(client, name)
            logger.info("Created canonical collection %s with vector_size=%s", name, vector_size)
        else:
            info = await client.get_collection(name)
            vectors_config = info.config.params.vectors
            if isinstance(vectors_config, qdrant_http.VectorParams):
                if vectors_config.size != vector_size or vectors_config.distance != distance:
                    raise VectorSchemaMismatchError(
                        f"Collection {name} has size={vectors_config.size} distance={vectors_config.distance}, "
                        f"expected size={vector_size} distance={distance}."
                    )
            await _ensure_canonical_common_indexes(client, name)
            if name == COLLECTION_ACTIONS:
                await _ensure_action_indexes(client, name)
            elif name == COLLECTION_OBJECT_STATES:
                await _ensure_state_indexes(client, name)
