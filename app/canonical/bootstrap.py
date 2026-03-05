"""Bootstrap canonical Qdrant collections at startup (Phase 4)."""
from __future__ import annotations

import logging

from app.canonical.qdrant_collections import ensure_canonical_collections
from app.embeddings.settings import EmbedSettings
from app.vectorstore import build_qdrant_client

logger = logging.getLogger(__name__)


async def bootstrap_canonical_collections() -> None:
    """Ensure all four canonical Qdrant collections exist with correct vector size and distance.

    Call once at application startup (e.g. FastAPI lifespan or worker init).
    Vector size is taken from EmbedSettings.dims (must match embedding model, e.g. 768 for nomic-embed-text).
    """
    settings = EmbedSettings()
    vector_size = settings.dims
    client = build_qdrant_client()
    await ensure_canonical_collections(client, vector_size)
    logger.info(
        "Canonical collections bootstrap complete (vector_size=%s)",
        vector_size,
    )
