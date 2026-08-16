from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user, require_permissions
from app.core.permissions import TASK_RUN
from app.db.session import get_db
from app.extensions.runtime.auto_search_scheduler import auto_search_scheduler_manager
from app.models.task import Task
from app.services import audit
from app.services.auto_search import run_auto_search
from app.services.tasks import get_task

router = APIRouter()


class AutoSearchRunIn(BaseModel):
    task_id: int | None = Field(default=None)
    force: bool = Field(default=False)


@router.post('/run', dependencies=[Depends(require_permissions(TASK_RUN))])
def post_auto_search_run(
    request: Request,
    payload: AutoSearchRunIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """手动触发自动搜索：指定 task_id 搜索单个任务；否则搜索全部开启自动搜索且需要链接的任务"""
    summaries: list[dict] = []

    if payload.task_id:
        task = get_task(db, payload.task_id)
        summaries.append(run_auto_search(db, task, force=payload.force, trigger="manual"))
    else:
        tasks = db.execute(select(Task).where(Task.enabled.is_(True))).scalars().all()
        for task in tasks:
            summaries.append(run_auto_search(db, task, force=payload.force, trigger="manual"))

    found = [s for s in summaries if s.get("result") == "found"]
    audit.write_audit_log(
        db,
        actor_user_id=current.user.id,
        action="task.auto_search.run",
        target_type="task",
        target_id=str(payload.task_id or "all"),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        success=bool(summaries),
        detail=f"found={len(found)}/{len(summaries)}",
    )
    db.commit()

    # 任务链接可能变化，重载调度
    threading.Thread(target=auto_search_scheduler_manager.reload_from_db, daemon=True).start()
    return {"ok": True, "total": len(summaries), "found": len(found), "items": summaries}
