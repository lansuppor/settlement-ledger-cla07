import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_settlements.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store import refunds as refund_store
from app.store import settlements as settlement_store
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def _settled_order(order_id: str, tenant: str = "t1", amount: int = 1000) -> None:
    client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    )
    client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})


# ---------- 正常受理 / 读取 / 订单守恒 ----------

def test_accept_and_read_settlement() -> None:
    _settled_order("s-o1", amount=1000)
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-1", "order_id": "s-o1", "amount_cents": 400},
        headers=H,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["amount_cents"] == 400
    assert body["order_id"] == "s-o1"
    assert body["received_cents"] == 0
    assert body["unreceived_cents"] == 400
    assert body["reversed_at"] is None

    got = client.get("/settlements/s-1", headers=H)
    assert got.status_code == 200 and got.json()["settlement_id"] == "s-1"


def test_order_exposes_settled_and_settleable_and_conserves() -> None:
    _settled_order("s-o2", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-2", "order_id": "s-o2", "amount_cents": 400},
        headers=H,
    )
    order = client.get("/orders/s-o2", headers=H).json()
    # 订单收款链路不被改写
    assert order["paid_cents"] == 1000
    assert order["outstanding_cents"] == 0
    assert order["settled_cents"] == 400
    assert order["settleable_cents"] == 600
    # 累计已结算 + 剩余可结算 与订单金额守恒
    assert order["settled_cents"] + order["settleable_cents"] == order["amount_cents"]
    # 退款链路字段不受结算影响
    assert order["refunded_cents"] == 0
    assert order["refundable_cents"] == 1000


# ---------- 分期收款 ----------

def test_installment_payments_accumulate() -> None:
    _settled_order("s-o3", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-3", "order_id": "s-o3", "amount_cents": 600},
        headers=H,
    )
    r1 = client.post("/settlements/s-3/payments", json={"amount_cents": 200}, headers=H)
    assert r1.status_code == 200, r1.text
    assert r1.json()["received_cents"] == 200
    assert r1.json()["unreceived_cents"] == 400
    # 订单已收金额不被结算收款改写
    assert client.get("/orders/s-o3", headers=H).json()["paid_cents"] == 1000

    r2 = client.post("/settlements/s-3/payments", json={"amount_cents": 400}, headers=H)
    assert r2.status_code == 200
    assert r2.json()["received_cents"] == 600
    assert r2.json()["unreceived_cents"] == 0
    # 订单金额占用不随收款变化
    assert client.get("/orders/s-o3", headers=H).json()["settled_cents"] == 600


def test_payment_cannot_exceed_settlement_amount() -> None:
    _settled_order("s-o4", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-4", "order_id": "s-o4", "amount_cents": 300},
        headers=H,
    )
    client.post("/settlements/s-4/payments", json={"amount_cents": 200}, headers=H)
    resp = client.post("/settlements/s-4/payments", json={"amount_cents": 200}, headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "payment exceeds unreceived amount"
    # 不改动结算单
    doc = client.get("/settlements/s-4", headers=H).json()
    assert doc["received_cents"] == 200 and doc["unreceived_cents"] == 100
    # 订单数据不变
    order = client.get("/orders/s-o4", headers=H).json()
    assert order["settled_cents"] == 300 and order["paid_cents"] == 1000


def test_payment_on_unknown_or_cross_tenant_settlement_is_404() -> None:
    assert (
        client.post("/settlements/nope/payments", json={"amount_cents": 100}, headers=H).status_code
        == 404
    )
    _settled_order("s-o4b", amount=500)
    client.post(
        "/settlements",
        json={"settlement_id": "s-4b", "order_id": "s-o4b", "amount_cents": 200},
        headers=H,
    )
    cross = client.post(
        "/settlements/s-4b/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t2"}
    )
    assert cross.status_code == 404 and cross.json()["detail"] == "settlement not found"
    # 跨租户收款不改变真实单据
    assert client.get("/settlements/s-4b", headers=H).json()["received_cents"] == 0


# ---------- 冲正 ----------

def test_reverse_without_payments_releases_order_balance() -> None:
    _settled_order("s-o5", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-5", "order_id": "s-o5", "amount_cents": 600},
        headers=H,
    )
    resp = client.post("/settlements/s-5/reverse", headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reversed"
    assert resp.json()["reversed_at"] is not None

    order = client.get("/orders/s-o5", headers=H).json()
    # 整体释放占用的剩余结算金额
    assert order["settled_cents"] == 0
    assert order["settleable_cents"] == 1000
    # 收款/退款链路均不变
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0
    assert order["refunded_cents"] == 0

    # 释放出的额度可被新的结算单占用
    again = client.post(
        "/settlements",
        json={"settlement_id": "s-5b", "order_id": "s-o5", "amount_cents": 1000},
        headers=H,
    )
    assert again.status_code == 201, again.text


def test_reverse_refused_when_payments_exist_and_distinguishable() -> None:
    _settled_order("s-o6", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-6", "order_id": "s-o6", "amount_cents": 500},
        headers=H,
    )
    client.post("/settlements/s-6/payments", json={"amount_cents": 100}, headers=H)
    resp = client.post("/settlements/s-6/reverse", headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settlement has payments"
    # 订单与结算单数据保持不变
    assert client.get("/settlements/s-6", headers=H).json()["status"] == "accepted"
    order = client.get("/orders/s-o6", headers=H).json()
    assert order["settled_cents"] == 500


def test_reverse_conflicts_are_distinguishable() -> None:
    _settled_order("s-o7", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-7", "order_id": "s-o7", "amount_cents": 100},
        headers=H,
    )
    # 对未受理的标识冲正
    assert client.post("/settlements/nope/reverse", headers=H).status_code == 404
    # 正常冲正
    assert client.post("/settlements/s-7/reverse", headers=H).status_code == 200
    # 重复冲正
    again = client.post("/settlements/s-7/reverse", headers=H)
    assert again.status_code == 409
    assert again.json()["detail"] == "settlement already reversed"
    # 冲正后同一标识不得再次受理
    reaccept = client.post(
        "/settlements",
        json={"settlement_id": "s-7", "order_id": "s-o7", "amount_cents": 100},
        headers=H,
    )
    assert reaccept.status_code == 409
    assert reaccept.json()["detail"] == "settlement already reversed"
    # 冲正后也不得继续收款
    pay = client.post("/settlements/s-7/payments", json={"amount_cents": 100}, headers=H)
    assert pay.status_code == 409
    assert pay.json()["detail"] == "settlement already reversed"
    order = client.get("/orders/s-o7", headers=H).json()
    assert order["settled_cents"] == 0


# ---------- 受理拒绝原因可区分，且不留部分数据 ----------

def test_settlement_requires_settled_order() -> None:
    client.post(
        "/orders",
        json={"tenant": "t1", "order_id": "s-o8", "amount_cents": 500, "currency": "CNY"},
    )
    client.post("/orders/s-o8/payments", json={"amount_cents": 100}, headers=H)
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-8", "order_id": "s-o8", "amount_cents": 100},
        headers=H,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "order is not settled"
    assert client.get("/settlements/s-8", headers=H).status_code == 404
    order = client.get("/orders/s-o8", headers=H).json()
    assert order["settled_cents"] == 0 and order["paid_cents"] == 100


def test_settlement_requires_no_refunds() -> None:
    _settled_order("s-o9", amount=500)
    client.post(
        "/refunds",
        json={"refund_id": "s-rf9", "order_id": "s-o9", "amount_cents": 100},
        headers=H,
    )
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-9", "order_id": "s-o9", "amount_cents": 100},
        headers=H,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "order has refunds"
    assert client.get("/settlements/s-9", headers=H).status_code == 404
    order = client.get("/orders/s-o9", headers=H).json()
    assert order["settled_cents"] == 0 and order["refunded_cents"] == 100


def test_settlement_cannot_exceed_settleable_balance() -> None:
    _settled_order("s-o10", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-10a", "order_id": "s-o10", "amount_cents": 600},
        headers=H,
    )
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-10b", "order_id": "s-o10", "amount_cents": 500},
        headers=H,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settlement exceeds settleable amount"
    assert client.get("/settlements/s-10b", headers=H).status_code == 404
    order = client.get("/orders/s-o10", headers=H).json()
    assert order["settled_cents"] == 600 and order["settleable_cents"] == 400


def test_reversed_settlement_not_counted_in_settleable_balance() -> None:
    _settled_order("s-o11", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-11a", "order_id": "s-o11", "amount_cents": 800},
        headers=H,
    )
    client.post("/settlements/s-11a/reverse", headers=H)
    # 已冲正部分不占用剩余可结算金额：1000 仍可全额受理
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-11b", "order_id": "s-o11", "amount_cents": 1000},
        headers=H,
    )
    assert resp.status_code == 201, resp.text
    order = client.get("/orders/s-o11", headers=H).json()
    assert order["settled_cents"] == 1000 and order["settleable_cents"] == 0


def test_order_not_found_and_cross_tenant_are_indistinguishable() -> None:
    r1 = client.post(
        "/settlements",
        json={"settlement_id": "s-12a", "order_id": "missing", "amount_cents": 100},
        headers=H,
    )
    assert r1.status_code == 404 and r1.json()["detail"] == "order not found"

    _settled_order("s-o12", tenant="t1")
    r2 = client.post(
        "/settlements",
        json={"settlement_id": "s-12b", "order_id": "s-o12", "amount_cents": 100},
        headers={"X-Tenant": "t2"},
    )
    assert r2.status_code == 404 and r2.json()["detail"] == "order not found"


# ---------- 幂等、重复与跨租户 ----------

def test_duplicate_accept_is_idempotent_and_keeps_first_doc() -> None:
    _settled_order("s-o13", amount=1000)
    first = client.post(
        "/settlements",
        json={"settlement_id": "s-13", "order_id": "s-o13", "amount_cents": 200},
        headers=H,
    )
    assert first.status_code == 201
    # 用不同金额、不同原订单重复受理：结论确定，不改变首次单据
    second = client.post(
        "/settlements",
        json={"settlement_id": "s-13", "order_id": "s-o13", "amount_cents": 900},
        headers=H,
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "settlement already accepted"

    doc = client.get("/settlements/s-13", headers=H).json()
    assert doc["amount_cents"] == 200 and doc["status"] == "accepted"
    order = client.get("/orders/s-o13", headers=H).json()
    assert order["settled_cents"] == 200  # 金额只占用一次


def test_cross_tenant_read_and_reverse_are_not_found() -> None:
    _settled_order("s-o14", tenant="t1")
    client.post(
        "/settlements",
        json={"settlement_id": "s-14", "order_id": "s-o14", "amount_cents": 100},
        headers=H,
    )
    assert client.get("/settlements/s-14", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/settlements/s-14/reverse", headers={"X-Tenant": "t2"}).status_code == 404
    # 其他租户的冲正不改变真实单据
    assert client.get("/settlements/s-14", headers=H).json()["status"] == "accepted"
    assert client.get("/orders/s-o14", headers=H).json()["settled_cents"] == 100


def test_invalid_amount_rejected() -> None:
    _settled_order("s-o15", amount=500)
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-15", "order_id": "s-o15", "amount_cents": 0},
        headers=H,
    )
    assert resp.status_code == 422


def test_tenant_header_required() -> None:
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-16", "order_id": "s-o15", "amount_cents": 100},
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "tenant header is required"


# ---------- 与退款链路的隔离 ----------

def test_settlement_chain_does_not_touch_refund_chain() -> None:
    _settled_order("s-o17", amount=1000)
    # 先退款（此后不可再受理新结算单，但已受理的结算链路不受影响）
    client.post(
        "/refunds",
        json={"refund_id": "s-rf17", "order_id": "s-o17", "amount_cents": 300},
        headers=H,
    )
    # 退款链路自身的守恒
    order = client.get("/orders/s-o17", headers=H).json()
    assert order["refunded_cents"] == 300 and order["refundable_cents"] == 700
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0

    # 冲正退款，使订单回到无退款状态后受理结算单
    client.post("/refunds/s-rf17/reverse", headers=H)
    client.post(
        "/settlements",
        json={"settlement_id": "s-17", "order_id": "s-o17", "amount_cents": 400},
        headers=H,
    )
    # 分期收款只动结算单，不触碰订单收款与退款链路
    assert (
        client.post("/settlements/s-17/payments", json={"amount_cents": 250}, headers=H).status_code
        == 200
    )
    mid = client.get("/orders/s-o17", headers=H).json()
    assert mid["settled_cents"] == 400 and mid["settleable_cents"] == 600
    assert mid["refunded_cents"] == 0 and mid["paid_cents"] == 1000

    # 有收款时冲正被拒，订单与结算单均不变
    blocked = client.post("/settlements/s-17/reverse", headers=H)
    assert blocked.status_code == 409 and blocked.json()["detail"] == "settlement has payments"

    # 另一张无收款结算单冲正后整体释放占用
    client.post(
        "/settlements",
        json={"settlement_id": "s-17b", "order_id": "s-o17", "amount_cents": 300},
        headers=H,
    )
    assert client.post("/settlements/s-17b/reverse", headers=H).status_code == 200

    order = client.get("/orders/s-o17", headers=H).json()
    # s-17b 整体释放，s-17 仍占用 400；退款与订单收款链路始终不变
    assert order["settled_cents"] == 400 and order["settleable_cents"] == 600
    assert order["refunded_cents"] == 0 and order["refundable_cents"] == 1000
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


# ---------- 并发 / 重试 / 重启 ----------

def test_concurrent_accept_same_id_succeeds_at_most_once() -> None:
    _settled_order("s-c1", amount=1000)

    def call() -> tuple[str, dict | None]:
        return settlement_store.accept("t1", "s-c1r", "s-c1", 200)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(12)))

    accepted = [r for r in results if r[0] == "accepted"]
    assert len(accepted) == 1
    assert len(results) - len(accepted) == 11  # 其余全部 duplicate
    order = order_store.get("t1", "s-c1")
    assert order["settled_cents"] == 200  # 金额不重复占用


def test_concurrent_accepts_never_overshoot_settleable_balance() -> None:
    _settled_order("s-c2", amount=1000)

    def call(i: int) -> str:
        try:
            result, _ = settlement_store.accept("t1", f"s-c2r-{i}", "s-c2", 200)
        except settlement_store.SettlementExceedsBalance:
            return "exceeded"
        return result

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(call, range(8)))

    assert results.count("accepted") == 5  # 5 × 200 = 1000 恰好用尽剩余可结算金额
    assert results.count("exceeded") == 3
    order = order_store.get("t1", "s-c2")
    assert order["settled_cents"] == 1000
    assert order["settleable_cents"] == 0


def test_concurrent_payments_never_overshoot_settlement_amount() -> None:
    _settled_order("s-c3", amount=1000)
    settlement_store.accept("t1", "s-c3r", "s-c3", 600)

    def call() -> str:
        try:
            settlement_store.add_payment("t1", "s-c3r", 300)
        except settlement_store.PaymentExceedsUnreceived:
            return "exceeded"
        return "paid"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(8)))

    assert results.count("paid") == 2  # 2 × 300 = 600 恰好收清
    assert results.count("exceeded") == 6
    doc = settlement_store.get("t1", "s-c3r")
    assert doc["received_cents"] == 600 and doc["unreceived_cents"] == 0


def test_concurrent_reverse_with_payment_race_has_one_outcome() -> None:
    _settled_order("s-c4", amount=1000)
    settlement_store.accept("t1", "s-c4r", "s-c4", 600)

    def pay() -> str:
        try:
            settlement_store.add_payment("t1", "s-c4r", 600)
        except settlement_store.SettlementAlreadyReversed:
            return "doc_reversed"
        return "paid"

    def reverse() -> str:
        try:
            settlement_store.reverse("t1", "s-c4r")
        except settlement_store.SettlementHasPayments:
            return "has_payments"
        return "reversed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_pay = pool.submit(pay)
        f_rev = pool.submit(reverse)
        pay_outcome = f_pay.result()
        rev_outcome = f_rev.result()

    doc = settlement_store.get("t1", "s-c4r")
    order = order_store.get("t1", "s-c4")
    # 写锁串行化，结论唯一且金额自洽：
    # 1) 收款先落账：冲正被拒，结算单 accepted、占用 600
    # 2) 冲正先落账：收款被拒，结算单 reversed、占用整体释放为 0
    if rev_outcome == "has_payments":
        assert pay_outcome == "paid"
        assert doc["status"] == "accepted"
        assert doc["received_cents"] == 600
        assert order["settled_cents"] == 600
    else:
        assert rev_outcome == "reversed" and pay_outcome == "doc_reversed"
        assert doc["status"] == "reversed"
        assert doc["received_cents"] == 0
        assert order["settled_cents"] == 0


def test_state_survives_restart_and_migrate_remains_idempotent() -> None:
    _settled_order("s-p1", amount=1000)
    client.post(
        "/settlements",
        json={"settlement_id": "s-p1a", "order_id": "s-p1", "amount_cents": 300},
        headers=H,
    )
    client.post(
        "/settlements",
        json={"settlement_id": "s-p1b", "order_id": "s-p1", "amount_cents": 100},
        headers=H,
    )
    client.post("/settlements/s-p1a/payments", json={"amount_cents": 120}, headers=H)
    client.post("/settlements/s-p1b/reverse", headers=H)

    # 模拟重启：迁移重复执行不报错、不丢数据
    migrate()
    migrate()

    a = client.get("/settlements/s-p1a", headers=H).json()
    b = client.get("/settlements/s-p1b", headers=H).json()
    assert a["status"] == "accepted" and a["received_cents"] == 120
    assert b["status"] == "reversed"
    # 重启后仍可继续收款、受理新的结算单
    assert (
        client.post("/settlements/s-p1a/payments", json={"amount_cents": 180}, headers=H).status_code
        == 200
    )
    assert client.post(
        "/settlements",
        json={"settlement_id": "s-p1c", "order_id": "s-p1", "amount_cents": 700},
        headers=H,
    ).status_code == 201
    order = client.get("/orders/s-p1", headers=H).json()
    assert order["settled_cents"] == 1000
    assert order["settled_cents"] + order["settleable_cents"] == 1000
    # 无收款的新单据仍可冲正
    assert client.post("/settlements/s-p1c/reverse", headers=H).status_code == 200
    assert order_store.get("t1", "s-p1")["settled_cents"] == 300
    # 退款链路完全未受影响
    assert refund_store.get("t1", "s-p1x") is None
    assert order_store.get("t1", "s-p1")["refunded_cents"] == 0
