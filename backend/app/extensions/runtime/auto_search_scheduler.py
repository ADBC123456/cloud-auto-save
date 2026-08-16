"""自动搜索调度管理器：为每个开启自动搜索的任务注册独立 cron job（job id: auto_search_task_{task_uid}）"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.task import Task
from app.services.auto_search import get_auto_search_conf, run_auto_search, run_task_after_found

logger = logging.getLogger(__name__)


class AutoSearchSchedulerManager:
    JOB_PREFIX = "auto_search_task_"
    SWEEP_JOB_ID = "auto_search_repair_sweep"
    SWEEP_INTERVAL_MINUTES = 15

    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    def start(self) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        # 兜底巡检：定期扫描失效/缺链任务并自动补链（不依赖每任务 cron）
        self._scheduler.add_job(
            self._sweep,
            trigger="interval",
            minutes=self.SWEEP_INTERVAL_MINUTES,
            id=self.SWEEP_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            replace_existing=True,
        )
        try:
            with SessionLocal() as db:
                self.reload_tasks(db)
        except Exception:
            logger.warning("自动搜索调度器启动加载任务失败", exc_info=True)

    def _sweep(self) -> None:
        try:
            from app.services.auto_search import auto_repair_banned_tasks

            with SessionLocal() as db:
                summaries = auto_repair_banned_tasks(db)
                db.commit()
            if summaries:
                logger.info("[自动补链巡检] 已处理 %d 个失效/缺链任务", len(summaries))
        except Exception:
            logger.warning("自动补链巡检失败", exc_info=True)

    def shutdown(self) -> None:
        if not self._started:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        self._started = False

    def reload_tasks(self, db: Session) -> int:
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith(self.JOB_PREFIX):
                try:
                    self._scheduler.remove_job(job.id)
                except Exception:
                    pass

        loaded = 0
        try:
            tasks = db.execute(select(Task).where(Task.enabled.is_(True))).scalars().all()
        except Exception:
            logger.warning("自动搜索调度器读取任务失败", exc_info=True)
            return 0

        for task in tasks:
            conf = get_auto_search_conf(task)
            if not conf.get("enable"):
                continue
            task_uid = str(task.task_uid or "")
            crontab = str(conf.get("crontab") or "").strip()
            if not task_uid or not crontab:
                continue
            try:
                trigger = CronTrigger.from_crontab(crontab)
            except Exception as e:
                logger.warning("任务 [%s] 自动搜索 cron 无效：%s (%s)", task.taskname, crontab, e)
                continue
            self._scheduler.add_job(
                self._execute,
                trigger=trigger,
                args=[int(task.id), task_uid],
                id=f"{self.JOB_PREFIX}{task_uid}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
                replace_existing=True,
            )
            loaded += 1
            logger.info("已调度自动搜索任务: %s (%s)", task.taskname, crontab)

        logger.info("自动搜索调度器已加载 %d 个任务", loaded)
        return loaded

    def reload_from_db(self) -> None:
        try:
            with SessionLocal() as db:
                self.reload_tasks(db)
        except Exception:
            logger.warning("自动搜索调度器重载失败", exc_info=True)

    def _execute(self, task_id: int, task_uid: str) -> None:
        try:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if not task or not task.enabled or str(task.task_uid or "") != task_uid:
                    return
                summary = run_auto_search(db, task, trigger="scheduler")
                logger.info(
                    "[自动搜索] %s -> %s (%s)",
                    task.taskname,
                    summary.get("result"),
                    summary.get("message", ""),
                )
                found = summary.get("result") == "found"
            if found:
                run_task_after_found(SessionLocal, task_id)
        except Exception:
            logger.warning("自动搜索任务执行失败 task_id=%s", task_id, exc_info=True)


auto_search_scheduler_manager = AutoSearchSchedulerManager()
