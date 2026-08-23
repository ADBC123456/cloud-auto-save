"""tasks add paused_until

Revision ID: 20260823_tasks_add_paused_until
Revises: 20260613_telegram_bot_state_and_sessions
Create Date: 2026-08-23 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260823_tasks_add_paused_until"
down_revision = "20260613_telegram_bot_state_and_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "tasks" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("tasks")}
    if "paused_until" not in cols:
        op.add_column("tasks", sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "tasks" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("tasks")}
    if "paused_until" in cols:
        op.drop_column("tasks", "paused_until")
