"""PM state, decisions, and Cursor runs

Revision ID: e1f2a3b4c5d6
Revises: c9d0e1f2a3b4
Create Date: 2026-08-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_states",
        sa.Column("project_id", sa.String(120), primary_key=True),
        sa.Column("autonomy_level", sa.String(16), server_default="LEVEL_1", nullable=False),
        sa.Column("config", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_project_states_autonomy_level", "project_states", ["autonomy_level"])

    op.create_table(
        "decision_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_id", sa.String(120), nullable=False),
        sa.Column(
            "work_item_id",
            sa.Integer(),
            sa.ForeignKey("work_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decision_key", sa.String(64), nullable=False),
        sa.Column("topic", sa.String(300), server_default="", nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), server_default="", nullable=False),
        sa.Column("confirmed_by", sa.String(120), server_default="", nullable=False),
        sa.Column("source_message_id", sa.String(128), nullable=True),
        sa.Column("context_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.UniqueConstraint(
            "project_id", "decision_key", name="uq_decision_records_project_key"
        ),
    )
    op.create_index("ix_decision_records_created_at", "decision_records", ["created_at"])
    op.create_index("ix_decision_records_project_id", "decision_records", ["project_id"])
    op.create_index("ix_decision_records_work_item_id", "decision_records", ["work_item_id"])
    op.create_index(
        "ix_decision_records_source_message_id",
        "decision_records",
        ["source_message_id"],
    )
    op.create_index(
        "ix_decision_records_project_created",
        "decision_records",
        ["project_id", "created_at"],
    )

    op.create_table(
        "cursor_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "work_item_id",
            sa.Integer(),
            sa.ForeignKey("work_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(120), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("request_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_cursor_runs_idempotency_key"),
        sa.UniqueConstraint(
            "work_item_id", "attempt", name="uq_cursor_runs_work_item_attempt"
        ),
    )
    op.create_index("ix_cursor_runs_work_item_id", "cursor_runs", ["work_item_id"])
    op.create_index("ix_cursor_runs_project_id", "cursor_runs", ["project_id"])
    op.create_index("ix_cursor_runs_idempotency_key", "cursor_runs", ["idempotency_key"])
    op.create_index("ix_cursor_runs_status", "cursor_runs", ["status"])
    op.create_index(
        "ix_cursor_runs_project_status", "cursor_runs", ["project_id", "status"]
    )

    with op.batch_alter_table("work_items") as batch:
        batch.add_column(
            sa.Column("task_type", sa.String(64), server_default="task", nullable=False)
        )
        batch.add_column(
            sa.Column("context_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=False)
        )
        batch.add_column(
            sa.Column("requirements", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
        )
        batch.add_column(
            sa.Column(
                "acceptance_criteria",
                sa.JSON(),
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("constraints", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
        )
        batch.add_column(
            sa.Column("edge_cases", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
        )
        batch.add_column(
            sa.Column("priority", sa.String(16), server_default="normal", nullable=False)
        )
        batch.add_column(
            sa.Column(
                "pm_phase",
                sa.String(32),
                server_default="DISCUSSION",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("source_message_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("active_cursor_run_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_work_items_active_cursor_run_id",
            "cursor_runs",
            ["active_cursor_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_work_items_task_type", ["task_type"])
        batch.create_index("ix_work_items_priority", ["priority"])
        batch.create_index("ix_work_items_pm_phase", ["pm_phase"])
        batch.create_index("ix_work_items_source_message_id", ["source_message_id"])
        batch.create_index("ix_work_items_active_cursor_run_id", ["active_cursor_run_id"])
        batch.create_index("ix_work_items_project_pm_phase", ["project_id", "pm_phase"])
        batch.create_index("ix_work_items_project_priority", ["project_id", "priority"])
        batch.create_unique_constraint(
            "uq_work_items_source_message",
            ["agent_id", "source", "chat_id", "source_message_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("work_items") as batch:
        batch.drop_constraint("uq_work_items_source_message", type_="unique")
        batch.drop_index("ix_work_items_project_priority")
        batch.drop_index("ix_work_items_project_pm_phase")
        batch.drop_index("ix_work_items_active_cursor_run_id")
        batch.drop_index("ix_work_items_source_message_id")
        batch.drop_index("ix_work_items_pm_phase")
        batch.drop_index("ix_work_items_priority")
        batch.drop_index("ix_work_items_task_type")
        batch.drop_constraint("fk_work_items_active_cursor_run_id", type_="foreignkey")
        batch.drop_column("active_cursor_run_id")
        batch.drop_column("source_message_id")
        batch.drop_column("pm_phase")
        batch.drop_column("priority")
        batch.drop_column("edge_cases")
        batch.drop_column("constraints")
        batch.drop_column("acceptance_criteria")
        batch.drop_column("requirements")
        batch.drop_column("context_json")
        batch.drop_column("task_type")

    op.drop_table("cursor_runs")
    op.drop_index("ix_decision_records_source_message_id", table_name="decision_records")
    op.drop_table("decision_records")
    op.drop_table("project_states")
