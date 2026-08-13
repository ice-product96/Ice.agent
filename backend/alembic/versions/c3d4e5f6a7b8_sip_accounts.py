"""sip accounts and calls

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sip_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("sip_server", sa.String(length=255), nullable=False, server_default="voice.telphin.com:5068"),
        sa.Column("domain", sa.String(length=255), nullable=False, server_default="sip.telphin.com"),
        sa.Column("login", sa.String(length=120), nullable=False),
        sa.Column("auth_username", sa.String(length=120), nullable=True),
        sa.Column("password_ciphertext", sa.Text(), nullable=True),
        sa.Column("transport", sa.String(length=16), nullable=False, server_default="udp"),
        sa.Column("sip_proxy", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("caller_id", sa.String(length=64), nullable=True),
        sa.Column("stun_server", sa.String(length=255), nullable=True),
        sa.Column("public_ip", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("register_on_startup", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("max_concurrent_calls", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sip_accounts_name", "sip_accounts", ["name"], unique=True)
    op.create_index("ix_sip_accounts_login", "sip_accounts", ["login"], unique=False)

    op.create_table(
        "sip_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sip_account_id", sa.Integer(), sa.ForeignKey("sip_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False, server_default="outbound"),
        sa.Column("remote_number", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="initiated"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hangup_cause", sa.String(length=120), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sip_calls_agent_id", "sip_calls", ["agent_id"])
    op.create_index("ix_sip_calls_sip_account_id", "sip_calls", ["sip_account_id"])
    op.create_index("ix_sip_calls_status", "sip_calls", ["status"])

    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("sip_account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_agents_sip_account_id",
            "sip_accounts",
            ["sip_account_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_constraint("fk_agents_sip_account_id", type_="foreignkey")
        batch.drop_column("sip_account_id")
    op.drop_table("sip_calls")
    op.drop_table("sip_accounts")
