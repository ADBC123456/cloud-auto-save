from unittest.mock import Mock, patch

from app.extensions.plugins.emby import Emby
from app.extensions.plugins.openlist import Openlist
from app.services.media_library import (
    apply_category_to_addition,
    build_suggestions,
    driver_to_drive_type,
    guess_kind,
    match_location,
    normalize_storage,
    pick_category,
    storage_root_path,
)


BAIDU = {"id": "1", "mount_path": "/百度", "driver": "BaiduNetdisk", "drive_type": "baidu", "root_path": "/"}
BAIDU_SUB = {"id": "2", "mount_path": "/百度/影视", "driver": "BaiduNetdisk", "drive_type": "baidu", "root_path": "/影视"}
QUARK_SUBDIR = {"id": "3", "mount_path": "/夸克", "driver": "Quark", "drive_type": "quark", "root_path": None}


def test_rclone_mount_location_maps_to_cloud_folder():
    match = match_location("/mnt/openlist/百度/TV/国产剧", [BAIDU])
    assert match["storage"]["id"] == "1"
    assert match["openlist_path"] == "/百度/TV/国产剧"
    assert match["cloud_root"] == "/TV/国产剧"


def test_strm_location_maps_to_cloud_folder():
    match = match_location("/media/百度/TV/动漫", [BAIDU])
    assert match["cloud_root"] == "/TV/动漫"


def test_windows_style_location_is_supported():
    match = match_location("D:\\openlist\\百度\\TV\\综艺", [BAIDU])
    assert match["cloud_root"] == "/TV/综艺"


def test_longest_mount_path_wins():
    match = match_location("/mnt/openlist/百度/影视/电影", [BAIDU, BAIDU_SUB])
    assert match["storage"]["id"] == "2"
    assert match["cloud_root"] == "/影视/电影"


def test_unmatched_location_returns_none():
    assert match_location("/data/local/movies", [BAIDU]) is None


def test_storage_with_id_root_needs_manual_cloud_root():
    match = match_location("/mnt/夸克/剧集", [QUARK_SUBDIR])
    assert match["storage"]["id"] == "3"
    assert match["cloud_root"] is None


def test_storage_root_path_parsing():
    assert storage_root_path('{"root_folder_path": "/TV"}') == "/TV"
    assert storage_root_path('{"root_folder_id": "0"}') == "/"
    assert storage_root_path('{"root_folder_id": "root"}') == "/"
    assert storage_root_path('{"root_folder_id": "-11"}') == "/"
    assert storage_root_path('{"root_folder_id": "c0ffee"}') is None
    assert storage_root_path("not json") == "/"


def test_driver_names_map_to_drive_types():
    assert driver_to_drive_type("BaiduNetdisk") == "baidu"
    assert driver_to_drive_type("115 Cloud") == "115"
    assert driver_to_drive_type("AliyundriveOpen") == "aliyun"
    assert driver_to_drive_type("189CloudPC") == "cloud189"
    assert driver_to_drive_type("ThunderBrowser") == "xunlei"
    assert driver_to_drive_type("123Pan") == "123pan"
    assert driver_to_drive_type("Local") is None


def test_normalize_storage_from_openlist_payload():
    storage = normalize_storage({"id": 7, "mount_path": "/百度/", "driver": "BaiduNetdisk",
                                 "addition": '{"root_folder_path":"/TV"}', "status": "work"})
    assert storage == {"id": "7", "mount_path": "/百度", "driver": "BaiduNetdisk", "drive_type": "baidu",
                       "root_path": "/TV", "status": "work", "disabled": False, "remark": ""}
    assert normalize_storage({"id": 8}) is None


def test_guess_kind():
    assert guess_kind("动漫", "tvshows") == "anime"
    assert guess_kind("综艺节目", "tvshows") == "variety"
    assert guess_kind("Movies", "movies") == "movie"
    assert guess_kind("国产剧", "") == "tv"
    assert guess_kind("Music", "music") == ""


def test_suggestions_fill_everything_when_matched():
    libraries = [
        {"id": "11", "name": "国产剧", "collection_type": "tvshows", "locations": ["/mnt/openlist/百度/TV/国产剧"]},
        {"id": "12", "name": "本地电影", "collection_type": "movies", "locations": ["/data/movies"]},
        {"id": "13", "name": "音乐", "collection_type": "music", "locations": ["/mnt/openlist/百度/music"]},
    ]
    out = build_suggestions(libraries, [BAIDU], existing=[{"emby_library_id": "11"}])
    assert [item["name"] for item in out] == ["国产剧", "本地电影"]
    first, second = out
    assert first["matched"] is True and first["already_configured"] is True
    assert first["cloud_root"] == "/TV/国产剧"
    assert first["openlist_storage_id"] == "1"
    assert first["openlist_mount_path"] == "/百度"
    assert first["openlist_root_dir"] == "/"
    assert first["drive_type"] == "baidu"
    assert first["emby_library_id"] == "11"
    assert first["kind"] == "tv"
    assert second["matched"] is False and second["cloud_root"] == ""
    assert second["emby_library_id"] == "12" and second["kind"] == "movie"


CATEGORIES = [
    {"id": 1, "name": "国产剧", "drive_type": "baidu", "cloud_root": "/TV/国产剧",
     "openlist_storage_id": "1", "openlist_mount_path": "/百度", "openlist_root_dir": "/", "emby_library_id": "11"},
    {"id": 2, "name": "剧集", "drive_type": "baidu", "cloud_root": "/TV",
     "openlist_storage_id": "1", "openlist_mount_path": "/百度", "openlist_root_dir": "/", "emby_library_id": "10"},
    {"id": 3, "name": "夸克剧", "drive_type": "quark", "cloud_root": "/TV/国产剧",
     "openlist_storage_id": "3", "openlist_mount_path": "/夸克", "openlist_root_dir": "/", "emby_library_id": "30"},
]


def test_pick_category_prefers_explicit_id():
    assert pick_category(CATEGORIES, savepath="/other/Show", category_id="2")["id"] == 2


def test_pick_category_uses_longest_root_and_drive_type():
    assert pick_category(CATEGORIES, savepath="/TV/国产剧/Show", drive_type="baidu")["id"] == 1
    assert pick_category(CATEGORIES, savepath="/TV/国产剧/Show", drive_type="quark")["id"] == 3
    assert pick_category(CATEGORIES, savepath="/TV/美剧/Show", drive_type="baidu")["id"] == 2


def test_pick_category_respects_path_segment_boundary():
    assert pick_category(CATEGORIES, savepath="/TV/国产剧2/Show", drive_type="baidu")["id"] == 2
    assert pick_category(CATEGORIES, savepath="/TVarchive/Show", drive_type="baidu") is None


def test_apply_category_fills_missing_plugin_params():
    addition = apply_category_to_addition({}, CATEGORIES[0])
    assert addition["openlist"] == {"enable": True, "refresh": True, "password": "",
                                    "driver": "1", "mount_path": "/百度", "root_dir": "/"}
    assert addition["emby"] == {"try_match": True, "media_id": "", "library_id": "11"}


def test_apply_category_never_overrides_manual_values():
    addition = {"openlist": {"enable": False, "driver": "9", "mount_path": "/手填", "root_dir": ""},
                "emby": {"try_match": False, "media_id": "0", "library_id": "99"}}
    out = apply_category_to_addition(addition, CATEGORIES[0])
    assert out["openlist"] == {"enable": False, "driver": "9", "mount_path": "/手填", "root_dir": "/"}
    assert out["emby"] == {"try_match": False, "media_id": "0", "library_id": "99"}


def test_category_params_drive_openlist_refresh_end_to_end():
    task = {"savepath": "/TV/国产剧/New Show", "addition": apply_category_to_addition({}, CATEGORIES[0])}
    plugin = Openlist()
    plugin._request_json = Mock(return_value={"code": 200, "data": {"content": []}})
    plugin.refresh_mount_cache = Mock()
    plugin.run(task)
    assert plugin._request_json.call_args.kwargs["json"]["path"] == "/百度/TV/国产剧/New Show"
    plugin.refresh_mount_cache.assert_called_once_with("/百度/TV/国产剧/New Show", "/百度")


def test_openlist_plugin_is_active_without_global_storage():
    with patch.object(Openlist, "_check_connection", return_value=True):
        plugin = Openlist(url="http://openlist:5244", token="t")
    assert plugin.is_active is True


def test_emby_task_library_overrides_global_library():
    with patch.object(Emby, "get_info", return_value=True):
        plugin = Emby(url="http://emby:8096", token="test", library_id="1738")
    plugin.search = Mock(return_value="")
    plugin.refresh = Mock(return_value=True)
    plugin.run({"taskname": "New Show", "addition": {"emby": {"try_match": True, "media_id": "", "library_id": "11"}}})
    plugin.refresh.assert_called_once_with("11")
