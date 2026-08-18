"""work items and activity timeline

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300), server_default="", nullable=False),
        sa.Column("goal", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(32), server_default="open", nullable=False),
        sa.Column("next_action", sa.Text(), server_default="", nullable=False),
        sa.Column("wait_owner", sa.String(24), server_default="self", nullable=False),
        sa.Column("wait_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(32), server_default="telegram", nullable=False),
        sa.Column("chat_id", sa.String(64), nullable=True),
        sa.Column("reply_phone", sa.String(32), nullable=True),
        sa.Column("sender_id", sa.String(64), nullable=True),
        sa.Column("sender_username", sa.String(120), nullable=True),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("project_id", sa.String(120), nullable=True),
        sa.Column("customer_id", sa.String(120), nullable=True),
        sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consultation_id", sa.Integer(), sa.ForeignKey("consultations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cron_job_id", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_work_items_agent_id", "work_items", ["agent_id"])
    op.create_index("ix_work_items_status", "work_items", ["status"])
    op.create_index("ix_work_items_chat_id", "work_items", ["chat_id"])
    op.create_index("ix_work_items_agent_status", "work_items", ["agent_id", "status"])
    op.create_index("ix_work_items_agent_chat", "work_items", ["agent_id", "chat_id"])
    op.create_index("ix_work_items_consultation_id", "work_items", ["consultation_id"])
    op.create_index("ix_work_items_cron_job_id", "work_items", ["cron_job_id"])
    op.create_index("ix_work_items_project_id", "work_items", ["project_id"])
    op.create_index("ix_work_items_customer_id", "work_items", ["customer_id"])

    op.create_table(
        "work_item_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("work_item_id", sa.Integer(), sa.ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), server_default="note", nullable=False),
        sa.Column("title", sa.String(300), server_default="", nullable=False),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
    )
    op.create_index("ix_work_item_events_work_item_id", "work_item_events", ["work_item_id"])
    op.create_index("ix_work_item_events_created_at", "work_item_events", ["created_at"])
    op.create_index("ix_work_item_events_kind", "work_item_events", ["kind"])

    with op.batch_alter_table("consultations") as batch:
        batch.add_column(sa.Column("work_item_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_consultations_work_item_id",
            "work_items",
            ["work_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_consultations_work_item_id", ["work_item_id"])

    with op.batch_alter_table("message_logs") as batch:
        batch.add_column(sa.Column("work_item_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_message_logs_work_item_id",
            "work_items",
            ["work_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_message_logs_work_item_id", ["work_item_id"])


def downgrade() -> None:
    with op.batch_alter_table("message_logs") as batch:
        batch.drop_constraint("fk_message_logs_work_item_id", type_="foreignkey")
        batch.drop_index("ix_message_logs_work_item_id")
        batch.drop_column("work_item_id")
    with op.batch_alter_table("consultations") as batch:
        batch.drop_constraint("fk_consultations_work_item_id", type_="foreignkey")
        batch.drop_index("ix_consultations_work_item_id")
        batch.drop_column("work_item_id")
    op.drop_table("work_item_events")
    op.drop_table("work_items")
