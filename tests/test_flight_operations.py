"""自动航班查询的日期、身份、前序关系与真实时间口径。"""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from core.flight_operations import BASE_URL, FlightAwareProvider, FlightDataError

NOW = datetime(2026, 11, 18, 8, 30, tzinfo=UTC)
DEPARTURE = datetime(2026, 11, 18, 5, 15, tzinfo=UTC)


def airport(code):
    return {"iata": code, "TZ": ":Asia/Shanghai", "friendlyName": code}


def flight(ident="CCA1430", departure=DEPARTURE, origin="CKG", destination="PEK", flight_id=None):
    scheduled = int(departure.timestamp())
    return {
        "ident": ident, "displayIdent": ident,
        "flightId": flight_id or ident + "-" + str(scheduled),
        "origin": airport(origin), "destination": airport(destination),
        "gateDepartureTimes": {"scheduled": scheduled, "estimated": scheduled, "actual": None},
        "gateArrivalTimes": {"scheduled": scheduled + 9000, "estimated": scheduled + 9000, "actual": None},
        "takeoffTimes": {"scheduled": scheduled + 600, "estimated": scheduled + 600, "actual": None},
        "landingTimes": {"scheduled": scheduled + 8400, "estimated": scheduled + 8400, "actual": None},
        "aircraft": {"tail": None}, "cancelled": False, "flightStatus": "",
        "links": {"permanent": f"/live/flight/{ident}/history/{departure:%Y%m%d}/0525Z/ZUCK/ZBAA"},
        "inboundFlight": None,
    }


def page(rows):
    payload = {"flights": {row["flightId"]: row for row in rows}}
    return "<html><script>var trackpollBootstrap = " + json.dumps(payload) + ";</script></html>"


def provider(responses):
    requests = []

    def handle(request):
        requests.append(str(request.url))
        expected = responses.get(request.url.path)
        if expected is None:
            return httpx.Response(404, text="missing fixture")
        if isinstance(expected, httpx.Response):
            return expected
        return httpx.Response(200, text=page(expected))

    client = httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)
    return FlightAwareProvider(client), client, requests


def test_iata_is_converted_to_icao_and_source_dates_are_aware():
    query, client, calls = provider({"/live/flight/CCA1430": [flight()]})
    with client:
        result = query.fetch("ca1430", "2026-11-18", now=NOW)
    assert calls == [BASE_URL + "/live/flight/CCA1430"]
    assert result["flight"]["flight_no"] == "CA1430"
    assert result["flight"]["scheduled_departure"] == "2026-11-18T13:15:00+08:00"
    assert result["previous"]["status"] == "unpublished"
    assert result["previous"]["flight"] is None


@pytest.mark.parametrize("number", ["../CA1430", "CA1430?x=1", "https://evil.example", "ZZ123", None])
def test_invalid_ident_never_makes_network_request(number):
    query, client, calls = provider({})
    with client, pytest.raises(ValueError):
        query.fetch(number, "2026-11-18", now=NOW)
    assert not calls


@pytest.mark.parametrize("day", ["2026-02-30", "20261118", "2026-1-18", None])
def test_invalid_date_never_makes_network_request(day):
    query, client, calls = provider({})
    with client, pytest.raises(ValueError):
        query.fetch("CA1430", day, now=NOW)
    assert not calls


def test_icao_input_is_supported():
    query, client, _ = provider({"/live/flight/CCA1430": [flight()]})
    with client:
        result = query.fetch("CCA1430", "2026-11-18", now=NOW)
    assert result["flight"]["flight_no"] == "CA1430"


def test_requested_future_day_fetches_its_exact_detail():
    today = flight()
    tomorrow = flight(departure=DEPARTURE + timedelta(days=1))
    today["activityLog"] = {"flights": [tomorrow, deepcopy(today)]}
    link = tomorrow["links"]["permanent"]
    query, client, calls = provider({"/live/flight/CCA1430": [today], link: [tomorrow]})
    with client:
        result = query.fetch("CA1430", "2026-11-19", now=NOW)
    assert result["flight"]["scheduled_departure"].startswith("2026-11-19")
    assert calls[-1] == BASE_URL + link
    assert result["previous"]["status"] == "unpublished"


def test_missing_date_is_not_replaced_by_current_date():
    query, client, _ = provider({"/live/flight/CCA1430": [flight()]})
    with client, pytest.raises(FlightDataError, match="指定日期"):
        query.fetch("CA1430", "2026-11-19", now=NOW)


def test_unrelated_ident_is_rejected_even_when_date_and_airports_match():
    query, client, _ = provider({"/live/flight/CCA1430": [flight("CCA9999")]})
    with client, pytest.raises(FlightDataError, match="指定日期"):
        query.fetch("CA1430", "2026-11-18", now=NOW)


def test_airport_filter_and_ambiguous_same_day_segments():
    first = flight()
    second = flight(origin="PEK", destination="CKG", departure=DEPARTURE + timedelta(hours=3))
    first["activityLog"] = {"flights": [second]}
    link = second["links"]["permanent"]
    query, client, _ = provider({"/live/flight/CCA1430": [first], link: [second]})
    with client:
        with pytest.raises(FlightDataError, match="多个航段"):
            query.fetch("CA1430", "2026-11-18", now=NOW)
        selected = query.fetch("CA1430", "2026-11-18", departure_airport="PEK", arrival_airport="CKG", now=NOW)
    assert selected["flight"]["departure_airport"] == "PEK"


def test_city_code_cannot_silently_choose_a_beijing_airport():
    query, client, _ = provider({"/live/flight/CCA1430": [flight()]})
    with client, pytest.raises(FlightDataError, match="指定日期"):
        query.fetch("CA1430", "2026-11-18", arrival_airport="BJS", now=NOW)


def test_confirmed_inbound_is_followed_and_actual_arrival_is_preserved():
    main = flight()
    previous = flight("CCA1429", DEPARTURE - timedelta(hours=4), "PEK", "CKG")
    previous["gateArrivalTimes"]["actual"] = previous["gateArrivalTimes"]["scheduled"] - 600
    main["inboundFlight"] = {"flightId": previous["flightId"], "linkUrl": "/live/flight/id/confirmed-inbound"}
    query, client, _ = provider({"/live/flight/CCA1430": [main], "/live/flight/id/confirmed-inbound": [previous]})
    with client:
        result = query.fetch("CA1430", "2026-11-18", now=NOW)
    inbound = result["previous"]
    assert inbound["status"] == "available"
    assert inbound["relation_source"] == "inboundFlight"
    assert inbound["flight"]["flight_no"] == "CA1429"
    assert inbound["flight"]["actual_arrival"] is not None


def test_inbound_without_explicit_relation_is_not_inferred_from_nearby_flights():
    main = flight()
    main["activityLog"] = {"flights": [flight("CCA1429", DEPARTURE - timedelta(hours=4), "PEK", "CKG")]}
    query, client, calls = provider({"/live/flight/CCA1430": [main]})
    with client:
        result = query.fetch("CA1430", "2026-11-18", now=NOW)
    assert result["previous"]["status"] == "unpublished"
    assert len(calls) == 1


@pytest.mark.parametrize("mismatch", ["identity", "arrival_airport", "sequence"])
def test_inbound_mismatch_is_explicitly_unavailable(mismatch):
    main = flight()
    previous = flight("CCA1429", DEPARTURE - timedelta(hours=4), "PEK", "CKG")
    previous_id = previous["flightId"]
    main["inboundFlight"] = {"flightId": previous_id, "linkUrl": "/live/flight/id/inbound"}
    if mismatch == "identity":
        previous["flightId"] = "unrelated"
    elif mismatch == "arrival_airport":
        previous["destination"] = airport("CTU")
    else:
        previous["gateDepartureTimes"]["scheduled"] = int(DEPARTURE.timestamp())
    query, client, _ = provider({"/live/flight/CCA1430": [main], "/live/flight/id/inbound": [previous]})
    with client:
        result = query.fetch("CA1430", "2026-11-18", now=NOW)
    assert result["previous"]["status"] == "unavailable"
    assert result["previous"]["reason"]
    assert result["previous"]["flight"] is None


def test_failed_inbound_fetch_does_not_mask_the_reason():
    main = flight()
    main["inboundFlight"] = {"flightId": "inbound-1", "linkUrl": "/live/flight/id/inbound"}
    query, client, _ = provider({"/live/flight/CCA1430": [main], "/live/flight/id/inbound": httpx.Response(503)})
    with client:
        result = query.fetch("CA1430", "2026-11-18", now=NOW)
    assert result["previous"]["status"] == "unavailable"
    assert "503" in result["previous"]["reason"]


@pytest.mark.parametrize("link", ["https://evil.example/live/flight/id/1", "//evil.example/live/flight/id/1", "/live/flight/../1", "/live/flight/%2e%2e/1", "https://zh.flightaware.com:8080/live/flight/id/1"])
def test_remote_inbound_link_never_changes_host(link):
    main = flight()
    main["inboundFlight"] = {"flightId": "inbound-1", "linkUrl": link}
    query, client, calls = provider({"/live/flight/CCA1430": [main]})
    with client:
        result = query.fetch("CA1430", "2026-11-18", now=NOW)
    assert result["previous"]["status"] == "unavailable"
    assert len(calls) == 1


def test_cross_host_redirect_is_rejected_before_following():
    query, client, calls = provider({"/live/flight/CCA1430": httpx.Response(302, headers={"Location": "https://evil.example/live/flight/id/1"})})
    with client, pytest.raises(FlightDataError, match="允许"):
        query.fetch("CA1430", "2026-11-18", now=NOW)
    assert len(calls) == 1


@pytest.mark.parametrize("response", [httpx.Response(403), httpx.Response(200, text="<html>challenge</html>"), httpx.Response(200, text="var trackpollBootstrap = {broken}"), httpx.Response(200, text="var trackpollBootstrap = {}")])
def test_unusable_source_is_a_visible_error(response):
    query, client, _ = provider({"/live/flight/CCA1430": response})
    with client, pytest.raises(FlightDataError):
        query.fetch("CA1430", "2026-11-18", now=NOW)


@pytest.mark.parametrize("bad_time", [True, "1794978900", float("nan"), float("inf"), -1])
def test_corrupt_timestamp_is_rejected(bad_time):
    main = flight()
    main["gateDepartureTimes"]["scheduled"] = bad_time
    query, client, _ = provider({"/live/flight/CCA1430": [main]})
    with client, pytest.raises(FlightDataError, match="时间戳"):
        query.fetch("CA1430", "2026-11-18", now=NOW)


def test_historical_samples_use_only_completed_same_route_actual_gate_times():
    main = flight()
    completed = flight(departure=DEPARTURE - timedelta(days=1))
    completed["gateArrivalTimes"]["actual"] = completed["gateArrivalTimes"]["scheduled"] + 1200
    completed["takeoffTimes"]["actual"] = completed["takeoffTimes"]["scheduled"] + 900
    future = flight(departure=DEPARTURE + timedelta(days=1))
    future["gateArrivalTimes"]["estimated"] += 7200
    wrong_route = flight(departure=DEPARTURE - timedelta(days=2), destination="PKX")
    wrong_route["gateArrivalTimes"]["actual"] = wrong_route["gateArrivalTimes"]["scheduled"] + 7200
    wrong_ident = flight("CCA9999", DEPARTURE - timedelta(days=3))
    wrong_ident["gateArrivalTimes"]["actual"] = wrong_ident["gateArrivalTimes"]["scheduled"] + 7200
    old = flight(departure=DEPARTURE - timedelta(days=31))
    old["gateArrivalTimes"]["actual"] = old["gateArrivalTimes"]["scheduled"] + 7200
    main["activityLog"] = {"flights": [completed, deepcopy(completed), future, wrong_route, wrong_ident, old]}
    original = deepcopy(main)
    query, client, _ = provider({"/live/flight/CCA1430": [main]})
    with client:
        result = query.fetch("CA1430", "2026-11-18", now=NOW)
    history = result["historical"]
    assert history["arrival_delays_minutes"] == [20]
    assert history["departure_delays_minutes"] == []
    assert history["takeoff_delays_minutes"] == [15]
    assert history["arrival_sample_count"] == 1
    assert history["completed_flights_count"] == 1
    assert "离港" in history["reason"]
    assert main == original


def test_historical_early_arrival_and_departure_are_preserved():
    main = flight()
    completed = flight(departure=DEPARTURE - timedelta(days=1))
    completed["gateArrivalTimes"]["actual"] = completed["gateArrivalTimes"]["scheduled"] - 600
    completed["gateDepartureTimes"]["actual"] = completed["gateDepartureTimes"]["scheduled"] - 60
    main["activityLog"] = {"flights": [completed]}
    query, client, _ = provider({"/live/flight/CCA1430": [main]})
    with client:
        history = query.fetch("CA1430", "2026-11-18", now=NOW)["historical"]
    assert history["arrival_delays_minutes"] == [-10]
    assert history["departure_delays_minutes"] == [-1]


def test_actual_takeoff_is_explicit_without_replacing_missing_gate_departure():
    main = flight()
    main["takeoffTimes"]["actual"] = main["takeoffTimes"]["scheduled"]
    query, client, _ = provider({"/live/flight/CCA1430": [main]})
    with client:
        result = query.fetch("CA1430", "2026-11-18", now=NOW)
    assert result["flight"]["actual_departure"] is None
    assert result["flight"]["actual_takeoff"] is not None


def test_unknown_cancellation_status_is_not_silently_treated_as_operating():
    main = flight()
    main["cancelled"] = None
    query, client, _ = provider({"/live/flight/CCA1430": [main]})
    with client, pytest.raises(FlightDataError, match="取消状态"):
        query.fetch("CA1430", "2026-11-18", now=NOW)


def test_invalid_aircraft_registration_is_rejected():
    main = flight()
    main["aircraft"]["tail"] = {"bad": True}
    query, client, _ = provider({"/live/flight/CCA1430": [main]})
    with client, pytest.raises(FlightDataError, match="注册号"):
        query.fetch("CA1430", "2026-11-18", now=NOW)
