import json
from datetime import datetime
from unittest.mock import Mock
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from treelib import Tree

import app.models
from app.db.base import Base
from app.models.drive_account import DriveAccount
from app.models.task import Task
from app.models.task_savepath_snapshot import TaskSavepathSnapshot
from app.schemas.task_browse import SharePreviewBatchItemOut, SharePreviewBatchLatestOut, SharePreviewBatchOut
from app.services import drama_share_autoupdate as updates
from app.services import resource_search as search
from app.services.unisearch import UniSearchClient


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def response(data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError()
    return result


def test_login_expiry_retries_once_and_updates_bearer_token():
    client = UniSearchClient(username="user", password="secret", token="expired")
    client.session = Mock()
    client.session.headers = {}
    client.session.post.side_effect = [
        response({}, 401), response({"access_token": "renewed"}),
        response({"resources": [{"title": "Example", "links": [{"url": "https://pan.quark.cn/s/abc"}]}]}),
    ]
    items = client.search("Example", cloud_type="quark")
    assert items[0]["source"] == "UniSearch"
    assert client.token == "renewed"
    assert client.session.headers["Authorization"] == "Bearer renewed"
    assert client.session.post.call_args_list[-1].kwargs["json"]["cloud_types"] == ["quark"]
    assert client.session.post.call_count == 3


def test_login_failure_does_not_retry_forever():
    client = UniSearchClient(username="user", password="wrong", token="expired")
    client.session = Mock()
    client.session.headers = {}
    client.session.post.side_effect = [response({}, 401), response({}, 401)]
    with pytest.raises(requests.HTTPError):
        client.search("Example")
    assert client.session.post.call_count == 2


def test_formats_direct_links_passwords_and_ignores_unusable_links():
    items = UniSearchClient.format_results({"resources": [{
        "title": "Example S01E10", "source": {"id": "test"}, "links": [
            {"url": "https://pan.quark.cn/s/abc?x=1", "password": "a&b"},
            {"url": "https://pan.quark.cn/s/abc?x=1", "password": "a&b"},
            {"url": "https://pan.quark.cn/s/bad", "resolution": {"status": "invalid"}},
            {"url": "https://pan.quark.cn/s/scan", "access_mode": "scan_transfer"},
            {"url": "javascript:alert(1)"},
        ],
    }]})
    assert len(items) == 1
    assert items[0]["shareurl"] == "https://pan.quark.cn/s/abc?x=1&pwd=a%26b"
    assert items[0]["taskname"] == "Example S01E10"


def test_save_preserves_password_and_never_returns_credentials(db, monkeypatch):
    login = Mock(return_value="private-token")
    monkeypatch.setattr(UniSearchClient, "login", login)
    search.update_source(db, "unisearch", {"enabled": True, "username": "user", "password": "private-password", "auto_replace": True})
    search.update_source(db, "unisearch", {"enabled": True, "password": ""})
    cfg = search._loads(search.ensure_default_sources(db)["unisearch"].config_json)
    assert cfg["password"] == "private-password"
    public = next(x for x in search.list_sources(db) if x["key"] == "unisearch")
    assert public["has_password"] and public["auto_replace"]
    assert public["password"] is None and public["token"] is None
    assert "private" not in json.dumps(public)


@pytest.fixture
def drama(db, monkeypatch):
    sources = search.ensure_default_sources(db)
    for row in sources.values():
        row.enabled = row.key == "unisearch"
    sources["unisearch"].config_json = json.dumps({"auto_replace": True})
    db.add(DriveAccount(name="quark-account", drive_type="quark", cookie="", enabled=True, runtime_status="active"))
    task = Task(task_uid="test-task", task_type="drama", taskname="Example", shareurl="https://pan.quark.cn/s/old", savepath="/Example", account_name="quark-account", tmdb_media_type="tv", tmdb_id=123, startfid="old-source-fid")
    db.add(task)
    db.flush()
    snapshot = TaskSavepathSnapshot(task_uid=task.task_uid, savepath=task.savepath, files_json="[]", saved_latest_season=1, saved_latest_episode=8)
    db.add(snapshot)
    db.flush()
    context = updates._TMDBContext(names=["Example"], detail={"last_episode_to_air": {"season_number": 1, "episode_number": 10}}, weekdays=[datetime.now(ZoneInfo("Asia/Shanghai")).isoweekday()])
    monkeypatch.setattr(updates, "_load_tmdb_context", lambda *_: context)
    return task, snapshot, context


def preview(url, episode=None, *, ok=True, season=1):
    latest = SharePreviewBatchLatestOut(season=season, episode=episode, size=100) if episode else None
    return SharePreviewBatchItemOut(shareurl=url, ok=ok, latest_video=latest)


def install_search(monkeypatch, candidates, previews, *, old_ok=True, old_episode=8):
    fetch = Mock(return_value=(candidates, False, None))
    monkeypatch.setattr(updates, "fetch_task_suggestions", fetch)

    def batch(_db, payload):
        assert payload.account_name == "quark-account"
        items = [preview(url, old_episode, ok=old_ok) if url.endswith("/old") else previews[url] for url in payload.shareurls]
        return SharePreviewBatchOut(items=items), False

    monkeypatch.setattr(updates, "preview_share_batch", batch)
    return fetch


@pytest.mark.parametrize("old_ok", [True, False])
def test_reuses_matching_and_preview_to_replace_stale_or_invalid_source(db, drama, monkeypatch, old_ok):
    task, _, _ = drama
    new = "https://pan.quark.cn/s/new"
    candidates = [
        {"taskname": "Example S01E10", "shareurl": new},
        {"taskname": "Other Show S01E10", "shareurl": "https://pan.quark.cn/s/wrong-show"},
        {"taskname": "Example S01E10", "shareurl": "https://pan.baidu.com/s/wrong-drive"},
    ]
    fetch = install_search(monkeypatch, candidates, {new: preview(new, 10)}, old_ok=old_ok)
    task.shareurl_ban = "invalid" if not old_ok else None
    result = updates.resolve_drama_shareurl_update(db, task, on_update_day=True)
    assert result["updated"] and result["episode"] == 10
    assert task.shareurl == new and task.shareurl_ban is None and task.startfid is None
    assert fetch.call_args.kwargs["drive_type"] == "quark"
    assert fetch.call_args.kwargs["source_keys"] == ("unisearch",)


@pytest.mark.parametrize("actual_episode,ok,season", [(8, True, 1), (7, True, 1), (None, True, 1), (10, False, 1), (10, True, 2)])
def test_never_trusts_advertised_episode_or_downgrades(db, drama, monkeypatch, actual_episode, ok, season):
    task, _, _ = drama
    new = "https://pan.quark.cn/s/new"
    install_search(monkeypatch, [{"taskname": "Example S01E10", "shareurl": new}], {new: preview(new, actual_episode, ok=ok, season=season)}, old_ok=False)
    result = updates.resolve_drama_shareurl_update(db, task, on_update_day=True)
    assert not result["updated"]
    assert task.shareurl.endswith("/old") and task.startfid == "old-source-fid"


def test_no_search_on_non_update_day(db, drama, monkeypatch):
    task, _, context = drama
    context.weekdays = [context.weekdays[0] % 7 + 1]
    fetch = Mock()
    monkeypatch.setattr(updates, "fetch_task_suggestions", fetch)
    result = updates.resolve_drama_shareurl_update(db, task, on_update_day=True)
    assert result["reason"] == "not_tmdb_update_day"
    fetch.assert_not_called()


def test_no_search_when_already_saved_latest_episode(db, drama, monkeypatch):
    task, snapshot, _ = drama
    snapshot.saved_latest_episode = 10
    fetch = install_search(monkeypatch, [], {}, old_ok=False)
    result = updates.resolve_drama_shareurl_update(db, task, on_update_day=True)
    assert result["reason"] == "up_to_date"
    fetch.assert_not_called()


def test_invalid_source_with_unknown_saved_progress_is_preserved(db, drama, monkeypatch):
    task, snapshot, _ = drama
    snapshot.saved_latest_episode = None
    fetch = install_search(monkeypatch, [], {}, old_ok=False)
    result = updates.resolve_drama_shareurl_update(db, task, on_update_day=True)
    assert result["reason"] == "progress_unknown"
    fetch.assert_not_called()


def test_existing_115_toggle_still_applies(db, drama):
    task, _, _ = drama
    result = updates.resolve_drama_shareurl_update(db, task)
    assert result["reason"] == "not_applicable"


@pytest.mark.parametrize("saved_episode", [None, 8, 10])
def test_failed_share_recovers_outside_update_day_and_once_runs(db, drama, monkeypatch, saved_episode):
    task, snapshot, context = drama
    context.weekdays = []
    snapshot.saved_latest_episode = saved_episode
    task.extra_json = json.dumps({"allow_once": True})
    new = "https://pan.quark.cn/s/new"
    install_search(monkeypatch, [{"taskname": "Example S01E10", "shareurl": new}], {new: preview(new, 10)}, old_ok=False)
    result = updates.resolve_drama_shareurl_update(db, task, on_share_failure=True)
    assert result["updated"] and task.shareurl == new and task.startfid is None


@pytest.mark.parametrize("episode,ok,season", [(7, True, 1), (None, True, 1), (10, False, 1), (10, True, 2), (11, True, 1)])
def test_failed_share_still_rejects_older_unreadable_and_wrong_season_candidates(db, drama, monkeypatch, episode, ok, season):
    task, _, _ = drama
    new = "https://pan.quark.cn/s/new"
    install_search(monkeypatch, [{"taskname": "Example S01E10", "shareurl": new}], {new: preview(new, episode, ok=ok, season=season)}, old_ok=False)
    result = updates.resolve_drama_shareurl_update(db, task, on_share_failure=True)
    assert not result["updated"]
    assert task.shareurl.endswith("/old")


def test_failed_share_recovery_respects_disabled_setting(db, drama, monkeypatch):
    task, _, _ = drama
    search.ensure_default_sources(db)["unisearch"].config_json = "{}"
    fetch = Mock()
    monkeypatch.setattr(updates, "fetch_task_suggestions", fetch)
    result = updates.resolve_drama_shareurl_update(db, task, on_share_failure=True)
    assert result["reason"] == "disabled"
    fetch.assert_not_called()


@pytest.mark.parametrize("title", [
    "我不是大师 全24集 [2026][4K]",
    "我不是大师 (2026) 4K 全24集 已完结",
    "我不是大师（2026）4K 60FPS S01 杜比音效",
    "我不是大师（2026）4K 10bit 60FPS 杜比环绕声 更至EP24",
])
def test_release_metadata_does_not_hide_the_matching_series(title):
    assert updates._title_matches_tmdb_names(title, ["我不是大师"])


@pytest.mark.parametrize("title", ["我不是大师兄 全24集 [2026][4K]", "大师 全24集 [2026][4K]"])
def test_release_metadata_matching_still_rejects_different_series(title):
    assert not updates._title_matches_tmdb_names(title, ["我不是大师"])


def test_latest_episode_wins_over_file_modification_time(monkeypatch):
    from app.services.share_preview_batch import _auto_resolve_latest_video
    monkeypatch.setattr("app.services.share_preview_batch.guessit_episode_numbers", lambda name, **kwargs: {
        "S01E23.mp4": (1, 23), "S01E24.mp4": (1, 24),
    }[name])
    adapter = Mock()
    adapter.get_detail.return_value = {"code": 0, "data": {"list": [
        {"fid": "23", "file_name": "S01E23.mp4", "updated_at": 1791111176},
        {"fid": "24", "file_name": "S01E24.mp4", "updated_at": 1791011176},
    ]}}
    _, latest = _auto_resolve_latest_video(adapter, "share", "token", "folder")
    assert latest["name"] == "S01E24.mp4"


def test_preview_rejects_api_errors_instead_of_reporting_empty_success():
    from app.services.share_preview_batch import _auto_resolve_latest_video
    adapter = Mock()
    adapter.get_detail.return_value = {"status": 400, "code": -7, "message": "分享文件夹等待审核中", "data": {"list": []}}
    with pytest.raises(RuntimeError, match="等待审核"):
        _auto_resolve_latest_video(adapter, "share", "token", "folder")


def test_movie_preview_keeps_modification_time_order(monkeypatch):
    from app.services.share_preview_batch import _auto_resolve_latest_video
    monkeypatch.setattr("app.services.share_preview_batch.guessit_episode_numbers", lambda *_args, **_kwargs: (None, None))
    adapter = Mock()
    adapter.get_detail.return_value = {"code": 0, "data": {"list": [
        {"fid": "old", "file_name": "Movie.mp4", "updated_at": 1791011176},
        {"fid": "new", "file_name": "Movie.mkv", "updated_at": 1791111176},
    ]}}
    _, latest = _auto_resolve_latest_video(adapter, "share", "token", "folder")
    assert latest["fid"] == "new"


def test_waiting_for_review_is_not_permanently_blacklisted():
    from app.services.share_preview_batch import _should_persist_invalid_share_link
    assert not _should_persist_invalid_share_link("get_detail status=400 errno=-7: 分享文件夹等待审核中")
    assert _should_persist_invalid_share_link("文件不存在或已被删除")


@pytest.mark.parametrize("second_failure", [False, True])
def test_share_failure_retries_new_source_once_within_same_execution(db, drama, monkeypatch, second_failure):
    from app.extensions.runtime.task_executor import TaskExecutor
    from app.extensions.runtime.execution_log import ExecutionLog
    import app.extensions.runtime.task_executor as runtime

    task, _, context = drama
    context.weekdays = []
    new = "https://pan.quark.cn/s/new"
    fetch = install_search(monkeypatch, [{"taskname": "Example S01E10", "shareurl": new}], {new: preview(new, 10)}, old_ok=False)
    log = ExecutionLog()
    tree = Tree()
    tree.create_node("Example", "root")
    attempts = []

    def execute(instance):
        attempts.append(instance.task_data["shareurl"])
        log.set_stage("fetch_share_items")
        if len(attempts) == 1 or second_failure:
            raise RuntimeError("get_detail status=400 errno=-7")
        instance.transfer_count = 1
        return tree

    monkeypatch.setattr(runtime.DramaTaskExecutor, "execute", execute)
    executor = TaskExecutor(db)
    data = executor._task_to_dict(task)
    if second_failure:
        with pytest.raises(RuntimeError, match="errno=-7"):
            executor._execute_drama_task(Mock(), data, log, task=task)
    else:
        result = executor._execute_drama_task(Mock(), data, log, task=task)
        assert result._transfer_count == 1
    assert attempts == ["https://pan.quark.cn/s/old", new]
    fetch.assert_called_once()
    assert "分享失败自动换源" in log.render()


def test_source_integration_filters_unavailable_drives(db, drama, monkeypatch):
    monkeypatch.setattr(UniSearchClient, "search", lambda *_args, **_kwargs: [
        {"taskname": "Example", "shareurl": "https://pan.quark.cn/s/new"},
        {"taskname": "Example", "shareurl": "https://pan.baidu.com/s/unavailable"},
    ])
    items, _, _ = search.fetch_task_suggestions(db, "Example", 1, source_keys=("unisearch",))
    assert [item["shareurl"] for item in items] == ["https://pan.quark.cn/s/new"]


def test_unisearch_uses_baidu_cloud_type_name(db, monkeypatch):
    from app.services import resource_search as service

    client = Mock()
    client.search.return_value = []
    client.token = ""
    monkeypatch.setattr(service, "UniSearchClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(service, "ensure_default_sources", lambda _db: {
        key: Mock(key=key, enabled=(key == "unisearch"), config_json=(json.dumps({"username": "u", "password": "p"}) if key == "unisearch" else "{}"))
        for key in service.SOURCE_KEYS
    })
    monkeypatch.setattr(service, "list_invalid_shareurls", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(service, "AdapterRegistry", Mock())
    service.fetch_task_suggestions(db, "\u7267\u795e\u8bb0", 1, drive_type="baidu", source_keys=("unisearch",))
    assert client.search.call_args.kwargs["cloud_type"] == "baidu"


def test_parallel_cloudsaver_and_unisearch_renewals_are_both_saved(db, monkeypatch):
    rows = search.ensure_default_sources(db)
    for row in rows.values():
        row.enabled = row.key in {"cloudsaver", "unisearch"}
        row.config_json = json.dumps({"server": "https://example.com", "username": "user", "password": "test", "token": "old"})
    cs = Mock()
    cs.auto_login_search.return_value = {"success": True, "new_token": "cloudsaver-renewed", "data": {}}
    cs.clean_search_results.return_value = []
    us = Mock(token="unisearch-renewed")
    us.search.return_value = []
    monkeypatch.setattr(search, "CloudSaverClient", lambda *_args: cs)
    monkeypatch.setattr(search, "UniSearchClient", lambda *_args, **_kwargs: us)
    _, changed, _ = search.fetch_task_suggestions(db, "Example", 1)
    assert changed
    assert json.loads(rows["cloudsaver"].config_json)["token"] == "cloudsaver-renewed"
    assert json.loads(rows["unisearch"].config_json)["token"] == "unisearch-renewed"


def test_unisearch_timestamp_keeps_existing_china_time_search_ordering():
    assert search._unisearch_to_cst("2026-10-04T00:30:00Z") == "2026-10-04 08:30:00"


def test_executor_transfers_repaired_source_in_same_run(db, drama, monkeypatch):
    from app.extensions.runtime.task_executor import TaskExecutor

    task, _, _ = drama
    task.shareurl_ban = "invalid"
    new = "https://pan.quark.cn/s/new"
    install_search(monkeypatch, [{"taskname": "Example S01E10", "shareurl": new}], {new: preview(new, 10)}, old_ok=False)
    adapter = SimpleNamespace(is_active=True, nickname="test", DRIVE_TYPE="quark")
    manager = Mock()
    manager.get_adapter_for_task.return_value = adapter
    manager.get_default_adapter.return_value = adapter
    executor = TaskExecutor(db)
    tree = Tree()
    tree.create_node("Example", "root")
    transfer = Mock(return_value=tree)
    monkeypatch.setattr(executor, "_execute_drama_task", transfer)
    execution = executor.run_task(task, account_manager=manager, defer_plugins=True, persist_execution=False)
    assert execution.status == "success"
    payload = transfer.call_args.args[1]
    assert payload["shareurl"] == new and payload["startfid"] is None
