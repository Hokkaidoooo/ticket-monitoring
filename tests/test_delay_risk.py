"""延误估算的传导、天气方向及单档保险经济边界。"""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal

import pytest

from core.delay_risk import estimate_delay, evaluate_insurance, normalize_parameters


def flight(**changes):
    return {
        "flight_no": "CA1430", "flight_id": "target", "departure_airport": "CKG",
        "arrival_airport": "PEK", "scheduled_departure": "2026-11-19T10:00:00+08:00",
        "scheduled_arrival": "2026-11-19T12:00:00+08:00", "estimated_departure": None,
        "estimated_arrival": None, "actual_departure": None, "actual_arrival": None,
        "actual_takeoff": None, "status": "scheduled", "cancelled": False, **changes,
    }


def previous(**changes):
    inbound = flight(flight_no="CA1429", flight_id="inbound", departure_airport="PEK",
                     arrival_airport="CKG", scheduled_departure="2026-11-19T07:00:00+08:00",
                     scheduled_arrival="2026-11-19T09:00:00+08:00",
                     actual_arrival="2026-11-19T08:50:00+08:00", status="arrived", **changes)
    return {"status": "available", "reason": "", "flight": inbound,
            "relation_source": "inboundFlight"}


def weather(airport="CKG", code=0, rain=0, wind=10, gust=15, visibility=10000):
    return {"airport": airport, "source": "test forecast",
            "fetched_at": "2026-11-18T16:00:00+08:00", "hours": [{
                "time": "2026-11-19T10:00:00+08:00", "weather_code": code,
                "precipitation_mm": rain, "wind_speed_kmh": wind,
                "wind_gusts_kmh": gust, "visibility_m": visibility,
            }]}


def history(samples=None):
    arrivals = [-8, -10, -2, 0, 3, 6, 10, 14, 16, 18, 45, 130] if samples is None else samples
    return {"arrival_delays_minutes": arrivals, "departure_delays_minutes": []}


def estimate(**changes):
    inputs = {"flight": flight(), "previous": previous(), "historical": history(),
              "weather": [weather(), weather("PEK")], **changes}
    return estimate_delay(**inputs)


def insurance(probability=0.2, premium=20, payout=200, policy_status="eligible", **changes):
    arguments = {"premium": premium, "payout": payout, "policy_status": policy_status,
                 "flight": flight(), "now": datetime.fromisoformat("2026-11-19T09:00:00+08:00"),
                 **changes}
    return evaluate_insurance({"delay_probability": probability}, **arguments)


def test_prior_is_explicit_when_no_actual_history_exists():
    result = estimate(historical=history([]))
    assert result["sample_count"] == 0
    assert result["confidence"] == "low"
    assert result["prior_used"] is True
    assert any("尚无" in text for text in result["assumptions"])
    assert 0 < result["delay_probability"] < 0.1


def test_weather_severity_raises_probability():
    probabilities = [estimate(weather=[weather(code=code), weather("PEK")])["delay_probability"]
                     for code in (0, 61, 63, 95)]
    assert probabilities == sorted(probabilities)
    assert probabilities[-1] > probabilities[0]


def test_larger_claim_threshold_cannot_raise_probability():
    probabilities = [estimate(threshold_minutes=threshold, weather=[weather(code=95)])["delay_probability"]
                     for threshold in (30, 60, 120, 180, 240)]
    assert probabilities == sorted(probabilities, reverse=True)


def test_same_storm_at_two_airports_is_not_added_twice():
    single = estimate(weather=[weather(code=95)])["delay_probability"]
    double = estimate(weather=[weather(code=95), weather("PEK", code=95)])["delay_probability"]
    assert double == single


def test_history_with_more_long_delays_raises_probability():
    low = estimate(historical=history([0] * 12))["delay_probability"]
    high = estimate(historical=history([180] * 12))["delay_probability"]
    assert high > low


def test_actual_inbound_arrival_takes_priority_over_estimate():
    prev = previous(estimated_arrival="2026-11-19T12:00:00+08:00")
    result = estimate(previous=prev)
    assert result["propagated_delay_minutes"] == 0


def test_current_and_inbound_delays_take_max_instead_of_sum():
    prev = previous()
    prev["flight"]["actual_arrival"] = "2026-11-19T10:45:00+08:00"
    result = estimate(flight=flight(estimated_departure="2026-11-19T11:00:00+08:00"), previous=prev)
    assert result["propagated_delay_minutes"] == 75


def test_arrival_estimate_is_not_recovered_twice():
    result = estimate(flight=flight(estimated_departure="2026-11-19T11:00:00+08:00",
                                   estimated_arrival="2026-11-19T13:30:00+08:00"))
    assert result["propagated_delay_minutes"] == 90


def test_departure_basis_does_not_subtract_airborne_recovery():
    result = estimate(flight=flight(estimated_departure="2026-11-19T11:00:00+08:00"),
                      claim_basis="departure", recovery_minutes=60)
    assert result["propagated_delay_minutes"] == 60
    assert result["sample_count"] == 0


def test_unpublished_previous_is_visible_and_low_confidence():
    result = estimate(previous={"status": "unpublished", "reason": "同机前序尚未公布", "flight": None})
    assert result["confidence"] == "low"
    assert "同机前序尚未公布" in result["factors"]


def test_previous_without_arrival_times_keeps_low_confidence():
    prev = previous()
    prev["flight"]["actual_arrival"] = None
    result = estimate(previous=prev)
    assert result["confidence"] == "low"
    assert any("到达动态尚未" in text for text in result["factors"])


def test_sensitive_range_contains_point_and_is_labeled_as_scenario_range():
    result = estimate(weather=[weather(code=95)])
    low, high = result["probability_range"]
    assert 0 <= low <= result["delay_probability"] <= high <= 1
    assert any("不是统计置信区间" in text for text in result["assumptions"])


def test_inherited_extreme_delay_does_not_claim_certainty():
    result = estimate(flight=flight(estimated_arrival="2026-11-19T20:00:00+08:00"))
    assert 0.9 < result["delay_probability"] < 1


def test_inputs_are_not_modified():
    inputs = {"flight": flight(), "previous": previous(), "historical": history(),
              "weather": [weather(), weather("PEK")]}
    before = deepcopy(inputs)
    estimate_delay(**inputs)
    assert inputs == before


def test_fixed_payout_recoup_and_expected_value():
    result = insurance()
    assert result["break_even_probability"] == pytest.approx(0.1)
    assert result["recoup_probability"] == pytest.approx(0.2)
    assert result["expected_payout"] == 40
    assert result["expected_net"] == 20
    assert result["recommendation"]["code"] == "buy"


def test_negative_expected_value_suggests_skip():
    result = insurance(probability=0.04)
    assert result["expected_net"] == -12
    assert result["recommendation"]["code"] == "skip"


def test_equal_payout_and_premium_can_recoup_but_cannot_profit():
    result = insurance(premium=20, payout=20)
    assert result["break_even_probability"] == 1
    assert result["recoup_probability"] == 0.2
    assert result["profit_probability"] == 0
    assert result["expected_net"] == -16


def test_payout_below_cost_cannot_recoup_and_threshold_is_not_clipped():
    result = insurance(probability=0.9, premium=20, payout=10)
    assert result["break_even_probability"] == 2
    assert result["recoup_probability"] == 0
    assert result["expected_net"] == -11
    assert result["recommendation"]["code"] == "skip"


def test_free_insurance_recoups_without_guaranteeing_profit():
    result = insurance(premium=0, payout=100)
    assert result["recoup_probability"] == 1
    assert result["profit_probability"] == 0.2


def test_zero_payout_has_no_division_error_or_recoup_promise():
    result = insurance(payout=0)
    assert result["break_even_probability"] is None
    assert result["recoup_probability"] == 0
    assert result["expected_net"] == -20
    assert result["recommendation"]["code"] == "skip"


def test_unchecked_terms_preserve_conditional_economics():
    result = insurance(policy_status="unchecked")
    assert result["conditional"] is True
    assert result["expected_net"] == 20
    assert result["recommendation"]["code"] == "check_terms"


@pytest.mark.parametrize("premium,payout", [(None, None), (None, 200), (20, None)])
def test_missing_terms_preserve_delay_estimate_but_do_not_invent_economics(premium, payout):
    result = insurance(premium=premium, payout=payout)
    assert result["recoup_probability"] is None
    assert result["expected_net"] is None
    assert result["recommendation"]["code"] == "need_terms"


@pytest.mark.parametrize("changes", [
    {"actual_departure": "2026-11-19T08:59:00+08:00"},
    {"actual_takeoff": "2026-11-19T08:59:00+08:00"},
    {"actual_arrival": "2026-11-19T08:59:00+08:00"},
    {"status": "airborne"}, {"cancelled": True}, {"status": "Cancelled"},
])
def test_departed_or_cancelled_flight_never_suggests_buy(changes):
    result = insurance(probability=0.99, flight=flight(**changes))
    assert result["recommendation"]["code"] == "ineligible"
    assert result["recoup_probability"] == 0
    assert result["expected_payout"] == 0


def test_past_scheduled_departure_never_suggests_buy():
    result = insurance(probability=0.99, now=datetime.fromisoformat("2026-11-19T10:00:00+08:00"))
    assert result["recommendation"]["code"] == "ineligible"


def test_explicit_ineligibility_cannot_be_overridden_by_positive_expected_return():
    result = insurance(probability=0.99, policy_status="ineligible")
    assert result["recommendation"]["code"] == "ineligible"
    assert result["expected_net"] == -20


@pytest.mark.parametrize("bad", [None, [], {}, True, 95.0, "95", 97])
def test_invalid_weather_code_is_an_explicit_error(bad):
    item = weather()
    item["hours"][0]["weather_code"] = bad
    with pytest.raises(ValueError, match="天气代码"):
        estimate(weather=[item])


@pytest.mark.parametrize("field,bad", [("precipitation_mm", -1), ("wind_speed_kmh", float("nan")),
                                      ("visibility_m", -1), ("wind_gusts_kmh", True)])
def test_bad_weather_numbers_are_not_defaulted_to_clear_weather(field, bad):
    item = weather()
    item["hours"][0][field] = bad
    with pytest.raises(ValueError):
        estimate(weather=[item])


def test_missing_weather_cannot_generate_probability():
    with pytest.raises(ValueError, match="尚未取得机场天气"):
        estimate(weather=[])


def test_weather_requires_valid_airport_identity():
    with pytest.raises(ValueError, match="机场三字码"):
        estimate(weather=[weather(airport=True)])


@pytest.mark.parametrize("bad", [0, -1, 2881, True, 2.5, "120"])
def test_invalid_threshold_fails(bad):
    with pytest.raises(ValueError, match="延误门槛"):
        estimate(threshold_minutes=bad)


def test_parameter_normalization_defines_defaults_and_maximum():
    assert normalize_parameters() == {"threshold_minutes": 120, "claim_basis": "arrival",
                                      "turnaround_minutes": 45, "recovery_minutes": 15}
    assert normalize_parameters(threshold_minutes=2880)["threshold_minutes"] == 2880


@pytest.mark.parametrize("parameters", [{"claim_basis": "unknown"}, {"turnaround_minutes": -1}, {"turnaround_minutes": 0},
                                       {"turnaround_minutes": 181}, {"recovery_minutes": 121}])
def test_parameter_normalization_rejects_invalid_values_before_network_access(parameters):
    with pytest.raises(ValueError):
        normalize_parameters(**parameters)


def test_previous_relationship_requires_same_aircraft_evidence():
    prev = previous()
    prev["relation_source"] = "same_airline"
    with pytest.raises(ValueError, match="明确同机关系"):
        estimate(previous=prev)


def test_previous_destination_must_match_target_departure_airport():
    prev = previous()
    prev["flight"]["arrival_airport"] = "TFU"
    with pytest.raises(ValueError, match="出发机场不一致"):
        estimate(previous=prev)


@pytest.mark.parametrize("timestamp", ["2026-11-19T10:00:00", "invalid"])
def test_times_require_explicit_timezone(timestamp):
    with pytest.raises(ValueError):
        estimate(flight=flight(scheduled_departure=timestamp))


def test_bad_history_is_not_silently_discarded():
    with pytest.raises(ValueError, match="历史延误"):
        estimate(historical=history([0, float("nan")]))


@pytest.mark.parametrize("bad", [True, -1, float("nan"), Decimal("1.001")])
def test_invalid_money_reuses_existing_money_contract(bad):
    with pytest.raises(ValueError, match="保费"):
        insurance(premium=bad)


def test_insurance_requires_aware_current_time():
    with pytest.raises(ValueError, match="当前时间必须带时区"):
        insurance(now=datetime(2026, 11, 19, 9))


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("inf"), True])
def test_invalid_probability_cannot_generate_financial_result(bad):
    with pytest.raises(ValueError, match="延误概率"):
        insurance(probability=bad)
