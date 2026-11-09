from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MediaCategory(Base):
    """媒体库分类：把「网盘保存目录 ↔ OpenList 存储 ↔ Emby 媒体库」一次配好，建任务只需选分类。"""

    __tablename__ = "media_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    drive_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cloud_root: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    openlist_storage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    openlist_mount_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    openlist_root_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    emby_library_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    emby_library_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
