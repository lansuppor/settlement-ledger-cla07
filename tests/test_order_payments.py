import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_payments.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store import payments as payment_store
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def _order(order_id: str, tenant: str = "t1", amount: int = 1000) -> None:
    client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    )


# ---------- 分期登记 / 订单闭合 ----------

def test_installments_register_and_order_exposes_payments() -> None:
    _order("p-o1", amount=500)
    r1 = client.post("/orders/p-o1/payments", json={"amount_cents": 200}, headers=H)
    assert r1.status_code == 201, r1.text
    p1 = r1.json()
    assert p1["amount_cents"] == 200 and p1["status"] == "accepted" and p1["reversed"] is False
    assert p1["payment_id"] and p1["created_at"]

    r2 = client.post("/orders/p-o1/payments", json={"amount_cents": 300}, headers=H)
    assert r2.status_code == 201
    p2 = r2.json()
    assert p2["payment_id"] != p1["payment_id"]

    order = client.get("/orders/p-o1", headers=H).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0
    ids = [p["payment_id"] for p in order["payments"]]
    assert ids == [p1["payment_id"], p2["payment_id"]]  # 登记顺序稳定
    # 金额闭合：未收 = 订单金额 − 累计已收
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]


def test_register_cannot_exceed_order_amount_and_leaves_no_trace() -> None:
    _order("p-o2", amount=300)
    client.post("/orders/p-o2/payments", json={"amount_cents": 200}, headers=H)
    resp = client.post("/orders/p-o2/payments", json={"amount_cents": 200}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "payment exceeds outstanding amount"
    # 订单与既有收款不被改动，且不产生新流水
    order = client.get("/orders/p-o2", headers=H).json()
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 100
    assert len(order["payments"]) == 1 and order["payments"][0]["amount_cents"] == 200


def test_register_on_unknown_or_cross_tenant_order_is_404() -> None:
    r1 = client.post("/orders/missing/payments", json={"amount_cents": 100}, headers=H)
    assert r1.status_code == 404 and r1.json()["detail"] == "order not found"
    _order("p-o2b", tenant="t1")
    r2 = client.post("/orders/p-o2b/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t2"})
    assert r2.status_code == 404 and r2.json()["detail"] == "order not found"
    assert client.get("/orders/p-o2b", headers=H).json()["paid_cents"] == 0


def test_invalid_amount_rejected() -> None:
    _order("p-o2c")
    assert client.post("/orders/p-o2c/payments", json={"amount_cents": 0}, headers=H).status_code == 422


# ---------- 冲正 / 重复与冲突可区分 ----------

def test_reverse_returns_amount_and_closes_conservation() -> None:
    _order("p-o3", amount=500)
    p1 = client.post("/orders/p-o3/payments", json={"amount_cents": 300}, headers=H).json()
    p2 = client.post("/orders/p-o3/payments", json={"amount_cents": 200}, headers=H).json()
    resp = client.post(f"/payments/{p1['payment_id']}/reverse", headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reversed" and resp.json()["reversed"] is True
    assert resp.json()["reversed_at"] is not None

    order = client.get("/orders/p-o3", headers=H).json()
    # 仅 p1 整体退回，p2 仍有效
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 300
    assert order["payments"][0]["status"] == "reversed"
    assert order["payments"][1]["status"] == "accepted"
    assert p2["payment_id"]  # 第二条流水不受影响


def test_reverse_conflicts_are_distinguishable() -> None:
    _order("p-o4", amount=500)
    p = client.post("/orders/p-o4/payments", json={"amount_cents": 100}, headers=H).json()
    # 不存在的流水标识
    assert client.post("/payments/nope/reverse", headers=H).status_code == 404
    # 正常冲正
    assert client.post(f"/payments/{p['payment_id']}/reverse", headers=H).status_code == 200
    # 重复冲正
    again = client.post(f"/payments/{p['payment_id']}/reverse", headers=H)
    assert again.status_code == 409 and again.json()["detail"] == "payment already reversed"
    # 金额只退回一次
    assert client.get("/orders/p-o4", headers=H).json()["paid_cents"] == 0


def test_cross_tenant_reverse_is_not_found_and_does_nothing() -> None:
    _order("p-o5", tenant="t1")
    p = client.post("/orders/p-o5/payments", json={"amount_cents": 100}, headers=H).json()
    resp = client.post(f"/payments/{p['payment_id']}/reverse", headers={"X-Tenant": "t2"})
    assert resp.status_code == 404 and resp.json()["detail"] == "payment not found"
    doc = client.get("/orders/p-o5", headers=H).json()
    assert doc["paid_cents"] == 100 and doc["payments"][0]["status"] == "accepted"


# ---------- 幂等重试 ----------

def test_register_retry_with_same_idempotency_key_hits_one_conclusion() -> None:
    _order("p-o6", amount=500)
    body = {"amount_cents": 200, "idempotency_key": "idem-1"}
    first = client.post("/orders/p-o6/payments", json=body, headers=H)
    assert first.status_code == 201
    # 重试：不产生第二条流水、不二次入账
    second = client.post("/orders/p-o6/payments", json=body, headers=H)
    assert second.status_code == 409
    assert second.json()["detail"] == "payment already registered"
    order = client.get("/orders/p-o6", headers=H).json()
    assert order["paid_cents"] == 200 and len(order["payments"]) == 1


# ---------- 冲正后退款/结算被拒（原因可区分，链路守恒） ----------

def test_refund_and_settlement_blocked_after_reversal_with_distinct_reason() -> None:
    _order("p-o7", amount=500)
    p = client.post("/orders/p-o7/payments", json={"amount_cents": 500}, headers=H).json()
    # 先受理一张退款单与一张结算单（冲正前订单已结清）
    rf = client.post(
        "/refunds", json={"refund_id": "p-rf7", "order_id": "p-o7", "amount_cents": 100}, headers=H
    )
    assert rf.status_code == 201, rf.text
    # 结算单要求无退款，先冲正退款再受理结算单
    client.post("/refunds/p-rf7/reverse", headers=H)
    st = client.post(
        "/settlements", json={"settlement_id": "p-st7", "order_id": "p-o7", "amount_cents": 200}, headers=H
    )
    assert st.status_code == 201, st.text

    # 冲正收款使未收重新大于 0
    assert client.post(f"/payments/{p['payment_id']}/reverse", headers=H).status_code == 200
    order = client.get("/orders/p-o7", headers=H).json()
    assert order["outstanding_cents"] == 500 and order["paid_cents"] == 0
    # 退款/结算链路金额不被收款冲正改写
    assert order["refunded_cents"] == 0 and order["settled_cents"] == 200

    # 新退款被拒：原因为“收款被冲正”，与“从未结清”可区分
    r1 = client.post(
        "/refunds", json={"refund_id": "p-rf7b", "order_id": "p-o7", "amount_cents": 100}, headers=H
    )
    assert r1.status_code == 409 and r1.json()["detail"] == "order payment reversed"
    # 新结算单被拒：同样的可区分原因
    r2 = client.post(
        "/settlements", json={"settlement_id": "p-st7b", "order_id": "p-o7", "amount_cents": 100}, headers=H
    )
    assert r2.status_code == 409 and r2.json()["detail"] == "order payment reversed"
    # 不留部分数据
    assert client.get("/refunds/p-rf7b", headers=H).status_code == 404
    assert client.get("/settlements/p-st7b", headers=H).status_code == 404
    # 已有退款单、结算单及其结论保持不变
    assert client.get("/refunds/p-rf7", headers=H).json()["status"] == "reversed"
    assert client.get("/settlements/p-st7", headers=H).json()["status"] == "accepted"


def test_never_settled_order_still_returns_not_settled_reason() -> None:
    _order("p-o8", amount=500)
    client.post("/orders/p-o8/payments", json={"amount_cents": 100}, headers=H)  # 从未结清
    r = client.post(
        "/refunds", json={"refund_id": "p-rf8", "order_id": "p-o8", "amount_cents": 10}, headers=H
    )
    assert r.status_code == 409 and r.json()["detail"] == "order is not settled"


# ---------- 条件检索 / 分页 ----------

def test_search_combines_filters_and_paginates_stably() -> None:
    _order("p-o9", amount=1000)
    a = client.post("/orders/p-o9/payments", json={"amount_cents": 100}, headers=H).json()
    b = client.post("/orders/p-o9/payments", json={"amount_cents": 300}, headers=H).json()
    c = client.post("/orders/p-o9/payments", json={"amount_cents": 100}, headers=H).json()
    client.post(f"/payments/{b['payment_id']}/reverse", headers=H)

    # 按订单 + 金额区间
    resp = client.get("/payments", params={"order_id": "p-o9", "min_amount": 100, "max_amount": 200}, headers=H)
    assert [i["payment_id"] for i in resp.json()["items"]] == [a["payment_id"], c["payment_id"]]

    # 仅未冲正
    resp = client.get("/payments", params={"order_id": "p-o9", "reversed": "false"}, headers=H)
    assert [i["payment_id"] for i in resp.json()["items"]] == [a["payment_id"], c["payment_id"]]
    # 仅已冲正
    resp = client.get("/payments", params={"order_id": "p-o9", "reversed": "true"}, headers=H)
    assert [i["payment_id"] for i in resp.json()["items"]] == [b["payment_id"]]

    # 登记时间区间（用宽松边界覆盖本测试全部流水）
    resp = client.get(
        "/payments",
        params={"order_id": "p-o9", "created_from": "2000-01-01T00:00:00+00:00",
                "created_to": "2100-01-01T00:00:00+00:00"},
        headers=H,
    )
    assert [i["payment_id"] for i in resp.json()["items"]] == [
        a["payment_id"], b["payment_id"], c["payment_id"]
    ]

    # 游标分页不重不漏
    page1 = client.get("/payments", params={"order_id": "p-o9", "limit": 2}, headers=H).json()
    assert len(page1["items"]) == 2 and page1["next_seq"] == b["seq"]
    page2 = client.get(
        "/payments", params={"order_id": "p-o9", "limit": 2, "after_seq": page1["next_seq"]}, headers=H
    ).json()
    assert [i["payment_id"] for i in page2["items"]] == [c["payment_id"]]
    assert page2["next_seq"] is None
    seen = [i["payment_id"] for i in page1["items"]] + [i["payment_id"] for i in page2["items"]]
    assert seen == [a["payment_id"], b["payment_id"], c["payment_id"]]

    # 同一查询重复执行结果一致
    again = client.get("/payments", params={"order_id": "p-o9", "limit": 2}, headers=H).json()
    assert [i["payment_id"] for i in again["items"]] == [i["payment_id"] for i in page1["items"]]


def test_search_is_tenant_scoped() -> None:
    _order("p-o10", tenant="t1")
    p = client.post("/orders/p-o10/payments", json={"amount_cents": 100}, headers=H).json()
    other = client.get("/payments", params={"order_id": "p-o10"}, headers={"X-Tenant": "t2"})
    assert other.status_code == 200 and other.json()["items"] == []
    assert client.get("/payments", headers=H).json()["items"]  # 本租户有数据
    assert p["payment_id"]


# ---------- 并发 ----------

def test_concurrent_registers_never_overshoot_order_amount() -> None:
    _order("p-c1", amount=1000)

    def call() -> str:
        try:
            payment_store.register("t1", "p-c1", 300)
        except payment_store.PaymentExceedsOutstanding:
            return "exceeded"
        return "registered"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(8)))

    assert results.count("registered") == 3  # 3 × 300，余 100
    assert results.count("exceeded") == 5
    order = order_store.get("t1", "p-c1")
    assert order["paid_cents"] == 900 and order["outstanding_cents"] == 100


def test_concurrent_reverse_same_payment_succeeds_at_most_once() -> None:
    _order("p-c2", amount=1000)
    _, p = payment_store.register("t1", "p-c2", 400)

    def call() -> str:
        try:
            payment_store.reverse("t1", p["payment_id"])
        except payment_store.PaymentAlreadyReversed:
            return "already"
        return "reversed"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(12)))

    assert results.count("reversed") == 1
    assert results.count("already") == 11
    assert order_store.get("t1", "p-c2")["paid_cents"] == 0
    assert payment_store.get("t1", p["payment_id"])["status"] == "reversed"


def test_concurrent_register_retry_with_idempotency_key_makes_one_payment() -> None:
    _order("p-c3", amount=1000)

    def call() -> str:
        result, _ = payment_store.register("t1", "p-c3", 200, idempotency_key="idem-c3")
        return result

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(8)))

    assert results.count("accepted") == 1
    assert results.count("duplicate") == 7
    order = client.get("/orders/p-c3", headers=H).json()
    assert order["paid_cents"] == 200 and len(order["payments"]) == 1


# ---------- 重启持久化 ----------

def test_state_survives_restart() -> None:
    _order("p-p1", amount=800)
    p1 = client.post("/orders/p-p1/payments", json={"amount_cents": 500}, headers=H).json()
    p2 = client.post("/orders/p-p1/payments", json={"amount_cents": 300}, headers=H).json()
    client.post(f"/payments/{p1['payment_id']}/reverse", headers=H)

    migrate()
    migrate()

    order = client.get("/orders/p-p1", headers=H).json()
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 500
    statuses = {p["payment_id"]: p["status"] for p in order["payments"]}
    assert statuses[p1["payment_id"]] == "reversed" and statuses[p2["payment_id"]] == "accepted"
    # 冲正已持久化，重复冲正仍被拒
    again = client.post(f"/payments/{p1['payment_id']}/reverse", headers=H)
    assert again.status_code == 409 and again.json()["detail"] == "payment already reversed"
    # 收清后仍可受理退款（ever_settled 已置位不影响重新收清）
    client.post("/orders/p-p1/payments", json={"amount_cents": 500}, headers=H)
    assert (
        client.post(
            "/refunds", json={"refund_id": "p-rfp1", "order_id": "p-p1", "amount_cents": 100}, headers=H
        ).status_code
        == 201
    )
    # 退款与结算链路金额始终独立守恒
    order = client.get("/orders/p-p1", headers=H).json()
    assert order["paid_cents"] == 800 and order["refunded_cents"] == 100
    assert order["refunded_cents"] + order["refundable_cents"] == order["amount_cents"]
