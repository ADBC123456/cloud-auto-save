import json
from unittest.mock import Mock

import pytest
import requests

from app.extensions.adapters.baidu_adapter import BaiduAdapter
from app.extensions.runtime.drama_executor import DramaTaskExecutor


@pytest.fixture
def adapter():
    client = BaiduAdapter(no_login=True)
    client._session = Mock()
    client._request = Mock()
    client._api_access_shared = Mock(return_value={"errno": 0})
    return client


def response(body=None, text=""):
    result = Mock(text=text)
    result.json.return_value = body
    result.cookies.get_dict.return_value = {}
    return result


def test_share_page_supports_multiline_and_json_string_delimiters(adapter):
    payload = {"shareid": 12, "uk": 34, "file_list": [{"server_filename": "name);.mp4", "fs_id": 56}]}
    adapter._request.return_value = response(text="locals.mset(\n" + json.dumps(payload, indent=2) + "\n);")
    assert adapter._api_shared_paths("https://pan.baidu.com/s/test") == payload


def test_share_parser_skips_unrelated_page_state(adapter):
    payload = {"file_list": []}
    adapter._request.return_value = response(text='locals.mset({"theme":"light"}); yunData.setData(' + json.dumps(payload) + ');')
    assert adapter._api_shared_paths("https://pan.baidu.com/s/test") == payload


@pytest.mark.parametrize("html", ["<html>verification required</html>", "locals.mset({not json});", "locals.mset({});"])
def test_unreadable_share_page_is_an_error_instead_of_empty_success(adapter, html):
    adapter._request.return_value = response(text=html)
    detail = adapter.get_detail("test", "test:1234", "/")
    assert detail["code"] != 0
    assert not detail["data"]["list"]


def test_share_page_http_error_is_not_swallowed(adapter):
    adapter._request.return_value = response(text="gateway error")
    adapter._request.return_value.raise_for_status.side_effect = requests.HTTPError("503")
    detail = adapter.get_detail("test", "test:1234", "/")
    assert detail["code"] != 0


def test_password_error_is_reported_as_failed_token():
    client = BaiduAdapter(no_login=True)
    client._session = Mock()
    client._request = Mock(return_value=response({"errno": 4}))
    token = client.get_stoken("test", "wrong")
    assert token["status"] == 400 and token["code"] == 4


@pytest.mark.parametrize("body", [{"errno": -9}, {"errno": 0}, {"errno": 0, "list": None}])
def test_directory_api_errors_and_missing_lists_are_not_empty_success(adapter, body):
    adapter._request.return_value = response(body)
    with pytest.raises(ValueError):
        adapter._api_list_shared_paths("/show", 12, 34)


def test_genuinely_empty_directory_remains_supported(adapter):
    adapter._request.return_value = response({"errno": 0, "list": []})
    assert adapter._api_list_shared_paths("/show", 12, 34)["list"] == []


def test_missing_selected_folder_does_not_fall_back_to_another_folder(adapter):
    adapter._api_shared_paths = Mock(return_value={"uk": 12, "shareid": 34, "file_list": [{"fs_id": 1, "isdir": 1, "path": "/other", "server_filename": "Other"}]})
    adapter._bfs_share_tree = Mock(return_value=("", []))
    adapter._api_list_shared_paths = Mock()
    detail = adapter.get_detail("test", "test:1234", "999")
    assert detail["code"] != 0
    adapter._api_list_shared_paths.assert_not_called()


def test_existing_executor_retries_failed_share_page_and_recovers(adapter):
    adapter._request.side_effect = [response(text="verification page"), response(text='locals.mset({"uk":12,"shareid":34,"file_list":[{"fs_id":56,"isdir":0,"path":"/show/E01.mp4","server_filename":"E01.mp4"}]});')]
    executor = DramaTaskExecutor(adapter=adapter, task_data={})
    from unittest.mock import patch
    with patch("app.extensions.runtime.retry_utils.time.sleep"):
        items = executor._fetch_share_items(pwd_id="test", stoken="test:1234", pdir_fid="/")
    assert len(items) == 1
    assert items[0]["fid"] == "56"
    assert adapter._request.call_count == 2
