"""llm http proxy and telegram socks5

Revision ID: e8a1b2c3d4f5
Revises: d4e7a91c2f30
Create Date: 2026-07-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8a1b2c3d4f5"
down_revision: Union[str, None] = "d4e7a91c2f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("llm_profiles") as batch:
        batch.add_column(sa.Column("http_proxy", sa.String(length=1024), nullable=True))
    with op.batch_alter_table("telegram_accounts") as batch:
        batch.add_column(sa.Column("socks5_host", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("socks5_port", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("socks5_username", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("socks5_password_ciphertext", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("telegram_accounts") as batch:
        batch.drop_column("socks5_password_ciphertext")
        batch.drop_column("socks5_username")
        batch.drop_column("socks5_port")
        batch.drop_column("socks5_host")
    with op.batch_alter_table("llm_profiles") as batch:
        batch.drop_column("http_proxy")
