"""Ingest a real file into the DB as a document with chunks (for manual LLM extraction)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.repositories.document_repo import DocumentRepo
from app.db.repositories.source_repo import SourceRepo, SourceVersionRepo
from app.db.repositories.workspace_repo import WorkspaceRepo
from app.extraction.docling_chunker import docling_chunk


def ingest_file(
    session: Session,
    file_path: str | Path,
    *,
    workspace_name: str = "manual",
    extractor: str = "docling",
    extractor_version: str = "1",
) -> str:
    """Create workspace/source/version/document and chunk the file with Docling. Returns document id.

    Use the returned doc_id with prepare_big_llm_export for the manual LLM path.
    """
    path = Path(file_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Not a file: {path}")

    content = path.read_bytes()
    content_sha256 = hashlib.sha256(content).hexdigest()
    storage_uri = path.as_uri()
    size_bytes = len(content)
    ingested_at = datetime.now(timezone.utc)

    wr = WorkspaceRepo()
    sr = SourceRepo()
    svr = SourceVersionRepo()
    dr = DocumentRepo()

    workspace = wr.get_by_name(session, workspace_name)
    if not workspace:
        workspace = wr.create(session, workspace_name)

    sources = sr.list_by_workspace(session, workspace.id)
    file_source = next((s for s in sources if s.source_type == "file"), None)
    if not file_source:
        file_source = sr.create(
            session,
            workspace_id=workspace.id,
            source_type="file",
            title="File ingest",
        )

    version = svr.create_or_get(
        session,
        source_id=file_source.id,
        content_sha256=content_sha256,
        storage_uri=storage_uri,
        ingested_at=ingested_at,
        size_bytes=size_bytes,
    )

    doc = dr.create_or_get(
        session,
        source_version_id=version.id,
        extractor=extractor,
        extractor_version=extractor_version,
        structure_json_uri=storage_uri,
        plain_text_uri=storage_uri,
    )

    docling_chunk(doc.id, path, session=session)
    return doc.id
