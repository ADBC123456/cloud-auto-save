from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from treelib import Tree

from app.extensions.plugins.alist_strm_gen import Alist_strm_gen
from app.extensions.plugins.emby import Emby
from app.extensions.plugins.openlist import Openlist


def generator(root="/TV"):
    plugin = Alist_strm_gen()
    plugin.client = Mock()
    plugin.storage_mount_path = "/TV"
    plugin.root_dir = root
    return plugin


def test_new_tasks_enable_configured_openlist_refresh():
    assert Openlist.default_task_config["enable"] is True


def test_missing_task_directory_never_generates_entire_storage():
    plugin = generator()
    plugin._path_exists = Mock(side_effect=lambda path: path == "/TV")
    assert plugin._map_task_savepath("/TV/series/New Show") is None
    plugin._path_exists.assert_called_once_with("/TV/series/New Show")
    assert plugin.root_dir == "/TV"


def test_configured_root_is_a_directory_boundary():
    plugin = generator()
    plugin._path_exists = Mock(return_value=True)
    assert plugin._map_task_savepath("/TVarchive/New Show") is None
    plugin._path_exists.assert_not_called()


def test_valid_nested_series_keeps_its_exact_directory():
    plugin = generator()
    plugin._path_exists = Mock(return_value=True)
    assert plugin._map_task_savepath("/TV/series/New Show") == "/TV/series/New Show"


def test_unknown_root_does_not_fall_back_to_the_storage_root():
    plugin = generator("")
    plugin._path_exists = Mock(side_effect=lambda path: path == "/TV")
    assert plugin._map_task_savepath("/remote/series/New Show") is None


def emby():
    with patch.object(Emby, "get_info", return_value=True):
        plugin = Emby(url="http://emby:8096", token="test", library_id="1738")
    plugin.search = Mock(return_value="")
    plugin.refresh = Mock(return_value=True)
    return plugin


def test_new_series_refreshes_configured_library_before_it_is_indexed():
    plugin = emby()
    plugin.run({"taskname": "New Show", "addition": {"emby": {"try_match": True, "media_id": ""}}})
    plugin.refresh.assert_called_once_with("1738")


def test_explicitly_disabled_emby_task_is_respected():
    plugin = emby()
    plugin.run({"taskname": "New Show", "addition": {"emby": {"try_match": True, "media_id": "0"}}})
    plugin.refresh.assert_not_called()


def test_existing_series_uses_its_specific_refresh_target():
    plugin = emby()
    plugin.search.return_value = "series-5"
    task = {"taskname": "Existing Show", "addition": {"emby": {"try_match": True, "media_id": ""}}}
    plugin.run(task)
    plugin.refresh.assert_called_once_with("series-5")
    assert task["addition"]["emby"]["media_id"] == "series-5"


def test_openlist_refreshes_rclone_parent_before_emby_scan():
    plugin = Openlist()
    plugin.rclone_rc_url = "http://host:5572"
    plugin.rclone_rc_username = "integration"
    plugin.rclone_rc_password = "test-password"
    with patch("app.extensions.plugins.openlist.requests.post") as post:
        plugin.refresh_mount_cache("/TV/series/New Show", "/TV")
    assert [call.args[0] for call in post.call_args_list] == ["http://host:5572/vfs/forget", "http://host:5572/vfs/refresh"]
    assert post.call_args_list[0].kwargs["json"] == {"dir": "series"}
    assert post.call_args_list[1].kwargs["json"] == {"dir": "series", "recursive": "false"}
    assert post.call_args.kwargs["auth"] == ("integration", "test-password")


def test_unconfigured_rclone_does_not_make_extra_requests():
    plugin = Openlist()
    with patch("app.extensions.plugins.openlist.requests.post") as post:
        plugin.refresh_mount_cache("/TV/series/New Show", "/TV")
    post.assert_not_called()


def test_task_refresh_uses_absolute_openlist_path_without_double_mount():
    plugin = Openlist()
    plugin.register_driver("4", mount_path="/TV", root_dir="/TV", password="directory-password")
    plugin._request_json = Mock(return_value={"code": 200, "data": {"content": []}})
    plugin.refresh_mount_cache = Mock()
    plugin.run({"savepath": "/TV/anime/New Show", "addition": {"openlist": {
        "enable": True, "driver": "4", "mount_path": "/TV", "root_dir": "/TV", "refresh": True,
    }}})
    assert plugin._request_json.call_args.kwargs["json"]["path"] == "/TV/anime/New Show"
    assert plugin._request_json.call_args.kwargs["json"]["password"] == "directory-password"
    plugin.refresh_mount_cache.assert_called_once_with("/TV/anime/New Show", "/TV")


def test_task_refresh_respects_cloud_root_directory_boundary():
    plugin = Openlist()
    plugin.root_dir = "/TV"
    plugin.storage_mount_path = "/TV"
    plugin.refresh_dir = Mock()
    plugin.run({"savepath": "/TVarchive/New Show"})
    plugin.refresh_dir.assert_not_called()


@pytest.mark.parametrize("transfer_count,defer_plugins,failed,expected", [
    (0, False, False, ["openlist", "emby"]),
    (1, False, False, ["emby", "other", "openlist"]),
    (0, True, False, []),
    (0, False, True, []),
])
def test_manual_no_new_files_only_runs_refresh_plugins(transfer_count, defer_plugins, failed, expected):
    from app.extensions.runtime.task_executor import TaskExecutor
    from app.models.task import Task

    task = Task(id=0, task_uid="test", task_type="drama", taskname="Show", enabled=True,
                shareurl="test", savepath="/TV/anime/Show", extra_json="{}", addition_json="{}")
    adapter = Mock(is_active=True, nickname="test", DRIVE_TYPE="baidu")
    manager = Mock()
    manager.get_adapter_for_task.return_value = adapter
    manager.get_default_adapter.return_value = adapter
    tree = Tree()
    tree.create_node("Show", "root")
    tree._transfer_count = transfer_count
    calls = []
    plugins = []
    for key in ["emby", "other", "openlist"]:
        plugin = Mock(is_active=True)
        plugin.run.side_effect = lambda data, key=key, **kwargs: calls.append(key) or data
        plugin.task_after.return_value = {}
        plugins.append({"definition": SimpleNamespace(plugin_key=key), "instance": plugin,
                        "config": SimpleNamespace(enabled=True, runtime_status="active")})
    executor = TaskExecutor(Mock())
    with patch("app.extensions.runtime.task_executor.SessionLocal"), \
         patch("app.extensions.runtime.task_executor.sync_plugin_definitions"), \
         patch("app.services.magic_regex.get_enabled_magic_regex_map", return_value={}), \
         patch("app.services.tmdb_settings.get_or_create_tmdb_setting", side_effect=RuntimeError("unused")), \
         patch("app.extensions.runtime.task_executor.resolve_drama_shareurl_update", return_value={}), \
         patch("app.extensions.runtime.task_executor.is_115_auto_update_task", return_value=False), \
         patch("app.extensions.runtime.task_executor.PluginRegistry") as registry, \
         patch.object(executor, "_execute_drama_task", return_value=tree) as execute:
        registry.return_value.load_active_plugins.return_value = plugins
        if failed:
            execute.side_effect = RuntimeError("share read failed")
        result = executor.run_task(task, account_manager=manager, persist_execution=False, defer_plugins=defer_plugins)
    assert result.status == ("failed" if failed else "success")
    assert calls == expected
    if transfer_count == 0:
        for item in plugins:
            item["instance"].task_after.assert_not_called()
