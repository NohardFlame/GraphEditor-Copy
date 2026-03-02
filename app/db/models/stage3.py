"""Stage 3 resolved cards and baked objects ORM models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin


class Stage3ResolvedCard(Base, IdMixin):
    """One resolved card per (run_id, canonical_claim_id, kind)."""

    __tablename__ = "stage3_resolved_cards"

    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_claim_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resolved_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_refs: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "canonical_claim_id", "kind", name="uq_stage3_resolved_cards_run_claim_kind"),
    )


class Stage3ObjectBaked(Base, IdMixin):
    """Baked object with state pool; one row per (run_id, canonical_claim_id) for OBJECT."""

    __tablename__ = "stage3_objects_baked"

    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_claim_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    object_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "canonical_claim_id", name="uq_stage3_objects_baked_run_claim"),
    )
