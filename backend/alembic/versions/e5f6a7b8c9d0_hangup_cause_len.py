"""sip_calls hangup_cause length

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sip_calls") as batch:
        batch.alter_column(
            "hangup_cause",
            existing_type=sa.String(length=120),
            type_=sa.String(length=500),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("sip_calls") as batch:
        batch.alter_column(
            "hangup_cause",
            existing_type=sa.String(length=500),
            type_=sa.String(length=120),
            existing_nullable=True,
        )
