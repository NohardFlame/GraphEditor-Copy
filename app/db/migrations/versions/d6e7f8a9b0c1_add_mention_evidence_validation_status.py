"""Add validation_status to mention_evidence (Phase 6).

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-03-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("mention_evidence", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("validation_status", sa.String(length=32), nullable=False, server_default=sa.text("'PENDING'")),
        )


def downgrade() -> None:
    with op.batch_alter_table("mention_evidence", schema=None) as batch_op:
        batch_op.drop_column("validation_status")
