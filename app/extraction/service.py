"""Orchestration for extraction pipeline: prepare chunk-marked doc parts for big-LLM."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.db.models.document import Document
from app.extraction.chunking import DocPart
from app.extraction.marked_doc_builder import (
    DEFAULT_MAX_BYTES,
    load_chunks_for_document,
    size_guard_from_chunks,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def prepare_marked_docs_for_extraction(
    doc_id: str,
    *,
    session: Session,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[DocPart]:
    """Load chunks from SQL, split by size limit, return list of DocPart for big-LLM.

    Does not run Docling; caller must ensure chunks already exist (e.g. after ingest).
    """
    chunks = load_chunks_for_document(doc_id, session)
    if not chunks:
        return []
    doc = session.get(Document, doc_id)
    title = getattr(doc, "title", None) or "" if doc else ""
    source = ""
    if doc:
        source = getattr(doc, "plain_text_uri", None) or getattr(doc, "structure_json_uri", None) or ""
    return size_guard_from_chunks(
        doc_id,
        chunks,
        max_bytes=max_bytes,
        title=title,
        source=source,
    )
