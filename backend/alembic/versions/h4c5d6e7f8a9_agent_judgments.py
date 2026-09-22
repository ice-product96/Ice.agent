"""judgment layer: agent_judgments, decision trace ids, judge settings

Revision ID: h4c5d6e7f8a9
Revises: g3b4c5d6e7f8
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h4c5d6e7f8a9"
down_revision: Union[str, None] = "g3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_judgments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("work_item_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=120), nullable=True),
        sa.Column("decision_trace_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("verdict", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("verdict_json", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("enforced", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cached", sa.Boolean(), nullable=False),
        sa.Column("legacy_json", sa.JSON(), nullable=True),
        sa.Column("agreed", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("overridden_by", sa.String(length=64), nullable=True),
        sa.Column("override_verdict", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("created_at", "agent_id", "work_item_id", "chat_id", "project_id", "decision_trace_id", "kind"):
        op.create_index(f"ix_agent_judgments_{column}", "agent_judgments", [column])
    op.create_index("ix_agent_judgments_kind_digest", "agent_judgments", ["kind", "input_digest"])
    op.create_index(
        "ix_agent_judgments_work_item_created", "agent_judgments", ["work_item_id", "created_at"]
    )
    for table in ("message_logs", "work_item_events", "cursor_runs"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("decision_trace_id", sa.String(length=64), nullable=True))
        op.create_index(f"ix_{table}_decision_trace_id", table, ["decision_trace_id"])
    with op.batch_alter_table("runtime_settings") as batch:
        batch.add_column(sa.Column("judge_profile_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("judge_model", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("judge_premium_model", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("judge_thresholds", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("judge_modes", sa.JSON(), nullable=True))
        batch.create_foreign_key(
            "fk_runtime_settings_judge_profile_id",
            "llm_profiles",
            ["judge_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch:
        batch.drop_constraint("fk_runtime_settings_judge_profile_id", type_="foreignkey")
        batch.drop_column("judge_modes")
        batch.drop_column("judge_thresholds")
        batch.drop_column("judge_premium_model")
        batch.drop_column("judge_model")
        batch.drop_column("judge_profile_id")
    for table in ("message_logs", "work_item_events", "cursor_runs"):
        op.drop_index(f"ix_{table}_decision_trace_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("decision_trace_id")
    op.drop_index("ix_agent_judgments_work_item_created", table_name="agent_judgments")
    op.drop_index("ix_agent_judgments_kind_digest", table_name="agent_judgments")
    for column in ("created_at", "agent_id", "work_item_id", "chat_id", "project_id", "decision_trace_id", "kind"):
        op.drop_index(f"ix_agent_judgments_{column}", table_name="agent_judgments")
    op.drop_table("agent_judgments")
