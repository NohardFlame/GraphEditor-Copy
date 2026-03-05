"""DTOs and error types for big-extract JSON (Phase 3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EvidenceResult:
    snippet: str
    char_start: int | None
    char_end: int | None


@dataclass
class MentionResult:
    mention_id: str
    type: str
    fields: dict[str, Any]
    epistemic: str
    evidence: EvidenceResult
    confidence: str | float


@dataclass
class RelationResult:
    src_mention_id: str
    rel_type: str
    dst_mention_id: str


@dataclass
class FrameResult:
    frame_id: str
    chunk_id: str
    frame_index: int
    frame_text: str | None
    mentions: list[MentionResult]
    relations: list[RelationResult]


@dataclass
class ReferenceResult:
    ref_id: str
    chunk_id: str
    ref_text_raw: str
    ref_norm: str
    evidence: EvidenceResult


@dataclass
class BigExtractResult:
    doc_id: str
    prompt_version: str
    frames: list[FrameResult]
    references: list[ReferenceResult]
    errors: list[str]


@dataclass
class ParseError:
    """JSON or structure parse failure."""

    message: str
    raw_snippet: str


@dataclass
class ValidationError:
    """Schema or business rule violation."""

    message: str
    path: str = ""
