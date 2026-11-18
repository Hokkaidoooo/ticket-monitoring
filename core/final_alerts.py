"""提醒使用的价格口径：完整应付价或显式开启的来源报价。"""
# JSON和配置契约统一用ValueError，让调用方明确报告输入错误。
# ruff: noqa: TRY004
import json

from .pricequote import quote_comparison_amount, quote_from_record

_BASIS_LABELS = {
    "bare": "来源裸价",
    "tax_included": "来源含税报价",
    "unknown": "来源报价（税费口径未确认）",
}


def require_final_for_alerts(cfg: dict) -> bool:
    """只接受显式布尔配置，默认要求全部费用已核对。"""
    if not isinstance(cfg, dict):
        raise ValueError("配置必须为对象")
    pricing = cfg.get("pricing", {})
    if not isinstance(pricing, dict):
        raise ValueError("pricing必须为对象")
    value = pricing.get("require_final_for_alerts", True)
    if not isinstance(value, bool):
        raise ValueError("pricing.require_final_for_alerts必须为布尔值")
    return value


def alert_state_key(key: str, require_final: bool = True) -> str:
    """最终应付价和旧来源报价使用独立去抖基准，保留旧记录。"""
    if not isinstance(key, str) or not key:
        raise ValueError("提醒状态键必须为非空字符串")
    if not isinstance(require_final, bool):
        raise ValueError("require_final必须为布尔值")
    return "final-v2|" + key if require_final else key


def reminder_quote(record: dict) -> dict:
    """重新校验报价输入，拒绝仅携带完成旗标的伪结算结果。"""
    if not isinstance(record, dict):
        raise ValueError("提醒报价必须为对象")
    return quote_from_record(record)


def reminder_price(record: dict, require_final: bool = True):
    """缺少完整费用时不可判价；关闭限制时使用明确的来源报价。"""
    if not isinstance(require_final, bool):
        raise ValueError("require_final必须为布尔值")
    quote = reminder_quote(record)
    return quote_comparison_amount(quote, require_final)


def flight_quote_records(price) -> list:
    """保留每条销售报价的费用证据，不借用聚合最低价的证据。"""
    base = {
        "price": price.price,
        "platform": price.platform,
        "_platform": price.platform,
        "from_city": price.from_city,
        "to_city": price.to_city,
        "depart_date": price.depart_date,
    }
    if not price.extra:
        return [base]
    try:
        extra = json.loads(price.extra)
    except json.JSONDecodeError as error:
        raise ValueError("航班费用记录不是合法JSON") from error
    if isinstance(extra, dict):
        if "flights" in extra:
            extra = extra["flights"]
        else:
            return [{**base, **extra, "platform": price.platform,
                     "_platform": price.platform}]
    if not isinstance(extra, list):
        raise ValueError("航班费用记录必须为对象或数组")
    if not extra:
        return [base]
    records = []
    for row in extra:
        if not isinstance(row, dict) or "price" not in row:
            raise ValueError("航班报价行必须为含price的对象")
        records.append({**base, **row, "_platform": price.platform})
    return records


def flight_reminder_record(price, require_final: bool = True):
    """先筛可判价销售报价，再选最低，返回该行自身的费用证据。"""
    candidates = []
    for record in flight_quote_records(price):
        amount = reminder_price(record, require_final)
        if amount is not None:
            candidates.append((amount, record))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def flight_reminder_price(price, require_final: bool = True):
    record = flight_reminder_record(price, require_final)
    return reminder_price(record, require_final) if record is not None else None


def notification_price_note(record: dict, require_final: bool = True) -> str:
    """通知注明它实际使用的金额及费用确认状态。"""
    quote = reminder_quote(record)
    if require_final and quote["complete"]:
        return (f"最终应付价￥{_money_text(quote['payable_total'])}"
                "（税费、当前绑定费用和本单优惠已核对）")
    source = (f"{_BASIS_LABELS[quote['basis']]}￥"
              f"{_money_text(quote['quoted_amount'])}")
    if quote["complete"]:
        return f"{source}；已核最终价￥{_money_text(quote['payable_total'])}"
    return source + "；最终费用未确认"


def _money_text(amount) -> str:
    return f"{amount:.2f}".rstrip("0").rstrip(".")
