"""employee agent tables

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-08-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("autonomy_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("heartbeat_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column("workday_start", sa.String(8), server_default="09:00", nullable=False),
        sa.Column("workday_end", sa.String(8), server_default="18:00", nullable=False),
        sa.Column("timezone", sa.String(64), server_default="UTC", nullable=False),
        sa.Column("budget_ticks_per_day", sa.Integer(), server_default="48", nullable=False),
        sa.Column("ticks_used_today", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ticks_day", sa.String(16), nullable=True),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role_title", sa.String(200), server_default="", nullable=False),
        sa.Column("mission", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_employee_profiles_agent_id", "employee_profiles", ["agent_id"])

    op.create_table(
        "prompt_sections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("agent_id", "key", name="uq_prompt_sections_agent_key"),
    )
    op.create_index("ix_prompt_sections_agent_id", "prompt_sections", ["agent_id"])

    op.create_table(
        "consultations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.Text(), server_default="", nullable=False),
        sa.Column("context", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(24), server_default="open", nullable=False),
        sa.Column("requires_approval", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("action_name", sa.String(120), nullable=True),
        sa.Column("telegram_message_ids", sa.JSON(), nullable=True),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.String(64), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_consultations_agent_id", "consultations", ["agent_id"])
    op.create_index("ix_consultations_status", "consultations", ["status"])

    op.create_table(
        "employee_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(300), server_default="", nullable=False),
        sa.Column("body", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(24), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_employee_plans_agent_id", "employee_plans", ["agent_id"])
    op.create_index("ix_employee_plans_horizon", "employee_plans", ["horizon"])
    op.create_index("ix_employee_plans_status", "employee_plans", ["status"])

    op.create_table(
        "employee_needs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), server_default="info", nullable=False),
        sa.Column("title", sa.String(300), server_default="", nullable=False),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="5", nullable=False),
        sa.Column("status", sa.String(24), server_default="open", nullable=False),
        sa.Column("consultation_id", sa.Integer(), sa.ForeignKey("consultations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_employee_needs_agent_id", "employee_needs", ["agent_id"])
    op.create_index("ix_employee_needs_status", "employee_needs", ["status"])


def downgrade() -> None:
    op.drop_table("employee_needs")
    op.drop_table("employee_plans")
    op.drop_table("consultations")
    op.drop_table("prompt_sections")
    op.drop_table("employee_profiles")
