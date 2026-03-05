"""create_extraction_phase1_tables

Revision ID: f7a1b2c3d4e5
Revises: e6f7a8b9c0d1
Create Date: 2026-03-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f7a1b2c3d4e5"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # extraction_runs: high-level runs for big-LLM extraction and canonicalization
    op.create_table(
        "extraction_runs",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("run_kind", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=True),
        sa.Column("model_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("stats_json", sa.Text(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "document_id",
            "run_kind",
            "input_hash",
            name="uq_extraction_runs_workspace_document_kind_input",
        ),
    )
    op.create_index(
        "ix_extraction_runs_workspace_id",
        "extraction_runs",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_extraction_runs_document_id",
        "extraction_runs",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        "ix_extraction_runs_run_kind",
        "extraction_runs",
        ["run_kind"],
        unique=False,
    )
    op.create_index(
        "ix_extraction_runs_status",
        "extraction_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_extraction_runs_input_hash",
        "extraction_runs",
        ["input_hash"],
        unique=False,
    )

    # frames: grouping unit per chunk
    op.create_table(
        "frames",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("frame_index", sa.Integer(), nullable=False),
        sa.Column("frame_text", sa.Text(), nullable=True),
        sa.Column("section_path", sa.String(length=512), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chunk_id",
            "frame_index",
            name="uq_frames_chunk_index",
        ),
    )
    op.create_index(
        "ix_frames_document_id",
        "frames",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        "ix_frames_chunk_id",
        "frames",
        ["chunk_id"],
        unique=False,
    )

    # mentions: typed entities/actions/states
    op.create_table(
        "mentions",
        sa.Column("frame_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("fields_json", sa.Text(), nullable=False),
        sa.Column("epistemic", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=256), nullable=True),
        sa.Column("created_run_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["frame_id"],
            ["frames.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_run_id"],
            ["extraction_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mentions_frame_id", "mentions", ["frame_id"], unique=False)
    op.create_index("ix_mentions_document_id", "mentions", ["document_id"], unique=False)
    op.create_index("ix_mentions_chunk_id", "mentions", ["chunk_id"], unique=False)
    op.create_index("ix_mentions_type", "mentions", ["type"], unique=False)
    op.create_index("ix_mentions_dedupe_key", "mentions", ["dedupe_key"], unique=False)
    op.create_index(
        "ix_mentions_created_run_id",
        "mentions",
        ["created_run_id"],
        unique=False,
    )

    # mention_evidence: evidence snippet per mention
    op.create_table(
        "mention_evidence",
        sa.Column("mention_id", sa.String(length=36), nullable=False),
        sa.Column("snippet_text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["mention_id"],
            ["mentions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mention_id",
            name="uq_mention_evidence_mention_id",
        ),
    )
    op.create_index(
        "ix_mention_evidence_mention_id",
        "mention_evidence",
        ["mention_id"],
        unique=False,
    )

    # relations: explicit links between mentions
    op.create_table(
        "relations",
        sa.Column("frame_id", sa.String(length=36), nullable=False),
        sa.Column("src_mention_id", sa.String(length=36), nullable=False),
        sa.Column("rel_type", sa.String(length=64), nullable=False),
        sa.Column("dst_mention_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["frame_id"],
            ["frames.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["src_mention_id"],
            ["mentions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dst_mention_id"],
            ["mentions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "src_mention_id",
            "rel_type",
            "dst_mention_id",
            name="uq_relations_src_type_dst",
        ),
    )
    op.create_index("ix_relations_frame_id", "relations", ["frame_id"], unique=False)
    op.create_index(
        "ix_relations_src_mention_id",
        "relations",
        ["src_mention_id"],
        unique=False,
    )
    op.create_index(
        "ix_relations_dst_mention_id",
        "relations",
        ["dst_mention_id"],
        unique=False,
    )

    # canonicals: registry entries
    op.create_table(
        "canonicals",
        sa.Column("canonical_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("norm_name", sa.String(length=512), nullable=False),
        sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
        sa.Column("created_run_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["canonicals.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_run_id"],
            ["extraction_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canonical_type",
            "norm_name",
            name="uq_canonicals_type_norm_name",
        ),
    )
    op.create_index(
        "ix_canonicals_canonical_type",
        "canonicals",
        ["canonical_type"],
        unique=False,
    )
    op.create_index(
        "ix_canonicals_norm_name",
        "canonicals",
        ["norm_name"],
        unique=False,
    )
    op.create_index(
        "ix_canonicals_created_run_id",
        "canonicals",
        ["created_run_id"],
        unique=False,
    )

    # object_states: finite states per object canonical
    op.create_table(
        "object_states",
        sa.Column("canonical_object_id", sa.String(length=36), nullable=False),
        sa.Column("state_name", sa.String(length=128), nullable=False),
        sa.Column("state_norm", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["canonical_object_id"],
            ["canonicals.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canonical_object_id",
            "state_norm",
            name="uq_object_states_object_state_norm",
        ),
    )
    op.create_index(
        "ix_object_states_canonical_object_id",
        "object_states",
        ["canonical_object_id"],
        unique=False,
    )

    # canonical_aliases: aliases per canonical
    op.create_table(
        "canonical_aliases",
        sa.Column("canonical_id", sa.String(length=36), nullable=False),
        sa.Column("alias_text", sa.String(length=512), nullable=False),
        sa.Column("alias_norm", sa.String(length=512), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["canonical_id"],
            ["canonicals.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canonical_id",
            "alias_norm",
            name="uq_canonical_aliases_canonical_alias_norm",
        ),
    )
    op.create_index(
        "ix_canonical_aliases_canonical_id",
        "canonical_aliases",
        ["canonical_id"],
        unique=False,
    )

    # mention_to_canonical: mapping + decision metadata
    op.create_table(
        "mention_to_canonical",
        sa.Column("mention_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("decided_by", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["mention_id"],
            ["mentions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_id"],
            ["canonicals.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mention_id",
            name="uq_mention_to_canonical_mention_id",
        ),
    )
    op.create_index(
        "ix_mention_to_canonical_mention_id",
        "mention_to_canonical",
        ["mention_id"],
        unique=False,
    )
    op.create_index(
        "ix_mention_to_canonical_canonical_id",
        "mention_to_canonical",
        ["canonical_id"],
        unique=False,
    )
    op.create_index(
        "ix_mention_to_canonical_run_id",
        "mention_to_canonical",
        ["run_id"],
        unique=False,
    )

    # canonical_merges: merges between canonicals
    op.create_table(
        "canonical_merges",
        sa.Column("from_canonical_id", sa.String(length=36), nullable=False),
        sa.Column("to_canonical_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(datetime('now'))"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["from_canonical_id"],
            ["canonicals.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_canonical_id"],
            ["canonicals.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_canonical_id",
            "to_canonical_id",
            name="uq_canonical_merges_from_to",
        ),
    )
    op.create_index(
        "ix_canonical_merges_from_canonical_id",
        "canonical_merges",
        ["from_canonical_id"],
        unique=False,
    )
    op.create_index(
        "ix_canonical_merges_to_canonical_id",
        "canonical_merges",
        ["to_canonical_id"],
        unique=False,
    )
    op.create_index(
        "ix_canonical_merges_run_id",
        "canonical_merges",
        ["run_id"],
        unique=False,
    )

    # llm_calls: link to extraction_runs for big-LLM and local tournament calls
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("extraction_run_id", sa.String(length=36), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_llm_calls_extraction_run_id",
            "extraction_runs",
            ["extraction_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_llm_calls_extraction_run_id",
            ["extraction_run_id"],
            unique=False,
        )


def downgrade() -> None:
    # llm_calls: drop extraction_run_id linkage
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_calls_extraction_run_id")
        batch_op.drop_constraint(
            "fk_llm_calls_extraction_run_id",
            type_="foreignkey",
        )
        batch_op.drop_column("extraction_run_id")

    # drop canonical-related tables and mappings
    op.drop_index("ix_canonical_merges_run_id", table_name="canonical_merges")
    op.drop_index(
        "ix_canonical_merges_to_canonical_id",
        table_name="canonical_merges",
    )
    op.drop_index(
        "ix_canonical_merges_from_canonical_id",
        table_name="canonical_merges",
    )
    op.drop_table("canonical_merges")

    op.drop_index(
        "ix_mention_to_canonical_run_id",
        table_name="mention_to_canonical",
    )
    op.drop_index(
        "ix_mention_to_canonical_canonical_id",
        table_name="mention_to_canonical",
    )
    op.drop_index(
        "ix_mention_to_canonical_mention_id",
        table_name="mention_to_canonical",
    )
    op.drop_table("mention_to_canonical")

    op.drop_index(
        "ix_canonical_aliases_canonical_id",
        table_name="canonical_aliases",
    )
    op.drop_table("canonical_aliases")

    op.drop_index(
        "ix_object_states_canonical_object_id",
        table_name="object_states",
    )
    op.drop_table("object_states")

    op.drop_index(
        "ix_canonicals_created_run_id",
        table_name="canonicals",
    )
    op.drop_index("ix_canonicals_norm_name", table_name="canonicals")
    op.drop_index("ix_canonicals_canonical_type", table_name="canonicals")
    op.drop_table("canonicals")

    op.drop_index(
        "ix_relations_dst_mention_id",
        table_name="relations",
    )
    op.drop_index(
        "ix_relations_src_mention_id",
        table_name="relations",
    )
    op.drop_index("ix_relations_frame_id", table_name="relations")
    op.drop_table("relations")

    op.drop_index(
        "ix_mention_evidence_mention_id",
        table_name="mention_evidence",
    )
    op.drop_table("mention_evidence")

    op.drop_index(
        "ix_mentions_created_run_id",
        table_name="mentions",
    )
    op.drop_index("ix_mentions_dedupe_key", table_name="mentions")
    op.drop_index("ix_mentions_type", table_name="mentions")
    op.drop_index("ix_mentions_chunk_id", table_name="mentions")
    op.drop_index("ix_mentions_document_id", table_name="mentions")
    op.drop_index("ix_mentions_frame_id", table_name="mentions")
    op.drop_table("mentions")

    op.drop_index("ix_frames_chunk_id", table_name="frames")
    op.drop_index("ix_frames_document_id", table_name="frames")
    op.drop_table("frames")

    op.drop_index(
        "ix_extraction_runs_input_hash",
        table_name="extraction_runs",
    )
    op.drop_index("ix_extraction_runs_status", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_run_kind", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_document_id", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_workspace_id", table_name="extraction_runs")
    op.drop_table("extraction_runs")

