"""Collection creation, validation, and naming."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_http

from app.vectorstore.errors import VectorSchemaMismatchError
from app.vectorstore.settings import VectorStoreSettings


class EmbeddingSetSpec(Protocol):
    """Protocol for embedding set data (from SQL EmbeddingSet or similar)."""

    id: str
    workspace_id: str
    qdrant_collection: str
    dims: int
    distance: str


@dataclass(frozen=True)
class EmbeddingSetSpecValue:
    """Session-independent value object satisfying EmbeddingSetSpec. Use when passing spec across session/async boundaries."""

    id: str
    workspace_id: str
    qdrant_collection: str
    dims: int
    distance: str


def compute_collection_name(
    workspace_id: str,
    embedding_set_id: str,
    schema_version: int,
) -> str:
    """Compute deterministic collection name when qdrant_collection is empty.

    Pattern: ws_{workspace_id}__emb_{embedding_set_id}__v{schema_version}
    """
    return f"ws_{workspace_id}__emb_{embedding_set_id}__v{schema_version}"


def _qdrant_distance(s: str) -> qdrant_http.Distance:
    """Map string distance to Qdrant enum."""
    m = {
        "Cosine": qdrant_http.Distance.COSINE,
        "cosine": qdrant_http.Distance.COSINE,
        "Dot": qdrant_http.Distance.DOT,
        "dot": qdrant_http.Distance.DOT,
        "Euclid": qdrant_http.Distance.EUCLID,
        "euclid": qdrant_http.Distance.EUCLID,
    }
    return m.get(s, qdrant_http.Distance.COSINE)


async def ensure_collection(
    client: AsyncQdrantClient,
    embedding_set: EmbeddingSetSpec,
    settings: VectorStoreSettings | None = None,
) -> None:
    """Ensure collection exists with correct schema. Create if missing. Validate if exists.

    Raises VectorSchemaMismatchError if existing collection has wrong vector_size or distance.
    """
    s = settings or VectorStoreSettings()
    collection_name = embedding_set.qdrant_collection
    vector_size = embedding_set.dims
    distance = _qdrant_distance(embedding_set.distance)

    exists = await client.collection_exists(collection_name)
    if not exists:
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qdrant_http.VectorParams(size=vector_size, distance=distance),
            hnsw_config=qdrant_http.HnswConfigDiff(
                m=16,
                ef_construct=128,
            ),
        )
        await _ensure_payload_indexes(client, collection_name)
        return

    info = await client.get_collection(collection_name)
    # Validate existing collection
    vectors_config = info.config.params.vectors
    if isinstance(vectors_config, qdrant_http.VectorParams):
        if vectors_config.size != vector_size or vectors_config.distance != distance:
            raise VectorSchemaMismatchError(
                f"Collection {collection_name} has size={vectors_config.size} distance={vectors_config.distance}, "
                f"expected size={vector_size} distance={distance}. Migration (new collection + reindex) required."
            )
    await _ensure_payload_indexes(client, collection_name)


async def _ensure_payload_indexes(client: AsyncQdrantClient, collection_name: str) -> None:
    """Create payload indexes if they don't exist."""
    indexes = [
        ("workspace_id", qdrant_http.PayloadSchemaType.KEYWORD),
        ("document_id", qdrant_http.PayloadSchemaType.KEYWORD),
        ("chunk_index", qdrant_http.PayloadSchemaType.INTEGER),
        ("embedded_at", qdrant_http.PayloadSchemaType.DATETIME),
    ]
    for field, schema_type in indexes:
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=qdrant_http.PayloadSchemaType(schema_type),
            )
        except Exception:
            # Index may already exist
            pass


# Stage-1 claim cards collection: fixed name, claim payload indexes
STAGE1_CARDS_PAYLOAD_INDEXES = [
    ("doc_id", qdrant_http.PayloadSchemaType.KEYWORD),
    ("chunk_id", qdrant_http.PayloadSchemaType.KEYWORD),
    ("claim_type", qdrant_http.PayloadSchemaType.KEYWORD),
    ("prompt_version", qdrant_http.PayloadSchemaType.KEYWORD),
    ("extractor_version", qdrant_http.PayloadSchemaType.KEYWORD),
    ("dedupe_key", qdrant_http.PayloadSchemaType.KEYWORD),
]


async def ensure_stage1_cards_collection(
    client: AsyncQdrantClient,
    collection_name: str,
    vector_size: int,
    distance: str = "Cosine",
) -> None:
    """Ensure Stage-1 claim cards collection exists. Create if missing; validate if exists.

    Raises VectorSchemaMismatchError if existing collection has wrong vector_size or distance.
    """
    dist = _qdrant_distance(distance)
    exists = await client.collection_exists(collection_name)
    if not exists:
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qdrant_http.VectorParams(size=vector_size, distance=dist),
            hnsw_config=qdrant_http.HnswConfigDiff(
                m=16,
                ef_construct=128,
            ),
        )
        for field, schema_type in STAGE1_CARDS_PAYLOAD_INDEXES:
            try:
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=qdrant_http.PayloadSchemaType(schema_type),
                )
            except Exception:
                pass
        return

    info = await client.get_collection(collection_name)
    vectors_config = info.config.params.vectors
    if isinstance(vectors_config, qdrant_http.VectorParams):
        if vectors_config.size != vector_size or vectors_config.distance != dist:
            raise VectorSchemaMismatchError(
                f"Collection {collection_name} has size={vectors_config.size} distance={vectors_config.distance}, "
                f"expected size={vector_size} distance={dist}. Migration (new collection + reindex) required."
            )
    for field, schema_type in STAGE1_CARDS_PAYLOAD_INDEXES:
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=qdrant_http.PayloadSchemaType(schema_type),
            )
        except Exception:
            pass


async def ensure_stage1_canonicals_collection(
    client: AsyncQdrantClient,
    collection_name: str,
    vector_size: int,
    distance: str = "Cosine",
) -> None:
    """Ensure Stage-1 canonicals collection exists (same schema as stage1_cards). Create if missing; validate if exists."""
    dist = _qdrant_distance(distance)
    exists = await client.collection_exists(collection_name)
    if not exists:
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qdrant_http.VectorParams(size=vector_size, distance=dist),
            hnsw_config=qdrant_http.HnswConfigDiff(
                m=16,
                ef_construct=128,
            ),
        )
        for field, schema_type in STAGE1_CARDS_PAYLOAD_INDEXES:
            try:
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=qdrant_http.PayloadSchemaType(schema_type),
                )
            except Exception:
                pass
        return

    info = await client.get_collection(collection_name)
    vectors_config = info.config.params.vectors
    if isinstance(vectors_config, qdrant_http.VectorParams):
        if vectors_config.size != vector_size or vectors_config.distance != dist:
            raise VectorSchemaMismatchError(
                f"Collection {collection_name} has size={vectors_config.size} distance={vectors_config.distance}, "
                f"expected size={vector_size} distance={dist}. Migration (new collection + reindex) required."
            )
    for field, schema_type in STAGE1_CARDS_PAYLOAD_INDEXES:
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=qdrant_http.PayloadSchemaType(schema_type),
            )
        except Exception:
            pass


# Stage-3 cards: payload indexes for retrieval by doc_id and kind
STAGE3_CARDS_PAYLOAD_INDEXES = [
    ("doc_id", qdrant_http.PayloadSchemaType.KEYWORD),
    ("canonical_claim_id", qdrant_http.PayloadSchemaType.KEYWORD),
    ("kind", qdrant_http.PayloadSchemaType.KEYWORD),
    ("prompt_version", qdrant_http.PayloadSchemaType.KEYWORD),
    ("model_id", qdrant_http.PayloadSchemaType.KEYWORD),
    ("embedding_model_id", qdrant_http.PayloadSchemaType.KEYWORD),
]


async def ensure_stage3_cards_collection(
    client: AsyncQdrantClient,
    collection_name: str,
    vector_size: int,
    distance: str = "Cosine",
) -> None:
    """Ensure Stage-3 cards collection exists. Create if missing; validate if exists."""
    dist = _qdrant_distance(distance)
    exists = await client.collection_exists(collection_name)
    if not exists:
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qdrant_http.VectorParams(size=vector_size, distance=dist),
            hnsw_config=qdrant_http.HnswConfigDiff(
                m=16,
                ef_construct=128,
            ),
        )
        for field, schema_type in STAGE3_CARDS_PAYLOAD_INDEXES:
            try:
                await client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=qdrant_http.PayloadSchemaType(schema_type),
                )
            except Exception:
                pass
        return

    info = await client.get_collection(collection_name)
    vectors_config = info.config.params.vectors
    if isinstance(vectors_config, qdrant_http.VectorParams):
        if vectors_config.size != vector_size or vectors_config.distance != dist:
            raise VectorSchemaMismatchError(
                f"Collection {collection_name} has size={vectors_config.size} distance={vectors_config.distance}, "
                f"expected size={vector_size} distance={dist}. Migration (new collection + reindex) required."
            )
    for field, schema_type in STAGE3_CARDS_PAYLOAD_INDEXES:
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=qdrant_http.PayloadSchemaType(schema_type),
            )
        except Exception:
            pass
