"""Parse and validate big-extract LLM responses (Phase 3)."""
from __future__ import annotations

import json
import re
from typing import Any

from app.extraction.big_extract_models import (
    BigExtractResult,
    EvidenceResult,
    FrameResult,
    MentionResult,
    ParseError,
    ReferenceResult,
    RelationResult,
    ValidationError,
)
from app.extraction.big_extract_prompts import (
    BIG_EXTRACT_ALLOWED_MENTION_TYPES,
    BIG_EXTRACT_ALLOWED_REL_TYPES,
    PROMPT_VERSION_BIG_EXTRACT,
)


def parse_big_extract_response(raw_text: str) -> BigExtractResult | ParseError:
    """Parse raw LLM response into BigExtractResult or return ParseError."""
    raw_stripped = raw_text.strip()
    # Try to extract JSON from markdown code block if present
    if "```" in raw_stripped:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_stripped)
        if m:
            raw_stripped = m.group(1).strip()
    try:
        data = json.loads(raw_stripped)
    except json.JSONDecodeError as e:
        return ParseError(
            message=str(e),
            raw_snippet=raw_text[:500] + ("..." if len(raw_text) > 500 else ""),
        )
    if not isinstance(data, dict):
        return ParseError(
            message="Top-level value is not an object",
            raw_snippet=raw_text[:500],
        )
    doc_id = data.get("doc_id")
    prompt_version = data.get("prompt_version")
    frames_data = data.get("frames")
    refs_data = data.get("references")
    errs_data = data.get("errors")
    if not isinstance(doc_id, str):
        return ParseError(message="Missing or invalid doc_id", raw_snippet=raw_text[:500])
    if not isinstance(prompt_version, str):
        return ParseError(
            message="Missing or invalid prompt_version",
            raw_snippet=raw_text[:500],
        )
    if not isinstance(frames_data, list):
        return ParseError(
            message="Missing or invalid frames (must be array)",
            raw_snippet=raw_text[:500],
        )
    frames: list[FrameResult] = []
    for i, f in enumerate(frames_data):
        if not isinstance(f, dict):
            continue
        frame = _parse_frame(f, i)
        if isinstance(frame, ParseError):
            return frame
        frames.append(frame)
    references: list[ReferenceResult] = []
    if isinstance(refs_data, list):
        for r in refs_data:
            if isinstance(r, dict):
                ref = _parse_reference(r)
                if ref is not None:
                    references.append(ref)
    errors_list: list[str] = []
    if isinstance(errs_data, list):
        for e in errs_data:
            if isinstance(e, str):
                errors_list.append(e)
    return BigExtractResult(
        doc_id=doc_id,
        prompt_version=prompt_version,
        frames=frames,
        references=references,
        errors=errors_list,
    )


def _parse_evidence(d: Any) -> EvidenceResult | ParseError:
    if not isinstance(d, dict):
        return ParseError(message="evidence must be object", raw_snippet="")
    snippet = d.get("snippet")
    if not isinstance(snippet, str):
        return ParseError(message="evidence.snippet required", raw_snippet="")
    char_start = d.get("char_start")
    char_end = d.get("char_end")
    if char_start is not None and not isinstance(char_start, int):
        char_start = None
    if char_end is not None and not isinstance(char_end, int):
        char_end = None
    return EvidenceResult(
        snippet=snippet,
        char_start=char_start,
        char_end=char_end,
    )


def _parse_mention(d: Any) -> MentionResult | ParseError:
    if not isinstance(d, dict):
        return ParseError(message="mention must be object", raw_snippet="")
    mention_id = d.get("mention_id")
    typ = d.get("type")
    fields = d.get("fields")
    epistemic = d.get("epistemic")
    evidence_data = d.get("evidence")
    confidence = d.get("confidence")
    if not isinstance(mention_id, str):
        mention_id = ""
    if not isinstance(typ, str):
        typ = ""
    if not isinstance(fields, dict):
        fields = {}
    if not isinstance(epistemic, str):
        epistemic = "EXPLICIT"
    if evidence_data is None:
        return ParseError(message="mention missing evidence", raw_snippet="")
    ev = _parse_evidence(evidence_data)
    if isinstance(ev, ParseError):
        return ev
    if confidence is not None and not isinstance(confidence, (str, int, float)):
        confidence = None
    return MentionResult(
        mention_id=mention_id,
        type=typ,
        fields=fields,
        epistemic=epistemic,
        evidence=ev,
        confidence=confidence,
    )


def _parse_relation(d: Any) -> RelationResult | ParseError:
    if not isinstance(d, dict):
        return ParseError(message="relation must be object", raw_snippet="")
    src = d.get("src_mention_id")
    rel_type = d.get("rel_type")
    dst = d.get("dst_mention_id")
    if not isinstance(src, str):
        src = ""
    if not isinstance(rel_type, str):
        rel_type = ""
    if not isinstance(dst, str):
        dst = ""
    return RelationResult(
        src_mention_id=src,
        rel_type=rel_type,
        dst_mention_id=dst,
    )


def _parse_frame(d: dict, index: int) -> FrameResult | ParseError:
    frame_id = d.get("frame_id") or ""
    chunk_id = d.get("chunk_id") or ""
    frame_index = d.get("frame_index", index)
    if not isinstance(frame_index, int):
        frame_index = index
    frame_text = d.get("frame_text")
    if not isinstance(frame_text, str) and frame_text is not None:
        frame_text = None
    mentions_data = d.get("mentions") or []
    relations_data = d.get("relations") or []
    if not isinstance(mentions_data, list):
        mentions_data = []
    if not isinstance(relations_data, list):
        relations_data = []
    mentions: list[MentionResult] = []
    for m in mentions_data:
        mention = _parse_mention(m)
        if isinstance(mention, ParseError):
            return mention
        mentions.append(mention)
    relations: list[RelationResult] = []
    for r in relations_data:
        rel = _parse_relation(r)
        if isinstance(rel, ParseError):
            return rel
        relations.append(rel)
    return FrameResult(
        frame_id=frame_id,
        chunk_id=chunk_id,
        frame_index=frame_index,
        frame_text=frame_text,
        mentions=mentions,
        relations=relations,
    )


def _parse_reference(d: dict) -> ReferenceResult | None:
    ref_id = d.get("ref_id")
    chunk_id = d.get("chunk_id")
    ref_text_raw = d.get("ref_text_raw")
    ref_norm = d.get("ref_norm")
    evidence_data = d.get("evidence")
    if not all(
        isinstance(x, str)
        for x in (ref_id, chunk_id, ref_text_raw, ref_norm)
    ):
        return None
    if evidence_data is None:
        return None
    ev = _parse_evidence(evidence_data)
    if isinstance(ev, ParseError):
        return None
    return ReferenceResult(
        ref_id=ref_id,
        chunk_id=chunk_id,
        ref_text_raw=ref_text_raw,
        ref_norm=ref_norm,
        evidence=ev,
    )


def validate_big_extract_result(
    result: BigExtractResult,
    doc_id: str,
    known_chunk_ids: set[str],
) -> list[ValidationError]:
    """Validate result against schema and business rules. Returns list of errors."""
    errors: list[ValidationError] = []
    if result.doc_id != doc_id:
        errors.append(
            ValidationError(
                message=f"doc_id mismatch: got {result.doc_id!r}, expected {doc_id!r}",
                path="doc_id",
            )
        )
    if result.prompt_version != PROMPT_VERSION_BIG_EXTRACT:
        errors.append(
            ValidationError(
                message=f"prompt_version must be {PROMPT_VERSION_BIG_EXTRACT!r}",
                path="prompt_version",
            )
        )
    seen_frame_keys: set[tuple[str, int]] = set()
    all_mention_ids: set[str] = set()
    for frame in result.frames:
        if not frame.frame_id:
            errors.append(ValidationError(message="frame_id required", path="frames"))
        if not frame.chunk_id:
            errors.append(ValidationError(message="chunk_id required in frame", path="frames"))
        elif frame.chunk_id not in known_chunk_ids:
            errors.append(
                ValidationError(
                    message=f"chunk_id {frame.chunk_id!r} not in document",
                    path="frames",
                )
            )
        key = (frame.chunk_id, frame.frame_index)
        if key in seen_frame_keys:
            errors.append(
                ValidationError(
                    message=f"Duplicate (chunk_id, frame_index): {frame.chunk_id}, {frame.frame_index}",
                    path="frames",
                )
            )
        seen_frame_keys.add(key)
        if frame.frame_index < 0:
            errors.append(
                ValidationError(
                    message="frame_index must be >= 0",
                    path="frames",
                )
            )
        for m in frame.mentions:
            if not m.mention_id:
                errors.append(
                    ValidationError(message="mention_id required", path="mentions")
                )
            all_mention_ids.add(m.mention_id)
            if m.type not in BIG_EXTRACT_ALLOWED_MENTION_TYPES:
                errors.append(
                    ValidationError(
                        message=f"mention type {m.type!r} not in {BIG_EXTRACT_ALLOWED_MENTION_TYPES}",
                        path="mentions",
                    )
                )
            if m.epistemic not in ("EXPLICIT", "INFERRED"):
                errors.append(
                    ValidationError(
                        message=f"epistemic must be EXPLICIT or INFERRED, got {m.epistemic!r}",
                        path="mentions",
                    )
                )
            if not (m.evidence and m.evidence.snippet):
                errors.append(
                    ValidationError(
                        message="evidence.snippet required",
                        path="mentions",
                    )
                )
        for rel in frame.relations:
            if rel.rel_type not in BIG_EXTRACT_ALLOWED_REL_TYPES:
                errors.append(
                    ValidationError(
                        message=f"rel_type {rel.rel_type!r} not in {BIG_EXTRACT_ALLOWED_REL_TYPES}",
                        path="relations",
                    )
                )
    for frame in result.frames:
        for rel in frame.relations:
            if rel.src_mention_id and rel.src_mention_id not in all_mention_ids:
                errors.append(
                    ValidationError(
                        message=f"src_mention_id {rel.src_mention_id!r} not found",
                        path="relations",
                    )
                )
            if rel.dst_mention_id and rel.dst_mention_id not in all_mention_ids:
                errors.append(
                    ValidationError(
                        message=f"dst_mention_id {rel.dst_mention_id!r} not found",
                        path="relations",
                    )
                )
    return errors


def validate_evidence_snippet(
    mention: MentionResult,
    frame: FrameResult,
    chunk_text_by_id: dict[str, str],
) -> ValidationError | None:
    """Check that mention.evidence.snippet is a verbatim substring of the chunk.
    Returns a ValidationError if not.
    """
    chunk_text = chunk_text_by_id.get(frame.chunk_id)
    if chunk_text is None:
        return ValidationError(
            message=f"Chunk {frame.chunk_id!r} not in cache",
            path=f"mentions.{mention.mention_id}.evidence",
        )
    snippet = mention.evidence.snippet
    if snippet not in chunk_text:
        return ValidationError(
            message=f"evidence.snippet is not a substring of chunk {frame.chunk_id!r}",
            path=f"mentions.{mention.mention_id}.evidence",
        )
    if mention.evidence.char_start is not None and mention.evidence.char_end is not None:
        start, end = mention.evidence.char_start, mention.evidence.char_end
        if start < 0 or end > len(chunk_text) or start >= end:
            return ValidationError(
                message=f"Invalid char_start/char_end for chunk {frame.chunk_id!r}",
                path=f"mentions.{mention.mention_id}.evidence",
            )
        if chunk_text[start:end] != snippet:
            return ValidationError(
                message=f"chunk_text[char_start:char_end] does not match snippet",
                path=f"mentions.{mention.mention_id}.evidence",
            )
    return None
