"""Debug views for pipeline inspection (Phase 6): doc_debug, chunk_view, mention_view, canonical_view."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models.chunk import Chunk
from app.db.models.document import Document
from app.db.models.extraction import (
    Canonical,
    CanonicalAlias,
    ExtractionRun,
    Frame,
    Mention,
    MentionEvidence,
    MentionToCanonical,
    ObjectState,
    Relation,
)
from app.db.models.source import SourceVersion


def doc_debug(doc_id: str, session: Session) -> dict[str, Any]:
    """Pipeline status for a document: metadata and all extraction_runs with stats."""
    doc = session.get(Document, doc_id)
    if not doc:
        return {"error": "document_not_found", "doc_id": doc_id}

    # Document metadata (title/source from source_version if available)
    source_version = session.get(SourceVersion, doc.source_version_id)
    source_meta: dict[str, Any] = {}
    if source_version:
        source_meta["source_version_id"] = source_version.id
        if hasattr(source_version, "storage_uri"):
            source_meta["storage_uri"] = getattr(source_version, "storage_uri", None)

    runs_stmt = (
        select(ExtractionRun)
        .where(ExtractionRun.document_id == doc_id)
        .order_by(ExtractionRun.created_at.desc())
    )
    runs = list(session.execute(runs_stmt).scalars().all())

    runs_summary = []
    for r in runs:
        stats = {}
        if r.stats_json:
            try:
                stats = json.loads(r.stats_json)
            except json.JSONDecodeError:
                stats = {"raw": r.stats_json[:500]}
        runs_summary.append({
            "run_id": r.id,
            "run_kind": r.run_kind,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "prompt_version": r.prompt_version,
            "model_id": r.model_id,
            "stats": stats,
        })

    return {
        "doc_id": doc_id,
        "document_meta": {
            "source_version_id": doc.source_version_id,
            "extractor": doc.extractor,
            "extractor_version": doc.extractor_version,
            **source_meta,
        },
        "runs": runs_summary,
    }


def chunk_view(doc_id: str, chunk_index: int, session: Session) -> dict[str, Any]:
    """Chunk text, metadata, and associated frames/mentions with canonical mappings."""
    chunk = (
        session.execute(
            select(Chunk)
            .where(Chunk.document_id == doc_id, Chunk.chunk_index == chunk_index)
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if not chunk:
        return {"error": "chunk_not_found", "doc_id": doc_id, "chunk_index": chunk_index}

    section_path = None
    if chunk.meta_json:
        try:
            meta = json.loads(chunk.meta_json)
            section_path = meta.get("section_path")
        except json.JSONDecodeError:
            pass

    frames_stmt = (
        select(Frame)
        .where(Frame.chunk_id == chunk.id)
        .options(
            joinedload(Frame.mentions).joinedload(Mention.evidence),
            joinedload(Frame.mentions).joinedload(Mention.canonical_link),
            joinedload(Frame.relations),
        )
        .order_by(Frame.frame_index)
    )
    frames = list(session.execute(frames_stmt).scalars().unique().all())

    frames_data = []
    for f in frames:
        mentions_data = []
        for m in f.mentions:
            ev = m.evidence
            mention_entry: dict[str, Any] = {
                "mention_id": m.id,
                "type": m.type,
                "fields": json.loads(m.fields_json) if m.fields_json else {},
                "evidence_snippet": ev.snippet_text[:200] if ev and ev.snippet_text else None,
                "evidence_validation": ev.validation_status if ev else None,
            }
            if m.canonical_link:
                mention_entry["canonical"] = {
                    "canonical_id": m.canonical_link.canonical_id,
                    "decision": m.canonical_link.decision,
                    "decided_by": m.canonical_link.decided_by,
                    "run_id": m.canonical_link.run_id,
                }
            mentions_data.append(mention_entry)
        frames_data.append({
            "frame_id": f.id,
            "frame_index": f.frame_index,
            "frame_text": (f.frame_text or "")[:500],
            "section_path": f.section_path,
            "mentions": mentions_data,
            "relations_count": len(f.relations),
        })

    return {
        "doc_id": doc_id,
        "chunk_id": chunk.id,
        "chunk_index": chunk.chunk_index,
        "section_path": section_path,
        "text_preview": (chunk.text or "")[:2000],
        "frames": frames_data,
    }


def mention_view(mention_id: str, session: Session) -> dict[str, Any]:
    """Mention details, frame/chunk context, and canonical mapping (with optional llm_calls link)."""
    mention = (
        session.execute(
            select(Mention)
            .where(Mention.id == mention_id)
            .options(
                joinedload(Mention.evidence),
                joinedload(Mention.frame),
                joinedload(Mention.canonical_link),
            )
        )
        .scalar_one_or_none()
    )
    if not mention:
        return {"error": "mention_not_found", "mention_id": mention_id}

    frame = mention.frame
    chunk = session.get(Chunk, mention.chunk_id) if mention.chunk_id else None
    canonical_link = mention.canonical_link

    out: dict[str, Any] = {
        "mention_id": mention.id,
        "type": mention.type,
        "fields": json.loads(mention.fields_json) if mention.fields_json else {},
        "epistemic": mention.epistemic,
        "confidence": mention.confidence,
        "evidence": None,
        "frame_context": None,
        "chunk_snippet": None,
        "canonical_mapping": None,
    }

    if mention.evidence:
        out["evidence"] = {
            "snippet": mention.evidence.snippet_text,
            "char_start": mention.evidence.char_start,
            "char_end": mention.evidence.char_end,
            "validation_status": mention.evidence.validation_status,
        }

    if frame:
        out["frame_context"] = {
            "frame_id": frame.id,
            "frame_index": frame.frame_index,
            "frame_text": (frame.frame_text or "")[:500],
        }

    if chunk:
        out["chunk_snippet"] = (chunk.text or "")[:500]

    if canonical_link:
        canonical = session.get(Canonical, canonical_link.canonical_id)
        aliases: list[str] = []
        if canonical:
            alias_rows = session.execute(
                select(CanonicalAlias).where(CanonicalAlias.canonical_id == canonical.id)
            ).scalars().all()
            aliases = [a.alias_text for a in alias_rows]
        out["canonical_mapping"] = {
            "canonical_id": canonical_link.canonical_id,
            "canonical_name": canonical.name if canonical else None,
            "norm_name": canonical.norm_name if canonical else None,
            "aliases": aliases,
            "decision": canonical_link.decision,
            "decided_by": canonical_link.decided_by,
            "run_id": canonical_link.run_id,
        }

    return out


def canonical_view(canonical_id: str, session: Session) -> dict[str, Any]:
    """Canonical details, aliases, object_states (if OBJECT), and all mentions mapped to it."""
    canonical = (
        session.execute(
            select(Canonical)
            .where(Canonical.id == canonical_id)
            .options(
                joinedload(Canonical.aliases),
                joinedload(Canonical.object_states),
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if not canonical:
        return {"error": "canonical_not_found", "canonical_id": canonical_id}

    aliases = [{"alias_text": a.alias_text, "alias_norm": a.alias_norm} for a in canonical.aliases]
    object_states = []
    if canonical.canonical_type == "OBJECT":
        object_states = [
            {"state_name": s.state_name, "state_norm": s.state_norm}
            for s in canonical.object_states
        ]

    mtc_stmt = (
        select(MentionToCanonical, Mention)
        .join(Mention, MentionToCanonical.mention_id == Mention.id)
        .where(MentionToCanonical.canonical_id == canonical_id)
        .options(joinedload(Mention.frame))
    )
    rows = list(session.execute(mtc_stmt).unique().all())

    mentions_mapped = []
    for row in rows:
        mtc, m = row[0], row[1]
        frame = m.frame
        mentions_mapped.append({
            "mention_id": m.id,
            "mention_type": m.type,
            "decision": mtc.decision,
            "decided_by": mtc.decided_by,
            "run_id": mtc.run_id,
            "doc_id": m.document_id,
            "chunk_id": m.chunk_id,
            "frame_index": frame.frame_index if frame else None,
        })

    return {
        "canonical_id": canonical.id,
        "canonical_type": canonical.canonical_type,
        "name": canonical.name,
        "norm_name": canonical.norm_name,
        "superseded_by_id": canonical.superseded_by_id,
        "created_run_id": canonical.created_run_id,
        "aliases": aliases,
        "object_states": object_states,
        "mentions_mapped": mentions_mapped,
    }
