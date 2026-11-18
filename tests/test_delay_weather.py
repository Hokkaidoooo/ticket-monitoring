"""自动机场天气的真实机场、时间基准、单位与失败契约。"""

import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from core.delay_weather import (
    API_URL,
    OpenMeteoWeatherProvider,
    WeatherDataError,
    get_airports,
)

NOW = datetime(2026, 11, 18, tzinfo=UTC)
SCHEDULED = "2026-11-19T10:00:00+08:00"
SELECTED = 26


def forecast():
    hours = [(NOW + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")
             for index in range(16 * 24)]
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "iso8601", "weather_code": "wmo code", "precipitation": "mm",
            "wind_speed_10m": "km/h", "wind_gusts_10m": "km/h", "visibility": "m",
        },
        "hourly": {
            "time": hours,
            "weather_code": [3] * len(hours),
            "precipitation": [0.5] * len(hours),
            "wind_speed_10m": [18] * len(hours),
            "wind_gusts_10m": [25] * len(hours),
            "visibility": [10000] * len(hours),
        },
    }


def provider(payload=None, *, status=200, handler=None):
    if handler is None:
        def handler(request):
            return httpx.Response(status, content=json.dumps(payload).encode())
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    return OpenMeteoWeatherProvider(client), client


def test_data_covers_new_airports_and_keeps_city_code_out():
    airports = {row["code"]: row for row in get_airports()}
    assert {"CKG", "PEK", "PKX", "CTU", "TFU"} <= airports.keys()
    assert "BJS" not in airports
    assert len(airports) >= 300
    assert airports["CKG"]["time_zone"] == "Asia/Shanghai"


def test_matches_airport_local_time_and_keeps_actual_hourly_values():
    payload = forecast()
    payload["hourly"]["weather_code"][SELECTED] = 95
    payload["hourly"]["precipitation"][SELECTED] = 8.2
    weather, client = provider(payload)
    with client:
        result = weather.fetch("ckg", SCHEDULED, now=NOW)
    assert result["airport"] == "CKG"
    assert result["scheduled_at"] == SCHEDULED
    assert result["source_url"] == API_URL
    assert [row["time"] for row in result["hours"]] == [
        "2026-11-19T09:00:00+08:00", "2026-11-19T10:00:00+08:00",
        "2026-11-19T11:00:00+08:00",
    ]
    assert result["hours"][1]["weather_code"] == 95
    assert result["hours"][1]["precipitation_mm"] == 8.2


def test_fixed_endpoint_uses_airport_coordinates_and_utc_units():
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json=forecast())

    weather, client = provider(handler=serve)
    with client:
        weather.fetch("PEK", SCHEDULED, now=NOW)
    request = requests[0]
    assert str(request.url).split("?")[0] == API_URL
    assert 39 < float(request.url.params["latitude"]) < 41
    assert 115 < float(request.url.params["longitude"]) < 117
    assert request.url.params["timezone"] == "UTC"
    assert request.url.params["wind_speed_unit"] == "kmh"
    assert request.url.params["forecast_days"] == "16"


@pytest.mark.parametrize("airport", ["BJS", "ZZZ", "重庆", "CKG/PEK", "https://host", None])
def test_invalid_or_unmapped_airport_never_requests_weather(airport):
    def unexpected(request):
        pytest.fail("无效机场不应发出天气请求")

    weather, client = provider(handler=unexpected)
    with client, pytest.raises(WeatherDataError):
        weather.fetch(airport, SCHEDULED, now=NOW)


@pytest.mark.parametrize("at", ["2026-11-19", "2026-11-19T10:00", "bad", None,
                                "2026-11-17T10:00:00+08:00",
                                "2026-12-04T10:00:00+08:00"])
def test_missing_timezone_past_and_out_of_range_do_not_request(at):
    def unexpected(request):
        pytest.fail("无效日期不应发出天气请求")

    weather, client = provider(handler=unexpected)
    with client, pytest.raises(WeatherDataError):
        weather.fetch("CKG", at, now=NOW)


def test_last_forecast_day_is_available():
    weather, client = provider(forecast())
    with client:
        result = weather.fetch("PKX", "2026-12-03T18:00:00+08:00", now=NOW)
    assert result["hours"][1]["time"] == "2026-12-03T18:00:00+08:00"


def test_optional_missing_values_remain_unknown():
    payload = forecast()
    payload["hourly"]["wind_gusts_10m"][SELECTED] = None
    payload["hourly"]["visibility"][SELECTED] = None
    weather, client = provider(payload)
    with client:
        result = weather.fetch("CKG", SCHEDULED, now=NOW)
    assert result["hours"][1]["wind_gusts_kmh"] is None
    assert result["hours"][1]["visibility_m"] is None


@pytest.mark.parametrize("field,value", [
    ("weather_code", None), ("weather_code", 999), ("weather_code", True),
    ("precipitation", -1), ("wind_speed_10m", "18"),
    ("visibility", float("nan")), ("wind_gusts_10m", float("inf")),
])
def test_malformed_selected_hour_is_an_explicit_error(field, value):
    payload = forecast()
    payload["hourly"][field][SELECTED] = value
    weather, client = provider(payload)
    with client, pytest.raises(WeatherDataError, match="无效|不支持"):
        weather.fetch("CKG", SCHEDULED, now=NOW)


@pytest.mark.parametrize("mutation,error", [
    (lambda data: data.pop("hourly"), "缺少逐小时"),
    (lambda data: data.update(utc_offset_seconds=28800), "UTC"),
    (lambda data: data.update(utc_offset_seconds=False), "UTC"),
    (lambda data: data["hourly"]["precipitation"].pop(), "长度不一致"),
    (lambda data: data["hourly_units"].update(wind_speed_10m="m/s"), "单位"),
    (lambda data: data["hourly"]["time"].__setitem__(1, data["hourly"]["time"][0]), "递增"),
    (lambda data: data["hourly"]["time"].__setitem__(SELECTED, "bad"), "格式无效"),
    (lambda data: data["hourly"]["time"].__setitem__(SELECTED, "2026-11-19T02:30"), "连续逐小时"),
])
def test_broken_forecast_contract_is_rejected(mutation, error):
    payload = forecast()
    mutation(payload)
    weather, client = provider(payload)
    with client, pytest.raises(WeatherDataError, match=error):
        weather.fetch("CKG", SCHEDULED, now=NOW)


def test_forecast_without_requested_time_is_rejected():
    payload = forecast()
    for key in payload["hourly"]:
        payload["hourly"][key] = payload["hourly"][key][:24]
    weather, client = provider(payload)
    with client, pytest.raises(WeatherDataError, match="未覆盖"):
        weather.fetch("CKG", SCHEDULED, now=NOW)


@pytest.mark.parametrize("status", [429, 500])
def test_provider_failure_is_logged_and_never_returns_clear_weather(status, caplog):
    weather, client = provider({"reason": "不可用"}, status=status)
    with client, pytest.raises(WeatherDataError, match=str(status)):
        weather.fetch("CKG", SCHEDULED, now=NOW)
    assert "机场天气查询失败" in caplog.text


def test_invalid_json_is_explicit():
    weather, client = provider(handler=lambda request: httpx.Response(200, content=b"not-json"))
    with client, pytest.raises(WeatherDataError, match="JSON"):
        weather.fetch("CKG", SCHEDULED, now=NOW)


def test_timeout_is_explicit():
    def timeout(request):
        raise httpx.ReadTimeout("读取超时", request=request)

    weather, client = provider(handler=timeout)
    with client, pytest.raises(WeatherDataError, match="读取超时"):
        weather.fetch("CKG", SCHEDULED, now=NOW)


def test_results_do_not_share_mutable_hour_records():
    payload = forecast()
    weather, client = provider(copy.deepcopy(payload))
    with client:
        first = weather.fetch("CKG", SCHEDULED, now=NOW)
        first["hours"][0]["weather_code"] = 99
        second = weather.fetch("CKG", SCHEDULED, now=NOW)
    assert second["hours"][0]["weather_code"] == 3


def test_dataset_has_explicit_source_and_license():
    from core.delay_weather import AIRPORT_FILE

    data = json.loads(AIRPORT_FILE.read_text(encoding="utf-8"))
    assert "OurAirports" in data["source"]
    assert data["license"] == "ODC-PDDL-1.0 / Public Domain"
    assert data["source_url"].startswith("https://raw.githubusercontent.com/")
