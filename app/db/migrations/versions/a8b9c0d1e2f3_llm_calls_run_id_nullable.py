"""Make llm_calls.run_id nullable for extraction-run-only LLM calls.

Revision ID: a8b9c0d1e2f3
Revises: f7a1b2c3d4e5
Create Date: 2026-03-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "f7a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.alter_column(
            "run_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.alter_column(
            "run_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
