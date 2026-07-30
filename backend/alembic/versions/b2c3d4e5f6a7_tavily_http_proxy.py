"""add tavily http proxy for web search

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch:
        batch.add_column(sa.Column("tavily_http_proxy", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch:
        batch.drop_column("tavily_http_proxy")
