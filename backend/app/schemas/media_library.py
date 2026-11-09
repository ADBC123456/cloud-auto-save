from __future__ import annotations

from pydantic import BaseModel, Field


class OpenListStorageOut(BaseModel):
    id: str
    mount_path: str
    driver: str = ""
    drive_type: str | None = None
    root_path: str | None = None
    status: str = ""
    disabled: bool = False
    remark: str = ""


class EmbyLibraryOut(BaseModel):
    id: str
    name: str
    collection_type: str = ""
    locations: list[str] = []


class MediaCategoryBase(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    kind: str | None = None
    drive_type: str | None = None
    cloud_root: str | None = None
    openlist_storage_id: str | None = None
    openlist_mount_path: str | None = None
    openlist_root_dir: str | None = None
    emby_library_id: str | None = None
    emby_library_name: str | None = None
    sort_order: int | None = Field(default=None, ge=0, le=9999)


class MediaCategoryIn(MediaCategoryBase):
    name: str = Field(min_length=1, max_length=128)
    cloud_root: str = Field(min_length=1)


class MediaCategoryUpdateIn(MediaCategoryBase):
    pass


class MediaCategoryOut(BaseModel):
    id: int
    name: str
    kind: str = ""
    drive_type: str | None = None
    cloud_root: str
    openlist_storage_id: str | None = None
    openlist_mount_path: str | None = None
    openlist_root_dir: str | None = None
    emby_library_id: str | None = None
    emby_library_name: str | None = None
    sort_order: int = 0


class MediaCategorySuggestionOut(BaseModel):
    name: str
    kind: str = ""
    drive_type: str | None = None
    cloud_root: str = ""
    openlist_storage_id: str | None = None
    openlist_mount_path: str | None = None
    openlist_root_dir: str | None = None
    emby_library_id: str | None = None
    emby_library_name: str | None = None
    sort_order: int = 0
    locations: list[str] = []
    openlist_path: str | None = None
    matched: bool = False
    already_configured: bool = False
    note: str = ""


class MediaCategorySuggestOut(BaseModel):
    storages: list[OpenListStorageOut] = []
    libraries: list[EmbyLibraryOut] = []
    suggestions: list[MediaCategorySuggestionOut] = []
