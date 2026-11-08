"""媒体库分类：一次配置「网盘保存目录 ↔ OpenList 存储 ↔ Emby 媒体库」，建任务只需选分类。

- 连接信息全部复用已有配置（openlist / emby / alist_strm_gen 插件配置、系统设置里的 OpenList），不新增连接配置。
- Emby 媒体库路径无论是 rclone 挂载 OpenList（/mnt/openlist/百度/TV/国产剧）还是 strm 目录
  （/media/百度/TV/国产剧），结构都是「本地前缀 + OpenList 路径」，按挂载路径的连续段即可反推网盘目录。
- 任务执行时按 extra.media_category_id 或保存路径前缀匹配分类，只补空着的插件参数，不覆盖手填值。
"""

from __future__ import annotations

import json
import logging
import posixpath
from typing import Any, Iterable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import bad_request, not_found
from app.models.media_category import MediaCategory
from app.models.openlist_setting import OpenListSetting
from app.models.plugin_definition import PluginDefinition
from app.thirdparty.openlist_client import OpenListClient


logger = logging.getLogger(__name__)

CATEGORY_KINDS = ("tv", "anime", "variety", "movie")
_ROOT_FOLDER_IDS = {"", "0", "root", "-11", "/"}
_EDITABLE_FIELDS = (
    "name",
    "kind",
    "drive_type",
    "cloud_root",
    "openlist_storage_id",
    "openlist_mount_path",
    "openlist_root_dir",
    "emby_library_id",
    "emby_library_name",
    "sort_order",
)


# ---------------------------------------------------------------- 纯函数（可单测）


def norm_path(value: Any) -> str:
    p = str(value or "").strip().replace("\\", "/")
    if not p:
        return ""
    p = posixpath.normpath("/" + p.lstrip("/"))
    return "/" if p in (".", "/") else p


def _segments(value: Any) -> list[str]:
    return [s for s in str(value or "").replace("\\", "/").split("/") if s and s != "."]


def _join(base: str, rest: list[str]) -> str:
    return norm_path("/".join([base or "/", *rest]))


def driver_to_drive_type(driver: Any) -> str | None:
    """OpenList 驱动名 → 本项目网盘类型（adapters 的 DRIVE_TYPE）。"""
    d = str(driver or "").lower().replace(" ", "").replace("_", "").replace("-", "")
    if not d:
        return None
    rules = (
        ("baidunetdisk", "baidu"),
        ("quark", "quark"),
        ("uc", "uc"),
        ("115", "115"),
        ("aliyundrive", "aliyun"),
        ("189", "cloud189"),
        ("thunder", "xunlei"),
        ("123", "123pan"),
        ("139", "cloud139"),
    )
    for prefix, drive_type in rules:
        if d.startswith(prefix):
            return drive_type
    return None


def storage_root_path(addition: Any) -> str | None:
    """OpenList 存储根目录对应的网盘路径；根目录用 ID 表示且不是网盘根时返回 None（需手动填）。"""
    data = addition
    if isinstance(addition, str):
        try:
            data = json.loads(addition) if addition.strip() else {}
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        return None
    root_path = str(data.get("root_folder_path") or "").strip()
    if root_path:
        return norm_path(root_path)
    if "root_folder_id" in data:
        return "/" if str(data.get("root_folder_id") or "").strip() in _ROOT_FOLDER_IDS else None
    return "/"


def normalize_storage(item: dict[str, Any]) -> dict[str, Any] | None:
    mount_path = item.get("mount_path") or item.get("mountPath")
    if item.get("id") is None or not mount_path:
        return None
    driver = str(item.get("driver") or "")
    return {
        "id": str(item.get("id")),
        "mount_path": norm_path(mount_path),
        "driver": driver,
        "drive_type": driver_to_drive_type(driver),
        "root_path": storage_root_path(item.get("addition")),
        "status": str(item.get("status") or ""),
        "disabled": bool(item.get("disabled")),
        "remark": str(item.get("remark") or ""),
    }


def match_location(location: str, storages: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """在 Emby 媒体库路径里找 OpenList 挂载路径（越长越优先），反推 OpenList 路径和网盘目录。"""
    loc = _segments(location)
    candidates = sorted(
        (s for s in storages if _segments(s.get("mount_path"))),
        key=lambda s: len(_segments(s.get("mount_path"))),
        reverse=True,
    )
    for storage in candidates:
        mount = _segments(storage["mount_path"])
        for i in range(len(loc) - len(mount) + 1):
            if loc[i : i + len(mount)] != mount:
                continue
            rest = loc[i + len(mount) :]
            root_path = storage.get("root_path")
            return {
                "storage": storage,
                "openlist_path": _join("/", mount + rest),
                "cloud_root": _join(root_path, rest) if root_path else None,
            }
    return None


def guess_kind(name: Any, collection_type: Any = "") -> str:
    n = str(name or "").lower()
    ct = str(collection_type or "").lower()
    if any(k in n for k in ("动漫", "动画", "番剧", "anime", "cartoon")):
        return "anime"
    if any(k in n for k in ("综艺", "真人秀", "variety")):
        return "variety"
    if ct == "movies" or "电影" in n or "movie" in n:
        return "movie"
    if ct == "tvshows" or any(k in n for k in ("剧", "tv", "series")):
        return "tv"
    return ""


def build_suggestions(
    libraries: list[dict[str, Any]],
    storages: list[dict[str, Any]],
    *,
    existing: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    configured = {str(c.get("emby_library_id") or "") for c in existing if c.get("emby_library_id")}
    out: list[dict[str, Any]] = []
    for order, lib in enumerate(libraries, start=1):
        if str(lib.get("collection_type") or "").lower() in {"music", "books", "photos", "playlists", "boxsets"}:
            continue
        match = None
        for location in lib.get("locations") or []:
            match = match_location(location, storages)
            if match:
                break
        storage = (match or {}).get("storage") or {}
        if match and match.get("cloud_root"):
            note = "已自动匹配"
        elif match:
            note = "已匹配到 OpenList 存储，但该存储的网盘根目录是 ID，请手动选择网盘保存目录"
        else:
            note = "未能自动匹配 OpenList 存储，请手动选择"
        out.append(
            {
                "name": str(lib.get("name") or ""),
                "kind": guess_kind(lib.get("name"), lib.get("collection_type")),
                "drive_type": storage.get("drive_type"),
                "cloud_root": (match or {}).get("cloud_root") or "",
                "openlist_storage_id": storage.get("id"),
                "openlist_mount_path": storage.get("mount_path"),
                "openlist_root_dir": storage.get("root_path"),
                "emby_library_id": str(lib.get("id") or ""),
                "emby_library_name": str(lib.get("name") or ""),
                "sort_order": order,
                "locations": list(lib.get("locations") or []),
                "openlist_path": (match or {}).get("openlist_path"),
                "matched": bool(match and match.get("cloud_root")),
                "already_configured": str(lib.get("id") or "") in configured,
                "note": note,
            }
        )
    return out


def _under(path: str, root: str) -> bool:
    if not path or not root:
        return False
    return root == "/" or path == root or path.startswith(root.rstrip("/") + "/")


def pick_category(
    categories: Iterable[dict[str, Any]],
    *,
    savepath: Any,
    drive_type: Any = None,
    category_id: Any = None,
) -> dict[str, Any] | None:
    items = list(categories)
    if category_id not in (None, ""):
        for c in items:
            if str(c.get("id")) == str(category_id):
                return c
    sp = norm_path(savepath)
    dt = str(drive_type or "").strip()
    best: tuple[int, dict[str, Any]] | None = None
    for c in items:
        root = norm_path(c.get("cloud_root"))
        if not _under(sp, root):
            continue
        cdt = str(c.get("drive_type") or "").strip()
        if cdt and dt and cdt != dt:
            continue
        score = len(_segments(root))
        if best is None or score > best[0]:
            best = (score, c)
    return best[1] if best else None


def apply_category_to_addition(addition: Any, category: dict[str, Any]) -> dict[str, Any]:
    """把分类里的 OpenList/Emby 参数补进任务 addition（只补空字段，不覆盖手填值）。"""
    out = addition if isinstance(addition, dict) else {}
    mount_path = str(category.get("openlist_mount_path") or "").strip()
    if mount_path:
        ol = out.get("openlist")
        if not isinstance(ol, dict):
            ol = {"enable": True, "refresh": True, "password": ""}
        for key, value in (
            ("driver", category.get("openlist_storage_id")),
            ("mount_path", mount_path),
            ("root_dir", category.get("openlist_root_dir")),
        ):
            if value and not str(ol.get(key) or "").strip():
                ol[key] = str(value)
        out["openlist"] = ol
    library_id = str(category.get("emby_library_id") or "").strip()
    if library_id:
        em = out.get("emby")
        if not isinstance(em, dict):
            em = {"try_match": True, "media_id": ""}
        if not str(em.get("library_id") or "").strip():
            em["library_id"] = library_id
        out["emby"] = em
    return out


# ---------------------------------------------------------------- 连接（复用已有配置）


def _plugin_config(db: Session, plugin_key: str) -> dict[str, Any]:
    row = (
        db.execute(
            select(PluginDefinition)
            .options(selectinload(PluginDefinition.config))
            .where(PluginDefinition.plugin_key == plugin_key)
        )
        .scalars()
        .first()
    )
    if row is None or row.config is None or not row.config.config_json:
        return {}
    try:
        data = json.loads(row.config.config_json)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def openlist_connection(db: Session) -> tuple[str, str] | None:
    sources = [_plugin_config(db, "openlist")]
    setting = db.execute(select(OpenListSetting).order_by(OpenListSetting.id.asc())).scalars().first()
    if setting is not None:
        sources.append({"url": setting.url, "token": setting.token})
    sources.append(_plugin_config(db, "alist_strm_gen"))
    for src in sources:
        url = str(src.get("url") or "").strip().rstrip("/")
        token = str(src.get("token") or "").strip()
        if url and token:
            return url, token
    return None


def emby_connection(db: Session) -> tuple[str, str] | None:
    cfg = _plugin_config(db, "emby")
    url = str(cfg.get("url") or "").strip().rstrip("/")
    token = str(cfg.get("token") or "").strip()
    return (url, token) if url and token else None


def list_openlist_storages(db: Session) -> list[dict[str, Any]]:
    conn = openlist_connection(db)
    if conn is None:
        raise bad_request("OPENLIST_NOT_CONFIGURED", "请先在「插件管理 → openlist」或「系统设置 → OpenList」填写 OpenList 地址和 Token")
    try:
        payload = OpenListClient(conn[0], token=conn[1], max_retries=1).admin_storage_list()
    except Exception as exc:
        raise bad_request("OPENLIST_UNAVAILABLE", "获取 OpenList 存储列表失败（Token 需为管理员 Token）", detail=str(exc))
    data = payload.get("data")
    items = data.get("content") if isinstance(data, dict) else data
    out = [s for s in (normalize_storage(it) for it in (items or []) if isinstance(it, dict)) if s]
    return sorted(out, key=lambda s: s["mount_path"])


def list_emby_libraries(db: Session) -> list[dict[str, Any]]:
    conn = emby_connection(db)
    if conn is None:
        raise bad_request("EMBY_NOT_CONFIGURED", "请先在「插件管理 → emby」填写 Emby 地址和 Token")
    try:
        resp = requests.get(f"{conn[0]}/emby/Library/VirtualFolders", headers={"X-Emby-Token": conn[1]}, timeout=10)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        raise bad_request("EMBY_UNAVAILABLE", "获取 Emby 媒体库列表失败，请检查地址和 Token", detail=str(exc))
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        library_id = row.get("ItemId") or row.get("Id")
        if not library_id:
            continue
        out.append(
            {
                "id": str(library_id),
                "name": str(row.get("Name") or ""),
                "collection_type": str(row.get("CollectionType") or ""),
                "locations": [str(x) for x in (row.get("Locations") or []) if x],
            }
        )
    return out


# ---------------------------------------------------------------- 分类 CRUD


def category_to_dict(item: MediaCategory) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "kind": item.kind or "",
        "drive_type": item.drive_type or None,
        "cloud_root": item.cloud_root or "",
        "openlist_storage_id": item.openlist_storage_id or None,
        "openlist_mount_path": item.openlist_mount_path or None,
        "openlist_root_dir": item.openlist_root_dir or None,
        "emby_library_id": item.emby_library_id or None,
        "emby_library_name": item.emby_library_name or None,
        "sort_order": int(item.sort_order or 0),
    }


def list_categories(db: Session) -> list[MediaCategory]:
    return list(
        db.execute(select(MediaCategory).order_by(MediaCategory.sort_order.asc(), MediaCategory.id.asc())).scalars().all()
    )


def get_category(db: Session, category_id: int) -> MediaCategory:
    item = db.get(MediaCategory, category_id)
    if item is None:
        raise not_found("MEDIA_CATEGORY_NOT_FOUND", "媒体库分类不存在")
    return item


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _EDITABLE_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "sort_order":
            out[key] = int(value or 0)
        elif key in ("cloud_root", "openlist_mount_path", "openlist_root_dir"):
            out[key] = norm_path(value) or ("" if key == "cloud_root" else None)
        elif key == "kind":
            kind = str(value or "").strip()
            out[key] = kind if kind in CATEGORY_KINDS else ""
        else:
            text = str(value or "").strip()
            out[key] = text or (None if key != "name" else "")
    if "name" in out and not out["name"]:
        raise bad_request("MEDIA_CATEGORY_NAME_REQUIRED", "请填写分类名称")
    if "cloud_root" in out and not out["cloud_root"]:
        raise bad_request("MEDIA_CATEGORY_ROOT_REQUIRED", "请选择网盘保存目录")
    return out


def create_category(db: Session, payload: dict[str, Any]) -> MediaCategory:
    data = _clean(payload)
    if not data.get("name"):
        raise bad_request("MEDIA_CATEGORY_NAME_REQUIRED", "请填写分类名称")
    if not data.get("cloud_root"):
        raise bad_request("MEDIA_CATEGORY_ROOT_REQUIRED", "请选择网盘保存目录")
    item = MediaCategory(**data)
    db.add(item)
    db.flush()
    return item


def update_category(db: Session, category_id: int, payload: dict[str, Any]) -> MediaCategory:
    item = get_category(db, category_id)
    for key, value in _clean(payload).items():
        setattr(item, key, value)
    db.flush()
    return item


def delete_category(db: Session, category_id: int) -> None:
    db.delete(get_category(db, category_id))
    db.flush()


def suggest_categories(db: Session) -> dict[str, Any]:
    libraries = list_emby_libraries(db)
    storages = list_openlist_storages(db)
    existing = [category_to_dict(c) for c in list_categories(db)]
    return {
        "storages": storages,
        "libraries": libraries,
        "suggestions": build_suggestions(libraries, storages, existing=existing),
    }


# ---------------------------------------------------------------- 任务执行时自动补参数


def _task_drive_type(db: Session, task_data: dict[str, Any]) -> str | None:
    account_name = str(task_data.get("account_name") or "").strip()
    if account_name:
        from app.models.drive_account import DriveAccount

        row = db.execute(select(DriveAccount).where(DriveAccount.name == account_name)).scalars().first()
        if row is not None and row.drive_type:
            return str(row.drive_type)
    try:
        from app.extensions.runtime.adapter_registry import AdapterRegistry

        return AdapterRegistry.detect_drive_type(str(task_data.get("shareurl") or "")) or None
    except Exception:
        return None


def apply_category_to_task_data(db: Session, task_data: dict[str, Any]) -> dict[str, Any] | None:
    categories = [category_to_dict(c) for c in list_categories(db)]
    if not categories:
        return None
    extra = task_data.get("extra") if isinstance(task_data.get("extra"), dict) else {}
    category = pick_category(
        categories,
        savepath=task_data.get("savepath"),
        drive_type=_task_drive_type(db, task_data),
        category_id=extra.get("media_category_id"),
    )
    if category is None:
        return None
    task_data["addition"] = apply_category_to_addition(task_data.get("addition"), category)
    return category
