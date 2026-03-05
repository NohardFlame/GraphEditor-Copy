"""Extraction runs, frames, mentions, relations, and canonical registry ORM models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Float,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.claim import LlmCall


class ExtractionRun(Base, IdMixin, TimestampMixin):
    """High-level extraction run for big-LLM preprocessing and canonicalization."""

    __tablename__ = "extraction_runs"

    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "document_id",
            "run_kind",
            "input_hash",
            name="uq_extraction_runs_workspace_document_kind_input",
        ),
    )

    if TYPE_CHECKING:
        llm_calls: Mapped[list["LlmCall"]]
    else:
        llm_calls: Mapped[list["LlmCall"]] = relationship(
            "LlmCall",
            back_populates="extraction_run",
            foreign_keys="LlmCall.extraction_run_id",
        )


class Frame(Base, IdMixin):
    """Smallest extraction unit (sentence/bullet) grouped per chunk."""

    __tablename__ = "frames"

    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    section_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        UniqueConstraint("chunk_id", "frame_index", name="uq_frames_chunk_index"),
    )

    mentions: Mapped[list["Mention"]] = relationship(
        "Mention",
        back_populates="frame",
        cascade="all, delete-orphan",
    )
    relations: Mapped[list["Relation"]] = relationship(
        "Relation",
        back_populates="frame",
        cascade="all, delete-orphan",
    )


class Mention(Base, IdMixin, TimestampMixin):
    """Typed mention (ACTOR / OBJECT / ACTION / STATE) within a frame."""

    __tablename__ = "mentions"

    frame_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("frames.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    epistemic: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    created_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    frame: Mapped["Frame"] = relationship("Frame", back_populates="mentions")
    evidence: Mapped["MentionEvidence | None"] = relationship(
        "MentionEvidence",
        back_populates="mention",
        cascade="all, delete-orphan",
        uselist=False,
    )
    canonical_link: Mapped["MentionToCanonical | None"] = relationship(
        "MentionToCanonical",
        back_populates="mention",
        cascade="all, delete-orphan",
        uselist=False,
    )
    relations_as_src: Mapped[list["Relation"]] = relationship(
        "Relation",
        back_populates="src_mention",
        foreign_keys="Relation.src_mention_id",
    )
    relations_as_dst: Mapped[list["Relation"]] = relationship(
        "Relation",
        back_populates="dst_mention",
        foreign_keys="Relation.dst_mention_id",
    )


class MentionEvidence(Base, IdMixin):
    """Verbatim evidence snippet for a mention."""

    __tablename__ = "mention_evidence"

    mention_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mentions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snippet_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PENDING",
    )

    __table_args__ = (
        UniqueConstraint("mention_id", name="uq_mention_evidence_mention_id"),
    )

    mention: Mapped["Mention"] = relationship("Mention", back_populates="evidence")


class Relation(Base, IdMixin):
    """Explicit relation between mentions inside a frame."""

    __tablename__ = "relations"

    frame_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("frames.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    src_mention_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mentions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rel_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dst_mention_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mentions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "src_mention_id",
            "rel_type",
            "dst_mention_id",
            name="uq_relations_src_type_dst",
        ),
    )

    frame: Mapped["Frame"] = relationship("Frame", back_populates="relations")
    src_mention: Mapped["Mention"] = relationship(
        "Mention",
        foreign_keys="Relation.src_mention_id",
        back_populates="relations_as_src",
    )
    dst_mention: Mapped["Mention"] = relationship(
        "Mention",
        foreign_keys="Relation.dst_mention_id",
        back_populates="relations_as_dst",
    )


class Canonical(Base, IdMixin, TimestampMixin):
    """Canonical registry entry (actor, object, action, or state)."""

    __tablename__ = "canonicals"

    canonical_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    norm_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("canonicals.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "canonical_type",
            "norm_name",
            name="uq_canonicals_type_norm_name",
        ),
    )

    aliases: Mapped[list["CanonicalAlias"]] = relationship(
        "CanonicalAlias",
        back_populates="canonical",
        cascade="all, delete-orphan",
    )
    object_states: Mapped[list["ObjectState"]] = relationship(
        "ObjectState",
        back_populates="object_canonical",
        cascade="all, delete-orphan",
    )
    outgoing_merges: Mapped[list["CanonicalMerge"]] = relationship(
        "CanonicalMerge",
        foreign_keys="CanonicalMerge.from_canonical_id",
        back_populates="from_canonical",
    )
    incoming_merges: Mapped[list["CanonicalMerge"]] = relationship(
        "CanonicalMerge",
        foreign_keys="CanonicalMerge.to_canonical_id",
        back_populates="to_canonical",
    )


class CanonicalAlias(Base, IdMixin):
    """Alias text for a canonical entity."""

    __tablename__ = "canonical_aliases"

    canonical_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canonicals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    alias_text: Mapped[str] = mapped_column(String(512), nullable=False)
    alias_norm: Mapped[str] = mapped_column(String(512), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "canonical_id",
            "alias_norm",
            name="uq_canonical_aliases_canonical_alias_norm",
        ),
    )

    canonical: Mapped["Canonical"] = relationship("Canonical", back_populates="aliases")


class ObjectState(Base, IdMixin):
    """Finite state within a canonical object."""

    __tablename__ = "object_states"

    canonical_object_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canonicals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state_name: Mapped[str] = mapped_column(String(128), nullable=False)
    state_norm: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "canonical_object_id",
            "state_norm",
            name="uq_object_states_object_state_norm",
        ),
    )

    object_canonical: Mapped["Canonical"] = relationship(
        "Canonical",
        back_populates="object_states",
    )


class MentionToCanonical(Base, IdMixin, TimestampMixin):
    """Mapping from mention to canonical entity, with decision metadata."""

    __tablename__ = "mention_to_canonical"

    mention_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mentions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canonicals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("mention_id", name="uq_mention_to_canonical_mention_id"),
    )

    mention: Mapped["Mention"] = relationship(
        "Mention",
        back_populates="canonical_link",
    )
    canonical: Mapped["Canonical"] = relationship("Canonical")
    run: Mapped["ExtractionRun | None"] = relationship("ExtractionRun")


class CanonicalMerge(Base, IdMixin, TimestampMixin):
    """Recorded merge between two canonical entities."""

    __tablename__ = "canonical_merges"

    from_canonical_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canonicals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_canonical_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("canonicals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "from_canonical_id",
            "to_canonical_id",
            name="uq_canonical_merges_from_to",
        ),
    )

    from_canonical: Mapped["Canonical"] = relationship(
        "Canonical",
        foreign_keys="CanonicalMerge.from_canonical_id",
        back_populates="outgoing_merges",
    )
    to_canonical: Mapped["Canonical"] = relationship(
        "Canonical",
        foreign_keys="CanonicalMerge.to_canonical_id",
        back_populates="incoming_merges",
    )
    run: Mapped["ExtractionRun | None"] = relationship("ExtractionRun")


class CanonicalizationSettings(Base, IdMixin, TimestampMixin):
    """Per-workspace (or global) settings for canonicalization strategy and thresholds."""

    __tablename__ = "canonicalization_settings"

    workspace_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        unique=True,
    )
    dedup_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="LLM_TOURNAMENT"
    )
    min_mentions_for_canonical: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    cosine_high_merge_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.9)
    cosine_low_new_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)

    __table_args__ = ()

