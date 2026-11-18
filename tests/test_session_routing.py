"""匿名监控、账号资料和后台核验的运行期路由。"""
import copy
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
import pytest

import webui
from core.query_sessions import resolve_query_sessions


@pytest.fixture
def routed_state(tmp_path, monkeypatch):
    crawler = {
        "user_data_dir": str(tmp_path / "account"),
        "anonymous_user_data_dir": str(tmp_path / "anonymous"),
        "monitor_anonymous": True,
        "verify_account_in_background": False,
        "timeout": 30,
    }
    config = {"crawler": crawler}
    roles = resolve_query_sessions(crawler)
    monkeypatch.setattr(webui._State, "cfg", copy.deepcopy(config))
    monkeypatch.setattr(webui._State, "boot_cfg", copy.deepcopy(config))
    monkeypatch.setattr(webui._State, "query_sessions", roles)
    return crawler, roles


def test_monitor_and_login_have_separate_frozen_directories(routed_state, monkeypatch):
    crawler, roles = routed_state
    monkeypatch.setenv("TM_USER_DATA_DIR", "changed-environment")
    webui._State.cfg["crawler"]["user_data_dir"] = "changed-config"
    assert webui._ud_root_runtime() == roles["monitor"]["user_data_dir"]
    assert webui._ud_root_runtime("account") == crawler["user_data_dir"]
    assert webui._effective_crawler_config()["anonymous_query"] is True
    assert webui._effective_crawler_config("account")["anonymous_query"] is False


def test_live_options_do_not_replace_session_roots(routed_state):
    _crawler, roles = routed_state
    webui._State.cfg["crawler"]["timeout"] = 80
    config = webui._effective_crawler_config()
    assert config["timeout"] == 80
    assert config["user_data_dir"] == roles["monitor"]["user_data_dir"]


def test_login_file_lookup_uses_saved_account_root(routed_state, tmp_path):
    crawler, _roles = routed_state
    path = tmp_path / "account" / "ctrip" / "session-state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    webui._State.cfg["crawler"]["user_data_dir"] = str(tmp_path / "other")
    assert webui._profile_session_file("ctrip", "ctrip") == str(path)
    assert webui._effective_crawler_config("account")["user_data_dir"] == crawler["user_data_dir"]


def test_anonymous_results_do_not_change_account_auth_evidence(routed_state, monkeypatch):
    monkeypatch.setattr(webui, "_PROBE_AUTH", {})
    webui._record_probe_auth("ctrip", "dom", 2, "CKG", "BJS", "2026-12-01")
    webui._record_probe_auth_reject(
        "ctrip", "dom", "需要登录", "CKG", "BJS", "2026-12-01")
    assert webui._PROBE_AUTH == {}


def test_background_verification_does_not_sleep_or_request_when_disabled(
        routed_state, monkeypatch):
    def unexpected_sleep(_seconds):
        raise AssertionError("关闭后台账号核验后不应进入定时轮询")

    monkeypatch.setattr(webui.time, "sleep", unexpected_sleep)
    webui._slot_verify_loop()
    assert webui._kick_login_probe("ctrip")["ok"] is False


def test_unknown_role_is_an_explicit_error(routed_state):
    with pytest.raises(ValueError, match="未知"):
        webui._effective_crawler_config("guest_typo")


def test_background_switch_is_fixed_until_restart(routed_state):
    assert webui._background_account_verification() is False
    webui._State.cfg["crawler"]["verify_account_in_background"] = True
    assert webui._background_account_verification() is False


def test_stale_h5_save_does_not_start_background_verification(
        routed_state, tmp_path, monkeypatch):
    path = tmp_path / "account" / "tuniu_h5" / "session-state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    stale_time = time.time() - 7 * 3600
    os.utime(path, (stale_time, stale_time))
    monkeypatch.setattr(webui._State, "login_sessions", {})
    requests = []
    monkeypatch.setattr(webui._State, "probe_cb", lambda *a, **kw: requests.append(kw))
    monkeypatch.setattr(webui, "_verify_login_slot", lambda key: requests.append(key))
    threads = []
    real_thread = threading.Thread

    def capture_thread(*args, **kwargs):
        threads.append(kwargs.get("name"))
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(webui.threading, "Thread", capture_thread)
    result = webui._finish_login("tuniu", "tuniu_h5")
    assert result["stale"] is True
    assert result["verifying"] is False
    assert "后台账号核验已关闭" in result["hint"]
    assert requests == []
    assert threads == []


def test_h5_login_worker_saves_without_automatic_account_queries(
        routed_state, tmp_path, monkeypatch):
    class SavedLogin:
        def _login_window(self, evt, idx, timeout_s, ready_evt):
            ready_evt.set()
            evt.set()
            return True

    from core import interrupt

    monkeypatch.setattr(interrupt, "request", lambda **kwargs: None)
    monkeypatch.setattr(webui, "slot_crawler", lambda *args: SavedLogin())
    monkeypatch.setattr(webui, "_no_graphical_env", lambda: "")
    monkeypatch.setattr(webui, "_clear_login_wall_memo", lambda *args: [])
    monkeypatch.setattr(webui._State, "login_saved_cb", None)
    for key in ("login_sessions", "login_pending", "login_errors"):
        monkeypatch.setattr(webui._State, key, {})
    webui._State.cfg["output"] = {"db_path": str(tmp_path / "login-test.db")}
    requests = []
    monkeypatch.setattr(webui._State, "probe_cb", lambda *a, **kw: requests.append(kw))
    monkeypatch.setattr(webui, "_verify_login_slot", lambda key: requests.append(key))
    created = []
    real_thread = threading.Thread

    def capture_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(webui.threading, "Thread", capture_thread)
    result = webui._start_login("tuniu", "tuniu_h5")
    for thread in created:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert result["ok"] is True
    assert requests == []
    assert all("slot-verify" not in thread.name for thread in created)


def test_slot_evidence_reads_and_writes_only_frozen_account_root(
        routed_state, tmp_path, monkeypatch):
    account = tmp_path / "account" / "slot_verify.json"
    monitor = tmp_path / "anonymous" / "slot_verify.json"
    account.parent.mkdir(parents=True)
    monitor.parent.mkdir(parents=True)
    payload = {"tuniu_h5": {"status": "page_ok", "ts": 1}}
    account.write_text(json.dumps(payload), encoding="utf-8")
    monitor.write_text('{"wrong_root":{"status":"challenge"}}', encoding="utf-8")
    before = monitor.read_bytes()
    webui._State.cfg["crawler"]["user_data_dir"] = str(tmp_path / "changed")
    monkeypatch.chdir(tmp_path)
    assert webui._slot_verify_load() == payload
    monkeypatch.setattr(webui, "_SLOT_VERIFY", payload)
    webui._slot_verify_save()
    assert json.loads(account.read_text(encoding="utf-8")) == payload
    assert monitor.read_bytes() == before
    assert not (tmp_path / "changed").exists()


@pytest.mark.parametrize("payload", [[], None, {"tuniu": None}])
def test_invalid_slot_evidence_is_reported(routed_state, tmp_path, payload):
    path = tmp_path / "account" / "slot_verify.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="账号核验"):
        webui._slot_verify_load()


def test_anonymous_challenge_is_rejected_before_window_creation(
        routed_state, monkeypatch):
    ticket = {"plat": "ctrip", "url": "https://example.invalid", "status": "open"}
    monkeypatch.setattr(webui, "_PROBE_CHALLENGES", {"test": ticket})
    monkeypatch.setattr(webui, "_PROBE_CHALLENGE_SESSIONS", {})
    result = webui._probe_open_challenge("test")
    assert result["ok"] is False
    assert result["status"] == "unsupported"
    assert ticket["status"] == "open"
    assert webui._PROBE_CHALLENGE_SESSIONS == {}


@pytest.fixture
def session_http_server(routed_state, monkeypatch):
    monkeypatch.setitem(webui.DEMO, "on", False)
    monkeypatch.setattr(webui._State, "pacing", None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}",
                          trust_env=False) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_anonymous_reset_and_restore_preserve_account_profile(
        session_http_server, tmp_path):
    account = tmp_path / "account" / "ctrip" / "session-state.json"
    monitor = tmp_path / "anonymous" / "ctrip" / "diagnostic.txt"
    account.parent.mkdir(parents=True)
    monitor.parent.mkdir(parents=True)
    account.write_text("protected test account", encoding="utf-8")
    monitor.write_text("test monitor state", encoding="utf-8")
    before = account.read_bytes()
    webui._State.cfg["crawler"]["user_data_dir"] = str(tmp_path / "changed")
    response = session_http_server.post("/api/session/reset", json={"plat": "ctrip"})
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["session_role"] == "monitor"
    assert "继续匿名查询" in result["note"]
    assert account.read_bytes() == before
    assert not monitor.exists()
    response = session_http_server.post("/api/session/restore", json={
        "plat": "ctrip", "name": result["result"]["backup"],
    })
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert monitor.read_text(encoding="utf-8") == "test monitor state"
    assert account.read_bytes() == before
    assert not (tmp_path / "changed").exists()
