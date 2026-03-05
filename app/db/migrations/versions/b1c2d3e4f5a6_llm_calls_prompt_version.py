"""Add prompt_version to llm_calls for observability (Phase 6).

Revision ID: b1c2d3e4f5a6
Revises: f7a1b2c3d4e5
Create Date: 2026-03-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "f7a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("prompt_version", sa.String(length=128), nullable=True),
        )
        batch_op.create_index(
            "ix_llm_calls_model_prompt",
            ["model", "prompt_version"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_calls_model_prompt")
        batch_op.drop_column("prompt_version")
