"""conversation memory scope columns

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_states") as batch:
        batch.add_column(
            sa.Column("thread_id", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("project_id", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("customer_id", sa.String(length=120), nullable=True))
        batch.drop_constraint("uq_conversation_states_identity", type_="unique")
        batch.create_unique_constraint(
            "uq_conversation_states_identity",
            ["agent_id", "account_id", "chat_id", "user_id", "thread_id"],
        )
        batch.create_index("ix_conversation_states_thread_id", ["thread_id"], unique=False)
        batch.create_index("ix_conversation_states_project_id", ["project_id"], unique=False)
        batch.create_index("ix_conversation_states_customer_id", ["customer_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("conversation_states") as batch:
        batch.drop_index("ix_conversation_states_customer_id")
        batch.drop_index("ix_conversation_states_project_id")
        batch.drop_index("ix_conversation_states_thread_id")
        batch.drop_constraint("uq_conversation_states_identity", type_="unique")
        batch.create_unique_constraint(
            "uq_conversation_states_identity",
            ["agent_id", "account_id", "chat_id", "user_id"],
        )
        batch.drop_column("customer_id")
        batch.drop_column("project_id")
        batch.drop_column("thread_id")
