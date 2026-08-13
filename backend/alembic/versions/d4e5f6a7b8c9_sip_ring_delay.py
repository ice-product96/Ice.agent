"""sip inbound ring delay

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sip_accounts") as batch:
        batch.add_column(sa.Column("ring_delay_seconds", sa.Float(), nullable=False, server_default="4"))


def downgrade() -> None:
    with op.batch_alter_table("sip_accounts") as batch:
        batch.drop_column("ring_delay_seconds")
