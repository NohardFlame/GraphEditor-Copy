"""Repositories for CRUD and idempotent operations.

CanonicalRepo is not imported here to avoid circular imports (canonical_repo -> app.canonical -> service -> canonical_repo).
Import it as: from app.db.repositories.canonical_repo import CanonicalRepo
"""
from app.db.repositories.workspace_repo import WorkspaceRepo
from app.db.repositories.source_repo import SourceRepo, SourceVersionRepo
from app.db.repositories.document_repo import DocumentRepo
from app.db.repositories.chunk_repo import ChunkRepo, ChunkPayload
from app.db.repositories.embedding_repo import EmbeddingSetRepo, ChunkEmbeddingRepo
from app.db.repositories.run_repo import RunRepo, RunItemRepo, EventRepo
from app.db.repositories.query_repo import QueryRepo
from app.db.repositories.extract_run_repo import ExtractRunRepo

__all__ = [
    "WorkspaceRepo",
    "SourceRepo",
    "SourceVersionRepo",
    "DocumentRepo",
    "ChunkRepo",
    "ChunkPayload",
    "EmbeddingSetRepo",
    "ChunkEmbeddingRepo",
    "RunRepo",
    "RunItemRepo",
    "EventRepo",
    "QueryRepo",
    "ExtractRunRepo",
]
