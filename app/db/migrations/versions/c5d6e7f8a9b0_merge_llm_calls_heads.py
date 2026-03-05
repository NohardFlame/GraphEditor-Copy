"""Merge llm_calls branch heads (run_id nullable + prompt_version).

Revision ID: c5d6e7f8a9b0
Revises: a8b9c0d1e2f3, b1c2d3e4f5a6
Create Date: 2026-03-03

"""
from typing import Sequence, Union

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, tuple[str, ...], None] = ("a8b9c0d1e2f3", "b1c2d3e4f5a6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
