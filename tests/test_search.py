import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

HEADERS = {"X-Tenant": "t-search"}
OTHER = {"X-Tenant": "t-other"}

PLAN_ITEMS = [
    {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01T00:00:00Z"},
    {"installment_id": "i2", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"},
]


def _new_order(oid: str, amount: int, currency: str = "CNY", tenant: str = "t-search") -> None:
    resp = client.post(
        "/orders",
        json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": currency},
    )
    assert resp.status_code == 201, resp.text


def _pay(oid: str, amount: int, originator: str | None = None, tenant: str = "t-search"):
    headers = {"X-Tenant": tenant}
    if originator:
        headers["X-Originator"] = originator
    return client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers=headers)


def _search(params: dict | None = None, headers: dict | None = None):
    return client.get("/orders", params=params or {}, headers=headers if headers is not None else HEADERS)


def _all_pages(params: dict | None = None, page_size: int = 2, headers: dict | None = None) -> list:
    """按续取标记逐页取全量，断言无重复，返回拼序后的订单列表。"""
    params = dict(params or {})
    token: str | None = None
    seen: list = []
    seen_ids: set[str] = set()
    while True:
        query = {**params, "page_size": page_size}
        if token:
            query["continuation_token"] = token
        resp = _search(query, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for item in body["orders"]:
            assert item["order_id"] not in seen_ids, "已返回的订单被重复返回"
            seen_ids.add(item["order_id"])
            seen.append(item)
        token = body["continuation_token"]
        if not token:
            break
    return seen


# ---------- 正常路径 ----------

def test_empty_conditions_pages_all_tenant_orders_ascending() -> None:
    headers = {"X-Tenant": "t-all"}
    for oid, amount in (("q01", 100), ("q02", 300), ("q03", 200), ("q04", 500), ("q05", 50)):
        _new_order(oid, amount, tenant="t-all")
    orders = _all_pages(page_size=2, headers=headers)
    mine = [o for o in orders if o["order_id"].startswith("q0")]
    ids = [o["order_id"] for o in mine]
    assert ids == sorted(ids) == ["q01", "q02", "q03", "q04", "q05"]
    # 每条给出订单标识、订单金额、已收金额、未收金额、币种与当前状态
    first = mine[0]
    assert set(first) == {"order_id", "amount_cents", "paid_cents", "outstanding_cents", "currency", "status"}
    assert first["amount_cents"] == 100 and first["paid_cents"] == 0
    assert first["outstanding_cents"] == 100 and first["currency"] == "CNY"


def test_page_size_larger_than_resultset_returns_empty_token() -> None:
    _new_order("q06", 100)
    resp = _search({"page_size": 100})
    assert resp.status_code == 200
    body = resp.json()
    assert any(o["order_id"] == "q06" for o in body["orders"])
    assert body["continuation_token"] == ""


def test_no_match_returns_empty_list_and_empty_token() -> None:
    _new_order("q07", 100)
    resp = _search({"currency": "JPY"})
    assert resp.status_code == 200
    assert resp.json() == {"orders": [], "continuation_token": ""}


def test_filter_by_status_uses_current_books() -> None:
    _new_order("q10", 500)
    _new_order("q11", 500)
    assert _pay("q11", 500).status_code == 200  # q11 结清

    settled = _all_pages({"status": "settled"})
    assert [o["order_id"] for o in settled if o["order_id"].startswith("q1")] == ["q11"]
    unsettled = _all_pages({"status": "unsettled"})
    assert [o["order_id"] for o in unsettled if o["order_id"].startswith("q1")] == ["q10"]

    # 退款使未收转正：自然回到未结清；全部退完回到未收款状态
    payment_id = client.get("/orders/q11/payments", headers=HEADERS).json()["records"][0]["record_id"]
    refund = client.post(
        f"/orders/q11/payments/{payment_id}/refund", json={"amount_cents": 500}, headers=HEADERS
    )
    assert refund.status_code == 201
    unsettled_after = _all_pages({"status": "unsettled"})
    assert {o["order_id"] for o in unsettled_after if o["order_id"].startswith("q1")} == {"q10", "q11"}
    settled_after = _all_pages({"status": "settled"})
    assert not any(o["order_id"].startswith("q1") for o in settled_after)


def test_filter_by_currency() -> None:
    _new_order("q20", 100, currency="USD")
    _new_order("q21", 100, currency="EUR")
    usd = _all_pages({"currency": "USD"})
    assert [o["order_id"] for o in usd if o["order_id"].startswith("q2")] == ["q20"]


def test_filter_by_amount_range_is_inclusive() -> None:
    for oid, amount in (("q30", 100), ("q31", 200), ("q32", 300), ("q33", 400)):
        _new_order(oid, amount)
    ids = {o["order_id"] for o in _all_pages({"min_amount_cents": 200, "max_amount_cents": 300})}
    assert {"q30", "q31", "q32", "q33"} & ids == {"q31", "q32"}
    only_min = {o["order_id"] for o in _all_pages({"min_amount_cents": 300})}
    assert {"q30", "q31", "q32", "q33"} & only_min == {"q32", "q33"}
    only_max = {o["order_id"] for o in _all_pages({"max_amount_cents": 200})}
    assert {"q30", "q31", "q32", "q33"} & only_max == {"q30", "q31"}


def test_filter_by_payment_originator() -> None:
    _new_order("q40", 500)
    _new_order("q41", 500)
    _new_order("q42", 500)
    assert _pay("q40", 100, originator="alice").status_code == 200
    assert _pay("q41", 100, originator="bob").status_code == 200
    # q42 未发起过收款
    ids = {o["order_id"] for o in _all_pages({"payment_originator": "alice"})}
    assert {"q40", "q41", "q42"} & ids == {"q40"}

    # 该发起方的收款被冲正后，“发起过收款”的事实仍然成立
    record_id = client.get("/orders/q40/payments", headers=HEADERS).json()["records"][0]["record_id"]
    assert client.post(f"/orders/q40/payments/{record_id}/reversal", headers=HEADERS).status_code == 201
    ids_after = {o["order_id"] for o in _all_pages({"payment_originator": "alice"})}
    assert "q40" in ids_after


def test_filter_by_has_installment_plan() -> None:
    _new_order("q50", 1000)
    _new_order("q51", 1000)
    plan = client.post(
        "/orders/q51/installments", json={"installments": PLAN_ITEMS}, headers=HEADERS
    )
    assert plan.status_code == 201
    with_plan = {o["order_id"] for o in _all_pages({"has_installment_plan": "true"})}
    assert {"q50", "q51"} & with_plan == {"q51"}
    without_plan = {o["order_id"] for o in _all_pages({"has_installment_plan": "false"})}
    assert {"q50", "q51"} & without_plan == {"q50"}


def test_combined_filters_must_all_hold() -> None:
    _new_order("q60", 500, currency="USD")
    _new_order("q61", 500, currency="USD")
    _new_order("q62", 500, currency="CNY")
    assert _pay("q60", 500, originator="alice").status_code == 200  # USD 已结清 + alice
    assert _pay("q61", 200, originator="alice").status_code == 200  # USD 未结清 + alice
    resp = _search({
        "status": "settled", "currency": "USD", "min_amount_cents": 100, "max_amount_cents": 1000,
        "payment_originator": "alice", "has_installment_plan": "false",
    })
    assert resp.status_code == 200
    ids = {o["order_id"] for o in resp.json()["orders"]}
    assert {"q60", "q61", "q62"} & ids == {"q60"}


def test_search_is_tenant_scoped_and_leaks_nothing() -> None:
    _new_order("q70", 100, tenant="t-search")
    _new_order("q71", 100, tenant="t-other")
    mine = _all_pages(headers=HEADERS)
    assert "q71" not in {o["order_id"] for o in mine}
    assert "q70" in {o["order_id"] for o in mine}
    theirs = _all_pages(headers=OTHER)
    assert "q70" not in {o["order_id"] for o in theirs}
    assert "q71" in {o["order_id"] for o in theirs}
    # 其他租户的发起方标识也不能用于捞出本租户视角外的任何信息
    resp = _search({"payment_originator": "alice"}, headers=OTHER)
    assert resp.status_code == 200 and all(o["order_id"] != "q70" for o in resp.json()["orders"])


def test_pagination_stays_stable_under_inserts_and_book_changes() -> None:
    headers = {"X-Tenant": "t-page"}
    for i in range(7):
        _new_order(f"q8{i}", 100, tenant="t-page")

    # 第 1 页
    resp = _search({"page_size": 3}, headers=headers)
    page1 = [o["order_id"] for o in resp.json()["orders"]]
    assert page1 == ["q80", "q81", "q82"]
    token = resp.json()["continuation_token"]
    assert token

    # 翻页过程中：新增排序键靠后的订单、把后续页订单结清、把后续页订单金额相关账面变更
    _new_order("q89", 100, tenant="t-page")  # 排序键在游标之后，后续页应能取到
    assert _pay("q83", 100, tenant="t-page").status_code == 200  # 后续页订单状态变更，位置不漂移
    _new_order("q7z", 100, tenant="t-page")  # 排序键在游标之前的新单，不影响本次续取的不重不漏

    seen = set(page1)
    while token:
        body = _search({"page_size": 3, "continuation_token": token}, headers=headers).json()
        for o in body["orders"]:
            assert o["order_id"] not in seen
            seen.add(o["order_id"])
        token = body["continuation_token"]

    # 翻页开始时就存在且排序键在游标之后的订单全部取到，无重复无遗漏
    assert {"q83", "q84", "q85", "q86"} <= seen
    # 翻页期间新增的靠后订单也被取到
    assert "q89" in seen
    # 翻页期间新增在游标之前的订单不会插入本次遍历（保证已返回区间不重复）
    assert "q7z" not in seen
    # 账面变更已反映到快照（q83 已结清）
    q83 = _search({"status": "settled"}, headers=headers).json()["orders"]
    assert any(o["order_id"] == "q83" and o["paid_cents"] == 100 and o["outstanding_cents"] == 0 for o in q83)


def test_continuation_token_is_opaque_and_resumable() -> None:
    headers = {"X-Tenant": "t-cursor"}
    for i in range(4):
        _new_order(f"q9{i}", 100, tenant="t-cursor")
    first = _search({"page_size": 2}, headers=headers).json()
    assert len(first["orders"]) == 2 and first["continuation_token"]
    # 同一令牌重放：返回同一第二页，第一页的订单不会重复
    for _ in range(2):
        second = _search({"page_size": 2, "continuation_token": first["continuation_token"]}, headers=headers).json()
        assert [o["order_id"] for o in second["orders"]] == ["q92", "q93"]
        assert second["continuation_token"] == ""


# ---------- 失败路径 ----------

def test_missing_tenant_header_is_rejected() -> None:
    assert client.get("/orders", params={"page_size": 10}).status_code == 400


def test_invalid_amount_range_is_rejected() -> None:
    resp = _search({"min_amount_cents": 300, "max_amount_cents": 100})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "min_amount_cents must not be greater than max_amount_cents"


def test_non_positive_page_size_is_rejected() -> None:
    for bad in ("0", "-1"):
        resp = _search({"page_size": bad})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "page_size must be a positive integer"


def test_non_integer_params_are_rejected_with_distinct_reasons() -> None:
    resp = _search({"page_size": "abc"})
    assert resp.status_code == 400 and resp.json()["detail"] == "page_size must be an integer"
    resp = _search({"min_amount_cents": "xyz"})
    assert resp.status_code == 400 and resp.json()["detail"] == "min_amount_cents must be an integer"


def test_unparseable_continuation_token_is_rejected() -> None:
    for bad in ("not-a-token!!!", "eyJhZnRlcl9vcmRlcl9pZCI6IH0="):
        resp = _search({"continuation_token": bad})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "continuation token is not parseable"


def test_invalid_status_and_currency_and_flag_are_rejected() -> None:
    assert _search({"status": "paid"}).status_code == 400
    assert _search({"currency": "cny"}).status_code == 400
    assert _search({"currency": "XXX"}).status_code == 400
    assert _search({"has_installment_plan": "yes"}).status_code == 400


def test_rejected_request_changes_nothing() -> None:
    before = _search({"page_size": 1}).json()
    for params in (
        {"min_amount_cents": 9, "max_amount_cents": 1},
        {"page_size": "0"},
        {"continuation_token": "garbage"},
        {"status": "nope"},
    ):
        assert _search(params).status_code == 400
    after = _search({"page_size": 1}).json()
    assert after == before
