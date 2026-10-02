import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)


def _order(order_id: str, amount: int, tenant: str, currency: str = "CNY") -> None:
    resp = client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": currency},
    )
    assert resp.status_code == 201, resp.text


def _pay(order_id: str, amount: int, tenant: str, originator: str | None = None, installment_id: str | None = None):
    headers = {"X-Tenant": tenant}
    if originator:
        headers["X-Originator"] = originator
    body = {"amount_cents": amount}
    if installment_id is not None:
        body["installment_id"] = installment_id
    return client.post(f"/orders/{order_id}/payments", json=body, headers=headers)


def _records(order_id: str, tenant: str) -> list:
    return client.get(f"/orders/{order_id}/payments", headers={"X-Tenant": tenant}).json()["records"]


def _plan(order_id: str, tenant: str, items: list) -> None:
    resp = client.post(
        f"/orders/{order_id}/installments",
        json={"installments": items},
        headers={"X-Tenant": tenant},
    )
    assert resp.status_code == 201, resp.text


def _search(tenant: str, **params):
    return client.get("/orders", params=params, headers={"X-Tenant": tenant})


PLAN = [
    {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01T00:00:00Z"},
    {"installment_id": "i2", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"},
]


# ---------- 基础检索与账面快照 ----------

def test_empty_conditions_returns_all_tenant_orders_sorted() -> None:
    tenant = "sea"
    for oid, amount in (("sa2", 200), ("sa1", 100), ("sa3", 300)):
        _order(oid, amount, tenant)
    resp = _search(tenant)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [o["order_id"] for o in body["orders"]] == ["sa1", "sa2", "sa3"]
    assert body["next_cursor"] == ""
    first = body["orders"][0]
    # 每条给出订单标识、订单金额、已收、未收、币种与当前状态
    assert set(first) == {"order_id", "amount_cents", "paid_cents", "outstanding_cents", "currency", "status"}
    assert first["amount_cents"] == 100 and first["paid_cents"] == 0
    assert first["outstanding_cents"] == 100 and first["status"] == "accepted"


def test_snapshot_reflects_current_books_settled_and_refunded() -> None:
    tenant = "seb"
    _order("sb1", 500, tenant)
    assert _pay("sb1", 500, tenant).status_code == 200
    settled = _search(tenant).json()["orders"]
    sb1 = next(o for o in settled if o["order_id"] == "sb1")
    assert sb1["paid_cents"] == 500 and sb1["outstanding_cents"] == 0 and sb1["status"] == "settled"

    # 部分退款使未收转正：快照与状态过滤都自然回到未结清
    pay_id = _records("sb1", tenant)[0]["record_id"]
    refund = client.post(
        f"/orders/sb1/payments/{pay_id}/refund",
        json={"amount_cents": 200},
        headers={"X-Tenant": tenant},
    )
    assert refund.status_code == 201
    sb1 = next(o for o in _search(tenant).json()["orders"] if o["order_id"] == "sb1")
    assert sb1["paid_cents"] == 300 and sb1["outstanding_cents"] == 200 and sb1["status"] == "accepted"


def test_no_matches_returns_empty_list_and_empty_cursor() -> None:
    resp = _search("tenant-without-orders")
    assert resp.status_code == 200
    assert resp.json() == {"orders": [], "next_cursor": ""}
    # 不命中任何订单的过滤条件同样为空结果而非报错
    resp = _search("sea", currency="ZZZ")
    assert resp.json() == {"orders": [], "next_cursor": ""}


# ---------- 条件过滤 ----------

def test_status_filter_follows_current_books() -> None:
    tenant = "sec"
    _order("sc1", 100, tenant)
    _order("sc2", 100, tenant)
    _pay("sc2", 100, tenant)  # sc2 结清
    assert [o["order_id"] for o in _search(tenant, status="settled").json()["orders"]] == ["sc2"]
    assert [o["order_id"] for o in _search(tenant, status="accepted").json()["orders"]] == ["sc1"]


def test_status_after_reversal_returns_to_unsettled() -> None:
    tenant = "sed"
    _order("sd1", 100, tenant)
    _pay("sd1", 100, tenant)
    pay_id = _records("sd1", tenant)[0]["record_id"]
    assert client.post(f"/orders/sd1/payments/{pay_id}/reversal", headers={"X-Tenant": tenant}).status_code == 201
    # 冲正使未收转正：已结清自然回到未结清
    assert [o["order_id"] for o in _search(tenant, status="accepted").json()["orders"]] == ["sd1"]
    assert _search(tenant, status="settled").json()["orders"] == []


def test_currency_filter() -> None:
    tenant = "see"
    _order("se1", 100, tenant, currency="CNY")
    _order("se2", 100, tenant, currency="USD")
    assert [o["order_id"] for o in _search(tenant, currency="CNY").json()["orders"]] == ["se1"]
    assert [o["order_id"] for o in _search(tenant, currency="USD").json()["orders"]] == ["se2"]


def test_amount_range_filter_is_inclusive_on_order_amount() -> None:
    tenant = "sef"
    for oid, amount in (("sf1", 100), ("sf2", 500), ("sf3", 1000)):
        _order(oid, amount, tenant)
    ids = [o["order_id"] for o in _search(tenant, amount_min=500, amount_max=500).json()["orders"]]
    assert ids == ["sf2"]
    ids = [o["order_id"] for o in _search(tenant, amount_min=100, amount_max=500).json()["orders"]]
    assert ids == ["sf1", "sf2"]


def test_originator_filter_matches_orders_ever_paid_by_originator() -> None:
    tenant = "seg"
    _order("sg1", 500, tenant)
    _order("sg2", 500, tenant)
    _order("sg3", 500, tenant)
    _pay("sg1", 100, tenant, originator="bob")
    _pay("sg2", 100, tenant, originator="alice")
    # sg1 的收款随后被冲正：“曾由 bob 发起收款”这一事实仍成立
    pay_id = _records("sg1", tenant)[0]["record_id"]
    client.post(f"/orders/sg1/payments/{pay_id}/reversal", headers={"X-Tenant": tenant})

    ids = [o["order_id"] for o in _search(tenant, originator="bob").json()["orders"]]
    assert ids == ["sg1"]
    ids = [o["order_id"] for o in _search(tenant, originator="alice").json()["orders"]]
    assert ids == ["sg2"]
    assert _search(tenant, originator="nobody").json()["orders"] == []


def test_has_installment_filter() -> None:
    tenant = "seh"
    _order("sh1", 1000, tenant)
    _order("sh2", 1000, tenant)
    _plan("sh1", tenant, PLAN)
    assert [o["order_id"] for o in _search(tenant, has_installment="true").json()["orders"]] == ["sh1"]
    assert [o["order_id"] for o in _search(tenant, has_installment="false").json()["orders"]] == ["sh2"]


def test_multiple_conditions_are_and_combined() -> None:
    tenant = "sei"
    _order("si1", 500, tenant, currency="CNY")
    _order("si2", 500, tenant, currency="USD")
    _order("si3", 5000, tenant, currency="CNY")
    _order("si4", 500, tenant, currency="CNY")
    _pay("si1", 100, tenant, originator="alice")   # CNY、区间内、alice 发起、未结清
    _pay("si4", 500, tenant, originator="alice")   # 已结清
    body = _search(
        tenant,
        status="accepted",
        currency="CNY",
        amount_min=100,
        amount_max=1000,
        originator="alice",
        has_installment="false",
    ).json()
    assert [o["order_id"] for o in body["orders"]] == ["si1"]


# ---------- 租户隔离 ----------

def test_search_is_scoped_to_current_tenant() -> None:
    _order("sj1", 100, "ten-a")
    _order("sj1", 100, "ten-b")  # 同标识、不同租户
    ids_a = [o["order_id"] for o in _search("ten-a").json()["orders"]]
    ids_b = [o["order_id"] for o in _search("ten-b").json()["orders"]]
    assert "sj1" in ids_a and "sj1" in ids_b
    # 任何过滤条件下都不返回其他租户的订单
    for params in ({}, {"currency": "CNY"}, {"originator": "x"}, {"status": "settled"}, {"has_installment": "true"}):
        rows = _search("ten-a", **params).json()["orders"]
        assert all(o["order_id"] != "sj-tenant-b-only" for o in rows)
    _order("sj-tenant-b-only", 100, "ten-b")
    assert all(o["order_id"] != "sj-tenant-b-only" for o in _search("ten-a").json()["orders"])


def test_search_requires_tenant_header() -> None:
    assert client.get("/orders").status_code == 400


# ---------- 分页稳定性 ----------

def _walk_all(tenant: str, page_size: int, **filters) -> list[str]:
    ids: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"page_size": page_size, **filters}
        if cursor:
            params["cursor"] = cursor
        body = _search(tenant, **params).json()
        pages += 1
        batch = [o["order_id"] for o in body["orders"]]
        ids.extend(batch)
        cursor = body["next_cursor"]
        if not cursor:
            break
        assert pages <= 100  # 防御：续取标记必须能走到末页
    return ids


def test_keyset_pagination_covers_every_order_once_in_order() -> None:
    tenant = "sek"
    for i in range(5):
        _order(f"sk{i}", 100, tenant)
    ids = _walk_all(tenant, 2)
    assert ids == [f"sk{i}" for i in range(5)]  # 不重不漏、升序
    assert len(set(ids)) == len(ids)


def test_default_page_size_applies() -> None:
    tenant = "sel"
    for i in range(21):
        _order(f"sl{i:02d}", 100, tenant)
    resp = _search(tenant)
    body = resp.json()
    assert len(body["orders"]) == 20 and body["next_cursor"] != ""
    rest = _search(tenant, cursor=body["next_cursor"]).json()
    assert [o["order_id"] for o in rest["orders"]] == ["sl20"] and rest["next_cursor"] == ""


def test_pagination_is_stable_under_inserts_and_changes_between_pages() -> None:
    tenant = "sem"
    for i in range(1, 6):
        _order(f"sm{i}", 100, tenant)

    first = _search(tenant, page_size=2).json()
    assert [o["order_id"] for o in first["orders"]] == ["sm1", "sm2"]

    # 翻页期间：已返回的 sm1 被结清；新增一条排在游标之前（sm0）和一条之后（sm6）
    _pay("sm1", 100, tenant)
    _order("sm0", 100, tenant)
    _order("sm6", 100, tenant)

    second = _search(tenant, page_size=2, cursor=first["next_cursor"]).json()
    assert [o["order_id"] for o in second["orders"]] == ["sm3", "sm4"]
    third = _search(tenant, page_size=2, cursor=second["next_cursor"]).json()
    # 已返回过的 sm1 不重复；游标之后的新单 sm6 能被取到，不被跳过；sm0 落在游标之前，不回卷
    assert [o["order_id"] for o in third["orders"]] == ["sm5", "sm6"]
    assert third["next_cursor"] == ""


def test_returned_order_changing_status_is_not_duplicated_on_later_pages() -> None:
    tenant = "sen"
    for i in range(4):
        _order(f"sn{i}", 100, tenant)
    first = _search(tenant, page_size=2, status="accepted").json()
    assert [o["order_id"] for o in first["orders"]] == ["sn0", "sn1"]
    # 已返回的 sn0 在翻页期间变为已结清：后续页不得再把它返回一次
    _pay("sn0", 100, tenant)
    second = _search(tenant, page_size=2, status="accepted", cursor=first["next_cursor"]).json()
    assert [o["order_id"] for o in second["orders"]] == ["sn2", "sn3"]
    assert second["next_cursor"] == ""


def test_cursor_is_opaque_and_reusable() -> None:
    tenant = "seo"
    for i in range(3):
        _order(f"so{i}", 100, tenant)
    first = _search(tenant, page_size=1).json()
    token = first["next_cursor"]
    assert token and "." not in token and "so0" not in token  # 不透明：不裸露排序键
    # 同一续取标记重放得到同一页
    again = _search(tenant, page_size=1, cursor=token).json()
    assert [o["order_id"] for o in again["orders"]] == ["so1"]


# ---------- 非法参数 ----------

def test_invalid_params_are_distinguishable_rejections() -> None:
    tenant = "sep"
    _order("sp1", 100, tenant)
    cases = [
        ({"amount_min": 500, "amount_max": 100}, "amount_min must not be greater than amount_max"),
        ({"page_size": 0}, "page_size must be a positive integer"),
        ({"page_size": -3}, "page_size must be a positive integer"),
        ({"page_size": "abc"}, "page_size must be a positive integer"),
        ({"status": "weird"}, "unsupported status filter"),
        ({"has_installment": "yes"}, "has_installment must be 'true' or 'false'"),
        ({"amount_min": -1}, "amount_min must be a non-negative integer"),
        ({"amount_max": "x"}, "amount_max must be a non-negative integer"),
        ({"cursor": "not-a-real-token!!"}, "cursor is not parseable"),
    ]
    for params, detail in cases:
        resp = _search(tenant, **params)
        assert resp.status_code == 400, params
        assert resp.json()["detail"] == detail, params

    # 全部拒绝都不改动数据：无条件检索仍是原来的订单
    assert [o["order_id"] for o in _search(tenant).json()["orders"]] == ["sp1"]


def test_well_formed_but_unknown_cursor_version_is_rejected() -> None:
    import base64
    import json

    forged = base64.urlsafe_b64encode(json.dumps({"v": "v9", "after": "sp1"}).encode()).decode().rstrip("=")
    resp = _search("sep", cursor=forged)
    assert resp.status_code == 400 and resp.json()["detail"] == "cursor is not parseable"
