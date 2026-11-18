"""真实延误样本、前序衔接和机场天气的情景概率及单档保险收益。"""

# JSON契约统一使用ValueError，供HTTP层明确返回输入错误。
# ruff: noqa: TRY004
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from core.pricequote import money_decimal

MODEL_NAME = "历史样本＋前序衔接＋天气情景估算"
PRIOR_STRENGTH = 2.0
PRIOR_LATE_PROBABILITY = 0.30
PRIOR_TAIL_MINUTES = 60.0
PRIOR_DELAY_START_MINUTES = 15.0
MAX_POINT_PROBABILITY = 0.99
MIN_HISTORY_SAMPLES = 10
ETA_UNCERTAINTY_MINUTES = 30.0
ACTUAL_ARRIVAL_UNCERTAINTY_MINUTES = 15.0
INTEGRATION_POINTS = 61
SENSITIVITY_SCALE = 0.25
DEFAULT_THRESHOLD_MINUTES = 120
DEFAULT_TURNAROUND_MINUTES = 45
DEFAULT_RECOVERY_MINUTES = 15
MIN_TURNAROUND_MINUTES = 1
MAX_THRESHOLD_MINUTES = 2880
MAX_TURNAROUND_MINUTES = 180
MAX_RECOVERY_MINUTES = 120
LIGHT_RAIN_MM = 0.1
MEDIUM_RAIN_MM = 3.0
STRONG_RAIN_MM = 8.0
LIGHT_WIND_KMH = 30.0
MEDIUM_WIND_KMH = 40.0
STRONG_WIND_KMH = 55.0
LIGHT_GUST_KMH = 35.0
MEDIUM_GUST_KMH = 50.0
STRONG_GUST_KMH = 70.0
MEDIUM_VISIBILITY_M = 3000.0
STRONG_VISIBILITY_M = 1000.0
WEATHER_IMPACT = ((0.0, 0.0), (0.20, 15.0), (0.55, 45.0), (0.80, 90.0))
WEATHER_LABELS = ("无明显恶劣天气", "轻度天气影响", "明显天气影响", "强天气影响")
WEATHER_CODE_INFO = {
    0: ("晴", 0), 1: ("少云", 0), 2: ("多云", 0), 3: ("阴", 0),
    45: ("雾", 1), 48: ("雾凇", 1), 51: ("小毛毛雨", 1),
    53: ("毛毛雨", 1), 55: ("密集毛毛雨", 2), 56: ("轻度冻毛毛雨", 1),
    57: ("明显冻毛毛雨", 2), 61: ("小雨", 1), 63: ("中雨", 2),
    65: ("大雨", 3), 66: ("轻度冻雨", 1), 67: ("明显冻雨", 3),
    71: ("小雪", 1), 73: ("中雪", 2), 75: ("大雪", 3), 77: ("雪粒", 1),
    80: ("小阵雨", 1), 81: ("中阵雨", 2), 82: ("强阵雨", 3),
    85: ("小阵雪", 2), 86: ("强阵雪", 3), 95: ("雷暴", 3),
    96: ("雷暴伴冰雹", 3), 99: ("强雷暴伴冰雹", 3),
}
FLIGHT_TIME_FIELDS = (
    "scheduled_departure", "scheduled_arrival", "estimated_departure",
    "estimated_arrival", "actual_departure", "actual_arrival", "actual_takeoff",
)
DEPARTED_STATUSES = frozenset(("departed", "airborne", "enroute", "landed", "arrived"))
CANCELLED_STATUSES = frozenset(("cancelled", "canceled"))


@dataclass(frozen=True)
class _Scenario:
    samples: tuple[float, ...]
    propagated: float
    uncertainty: float
    weather_probability: float
    weather_minutes: float
    prior_probability: float = PRIOR_LATE_PROBABILITY
    prior_minutes: float = PRIOR_TAIL_MINUTES


def _number(value, label, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label}必须是有限数字")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label}必须是有限数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数字")
    if minimum is not None and number < minimum:
        raise ValueError(f"{label}不能小于{minimum}")
    return number


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}必须在{minimum}至{maximum}之间")
    return value


def normalize_parameters(*, threshold_minutes=DEFAULT_THRESHOLD_MINUTES, claim_basis="arrival",
                         turnaround_minutes=DEFAULT_TURNAROUND_MINUTES,
                         recovery_minutes=DEFAULT_RECOVERY_MINUTES):
    """验证延误估算参数；供模型和联网前请求校验共用。"""
    if claim_basis not in ("arrival", "departure"):
        raise ValueError("延误口径必须是arrival或departure")
    return {
        "threshold_minutes": _integer(threshold_minutes, "延误门槛分钟", 1, MAX_THRESHOLD_MINUTES),
        "claim_basis": claim_basis,
        "turnaround_minutes": _integer(turnaround_minutes, "前序衔接分钟", MIN_TURNAROUND_MINUTES, MAX_TURNAROUND_MINUTES),
        "recovery_minutes": _integer(recovery_minutes, "空中追回分钟", 0, MAX_RECOVERY_MINUTES),
    }


def _timestamp(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须是带时区的时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}时间格式无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label}必须带时区")
    return parsed


def _airport_code(value, label):
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isalpha() or value != value.upper():
        raise ValueError(f"{label}必须是有效机场三字码")
    return value


def _flight_times(flight):
    if not isinstance(flight, dict):
        raise ValueError("航班资料必须是对象")
    for key in ("departure_airport", "arrival_airport"):
        _airport_code(flight.get(key), key)
    if not isinstance(flight.get("status", ""), str):
        raise ValueError("航班状态必须是字符串")
    times = {}
    for key in FLIGHT_TIME_FIELDS:
        value = flight.get(key)
        if value is None and key.startswith("scheduled_"):
            raise ValueError(f"航班缺少{key}")
        times[key] = None if value is None else _timestamp(value, key)
    if times["scheduled_arrival"] <= times["scheduled_departure"]:
        raise ValueError("计划到达时间必须晚于计划出发时间")
    return times


def _history_samples(historical, claim_basis):
    if not isinstance(historical, dict):
        raise ValueError("历史延误资料必须是对象")
    key = f"{claim_basis}_delays_minutes"
    raw = historical.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"历史资料缺少{key}数组")
    return tuple(_number(value, "历史延误分钟") for value in raw)


def _hour_severity(hour):
    if not isinstance(hour, dict):
        raise ValueError("逐小时天气必须是对象")
    code = hour.get("weather_code")
    if isinstance(code, bool) or not isinstance(code, int) or code not in WEATHER_CODE_INFO:
        raise ValueError("天气代码缺失或不支持")
    rain = _number(hour.get("precipitation_mm"), "降水量", minimum=0)
    wind = _number(hour.get("wind_speed_kmh"), "风速", minimum=0)
    gust = hour.get("wind_gusts_kmh")
    visibility = hour.get("visibility_m")
    gust = None if gust is None else _number(gust, "阵风", minimum=0)
    visibility = None if visibility is None else _number(visibility, "能见度", minimum=0)
    level = WEATHER_CODE_INFO[code][1]
    if rain >= LIGHT_RAIN_MM or wind >= LIGHT_WIND_KMH or (gust is not None and gust >= LIGHT_GUST_KMH):
        level = max(level, 1)
    if rain >= MEDIUM_RAIN_MM or wind >= MEDIUM_WIND_KMH or (gust is not None and gust >= MEDIUM_GUST_KMH) or (visibility is not None and visibility < MEDIUM_VISIBILITY_M):
        level = max(level, 2)
    if rain >= STRONG_RAIN_MM or wind >= STRONG_WIND_KMH or (gust is not None and gust >= STRONG_GUST_KMH) or (visibility is not None and visibility < STRONG_VISIBILITY_M):
        level = 3
    return level, code, rain, wind, gust, visibility


def _weather_summary(weather):
    if not isinstance(weather, list) or not weather:
        raise ValueError("尚未取得机场天气，无法估算延误概率")
    summaries = []
    for airport in weather:
        if not isinstance(airport, dict):
            raise ValueError("机场天气必须是对象")
        _airport_code(airport.get("airport"), "天气机场")
        hours = airport.get("hours")
        if not isinstance(hours, list) or not hours:
            raise ValueError("机场天气缺少逐小时预报")
        assessed = [_hour_severity(hour) for hour in hours]
        level = max(item[0] for item in assessed)
        strongest = next(item for item in assessed if item[0] == level)
        _, code, rain, wind, gust, visibility = strongest
        reasons = [WEATHER_CODE_INFO[code][0], f"降水{rain:g}毫米/小时", f"风速{wind:g}公里/小时"]
        reasons.append("阵风未返回" if gust is None else f"阵风{gust:g}公里/小时")
        reasons.append("能见度未返回" if visibility is None else f"能见度{visibility:g}米")
        summaries.append({"airport": airport["airport"], "level": level,
                          "label": WEATHER_LABELS[level], "reasons": reasons,
                          "missing_metrics": gust is None or visibility is None})
    return summaries


def _positive_minutes(later, earlier):
    return max(0.0, (later - earlier).total_seconds() / 60.0)


def _propagation(flight, times, previous, claim_basis, turnaround, recovery):
    if not isinstance(previous, dict) or previous.get("status") not in ("available", "unpublished", "unavailable"):
        raise ValueError("前序状态缺失或无效")
    if not isinstance(previous.get("reason", ""), str):
        raise ValueError("前序状态说明必须是字符串")
    propagated, uncertainty, factors, previous_known = 0.0, 0.0, [], False
    prev = previous.get("flight")
    if previous["status"] == "available":
        if previous.get("relation_source") != "inboundFlight" or not isinstance(prev, dict):
            raise ValueError("前序必须有明确同机关系与航班资料")
        if prev.get("arrival_airport") != flight.get("departure_airport"):
            raise ValueError("前序到达机场与本班出发机场不一致")
        prev_times = _flight_times(prev)
        arrival = prev_times["actual_arrival"] or prev_times["estimated_arrival"]
        if arrival is not None:
            previous_known = True
            ready = arrival + timedelta(minutes=turnaround)
            propagated = _positive_minutes(ready, times["scheduled_departure"])
            uncertainty = ACTUAL_ARRIVAL_UNCERTAINTY_MINUTES if prev_times["actual_arrival"] else ETA_UNCERTAINTY_MINUTES
            factors.append(f"同机前序{prev.get('flight_no', '')}到达后按{turnaround}分钟衔接，预计传导{propagated:g}分钟")
        else:
            factors.append("已确认同机前序，其到达动态尚未返回")
    else:
        factors.append(previous.get("reason") or "同机前序动态尚未公布")
    current = times["estimated_departure"]
    if current is not None:
        propagated = max(propagated, _positive_minutes(current, times["scheduled_departure"]))
        uncertainty = max(uncertainty, ETA_UNCERTAINTY_MINUTES)
    if claim_basis == "arrival":
        propagated = max(0.0, propagated - recovery)
        current_arrival = times["estimated_arrival"]
        if current_arrival is not None:
            propagated = max(propagated, _positive_minutes(current_arrival, times["scheduled_arrival"]))
            uncertainty = max(uncertainty, ETA_UNCERTAINTY_MINUTES)
    factors.append(f"本班与前序预计延误取较大值，{('到达' if claim_basis == 'arrival' else '离港')}传导约{propagated:g}分钟")
    return propagated, uncertainty, factors, previous_known


def _exp_survival(minutes, scale):
    return 1.0 if minutes <= 0 else math.exp(-minutes / scale)


def _sum_exp_survival(minutes, first, second):
    if minutes <= 0:
        return 1.0
    if math.isclose(first, second):
        return math.exp(-minutes / first) * (1.0 + minutes / first)
    return (first * math.exp(-minutes / first) - second * math.exp(-minutes / second)) / (first - second)


def _prior_survival(threshold, scenario):
    if threshold <= 0:
        return 1.0
    return scenario.prior_probability * _exp_survival(threshold - PRIOR_DELAY_START_MINUTES, scenario.prior_minutes)


def _prior_weather_survival(threshold, propagated, scenario):
    weather = scenario.weather_minutes
    zero_part = _exp_survival(threshold - propagated, weather)
    floor = max(propagated, PRIOR_DELAY_START_MINUTES)
    tail_mass = _exp_survival(floor - PRIOR_DELAY_START_MINUTES, scenario.prior_minutes)
    late_part = ((1.0 - tail_mass) * _exp_survival(threshold - floor, weather)
                 + tail_mass * _sum_exp_survival(threshold - floor, scenario.prior_minutes, weather))
    return (1.0 - scenario.prior_probability) * zero_part + scenario.prior_probability * late_part


def _fixed_propagation_probability(threshold, propagated, scenario):
    if propagated >= threshold:
        return 1.0
    total_weight = len(scenario.samples) + PRIOR_STRENGTH
    baseline = (sum(delay >= threshold for delay in scenario.samples)
                + PRIOR_STRENGTH * _prior_survival(threshold, scenario)) / total_weight
    if scenario.weather_probability == 0:
        return baseline
    weather_hits = sum(_exp_survival(threshold - max(delay, propagated), scenario.weather_minutes)
                       for delay in scenario.samples)
    weather_hits += PRIOR_STRENGTH * _prior_weather_survival(threshold, propagated, scenario)
    return ((1.0 - scenario.weather_probability) * baseline
            + scenario.weather_probability * weather_hits / total_weight)


def _scenario_probability(threshold, scenario):
    if scenario.uncertainty == 0:
        probability = _fixed_propagation_probability(threshold, scenario.propagated, scenario)
    else:
        values = (max(0.0, scenario.propagated + scenario.uncertainty * (2 * (index + 0.5) / INTEGRATION_POINTS - 1))
                  for index in range(INTEGRATION_POINTS))
        probability = sum(_fixed_propagation_probability(threshold, value, scenario) for value in values) / INTEGRATION_POINTS
    return min(MAX_POINT_PROBABILITY, max(0.0, probability))


def _sensitivity_range(threshold, scenario, probability):
    low = replace(scenario, propagated=max(0.0, scenario.propagated - scenario.uncertainty),
                  uncertainty=0, weather_probability=scenario.weather_probability * (1 - SENSITIVITY_SCALE),
                  weather_minutes=scenario.weather_minutes * (1 - SENSITIVITY_SCALE),
                  prior_probability=scenario.prior_probability * (1 - SENSITIVITY_SCALE),
                  prior_minutes=scenario.prior_minutes * (1 - SENSITIVITY_SCALE))
    high = replace(scenario, propagated=scenario.propagated + scenario.uncertainty, uncertainty=0,
                   weather_probability=min(1.0, scenario.weather_probability * (1 + SENSITIVITY_SCALE)),
                   weather_minutes=scenario.weather_minutes * (1 + SENSITIVITY_SCALE),
                   prior_probability=min(1.0, scenario.prior_probability * (1 + SENSITIVITY_SCALE)),
                   prior_minutes=scenario.prior_minutes * (1 + SENSITIVITY_SCALE))
    return [min(probability, _scenario_probability(threshold, low)),
            max(probability, _scenario_probability(threshold, high))]


def estimate_delay(flight, previous, historical, weather, *, threshold_minutes=DEFAULT_THRESHOLD_MINUTES,
                   claim_basis="arrival", turnaround_minutes=DEFAULT_TURNAROUND_MINUTES,
                   recovery_minutes=DEFAULT_RECOVERY_MINUTES):
    """估算指定到达或离港延误门槛；区间表示参数敏感性。"""
    parameters = normalize_parameters(threshold_minutes=threshold_minutes, claim_basis=claim_basis,
                                      turnaround_minutes=turnaround_minutes, recovery_minutes=recovery_minutes)
    threshold = parameters["threshold_minutes"]
    turnaround = parameters["turnaround_minutes"]
    recovery = parameters["recovery_minutes"]
    times = _flight_times(flight)
    samples = _history_samples(historical, claim_basis)
    summaries = _weather_summary(weather)
    propagated, uncertainty, factors, previous_known = _propagation(flight, times, previous, claim_basis, turnaround, recovery)
    weather_probability, weather_minutes = WEATHER_IMPACT[max(item["level"] for item in summaries)]
    scenario = _Scenario(samples, propagated, uncertainty, weather_probability, weather_minutes)
    probability = _scenario_probability(threshold, scenario)
    assumptions = [
        "历史样本、前序衔接与天气的情景估算，尚未按真实理赔结果校准。",
        f"历史样本外保留权重{PRIOR_STRENGTH:g}的一般情景：{PRIOR_LATE_PROBABILITY:.0%}晚点，15分钟后尾部平均{PRIOR_TAIL_MINUTES:g}分钟。",
        "前序与本班预计延误取较大值，历史延误与传导延误取较大值；天气按最强机场信号修正。",
        f"前序衔接按{turnaround}分钟；到达延误的起飞传导可追回{recovery}分钟。",
        "概率范围为情景参数敏感性范围，不是统计置信区间。",
        f"点估计最高为{MAX_POINT_PROBABILITY:.0%}，保留临时调机与航班调整不确定性。",
    ]
    if not samples:
        assumptions.append("尚无符合本次延误口径的已完成航班样本，概率采用明确的一般情景参数。")
    for item in summaries:
        factors.append(f"{item['airport']}：{item['label']}（{'，'.join(item['reasons'])}）")
    confidence = "medium" if len(samples) >= MIN_HISTORY_SAMPLES and previous_known and not any(item["missing_metrics"] for item in summaries) else "low"
    return {"delay_probability": probability, "probability_range": _sensitivity_range(threshold, scenario, probability),
            "threshold_minutes": threshold, "claim_basis": claim_basis, "sample_count": len(samples),
            "confidence": confidence, "model_name": MODEL_NAME, "prior_used": True,
            "assumptions": assumptions, "factors": factors, "propagated_delay_minutes": propagated,
            "weather_summary": summaries}


def _ineligible_reason(flight, times, policy_status, now):
    cancelled = flight.get("cancelled", False)
    if not isinstance(cancelled, bool):
        raise ValueError("航班取消状态必须是布尔值")
    status = flight.get("status", "").casefold()
    if cancelled or status in CANCELLED_STATUSES:
        return "航班已取消；取消赔付需要按取消条款单独核对。"
    if times["actual_takeoff"] is not None or times["actual_departure"] is not None or times["actual_arrival"] is not None or status in DEPARTED_STATUSES:
        return "航班已经出发，当前不能按起飞前投保条件建议购买。"
    if now >= times["scheduled_departure"]:
        return "计划出发时间已过，当前不能按起飞前投保条件建议购买。"
    if policy_status == "ineligible":
        return "本单不符合投保或赔付条件。"
    return ""


def _economic_result(probability, premium, payout):
    expected = (Decimal(str(probability)) * payout).quantize(Decimal("0.01"))
    net = (Decimal(str(probability)) * payout - premium).quantize(Decimal("0.01"))
    return {"break_even_probability": None if payout == 0 else float(premium / payout),
            "recoup_probability": 1.0 if premium == 0 else (probability if payout >= premium else 0.0),
            "profit_probability": probability if payout > premium else 0.0,
            "expected_payout": float(expected), "expected_net": float(net)}


def _recommendation(economics, premium, payout, policy_status):
    if payout == 0:
        return {"code": "skip", "label": "倾向不买", "reason": "赔付金额为0，没有赔付收益。"}
    if payout < premium:
        return {"code": "skip", "label": "倾向不买", "reason": "单次赔付不足以覆盖保费。"}
    net = economics["expected_net"]
    if net < 0:
        return {"code": "skip", "label": "倾向不买", "reason": "估算赔付的期望金额低于保费。"}
    if net == 0:
        return {"code": "borderline", "label": "处于回本边界", "reason": "估算赔付的期望金额与保费相当。"}
    if policy_status == "unchecked":
        return {"code": "check_terms", "label": "核对条款后可考虑", "reason": "按全部投保及赔付条件满足时计算，估算期望收益为正。"}
    return {"code": "buy", "label": "倾向购买", "reason": "估算赔付的期望金额高于保费。"}


def evaluate_insurance(estimate, *, premium, payout, policy_status="unchecked", flight, now):
    """按达到门槛后一次固定赔付计算回本概率和期望净收益。"""
    if policy_status not in ("unchecked", "eligible", "ineligible"):
        raise ValueError("投保条件状态无效")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("当前时间必须带时区")
    if not isinstance(estimate, dict):
        raise ValueError("概率估算必须是对象")
    probability = _number(estimate.get("delay_probability"), "延误概率", minimum=0)
    if probability > 1:
        raise ValueError("延误概率不能超过1")
    cost = None if premium is None else money_decimal(premium, "保费")
    benefit = None if payout is None else money_decimal(payout, "赔付金额")
    reason = _ineligible_reason(flight, _flight_times(flight), policy_status, now)
    result = {"premium": None if cost is None else float(cost), "payout": None if benefit is None else float(benefit),
              "conditional": policy_status == "unchecked", "break_even_probability": None,
              "recoup_probability": None, "profit_probability": None, "expected_payout": None, "expected_net": None}
    if cost is not None and benefit is not None:
        result.update(_economic_result(0.0 if reason else probability, cost, benefit))
    if reason:
        result["recommendation"] = {"code": "ineligible", "label": "当前不建议购买", "reason": reason}
    elif cost is None or benefit is None:
        result["recommendation"] = {"code": "need_terms", "label": "填写保险条款后判断", "reason": "已估算延误概率；回本判断还需要本单保费和固定赔付金额。"}
    else:
        result["recommendation"] = _recommendation(result, cost, benefit, policy_status)
    return result
