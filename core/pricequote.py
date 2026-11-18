"""来源报价、费用明细和最终应付价的统一契约。

列表价只确认其自身口径。结算确认须同时核对全部税费、附加费用和优惠；
已含项目不重复计算，优惠只采用本单已生效的实际抵扣，未知金额不能按零处理。
"""
# JSON和配置契约统一用ValueError，让调用方明确报告输入错误。
# ruff: noqa: TRY004
from decimal import Decimal, InvalidOperation

BASES = frozenset(("bare", "tax_included", "unknown"))
SOURCE_TYPES = frozenset(("list", "manual_checkout", "channel_checkout"))
CHARGE_KINDS = frozenset(("tax", "insurance", "service", "other", "discount"))
_CENT = Decimal("0.01")
_FLOAT_NOISE = Decimal("0.000000001")
_BASIS_ORDER = {"tax_included": 0, "bare": 1, "unknown": 2}


def money_decimal(value, label="金额") -> Decimal:
    """验证非负、有限、分精度金额；只容忍二进制浮点的微小尾差。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{label}必须是有效金额")
    try:
        amount = Decimal(str(value).strip())
        rounded = amount.quantize(_CENT)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}必须是有效金额") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label}必须是非负有限金额")
    if rounded != amount and (
        not isinstance(value, float) or abs(rounded - amount) > _FLOAT_NOISE
    ):
        raise ValueError(f"{label}最多保留两位小数")
    return rounded


def _flag(payload, key, default=False, nullable=False):
    value = payload.get(key, default)
    if nullable and value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{key}必须是布尔值")
    return value


def _charge(raw):
    if not isinstance(raw, dict):
        raise ValueError("费用项目必须是对象")
    charge_id = raw.get("id")
    label = raw.get("label")
    if not isinstance(charge_id, str) or not charge_id.strip():
        raise ValueError("费用项目缺少id")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("费用项目缺少名称")
    kind = raw.get("kind")
    if kind not in CHARGE_KINDS:
        raise ValueError("费用类型无效")
    for key in ("amount", "included", "selected", "mandatory"):
        if key not in raw:
            raise ValueError(f"费用项目缺少{key}")
    selected = _flag(raw, "selected")
    mandatory = _flag(raw, "mandatory")
    included = _flag(raw, "included", nullable=True)
    if mandatory and not selected:
        raise ValueError(f"{label}为必选费用，不能取消")
    if not selected and included is True:
        raise ValueError(f"{label}已包含在来源报价中，取消后请重新核对报价")
    if kind == "tax" and not selected:
        raise ValueError("税费必须计入费用核对")
    amount = raw["amount"]
    charge = {
        "id": charge_id.strip(),
        "kind": kind,
        "label": label.strip(),
        "amount": (None if amount is None else float(money_decimal(amount, label))),
        "included": included,
        "selected": selected,
        "mandatory": mandatory,
    }
    return _discount_charge(raw, charge) if kind == "discount" else charge


def _discount_charge(raw, charge):
    applicable = _flag(raw, "applicable", default=None, nullable=True)
    applied = _flag(raw, "applied", default=None, nullable=True)
    if charge["selected"] and applicable is False:
        raise ValueError(f"{charge['label']}不适用于本单，不能选用")
    if not charge["selected"] and applied is True:
        raise ValueError(f"{charge['label']}已在本单生效，取消后请重新核对报价")
    return {**charge, "applicable": applicable, "applied": applied}


def _tax_charge(basis, amount=None):
    return {
        "id": "tax",
        "kind": "tax",
        "label": "税费",
        "amount": amount,
        "included": {"tax_included": True, "bare": False, "unknown": None}[basis],
        "selected": True,
        "mandatory": True,
    }


def build_quote(payload: dict) -> dict:
    """按费用是否已含及当前选择计算结算总价，缺项返回未确认状态。"""
    if not isinstance(payload, dict):
        raise ValueError("报价必须是对象")
    amount = money_decimal(payload.get("quoted_amount"), "来源报价")
    basis = payload.get("basis", "unknown")
    if basis not in BASES:
        raise ValueError("报价口径无效")
    source_type = payload.get("source_type", "list")
    if source_type not in SOURCE_TYPES:
        raise ValueError("报价来源类型无效")
    if amount == 0 and source_type == "list":
        raise ValueError("列表来源报价必须大于零")
    extras_complete = _flag(payload, "extras_complete")
    discounts_complete = _flag(payload, "discounts_complete")
    confirmed = _flag(payload, "confirmed")
    raw_charges = payload.get("charges", [])
    if not isinstance(raw_charges, list):
        raise ValueError("费用明细必须是列表")
    charges = [_charge(raw) for raw in raw_charges]
    ids = [charge["id"] for charge in charges]
    if len(set(ids)) != len(ids):
        raise ValueError("费用id重复")
    if not any(charge["kind"] == "tax" for charge in charges):
        tax = _tax_charge(basis)
        if tax["id"] in ids:
            raise ValueError("tax为税费项目保留id")
        charges.insert(0, tax)

    missing = []
    if basis == "unknown":
        missing.append("报价是否含税")
    total = amount
    for charge in charges:
        if not charge["selected"]:
            if charge["included"] is None:
                missing.append(charge["label"] + "取消后的报价")
            if charge["kind"] == "discount" and charge["applied"] is None:
                missing.append(charge["label"] + "本单是否已生效")
            continue
        included = charge["included"]
        discount_ready = (charge["kind"] != "discount"
                          or _discount_ready(charge, missing))
        if charge["kind"] == "tax" and basis != "unknown":
            expected = basis == "tax_included"
            if included is not None and included is not expected:
                raise ValueError("税费是否已含与报价口径不一致")
        if included is None:
            missing.append(charge["label"] + "是否已含")
        elif not included:
            if charge["amount"] is None:
                missing.append(charge["label"])
            elif discount_ready:
                component = money_decimal(charge["amount"], charge["label"])
                total += -component if charge["kind"] == "discount" else component
    if total < 0:
        raise ValueError("本单已生效优惠抵扣超出票价和费用总额")
    if not extras_complete:
        missing.append("保险/服务费等附加费用")
    if not discounts_complete:
        missing.append("优惠券/订单优惠核对")
    if not confirmed or source_type == "list":
        missing.append("结算费用核对")
    missing = list(dict.fromkeys(missing))
    complete = not missing
    return {
        "quoted_amount": float(amount),
        "basis": basis,
        "payable_total": float(total) if complete else None,
        "complete": complete,
        "source_type": source_type,
        "charges": charges,
        "extras_complete": extras_complete,
        "discounts_complete": discounts_complete,
        "confirmed": confirmed,
        "missing": missing,
    }


def _discount_ready(charge, missing):
    if charge["applicable"] is None:
        missing.append(charge["label"] + "本单是否适用")
    if charge["applied"] is None:
        missing.append(charge["label"] + "本单是否已生效")
    elif not charge["applied"]:
        missing.append(charge["label"] + "本单未生效")
    return charge["applicable"] is True and charge["applied"] is True


def quote_comparison_amount(quote: dict, require_final: bool = True):
    """从已校验报价选择比较金额；缺项时没有最终价可供提醒。"""
    if not isinstance(require_final, bool):
        raise ValueError("require_final必须为布尔值")
    if not require_final:
        return quote["quoted_amount"]
    return quote["payable_total"] if quote["complete"] else None


def _record_basis(record, amount, tax):
    basis = record.get("basis", record.get("price_basis")) or "unknown"
    if basis not in BASES:
        raise ValueError("报价口径无效")
    included = record.get("tax_included")
    if included is not None and not isinstance(included, bool):
        raise ValueError("tax_included必须是布尔值或未知")
    if included is not None:
        known_basis = "tax_included" if included else "bare"
        if basis not in ("unknown", known_basis):
            raise ValueError("报价口径与含税标记不一致")
        return known_basis
    bare = record.get("barePrice")
    if basis == "unknown" and tax is not None and bare is not None:
        base = money_decimal(bare, "不含税票价")
        if amount == base + tax:
            return "tax_included"
        if amount == base:
            return "bare"
    return basis


def quote_from_record(record: dict) -> dict:
    """重算独立报价或采集行，核验来源价和提醒派生价各自的引用。"""
    if not isinstance(record, dict):
        raise ValueError("报价记录必须是对象")
    reminder_view = _flag(record, "_reminder_view")
    if "quote" in record or "quoted_amount" in record:
        quote = build_quote(record.get("quote", record))
        _check_price_reference(record, quote, reminder_view)
        return quote
    if reminder_view:
        raise ValueError("提醒视图必须携带来源报价核对明细")
    amount = money_decimal(record.get("price"), "来源报价")
    tax_value = record.get("tax")
    tax = None if tax_value is None else money_decimal(tax_value, "税费")
    basis = _record_basis(record, amount, tax)
    return build_quote({
        "quoted_amount": amount,
        "basis": basis,
        "source_type": "list",
        "charges": [_tax_charge(basis, None if tax is None else float(tax))],
        "extras_complete": False,
        "discounts_complete": False,
        "confirmed": False,
    })


def _check_price_reference(record, quote, reminder_view):
    if "price" not in record:
        return
    amount = money_decimal(record["price"], "记录报价")
    expected = quote["quoted_amount"]
    if reminder_view:
        expected = quote_comparison_amount(
            quote, _flag(record, "_require_final_prices", default=True))
        if expected is None:
            raise ValueError("提醒视图缺少可比较的完整应付价")
    if amount != money_decimal(expected, "引用报价"):
        label = "提醒视图金额与已核对报价" if reminder_view else "记录报价与来源报价"
        raise ValueError(label + "不一致")


def quote_sort_key(record: dict) -> tuple:
    """完整应付价可直接比较；未确认报价按同一口径分别比较。"""
    quote = quote_from_record(record)
    if quote["complete"]:
        return 0, 0, money_decimal(quote["payable_total"])
    return 1, _BASIS_ORDER[quote["basis"]], money_decimal(quote["quoted_amount"])
