"""In-memory Chunk and DocPart structures for extraction pipeline.

Invariants (documented for downstream evidence-substring checks):
- For a given doc_id, chunks are in strictly increasing chunk_index order.
- Chunk text is verbatim: no post-processing; must match SQL chunks.text exactly.
- chunk_id in markers must match the SQL primary key of the corresponding chunks row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Chunk:
    """Single chunk used across ingestion, storage, and marked-doc building.

    Invariants:
    - chunk_id matches the SQL primary key in chunks table.
    - For a given doc_id, chunks are ordered by strictly increasing chunk_index.
    - text is verbatim (same as stored in SQL); do not modify for evidence checks.
    """

    chunk_id: str
    doc_id: str
    chunk_index: int
    section_path: str | None
    text: str

    @classmethod
    def from_docling(
        cls,
        node: Any,
        doc_id: str,
        chunk_id: str,
        index: int,
        *,
        section_path: str | None = None,
    ) -> Chunk:
        """Build a Chunk from a Docling node/segment.

        node: Docling chunk or segment (must have .text or .content or be stringifiable).
        doc_id: Parent document ID.
        chunk_id: Stable ID (e.g. from SQL after insert, or uuid5(doc_id, f"chunk-{index}")).
        index: Sequential chunk_index (0-based).
        section_path: Optional hierarchy path like "1/1.2/3"; derived from node if not given.
        """
        text = _text_from_docling_node(node)
        path = section_path
        if path is None and hasattr(node, "meta") and node.meta:
            path = getattr(node.meta, "section_path", None) or getattr(
                node.meta, "heading_path", None
            )
        if path is None and hasattr(node, "section_path"):
            path = getattr(node, "section_path", None)
        return cls(
            chunk_id=chunk_id,
            doc_id=doc_id,
            chunk_index=index,
            section_path=path,
            text=text or "",
        )


def _text_from_docling_node(node: Any) -> str:
    """Extract verbatim text from a Docling chunk/node."""
    if hasattr(node, "text") and node.text is not None:
        return str(node.text)
    if hasattr(node, "content") and node.content is not None:
        return str(node.content)
    return str(node) if node else ""


@dataclass(frozen=True)
class DocPart:
    """One part of a size-guarded marked document (when full doc exceeds max_bytes).

    part_id: Stable id, e.g. "doc::<doc_id>::part::<part_index>".
    first_chunk_index / last_chunk_index: Inclusive range of chunk indices in this part.
    """

    part_id: str
    doc_id: str
    part_index: int
    text: str
    first_chunk_index: int
    last_chunk_index: int
