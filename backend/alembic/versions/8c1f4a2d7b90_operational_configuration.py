"""dashboard-managed operational configuration

Revision ID: 8c1f4a2d7b90
Revises: bf9dd8f5cde3
Create Date: 2026-07-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c1f4a2d7b90"
down_revision: Union[str, None] = "bf9dd8f5cde3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=48), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("default_model", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_profiles_name", "llm_profiles", ["name"], unique=True)
    with op.batch_alter_table("telegram_accounts") as batch:
        batch.add_column(sa.Column("api_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("api_hash_ciphertext", sa.Text(), nullable=True))
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("llm_profile_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_agents_llm_profile_id",
            "llm_profiles",
            ["llm_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("mcp_servers") as batch:
        batch.add_column(sa.Column("env_ciphertext", sa.Text(), nullable=True))
    op.create_table(
        "runtime_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("search_provider", sa.String(length=16), nullable=False),
        sa.Column("searxng_url", sa.String(length=1024), nullable=True),
        sa.Column("memory_enabled", sa.Boolean(), nullable=False),
        sa.Column("memory_backend", sa.String(length=16), nullable=False),
        sa.Column("mem0_api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("qdrant_url", sa.String(length=1024), nullable=True),
        sa.Column("memory_llm_profile_id", sa.Integer(), nullable=True),
        sa.Column("typing_min_seconds", sa.Float(), nullable=False),
        sa.Column("typing_max_seconds", sa.Float(), nullable=False),
        sa.Column("typing_jitter_seconds", sa.Float(), nullable=False),
        sa.Column("typing_chunk_size", sa.Integer(), nullable=False),
        sa.Column("typing_presence", sa.Boolean(), nullable=False),
        sa.Column("task_workers", sa.Integer(), nullable=False),
        sa.Column("max_tool_rounds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_llm_profile_id"], ["llm_profiles.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
    with op.batch_alter_table("mcp_servers") as batch:
        batch.drop_column("env_ciphertext")
    with op.batch_alter_table("agents") as batch:
        batch.drop_constraint("fk_agents_llm_profile_id", type_="foreignkey")
        batch.drop_column("llm_profile_id")
    with op.batch_alter_table("telegram_accounts") as batch:
        batch.drop_column("api_hash_ciphertext")
        batch.drop_column("api_id")
    op.drop_index("ix_llm_profiles_name", table_name="llm_profiles")
    op.drop_table("llm_profiles")
