"""按真实机场和计划时刻取得附近逐小时天气预报。"""

import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

API_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 16
REQUEST_TIMEOUT_SECONDS = 20
WEATHER_WINDOW = timedelta(hours=1)
AIRPORT_FILE = Path(__file__).with_name("airport_locations.json")
LOGGER = logging.getLogger(__name__)

_FIELDS = {
    "weather_code": ("weather_code", "wmo code", False),
    "precipitation": ("precipitation_mm", "mm", False),
    "wind_speed_10m": ("wind_speed_kmh", "km/h", False),
    "wind_gusts_10m": ("wind_gusts_kmh", "km/h", True),
    "visibility": ("visibility_m", "m", True),
}
_WMO_CODES = frozenset((
    0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
    71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99,
))


class WeatherDataError(Exception):
    """机场输入、天气接口或响应数据不能用于评估。"""


@lru_cache(maxsize=1)
def _airports():
    try:
        data = json.loads(AIRPORT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeatherDataError(f"机场定位数据无法读取：{exc}") from exc
    locations = data.get("airports") if isinstance(data, dict) else None
    if not isinstance(locations, dict) or not locations:
        raise WeatherDataError("机场定位数据缺少 airports 表")
    for code, location in locations.items():
        if not re.fullmatch(r"[A-Z]{3}", code) or not isinstance(location, dict):
            raise WeatherDataError("机场定位数据存在无效机场记录")
        _location_values(code, location)
    return locations


def _location_values(code, location):
    for field, limit in (("latitude", 90), ("longitude", 180)):
        value = location.get(field)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or abs(value) > limit):
            raise WeatherDataError(f"机场 {code} 的 {field} 无效")
    if not isinstance(location.get("name"), str) or not location["name"].strip():
        raise WeatherDataError(f"机场 {code} 缺少机场名称")
    try:
        ZoneInfo(location["time_zone"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherDataError(f"机场 {code} 的时区无效") from exc


def get_airports():
    """返回有独立机场坐标的 IATA 机场列表。"""
    return [
        {"code": code, "name": location["name"], "time_zone": location["time_zone"]}
        for code, location in sorted(_airports().items())
    ]


def _airport_location(airport):
    if not isinstance(airport, str) or not re.fullmatch(r"[A-Za-z]{3}", airport):
        raise WeatherDataError("请提供真实机场的三字码，如 CKG、PEK 或 PKX")
    code = airport.upper()
    location = _airports().get(code)
    if location is None:
        raise WeatherDataError(f"机场 {code} 未收录；请使用中国大陆真实机场三字码，BJS 为城市码")
    return code, location


def _scheduled_time(value):
    if not isinstance(value, str):
        raise WeatherDataError("计划时刻必须为带时区的 ISO 日期时间")
    try:
        at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WeatherDataError("计划时刻格式无效") from exc
    if at.tzinfo is None or at.utcoffset() is None:
        raise WeatherDataError("计划时刻缺少时区，如 2026-11-19T10:00:00+08:00")
    return at


def _check_horizon(at, current, location):
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise WeatherDataError("当前时刻必须带有时区")
    if at < current:
        raise WeatherDataError("计划时刻已过，不能用当前预报评估过去的航班")
    airport_zone = ZoneInfo(location["time_zone"])
    day_gap = (at.astimezone(airport_zone).date()
               - current.astimezone(airport_zone).date()).days
    if day_gap >= FORECAST_DAYS:
        raise WeatherDataError("计划日期超出未来 16 天的天气预报范围，请临近出发时再查询")


def _request_forecast(client, location):
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "hourly": ",".join(_FIELDS),
        "timezone": "UTC",
        "forecast_days": FORECAST_DAYS,
        "wind_speed_unit": "kmh",
    }
    try:
        response = client.get(API_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise WeatherDataError(f"Open-Meteo 天气请求失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise WeatherDataError("Open-Meteo 返回的天气数据不是有效 JSON") from exc


def _forecast_arrays(data):
    if (not isinstance(data, dict) or type(data.get("utc_offset_seconds")) is not int
            or data["utc_offset_seconds"] != 0):
        raise WeatherDataError("Open-Meteo 天气响应缺少明确的 UTC 时间基准")
    hourly, units = data.get("hourly"), data.get("hourly_units")
    if not isinstance(hourly, dict) or not isinstance(units, dict):
        raise WeatherDataError("Open-Meteo 天气响应缺少逐小时数据或单位")
    times = hourly.get("time")
    if not isinstance(times, list) or not times or units.get("time") != "iso8601":
        raise WeatherDataError("Open-Meteo 逐小时时间轴无效")
    for field, (_, unit, _) in _FIELDS.items():
        if not isinstance(hourly.get(field), list) or len(hourly[field]) != len(times):
            raise WeatherDataError(f"Open-Meteo 的 {field} 时间轴长度不一致")
        if units.get(field) != unit:
            raise WeatherDataError(f"Open-Meteo 的 {field} 单位应为 {unit}")
    return hourly


def _forecast_time(value):
    if not isinstance(value, str):
        raise WeatherDataError("Open-Meteo 的逐小时时刻格式无效")
    try:
        at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WeatherDataError("Open-Meteo 的逐小时时刻格式无效") from exc
    # 请求和响应均锁定 UTC，接口的无偏移时刻按 UTC 解读。
    if at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    if at.utcoffset() != timedelta(0):
        raise WeatherDataError("Open-Meteo 的逐小时时刻与 UTC 基准冲突")
    return at


def _forecast_value(value, field, optional):
    if value is None and optional:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise WeatherDataError(f"Open-Meteo 的 {field} 数值无效")
    if field == "weather_code":
        if value not in _WMO_CODES:
            raise WeatherDataError(f"Open-Meteo 返回不支持的天气代码 {value}")
        return int(value)
    return float(value)


def _window_hours(data, at, location):
    hourly = _forecast_arrays(data)
    airport_zone = ZoneInfo(location["time_zone"])
    output, previous = [], None
    for index, value in enumerate(hourly["time"]):
        forecast_at = _forecast_time(value)
        if previous is not None and forecast_at <= previous:
            raise WeatherDataError("Open-Meteo 的逐小时时间轴未严格递增")
        if previous is not None and forecast_at - previous != timedelta(hours=1):
            raise WeatherDataError("Open-Meteo 的时间轴不是连续逐小时预报")
        previous = forecast_at
        if abs(forecast_at - at) > WEATHER_WINDOW:
            continue
        row = {"time": forecast_at.astimezone(airport_zone).isoformat()}
        for field, (output_field, _, optional) in _FIELDS.items():
            row[output_field] = _forecast_value(hourly[field][index], field, optional)
        output.append(row)
    if not output:
        raise WeatherDataError("Open-Meteo 预报未覆盖航班计划时刻前后 1 小时")
    return output


class OpenMeteoWeatherProvider:
    """固定公开天气接口；可注入 HTTP 客户端，调用结果不落盘。"""

    def __init__(self, client=None):
        self.client = client

    def fetch(self, airport: str, scheduled_at: str, *, now=None) -> dict:
        try:
            return self._fetch(airport, scheduled_at, now)
        except WeatherDataError as exc:
            LOGGER.error("机场天气查询失败：%s", exc)
            raise

    def _fetch(self, airport, scheduled_at, now):
        code, location = _airport_location(airport)
        at = _scheduled_time(scheduled_at)
        current = datetime.now(UTC) if now is None else now
        _check_horizon(at, current, location)
        if self.client is not None:
            data = _request_forecast(self.client, location)
        else:
            with httpx.Client(trust_env=False) as client:
                data = _request_forecast(client, location)
        return {
            "airport": code,
            "airport_name": location["name"],
            "scheduled_at": at.astimezone(ZoneInfo(location["time_zone"])).isoformat(),
            "fetched_at": current.isoformat(),
            "timezone": location["time_zone"],
            "source": "Open-Meteo",
            "source_url": API_URL,
            "hours": _window_hours(data, at, location),
        }
