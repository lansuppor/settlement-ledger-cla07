import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_order_payments.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store import payments as payment_store
from app.store import refunds as refund_store
from app.store import settlements as settlement_store
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def _new_order(order_id: str, tenant: str = "t1", amount: int = 1000) -> None:
    client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    )


def _pay(order_id: str, amount: int, key: str | None = None, tenant: str = "t1"):
    return client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": amount, **({"idempotency_key": key} if key else {})},
        headers={"X-Tenant": tenant},
    )


# ---------- 分期登记与订单闭合 ----------

def test_installments_register_and_order_exposes_flows() -> None:
    _new_order("p-o1", amount=500)
    r1 = _pay("p-o1", 200)
    assert r1.status_code == 200, r1.text
    f1 = r1.json()
    assert f1["amount_cents"] == 200 and f1["order_id"] == "p-o1"
    assert f1["status"] == "accepted" and f1["reversed_at"] is None
    assert f1["payment_id"] and f1["created_at"]

    r2 = _pay("p-o1", 300)
    assert r2.status_code == 200 and r2.json()["payment_id"] != f1["payment_id"]

    order = client.get("/orders/p-o1", headers=H).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0
    assert order["status"] == "settled"
    # 流水标识在订单查询结果中可读，记录金额与登记时刻，按登记顺序排列
    flows = order["payments"]
    assert [f["payment_id"] for f in flows] == [f1["payment_id"], r2.json()["payment_id"]]
    assert [f["amount_cents"] for f in flows] == [200, 300]
    assert all(f["created_at"] for f in flows)


def test_partial_payment_leaves_outstanding() -> None:
    _new_order("p-o2", amount=500)
    assert _pay("p-o2", 100).status_code == 200
    order = client.get("/orders/p-o2", headers=H).json()
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 400
    # 金额闭合
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]


def test_payment_cannot_exceed_order_amount() -> None:
    _new_order("p-o3", amount=300)
    assert _pay("p-o3", 200).status_code == 200
    resp = _pay("p-o3", 200)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "payment exceeds outstanding amount"
    # 订单与既有收款不被改动
    order = client.get("/orders/p-o3", headers=H).json()
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 100
    assert len(order["payments"]) == 1


def test_payment_on_unknown_or_cross_tenant_order_is_404() -> None:
    r1 = _pay("missing", 100)
    assert r1.status_code == 404 and r1.json()["detail"] == "order not found"
    _new_order("p-o3b", tenant="t1")
    r2 = _pay("p-o3b", 100, tenant="t2")
    assert r2.status_code == 404 and r2.json()["detail"] == "order not found"
    # 跨租户登记不产生流水、不动订单
    order = client.get("/orders/p-o3b", headers=H).json()
    assert order["paid_cents"] == 0 and order["payments"] == []


def test_invalid_amount_rejected() -> None:
    _new_order("p-o3c", amount=100)
    assert _pay("p-o3c", 0).status_code == 422


# ---------- 逐笔冲正 ----------

def test_reverse_returns_that_flow_amount_back() -> None:
    _new_order("p-o4", amount=500)
    f1 = _pay("p-o4", 200).json()
    f2 = _pay("p-o4", 300).json()

    resp = client.post(f"/payments/{f1['payment_id']}/reverse", headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "reversed" and body["reversed_at"] is not None
    assert body["payment_id"] == f1["payment_id"]

    order = client.get("/orders/p-o4", headers=H).json()
    # 只整体退回被冲正那一笔；另一笔不动；订单重新未结清
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 200
    assert order["status"] == "accepted"
    statuses = {f["payment_id"]: f["status"] for f in order["payments"]}
    assert statuses[f1["payment_id"]] == "reversed"
    assert statuses[f2["payment_id"]] == "accepted"

    # 再登记可补齐；已冲正流水不占用额度
    assert _pay("p-o4", 200).status_code == 200
    order = client.get("/orders/p-o4", headers=H).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0


def test_reverse_conflicts_are_distinguishable() -> None:
    _new_order("p-o5", amount=500)
    f = _pay("p-o5", 100).json()
    # 不存在的流水标识
    assert client.post("/payments/nope/reverse", headers=H).status_code == 404
    # 正常冲正
    assert client.post(f"/payments/{f['payment_id']}/reverse", headers=H).status_code == 200
    # 重复冲正：与“不存在”可区分
    again = client.post(f"/payments/{f['payment_id']}/reverse", headers=H)
    assert again.status_code == 409 and again.json()["detail"] == "payment already reversed"
    # 金额只退回一次
    order = client.get("/orders/p-o5", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 500


def test_cross_tenant_flow_read_and_reverse_are_not_found() -> None:
    _new_order("p-o6", tenant="t1")
    f = _pay("p-o6", 100, tenant="t1").json()
    assert client.get(f"/payments/{f['payment_id']}", headers={"X-Tenant": "t2"}).status_code == 404
    rev = client.post(f"/payments/{f['payment_id']}/reverse", headers={"X-Tenant": "t2"})
    assert rev.status_code == 404 and rev.json()["detail"] == "payment not found"
    # 其他租户的冲正不改变真实流水与订单
    assert client.get(f"/payments/{f['payment_id']}", headers=H).json()["status"] == "accepted"
    order = client.get("/orders/p-o6", headers=H).json()
    assert order["paid_cents"] == 100


# ---------- 冲正重开未收后：退款 / 结算前置条件，且既有单据不变 ----------

def test_reversal_reopens_order_and_blocks_refund_and_settlement() -> None:
    _new_order("p-o7", amount=1000)
    f = _pay("p-o7", 1000).json()
    # 已结清：退款单与结算单均可受理
    rf = client.post(
        "/refunds", json={"refund_id": "p-rf7", "order_id": "p-o7", "amount_cents": 300}, headers=H
    )
    assert rf.status_code == 201, rf.text

    # 冲正收款使未收重新 > 0
    assert client.post(f"/payments/{f['payment_id']}/reverse", headers=H).status_code == 200
    order = client.get("/orders/p-o7", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000

    # 不再受理退款 / 结算单，拒绝原因与既有“未结清”等原因可区分
    r_refund = client.post(
        "/refunds", json={"refund_id": "p-rf7b", "order_id": "p-o7", "amount_cents": 100}, headers=H
    )
    assert r_refund.status_code == 409
    assert r_refund.json()["detail"] == "order reopened by payment reversal"
    r_settlement = client.post(
        "/settlements",
        json={"settlement_id": "p-st7", "order_id": "p-o7", "amount_cents": 100},
        headers=H,
    )
    assert r_settlement.status_code == 409
    assert r_settlement.json()["detail"] == "order reopened by payment reversal"
    # 对比：从未结清的订单仍是既有原因
    _new_order("p-o7b", amount=1000)
    _pay("p-o7b", 100)
    plain = client.post(
        "/refunds", json={"refund_id": "p-rf7c", "order_id": "p-o7b", "amount_cents": 100}, headers=H
    )
    assert plain.status_code == 409 and plain.json()["detail"] == "order is not settled"
    # 不留部分数据
    assert client.get("/refunds/p-rf7b", headers=H).status_code == 404
    assert client.get("/settlements/p-st7", headers=H).status_code == 404

    # 既有退款单及其结论保持不变；退款链路金额不被收款冲正改写
    assert client.get("/refunds/p-rf7", headers=H).json()["status"] == "accepted"
    assert order["refunded_cents"] == 300 and order["refundable_cents"] == 700


def test_reversal_keeps_existing_settlement_chain_intact() -> None:
    _new_order("p-o8", amount=1000)
    f = _pay("p-o8", 1000).json()
    client.post(
        "/settlements", json={"settlement_id": "p-st8", "order_id": "p-o8", "amount_cents": 600}, headers=H
    )
    client.post("/settlements/p-st8/payments", json={"amount_cents": 400}, headers=H)

    # 冲正订单收款：结算单、其收款、订单累计已结算一律不变
    assert client.post(f"/payments/{f['payment_id']}/reverse", headers=H).status_code == 200
    st = client.get("/settlements/p-st8", headers=H).json()
    assert st["status"] == "accepted" and st["received_cents"] == 400
    order = client.get("/orders/p-o8", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000
    assert order["settled_cents"] == 600 and order["settleable_cents"] == 400
    assert order["refunded_cents"] == 0


# ---------- 幂等重试 ----------

def test_retry_with_same_idempotency_key_hits_first_conclusion() -> None:
    _new_order("p-o9", amount=500)
    first = _pay("p-o9", 200, key="key-9")
    assert first.status_code == 200
    # 超时重试：即使金额/参数不同也命中首次结论
    second = _pay("p-o9", 200, key="key-9")
    assert second.status_code == 200
    assert second.json()["payment_id"] == first.json()["payment_id"]

    order = client.get("/orders/p-o9", headers=H).json()
    assert order["paid_cents"] == 200 and len(order["payments"]) == 1

    # 首次流水被冲正后重试：仍命中同一条（已冲正）结论，不产生新流水、不二次变动
    assert client.post(f"/payments/{first.json()['payment_id']}/reverse", headers=H).status_code == 200
    third = _pay("p-o9", 200, key="key-9")
    assert third.status_code == 200 and third.json()["status"] == "reversed"
    order = client.get("/orders/p-o9", headers=H).json()
    assert order["paid_cents"] == 0 and len(order["payments"]) == 1


# ---------- 条件检索 ----------

def test_search_filters_combine_and_pagination_is_stable() -> None:
    _new_order("p-q1", amount=100000)
    _new_order("p-q2", amount=100000)
    a = _pay("p-q1", 100).json()
    b = _pay("p-q1", 200).json()
    c = _pay("p-q2", 300).json()
    d = _pay("p-q1", 400).json()
    client.post(f"/payments/{b['payment_id']}/reverse", headers=H)

    # 按订单
    resp = client.get("/payments", params={"order_id": "p-q1"}, headers=H)
    assert [f["payment_id"] for f in resp.json()["items"]] == [
        a["payment_id"],
        b["payment_id"],
        d["payment_id"],
    ]
    # 金额区间（闭区间），与订单条件组合
    resp = client.get(
        "/payments",
        params={"order_id": "p-q1", "min_amount_cents": 150, "max_amount_cents": 350},
        headers=H,
    )
    assert [f["payment_id"] for f in resp.json()["items"]] == [b["payment_id"]]
    resp = client.get(
        "/payments", params={"min_amount_cents": 250, "max_amount_cents": 350}, headers=H
    )
    assert c["payment_id"] in [f["payment_id"] for f in resp.json()["items"]]
    # 仅未冲正 / 仅已冲正（本订单内 b 是唯一已冲正流水）
    resp = client.get("/payments", params={"order_id": "p-q1", "reversed_only": "true"}, headers=H)
    assert [f["payment_id"] for f in resp.json()["items"]] == [b["payment_id"]]
    resp = client.get("/payments", params={"order_id": "p-q1", "reversed_only": "false"}, headers=H)
    assert [f["payment_id"] for f in resp.json()["items"]] == [a["payment_id"], d["payment_id"]]
    # 登记时间区间（含端点）
    resp = client.get(
        "/payments", params={"created_from": a["created_at"], "created_to": a["created_at"]}, headers=H
    )
    assert [f["payment_id"] for f in resp.json()["items"]] == [a["payment_id"]]
    # 非法时间
    bad = client.get("/payments", params={"created_from": "not-a-time"}, headers=H)
    assert bad.status_code == 400

    # 分页不重不漏：limit=2 走完游标
    seen: list[str] = []
    cursor = None
    while True:
        params = {"order_id": "p-q1", "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.get("/payments", params=params, headers=H).json()
        seen.extend(f["payment_id"] for f in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    full = [f["payment_id"] for f in client.get("/payments", params={"order_id": "p-q1"}, headers=H).json()["items"]]
    assert seen == full and len(seen) == len(set(seen))

    # 同一查询重复执行结果一致
    again = client.get("/payments", params={"order_id": "p-q1"}, headers=H).json()
    assert [f["payment_id"] for f in again["items"]] == full


def test_search_is_tenant_scoped() -> None:
    _new_order("p-q3", tenant="t1")
    f = _pay("p-q3", 100, tenant="t1").json()
    other = client.get("/payments", params={"order_id": "p-q3"}, headers={"X-Tenant": "t2"})
    assert other.status_code == 200 and other.json()["items"] == []
    assert client.get(f"/payments/{f['payment_id']}", headers={"X-Tenant": "t2"}).status_code == 404


# ---------- 并发 / 重启 ----------

def test_concurrent_registers_never_overshoot_order_amount() -> None:
    _new_order("p-c1", amount=1000)

    def call() -> str:
        try:
            result, _ = payment_store.register("t1", "p-c1", 300, None)
        except payment_store.PaymentExceedsOutstanding:
            return "exceeded"
        return result

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(8)))

    assert results.count("registered") == 3  # 3 × 300 = 900，第 4 笔必超限
    assert "exceeded" in results
    order = order_store.get("t1", "p-c1")
    assert order["paid_cents"] == 900 and order["outstanding_cents"] == 100


def test_concurrent_reverse_same_flow_succeeds_at_most_once() -> None:
    _new_order("p-c2", amount=1000)
    _, flow = payment_store.register("t1", "p-c2", 400, None)

    def call() -> str:
        try:
            payment_store.reverse("t1", flow["payment_id"])
        except payment_store.PaymentAlreadyReversed:
            return "already"
        return "reversed"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(12)))

    assert results.count("reversed") == 1 and results.count("already") == 11
    order = order_store.get("t1", "p-c2")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000


def test_concurrent_retry_same_key_creates_single_flow() -> None:
    _new_order("p-c3", amount=1000)

    def call() -> tuple[str, dict]:
        return payment_store.register("t1", "p-c3", 200, "same-key")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(8)))

    assert len({f["payment_id"] for _, f in results}) == 1
    assert [r for r, _ in results].count("registered") == 1
    order = order_store.get("t1", "p-c3")
    assert order["paid_cents"] == 200 and len(order["payments"]) == 1


def test_state_survives_restart_and_migrate_remains_idempotent() -> None:
    _new_order("p-p1", amount=800)
    f1 = _pay("p-p1", 300, key="persist-1").json()
    f2 = _pay("p-p1", 200).json()
    client.post(f"/payments/{f2['payment_id']}/reverse", headers=H)

    migrate()
    migrate()

    # 流水、已冲正标记、金额、幂等结论重启后仍可判定
    assert client.get(f"/payments/{f1['payment_id']}", headers=H).json()["status"] == "accepted"
    assert client.get(f"/payments/{f2['payment_id']}", headers=H).json()["status"] == "reversed"
    order = client.get("/orders/p-p1", headers=H).json()
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 500
    # 幂等键重试仍命中首次流水
    assert _pay("p-p1", 300, key="persist-1").json()["payment_id"] == f1["payment_id"]
    # 可继续登记与冲正
    assert _pay("p-p1", 500).status_code == 200
    assert client.post(f"/payments/{f1['payment_id']}/reverse", headers=H).status_code == 200
    order = client.get("/orders/p-p1", headers=H).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 300
    # 退款 / 结算链路完全未被收款分期触碰
    assert order["refunded_cents"] == 0 and order["settled_cents"] == 0
    assert refund_store.get("t1", "p-p1x") is None
    assert settlement_store.get("t1", "p-p1x") is None
