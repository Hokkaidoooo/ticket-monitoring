"""雷达会话标志传播、App 能力边界与低价浏览器调用契约。"""
import copy
import importlib
import inspect
import logging
from dataclasses import replace
from datetime import date, timedelta

import pytest

from core import special_radar as radar
from core.query_sessions import AnonymousSessionUnsupportedError
from crawlers import base, qunar

_LOG = logging.getLogger("radar-query-sessions")
_APP_CLASSES = {
    "app_qunar": "AppQunarCrawler",
    "app_ctrip": "AppCtripCrawler",
    "app_fliggy": "AppFliggyCrawler",
    "app_tongcheng": "AppTongchengCrawler",
    "app_zhixing": "AppZhixingCrawler",
}


def _body(channels):
    day = (date.today() + timedelta(days=1)).isoformat()
    return {"user": "synthetic-radar", "from": "CKG", "to": "BJS",
            "start": day, "end": day, "channels": list(channels)}


@pytest.fixture(autouse=True)
def isolated_radar(monkeypatch):
    for name in ("_QUNAR_CACHE", "_FLIGGY_CACHE", "_TUNIU_CACHE", "_TONGCHENG_CACHE"):
        monkeypatch.setattr(radar, name, {})
    monkeypatch.setattr(radar, "_QUNAR_COOLDOWN_UNTIL", 0.0)
    monkeypatch.setattr(radar, "_FLIGGY_COOLDOWN_UNTIL", 0.0)
    monkeypatch.setattr(radar, "_gate_path", lambda *args, **kwargs: "")
    monkeypatch.setattr(radar, "_budget_s", lambda platform: 0.0)
    monkeypatch.setattr(radar, "_on", lambda *args: True)


def test_radar_and_calendar_keep_explicit_account_default():
    assert inspect.signature(radar.build_radar).parameters["anonymous_query"].default is False
    assert inspect.signature(qunar.QunarCrawler.fetch_low_price_calendar).parameters[
        "anonymous_query"].default is False


@pytest.mark.parametrize("anonymous", [True, False])
def test_radar_passes_selected_role_to_browser_calendar(tmp_path, monkeypatch, anonymous):
    calls = []
    body = _body(["qunar"])
    original = copy.deepcopy(body)

    def calendar(*args, **kwargs):
        calls.append(kwargs)
        return {"bstatus": 0, "lowPriceList": [{"depDate": body["start"], "price": 500}]}

    monkeypatch.setattr(qunar.QunarCrawler, "fetch_low_price_calendar", staticmethod(calendar))
    root = str(tmp_path / "selected-role")
    result = radar.build_radar(body, _LOG, proxy="http://127.0.0.1:7897",
                               user_data_dir=root, anonymous_query=anonymous)
    assert calls == [{"timeout": 22.0, "proxy": "http://127.0.0.1:7897",
                      "profile_root": root, "anonymous_query": anonymous}]
    assert result["channels"]["qunar"]["status"] == "ok"
    assert result["lowest"]["quote"]["payable_total"] is None
    assert body == original and "crawler_config" not in result["query"]


def test_account_and_anonymous_calendar_do_not_share_cache(tmp_path, monkeypatch):
    calls = []
    body = _body(["qunar"])

    def calendar(*args, **kwargs):
        calls.append(kwargs["anonymous_query"])
        return {"bstatus": 0, "lowPriceList": [{"depDate": body["start"], "price": 500}]}

    monkeypatch.setattr(qunar.QunarCrawler, "fetch_low_price_calendar", staticmethod(calendar))
    root = str(tmp_path / "same-root")
    for anonymous in (False, True, True):
        radar.build_radar(body, _LOG, user_data_dir=root, anonymous_query=anonymous)
    assert calls == [False, True]


@pytest.mark.parametrize("invalid", ["false", None, 0, []])
def test_radar_rejects_invalid_anonymous_flag_before_sources(monkeypatch, invalid):
    def forbidden(*args, **kwargs):
        pytest.fail("非法会话标志不得调用数据源")

    monkeypatch.setattr(qunar.QunarCrawler, "fetch_low_price_calendar", staticmethod(forbidden))
    with pytest.raises(ValueError, match="anonymous_query.*布尔"):
        radar.build_radar(_body(["qunar"]), _LOG, anonymous_query=invalid)


def test_anonymous_apps_are_unsupported_while_browser_result_is_preserved(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("匿名 App 不得构造设备或快照采集器")

    for platform, class_name in _APP_CLASSES.items():
        module = importlib.import_module("crawlers." + platform)
        monkeypatch.setattr(module, class_name, forbidden)
    body = _body(["qunar", *_APP_CLASSES])
    monkeypatch.setattr(qunar.QunarCrawler, "fetch_low_price_calendar", staticmethod(
        lambda *args, **kwargs: {"bstatus": 0, "lowPriceList": [
            {"depDate": body["start"], "price": 500}]}))
    result = radar.build_radar(body, _LOG, user_data_dir=str(tmp_path), anonymous_query=True)
    for platform in _APP_CLASSES:
        status = result["channels"][platform]
        assert status["status"] == "unsupported"
        assert status["rows"] == 0 and status["days"] == 0
        assert "不支持匿名查询" in status["reason"]
    assert result["channels"]["qunar"]["rows"] == 1
    assert result["lowest_platform"] == "qunar"


@pytest.mark.parametrize("platform,class_name", _APP_CLASSES.items())
def test_direct_anonymous_app_helper_rejects_before_constructor(tmp_path, monkeypatch,
                                                               platform, class_name):
    module = importlib.import_module("crawlers." + platform)

    def forbidden(*args, **kwargs):
        pytest.fail("匿名 App 不得构造设备或快照采集器")

    monkeypatch.setattr(module, class_name, forbidden)
    query = replace(radar.parse_query(_body([platform])), crawler_config={
        "anonymous_query": True, "user_data_dir": str(tmp_path),
    })
    with pytest.raises(AnonymousSessionUnsupportedError):
        getattr(radar, "_" + platform + "_deals")(query, _LOG)


def test_all_account_app_constructors_receive_same_role_root(tmp_path, monkeypatch):
    calls = {}
    body = _body(_APP_CLASSES)

    def fake_class(platform):
        class _Crawler:
            def __init__(self, cfg, logger):
                self.paths = []
                calls[platform] = copy.deepcopy(cfg)

            def low_fare(self, *args, **kwargs):
                return [{"date": body["start"], "price": 500}]
        return _Crawler

    for platform, class_name in _APP_CLASSES.items():
        module = importlib.import_module("crawlers." + platform)
        monkeypatch.setattr(module, class_name, fake_class(platform))
    root = str(tmp_path / "account")
    result = radar.build_radar(body, _LOG, user_data_dir=root)
    assert set(calls) == set(_APP_CLASSES)
    for platform, cfg in calls.items():
        assert cfg == {"user_data_dir": root, "proxy": "", "anonymous_query": False,
                       "android": {"enabled": True}}
        assert result["channels"][platform]["rows"] == 1


@pytest.mark.parametrize("anonymous", [True, False])
def test_public_http_sources_keep_existing_arguments_without_cookies(monkeypatch, anonymous):
    calls = {}
    responses = {"ctrip": None, "fliggy": [], "tuniu": [], "tongcheng": []}

    def fetcher(platform):
        def fetch(*args, **kwargs):
            calls[platform] = kwargs
            return responses[platform]
        return fetch

    for platform in responses:
        monkeypatch.setattr(radar, "fetch_" + platform + "_calendar", fetcher(platform))
    radar.build_radar(_body(responses), _LOG, proxy="http://127.0.0.1:7897",
                      anonymous_query=anonymous)
    assert calls == {platform: {"proxy": "http://127.0.0.1:7897"} for platform in responses}


@pytest.mark.parametrize("anonymous", [True, False])
def test_radar_session_change_preserves_tax_and_unconfirmed_final_price(monkeypatch, anonymous):
    body = _body(["fliggy", "tuniu"])
    monkeypatch.setattr(radar, "fetch_fliggy_calendar", lambda *args, **kwargs: [
        {"leaveDate": body["start"], "price": 800, "tax": 462}])
    monkeypatch.setattr(radar, "fetch_tuniu_calendar", lambda *args, **kwargs: [
        {"date": body["start"], "price": 450}])
    result = radar.build_radar(body, _LOG, anonymous_query=anonymous)
    channels = result["dates"][0]["by_channel"]
    assert channels["fliggy"]["price"] == 1262
    assert channels["fliggy"]["tax_included"] is True
    assert channels["tuniu"]["basis"] == "bare"
    assert channels["tuniu"]["tax_included"] is False
    assert result["lowest_platform"] == "fliggy"
    assert result["lowest"]["quote"]["payable_total"] is None
    assert "最终价未确认" in result["lowest"]["note"]


class _Response:
    url = "https://example.invalid/getFlightCalendar"

    def json(self):
        return {"bstatus": 0, "lowPriceList": [{"depDate": "2027-01-01", "price": 500}]}


class _Page:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def goto(self, url, **kwargs):
        self.handlers["response"](_Response())

    def wait_for_timeout(self, milliseconds):
        pytest.fail("合成日历响应已送达，不应等待平台")


class _Context:
    def __init__(self):
        self.close_calls = 0

    def new_page(self):
        return _Page()

    def close(self):
        self.close_calls += 1

    def storage_state(self):
        pytest.fail("匿名雷达不得导出会话快照")


class _Browser:
    def __init__(self, chromium):
        self.chromium = chromium
        self.close_calls = 0

    def new_context(self, **kwargs):
        self.chromium.context_kwargs.append(kwargs)
        if self.chromium.context_error:
            raise self.chromium.context_error
        return self.chromium.context

    def close(self):
        self.close_calls += 1


class _Chromium:
    def __init__(self):
        self.context = _Context()
        self.browser = _Browser(self)
        self.launch_kwargs = []
        self.context_kwargs = []
        self.persistent_calls = []
        self.context_error = None

    def launch(self, **kwargs):
        self.launch_kwargs.append(kwargs)
        return self.browser

    def launch_persistent_context(self, *args, **kwargs):
        self.persistent_calls.append((args, kwargs))
        return self.context


class _Playwright:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False


@pytest.fixture
def sdk(monkeypatch):
    chromium = _Chromium()
    factory = lambda: _Playwright(chromium)
    monkeypatch.setattr(base, "sync_playwright", factory)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", factory)
    return chromium


def _calendar(**kwargs):
    return qunar.QunarCrawler.fetch_low_price_calendar(
        "重庆", "北京", "2027-01-01", "2027-01-01", _LOG, **kwargs)


def test_qunar_anonymous_constructor_uses_base_memory_context_and_no_profile_io(tmp_path,
                                                                             monkeypatch, sdk):
    constructors = []
    init = base.BaseCrawler.__init__

    def record_init(self, cfg, logger):
        constructors.append(copy.deepcopy(cfg))
        init(self, cfg, logger)

    def forbidden(*args, **kwargs):
        pytest.fail("匿名雷达不得创建、复制或清理持久 profile")

    monkeypatch.setattr(base.BaseCrawler, "__init__", record_init)
    monkeypatch.setattr(qunar.tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(qunar.shutil, "copy2", forbidden)
    monkeypatch.setattr(qunar.shutil, "rmtree", forbidden)
    result = _calendar(profile_root=str(tmp_path), anonymous_query=True,
                       proxy="http://127.0.0.1:7897")
    assert result["lowPriceList"][0]["price"] == 500
    assert constructors[0]["anonymous_query"] is True
    assert constructors[0]["user_data_dir"] == str(tmp_path)
    assert len(sdk.launch_kwargs) == 1 and sdk.persistent_calls == []
    assert sdk.launch_kwargs[0]["proxy"] == {"server": "http://127.0.0.1:7897"}
    assert "user_data_dir" not in sdk.launch_kwargs[0]
    assert sdk.context_kwargs[0]["viewport"] == {"width": 390, "height": 844}
    assert sdk.context_kwargs[0]["is_mobile"] is True
    assert sdk.context_kwargs[0]["locale"] == "zh-CN"
    assert sdk.context_kwargs[0]["timezone_id"] == "Asia/Shanghai"
    assert sdk.context_kwargs[0]["user_agent"] == constructors[0]["mobile_user_agent"]
    assert "storage_state" not in sdk.context_kwargs[0]
    assert sdk.context.close_calls == 1 and sdk.browser.close_calls == 1


def test_qunar_anonymous_context_failure_does_not_switch_to_account(tmp_path, monkeypatch, sdk):
    sdk.context_error = RuntimeError("synthetic context failure")

    def forbidden(*args, **kwargs):
        pytest.fail("匿名失败不得创建或清理持久 profile")

    monkeypatch.setattr(qunar.tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(qunar.shutil, "rmtree", forbidden)
    with pytest.raises(RuntimeError, match="synthetic context failure"):
        _calendar(profile_root=str(tmp_path), anonymous_query=True)
    assert sdk.persistent_calls == [] and sdk.browser.close_calls == 1


def test_qunar_account_mode_retains_isolated_profile_copy(tmp_path, monkeypatch, sdk):
    root = tmp_path / "account"
    cookie = root / "qunar" / "Default" / "Network" / "Cookies"
    cookie.parent.mkdir(parents=True)
    cookie.write_bytes(b"synthetic-account-cookie-database")
    profile = tmp_path / "temporary-profile"
    profile.mkdir()
    cleanup_calls = []
    monkeypatch.setattr(qunar.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    monkeypatch.setattr(qunar.shutil, "rmtree", lambda path, **kwargs: cleanup_calls.append(path))
    result = _calendar(profile_root=str(root))
    assert result["lowPriceList"][0]["price"] == 500
    assert (profile / "Default" / "Network" / "Cookies").read_bytes() == cookie.read_bytes()
    assert sdk.launch_kwargs == []
    assert sdk.persistent_calls[0][0] == (str(profile),)
    assert cleanup_calls == [str(profile)]
    assert sdk.context.close_calls == 1
