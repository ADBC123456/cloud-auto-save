"""media categories

Revision ID: 20261108_media_categories
Revises: 20260823_tasks_add_paused_until
Create Date: 2026-11-08 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20261108_media_categories"
down_revision = "20260823_tasks_add_paused_until"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "media_categories" in inspector.get_table_names():
        return
    op.create_table(
        "media_categories",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=True),
        sa.Column("drive_type", sa.String(length=32), nullable=True),
        sa.Column("cloud_root", sa.Text(), nullable=False, server_default=""),
        sa.Column("openlist_storage_id", sa.String(length=64), nullable=True),
        sa.Column("openlist_mount_path", sa.Text(), nullable=True),
        sa.Column("openlist_root_dir", sa.Text(), nullable=True),
        sa.Column("emby_library_id", sa.String(length=128), nullable=True),
        sa.Column("emby_library_name", sa.String(length=256), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "media_categories" not in inspector.get_table_names():
        return
    op.drop_table("media_categories")
