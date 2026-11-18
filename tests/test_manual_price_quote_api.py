"""费用核对接口的金额、来源与响应契约。"""
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

import webui


def charge(kind, amount, included=False, mandatory=False, selected=True):
    return {
        "id": kind,
        "kind": kind,
        "label": {"tax": "税费", "insurance": "保险", "service": "服务费"}[kind],
        "amount": amount,
        "included": included,
        "mandatory": mandatory,
        "selected": selected,
    }


def test_manual_quote_counts_tax_and_bound_fees():
    result = webui._manual_price_quote({
        "quoted_amount": 450,
        "basis": "bare",
        "charges": [
            charge("tax", 50, mandatory=True),
            charge("insurance", 20, mandatory=True),
            charge("service", 10, mandatory=True),
        ],
        "extras_complete": True,
        "discounts_complete": True,
        "confirmed": True,
    })
    assert result["payable_total"] == 530
    assert result["complete"] is True
    assert result["source_type"] == "manual_checkout"


def test_manual_quote_does_not_add_included_tax_again():
    result = webui._manual_price_quote({
        "quoted_amount": 500,
        "basis": "tax_included",
        "charges": [
            charge("tax", 50, included=True, mandatory=True),
            charge("insurance", 20),
            charge("service", 10, mandatory=True),
        ],
        "extras_complete": True,
        "discounts_complete": True,
        "confirmed": True,
    })
    assert result["payable_total"] == 530


def test_manual_quote_keeps_unknown_service_fee_unconfirmed():
    result = webui._manual_price_quote({
        "quoted_amount": 450,
        "basis": "bare",
        "charges": [charge("tax", 50), charge("service", None, mandatory=True)],
        "extras_complete": True,
        "discounts_complete": True,
        "confirmed": True,
    })
    assert result["payable_total"] is None
    assert result["complete"] is False
    assert "服务费" in result["missing"]


@pytest.mark.parametrize("field", ["source_type", "complete", "payable_total", "charge"])
def test_manual_quote_rejects_asserted_result_or_unknown_keys(field):
    with pytest.raises(ValueError, match="不支持的费用字段"):
        webui._manual_price_quote({"quoted_amount": 450, "basis": "bare", field: True})


def test_price_response_annotation_preserves_raw_quotes():
    payload = {"dates": [{"best": {"price": 450, "src": "tuniucal", "basis": "bare"}}]}
    output = webui._annotate_price_quotes(payload)
    assert "quote" not in payload["dates"][0]["best"]
    record = output["dates"][0]["best"]
    assert record["price"] == 450
    assert record["quote"]["quoted_amount"] == 450
    assert record["quote"]["payable_total"] is None
    assert record["quote"]["complete"] is False


def test_nonprice_response_is_unchanged():
    payload = {"ok": False, "error": "费用格式有误", "rows": 0}
    assert webui._annotate_price_quotes(payload) == payload


@pytest.fixture
def quote_http_server(monkeypatch):
    monkeypatch.setitem(webui.DEMO, "on", False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_http_quote_calculates_all_known_charges(quote_http_server):
    response = quote_http_server.post("/api/price-quote", json={
        "quoted_amount": 100.50,
        "basis": "bare",
        "charges": [
            charge("tax", 10.11, mandatory=True),
            charge("insurance", 0),
            charge("service", 1.39, mandatory=True),
        ],
        "extras_complete": True,
        "discounts_complete": True,
        "confirmed": True,
    })
    assert response.status_code == 200
    assert response.json()["quote"]["payable_total"] == 112
    assert response.json()["quote"]["source_type"] == "manual_checkout"


def test_real_http_rejects_invalid_money(quote_http_server):
    response = quote_http_server.post("/api/price-quote", json={
        "quoted_amount": -100,
        "basis": "bare",
    })
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert response.json()["error"]


def test_real_http_rejects_cross_origin_request(quote_http_server):
    response = quote_http_server.post(
        "/api/price-quote",
        json={"quoted_amount": 100, "basis": "bare"},
        headers={"Origin": "http://other.example"},
    )
    assert response.status_code == 403
    assert response.json()["ok"] is False
