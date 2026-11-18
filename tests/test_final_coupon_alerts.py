"""优惠后提醒预算、零元订单及来源行情保留。"""
import copy
import json
import logging

import pytest

from core.alerter import Alerter
from core.final_alerts import alert_state_key, reminder_price
from core.models import FlightPrice, Route
from core.pricequote import build_quote
from core.storage import PriceStorage

DATE = "2026-12-01"


def _quote(price=500, tax=50, discount=100, **fields):
    payload = {
        "quoted_amount": price, "basis": "bare", "source_type": "manual_checkout",
        "extras_complete": True, "discounts_complete": True, "confirmed": True,
        "charges": [
            {"id": "tax", "kind": "tax", "label": "税费", "amount": tax,
             "included": False, "selected": True, "mandatory": True},
            {"id": "coupon", "kind": "discount", "label": "本单优惠",
             "amount": discount, "included": False, "selected": True,
             "mandatory": False, "applicable": True, "applied": True},
        ],
    }
    return build_quote({**payload, **fields})


def _price(quote=None, price=500, platform="ctrip", **fields):
    row = {
        "price": price, "code": "CA1234", "name": "CA1234",
        "depDate": DATE, "arrDate": DATE, "depTime": "10:00", "arrTime": "12:00",
        **fields,
    }
    if quote is not None:
        row["quote"] = quote
    return FlightPrice(platform, "CKG", "BJS", DATE, price, flight_no=row["code"],
                       depart_time=row["depTime"], arrive_time=row["arrTime"],
                       extra=json.dumps([row], ensure_ascii=False))


class _Notifier:
    def __init__(self):
        self.messages = []

    def send(self, title, body, **kwargs):
        self.messages.append((title, body, kwargs))
        return True


@pytest.fixture
def setup_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_RENDER_DIR", str(tmp_path / "renders"))
    store = PriceStorage(str(tmp_path / "prices.db"))
    notifier = _Notifier()
    alert = Alerter(logging.getLogger("coupon-alert-test"), notifier=notifier,
                    storage=store, user="coupon-test", storm_repeat=1)
    route = Route("CKG", "重庆", "BJS", "北京", [DATE],
                  alert_direct=460, price_max=450)
    return alert, route, notifier, store


def test_coupon_can_make_a_source_quote_above_the_budget_eligible(setup_alert):
    alert, route, _notifier, _store = setup_alert
    price = _price(_quote())
    before = copy.deepcopy(price)
    sections = alert._build_sections(route, [price])
    best = sections[0]["best_direct"]
    assert best is not None
    assert best["price"] == 450
    assert best["_reminder_view"] is True
    assert best["_require_final_prices"] is True
    assert best["_quoted_price"] == 500
    assert sections[0]["all_flights"][0]["price"] == 500
    assert sections[0]["platform_mins"]["ctrip"] == 500
    assert price == before
    hits = alert._collect_hits([(route, sections)])
    assert len(hits) == 1
    assert hits[0][3]["price"] == 450
    assert reminder_price(hits[0][3]) == 450


def test_reminder_view_is_idempotent_without_reapplying_fees_or_coupons(setup_alert):
    alert, _route, _notifier, _store = setup_alert
    row = json.loads(_price(_quote()).extra)[0]
    first = alert._alert_row(row)
    second = alert._alert_row(first)
    assert second == first
    assert second["price"] == 450
    assert second["_quoted_price"] == 500
    assert row["price"] == 500


def test_final_price_above_budget_does_not_become_a_candidate(setup_alert):
    alert, route, notifier, _store = setup_alert
    route.price_max = 500
    route.alert_direct = 600
    price = _price(_quote(price=450, tax=80, discount=0), price=450)
    sections = alert._build_sections(route, [price])
    assert sections[0]["best_direct"] is None
    assert alert._collect_hits([(route, sections)]) == []
    alert.check_and_alert(route, [price])
    assert notifier.messages == []


def test_unknown_fees_do_not_supply_a_reminder_candidate(setup_alert):
    alert, route, notifier, _store = setup_alert
    route.price_max = 600
    route.alert_direct = 600
    price = _price(_quote(tax=None))
    sections = alert._build_sections(route, [price])
    assert sections[0]["best_direct"] is None
    assert alert._collect_hits([(route, sections)]) == []
    alert.check_and_alert(route, [price])
    assert notifier.messages == []


@pytest.mark.parametrize("payable", [70, 0])
def test_low_and_zero_coupon_totals_are_valid_candidates_and_notifications(setup_alert, payable):
    alert, route, notifier, store = setup_alert
    route.price_max = 70
    route.alert_direct = 100
    price = _price(_quote(discount=550 - payable))
    store.save(price)
    sections = alert._build_sections(route, [price])
    assert sections[0]["best_direct"]["price"] == payable
    hits = alert._collect_hits([(route, sections)])
    assert len(hits) == 1
    assert hits[0][3]["price"] == payable
    alert.check_and_alert(route, [price])
    assert len(notifier.messages) == 1
    assert f"最终应付价￥{payable}" in notifier.messages[0][1]
    key = alert_state_key(f"coupon-test|CKG-BJS-{DATE}-direct")
    assert store.get_alert_state(key) == payable
    assert price.price == 500
    assert json.loads(price.extra)[0]["price"] == 500
    assert store.latest_prices()[0]["price"] == 500


def test_legacy_threshold_applies_the_same_coupon_budget(setup_alert):
    alert, route, notifier, store = setup_alert
    route.alert_direct = 0
    route.alert_threshold = 460
    price = _price(_quote())
    alert.check_and_alert(route, [price])
    assert len(notifier.messages) == 1
    assert "最终应付价￥450" in notifier.messages[0][1]
    key = alert_state_key(f"coupon-test|CKG-BJS-{DATE}")
    assert store.get_alert_state(key) == 450
    assert price.price == 500


def test_legacy_threshold_rejects_final_price_above_budget(setup_alert):
    alert, route, notifier, _store = setup_alert
    route.alert_direct = 0
    route.alert_threshold = 600
    route.price_max = 500
    price = _price(_quote(price=450, tax=80, discount=0), price=450)
    alert.check_and_alert(route, [price])
    assert notifier.messages == []


@pytest.mark.parametrize("payable", [70, 0])
def test_legacy_threshold_does_not_treat_coupon_reduction_as_a_channel_anomaly(setup_alert, payable):
    alert, route, notifier, _store = setup_alert
    route.alert_direct = 0
    route.alert_threshold = 100
    route.price_max = 0
    discounted = _price(_quote(discount=550 - payable))
    other = _price(_quote(price=800, discount=0), price=800, platform="tuniu")
    alert.check_and_alert(route, [discounted, other])
    assert len(notifier.messages) == 1
    assert f"最终应付价￥{payable}" in notifier.messages[0][1]
    assert discounted.price == 500
    assert other.price == 800


def test_explicit_quote_mode_uses_source_price_for_budget_and_target(setup_alert):
    alert, route, notifier, _store = setup_alert
    alert.require_final_prices = False
    route.price_max = 500
    route.alert_direct = 460
    price = _price(_quote(price=450, tax=80, discount=0), price=450)
    sections = alert._build_sections(route, [price])
    assert sections[0]["best_direct"]["price"] == 450
    assert sections[0]["best_direct"]["_require_final_prices"] is False
    assert len(alert._collect_hits([(route, sections)])) == 1
    alert.check_and_alert(route, [price])
    assert len(notifier.messages) == 1
    assert "来源裸价￥450" in notifier.messages[0][1]
    assert "已核最终价￥530" in notifier.messages[0][1]


def test_explicit_quote_mode_does_not_use_a_coupon_to_meet_the_source_budget(setup_alert):
    alert, route, notifier, _store = setup_alert
    alert.require_final_prices = False
    price = _price(_quote())
    sections = alert._build_sections(route, [price])
    assert sections[0]["best_direct"] is None
    alert.check_and_alert(route, [price])
    assert notifier.messages == []


def test_explicit_quote_threshold_mode_restores_unconfirmed_source_price(setup_alert):
    alert, route, notifier, _store = setup_alert
    alert.require_final_prices = False
    route.alert_direct = 0
    route.alert_threshold = 510
    route.price_max = 500
    price = _price()
    alert.check_and_alert(route, [price])
    assert len(notifier.messages) == 1
    assert "来源报价" in notifier.messages[0][1]
    assert "最终费用未确认" in notifier.messages[0][1]
    assert price.price == 500
