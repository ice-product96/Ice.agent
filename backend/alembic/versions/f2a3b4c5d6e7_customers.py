"""Customer cards for Cursor MCP project binding

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=120), primary_key=True),
        sa.Column("name", sa.String(length=200), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("project_id", sa.String(length=120), server_default="", nullable=False),
        sa.Column("cursor_workspace", sa.String(length=512), server_default="", nullable=False),
        sa.Column("cursor_window_id", sa.String(length=128), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_customers_agent_id", "customers", ["agent_id"])
    op.create_index("ix_customers_project_id", "customers", ["project_id"])
    op.create_index("ix_customers_is_default", "customers", ["is_default"])


def downgrade() -> None:
    op.drop_index("ix_customers_is_default", table_name="customers")
    op.drop_index("ix_customers_project_id", table_name="customers")
    op.drop_index("ix_customers_agent_id", table_name="customers")
    op.drop_table("customers")
