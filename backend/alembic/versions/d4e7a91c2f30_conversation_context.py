"""conversation context and temporal memory

Revision ID: d4e7a91c2f30
Revises: 8c1f4a2d7b90
Create Date: 2026-07-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e7a91c2f30"
down_revision: Union[str, None] = "8c1f4a2d7b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_settings") as batch:
        batch.add_column(
            sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC")
        )
        batch.add_column(
            sa.Column("telegram_history_limit", sa.Integer(), nullable=False, server_default="100")
        )
        batch.add_column(
            sa.Column("recent_context_messages", sa.Integer(), nullable=False, server_default="30")
        )
        batch.add_column(
            sa.Column("context_max_chars", sa.Integer(), nullable=False, server_default="30000")
        )
        batch.add_column(
            sa.Column("summarization_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.add_column(
            sa.Column("summarize_after_messages", sa.Integer(), nullable=False, server_default="80")
        )

    with op.batch_alter_table("message_logs") as batch:
        batch.add_column(sa.Column("user_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("sender_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("message_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("message_at", sa.DateTime(timezone=True), nullable=True))

    # Preserve old rows and promote Telegram metadata into searchable columns where SQLite supports it.
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text("""
            UPDATE message_logs
            SET
                user_id = COALESCE(
                    CAST(json_extract(metadata_json, '$.user_id') AS TEXT),
                    CAST(json_extract(metadata_json, '$.sender_id') AS TEXT)
                ),
                sender_id = CAST(json_extract(metadata_json, '$.sender_id') AS TEXT),
                message_id = CAST(json_extract(metadata_json, '$.message_id') AS TEXT),
                message_at = COALESCE(
                    json_extract(metadata_json, '$.message_at'),
                    json_extract(metadata_json, '$.date')
                )
            WHERE metadata_json IS NOT NULL
        """))
    connection.execute(sa.text("""
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY agent_id, account_id, chat_id, message_id
                ORDER BY id DESC
            ) AS duplicate_rank
            FROM message_logs
            WHERE message_id IS NOT NULL
        )
        UPDATE message_logs
        SET message_id = NULL
        WHERE id IN (SELECT id FROM ranked WHERE duplicate_rank > 1)
    """))

    with op.batch_alter_table("message_logs") as batch:
        batch.create_index("ix_message_logs_user_id", ["user_id"], unique=False)
        batch.create_index("ix_message_logs_sender_id", ["sender_id"], unique=False)
        batch.create_index("ix_message_logs_message_id", ["message_id"], unique=False)
        batch.create_index("ix_message_logs_message_at", ["message_at"], unique=False)
        batch.create_index(
            "ix_message_logs_conversation_time",
            ["agent_id", "account_id", "chat_id", "user_id", "message_at"],
            unique=False,
        )
        batch.create_unique_constraint(
            "uq_message_logs_telegram_message",
            ["agent_id", "account_id", "chat_id", "message_id"],
        )

    op.create_table(
        "conversation_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("rolling_summary", sa.Text(), nullable=False),
        sa.Column("summary_through_message_id", sa.String(length=64), nullable=True),
        sa.Column("summary_through_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_id", sa.String(length=64), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_user_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_agent_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["telegram_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "account_id",
            "chat_id",
            "user_id",
            name="uq_conversation_states_identity",
        ),
    )
    op.create_index("ix_conversation_states_agent_id", "conversation_states", ["agent_id"])
    op.create_index("ix_conversation_states_account_id", "conversation_states", ["account_id"])
    op.create_index("ix_conversation_states_chat_id", "conversation_states", ["chat_id"])
    op.create_index("ix_conversation_states_user_id", "conversation_states", ["user_id"])
    op.create_index(
        "ix_conversation_states_last_message_at",
        "conversation_states",
        ["last_message_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_states_last_message_at", table_name="conversation_states")
    op.drop_index("ix_conversation_states_user_id", table_name="conversation_states")
    op.drop_index("ix_conversation_states_chat_id", table_name="conversation_states")
    op.drop_index("ix_conversation_states_account_id", table_name="conversation_states")
    op.drop_index("ix_conversation_states_agent_id", table_name="conversation_states")
    op.drop_table("conversation_states")
    with op.batch_alter_table("message_logs") as batch:
        batch.drop_constraint("uq_message_logs_telegram_message", type_="unique")
        batch.drop_index("ix_message_logs_conversation_time")
        batch.drop_index("ix_message_logs_message_at")
        batch.drop_index("ix_message_logs_message_id")
        batch.drop_index("ix_message_logs_sender_id")
        batch.drop_index("ix_message_logs_user_id")
        batch.drop_column("message_at")
        batch.drop_column("message_id")
        batch.drop_column("sender_id")
        batch.drop_column("user_id")
    with op.batch_alter_table("runtime_settings") as batch:
        batch.drop_column("summarize_after_messages")
        batch.drop_column("summarization_enabled")
        batch.drop_column("context_max_chars")
        batch.drop_column("recent_context_messages")
        batch.drop_column("telegram_history_limit")
        batch.drop_column("timezone")
