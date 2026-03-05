"""Build chunk-marked document string from SQL chunks (plan.md §4.1)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models.chunk import Chunk as ChunkORM
from app.db.models.document import Document
from app.extraction.chunking import Chunk, DocPart

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# Marker format: opening line per chunk, verbatim text, closing line (plan.md §4.1)
CHUNK_OPEN_PREFIX = "<<<CHUNK "
CHUNK_OPEN_SUFFIX = ">>>"
CHUNK_CLOSE = "<<<END_CHUNK>>>"

# Default max UTF-8 bytes per part for big-LLM input (plan.md)
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_OVERLAP_CHUNKS = 2


def _escape_section_path(section_path: str | None) -> str:
    """Escape section_path for use inside double-quoted attribute (no internal ")."""
    if not section_path:
        return ""
    return section_path.replace('"', '\\"')


def _build_chunk_marker(chunk_id: str, chunk_index: int, section_path: str | None) -> str:
    """Single opening marker line: <<<CHUNK id="..." index=... section_path="...">>>."""
    path_attr = _escape_section_path(section_path)
    return f'{CHUNK_OPEN_PREFIX}id="{chunk_id}" index={chunk_index} section_path="{path_attr}"{CHUNK_OPEN_SUFFIX}'


def load_chunks_for_document(doc_id: str, session: Session) -> list[Chunk]:
    """Load ordered chunks for doc_id from SQL into list of Chunk (for size_guard)."""
    orm_chunks = session.scalars(
        select(ChunkORM)
        .where(ChunkORM.document_id == doc_id)
        .order_by(ChunkORM.chunk_index)
    ).all()
    out: list[Chunk] = []
    for row in orm_chunks:
        section_path = None
        if row.meta_json:
            try:
                meta = json.loads(row.meta_json)
                if isinstance(meta, dict):
                    section_path = meta.get("section_path")
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(Chunk(
            chunk_id=row.id,
            doc_id=doc_id,
            chunk_index=row.chunk_index,
            section_path=section_path,
            text=(row.text or ""),
        ))
    return out


def build_marked_doc(doc_id: str, *, session: Session) -> str:
    """Build a single chunk-marked document string for doc_id from SQL.

    Format (plan.md §4.1):
      # DOC_META
      doc_id: <UUID>
      title: <optional>
      source: <optional>

      # CHUNKS (ordered)
      <<<CHUNK id="<chunk_id>" index=<chunk_index> section_path="<path>">>>
      <verbatim chunk text>
      <<<END_CHUNK>>>
      ...
    Chunk text is emitted exactly as stored; no whitespace normalization.
    """
    doc = session.get(Document, doc_id)
    title = ""
    source = ""
    if doc:
        source = getattr(doc, "plain_text_uri", None) or getattr(doc, "structure_json_uri", None) or ""
        title = getattr(doc, "title", None) or ""

    orm_chunks = session.scalars(
        select(ChunkORM)
        .where(ChunkORM.document_id == doc_id)
        .order_by(ChunkORM.chunk_index)
    ).all()

    parts = [
        "# DOC_META",
        f"doc_id: {doc_id}",
        f"title: {title}",
        f"source: {source}",
        "",
        "# CHUNKS (ordered)",
    ]
    for row in orm_chunks:
        section_path = None
        if row.meta_json:
            try:
                meta = json.loads(row.meta_json)
                if isinstance(meta, dict):
                    section_path = meta.get("section_path")
            except (json.JSONDecodeError, TypeError):
                pass
        parts.append(_build_chunk_marker(row.id, row.chunk_index, section_path))
        parts.append(row.text or "")
        parts.append(CHUNK_CLOSE)
    return "\n".join(parts)


def build_marked_doc_for_chunks(
    doc_id: str,
    title: str,
    source: str,
    chunks: list,
) -> str:
    """Build marked document from an in-memory list of chunk-like objects.

    Each chunk must have: chunk_id (or id), chunk_index (or chunk_index), section_path, text.
    Used by size_guard to build a part from a range of Chunk objects.
    """
    parts = [
        "# DOC_META",
        f"doc_id: {doc_id}",
        f"title: {title}",
        f"source: {source}",
        "",
        "# CHUNKS (ordered)",
    ]
    for c in chunks:
        cid = getattr(c, "chunk_id", None) or getattr(c, "id", None)
        idx = getattr(c, "chunk_index", None)
        path = getattr(c, "section_path", None)
        text = getattr(c, "text", None) or ""
        if cid is None or idx is None:
            continue
        parts.append(_build_chunk_marker(str(cid), idx, path))
        parts.append(text)
        parts.append(CHUNK_CLOSE)
    return "\n".join(parts)


def _estimate_part_bytes(doc_id: str, title: str, source: str, chunks: list[Chunk]) -> int:
    """Estimate UTF-8 byte size of the marked doc for the given chunk list."""
    header = "\n".join([
        "# DOC_META",
        f"doc_id: {doc_id}",
        f"title: {title}",
        f"source: {source}",
        "",
        "# CHUNKS (ordered)",
    ])
    total = len(header.encode("utf-8"))
    for c in chunks:
        line = _build_chunk_marker(c.chunk_id, c.chunk_index, c.section_path)
        total += len((line + "\n").encode("utf-8"))
        total += len((c.text + "\n").encode("utf-8"))
        total += len((CHUNK_CLOSE + "\n").encode("utf-8"))
    return total


def size_guard_from_chunks(
    doc_id: str,
    chunks: list[Chunk],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    overlap_chunks: int = DEFAULT_OVERLAP_CHUNKS,
    title: str = "",
    source: str = "",
) -> list[DocPart]:
    """Split chunks into one or more DocParts such that each part's marked-doc size <= max_bytes.

    Uses chunk boundaries only; adjacent parts overlap by overlap_chunks (e.g. 2).
    Deterministic: same inputs produce same part indices and ranges.
    """
    if not chunks:
        return []
    if _estimate_part_bytes(doc_id, title, source, chunks) <= max_bytes:
        text = build_marked_doc_for_chunks(doc_id, title, source, chunks)
        return [
            DocPart(
                part_id=f"doc::{doc_id}::part::0",
                doc_id=doc_id,
                part_index=0,
                text=text,
                first_chunk_index=chunks[0].chunk_index,
                last_chunk_index=chunks[-1].chunk_index,
            )
        ]
    parts_out: list[DocPart] = []
    start = 0
    part_index = 0
    while start < len(chunks):
        end = start
        while end < len(chunks):
            span = chunks[start : end + 1]
            if _estimate_part_bytes(doc_id, title, source, span) > max_bytes:
                break
            end += 1
        if end == start:
            end = start + 1
        span = chunks[start : end + 1]
        if not span:
            break
        text = build_marked_doc_for_chunks(doc_id, title, source, span)
        parts_out.append(
            DocPart(
                part_id=f"doc::{doc_id}::part::{part_index}",
                doc_id=doc_id,
                part_index=part_index,
                text=text,
                first_chunk_index=span[0].chunk_index,
                last_chunk_index=span[-1].chunk_index,
            )
        )
        next_start = end - overlap_chunks + 1
        if next_start <= start:
            next_start = end + 1
        start = next_start
        part_index += 1
    return parts_out
