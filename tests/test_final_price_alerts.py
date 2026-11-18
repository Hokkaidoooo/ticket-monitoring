"""最终费用提醒的选价、去抖和渠道证据隔离。"""
import json
import logging

import pytest

from core.alerter import Alerter, _dedup_tie, _tie_any_ok
from core.final_alerts import (
    alert_state_key,
    flight_reminder_price,
    reminder_price,
    require_final_for_alerts,
)
from core.models import FlightPrice, Route
from core.pricequote import build_quote
from core.storage import PriceStorage

DATE = "2026-12-01"


def _verified(price=450, tax=50, insurance=20, service=10):
    return build_quote({
        "quoted_amount": price, "basis": "bare",
        "source_type": "manual_checkout", "confirmed": True,
        "extras_complete": True,
        "discounts_complete": True,
        "charges": [
            {"id": "tax", "kind": "tax", "label": "税费",
             "amount": tax, "included": False, "selected": True, "mandatory": True},
            {"id": "insurance", "kind": "insurance", "label": "当前绑定保险",
             "amount": insurance, "included": False, "selected": True, "mandatory": False},
            {"id": "service", "kind": "service", "label": "服务费",
             "amount": service, "included": False, "selected": True, "mandatory": True},
        ],
    })


def _row(price=450, code="CA1234", quote=None, platform="ctrip"):
    record = {
        "price": price, "name": code, "code": code, "_platform": platform,
        "depTime": "10:00", "arrTime": "12:00", "depDate": DATE, "arrDate": DATE,
    }
    if quote is not None:
        record["quote"] = quote
    return record


def _price(rows, platform="ctrip"):
    return FlightPrice(
        platform=platform, from_city="CKG", to_city="BJS", depart_date=DATE,
        price=min(row["price"] for row in rows),
        extra=json.dumps(rows, ensure_ascii=False),
    )


class _Notifier:
    def __init__(self):
        self.messages = []

    def send(self, title, body, **kwargs):
        self.messages.append((title, body, kwargs))
        return True


class _Storage(PriceStorage):
    def __init__(self, path):
        super().__init__(str(path))
        self.writes = []

    def set_alert_state(self, key, price):
        self.writes.append(("set", key, price))
        return super().set_alert_state(key, price)

    def clear_alert_state(self, key):
        self.writes.append(("clear", key))
        return super().clear_alert_state(key)


@pytest.fixture
def setup_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_RENDER_DIR", str(tmp_path / "renders"))
    store = _Storage(tmp_path / "prices.db")
    notifier = _Notifier()
    route = Route("CKG", "重庆", "BJS", "北京", [DATE],
                  alert_threshold=600, alert_direct=600)
    alert = Alerter(logging.getLogger("final-price-test"), notifier=notifier,
                    storage=store, storm_repeat=1, user="test")
    return alert, route, notifier, store


@pytest.mark.parametrize("digest", [False, True])
def test_unknown_fees_do_not_notify_or_change_alert_state(setup_alert, digest):
    alert, route, notifier, store = setup_alert
    alert.digest = digest
    key = f"test|CKG-BJS-{DATE}"
    store.set_alert_state(key, 550)
    store.writes.clear()
    row = _row()
    row["tax_included"] = True
    price = _price([row])
    alert.check_multi([(route, [price])])
    assert notifier.messages == []
    assert store.writes == []
    assert store.get_alert_state(key) == 550
    assert json.loads(price.extra)[0]["price"] == 450


def test_complete_final_price_controls_legacy_threshold(setup_alert):
    alert, route, notifier, store = setup_alert
    route.alert_direct = 0
    route.alert_threshold = 520
    price = _price([_row(quote=_verified())])
    alert.check_and_alert(route, [price])
    assert notifier.messages == []
    route.alert_threshold = 540
    store.writes.clear()
    alert.check_and_alert(route, [price])
    assert len(notifier.messages) == 1
    assert "￥530" in notifier.messages[0][0]
    assert "最终应付价￥530" in notifier.messages[0][1]
    assert store.get_alert_state(alert_state_key(f"test|CKG-BJS-{DATE}")) == 530
    assert price.price == 450


def test_second_quote_can_be_lowest_confirmed_and_market_is_preserved(setup_alert):
    alert, route, _notifier, _store = setup_alert
    rows = [_row(420, "CA1111"),
            _row(450, "CA2222", quote=_verified())]
    price = _price(rows)
    sections = alert._build_sections(route, [price])
    assert sections[0]["best_direct"]["code"] == "CA2222"
    assert sections[0]["best_direct"]["price"] == 530
    assert [row["price"] for row in sections[0]["all_flights"]] == [420, 450]
    assert flight_reminder_price(price) == 530
    assert len(alert._collect_hits([(route, sections)])) == 1


def test_explicit_quote_mode_notifies_with_unconfirmed_fee_notice(setup_alert):
    _alert, route, notifier, store = setup_alert
    alert = Alerter(logging.getLogger("quote-mode"), notifier=notifier,
                    storage=store, storm_repeat=1, user="test",
                    require_final_prices=False)
    route.alert_threshold = 0
    alert.check_multi([(route, [_price([_row()])])])
    assert len(notifier.messages) == 1
    assert "来源报价" in notifier.messages[0][1]
    assert "最终费用未确认" in notifier.messages[0][1]


def test_same_quote_on_other_channel_does_not_borrow_checkout_proof():
    verified = _row(quote=_verified(), platform="ctrip")
    unknown = _row(platform="fliggy")
    assert len(_dedup_tie([verified, unknown])) == 2
    route = Route("CKG", "重庆", "BJS", "北京", [DATE],
                  transfer_baggage="direct", transfer_arrival_max="23:59")
    legacy_group = {
        **verified, "transCity": "天津", "layoverM": 120,
        "transferBaggage": "", "_tie_rows": [("fliggy", "direct", 120)],
    }
    assert not _tie_any_ok(legacy_group, 600, route, True)
    legacy_group["_require_final_prices"] = False
    assert _tie_any_ok(legacy_group, 600, route, True)


def test_result_flags_without_complete_cost_input_are_not_trusted():
    record = _row(quote={
        "quoted_amount": 450, "basis": "tax_included",
        "payable_total": 10, "complete": True,
    })
    assert reminder_price(record) is None


@pytest.mark.parametrize("bad", ["false", 0, None, []])
def test_bad_policy_is_an_error(bad):
    with pytest.raises(ValueError, match="布尔值"):
        require_final_for_alerts({"pricing": {"require_final_for_alerts": bad}})


def test_notification_preserves_cents_and_terminal_at(setup_alert):
    alert, _route, _notifier, _store = setup_alert
    row = _row(450.01, quote=_verified(450.01, 50.02, 20.03, 10.04))
    text = alert._notification_text("正文\n\n@123 ", [row])
    assert "最终应付价￥530.1" in text
    assert text.rstrip().endswith("@123")


def test_price_policy_uses_separate_alert_baselines(setup_alert):
    alert, route, notifier, store = setup_alert
    route.alert_direct = 0
    old_key = f"test|CKG-BJS-{DATE}"
    store.set_alert_state(old_key, 450)
    store.writes.clear()
    alert.check_and_alert(route, [_price([_row(quote=_verified())])])
    assert len(notifier.messages) == 1
    assert store.get_alert_state(old_key) == 450
    assert store.get_alert_state(alert_state_key(old_key)) == 530
    assert alert_state_key(old_key, False) == old_key


def test_phone_state_uses_final_namespace_and_keeps_quoted_baseline(setup_alert):
    alert, route, _notifier, store = setup_alert
    old_key = f"test|phone|CKG-BJS-{DATE}-直飞"
    store.set_alert_state(old_key, 450)
    row = _row(quote=_verified())
    hit = (route, {"date": DATE}, "直飞", row, 600)
    assert alert._phone_should_ring(hit)
    assert store.get_alert_state(old_key) == 450
    assert store.get_alert_state(alert_state_key(old_key)) == 530
    assert not alert._phone_should_ring(hit)


def test_unknown_price_preview_does_not_display_nonfinite_amounts(setup_alert):
    alert, route, _notifier, _store = setup_alert
    sections = alert._build_sections(route, [_price([_row()])])
    payload = alert._digest_payload([(route, sections)], with_tables=False)
    assert "450" in alert._top3_blocks(route, sections)
    assert "inf" not in payload["desp"].lower()
    assert "nan" not in payload["desp"].lower()
