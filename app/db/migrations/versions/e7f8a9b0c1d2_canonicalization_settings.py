"""canonicalization_settings

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-03-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "canonicalization_settings",
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("dedup_strategy", sa.String(length=32), nullable=False, server_default="LLM_TOURNAMENT"),
        sa.Column("min_mentions_for_canonical", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cosine_high_merge_threshold", sa.Float(), nullable=False, server_default="0.9"),
        sa.Column("cosine_low_new_threshold", sa.Float(), nullable=False, server_default="0.2"),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_canonicalization_settings_workspace_id"),
    )
    op.create_index(
        "ix_canonicalization_settings_workspace_id",
        "canonicalization_settings",
        ["workspace_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_canonicalization_settings_workspace_id",
        table_name="canonicalization_settings",
    )
    op.drop_table("canonicalization_settings")
