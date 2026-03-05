"""Document helpers (e.g. workspace resolution) used by extraction and canonicalization."""
from __future__ import annotations

from app.db.models.document import Document


def workspace_id_for_document(doc_id: str, session) -> str:
    """Resolve workspace_id from document via source_version -> source."""
    doc = session.get(Document, doc_id)
    if not doc:
        raise ValueError(f"Document not found: {doc_id}")
    sv = getattr(doc, "source_version", None)
    if not sv:
        raise ValueError(f"Document {doc_id} has no source_version")
    source = getattr(sv, "source", None)
    if not source:
        raise ValueError(f"SourceVersion for {doc_id} has no source")
    wid = getattr(source, "workspace_id", None)
    if not wid:
        raise ValueError(f"Source for {doc_id} has no workspace_id")
    return wid
