"""从 FlightAware 公开详情读取指定航班及其明确关联的前序航班。"""

import json
import logging
import math
import re
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://zh.flightaware.com"
REQUEST_TIMEOUT_SECONDS = 18
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 2
HISTORY_DAYS = 30
BOOTSTRAP_MARKER = re.compile(r"\bvar\s+trackpollBootstrap\s*=\s*")

# FlightAware 的航班地址使用 ICAO 航司码；IATA 地址可能命中无关的旧私有航班。
AIRLINE_ICAO = {
    "CA": "CCA", "MU": "CES", "CZ": "CSN", "HU": "CHH", "9C": "CQH",
    "HO": "DKH", "MF": "CXA", "ZH": "CSZ", "3U": "CSC", "SC": "CDG",
    "FM": "CSH", "KN": "CUA", "GS": "GCR", "JD": "CBJ", "PN": "CHB",
    "G5": "HXA", "KY": "KNA", "TV": "TBA", "NS": "HBH", "GJ": "CDC",
    "EU": "UEA", "BK": "OKA", "DZ": "EPA", "GX": "CBG", "QW": "QDA",
    "DR": "RLH", "AQ": "JYH", "UQ": "CUH", "8L": "LKE", "9H": "CGN",
    "FU": "FZA", "GT": "CGH", "RY": "CJX", "A6": "OTC", "Y8": "YZR",
    "CX": "CPA", "BR": "EVA", "CI": "CAL", "SQ": "SIA", "TG": "THA",
    "JL": "JAL", "NH": "ANA", "KE": "KAL", "OZ": "AAR", "QF": "QFA",
    "EK": "UAE", "QR": "QTR", "TK": "THY", "LH": "DLH", "AF": "AFR",
    "KL": "KLM", "BA": "BAW", "UA": "UAL", "AA": "AAL", "DL": "DAL",
    "AC": "ACA",
}
ICAO_AIRLINE = {icao: iata for iata, icao in AIRLINE_ICAO.items()}


class FlightDataError(Exception):
    """查询失败或数据不足以确认航班身份。"""


def _fail(message):
    LOGGER.error("航班动态查询失败：%s", message)
    raise FlightDataError(message)


def _flight_ident(flight_no):
    if not isinstance(flight_no, str):
        raise ValueError("航班号必须是文字")  # noqa: TRY004 — 请求字段校验统一由 ValueError 返回 400。
    number = flight_no.strip().upper()
    match = re.fullmatch(r"([A-Z0-9]{2}|[A-Z]{3})([0-9]{1,5}[A-Z]?)", number)
    if not match:
        raise ValueError("航班号格式有误，例如 CA1430 或 CCA1430")
    airline, suffix = match.groups()
    if len(airline) == 3:
        return airline + suffix, ICAO_AIRLINE.get(airline, airline) + suffix
    if airline not in AIRLINE_ICAO:
        raise ValueError("该航司暂未配置 ICAO 码，请使用三字航司码的完整航班号")
    return AIRLINE_ICAO[airline] + suffix, airline + suffix


def _flight_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("航班日期须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("航班日期无效") from exc


def _airport_input(value):
    if not isinstance(value, str):
        raise ValueError("机场须为三字码")  # noqa: TRY004 — 请求字段校验统一由 ValueError 返回 400。
    airport = value.strip().upper()
    if airport and not re.fullmatch(r"[A-Z]{3}", airport):
        raise ValueError("机场须为三字码，例如 CKG、PEK 或 PKX")
    return airport


def _clock(now):
    current = datetime.now(UTC) if now is None else now
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError("查询时间必须包含时区")
    return current.astimezone(UTC)


def _safe_url(link, current_url=BASE_URL):
    if not isinstance(link, str) or not link or len(link) > 1500:
        _fail("航班详情链接缺失或无效")
    url = urljoin(current_url, link)
    try:
        parts = urlsplit(url)
    except ValueError:
        _fail("航班详情链接格式无效")
    decoded = unquote(parts.path)
    try:
        valid_port = parts.port in (None, 443)
    except ValueError:
        valid_port = False
    if (
        parts.scheme != "https" or parts.hostname != "zh.flightaware.com"
        or parts.username or parts.password or not valid_port or parts.fragment
        or not decoded.startswith("/live/flight/") or "\\" in decoded
        or "//" in decoded or ".." in decoded.split("/")
        or any(ord(char) < 32 for char in url)
    ):
        _fail("航班详情链接不属于允许的 FlightAware 地址")
    return url


def _bootstrap(response):
    if len(response.content) > MAX_RESPONSE_BYTES:
        _fail("FlightAware 响应超过允许大小")
    marker = BOOTSTRAP_MARKER.search(response.text)
    if marker is None:
        _fail("FlightAware 页面缺少航班动态数据，可能尚未公布或查询受限")
    try:
        payload, _ = json.JSONDecoder().raw_decode(response.text[marker.end():])
    except json.JSONDecodeError as exc:
        _fail("FlightAware 航班动态数据不是有效 JSON：" + str(exc))
    if not isinstance(payload, dict) or not isinstance(payload.get("flights"), dict):
        _fail("FlightAware 航班动态结构无效")
    flights = payload["flights"]
    if not flights or len(flights) > 50 or not all(isinstance(row, dict) for row in flights.values()):
        _fail("FlightAware 没有有效的航班详情")
    return list(flights.values())


def _read(client, url):
    target = _safe_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        try:
            response = client.get(
                target, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            )
        except httpx.HTTPError as exc:
            _fail("无法连接 FlightAware：" + str(exc))
        if response.is_redirect:
            target = _safe_url(response.headers.get("Location"), target)
            continue
        if response.status_code != 200:
            _fail(f"FlightAware 返回 HTTP {response.status_code}")
        return _bootstrap(response), target
    _fail("FlightAware 重定向次数超过允许范围")


def _epoch(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail("FlightAware 时间戳格式无效")
    try:
        moment = datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        _fail("FlightAware 时间戳超出范围")
    if not 2000 <= moment.year <= 2100:
        _fail("FlightAware 时间戳超出有效年份范围")
    return moment


def _times(row, stage):
    value = row.get(stage)
    if not isinstance(value, dict):
        _fail("FlightAware 缺少起降时间结构：" + stage)
    return value


def _airport(row, key):
    airport = row.get(key)
    if not isinstance(airport, dict) or not re.fullmatch(r"[A-Z]{3}", str(airport.get("iata", ""))):
        _fail("FlightAware 缺少具体起降机场")
    timezone_name = str(airport.get("TZ") or "").removeprefix(":")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        _fail("FlightAware 缺少有效的机场时区")
    return airport, zone


def _ident_matches(row, ident, iata_ident):
    fields = ("ident", "globalIdent", "displayIdent", "iataIdent")
    return any(row.get(field) in (ident, iata_ident) for field in fields)


def _matches(row, ident, iata_ident, day, departure, arrival):
    if not _ident_matches(row, ident, iata_ident):
        return False
    origin, zone = _airport(row, "origin")
    destination, _ = _airport(row, "destination")
    scheduled = _epoch(_times(row, "gateDepartureTimes").get("scheduled"))
    if scheduled is None or scheduled.astimezone(zone).date() != day:
        return False
    return (not departure or origin["iata"] == departure) and (not arrival or destination["iata"] == arrival)


def _activity(roots):
    rows = []
    for root in roots:
        activity = root.get("activityLog", {})
        if not isinstance(activity, dict):
            _fail("FlightAware 历史航班结构无效")
        items = activity.get("flights", [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            _fail("FlightAware 历史航班列表无效")
        if len(items) > 200:
            _fail("FlightAware 历史航班数量超出允许范围")
        rows.extend(items)
    return rows


def _select(rows, ident, iata_ident, day, departure, arrival):
    matches = {}
    for row in rows:
        if not _matches(row, ident, iata_ident, day, departure, arrival):
            continue
        flight_id = row.get("flightId")
        if not isinstance(flight_id, str) or not flight_id:
            _fail("FlightAware 缺少航班唯一标识")
        matches.setdefault(flight_id, row)
    if not matches:
        _fail("未查到指定日期和机场的航班；航班计划可能尚未公布，请临近出发再查")
    if len(matches) > 1:
        _fail("同一天查到多个航段，请指定起降机场")
    return next(iter(matches.values()))


def _detail_url(row):
    links = row.get("links")
    if not isinstance(links, dict):
        _fail("FlightAware 缺少航班详情链接")
    return _safe_url(links.get("permanent") or links.get("operated"))


def _iso(value, zone):
    moment = _epoch(value)
    return moment.astimezone(zone).isoformat() if moment is not None else None


def _normalize(row):
    origin, departure_zone = _airport(row, "origin")
    destination, arrival_zone = _airport(row, "destination")
    departure = _times(row, "gateDepartureTimes")
    arrival = _times(row, "gateArrivalTimes")
    scheduled_departure = _iso(departure.get("scheduled"), departure_zone)
    scheduled_arrival = _iso(arrival.get("scheduled"), arrival_zone)
    if scheduled_departure is None or scheduled_arrival is None:
        _fail("FlightAware 缺少计划离港或到港时间")
    if datetime.fromisoformat(scheduled_arrival) <= datetime.fromisoformat(scheduled_departure):
        _fail("FlightAware 计划到港时间不晚于离港时间")
    flight_id = row.get("flightId")
    if not isinstance(flight_id, str) or not flight_id:
        _fail("FlightAware 缺少航班唯一标识")
    aircraft = row.get("aircraft") or {}
    if not isinstance(aircraft, dict):
        _fail("FlightAware 飞机信息结构无效")
    cancelled = row.get("cancelled")
    if not isinstance(cancelled, bool):
        _fail("FlightAware 航班取消状态未明确提供")
    registration = aircraft.get("tail")
    if registration is not None and not isinstance(registration, str):
        _fail("FlightAware 飞机注册号格式无效")
    number = row.get("iataIdent") or row.get("ident") or row.get("displayIdent")
    if not isinstance(number, str) or not number:
        _fail("FlightAware 缺少航班号")
    converted = re.fullmatch(r"([A-Z]{3})([0-9]{1,5}[A-Z]?)", number)
    if converted and converted.group(1) in ICAO_AIRLINE:
        number = ICAO_AIRLINE[converted.group(1)] + converted.group(2)
    output = {
        "flight_no": number, "flight_id": flight_id,
        "departure_airport": origin["iata"], "arrival_airport": destination["iata"],
        "departure_name": origin.get("friendlyLocation") or origin.get("friendlyName") or origin["iata"],
        "arrival_name": destination.get("friendlyLocation") or destination.get("friendlyName") or destination["iata"],
        "departure_timezone": str(departure_zone), "arrival_timezone": str(arrival_zone),
        "scheduled_departure": scheduled_departure, "scheduled_arrival": scheduled_arrival,
        "estimated_departure": _iso(departure.get("estimated"), departure_zone),
        "estimated_arrival": _iso(arrival.get("estimated"), arrival_zone),
        "actual_departure": _iso(departure.get("actual"), departure_zone),
        "actual_arrival": _iso(arrival.get("actual"), arrival_zone),
        "status": str(row.get("flightStatus") or ""),
        "cancelled": cancelled,
        "aircraft_registration": registration or None,
    }
    for stage, name, zone in (
        ("takeoffTimes", "takeoff", departure_zone),
        ("landingTimes", "landing", arrival_zone),
    ):
        times = _times(row, stage)
        for kind in ("scheduled", "estimated", "actual"):
            output[kind + "_" + name] = _iso(times.get(kind), zone)
    return output


def _previous(client, row, main_flight):
    relation = row.get("inboundFlight")
    if relation is None:
        return {
            "status": "unpublished", "reason": "数据源尚未公布同机前序航班",
            "flight": None, "relation_source": "inboundFlight", "source_url": None,
        }
    source_url = None
    try:
        if not isinstance(relation, dict) or not isinstance(relation.get("flightId"), str):
            _fail("FlightAware 前序关系结构无效")
        previous_id = relation["flightId"]
        if not previous_id or previous_id == main_flight["flight_id"]:
            _fail("FlightAware 前序航班身份无效")
        source_url = _safe_url(relation.get("linkUrl"))
        roots, source_url = _read(client, source_url)
        matches = [item for item in roots if item.get("flightId") == previous_id]
        if len(matches) != 1:
            _fail("前序详情与数据源明确关联的航班标识不一致")
        previous = _normalize(matches[0])
        if previous["arrival_airport"] != main_flight["departure_airport"]:
            _fail("前序到达机场与本航班出发机场不一致")
        if datetime.fromisoformat(previous["scheduled_departure"]) >= datetime.fromisoformat(main_flight["scheduled_departure"]):
            _fail("前序计划离港时间不早于本航班")
        return {
            "status": "available", "reason": "数据源已明确关联同机前序航班",
            "flight": previous, "relation_source": "inboundFlight", "source_url": source_url,
        }
    except FlightDataError as exc:
        return {
            "status": "unavailable", "reason": str(exc), "flight": None,
            "relation_source": "inboundFlight", "source_url": source_url,
        }


def _history(rows, main_flight, ident, iata_ident, current):
    departure_delays = []
    arrival_delays = []
    takeoff_delays = []
    seen = set()
    cutoff = current - timedelta(days=HISTORY_DAYS)
    for row in rows:
        flight_id = row.get("flightId")
        if flight_id in seen or flight_id == main_flight["flight_id"]:
            continue
        if not _ident_matches(row, ident, iata_ident) or row.get("cancelled") or row.get("diverted"):
            continue
        origin, _ = _airport(row, "origin")
        destination, _ = _airport(row, "destination")
        if origin["iata"] != main_flight["departure_airport"] or destination["iata"] != main_flight["arrival_airport"]:
            continue
        dep = _times(row, "gateDepartureTimes")
        arr = _times(row, "gateArrivalTimes")
        takeoff = _times(row, "takeoffTimes")
        landing = _times(row, "landingTimes")
        scheduled = _epoch(dep.get("scheduled"))
        actual_arrival = _epoch(arr.get("actual"))
        actual_landing = _epoch(landing.get("actual"))
        if scheduled is None or not cutoff <= scheduled < current:
            continue
        completed_at = actual_arrival or actual_landing
        if completed_at is None or completed_at > current:
            continue
        if not isinstance(flight_id, str) or not flight_id:
            _fail("历史航班缺少唯一标识")
        seen.add(flight_id)
        for times, target in ((dep, departure_delays), (arr, arrival_delays), (takeoff, takeoff_delays)):
            scheduled_time = _epoch(times.get("scheduled"))
            actual_time = _epoch(times.get("actual"))
            if scheduled_time is not None and actual_time is not None and actual_time <= current:
                target.append(round((actual_time - scheduled_time).total_seconds() / 60, 2))
    missing = []
    if not departure_delays:
        missing.append("未取得已完成航班的实际离港时间；起飞时间另列，不作为离港样本")
    if not arrival_delays:
        missing.append("未取得已完成航班的实际到港时间")
    return {
        "arrival_delays_minutes": arrival_delays,
        "departure_delays_minutes": departure_delays,
        "takeoff_delays_minutes": takeoff_delays,
        "arrival_sample_count": len(arrival_delays),
        "departure_sample_count": len(departure_delays),
        "takeoff_sample_count": len(takeoff_delays),
        "sample_count": max(len(arrival_delays), len(departure_delays)),
        "completed_flights_count": len(seen), "window_days": HISTORY_DAYS,
        "source": "FlightAware completed flights", "reason": "；".join(missing),
        "arrival_basis": "gate_arrival", "departure_basis": "gate_departure",
    }


class FlightAwareProvider:
    """一次查询读取根页、指定日期详情及明确关联的前序详情。"""

    def __init__(self, client=None):
        self.client = client

    def fetch(self, flight_no, flight_date, *, departure_airport="", arrival_airport="", now=None):
        ident, iata_ident = _flight_ident(flight_no)
        day = _flight_date(flight_date)
        departure = _airport_input(departure_airport)
        arrival = _airport_input(arrival_airport)
        current = _clock(now)
        context = nullcontext(self.client) if self.client is not None else httpx.Client(trust_env=False)
        with context as client:
            roots, root_url = _read(client, BASE_URL + "/live/flight/" + ident)
            historical_rows = _activity(roots)
            selected = _select(roots + historical_rows, ident, iata_ident, day, departure, arrival)
            source_url = root_url
            if not any(item.get("flightId") == selected["flightId"] for item in roots):
                detail_roots, source_url = _read(client, _detail_url(selected))
                selected = _select(detail_roots, ident, iata_ident, day, departure, arrival)
            flight = _normalize(selected)
            previous = _previous(client, selected, flight)
        return {
            "source": "FlightAware", "source_url": source_url,
            "fetched_at": current.isoformat(), "flight": flight, "previous": previous,
            "historical": _history(historical_rows, flight, ident, iata_ident, current),
        }
