"""定时自动搜索：到点在已配置的搜索源里找「时效内 + 网盘类型匹配」的分享链接，
验证有效后填入任务并清除封禁状态；候选按「最新更新 > 资源最大 > 集数最新」择优。
链接失效（shareurl_ban）或缺失时立即触发补链（auto_repair_banned_tasks）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.drive_account import DriveAccount
from app.models.task import Task
from app.schemas.task_browse import SharePreviewBatchIn
from app.services.resource_search import fetch_task_suggestions
from app.services.share_preview_batch import preview_share_batch

logger = logging.getLogger(__name__)

AUTO_SEARCH_DEFAULTS: dict[str, Any] = {
    "enable": False,
    "crontab": "0 */2 * * *",
    "keyword": "",
    "drive_type": "auto",
    "max_age_hours": 48,
}
MAX_VERIFY_CANDIDATES = 5


def get_extra(task: Task) -> dict[str, Any]:
    try:
        obj = json.loads(task.extra_json or "")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def set_extra(task: Task, extra: dict[str, Any]) -> None:
    task.extra_json = json.dumps(extra or {}, ensure_ascii=False)


def get_auto_search_conf(task: Task) -> dict[str, Any]:
    conf = get_extra(task).get("auto_search")
    if not isinstance(conf, dict):
        conf = {}
    merged = dict(AUTO_SEARCH_DEFAULTS)
    merged.update({k: v for k, v in conf.items() if v is not None})
    return merged


def is_auto_search_task(task: Task) -> bool:
    return bool(get_auto_search_conf(task).get("enable"))


def _resolve_drive_type(db: Session, task: Task, conf: dict[str, Any]) -> str | None:
    dt = str(conf.get("drive_type") or "auto").strip()
    if dt and dt != "auto":
        return dt
    account_name = str(task.account_name or "").strip()
    if account_name:
        row = db.execute(
            select(DriveAccount.drive_type).where(DriveAccount.name == account_name)
        ).scalar_one_or_none()
        if row:
            return str(row)
    return None


def _freshness_filter(
    results: list[dict[str, Any]], max_age_hours: int, now: datetime | None = None
) -> tuple[list[dict[str, Any]], int]:
    """按更新时效过滤；无法解析时间的结果在启用时效时丢弃"""
    if not max_age_hours or max_age_hours <= 0:
        return (results, 0)
    now = now or datetime.now()
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in results:
        try:
            dt = datetime.strptime(str(item.get("datetime") or ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            dropped += 1
            continue
        if dt and 0 <= (now - dt).total_seconds() <= max_age_hours * 3600:
            kept.append(item)
        else:
            dropped += 1
    return (kept, dropped)


def _parse_dt(value: Any) -> float:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def _update_status(task: Task, message: str, *, found: dict[str, Any] | None = None) -> None:
    extra = get_extra(task)
    status: dict[str, Any] = {
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
    }
    if found:
        status.update(
            {
                "found_at": status["last_run"],
                "found_url": found.get("shareurl") or "",
                "found_datetime": found.get("datetime") or "",
            }
        )
    extra["auto_search_status"] = status
    set_extra(task, extra)


def _pick_best(candidates: list[dict[str, Any]], preview_info: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """在验证通过的候选中择优：更新时间最新 > 资源最大 > 集数最新"""
    def score(c: dict[str, Any]):
        info = preview_info.get(str(c.get("shareurl"))) or {}
        size = float(info.get("size") or 0)
        episode = int(info.get("episode") or 0)
        return (_parse_dt(c.get("datetime")), size, episode)

    return sorted(candidates, key=score, reverse=True)[0]


def run_auto_search(db: Session, task: Task, *, force: bool = False, trigger: str = "manual") -> dict[str, Any]:
    """trigger: manual(手动/按钮) | scheduler(定时)"""
    conf = get_auto_search_conf(task)
    taskname = str(task.taskname or "")
    base = {"task_id": int(task.id), "taskname": taskname, "trigger": trigger}

    if not conf.get("enable") and not force:
        return {**base, "result": "skip", "message": "任务未开启自动搜索"}

    needs_search = (not task.shareurl) or bool(task.shareurl_ban)
    if not force and not needs_search and trigger != "manual":
        return {**base, "result": "skip", "message": "分享链接有效，无需搜索"}

    keyword = str(conf.get("keyword") or "").strip() or taskname
    if len(keyword) < 2:
        _update_status(task, "搜索关键词过短")
        db.commit()
        return {**base, "result": "error", "message": "搜索关键词过短"}

    drive_type = _resolve_drive_type(db, task, conf)
    try:
        max_age_hours = int(conf.get("max_age_hours") or 48)
    except Exception:
        max_age_hours = 48

    try:
        results, _, search_msg = fetch_task_suggestions(
            db, keyword, 1, filter_invalid=True, drive_type=drive_type
        )
    except Exception as e:
        logger.warning("自动搜索失败 [%s]: %s", taskname, e, exc_info=True)
        _update_status(task, f"搜索异常：{e}")
        db.commit()
        return {**base, "result": "error", "message": f"搜索异常：{e}"}

    fresh, dropped = _freshness_filter(results, max_age_hours)
    if not fresh:
        msg = (
            f"未找到 {max_age_hours} 小时内更新的资源"
            + (f"（{drive_type}）" if drive_type else "")
            + (f"，共 {len(results)} 条不满足条件" if results else "")
        )
        _update_status(task, msg)
        db.commit()
        return {**base, "result": "notfound", "message": msg, "total": len(results)}

    # 验证候选链接有效性（最多 5 个），全部通过的按 最新/最大 择优
    candidates = fresh[:MAX_VERIFY_CANDIDATES]
    verified: list[dict[str, Any]] = []
    preview_info: dict[str, dict[str, Any]] = {}
    try:
        batch_out, _ok = preview_share_batch(
            db, SharePreviewBatchIn(shareurls=[str(x.get("shareurl")) for x in candidates])
        )
        db.commit()
        for it in getattr(batch_out, "items", None) or []:
            url = str(getattr(it, "shareurl", "") or "")
            if not getattr(it, "ok", False):
                continue
            if url not in {str(c.get("shareurl")) for c in candidates}:
                continue
            lv = getattr(it, "latest_video", None)
            preview_info[url] = {
                "size": getattr(lv, "size", None) or 0,
                "episode": getattr(lv, "episode", None) or 0,
            }
        url_set = set(preview_info.keys())
        verified = [c for c in candidates if str(c.get("shareurl")) in url_set]
    except Exception as e:
        logger.warning("自动搜索验证失败 [%s]: %s", taskname, e, exc_info=True)

    if not verified:
        msg = f"找到 {len(fresh)} 条 {max_age_hours} 小时内更新的候选，但验证均不可用"
        _update_status(task, msg)
        db.commit()
        return {**base, "result": "notfound", "message": msg, "total": len(fresh)}

    hit = _pick_best(verified, preview_info)

    # 命中：填入链接、清封禁、记状态
    hit_url = str(hit.get("shareurl"))
    task.shareurl = hit_url
    task.shareurl_ban = None
    info = preview_info.get(hit_url) or {}
    msg = (
        f"已找到并填入链接（{hit.get('datetime') or '时间未知'} 更新"
        + (f"，单文件 {_fmt_size(info.get('size'))}" if info.get("size") else "")
        + ("）择优规则：最新更新 > 资源最大 > 集数最新" if len(verified) > 1 else "）")
    )
    _update_status(task, msg, found=hit)
    db.commit()

    try:
        from app.services.notifications.sender import send_runtime
        from app.services.notifications.task_notify import DRAMA_NOTIFY_TITLE

        send_runtime(
            db,
            DRAMA_NOTIFY_TITLE,
            f"🔍自动搜索《{taskname}》命中：\n{hit_url}\n更新时间：{hit.get('datetime') or '未知'}"
            + (f"\n来源：{hit.get('source', '')} {hit.get('channel', '')}".rstrip() if hit.get("source") else ""),
        )
    except Exception:
        logger.warning("自动搜索通知发送失败", exc_info=True)

    return {
        **base,
        "result": "found",
        "shareurl": hit_url,
        "datetime": hit.get("datetime") or "",
        "message": msg,
        "total": len(fresh),
        "candidates_verified": len(verified),
    }


def _parse_any_ts(value: Any):
    """容忍多种时间格式：epoch 秒、ISO 字符串、%Y-%m-%d %H:%M:%S"""
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "").isdigit()):
        try:
            ts = float(value)
            if ts > 10**12:  # 毫秒
                ts /= 1000.0
            if ts > 10**9:
                return datetime.fromtimestamp(ts)
        except Exception:
            return None
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00").replace("+00:00", "")[:19]) if text[:10].count("-") == 2 else None
    except Exception:
        return None


def current_share_last_update(db: Session, shareurl: str):
    """取当前分享链接内容的最近更新时间（最新文件 updated_at）；失败/未知返回 None"""
    try:
        out, _ok = preview_share_batch(db, SharePreviewBatchIn(shareurls=[str(shareurl)]))
        db.commit()
        for it in getattr(out, "items", None) or []:
            if not getattr(it, "ok", False):
                return None
            lv = getattr(it, "latest_video", None)
            ts = _parse_any_ts(getattr(lv, "updated_at", None)) if lv else None
            if ts:
                return ts
        return None
    except Exception:
        return None


def prefill_task_link(db: Session, task: Task, say=None) -> dict[str, Any] | None:
    """转存执行前预填：
    1) 链接缺失或已封禁 → 立即搜索补链；
    2) 链接有效但超过时效未更新 → 搜索更新的分享自动换链（找不到则保留现链接）。

    say: 可选的日志输出回调（流式执行时传入 log.line，日志实时进运行日志弹窗）。
    返回命中/未命中摘要；无需搜索返回 None。
    """
    if not is_auto_search_task(task):
        return None
    taskname = str(task.taskname or "")
    conf = get_auto_search_conf(task)

    if not task.shareurl or task.shareurl_ban:
        if say:
            say(f">>> 自动搜索预填：《{taskname}》分享链接缺失或已失效，开始全网搜索...")
        summary = run_auto_search(db, task, trigger="scheduler")
        if say:
            if summary.get("result") == "found":
                say(f">>> 自动搜索命中：《{taskname}》 {summary.get('shareurl', '')}（{summary.get('datetime', '')} 更新）")
            else:
                say(f">>> 自动搜索未命中：《{taskname}》 {summary.get('message', '')}")
        return summary

    # 链接有效：检查是否超过时效未更新
    try:
        max_age_hours = int(conf.get("max_age_hours") or 48)
    except Exception:
        max_age_hours = 48
    if max_age_hours <= 0:
        return None
    last_update = current_share_last_update(db, task.shareurl)
    if last_update is None:
        return None
    age_hours = (datetime.now() - last_update).total_seconds() / 3600
    if age_hours <= max_age_hours:
        return None

    if say:
        say(f">>> 链接时效检查：《{taskname}》当前分享已 {age_hours:.0f} 小时未更新（时效 {max_age_hours} 小时），搜索更新的分享...")
    summary = run_auto_search(db, task, force=True, trigger="scheduler")
    if summary.get("result") == "found":
        if say:
            say(f">>> 已换链：《{taskname}》 {summary.get('shareurl', '')}（{summary.get('datetime', '')} 更新）")
    else:
        # 未找到更新的：保留当前链接继续执行
        if say:
            say(f">>> 未找到时效内更新的分享，继续使用当前链接（{summary.get('message', '')}）")
    return summary


def _fmt_size(value: Any) -> str:
    try:
        size = float(value or 0)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f}{unit}"
        size /= 1024
    return ""


def _run_task_with_session(db: Session, task: Task) -> None:
    """转存一次并推送（使用调用方会话）"""
    try:
        from app.extensions.runtime.task_executor import TaskExecutor
        from app.services.notifications.sender import send_runtime
        from app.services.notifications.task_notify import DRAMA_NOTIFY_TITLE, build_task_section

        execution = TaskExecutor(db).run_task(task)
        db.commit()
        try:
            section, should_notify = build_task_section(task, execution)
            if should_notify:
                send_runtime(db, DRAMA_NOTIFY_TITLE, section)
        except Exception:
            pass
    except Exception:
        logger.warning("自动搜索后的转存执行失败 task_id=%s", task.id, exc_info=True)


def run_task_after_found(db_session_factory, task_id: int) -> None:
    """命中后立即转存（独立会话，供后台线程调用）"""
    try:
        with db_session_factory() as db:
            task = db.get(Task, task_id)
            if not task or not task.enabled:
                return
            _run_task_with_session(db, task)
    except Exception:
        logger.warning("自动搜索后的转存执行失败 task_id=%s", task_id, exc_info=True)


def _link_unhealthy(db: Session, shareurl: str) -> bool:
    """明确验证失败的链接（坏链/被删除）；网络等未知异常不算，避免误伤"""
    try:
        out, _ok = preview_share_batch(db, SharePreviewBatchIn(shareurls=[str(shareurl)]))
        db.commit()
        for it in getattr(out, "items", None) or []:
            return not bool(getattr(it, "ok", False))
    except Exception:
        return False
    return False


def auto_repair_banned_tasks(db: Session) -> list[dict[str, Any]]:
    """链接失效或缺失且开启自动搜索的任务：立即重搜补链，命中则马上转存一次"""
    summaries: list[dict[str, Any]] = []
    try:
        tasks = db.execute(select(Task).where(Task.enabled.is_(True))).scalars().all()
    except Exception:
        logger.warning("自动补链读取任务失败", exc_info=True)
        return summaries
    for task in tasks:
        if not is_auto_search_task(task):
            continue
        if task.shareurl and not task.shareurl_ban:
            # 链接体检：明确验证失败（坏链/已删除）时进入换链流程
            if not _link_unhealthy(db, task.shareurl):
                continue
            logger.info("[自动补链巡检] %s 链接体检失败，尝试换链", task.taskname)
        old_url = str(task.shareurl or "")
        summary = run_auto_search(db, task, trigger="scheduler")
        logger.info(
            "[自动补链] %s -> %s (%s)", task.taskname, summary.get("result"), summary.get("message", "")
        )
        summaries.append(summary)
        if summary.get("result") == "found" and summary.get("shareurl") and summary["shareurl"] != old_url:
            _run_task_with_session(db, task)
    return summaries


def repair_task_async(task_id: int) -> None:
    """后台线程：对单个任务立即补链并转存（用于手动执行失败后）"""
    import threading

    from app.db.session import SessionLocal

    def _worker():
        try:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if not task or not task.enabled:
                    return
                if not (task.shareurl and not task.shareurl_ban) and is_auto_search_task(task):
                    summary = run_auto_search(db, task, trigger="scheduler")
                    if summary.get("result") == "found":
                        _run_task_with_session(db, task)
        except Exception:
            logger.warning("后台自动补链失败 task_id=%s", task_id, exc_info=True)

    threading.Thread(target=_worker, daemon=True).start()
