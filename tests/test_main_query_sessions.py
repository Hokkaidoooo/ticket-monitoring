"""主程序的部分渠道可用、匿名挑战拒绝与账号核验隔离。"""
import inspect
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import main
from core.query_sessions import AnonymousSessionUnsupportedError


class _Storage:
    def __init__(self, root):
        self.db_path = str(root / "prices.db")
        self.interventions = []
        self.paths = []
        self.intervention_error = None

    def latest_prices(self, hours):
        return []

    def save_intervention(self, *args, **kwargs):
        if self.intervention_error is not None:
            raise self.intervention_error
        self.interventions.append((args, kwargs))

    def save_fetch_path(self, *args, **kwargs):
        self.paths.append((args, kwargs))
        return len(self.paths)


class _MonitorPacing:
    def __init__(self):
        self.calls = []

    def record(self, *args):
        self.calls.append(("record", args))

    def reset(self, *args):
        self.calls.append(("reset", args))

    def note_fetch(self, *args):
        self.calls.append(("note_fetch", args))


def _setup(tmp_path, monkeypatch, mode="rows", anonymous=True):
    instances = []

    class _Crawler:
        name = "ctrip"
        warmup_url = "https://example.invalid/query"
        warmup_cookies = ()

        def __init__(self, config, logger):
            assert not self.name.startswith("app_") or not config["anonymous_query"], (
                "匿名 App 必须在创建设备渠道前拒绝")
            self.config = config
            self.anonymous_query = config["anonymous_query"]
            self.paths = []
            self.calls = []
            instances.append(self)

        def warmup(self):
            self.calls.append("warmup")
            return True

        def safe_fetch(self, frm, to, dates):
            self.calls.append("fetch")
            self.paths = [{
                "path": "WEB", "status": "ok" if mode == "rows" else "blocked",
                "rows": 1 if mode == "rows" else 0,
                "detail": "合成响应" if mode == "rows" else "合成登录墙",
            }]
            if mode == "raise":
                raise RuntimeError("合成查询失败")
            return [SimpleNamespace(price=450)] if mode == "rows" else []

        def open_challenge_window(self, *args, **kwargs):
            pytest.fail("匿名挑战不得打开窗口或保存登录快照")

    class _SecondBrowser(_Crawler):
        name = "fliggy"

    class _App(_Crawler):
        name = "app_ctrip"

    monkeypatch.setattr(main, "REGISTRY", {
        "ctrip": _Crawler, "fliggy": _SecondBrowser, "app_ctrip": _App,
    })
    cfg = {
        "crawler": {
            "monitor_anonymous": anonymous,
            "user_data_dir": str(tmp_path / "account"),
            "anonymous_user_data_dir": str(tmp_path / "monitor"),
        },
        "schedule": {}, "web": {}, "output": {},
    }
    rt = {
        "cfg": cfg, "alerters": [],
        "users": [{"routes": [{"from": "CKG", "to": "PEK",
                                "dates": ["2027-06-01"]}]}],
    }
    storage = _Storage(tmp_path)
    job = main.make_sweep_job(
        cfg, rt, logging.getLogger("main-query-sessions"), storage,
        lambda *args, **kwargs: {})
    inner = inspect.getclosurevars(job).nonlocals["_sweep_inner"]
    factory = inspect.getclosurevars(inner).nonlocals["get_crawlers"]
    monitor_pacing = _MonitorPacing()
    rt["pacing"] = monitor_pacing
    return SimpleNamespace(
        rt=rt, storage=storage, instances=instances,
        factory=factory, monitor_pacing=monitor_pacing)


def test_mixed_anonymous_channels_keep_web_crawlers_and_record_app_rejection(
        tmp_path, monkeypatch, caplog):
    env = _setup(tmp_path, monkeypatch)
    caplog.set_level(logging.WARNING, logger="main-query-sessions")

    crawlers = env.factory(["app_ctrip", "ctrip", "fliggy"])

    assert [crawler.name for crawler in crawlers] == ["ctrip", "fliggy"]
    assert all(crawler.anonymous_query for crawler in crawlers)
    assert all(crawler.config["user_data_dir"] == str(tmp_path / "monitor")
               for crawler in crawlers)
    assert env.factory(["fliggy", "ctrip", "app_ctrip"]) == crawlers
    assert len(env.instances) == 2
    assert sum(len(c.safe_fetch("CKG", "PEK", ["2027-06-01"])) for c in crawlers) == 2
    assert env.rt["scene"]["app_ctrip"]["status"] == "unsupported"
    args, record = env.storage.interventions[0]
    assert args == ("app_ctrip", "query_session_unsupported")
    assert record["ok"] is False and record["scope"] == "monitor"
    assert "暂不支持匿名查询" in record["note"]
    assert "app_ctrip 已拒绝" in caplog.text


def test_all_unsupported_channels_fail_explicitly_before_construction(
        tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    with pytest.raises(AnonymousSessionUnsupportedError, match="暂不支持匿名查询"):
        env.factory(["app_ctrip"])
    assert env.instances == []
    assert env.rt["scene"]["app_ctrip"]["kind"] == "unsupported"


def test_rejection_ledger_failure_is_visible_and_keeps_supported_web_channels(
        tmp_path, monkeypatch, caplog):
    env = _setup(tmp_path, monkeypatch)
    env.storage.intervention_error = sqlite3.OperationalError("合成台账写入失败")
    caplog.set_level(logging.WARNING, logger="main-query-sessions")

    crawlers = env.factory(["app_ctrip", "ctrip"])

    assert [crawler.name for crawler in crawlers] == ["ctrip"]
    assert env.rt["scene"]["app_ctrip"]["diagnostic_error"] == "合成台账写入失败"
    assert "拒绝记录写入失败" in caplog.text


def test_factory_does_not_hide_unrelated_construction_errors(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)

    class _Broken:
        def __init__(self, *args):
            raise ValueError("合成渠道配置错误")

    monkeypatch.setitem(main.REGISTRY, "ctrip", _Broken)
    with pytest.raises(ValueError, match="合成渠道配置错误"):
        env.factory(["app_ctrip", "ctrip"])


def test_account_and_monitor_cache_instances_are_separate(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    monitor = env.factory(["ctrip"])[0]
    account = env.factory(["ctrip"], role="account")[0]
    assert monitor is not account
    assert monitor.anonymous_query is True and account.anonymous_query is False
    assert account.config["user_data_dir"] == str(tmp_path / "account")
    assert env.factory(["app_ctrip", "ctrip"], role="account")[0].name == "app_ctrip"


def test_anonymous_challenge_rejects_before_thread_or_crawler_start(
        tmp_path, monkeypatch, caplog):
    env = _setup(tmp_path, monkeypatch)
    caplog.set_level(logging.WARNING, logger="main-query-sessions")
    env.rt["scene"]["ctrip"] = {
        "kind": "captcha", "url": "https://example.invalid/challenge",
    }

    def forbidden_thread(*args, **kwargs):
        pytest.fail("匿名挑战不得启动起窗线程")

    monkeypatch.setattr(threading, "Thread", forbidden_thread)
    out = env.rt["verify_channel"]("ctrip")

    assert out["ok"] is False and out["status"] == "unsupported"
    assert out["session_role"] == "monitor" and out["rows"] == 0
    assert "匿名监控不保存验证会话" in out["err"]
    assert not env.rt.get("verify_open") and env.instances == []
    assert env.rt["scene"]["ctrip"]["kind"] == "captcha"
    assert env.rt["scene"]["ctrip"]["verify"]["status"] == "unsupported"
    assert env.storage.interventions[-1][0] == ("ctrip", "verify")
    assert "已拒绝" in caplog.text


@pytest.mark.parametrize("mode", ["rows", "empty", "raise"])
def test_account_probe_never_updates_monitor_pacing_or_paths(
        tmp_path, monkeypatch, mode):
    env = _setup(tmp_path, monkeypatch, mode=mode)

    out = env.rt["probe_channel"]("ctrip", scope="dom", role="account")

    assert out["session_role"] == "account"
    assert out["ok"] is (mode == "rows")
    assert out["rows"] == (1 if mode == "rows" else 0)
    assert env.monitor_pacing.calls == [] and env.storage.paths == []
    assert env.instances[0].config["user_data_dir"] == str(tmp_path / "account")
    assert env.instances[0].anonymous_query is False
    assert env.instances[0].calls == ["warmup", "fetch"]
    if mode != "raise":
        assert env.storage.interventions[-1][0] == ("ctrip", "account_probe")


def test_monitor_probe_keeps_monitor_pacing_and_path_evidence(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)

    out = env.rt["probe_channel"]("ctrip", scope="dom")

    assert out["ok"] is True and out["session_role"] == "monitor"
    assert out["anonymous_query"] is True
    assert [name for name, args in env.monitor_pacing.calls] == [
        "record", "record", "reset", "note_fetch",
    ]
    assert env.storage.paths[0][0][:5] == (
        "ctrip", "CKG", "PEK", "2027-06-01", "WEB")
    assert env.storage.paths[0][1]["evidence_level"] == "probe"
    assert env.storage.interventions[-1][0] == ("ctrip", "probe")


def test_unsupported_probe_reports_rejection_and_role_without_querying(
        tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    out = env.rt["probe_channel"]("app_ctrip")
    assert out["ok"] is False and out["status"] == "unsupported"
    assert out["session_role"] == "monitor" and out["rows"] == 0
    assert env.instances == [] and env.monitor_pacing.calls == []
    assert env.rt["scene"]["app_ctrip"]["status"] == "unsupported"


def test_account_probe_validation_failure_reports_role(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch)
    out = env.rt["probe_channel"]("ctrip", scope="invalid", role="account")
    assert out["ok"] is False and out["session_role"] == "account"
    assert env.instances == [] and env.monitor_pacing.calls == []
