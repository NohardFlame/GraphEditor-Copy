"""Claim ledger and LLM call audit ORM models (Stage 1)."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin

if TYPE_CHECKING:
    from app.db.models.extraction import ExtractionRun
    from app.db.models.pipeline_run import PipelineRun


class LlmCall(Base, IdMixin):
    """Audit record for one LLM request/response."""

    __tablename__ = "llm_calls"

    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    extraction_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("extraction_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    chunk_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("chunks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    signature_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pipeline_run: Mapped["PipelineRun | None"] = relationship(  # noqa: F821
        "PipelineRun", back_populates="llm_calls", foreign_keys=[run_id]
    )
    extraction_run: Mapped["ExtractionRun | None"] = relationship(
        "ExtractionRun",
        back_populates="llm_calls",
        foreign_keys=[extraction_run_id],
    )


class Claim(Base, IdMixin):
    """One atomic claim from Stage 1 extraction."""

    __tablename__ = "claims"

    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_extraction_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("chunk_extractions.id", ondelete="SET NULL"),
        nullable=True,
    )
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    predicate: Mapped[str | None] = mapped_column(String(256), nullable=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    epistemic_tag: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNREVIEWED")
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("claims.id", ondelete="SET NULL"),
        nullable=True,
    )
    embedding_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pipeline_run: Mapped["PipelineRun"] = relationship(  # noqa: F821
        "PipelineRun", back_populates="claims"
    )
    evidence: Mapped[list["ClaimEvidence"]] = relationship(
        "ClaimEvidence",
        back_populates="claim",
        cascade="all, delete-orphan",
    )


class ClaimEvidence(Base, IdMixin):
    """Evidence snippet for a claim."""

    __tablename__ = "claim_evidence"

    claim_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    snippet_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    claim: Mapped["Claim"] = relationship("Claim", back_populates="evidence")