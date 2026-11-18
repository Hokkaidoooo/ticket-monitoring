"""自动查询航班运行与天气，计算单档延误险的回本机会。"""
# JSON 输入契约统一使用 ValueError，使 HTTP 层明确返回 400。
# ruff: noqa: TRY004

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from core.delay_risk import estimate_delay, evaluate_insurance, normalize_parameters
from core.delay_weather import OpenMeteoWeatherProvider, WeatherDataError
from core.flight_operations import FlightAwareProvider, FlightDataError
from core.pricequote import money_decimal

LOGGER = logging.getLogger(__name__)
_ALLOWED_FIELDS = frozenset((
    "flight_no", "flight_date", "departure_airport", "arrival_airport",
    "premium", "payout", "threshold_minutes", "claim_basis", "policy_status",
    "turnaround_minutes", "recovery_minutes",
))


class DelayInsuranceDataError(Exception):
    """自动数据获取失败，保留已经取得的证据供界面显示。"""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def _choice(payload, field, default, allowed):
    value = payload.get(field, default)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field}取值无效")
    return value


def _request(payload):
    if not isinstance(payload, dict):
        raise ValueError("延误险评估参数必须是对象")
    unknown = set(payload) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError("不支持的评估字段：" + "、".join(sorted(unknown)))
    query = {}
    for field in ("flight_no", "flight_date", "departure_airport", "arrival_airport"):
        value = payload.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"{field}必须是文本")
        query[field] = value.strip()
    if not query["flight_no"] or not query["flight_date"]:
        raise ValueError("请填写航班号和出发日期")
    premium, payout = payload.get("premium"), payload.get("payout")
    if (premium is None) != (payout is None):
        raise ValueError("保费与赔付金额需要一起填写，或全部留空先看延误概率")
    if premium is not None:
        premium = money_decimal(premium, "保费")
        payout = money_decimal(payout, "赔付金额")
        if payout <= 0:
            raise ValueError("赔付金额必须大于零")
    parameters = normalize_parameters(**{
        key: payload[key] for key in ("threshold_minutes", "claim_basis", "turnaround_minutes", "recovery_minutes")
        if key in payload
    })
    return {
        "query": query, "premium": premium, "payout": payout,
        "policy_status": _choice(payload, "policy_status", "unchecked", ("unchecked", "eligible", "ineligible")),
        **parameters,
    }


def _airport_weather(provider, airport, scheduled_at, current):
    try:
        data = provider.fetch(airport, scheduled_at, now=current)
    except WeatherDataError as exc:
        return {"status": "unavailable", "airport": airport, "error": str(exc), "data": None}
    return {"status": "available", "airport": airport, "error": "", "data": data}


def _weather_for_flight(provider, flight, current):
    requests = (
        ("departure", flight["departure_airport"], flight["scheduled_departure"]),
        ("arrival", flight["arrival_airport"], flight["scheduled_arrival"]),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [(role, pool.submit(_airport_weather, provider, airport, at, current))
                 for role, airport, at in requests]
        return {role: future.result() for role, future in tasks}


def assess_delay_insurance(payload, *, flight_provider=None, weather_provider=None, now=None):
    """数据错误显式失败；前序尚未公布时保留其真实状态和估算依据。"""
    request = _request(payload)
    current = now if now is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("当前时刻必须带时区")
    flights = flight_provider if flight_provider is not None else FlightAwareProvider()
    weather = weather_provider if weather_provider is not None else OpenMeteoWeatherProvider()
    query = request["query"]
    try:
        operations = flights.fetch(
            query["flight_no"], query["flight_date"],
            departure_airport=query["departure_airport"], arrival_airport=query["arrival_airport"],
            now=current,
        )
    except FlightDataError as exc:
        LOGGER.error("延误险航班自动查询失败：%s", exc)
        raise DelayInsuranceDataError(str(exc)) from exc
    flight = operations["flight"]
    evidence = {
        "flight": flight, "previous": operations["previous"],
        "historical": operations["historical"],
        "flight_source": {key: operations[key] for key in ("source", "source_url", "fetched_at")},
        "weather": _weather_for_flight(weather, flight, current),
        "evaluated_at": current.isoformat(), "estimate": None, "insurance": None,
    }
    failures = [item["error"] for item in evidence["weather"].values()
                if item["status"] != "available"]
    if failures:
        message = "天气自动查询未完成：" + "；".join(failures)
        LOGGER.error(message)
        raise DelayInsuranceDataError(message, evidence)
    estimate = estimate_delay(
        flight, operations["previous"], operations["historical"],
        [item["data"] for item in evidence["weather"].values()],
        threshold_minutes=request["threshold_minutes"], claim_basis=request["claim_basis"],
        turnaround_minutes=request["turnaround_minutes"], recovery_minutes=request["recovery_minutes"],
    )
    insurance = evaluate_insurance(
        estimate, premium=request["premium"], payout=request["payout"],
        policy_status=request["policy_status"], flight=flight, now=current,
    )
    return {**evidence, "estimate": estimate, "insurance": insurance}
