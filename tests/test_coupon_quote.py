"""本单优惠、实际应付金额及提醒派生行的引用一致性。"""
import copy

import pytest

from core.pricequote import (
    build_quote,
    quote_comparison_amount,
    quote_from_record,
    quote_sort_key,
)


def _coupon(amount=25, **fields):
    item = {
        "id": "coupon", "kind": "discount", "label": "订单优惠",
        "amount": amount, "included": False, "selected": True,
        "mandatory": False, "applicable": True, "applied": True,
    }
    return {**item, **fields}


def _checkout(*coupons, amount=450, tax=50, **fields):
    payload = {
        "quoted_amount": amount, "basis": "bare",
        "source_type": "manual_checkout", "confirmed": True,
        "extras_complete": True, "discounts_complete": True,
        "charges": [{
            "id": "tax", "kind": "tax", "label": "税费", "amount": tax,
            "included": False, "selected": True, "mandatory": True,
        }, *coupons],
    }
    return {**payload, **fields}


def test_available_coupon_has_to_be_applied_to_this_order():
    quote = build_quote(_checkout(_coupon(25, applied=False)))
    assert quote["complete"] is False
    assert quote["payable_total"] is None
    assert "订单优惠本单未生效" in quote["missing"]
    assert quote["charges"][1]["applied"] is False


@pytest.mark.parametrize("applicable", [True, False, None])
def test_unselected_unused_coupon_does_not_reduce_the_order(applicable):
    quote = build_quote(_checkout(_coupon(
        None, selected=False, applied=False, applicable=applicable)))
    assert quote["payable_total"] == 500
    assert quote["charges"][1]["amount"] is None


def test_discount_uses_the_actual_applied_amount():
    quote = build_quote(_checkout(_coupon("17.35")))
    assert quote["payable_total"] == 482.65
    assert quote["charges"][1]["amount"] == 17.35
    assert quote["charges"][1]["applicable"] is True
    assert quote["charges"][1]["applied"] is True


def test_discount_already_in_the_source_price_is_not_subtracted_twice():
    quote = build_quote(_checkout(_coupon(25, included=True)))
    assert quote["quoted_amount"] == 450
    assert quote["payable_total"] == 500


def test_multiple_applied_coupons_and_cent_precision():
    quote = build_quote(_checkout(
        _coupon("0.10"), _coupon("0.20", id="second"),
        amount="449.99", tax="50.01"))
    assert quote["payable_total"] == 499.70


def test_discounts_are_checked_after_all_fees_regardless_of_item_order():
    payload = _checkout(_coupon(475))
    payload["charges"].reverse()
    assert build_quote(payload)["payable_total"] == 25


def test_coupon_identifiers_cannot_repeat_after_trimming():
    with pytest.raises(ValueError, match="费用id重复"):
        build_quote(_checkout(_coupon(), _coupon(id=" coupon ")))


def test_coupon_cannot_replace_the_reserved_tax_item():
    with pytest.raises(ValueError, match="税费项目保留id"):
        build_quote(_checkout(charges=[_coupon(id="tax")]))


@pytest.mark.parametrize("applied", [True, False, None])
def test_inapplicable_coupon_cannot_be_selected(applied):
    with pytest.raises(ValueError, match="不适用于本单"):
        build_quote(_checkout(_coupon(applicable=False, applied=applied)))


@pytest.mark.parametrize("included", [True, False])
def test_cancelling_an_applied_coupon_requires_a_new_quote(included):
    with pytest.raises(ValueError, match="取消后请重新核对报价"):
        build_quote(_checkout(_coupon(selected=False, included=included)))


def test_cancelling_with_unknown_inclusion_does_not_guess_a_new_price():
    quote = build_quote(_checkout(_coupon(
        None, selected=False, included=None, applied=False)))
    assert quote["payable_total"] is None
    assert "订单优惠取消后的报价" in quote["missing"]


@pytest.mark.parametrize("field,missing", [
    ("applicable", "订单优惠本单是否适用"),
    ("applied", "订单优惠本单是否已生效"),
    ("included", "订单优惠是否已含"),
    ("amount", "订单优惠"),
])
def test_unknown_coupon_details_block_final_price(field, missing):
    quote = build_quote(_checkout(_coupon(**{field: None})))
    assert quote["complete"] is False
    assert quote["payable_total"] is None
    assert missing in quote["missing"]


def test_missing_applicability_and_application_flags_stay_unknown():
    coupon = _coupon()
    del coupon["applicable"]
    del coupon["applied"]
    quote = build_quote(_checkout(coupon))
    assert quote["charges"][1]["applicable"] is None
    assert quote["charges"][1]["applied"] is None
    assert quote["payable_total"] is None


def test_unselected_coupon_with_unknown_application_is_unconfirmed():
    quote = build_quote(_checkout(_coupon(selected=False, applied=None)))
    assert quote["payable_total"] is None
    assert "订单优惠本单是否已生效" in quote["missing"]


def test_old_fee_confirmation_does_not_confirm_coupon_coverage():
    payload = _checkout()
    del payload["discounts_complete"]
    quote = build_quote(payload)
    assert quote["discounts_complete"] is False
    assert quote["payable_total"] is None
    assert "优惠券/订单优惠核对" in quote["missing"]


def test_explicitly_checked_order_without_coupons_has_a_final_price():
    quote = build_quote(_checkout())
    assert quote["discounts_complete"] is True
    assert quote["payable_total"] == 500


@pytest.mark.parametrize("fields", [
    {"extras_complete": False}, {"discounts_complete": False},
    {"confirmed": False}, {"source_type": "list"},
])
def test_applied_coupon_does_not_bypass_other_checkout_requirements(fields):
    assert build_quote(_checkout(_coupon(), **fields))["payable_total"] is None


def test_login_and_fee_flags_on_a_list_record_do_not_confirm_coupons():
    quote = quote_from_record({
        "price": 450, "tax": 50, "basis": "bare", "logged_in": True,
        "extras_complete": True, "discounts_complete": True, "confirmed": True,
        "charges": [_coupon()],
    })
    assert quote["source_type"] == "list"
    assert quote["discounts_complete"] is False
    assert quote["payable_total"] is None


def test_exact_full_discount_can_produce_a_zero_payable_total():
    quote = build_quote(_checkout(_coupon(500)))
    assert quote["complete"] is True
    assert quote["payable_total"] == 0
    assert quote_comparison_amount(quote) == 0
    assert quote_sort_key(quote) < quote_sort_key(build_quote(_checkout(_coupon(499))))


def test_excess_discount_is_an_error_instead_of_a_zero_total():
    with pytest.raises(ValueError, match="抵扣超出"):
        build_quote(_checkout(_coupon("500.01")))


@pytest.mark.parametrize("source_type", ["manual_checkout", "channel_checkout"])
def test_explicit_zero_checkout_amount_is_valid(source_type):
    quote = build_quote(_checkout(amount=0, tax=0, source_type=source_type))
    assert quote["quoted_amount"] == 0
    assert quote["payable_total"] == 0


def test_zero_list_amount_does_not_mean_a_ticket_exists():
    with pytest.raises(ValueError, match="必须大于零"):
        quote_from_record({"price": 0, "basis": "tax_included"})


@pytest.mark.parametrize("amount", [None, "", True, -1, "NaN", "Infinity", "1.001"])
def test_invalid_checkout_amounts_are_never_converted_to_zero(amount):
    with pytest.raises(ValueError):
        build_quote(_checkout(amount=amount))


@pytest.mark.parametrize("amount", [True, -1, "NaN", "Infinity", "", "1.001"])
def test_invalid_discount_amounts_fail_explicitly(amount):
    with pytest.raises(ValueError):
        build_quote(_checkout(_coupon(amount)))


@pytest.mark.parametrize("field", ["applicable", "applied", "discounts_complete"])
@pytest.mark.parametrize("value", [0, 1, "true", []])
def test_discount_flags_require_actual_booleans(field, value):
    payload = (_checkout(**{field: value}) if field == "discounts_complete"
               else _checkout(_coupon(**{field: value})))
    with pytest.raises(ValueError, match="布尔值"):
        build_quote(payload)


def test_existing_quote_is_recalculated_from_actual_coupon_fields():
    payload = _checkout(_coupon())
    payload.update(complete=True, payable_total=1)
    quote = quote_from_record({"price": 450, "quote": payload})
    assert quote["payable_total"] == 475


@pytest.mark.parametrize("record_price", [449.99, 475, 500, None])
def test_quote_cannot_borrow_a_different_source_price(record_price):
    with pytest.raises(ValueError):
        quote_from_record({"price": record_price, "quote": _checkout(_coupon())})


def test_standalone_manual_quote_does_not_need_a_source_record():
    payload = _checkout(_coupon())
    assert quote_from_record(payload)["payable_total"] == 475
    assert quote_from_record({"quote": payload})["payable_total"] == 475


def test_standalone_quote_with_a_price_must_still_match_its_source():
    with pytest.raises(ValueError, match="记录报价与来源报价不一致"):
        quote_sort_key({**_checkout(), "price": 500})


def test_extra_quoted_amount_field_does_not_bypass_nested_quote_validation():
    with pytest.raises(ValueError, match="记录报价与来源报价不一致"):
        quote_sort_key({"price": 500, "quoted_amount": 500,
                        "quote": _checkout(_coupon())})


@pytest.mark.parametrize("require_final,price", [(True, 475), (False, 450)])
def test_derived_reminder_price_uses_the_verified_comparison_amount(require_final, price):
    quote = quote_from_record({
        "price": price, "quote": _checkout(_coupon()),
        "_reminder_view": True, "_require_final_prices": require_final,
    })
    assert quote["payable_total"] == 475
    assert quote["quoted_amount"] == 450


@pytest.mark.parametrize("require_final,price", [(True, 450), (False, 475), (True, 1)])
def test_reminder_marker_does_not_allow_an_arbitrary_price(require_final, price):
    with pytest.raises(ValueError, match="提醒视图金额与已核对报价不一致"):
        quote_from_record({
            "price": price, "quote": _checkout(_coupon()),
            "_reminder_view": True, "_require_final_prices": require_final,
        })


def test_unconfirmed_quote_cannot_be_a_final_price_reminder_view():
    with pytest.raises(ValueError, match="缺少可比较的完整应付价"):
        quote_from_record({"price": 450, "quote": _checkout(discounts_complete=False),
                           "_reminder_view": True})


def test_explicit_quote_reminder_mode_can_show_an_unconfirmed_source_quote():
    quote = quote_from_record({
        "price": 450, "quote": _checkout(discounts_complete=False),
        "_reminder_view": True, "_require_final_prices": False,
    })
    assert quote["payable_total"] is None


def test_reminder_view_requires_quote_evidence():
    with pytest.raises(ValueError, match="必须携带来源报价核对明细"):
        quote_from_record({"price": 450, "_reminder_view": True})


@pytest.mark.parametrize("field", ["_reminder_view", "_require_final_prices"])
def test_reminder_view_flags_cannot_use_truthy_strings(field):
    record = {"price": 475, "quote": _checkout(_coupon()), "_reminder_view": True}
    record[field] = "true"
    with pytest.raises(ValueError, match="布尔值"):
        quote_from_record(record)


def test_zero_final_price_is_a_valid_reminder_reference():
    quote = quote_from_record({
        "price": 0, "quote": _checkout(_coupon(500)), "_reminder_view": True,
    })
    assert quote["payable_total"] == 0


def test_coupon_normalization_does_not_mutate_input():
    payload = _checkout(_coupon())
    before = copy.deepcopy(payload)
    quote = build_quote(payload)
    quote["charges"][1]["applied"] = False
    assert payload == before
