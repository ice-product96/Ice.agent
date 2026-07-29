"""add tavily api key for web search

Revision ID: a1b2c3d4e5f6
Revises: f9b2c3d4e5a6
Create Date: 2026-07-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f9b2c3d4e5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch:
        batch.add_column(sa.Column("tavily_api_key_ciphertext", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch:
        batch.drop_column("tavily_api_key_ciphertext")
