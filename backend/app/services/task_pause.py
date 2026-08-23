from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def compute_next_resume_utc_naive(weekdays: object = None) -> datetime:
    """下一个恢复时刻(Asia/Shanghai 00:00),以 naive UTC 返回。

    配置了运行星期的任务:恢复到最早运行日的下一次出现(今天恰为最早运行日则 +7 天),
    即"从任务的开始天"起算的下一轮;未配置则回退到下周一。
    """
    now_sh = datetime.now(SHANGHAI_TZ)
    week = set()
    try:
        for x in weekdays or []:
            try:
                d = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= d <= 7:
                week.add(d)
    except TypeError:
        week = set()
    if week:
        target_iso = min(week)
    else:
        target_iso = 1  # 周一
    days_ahead = (target_iso - now_sh.isoweekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    target_sh = (now_sh + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    return target_sh.astimezone(timezone.utc).replace(tzinfo=None)


def get_task_run_weekdays(db, task) -> set[int]:
    """读取任务的有效运行星期(isoweekday 1-7):auto 模式取 TMDB 播出星期,否则取手动配置。"""
    import json

    extra: dict = {}
    try:
        raw = getattr(task, "extra_json", None)
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                extra = parsed
    except Exception:
        extra = {}

    mode = str(extra.get("runweek_mode") or "manual").strip().lower()
    if mode == "auto" and int(getattr(task, "tmdb_id", 0) or 0) > 0 and str(getattr(task, "tmdb_media_type", "") or "").strip().lower() == "tv":
        try:
            from app.services.tmdb_cache import get_tmdb_detail_cached

            configured, _detail, update_weekdays, episode_weekdays, _row = get_tmdb_detail_cached(db, media_type="tv", tmdb_id=int(task.tmdb_id))
            if configured:
                days = episode_weekdays or update_weekdays or []
                week = set()
                for x in days:
                    try:
                        d = int(x)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= d <= 7:
                        week.add(d)
                if week:
                    return week
        except Exception:
            pass

    week = set()
    for x in extra.get("runweek") or []:
        try:
            d = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= d <= 7:
            week.add(d)
    return week


def is_paused_value(value: object) -> bool:
    """paused_until 是否仍在休眠期。兼容 naive/aware 两种读取结果。"""
    if not value:
        return False
    v = value
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return False
    if not isinstance(v, datetime):
        return False
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v > datetime.now(timezone.utc).replace(tzinfo=None)


def is_task_week_paused(task: object) -> bool:
    return is_paused_value(getattr(task, "paused_until", None))


def paused_until_shanghai(value: object) -> datetime | None:
    """把存储的 naive UTC 时间转成带 +08:00 的上海时间,供 API 序列化。"""
    if not value or isinstance(value, str):
        return None
    v = value
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(SHANGHAI_TZ)
