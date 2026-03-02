"""stage3_resolved_cards_and_objects_baked

Add Stage 3 tables: stage3_resolved_cards, stage3_objects_baked.

Revision ID: g8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-02-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "g8b9c0d1e2f3"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stage3_resolved_cards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_claim_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("resolved_json", sa.Text(), nullable=False),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=True),
        sa.Column("model_id", sa.String(length=128), nullable=True),
        sa.Column("embedding_model_id", sa.String(length=128), nullable=True),
        sa.Column("evidence_refs", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["canonical_claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("stage3_resolved_cards", schema=None) as batch_op:
        batch_op.create_index("ix_stage3_resolved_cards_document_id", ["document_id"], unique=False)
        batch_op.create_index("ix_stage3_resolved_cards_run_id", ["run_id"], unique=False)
        batch_op.create_index("ix_stage3_resolved_cards_canonical_claim_id", ["canonical_claim_id"], unique=False)
        batch_op.create_index("ix_stage3_resolved_cards_kind", ["kind"], unique=False)
        batch_op.create_index("ix_stage3_resolved_cards_parse_status", ["parse_status"], unique=False)
        batch_op.create_unique_constraint(
            "uq_stage3_resolved_cards_run_claim_kind",
            ["run_id", "canonical_claim_id", "kind"],
        )

    op.create_table(
        "stage3_objects_baked",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_claim_id", sa.String(length=36), nullable=False),
        sa.Column("object_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["canonical_claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("stage3_objects_baked", schema=None) as batch_op:
        batch_op.create_index("ix_stage3_objects_baked_document_id", ["document_id"], unique=False)
        batch_op.create_index("ix_stage3_objects_baked_run_id", ["run_id"], unique=False)
        batch_op.create_index("ix_stage3_objects_baked_canonical_claim_id", ["canonical_claim_id"], unique=False)
        batch_op.create_unique_constraint(
            "uq_stage3_objects_baked_run_claim",
            ["run_id", "canonical_claim_id"],
        )


def downgrade() -> None:
    op.drop_table("stage3_objects_baked")
    op.drop_table("stage3_resolved_cards")
