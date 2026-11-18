"""最终价的费用完整性、金额运算和来源字段回归。"""
import copy
import json
from datetime import date, timedelta

import pytest

from core.pricequote import build_quote, quote_from_record, quote_sort_key
from core.special_radar import _pick_lowest, normalize, parse_query
from core.specials import SpecialDeal
from crawlers.ctrip import CtripCrawler
from crawlers.tongcheng import TongchengCrawler
from crawlers.tuniu import TuniuCrawler


def charge(amount, *, kind="tax", charge_id=None, label=None, included=False,
           selected=True, mandatory=True):
    return {
        "id": charge_id or kind,
        "kind": kind,
        "label": label or {"tax": "税费", "insurance": "保险",
                           "service": "服务费", "other": "其他费用"}[kind],
        "amount": amount,
        "included": included,
        "selected": selected,
        "mandatory": mandatory,
    }


def checkout(*charges, amount=450, basis="bare", **fields):
    payload = {
        "quoted_amount": amount,
        "basis": basis,
        "source_type": "manual_checkout",
        "charges": list(charges),
        "extras_complete": True,
        "discounts_complete": True,
        "confirmed": True,
    }
    payload.update(fields)
    return payload


def test_source_bare_price_does_not_become_final_price():
    quote = quote_from_record({"price": 450, "basis": "bare"})
    assert quote["quoted_amount"] == 450
    assert quote["payable_total"] is None
    assert quote["complete"] is False
    assert quote["charges"][0]["amount"] is None
    assert quote["charges"][0]["included"] is False
    assert "税费" in quote["missing"]
    assert "保险/服务费等附加费用" in quote["missing"]


def test_tax_included_list_quote_still_needs_extra_costs():
    quote = quote_from_record({"price": 500, "tax_included": True})
    assert quote["basis"] == "tax_included"
    assert quote["charges"][0]["included"] is True
    assert quote["charges"][0]["amount"] is None
    assert quote["payable_total"] is None
    assert "保险/服务费等附加费用" in quote["missing"]


def test_intl_flag_does_not_prove_tax_or_final_price():
    quote = quote_from_record({"price": 500, "intl": 1})
    assert quote["basis"] == "unknown"
    assert quote["payable_total"] is None
    assert "报价是否含税" in quote["missing"]


def test_explicit_base_and_tax_keep_their_actual_breakdown():
    quote = quote_from_record({"price": 520, "barePrice": 450, "tax": 70})
    assert quote["basis"] == "tax_included"
    assert quote["charges"][0]["amount"] == 70
    assert quote["charges"][0]["included"] is True
    assert quote["payable_total"] is None


def test_all_bound_costs_are_counted():
    quote = build_quote(checkout(
        charge(50),
        charge(30, kind="insurance", mandatory=False),
        charge(20, kind="service"),
        charge(8, kind="other"),
    ))
    assert quote["payable_total"] == 558
    assert quote["complete"] is True
    assert quote["source_type"] == "manual_checkout"
    assert quote["missing"] == []


def test_already_included_costs_are_not_counted_twice():
    quote = build_quote(checkout(
        charge(50, included=True),
        charge(30, kind="insurance", included=True, mandatory=False),
        charge(20, kind="service"),
        amount=530,
        basis="tax_included",
    ))
    assert quote["payable_total"] == 550


def test_included_tax_can_have_unknown_breakdown_amount():
    quote = build_quote(checkout(
        charge(None, included=True),
        charge(20, kind="service"),
        amount=500,
        basis="tax_included",
    ))
    assert quote["complete"] is True
    assert quote["payable_total"] == 520
    assert quote["charges"][0]["amount"] is None


def test_unselected_optional_extra_is_kept_without_charging():
    quote = build_quote(checkout(
        charge(50),
        charge(None, kind="insurance", selected=False, mandatory=False),
    ))
    assert quote["payable_total"] == 500
    assert quote["charges"][1]["selected"] is False
    assert quote["charges"][1]["amount"] is None


def test_included_extra_cancellation_requires_an_updated_quote():
    with pytest.raises(ValueError, match="取消后请重新核对报价"):
        build_quote(checkout(
            charge(50),
            charge(30, kind="insurance", included=True,
                   selected=False, mandatory=False),
        ))


def test_cancellation_with_unknown_inclusion_does_not_confirm_old_quote():
    quote = build_quote(checkout(
        charge(50),
        charge(None, kind="insurance", included=None,
               selected=False, mandatory=False),
    ))
    assert quote["payable_total"] is None
    assert "保险取消后的报价" in quote["missing"]


def test_mandatory_extra_cannot_be_unselected():
    with pytest.raises(ValueError, match="必选费用"):
        build_quote(checkout(
            charge(50),
            charge(20, kind="service", selected=False),
        ))


def test_explicit_zero_costs_are_distinct_from_missing_values():
    quote = build_quote(checkout(
        charge(0),
        charge(0, kind="insurance", mandatory=False),
        charge(0, kind="service"),
    ))
    assert quote["complete"] is True
    assert quote["payable_total"] == 450
    assert [item["amount"] for item in quote["charges"]] == [0, 0, 0]


def test_selected_unknown_extra_keeps_final_price_unconfirmed():
    quote = build_quote(checkout(
        charge(50), charge(None, kind="service"),
    ))
    assert quote["payable_total"] is None
    assert "服务费" in quote["missing"]


def test_unknown_inclusion_is_not_assumed_to_be_zero_or_excluded():
    quote = build_quote(checkout(charge(50, included=None)))
    assert quote["complete"] is False
    assert "税费是否已含" in quote["missing"]


@pytest.mark.parametrize("basis,included", [
    ("bare", True), ("tax_included", False),
])
def test_tax_inclusion_cannot_contradict_the_quote_basis(basis, included):
    with pytest.raises(ValueError, match="报价口径不一致"):
        build_quote(checkout(charge(50, included=included), basis=basis))


def test_unknown_basis_never_confirms_a_total():
    quote = build_quote(checkout(charge(50), basis="unknown"))
    assert quote["payable_total"] is None
    assert "报价是否含税" in quote["missing"]


@pytest.mark.parametrize("fields", [
    {"extras_complete": False},
    {"discounts_complete": False},
    {"confirmed": False},
    {"source_type": "list"},
])
def test_missing_disclosure_or_checkout_confirmation_blocks_final_total(fields):
    quote = build_quote(checkout(charge(50), **fields))
    assert quote["complete"] is False
    assert quote["payable_total"] is None


def test_component_sums_use_cent_precision():
    quote = build_quote(checkout(
        charge("50.01"),
        charge("0.10", kind="insurance", mandatory=False),
        charge("0.20", kind="service"),
        amount="449.99",
    ))
    assert quote["payable_total"] == 500.30


@pytest.mark.parametrize("amount", [
    None, True, False, -1, float("nan"), float("inf"), float("-inf"),
    "NaN", "Infinity", "", "bad", "450.001", [],
])
def test_invalid_source_amounts_fail_explicitly(amount):
    with pytest.raises(ValueError):
        build_quote(checkout(charge(50), amount=amount))


@pytest.mark.parametrize("amount", [
    True, -1, float("nan"), float("inf"), "", "bad", "1.001",
])
def test_invalid_extra_amounts_fail_explicitly(amount):
    with pytest.raises(ValueError):
        build_quote(checkout(charge(50), charge(amount, kind="service")))


def test_duplicate_cost_identifiers_fail_explicitly():
    with pytest.raises(ValueError, match="费用id重复"):
        build_quote(checkout(
            charge(50), charge(20, kind="service"),
            charge(10, kind="service"),
        ))


@pytest.mark.parametrize("field", ["amount", "included", "selected", "mandatory"])
def test_missing_cost_fields_fail_explicitly(field):
    item = charge(50)
    del item[field]
    with pytest.raises(ValueError, match="费用项目缺少"):
        build_quote(checkout(item))


def test_partial_result_flags_cannot_forge_a_confirmed_price():
    with pytest.raises(ValueError):
        quote_from_record({"price": 450, "quote": {
            "complete": True, "payable_total": 450,
        }})


def test_build_quote_does_not_mutate_input():
    payload = checkout(charge(50), charge(20, kind="service"))
    before = copy.deepcopy(payload)
    quote = build_quote(payload)
    quote["charges"][0]["label"] = "changed"
    assert payload == before


def test_confirmed_totals_and_same_basis_comparison():
    confirmed = build_quote(checkout(charge(50), amount=1000))
    assert quote_sort_key(confirmed) < quote_sort_key(
        {"price": 300, "basis": "tax_included"})
    assert quote_sort_key({"price": 550, "basis": "tax_included"}) < quote_sort_key(
        {"price": 450, "basis": "bare"})
    assert quote_sort_key({"price": 450, "basis": "bare"}) < quote_sort_key(
        {"price": 480, "basis": "bare"})


_DAY = "2026-12-01"


def intl_list(*policies):
    return json.dumps({"data": {"flightItineraryList": [{
        "flightSegments": [{"duration": 180, "flightList": [{
            "flightNo": "NH960", "departureAirportCode": "CKG",
            "arrivalAirportCode": "NRT",
            "departureDateTime": _DAY + " 08:00:00",
            "arrivalDateTime": _DAY + " 12:00:00",
        }]}],
        "priceList": list(policies),
    }]}})


def test_ctrip_missing_tax_keeps_bare_price_and_unknown_tax():
    rows, _ = CtripCrawler._extract_intl_list(
        intl_list({"adultPrice": 450}), _DAY)
    assert rows[0]["price"] == 450
    assert rows[0]["barePrice"] == 450
    assert rows[0]["tax"] is None
    assert rows[0]["basis"] == "bare"
    assert rows[0]["quote"]["payable_total"] is None


def test_ctrip_explicit_zero_tax_is_not_missing():
    rows, _ = CtripCrawler._extract_intl_list(
        intl_list({"adultPrice": 450, "adultTax": 0}), _DAY)
    assert rows[0]["tax"] == 0
    assert rows[0]["tax_included"] is True
    assert rows[0]["quote"]["charges"][0]["amount"] == 0


def test_ctrip_tax_only_policy_cannot_invent_an_adult_fare():
    rows, _ = CtripCrawler._extract_intl_list(
        intl_list({"adultTax": 450}), _DAY)
    assert rows == []


def test_ctrip_does_not_compare_bare_policy_against_included_policy():
    rows, _ = CtripCrawler._extract_intl_list(intl_list(
        {"adultPrice": 450},
        {"adultPrice": 500, "adultTax": 50},
    ), _DAY)
    assert rows[0]["price"] == 550
    assert rows[0]["basis"] == "tax_included"


def calendar_row(**fields):
    epoch = 1796054400000
    row = {"departDate": f"/Date({epoch}+0800)/", "price": 500}
    row.update(fields)
    return json.dumps({"priceList": [row]})


def test_ctrip_calendar_missing_total_does_not_claim_tax_included():
    rows = CtripCrawler._extract_intl_calendar(
        calendar_row(transportPrice=50), _DAY)
    assert rows[0]["price"] == 500
    assert rows[0]["tax"] is None
    assert rows[0]["tax_included"] is None
    assert rows[0]["basis"] == "unknown"
    assert rows[0]["transportPrice"] == 50
    assert rows[0]["quote"]["payable_total"] is None


def test_ctrip_calendar_preserves_transport_price_without_guessing_tax():
    rows = CtripCrawler._extract_intl_calendar(
        calendar_row(totalPrice=550, transportPrice=50), _DAY)
    assert rows[0]["price"] == 550
    assert rows[0]["tax"] is None
    assert rows[0]["transportPrice"] == 50
    assert rows[0]["basis"] == "tax_included"
    assert rows[0]["quote"]["payable_total"] is None


def test_ctrip_zero_total_does_not_fall_back_to_nonzero_price():
    assert CtripCrawler._extract_intl_calendar(
        calendar_row(totalPrice=0), _DAY) == []


def test_tongcheng_missing_bare_price_does_not_invent_zero_tax():
    body = json.dumps({"data": {"RD": [{"DD": _DAY, "TP": 500}]}})
    assert TongchengCrawler._parse_intl_calendar(body, _DAY) == (
        500, None, False,
    )


def test_tongcheng_explicit_equal_subtotal_confirms_zero_tax():
    body = json.dumps({"data": {"RD": [{"DD": _DAY, "TP": 500, "P": 500}]}})
    assert TongchengCrawler._parse_intl_calendar(body, _DAY) == (
        500, 0, False,
    )


def test_tongcheng_conflicting_breakdown_keeps_tax_unknown(caplog):
    body = json.dumps({"data": {"RD": [{"DD": _DAY, "TP": 500, "P": 600}]}})
    assert TongchengCrawler._parse_intl_calendar(body, _DAY) == (
        500, None, False,
    )
    assert "无法确认税费拆分" in caplog.text


def tuniu_row(**fields):
    row = {"avgTotalPrice": 500, "flightNos": "MU523",
           "dTime": _DAY + " 08:00", "aTime": _DAY + " 12:00"}
    row.update(fields)
    return {"productList": [row]}


def test_tuniu_missing_tax_split_is_unknown_but_total_source_is_preserved():
    crawler = object.__new__(TuniuCrawler)
    rows = crawler._parse_offers_intl(tuniu_row())
    assert rows[0]["tax"] is None
    assert rows[0]["barePrice"] is None
    assert rows[0]["basis"] == "tax_included"
    assert rows[0]["price_source"] == "avgTotalPrice"
    assert rows[0]["quote"]["payable_total"] is None


def test_tuniu_explicit_zero_tax_remains_zero():
    crawler = object.__new__(TuniuCrawler)
    rows = crawler._parse_offers_intl(tuniu_row(avgTax=0, avgBasePrice=500))
    assert rows[0]["tax"] == 0
    assert rows[0]["barePrice"] == 500
    assert rows[0]["quote"]["charges"][0]["amount"] == 0


def test_radar_daily_best_uses_same_basis_and_keeps_final_price_unconfirmed():
    day = (date.today() + timedelta(days=5)).isoformat()
    query = parse_query({"from": "CKG", "to": "BJS", "start": day,
                         "end": day, "channels": ["ctrip", "tuniu"]})
    rows = normalize([
        SpecialDeal("ctripbd", "CKG", "BJS", day, 450,
                    price_basis="bare", tax_included=False),
        SpecialDeal("ctrip", "CKG", "BJS", day, 550,
                    price_basis="tax_included", tax_included=True),
        SpecialDeal("tuniucal", "CKG", "BJS", day, 480,
                    price_basis="bare", tax_included=False),
    ], query)
    assert rows[0]["by_channel"]["ctrip"]["price"] == 550
    assert rows[0]["best"]["channel"] == "ctrip"
    assert rows[0]["basis_mixed"] is True
    assert rows[0]["best"]["quote"]["payable_total"] is None
    lowest, mixed = _pick_lowest(rows)
    assert mixed is True
    assert lowest["price"] == 550
    assert lowest["lowest_basis_label"] == "含税报价"
    assert lowest["quote"]["payable_total"] is None
    assert "最终价未确认" in lowest["note"]
