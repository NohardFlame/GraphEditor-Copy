"""Persistence and dedupe_key for big-extract results (Phase 3)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from app.canonical.norm import norm
from app.db.models.extraction import (
    Frame,
    Mention,
    MentionEvidence,
    Relation,
)
from app.extraction.big_extract_models import (
    FrameResult,
    MentionResult,
    RelationResult,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def dedupe_key_mention(mention: MentionResult) -> str:
    """Compute dedupe_key for a mention based on type and fields."""
    t = mention.type
    fields = mention.fields or {}
    if t == "ACTOR":
        name = fields.get("name") or fields.get("actor_name") or ""
        return f"actor::{norm(str(name))}"
    if t == "OBJECT":
        name = fields.get("name") or fields.get("object_name") or ""
        return f"object::{norm(str(name))}"
    if t == "ACTION":
        actor = norm(str(fields.get("actor_name") or fields.get("actor") or ""))
        verb = norm(str(fields.get("verb") or ""))
        obj = norm(str(fields.get("object_name") or fields.get("object") or ""))
        return f"action::{actor}::{verb}::{obj}"
    if t == "STATE":
        obj_hint = norm(str(fields.get("object_name") or fields.get("object") or ""))
        state = norm(str(fields.get("state_name") or fields.get("state") or ""))
        return f"state::{obj_hint}::{state}"
    return f"{t.lower()}::{norm(mention.mention_id)}"


def to_frame_entity(
    frame_result: FrameResult,
    doc_id: str,
    run_id: str,  # unused; Frame has no run_id; kept for API consistency
) -> Frame:
    """Map FrameResult to Frame ORM (id will be set by DB/default)."""
    return Frame(
        document_id=doc_id,
        chunk_id=frame_result.chunk_id,
        frame_index=frame_result.frame_index,
        frame_text=frame_result.frame_text,
        section_path=None,
    )


def to_mention_entity(
    mention_result: MentionResult,
    frame_entity: Frame,
    doc_id: str,
    run_id: str,
) -> Mention:
    """Map MentionResult to Mention ORM."""
    confidence: float | None = None
    if mention_result.confidence is not None:
        if isinstance(mention_result.confidence, (int, float)):
            confidence = float(mention_result.confidence)
        elif isinstance(mention_result.confidence, str):
            try:
                confidence = float(mention_result.confidence)
            except ValueError:
                pass
    return Mention(
        frame_id=frame_entity.id,
        document_id=doc_id,
        chunk_id=frame_entity.chunk_id,
        type=mention_result.type,
        fields_json=json.dumps(mention_result.fields or {}),
        epistemic=mention_result.epistemic,
        confidence=confidence,
        dedupe_key=dedupe_key_mention(mention_result),
        created_run_id=run_id,
    )


def to_mention_evidence_entity(mention_result: MentionResult) -> MentionEvidence:
    """Map MentionResult.evidence to MentionEvidence (mention_id set after mention is persisted)."""
    return MentionEvidence(
        mention_id="",  # caller must set after mention is flushed
        snippet_text=mention_result.evidence.snippet,
        char_start=mention_result.evidence.char_start,
        char_end=mention_result.evidence.char_end,
        validation_status="VERIFIED",
    )


def to_relation_entity(
    relation_result: RelationResult,
    frame_entity: Frame,
    run_id: str,
    src_mention_id: str,
    dst_mention_id: str,
) -> Relation:
    """Map RelationResult to Relation ORM (mention IDs must be resolved to persisted IDs)."""
    return Relation(
        frame_id=frame_entity.id,
        src_mention_id=src_mention_id,
        rel_type=relation_result.rel_type,
        dst_mention_id=dst_mention_id,
    )


def load_chunk_text_by_id(
    chunk_ids: set[str],
    session: Session,
) -> dict[str, str]:
    """Load chunks.text for given chunk_ids from DB."""
    from app.db.models.chunk import Chunk

    if not chunk_ids:
        return {}
    rows = session.scalars(
        select(Chunk).where(Chunk.id.in_(chunk_ids))
    ).all()
    return {r.id: (r.text or "") for r in rows}
