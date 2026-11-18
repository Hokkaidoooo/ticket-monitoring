"""自动延误险评估的输入、数据失败、经济结果和真实 HTTP 契约。"""

import threading
from copy import deepcopy
from datetime import datetime
from http.server import ThreadingHTTPServer

import httpx
import pytest

import webui
from core.delay_insurance import DelayInsuranceDataError, assess_delay_insurance
from core.delay_weather import WeatherDataError
from core.flight_operations import FlightDataError

NOW = datetime.fromisoformat("2030-05-01T09:00:00+08:00")
FLIGHT = {
    "flight_no": "CA1430", "flight_id": "CCA1430-test", "departure_airport": "CKG",
    "arrival_airport": "PEK", "scheduled_departure": "2030-05-01T13:15:00+08:00",
    "scheduled_arrival": "2030-05-01T15:45:00+08:00", "estimated_departure": None,
    "estimated_arrival": None, "actual_departure": None, "actual_arrival": None,
    "actual_takeoff": None, "status": "scheduled", "cancelled": False,
}
OPERATIONS = {
    "source": "FlightAware", "source_url": "https://zh.flightaware.com/live/flight/CCA1430",
    "fetched_at": NOW.isoformat(), "flight": FLIGHT,
    "previous": {"status": "unpublished", "reason": "前序尚未公布", "flight": None,
                 "relation_source": "inboundFlight", "source_url": ""},
    "historical": {"arrival_delays_minutes": [-5, 0, 10, 130], "departure_delays_minutes": [],
                   "source": "FlightAware completed flights", "reason": "没有实际离港样本"},
}


class FlightSource:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def fetch(self, number, day, **kwargs):
        self.calls.append((number, day, kwargs))
        if self.error:
            raise self.error
        return deepcopy(OPERATIONS)


class WeatherSource:
    def __init__(self, failing_airport=None):
        self.failing_airport = failing_airport
        self.calls = []

    def fetch(self, airport, scheduled_at, **kwargs):
        self.calls.append((airport, scheduled_at, kwargs))
        if airport == self.failing_airport:
            raise WeatherDataError("天气来源 HTTP 503")
        return {"airport": airport, "airport_name": airport, "scheduled_at": scheduled_at,
                "fetched_at": NOW.isoformat(), "source": "Open-Meteo",
                "source_url": "https://api.open-meteo.com/v1/forecast", "timezone": "Asia/Shanghai",
                "hours": [{"time": scheduled_at, "weather_code": 0, "precipitation_mm": 0,
                           "wind_speed_kmh": 10, "wind_gusts_kmh": 15, "visibility_m": 10000}]}


def payload(**extra):
    return {"flight_no": "CA1430", "flight_date": "2030-05-01", **extra}


def run_assessment(body=None, flights=None, weather=None):
    return assess_delay_insurance(
        payload() if body is None else body,
        flight_provider=flights if flights is not None else FlightSource(),
        weather_provider=weather if weather is not None else WeatherSource(), now=NOW,
    )


def test_number_date_automatically_resolve_plan_previous_history_and_weather():
    flights, weather = FlightSource(), WeatherSource()
    result = run_assessment(flights=flights, weather=weather)
    assert flights.calls[0][:2] == ("CA1430", "2030-05-01")
    assert {(airport, at) for airport, at, _ in weather.calls} == {
        ("CKG", FLIGHT["scheduled_departure"]), ("PEK", FLIGHT["scheduled_arrival"]),
    }
    assert result["previous"]["status"] == "unpublished"
    assert result["estimate"]["sample_count"] == 4
    assert result["estimate"]["confidence"] == "low"
    assert result["insurance"]["recommendation"]["code"] == "need_terms"
    assert OPERATIONS["flight"]["estimated_departure"] is None


def test_financial_result_uses_probability_and_policy_amounts():
    result = run_assessment(payload(premium=20, payout=200, policy_status="eligible"))
    probability = result["estimate"]["delay_probability"]
    insurance = result["insurance"]
    assert insurance["break_even_probability"] == 0.1
    assert insurance["recoup_probability"] == probability
    assert insurance["expected_net"] == pytest.approx(round(probability * 200 - 20, 2))


@pytest.mark.parametrize("extra", [
    {"premium": 20}, {"payout": 200}, {"premium": -1, "payout": 200},
    {"premium": 20, "payout": 0}, {"premium": True, "payout": 200},
    {"premium": "NaN", "payout": 200}, {"threshold_minutes": True},
    {"threshold_minutes": 0}, {"threshold_minutes": 2881},
    {"threshold_minutes": 120.5}, {"policy_status": "buy"}, {"claim_basis": "landing"},
    {"turnaround_minutes": 0}, {"recovery_minutes": -1},
    {"weather": {"weather_code": 0}}, {"historical": [0]},
])
def test_invalid_input_is_rejected_before_network_queries(extra):
    flights, weather = FlightSource(), WeatherSource()
    with pytest.raises(ValueError):
        run_assessment(payload(**extra), flights, weather)
    assert not flights.calls and not weather.calls


def test_flight_source_failure_does_not_create_estimated_plan():
    weather = WeatherSource()
    with pytest.raises(DelayInsuranceDataError, match="航班查询被拒绝"):
        run_assessment(flights=FlightSource(FlightDataError("航班查询被拒绝")), weather=weather)
    assert not weather.calls


def test_weather_failure_retains_evidence_without_probability_or_sunny_defaults():
    with pytest.raises(DelayInsuranceDataError) as caught:
        run_assessment(weather=WeatherSource("PEK"))
    evidence = caught.value.result
    assert evidence["flight"]["flight_no"] == "CA1430"
    assert evidence["weather"]["departure"]["status"] == "available"
    assert evidence["weather"]["arrival"]["status"] == "unavailable"
    assert evidence["weather"]["arrival"]["data"] is None
    assert evidence["estimate"] is None and evidence["insurance"] is None


@pytest.fixture
def delay_http(monkeypatch):
    monkeypatch.setitem(webui.DEMO, "on", False)
    monkeypatch.setattr(webui, "assess_delay_insurance", run_assessment)
    with ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
                yield client
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_real_http_assessment_returns_automatic_evidence_and_economics(delay_http):
    response = delay_http.post("/api/delay-insurance/assess", json=payload(premium=20, payout=200))
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["flight_source"]["source"] == "FlightAware"
    assert result["weather"]["arrival"]["data"]["source"] == "Open-Meteo"
    assert result["insurance"]["conditional"] is True


def test_real_http_invalid_terms_return_400(delay_http):
    response = delay_http.post("/api/delay-insurance/assess", json=payload(premium=-20, payout=200))
    assert response.status_code == 400 and response.json()["ok"] is False


def test_real_http_cross_origin_request_is_rejected(delay_http):
    response = delay_http.post("/api/delay-insurance/assess", json=payload(), headers={"Origin": "https://other.example"})
    assert response.status_code == 403 and response.json()["ok"] is False


def test_real_http_weather_failure_returns_502_with_visible_data_gap(delay_http, monkeypatch):
    monkeypatch.setattr(webui, "assess_delay_insurance", lambda body: run_assessment(body, weather=WeatherSource("PEK")))
    response = delay_http.post("/api/delay-insurance/assess", json=payload())
    assert response.status_code == 502
    assert response.json()["ok"] is False
    assert response.json()["result"]["estimate"] is None


def test_real_http_serves_script_and_actual_airport_list(delay_http):
    script = delay_http.get("/assets/delay-insurance.js")
    assert script.status_code == 200 and "diSubmit" in script.text
    assert "javascript" in script.headers["Content-Type"]
    airports = delay_http.get("/api/delay-insurance/airports").json()["airports"]
    codes = {airport["code"] for airport in airports}
    assert {"CKG", "PEK", "PKX", "CTU", "TFU"} <= codes
    assert "BJS" not in codes


def test_real_http_unexpected_failure_returns_500_and_logs_traceback(delay_http, monkeypatch, caplog):
    def failed(body):
        raise RuntimeError("意外的数据结构变化")
    monkeypatch.setattr(webui, "assess_delay_insurance", failed)
    response = delay_http.post("/api/delay-insurance/assess", json=payload())
    assert response.status_code == 500 and response.json()["ok"] is False
    assert "意外的数据结构变化" in caplog.text
