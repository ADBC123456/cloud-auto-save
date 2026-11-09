import json
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  注册全部模型
from app.core.errors import ApiError
from app.db.base import Base
from app.extensions.runtime.task_executor import TaskExecutor
from app.models.task_execution import TaskExecution
from app.schemas.task import TaskCreateIn
from app.services.tasks import create_task


AUTO_SEARCH_ON = {"auto_search": {"enable": True, "crontab": "0 */2 * * *"}}


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_create_schema_accepts_empty_or_missing_shareurl():
    assert TaskCreateIn(taskname="繁花", shareurl="", savepath="/TV/繁花").shareurl == ""
    assert TaskCreateIn(taskname="繁花", savepath="/TV/繁花").shareurl == ""


def test_auto_search_task_can_be_created_without_link(db):
    task = create_task(db, task_type="drama", taskname="繁花", shareurl="", savepath="/TV/繁花", extra=AUTO_SEARCH_ON)
    assert task.id and task.shareurl == ""
    assert json.loads(task.extra_json)["auto_search"]["enable"] is True


def test_link_still_required_without_auto_search(db):
    with pytest.raises(ApiError) as err:
        create_task(db, task_type="drama", taskname="繁花", shareurl="", savepath="/TV/繁花", extra={})
    assert err.value.code == "TASK_SHAREURL_REQUIRED"


def test_invalid_link_is_still_rejected(db):
    with pytest.raises(ApiError) as err:
        create_task(db, task_type="drama", taskname="繁花", shareurl="not-a-share-link", savepath="/TV/繁花", extra=AUTO_SEARCH_ON)
    assert err.value.code == "TASK_SHAREURL_INVALID"


def test_running_task_without_link_is_skipped_not_failed_or_banned(db):
    task = create_task(db, task_type="drama", taskname="繁花", shareurl="", savepath="/TV/繁花", extra=AUTO_SEARCH_ON)
    manager = Mock()
    manager.get_adapter_for_task.return_value = Mock(is_active=True, nickname="acc", DRIVE_TYPE="baidu")
    execution = TaskExecutor(db).run_task(task, account_manager=manager, defer_plugins=True)
    assert isinstance(execution, TaskExecution)
    assert execution.status == "skipped"
    assert execution.message == "分享链接为空，等待自动搜索填入"
    assert "跳过: 分享链接为空" in execution.run_log
    assert not task.shareurl_ban
