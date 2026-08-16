from __future__ import annotations

import base64
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.errors import bad_request
from app.db.session import engine
from app.extensions.runtime.adapter_registry import AdapterRegistry
from app.models.drive_account import DriveAccount
from app.models.resource_search_source import ResourceSearchSource
from app.services.invalid_share_links import list_invalid_shareurls


SOURCE_KEYS = ("net", "cloudsaver", "pansou", "unisearch")
EXTERNAL_TIMEOUT = (15, 200)
logger = logging.getLogger(__name__)

SUPPORTED_CLOUD_TYPES = {
    "quark",
    "pan123",
    "pan115",
    "cloud115",
    "uc",
    "tianyi",
    "cloud189",
    "aliyun",
    "xunlei",
    "baidupan",
    "baidu",
}


def _normalize_drive_type(value: str | None) -> str | None:
    v = str(value or "").strip()
    if not v:
        return None
    k = v.lower()
    mapping = {
        "pan115": "115",
        "cloud115": "115",
        "115": "115",
        "pan123": "123pan",
        "123pan": "123pan",
        "tianyi": "cloud189",
        "cloud189": "cloud189",
        "baidupan": "baidu",
        "baidu": "baidu",
        "quark": "quark",
        "uc": "uc",
        "aliyun": "aliyun",
        "xunlei": "xunlei",
    }
    return mapping.get(k) or k


def _pansou_cloud_type(value: str | None) -> str | None:
    dt = _normalize_drive_type(value)
    if not dt:
        return None
    mapping = {
        "115": "115",
        "123pan": "pan123",
        "cloud189": "tianyi",
        "baidu": "baiduPan",
        "quark": "quark",
        "uc": "uc",
        "aliyun": "aliyun",
        "xunlei": "xunlei",
        "cloud139" : "mobile",
        "guangya": "guangya"
    }
    return mapping.get(dt)


def _debug_enabled() -> bool:
    return os.getenv("DEBUG", "0").strip().lower() in {"1", "true", "yes", "y", "on"}


def _mask_secret(value: str, keep: int = 2) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if len(v) <= keep:
        return "*" * len(v)
    return v[:keep] + "*" * (len(v) - keep)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k).lower()
            if key in {"password", "passwd"}:
                out[k] = "***"
            elif key in {"token", "access_token", "refresh_token", "authorization"}:
                out[k] = _mask_secret(str(v), keep=4)
            elif key in {"cookie", "cookies"}:
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _truncate_text(text: str, max_chars: int = 20000) -> str:
    if max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n... (truncated, total_chars={len(text)}) ...\n{tail}"


def _log_debug(label: str, payload: Any) -> None:
    if not (_debug_enabled() or logger.isEnabledFor(logging.DEBUG)):
        return
    try:
        text = json.dumps(_redact(payload), ensure_ascii=False, indent=2, default=str)
    except Exception:
        text = repr(_redact(payload))
    msg = f"{label}={_truncate_text(text)}"
    if _debug_enabled():
        logger.info(msg)
    else:
        logger.debug(msg)


def _loads(value: str) -> dict[str, Any]:
    try:
        obj = json.loads(value or "")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def ensure_default_sources(db: Session) -> dict[str, ResourceSearchSource]:
    try:
        existing = {r.key: r for r in db.execute(select(ResourceSearchSource)).scalars().all()}
    except OperationalError as e:
        if "no such table: resource_search_sources" not in str(e):
            raise
        ResourceSearchSource.__table__.create(bind=engine, checkfirst=True)
        existing = {r.key: r for r in db.execute(select(ResourceSearchSource)).scalars().all()}
    changed = False
    for key in SOURCE_KEYS:
        if key in existing:
            continue
        enabled = True if key == "net" else False
        row = ResourceSearchSource(key=key, enabled=enabled, config_json=_dumps({}))
        db.add(row)
        db.flush()
        existing[key] = row
        changed = True
    if changed:
        db.flush()
    return existing


def list_sources(db: Session) -> list[dict[str, Any]]:
    rows = ensure_default_sources(db)
    out: list[dict[str, Any]] = []
    for key in SOURCE_KEYS:
        row = rows[key]
        cfg = _loads(row.config_json or "")
        item: dict[str, Any] = {"key": row.key, "enabled": bool(row.enabled), "server": None, "username": None, "password": None, "token": None}
        if key == "cloudsaver":
            item["server"] = cfg.get("server") or None
            item["username"] = cfg.get("username") or None
            item["token"] = cfg.get("token") or None
            item["password"] = None
        elif key == "pansou":
            item["server"] = cfg.get("server") or None
        elif key == "unisearch":
            item["server"] = cfg.get("server") or None
            item["username"] = cfg.get("username") or None
            item["token"] = cfg.get("token") or None
            item["password"] = None
        out.append(item)
    return out


def update_source(db: Session, key: str, payload: dict[str, Any]) -> ResourceSearchSource:
    key = str(key or "").strip()
    if key not in SOURCE_KEYS:
        raise bad_request("RESOURCE_SEARCH_SOURCE_INVALID", "不支持的搜索源")

    rows = ensure_default_sources(db)
    row = rows[key]
    cfg = _loads(row.config_json or "")

    if "enabled" in payload and payload["enabled"] is not None:
        row.enabled = bool(payload["enabled"])

    if key == "net":
        row.config_json = _dumps(cfg)
        db.flush()
        return row

    if "server" in payload and payload["server"] is not None:
        cfg["server"] = str(payload["server"] or "").strip()

    if key == "pansou":
        row.config_json = _dumps(cfg)
        db.flush()
        return row

    if key == "unisearch":
        if "username" in payload and payload["username"] is not None:
            cfg["username"] = str(payload["username"] or "").strip()
        if "token" in payload and payload["token"] is not None:
            cfg["token"] = str(payload["token"] or "").strip()
        if "password" in payload and payload["password"] is not None:
            pw = str(payload["password"] or "")
            if pw.strip():
                cfg["password"] = pw
        row.config_json = _dumps(cfg)
        db.flush()
        return row

    if "username" in payload and payload["username"] is not None:
        cfg["username"] = str(payload["username"] or "").strip()
    if "token" in payload and payload["token"] is not None:
        cfg["token"] = str(payload["token"] or "").strip()

    if "password" in payload and payload["password"] is not None:
        pw = str(payload["password"] or "")
        if pw.strip():
            cfg["password"] = pw

    row.config_json = _dumps(cfg)
    db.flush()
    return row


def _iso_to_cst(iso_time_str: str) -> str:
    from datetime import datetime, timezone, timedelta

    try:
        dt = datetime.fromisoformat(str(iso_time_str))
        dt_cst = dt if dt > datetime.now() else dt
        return dt_cst.strftime("%Y-%m-%d %H:%M:%S") if dt_cst.year >= 1970 else ""
    except Exception:
        return ""


class CloudSaverClient:
    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def set_auth(self, username: str, password: str, token: str = "") -> None:
        self.username = username
        self.password = password
        self.token = token
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})
        else:
            self.session.headers.pop("Authorization", None)

    def login(self) -> dict[str, Any]:
        url = f"{self.server}/api/user/login"
        _log_debug("[cloudsaver.login] request", {"url": url, "username": self.username})
        res = self.session.post(url, json={"username": self.username, "password": self.password}, timeout=EXTERNAL_TIMEOUT)
        data = res.json()
        _log_debug("[cloudsaver.login] response", {"status_code": res.status_code, "json": data})
        if data.get("success"):
            token = ((data.get("data") or {}).get("token")) or ""
            self.token = token
            self.session.headers.update({"Authorization": f"Bearer {token}"})
            return {"success": True, "token": token}
        return {"success": False, "message": data.get("message") or "CloudSaver 登录失败"}

    def search(self, keyword: str, last_message_id: str = "") -> dict[str, Any]:
        url = f"{self.server}/api/search"
        _log_debug("[cloudsaver.search] request", {"url": url, "keyword": keyword, "lastMessageId": last_message_id})
        res = self.session.get(url, params={"keyword": keyword, "lastMessageId": last_message_id})
        data = res.json()
        _log_debug("[cloudsaver.search] response", {"status_code": res.status_code, "json": data})
        if data.get("success"):
            return {"success": True, "data": data.get("data") or []}
        return {"success": False, "message": data.get("message") or "CloudSaver 搜索失败"}

    def auto_login_search(self, keyword: str) -> dict[str, Any]:
        if not getattr(self, "token", None):
            r = self.login()
            if not r.get("success"):
                return r
            token = r.get("token") or ""
            search = self.search(keyword)
            if search.get("success"):
                search["new_token"] = token
            return search

        search = self.search(keyword)
        if search.get("success"):
            return search
        r = self.login()
        if not r.get("success"):
            return search
        token = r.get("token") or ""
        search = self.search(keyword)
        if search.get("success"):
            search["new_token"] = token
        return search

    def clean_search_results(self, search_results: Any, keyword: str | None = None) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        if not isinstance(search_results, list):
            return links
        link_set: set[str] = set()
        keyword = str(keyword or "").strip()

        def _strip_html(text: str) -> str:
            if not text:
                return ""
            t = str(text).replace('<mark class="highlight">', "").replace("</mark>", "")
            return re.sub(r"<[^>]+>", "", t)

        def _title_contains_keyword(title: str) -> bool:
            if not keyword:
                return True
            title = (_strip_html(title) or "").strip()
            if match := re.search(r"(名称|标题)[：:]?(.*)", title, re.DOTALL):
                title = (match.group(2) or "").strip()
            title_norm = re.sub(r"\s+", "", title).lower()
            tokens = [t for t in re.split(r"\s+", keyword) if t]
            return all(re.sub(r"\s+", "", t).lower() in title_norm for t in tokens)

        enable_filter = os.getenv("CLOUDSAVER_TITLE_FILTER", "1").strip() != "0"

        for ch in search_results:
            if not isinstance(ch, dict):
                continue
            channel = ch.get("channel") or ch.get("name") or ""
            for item in (ch.get("list") or []):
                if not isinstance(item, dict):
                    continue
                if enable_filter and not _title_contains_keyword(str(item.get("title") or "")):
                    continue
                title = _strip_html(item.get("title") or "").strip() or ""
                if keyword and not _title_contains_keyword(title):
                    continue
                desc = _strip_html(item.get("desc") or item.get("content") or "").strip() or ""
                tm = item.get("datetime") or item.get("time") or ""
                if tm:
                    tm = _iso_to_cst(str(tm))
                cloud_links = item.get("cloudLinks") or item.get("cloud_links") or []
                if not isinstance(cloud_links, list):
                    continue
                for cl in cloud_links:
                    if not isinstance(cl, dict):
                        continue
                    url = str(cl.get("url") or cl.get("link") or "").strip()
                    if not url or url in link_set:
                        continue
                    drive = str(cl.get("cloudType") or cl.get("type") or "").lower()
                    if drive and drive not in SUPPORTED_CLOUD_TYPES:
                        continue
                    link_set.add(url)
                    links.append(
                        {
                            "shareurl": url,
                            "taskname": title,
                            "content": desc,
                            "datetime": tm,
                            "channel": str(channel),
                            "source": "CloudSaver",
                        }
                    )
        return links


class PanSouClient:
    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.session = requests.Session()

    def search(self, keyword: str, refresh: bool = False, *, drive_type: str | None = None) -> list[dict[str, Any]]:
        try:
            url = f"{self.server}/api/search"
            cloud_type = _pansou_cloud_type(drive_type)
            params = {
                "kw": keyword,
                "cloud_types": [cloud_type]
                if cloud_type
                else ["quark", "pan123", "pan115", "uc", "tianyi", "aliyun", "xunlei", "baiduPan"],
                "res": "merge",
                "refresh": bool(refresh),
            }
            res = self.session.get(url, params=params, timeout=EXTERNAL_TIMEOUT)
            data = res.json()
            if data.get("code") != 0:
                return []
            merged = (data.get("data") or {}).get("merged_by_type") or {}
            items: Any = []
            if cloud_type:
                keys: list[str] = [cloud_type, cloud_type.lower()]
                if cloud_type == "tianyi":
                    keys.extend(["cloud189", "tianyi"])
                elif cloud_type == "pan115":
                    keys.extend(["cloud115", "pan115", "115"])
                elif cloud_type == "pan123":
                    keys.extend(["123pan", "pan123"])
                elif cloud_type == "baiduPan":
                    keys.extend(["baidupan", "baidu"])
                for k in keys:
                    if isinstance(merged, dict) and isinstance(merged.get(k), list):
                        items = merged.get(k) or []
                        break
            if not items:
                items = (merged.get("quark") if isinstance(merged, dict) else None) or []
            return self.format_search_results(items)
        except Exception:
            return []

    def format_search_results(self, search_results: Any) -> list[dict[str, Any]]:
        import re

        if not isinstance(search_results, list):
            return []
        pattern = r"^(.*?)(?:[【\[]?(?:简介|介绍|描述)[】\]]?[:：]?)?(.*)$"
        out: list[dict[str, Any]] = []
        link_set: set[str] = set()
        for item in search_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in link_set:
                continue
            note = str(item.get("note") or "")
            tm = str(item.get("datetime") or "")
            if tm:
                tm = _iso_to_cst(tm)
            m = re.search(pattern, note, re.DOTALL)
            title = (m.group(1) if m else note).strip()
            content = (m.group(2) if m else "").strip()
            link_set.add(url)
            out.append(
                {
                    "shareurl": url,
                    "taskname": title,
                    "content": content,
                    "datetime": tm,
                    "channel": str(item.get("source") or ""),
                    "source": "PanSou",
                }
            )
        return out


class UniSearchClient:
    """UniSearch（PanSou 魔改 + 鉴权）客户端"""

    DRIVE_TYPE_TO_CLOUD = {
        "quark": "quark",
        "baidu": "baidu",
        "aliyun": "aliyun",
        "cloud189": "tianyi",
        "tianyi": "tianyi",
        "uc": "uc",
        "115": "115",
        "pan115": "115",
        "cloud115": "115",
        "xunlei": "xunlei",
        "123pan": "123",
        "pan123": "123",
    }
    ALL_CLOUD_TYPES = ["quark", "baidu", "aliyun", "tianyi", "uc", "115", "xunlei", "123"]

    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.session = requests.Session()
        self.username = ""
        self.password = ""
        self.token = ""

    def set_auth(self, username: str, password: str, token: str = "") -> None:
        self.username = username
        self.password = password
        self.token = token
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})
        else:
            self.session.headers.pop("Authorization", None)

    def login(self) -> str:
        url = f"{self.server}/api/auth/login"
        res = self.session.post(
            url,
            json={"username": self.username, "password": self.password},
            timeout=EXTERNAL_TIMEOUT,
        )
        data = res.json() if res.status_code == 200 else {}
        if data.get("code") in (0, 200):
            token = str(((data.get("data") or {}).get("access_token")) or "")
            if token:
                self.token = token
                self.session.headers.update({"Authorization": f"Bearer {token}"})
            return token
        return ""

    def _cloud_types(self, drive_type: str | None) -> list[str]:
        if not drive_type:
            return list(self.ALL_CLOUD_TYPES)
        cloud = self.DRIVE_TYPE_TO_CLOUD.get(str(drive_type).strip())
        return [cloud] if cloud else list(self.ALL_CLOUD_TYPES)

    def _search_once(self, keyword: str, cloud_types: list[str], refresh: bool) -> dict[str, Any]:
        url = f"{self.server}/api/search"
        res = self.session.post(
            url,
            json={"kw": keyword, "cloud_types": cloud_types, "src": "all", "res": "merge", "refresh": bool(refresh)},
            timeout=EXTERNAL_TIMEOUT,
        )
        if res.status_code == 401:
            return {"code": 401}
        data = res.json()
        return data if isinstance(data, dict) else {}

    def search(self, keyword: str, refresh: bool = False, *, drive_type: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        """返回 (统一格式结果, 新token|None)；401 时自动重登一次"""
        cloud_types = self._cloud_types(drive_type)
        data = self._search_once(keyword, cloud_types, refresh)
        new_token: str | None = None
        if data.get("code") == 401:
            token = self.login()
            if not token:
                return ([], None)
            new_token = token
            data = self._search_once(keyword, cloud_types, refresh)
        if data.get("code") not in (0, 200):
            return ([], new_token)
        resources = ((data.get("data") or {}).get("resources")) or []
        return (self.format_search_results(resources), new_token)

    def format_search_results(self, resources: Any) -> list[dict[str, Any]]:
        if not isinstance(resources, list):
            return []
        out: list[dict[str, Any]] = []
        link_set: set[str] = set()
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            src = resource.get("source") if isinstance(resource.get("source"), dict) else {}
            channel = str(src.get("channel") or src.get("name") or "")
            title = str(resource.get("title") or resource.get("name") or "")
            description = str(resource.get("description") or "")
            for link in resource.get("links") or []:
                if not isinstance(link, dict):
                    continue
                url = str(link.get("url") or "").strip()
                if not url or url in link_set:
                    continue
                link_set.add(url)
                out.append(
                    {
                        "shareurl": url,
                        "taskname": title,
                        "content": description,
                        "datetime": _unisearch_to_cst(str(link.get("datetime") or "")),
                        "channel": channel,
                        "source": "UniSearch",
                    }
                )
        return out


def _unisearch_to_cst(value: str) -> str:
    from datetime import datetime, timedelta, timezone

    v = str(value or "").strip()
    if not v:
        return ""
    try:
        v2 = v.replace("Z", "+00:00") if v.endswith("Z") else v
        dt = datetime.fromisoformat(v2)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt.year >= 1970 else ""
    except Exception:
        return ""


def fetch_task_suggestions(
    db: Session,
    keyword: str,
    deep: int,
    *,
    filter_invalid: bool = True,
    drive_type: str | None = None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    keyword = str(keyword or "").strip()
    if len(keyword) < 2:
        return ([], False, None)
    deep = deep or 0
    dt_filter = _normalize_drive_type(drive_type)
    _log_debug("[suggestions] request", {"keyword": keyword, "deep": deep, "drive_type": dt_filter})

    rows = ensure_default_sources(db)
    cfg_net = _loads(rows["net"].config_json or "")
    cfg_cs = _loads(rows["cloudsaver"].config_json or "")
    cfg_ps = _loads(rows["pansou"].config_json or "")
    cfg_us = _loads(rows["unisearch"].config_json or "") if "unisearch" in rows else {}
    _log_debug(
        "[suggestions] sources",
        {
            "net": {"enabled": bool(rows["net"].enabled), "config": cfg_net},
            "cloudsaver": {"enabled": bool(rows["cloudsaver"].enabled), "config": cfg_cs},
            "pansou": {"enabled": bool(rows["pansou"].enabled), "config": cfg_ps},
        },
    )

    def net_search():
        try:
            def clean_search_results(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                out = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if keyword not in item.get("title"):
                        continue
                    out.append(item)
                return out
            if not rows["net"].enabled:
                return []
            base_url = base64.b64decode("aHR0cHM6Ly9wYW5zZWFyY2guMTIzY2YudG9w").decode()
            url = f"{base_url}/task_suggestions"
            r = requests.get(url, params={"q": keyword.lower(), "d": str(deep)}, timeout=EXTERNAL_TIMEOUT)
            data = r.json()

            if isinstance(data, dict):
                items = data.get("data") if isinstance(data.get("data"), list) else []
                return clean_search_results(items)
            return clean_search_results(items)
        except Exception:
            return []

    def cs_search():
        try:
            if not rows["cloudsaver"].enabled:
                return ([], None)
            server = str(cfg_cs.get("server") or "").strip()
            username = str(cfg_cs.get("username") or "").strip()
            password = str(cfg_cs.get("password") or "")
            token = str(cfg_cs.get("token") or "")
            if not (server and username and password):
                return ([], None)
            cs = CloudSaverClient(server)
            cs.set_auth(username=username, password=password, token=token)
            search = cs.auto_login_search(keyword.lower())
            if not search.get("success"):
                return ([], None)
            new_token = search.get("new_token") or None
            _log_debug("[cloudsaver.search] raw_data", search.get("data"))
            results = cs.clean_search_results(search.get("data"), keyword=keyword.lower())
            _log_debug("[cloudsaver.search] cleaned_results", {"count": len(results), "items": results[:20]})
            return (results, new_token)
        except Exception:
            return ([], None)

    def ps_search():
        try:
            if not rows["pansou"].enabled:
                return []
            server = str(cfg_ps.get("server") or "").strip()
            if not server:
                return []
            ps = PanSouClient(server)
            return ps.search(keyword.lower(), refresh=deep == 1, drive_type=dt_filter)
        except Exception:
            return []

    def us_search():
        try:
            if "unisearch" not in rows or not rows["unisearch"].enabled:
                return ([], None)
            server = str(cfg_us.get("server") or "").strip()
            username = str(cfg_us.get("username") or "").strip()
            password = str(cfg_us.get("password") or "")
            token = str(cfg_us.get("token") or "")
            if not (server and username and password):
                return ([], None)
            us = UniSearchClient(server)
            us.set_auth(username=username, password=password, token=token)
            return us.search(keyword.lower(), refresh=deep == 1, drive_type=dt_filter)
        except Exception:
            return ([], None)

    search_results: list[dict[str, Any]] = []
    new_token: str | None = None
    new_token_source: str | None = None

    def _tag(src: str, fn):
        def wrapper():
            return (src, fn())
        return wrapper

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_tag("net", net_search)),
            executor.submit(_tag("cloudsaver", cs_search)),
            executor.submit(_tag("pansou", ps_search)),
            executor.submit(_tag("unisearch", us_search)),
        ]
        for fut in as_completed(futures):
            try:
                src, r = fut.result()
            except Exception:
                continue
            if isinstance(r, tuple):
                items, tok = r
                if tok:
                    new_token = str(tok)
                    new_token_source = src
                if isinstance(items, list):
                    search_results.extend([x for x in items if isinstance(x, dict)])
                continue
            if isinstance(r, list):
                search_results.extend([x for x in r if isinstance(x, dict)])

    results: list[dict[str, Any]] = []
    link_set: set[str] = set()

    def _sort_key(x: dict[str, Any]) -> str:
        v = x.get("datetime")
        return str(v or "")

    search_results.sort(key=_sort_key, reverse=True)
    for item in search_results:
        url = str(item.get("shareurl") or "").strip()
        if not url or url in link_set:
            continue
        link_set.add(url)
        results.append(item)

    token_updated = False
    if new_token and new_token_source in ("cloudsaver", "unisearch") and new_token_source in rows:
        src_cfg = _loads(rows[new_token_source].config_json or "")
        src_cfg["token"] = new_token
        rows[new_token_source].config_json = _dumps(src_cfg)
        db.flush()
        token_updated = True

    enabled_drive_types = set(
        str(x or "").strip()
        for x in db.execute(
            select(DriveAccount.drive_type).where(DriveAccount.enabled.is_(True), DriveAccount.runtime_status == "active")
        )
        .scalars()
        .all()
    )
    enabled_drive_types = {_normalize_drive_type(x) for x in enabled_drive_types}
    enabled_drive_types = {x for x in enabled_drive_types if x}
    if not enabled_drive_types:
        return ([], token_updated, "没有可用的网盘账号（enabled=true 且 runtime_status=active），已隐藏不支持的资源")
    if dt_filter and dt_filter not in enabled_drive_types:
        return ([], token_updated, f"指定网盘类型没有可用账号：{dt_filter}")

    filtered: list[dict[str, Any]] = []
    removed_by_accounts = 0
    removed_by_drive_type = 0
    for item in results:
        url = str(item.get("shareurl") or "").strip()
        if not url:
            continue
        dt = AdapterRegistry.detect_drive_type(url)
        if not dt or str(dt) not in enabled_drive_types:
            removed_by_accounts += 1
            continue
        if dt_filter and str(dt) != dt_filter:
            removed_by_drive_type += 1
            continue
        name = item.get("taskname") if item.get("taskname") else item.get("content", "")
        item['taskname'] = name
        filtered.append(item)

    message = None
    msg_parts: list[str] = []
    if dt_filter:
        msg_parts.append(f"已限定网盘类型：{dt_filter}")
    elif removed_by_accounts:
        msg_parts.append(f"已按可用网盘账号过滤：{', '.join(sorted(enabled_drive_types))}")
    if msg_parts:
        message = "; ".join(msg_parts)

    if filter_invalid:
        invalid = list_invalid_shareurls(db, shareurls=[str(x.get("shareurl") or "").strip() for x in filtered])
        if invalid:
            filtered = [x for x in filtered if str(x.get("shareurl") or "").strip() not in invalid]
            extra = f"已过滤失效链接：{len(invalid)}"
            message = f"{message}; {extra}" if message else extra

    return (filtered, token_updated, message)
