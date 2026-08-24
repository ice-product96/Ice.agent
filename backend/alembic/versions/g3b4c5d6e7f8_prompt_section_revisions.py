"""Prompt section revisions for restore

Revision ID: g3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g3b4c5d6e7f8"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompt_section_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.String(length=32), server_default="manager", nullable=False),
        sa.Column("note", sa.String(length=300), server_default="", nullable=False),
    )
    op.create_index(
        "ix_prompt_section_revisions_agent_id",
        "prompt_section_revisions",
        ["agent_id"],
    )
    op.create_index(
        "ix_prompt_section_revisions_key",
        "prompt_section_revisions",
        ["key"],
    )
    op.create_index(
        "ix_prompt_section_revisions_created_at",
        "prompt_section_revisions",
        ["created_at"],
    )
    op.create_index(
        "ix_prompt_section_revisions_agent_key_created",
        "prompt_section_revisions",
        ["agent_id", "key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_prompt_section_revisions_agent_key_created",
        table_name="prompt_section_revisions",
    )
    op.drop_index(
        "ix_prompt_section_revisions_created_at",
        table_name="prompt_section_revisions",
    )
    op.drop_index(
        "ix_prompt_section_revisions_key",
        table_name="prompt_section_revisions",
    )
    op.drop_index(
        "ix_prompt_section_revisions_agent_id",
        table_name="prompt_section_revisions",
    )
    op.drop_table("prompt_section_revisions")
