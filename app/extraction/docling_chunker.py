"""Docling-based chunking for extraction pipeline: parse document, segment into chunks, persist to SQL."""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.chunk import Chunk as ChunkORM
from app.db.repositories.chunk_repo import ChunkPayload, ChunkRepo
from app.extraction.chunking import Chunk
from app.modules.docling.ids import chunk_hash, chunker_version_hash

logger = logging.getLogger(__name__)

# Type for docling_chunk source: path, raw bytes, or pre-parsed Docling document
DoclingInput = Union[str, Path, bytes, Any]

# Chunker settings for extraction (semantic chunks; section_path preserved in meta)
EXTRACTION_CHUNKER_SETTINGS = {
    "chunker": "hybrid",
    "target_tokens": 800,
    "max_tokens": 1000,
    "overlap_tokens": 80,
}


def _get_docling_document(source: DoclingInput, *, file_suffix: str = ".bin") -> Any:
    """Produce a DoclingDocument from path, bytes, or return source if already parsed."""
    if hasattr(source, "export_to_text") or (hasattr(source, "document") and source.document is not None):
        # Pre-parsed: DoclingDocument or conversion result with .document
        return getattr(source, "document", source)
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Document path not found: {path}")
        return _convert_path(path)
    if isinstance(source, bytes):
        return _convert_bytes(source, file_suffix=file_suffix)
    raise TypeError(f"Unsupported source type: {type(source)}")


def _convert_path(path: Path) -> Any:
    """Run Docling DocumentConverter on a file path. Returns DoclingDocument."""
    from docling.document_converter import DocumentConverter
    converter = DocumentConverter()
    result = converter.convert(path)
    if not result or not getattr(result, "document", None):
        raise ValueError("Conversion produced no document")
    return result.document


def _convert_bytes(raw_bytes: bytes, *, file_suffix: str = ".bin") -> Any:
    """Run Docling DocumentConverter on raw bytes (writes to temp file). Returns DoclingDocument."""
    from docling.document_converter import DocumentConverter
    suffix = file_suffix if file_suffix.startswith(".") else f".{file_suffix}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(raw_bytes)
    tmp.close()
    path = Path(tmp.name)
    try:
        converter = DocumentConverter()
        result = converter.convert(path)
        if not result or not getattr(result, "document", None):
            raise ValueError("Conversion produced no document")
        return result.document
    finally:
        path.unlink(missing_ok=True)


def _section_path_from_chunk(chunk: Any) -> str | None:
    """Derive section path string from Docling chunk meta (e.g. headings)."""
    meta = getattr(chunk, "meta", None)
    if not meta:
        return None
    if hasattr(meta, "section_path") and meta.section_path:
        return str(meta.section_path)
    if hasattr(meta, "heading_path") and meta.heading_path:
        return str(meta.heading_path)
    # Build from labels if present
    labels = getattr(meta, "labels", None) or getattr(meta, "doc_items", None)
    if labels and isinstance(labels, (list, tuple)):
        parts = [str(getattr(l, "label", l)) for l in labels[:5] if l]
        if parts:
            return "/".join(parts)
    return None


def _run_extraction_chunker(docling_doc: Any) -> list[tuple[str, int, int, dict, str | None]]:
    """Run chunker on DoclingDocument; return list of (text, page_start, page_end, meta_dict, section_path)."""
    from app.modules.docling.chunking import _run_chunker
    from types import SimpleNamespace
    settings = SimpleNamespace(**EXTRACTION_CHUNKER_SETTINGS)
    chunker, chunk_iter = _run_chunker(docling_doc, settings, None)
    if chunk_iter is None:
        return []
    chunker_version = chunker_version_hash(EXTRACTION_CHUNKER_SETTINGS)
    out = []
    for ch in chunk_iter:
        if chunker and hasattr(chunker, "contextualize"):
            text = chunker.contextualize(chunk=ch)
        else:
            text = getattr(ch, "text", None) or getattr(ch, "content", None) or str(ch)
        page_start = getattr(ch, "page_start", None) or getattr(ch, "page_no", None)
        page_end = getattr(ch, "page_end", None) or page_start
        section_path = _section_path_from_chunk(ch)
        meta = {"chunker_version": chunker_version, "source_artifact": "structure_json_uri"}
        out.append((text or "", page_start, page_end, meta, section_path))
    return out


def docling_chunk(
    doc_id: str,
    source: DoclingInput,
    *,
    session: Session,
    file_suffix: str = ".bin",
) -> list[Chunk]:
    """Parse document with Docling, segment into chunks, persist to SQL, return ordered Chunk list.

    source: File path (str/Path), raw bytes, or pre-parsed DoclingDocument.
    Chunk text is stored verbatim; section_path is stored in meta_json for evidence/substring use.
    """
    docling_doc = _get_docling_document(source, file_suffix=file_suffix)
    segments = _run_extraction_chunker(docling_doc)
    if not segments:
        # Fallback: single chunk from full text
        try:
            text = docling_doc.export_to_text() if hasattr(docling_doc, "export_to_text") else ""
        except Exception:
            text = ""
        if text:
            segments = [(text, None, None, {}, None)]

    chunker_version = chunker_version_hash(EXTRACTION_CHUNKER_SETTINGS)
    repo = ChunkRepo()
    payloads: list[ChunkPayload] = []
    for idx, (text, page_start, page_end, meta, section_path) in enumerate(segments):
        path = section_path
        if path is None and meta:
            path = meta.get("section_path")
        meta_with_path = {**(meta or {}), "section_path": path}
        meta_json_str = json.dumps(meta_with_path, ensure_ascii=False, separators=(",", ":"))
        text_safe = (text or "")[:65535] if text else ""
        h = chunk_hash(chunker_version, idx, text_safe)
        payloads.append(ChunkPayload(
            chunk_hash=h,
            chunk_index=idx,
            text=text_safe or None,
            text_uri=None,
            page_start=page_start,
            page_end=page_end,
            meta_json=meta_json_str,
        ))

    repo.bulk_upsert_chunks(
        session,
        doc_id,
        payloads,
        batch_rows=200,
        batch_max_bytes=5 * 1024 * 1024,
    )
    session.flush()

    orm_chunks = session.scalars(
        select(ChunkORM)
        .where(ChunkORM.document_id == doc_id)
        .order_by(ChunkORM.chunk_index)
    ).all()
    result = []
    for row in orm_chunks:
        section_path = None
        if row.meta_json:
            try:
                meta_obj = json.loads(row.meta_json)
                section_path = meta_obj.get("section_path") if isinstance(meta_obj, dict) else None
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(Chunk(
            chunk_id=row.id,
            doc_id=doc_id,
            chunk_index=row.chunk_index,
            section_path=section_path,
            text=(row.text or ""),
        ))
    return result
