# -*- coding: utf-8 -*-
"""多程整单提醒：只认渠道整单总价，按行程去抖，通知失败可重试。"""
import logging
import os
import sys
from datetime import timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import webui  # noqa: E402
from core.multicity import segments_key  # noqa: E402
from core.storage import PriceStorage  # noqa: E402
from support_test_clock import MULTICITY_NOW, freeze_webui_clock  # noqa: E402

SEGS = [{"from": "SHA", "to": "TYO", "date": "2026-11-20"},
        {"from": "TYO", "to": "OSA", "date": "2026-11-23"}]
LEGS = [{"flight_no": "9C8515", "from": "SHA", "to": "TYO",
         "date": "2026-11-20", "dep": "21:10", "arr": "01:05"},
        {"flight_no": "MM304", "from": "TYO", "to": "OSA",
         "date": "2026-11-23", "dep": "07:30", "arr": "09:10"}]
KEY = segments_key(SEGS)


class Notifier:
    def __init__(self, res=True):
        self.res = res
        self.sent = []

    def send(self, title, desp):
        self.sent.append((title, desp))
        return self.res


@pytest.fixture(autouse=True)
def _freeze_alert_clock(monkeypatch):
    freeze_webui_clock(monkeypatch, MULTICITY_NOW)


def _user(thr=1500, platforms=("ctrip",), *, segments=None):
    segs = SEGS if segments is None else segments
    return {"name": "u", "platforms": list(platforms), "routes": [{
        "from": segs[0]["from"], "to": segs[-1]["to"],
        "dates": [s["date"] for s in segs],
        "alert_threshold": thr, "segments": segs}]}


def _fetched_at(hours_ago=0):
    return (MULTICITY_NOW - timedelta(hours=hours_ago)).isoformat(
        sep=" ", timespec="seconds")


def _save_price(storage, price, *, hours_ago=0):
    storage.save_itinerary("ctrip", KEY, SEGS, price, LEGS,
                           fetched_at=_fetched_at(hours_ago))


def _db(tmp_path):
    st = PriceStorage(str(tmp_path / "prices.db"))
    _save_price(st, 1298.0)
    return st


def test_alerts_once_then_only_on_further_drop(tmp_path):
    st, n = _db(tmp_path), Notifier()
    u = _user()
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 1
    title, desp = n.sent[0]
    assert "SHA→TYO→OSA" in title and "1298" in title
    assert "多程含税价" in desp and "9C8515" in desp and "MM304" in desp
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 0, "同价不得刷屏"
    assert len(n.sent) == 1

    _save_price(st, 1200.0)
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 1, "继续下探应补推"
    assert len(n.sent) == 2
    assert "1200" in n.sent[1][0]


def test_above_threshold_resets_so_next_crossing_alerts(tmp_path):
    st, n = _db(tmp_path), Notifier()
    u = _user()
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 1
    _save_price(st, 1600.0)
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 0
    assert st.get_alert_state("mc|u|%s|1500" % KEY) == 0
    _save_price(st, 1400.0)
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 1
    assert len(n.sent) == 2


OPEN = [{"from": "SHA", "to": "TYO", "date": "2026-11-20"},
        {"from": "OSA", "to": "SHA", "date": "2026-11-26"}]
OPEN_LEGS = [{"flight_no": "9C8515", "from": "SHA", "to": "TYO",
              "date": "2026-11-20", "dep": "21:10", "arr": "01:05"},
             {"flight_no": "MM079", "from": "OSA", "to": "SHA",
              "date": "2026-11-26", "dep": "22:25", "arr": "00:20"}]


def test_open_jaw_alert_title_breaks_chain(tmp_path):
    """开口程提醒标题也不能画成 SHA→TYO→SHA（第二段从 OSA 出发）。"""
    st = PriceStorage(str(tmp_path / "prices.db"))
    st.save_itinerary("ctrip", segments_key(OPEN), OPEN, 2184.0, OPEN_LEGS,
                      fetched_at=_fetched_at())
    u = _user(2500, segments=OPEN)
    n = Notifier()
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 1
    title, desp = n.sent[0]
    assert "SHA→TYO / OSA→SHA" in title and "2184" in title
    assert "9C8515" in desp and "MM079" in desp


def test_expired_first_segment_never_alerts(tmp_path):
    yesterday = (MULTICITY_NOW.date() - timedelta(days=1)).isoformat()
    old = [dict(SEGS[0], date=yesterday), SEGS[1]]
    old_legs = [dict(LEGS[0], date=yesterday), LEGS[1]]
    st = PriceStorage(str(tmp_path / "prices.db"))
    key = segments_key(old)
    st.save_itinerary("ctrip", key, old, 900.0, old_legs,
                      fetched_at=_fetched_at())
    u = _user(segments=old)
    n = Notifier()
    assert webui._multicity_alert_round(
        u, n, st, logging.getLogger("mc")) == 0
    assert n.sent == [], "首段已过期不得按隔夜行提醒"
    assert st.get_alert_state("mc|u|%s|1500" % key) is None


def test_no_threshold_stale_and_rejected_are_silent(tmp_path):
    st, n = _db(tmp_path), Notifier()
    assert webui._multicity_alert_round(
        _user(0), n, st, logging.getLogger("mc")) == 0
    assert webui._multicity_alert_round(
        _user(platforms=["qunar"]), n, st,
        logging.getLogger("mc")) == 0, "未勾携程的用户不得收到多程提醒"

    _save_price(st, 900.0, hours_ago=7)
    assert webui._multicity_alert_round(
        _user(), n, st, logging.getLogger("mc")) == 0, "过期行不得提醒"
    assert n.sent == []

    _save_price(st, 1000.0)
    bad = Notifier(res=False)
    assert webui._multicity_alert_round(
        _user(), bad, st, logging.getLogger("mc")) == 0
    assert len(bad.sent) == 1
    assert st.get_alert_state("mc|u|%s|1500" % KEY) is None, \
        "发送未被接受不得写去抖状态"
    ok = Notifier()
    assert webui._multicity_alert_round(
        _user(), ok, st, logging.getLogger("mc")) == 1
    assert len(ok.sent) == 1
