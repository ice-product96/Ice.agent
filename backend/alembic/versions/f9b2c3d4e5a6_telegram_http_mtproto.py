"""telegram http proxy and mtproto dc settings

Revision ID: f9b2c3d4e5a6
Revises: e8a1b2c3d4f5
Create Date: 2026-07-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9b2c3d4e5a6"
down_revision: Union[str, None] = "e8a1b2c3d4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("telegram_accounts") as batch:
        batch.add_column(sa.Column("http_proxy", sa.String(length=1024), nullable=True))
        batch.add_column(sa.Column("mtproto_host", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("mtproto_port", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("mtproto_dc_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("telegram_accounts") as batch:
        batch.drop_column("mtproto_dc_id")
        batch.drop_column("mtproto_port")
        batch.drop_column("mtproto_host")
        batch.drop_column("http_proxy")
