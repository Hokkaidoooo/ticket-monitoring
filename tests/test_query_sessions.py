"""查询角色、账号资料边界与 Playwright 上下文调用契约。"""
import copy
import json
import logging
import os
import time

import pytest

from core import paths
from core.query_sessions import (
    AnonymousSessionUnsupportedError,
    anonymous_query_enabled,
    crawler_for_session,
    ensure_session_supported,
    resolve_query_sessions,
)
from crawlers import base

_LOG = logging.getLogger("test_query_sessions")


def test_default_roles_follow_existing_environment_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_root", lambda: str(tmp_path))
    monkeypatch.setenv("TM_USER_DATA_DIR", "redirected_accounts")
    cfg = {"user_data_dir": "user_data"}
    roles = resolve_query_sessions(cfg)
    expected = str(tmp_path / "redirected_accounts")
    assert roles["monitor_anonymous"] is False
    assert roles["account"] == {
        "user_data_dir": expected, "anonymous_query": False,
    }
    assert roles["monitor"] == roles["account"]
    assert not (tmp_path / "redirected_accounts").exists()


def test_anonymous_roles_anchor_relative_roots_without_creating_them(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_root", lambda: str(tmp_path))
    monkeypatch.setenv("TM_USER_DATA_DIR", "environment_accounts")
    cfg = {"monitor_anonymous": True, "user_data_dir": "configured_accounts"}
    roles = resolve_query_sessions(cfg)
    assert roles["account"]["user_data_dir"] == str(tmp_path / "configured_accounts")
    assert roles["monitor"]["user_data_dir"] == str(tmp_path / "anonymous_data")
    assert roles["monitor"]["anonymous_query"] is True
    assert not (tmp_path / "anonymous_data").exists()
    assert not (tmp_path / "configured_accounts").exists()


def test_crawler_role_copy_reuses_frozen_resolution(tmp_path, monkeypatch):
    cfg = {
        "monitor_anonymous": True,
        "user_data_dir": str(tmp_path / "account"),
        "anonymous_user_data_dir": str(tmp_path / "anonymous"),
        "options": {"timeout": 5},
    }
    original = copy.deepcopy(cfg)
    roles = resolve_query_sessions(cfg)
    monkeypatch.setenv("TM_USER_DATA_DIR", str(tmp_path / "later"))
    monitor = crawler_for_session(cfg, roles)
    account = crawler_for_session(cfg, roles, "account")
    assert monitor is not cfg and account is not cfg
    assert cfg == original
    assert monitor["user_data_dir"] == roles["monitor"]["user_data_dir"]
    assert monitor["anonymous_query"] is True
    assert account["user_data_dir"] == roles["account"]["user_data_dir"]
    assert account["anonymous_query"] is False
    cfg["user_data_dir"] = str(tmp_path / "changed")
    assert crawler_for_session(cfg, roles, "account")["user_data_dir"] == account["user_data_dir"]


def test_resolved_roles_are_nested_immutable_and_json_serializable(tmp_path):
    roles = resolve_query_sessions({"user_data_dir": str(tmp_path)})
    assert json.loads(json.dumps(roles)) == roles
    assert copy.deepcopy(roles) == roles
    with pytest.raises(TypeError, match="冻结"):
        roles["monitor_anonymous"] = True
    with pytest.raises(TypeError, match="冻结"):
        roles["account"]["anonymous_query"] = True
    with pytest.raises(TypeError, match="冻结"):
        roles["account"].update(user_data_dir="elsewhere")


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
@pytest.mark.parametrize("key", ["monitor_anonymous", "verify_account_in_background"])
def test_session_flag_rejects_non_boolean_values(key, value):
    with pytest.raises(ValueError, match=key + ".*布尔"):
        resolve_query_sessions({key: value})


@pytest.mark.parametrize("value", [None, 0, "false", []])
def test_anonymous_flag_rejects_non_boolean_values(value):
    cfg = {"anonymous_query": value}
    with pytest.raises(ValueError, match="anonymous_query.*布尔"):
        anonymous_query_enabled(cfg)
    with pytest.raises(ValueError, match="anonymous_query.*布尔"):
        base.BaseCrawler(cfg, _LOG)


@pytest.mark.parametrize("account_part,monitor_part", [
    ("account", "account"),
    ("account", "account/anonymous"),
    ("anonymous/account", "anonymous"),
    ("account", "other/../account"),
])
def test_anonymous_roots_reject_overlap_and_containment(tmp_path, account_part, monitor_part):
    with pytest.raises(ValueError, match="不能重合或互相包含"):
        resolve_query_sessions({
            "monitor_anonymous": True,
            "user_data_dir": str(tmp_path / account_part),
            "anonymous_user_data_dir": str(tmp_path / monitor_part),
        })


def test_sibling_prefixes_are_independent_roots(tmp_path):
    roles = resolve_query_sessions({
        "monitor_anonymous": True,
        "user_data_dir": str(tmp_path / "account"),
        "anonymous_user_data_dir": str(tmp_path / "account_anonymous"),
    })
    assert roles["monitor"]["anonymous_query"] is True


def test_root_alias_is_checked_by_real_path(tmp_path, monkeypatch):
    account = str(tmp_path / "account")
    alias = str(tmp_path / "alias")
    realpath = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda value: (
        realpath(account) if value == alias else realpath(value)))
    with pytest.raises(ValueError, match="不能重合"):
        resolve_query_sessions({
            "monitor_anonymous": True,
            "user_data_dir": account,
            "anonymous_user_data_dir": alias,
        })


@pytest.mark.skipif(os.name != "nt", reason="Windows 盘符规则")
def test_different_drives_are_independent_without_creating_directories():
    roles = resolve_query_sessions({
        "monitor_anonymous": True,
        "user_data_dir": r"C:\__ticket_query_sessions_account_test__",
        "anonymous_user_data_dir": r"D:\__ticket_query_sessions_anonymous_test__",
    })
    assert roles["monitor"]["anonymous_query"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows 大小写规则")
def test_windows_case_changes_do_not_hide_overlap(tmp_path):
    root = str(tmp_path / "Account")
    with pytest.raises(ValueError, match="不能重合"):
        resolve_query_sessions({
            "monitor_anonymous": True,
            "user_data_dir": root,
            "anonymous_user_data_dir": root.upper(),
        })


@pytest.mark.parametrize("role", ["login", "", None])
def test_unknown_role_is_rejected(tmp_path, role):
    roles = resolve_query_sessions({"user_data_dir": str(tmp_path)})
    with pytest.raises(ValueError, match="未知查询角色"):
        crawler_for_session({}, roles, role)


@pytest.mark.parametrize("platform", ["app_qunar", "app_ctrip", "app_dummy"])
def test_app_anonymous_is_explicitly_unsupported(platform):
    cfg = {"anonymous_query": True}
    with pytest.raises(AnonymousSessionUnsupportedError, match="暂不支持匿名查询") as error:
        ensure_session_supported(platform, cfg)
    assert error.value.status == "unsupported"
    assert cfg == {"anonymous_query": True}


def test_browser_anonymous_and_app_account_are_supported():
    ensure_session_supported("qunar", {"anonymous_query": True})
    ensure_session_supported("app_qunar", {"anonymous_query": False})
    ensure_session_supported("app_qunar", {})


class _ContractCrawler(base.BaseCrawler):
    name = "query_contract"
    profile_name = "query_contract"
    AUTH_COOKIES = ("synthetic_auth",)


class _NoProfileCrawler(_ContractCrawler):
    @property
    def user_data_dir(self):
        raise AssertionError("匿名调用不得访问持久 profile")

    @property
    def session_state_path(self):
        raise AssertionError("匿名调用不得访问会话快照路径")


class _Page:
    def __init__(self):
        self.default_timeout = None
        self.navigation_timeout = None
        self.events = {}

    def set_default_timeout(self, value):
        self.default_timeout = value

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    def on(self, event, callback):
        self.events[event] = callback

    def goto(self, url, **options):
        raise AssertionError("本契约测试不访问平台")


class _Context:
    def __init__(self, storage=None, close_error=None):
        self.scripts = []
        self.added_cookies = []
        self.cookie_reads = 0
        self.storage_reads = 0
        self.close_calls = 0
        self.storage = storage or {"cookies": [], "origins": []}
        self.close_error = close_error

    def add_init_script(self, script):
        self.scripts.append(script)

    def new_page(self):
        return _Page()

    def cookies(self):
        self.cookie_reads += 1
        return []

    def add_cookies(self, cookies):
        self.added_cookies.extend(copy.deepcopy(cookies))

    def storage_state(self):
        self.storage_reads += 1
        return copy.deepcopy(self.storage)

    def close(self):
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


class _Browser:
    def __init__(self, context_error=None, close_error=None):
        self.context_error = context_error
        self.context_close_error = close_error
        self.context_kwargs = []
        self.contexts = []
        self.close_calls = 0

    def new_context(self, **kwargs):
        self.context_kwargs.append(kwargs)
        if self.context_error:
            raise self.context_error
        ctx = _Context(close_error=self.context_close_error)
        self.contexts.append(ctx)
        return ctx

    def close(self):
        self.close_calls += 1


class _Chromium:
    def __init__(self):
        self.launch_kwargs = []
        self.persistent_kwargs = []
        self.browsers = []
        self.account_context = _Context()
        self.context_error = None
        self.close_error = None

    def launch(self, **kwargs):
        self.launch_kwargs.append(kwargs)
        browser = _Browser(self.context_error, self.close_error)
        self.browsers.append(browser)
        return browser

    def launch_persistent_context(self, **kwargs):
        self.persistent_kwargs.append(kwargs)
        return self.account_context


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
    monkeypatch.setattr(base, "sync_playwright", lambda: _Playwright(chromium))
    monkeypatch.setattr(base, "kernel_major", lambda: 126)
    return chromium


def _anonymous(tmp_path, **options):
    return _NoProfileCrawler({
        "anonymous_query": True, "user_data_dir": str(tmp_path), **options,
    }, _LOG)


def test_anonymous_context_never_uses_profile_restore_snapshot_or_lock(tmp_path, monkeypatch, sdk):
    class _ForbiddenLock:
        def __enter__(self):
            raise AssertionError("匿名上下文不得取得 profile 锁")

    monkeypatch.setattr(base, "_PROFILE_LOCKS_GUARD", _ForbiddenLock())
    crawler = _anonymous(tmp_path)
    with crawler.browser() as ctx:
        assert ctx.storage_reads == 0
        assert len(ctx.scripts) == 1
    browser = sdk.browsers[0]
    assert sdk.persistent_kwargs == []
    assert ctx.cookie_reads == 0 and ctx.added_cookies == []
    assert ctx.storage_reads == 0
    assert ctx.close_calls == 1 and browser.close_calls == 1
    assert not (tmp_path / "query_contract").exists()


def test_anonymous_context_preserves_launch_and_fingerprint_parameters(tmp_path, sdk):
    crawler = _anonymous(tmp_path, proxy="http://127.0.0.1:7897", browser_channel="chrome")
    crawler.use_mobile = True
    fp = crawler.fingerprint()
    with crawler.browser(headless=False, window_front=True, window_idx=2):
        pass
    launch = sdk.launch_kwargs[0]
    context = sdk.browsers[0].context_kwargs[0]
    assert launch["headless"] is False
    assert launch["proxy"] == {"server": fp["proxy"]}
    assert launch["channel"] == "chrome"
    assert "--window-position=380,324" in launch["args"]
    assert context == {
        "user_agent": fp["ua"], "viewport": fp["viewport"],
        "locale": fp["locale"], "timezone_id": fp["timezone"],
        "is_mobile": fp["is_mobile"], "has_touch": fp["has_touch"],
    }
    assert "user_data_dir" not in launch and "storage_state" not in context
    assert "user_agent" not in launch and "proxy" not in context


def test_anonymous_page_uses_existing_timeout_and_response_guard(tmp_path, sdk):
    crawler = _anonymous(tmp_path, timeout_seconds=12)
    with crawler.browser() as ctx:
        page = crawler.new_page(ctx)
        assert page.default_timeout == 12000
        assert page.navigation_timeout == 12000
        assert "response" in page.events
        assert page.goto._challenge_guarded is True
        assert crawler._last_page is page


def test_anonymous_context_is_fresh_each_time(tmp_path, sdk):
    crawler = _anonymous(tmp_path, stealth=False)
    with crawler.browser() as first:
        first.add_cookies([{"name": "synthetic_cookie", "value": "one"}])
    with crawler.browser() as second:
        assert first is not second
        assert second.added_cookies == [] and second.scripts == []
    assert len(sdk.browsers) == 2
    assert all(browser.close_calls == 1 for browser in sdk.browsers)
    assert sdk.persistent_kwargs == []


def test_anonymous_state_metadata_and_surface_do_not_read_profile(tmp_path):
    crawler = _anonymous(tmp_path)
    assert crawler._read_session_state() == {}
    assert crawler.load_session_cookies(["example.invalid"]) == []
    assert crawler.session_saved_at == 0.0 and crawler.session_age_days == 0.0
    assert crawler._should_refresh_snapshot(None) is False
    assert crawler._session_state_allowed({"cookies": [{"name": "synthetic_auth"}]}, "login") is False
    profile = crawler.profile_name
    with crawler.using_surface("h5"):
        assert crawler.profile_name == profile
    with pytest.raises(ValueError, match="不允许保存"):
        crawler._write_session_state({"cookies": []}, 1.0)


@pytest.mark.parametrize("save_session", ["login", "migrate"])
def test_anonymous_rejects_save_before_browser_start(tmp_path, sdk, save_session):
    crawler = _anonymous(tmp_path)
    with (
        pytest.raises(ValueError, match="不能保存或迁移"),
        crawler.browser(save_session=save_session),
    ):
        pytest.fail("匿名保存必须在 yield 前拒绝")
    assert sdk.launch_kwargs == [] and sdk.persistent_kwargs == []


def test_anonymous_context_creation_failure_closes_browser_and_propagates(tmp_path, sdk):
    sdk.context_error = RuntimeError("context creation failed")
    with (
        pytest.raises(RuntimeError, match="context creation failed"),
        _anonymous(tmp_path).browser(),
    ):
        pytest.fail("上下文创建失败不得返回成功")
    assert sdk.browsers[0].close_calls == 1
    assert sdk.persistent_kwargs == []


def test_anonymous_query_failure_closes_resources_without_session_switch(tmp_path, sdk):
    with (
        pytest.raises(RuntimeError, match="query failed"),
        _anonymous(tmp_path).browser() as ctx,
    ):
        raise RuntimeError("query failed")
    assert ctx.close_calls == 1 and sdk.browsers[0].close_calls == 1
    assert ctx.storage_reads == 0 and sdk.persistent_kwargs == []


def test_anonymous_context_close_failure_still_closes_browser(tmp_path, sdk):
    sdk.close_error = RuntimeError("context close failed")
    with (
        pytest.raises(RuntimeError, match="context close failed"),
        _anonymous(tmp_path).browser(),
    ):
        pass
    assert sdk.browsers[0].close_calls == 1
    assert sdk.persistent_kwargs == []


def _auth_state(value):
    return {"cookies": [{
        "name": "synthetic_auth", "value": value, "domain": ".example.invalid",
        "path": "/", "expires": time.time() + 3600,
    }], "origins": []}


def test_account_context_keeps_persistent_restore_and_refresh(tmp_path, sdk):
    crawler = _ContractCrawler({"user_data_dir": str(tmp_path), "stealth": False}, _LOG)
    crawler._write_session_state(_auth_state("saved_synthetic"), 123.0)
    sdk.account_context.storage = _auth_state("refreshed_synthetic")
    with crawler.browser() as ctx:
        assert ctx.added_cookies[0]["value"] == "saved_synthetic"
    assert sdk.launch_kwargs == []
    assert sdk.persistent_kwargs[0]["user_data_dir"] == crawler.user_data_dir
    assert ctx.close_calls == 1 and ctx.storage_reads == 1
    state = crawler._read_session_state()
    assert state["saved_at"] == 123.0
    assert state["cookies"][0]["value"] == "refreshed_synthetic"


def test_account_login_save_still_publishes_authenticated_state(tmp_path, sdk):
    crawler = _ContractCrawler({"user_data_dir": str(tmp_path), "stealth": False}, _LOG)
    sdk.account_context.storage = _auth_state("login_synthetic")
    started = time.time()
    with crawler.browser(save_session="login"):
        pass
    assert crawler.session_saved_at >= started
    assert crawler.load_session_cookies()[0]["value"] == "login_synthetic"
    assert sdk.account_context.close_calls == 1
    assert sdk.launch_kwargs == []


def test_account_legacy_profile_migration_still_runs_only_for_account(tmp_path, sdk):
    crawler = _ContractCrawler({"user_data_dir": str(tmp_path), "stealth": False}, _LOG)
    (tmp_path / crawler.name / "Default").mkdir(parents=True)
    sdk.account_context.storage = _auth_state("legacy_synthetic")
    assert crawler.load_session_cookies()[0]["value"] == "legacy_synthetic"
    assert crawler.session_saved_at == 0.0
    assert len(sdk.persistent_kwargs) == 1 and sdk.launch_kwargs == []
