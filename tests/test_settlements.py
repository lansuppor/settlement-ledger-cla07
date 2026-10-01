import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_settlements.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store import settlements as settlement_store
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def _order(order_id: str, tenant: str = "t1", amount: int = 1000) -> None:
    client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    )


def _settled_order(order_id: str, tenant: str = "t1", amount: int = 1000) -> None:
    _order(order_id, tenant, amount)
    client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})


def _accept(settlement_id: str, order_id: str, amount: int, tenant: str = "t1"):
    return client.post(
        "/settlements",
        json={"settlement_id": settlement_id, "order_id": order_id, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


# ---------- 正常受理 / 读取 / 分期收款 / 冲正 ----------

def test_accept_read_settlement() -> None:
    _settled_order("s-o1", amount=500)
    resp = _accept("s-1", "s-o1", 200)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["amount_cents"] == 200
    assert body["received_cents"] == 0
    assert body["unreceived_cents"] == 200
    assert body["reversed_at"] is None

    got = client.get("/settlements/s-1", headers=H)
    assert got.status_code == 200 and got.json()["settlement_id"] == "s-1"


def test_order_exposes_settled_and_settleable_and_conserves() -> None:
    _settled_order("s-o2", amount=500)
    assert _accept("s-2", "s-o2", 200).status_code == 201
    order = client.get("/orders/s-o2", headers=H).json()
    # 收款/退款链路不被改写
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0
    assert order["refunded_cents"] == 0 and order["refundable_cents"] == 500
    assert order["settled_cents"] == 200
    assert order["settleable_cents"] == 300
    # 累计已结算 + 剩余可结算 与订单金额守恒
    assert order["settled_cents"] + order["settleable_cents"] == order["amount_cents"]


def test_installment_receipts_accumulate_and_expose_totals() -> None:
    _settled_order("s-o3", amount=500)
    assert _accept("s-3", "s-o3", 300).status_code == 201

    r1 = client.post("/settlements/s-3/payments", json={"amount_cents": 100}, headers=H)
    assert r1.status_code == 200, r1.text
    assert r1.json()["received_cents"] == 100
    assert r1.json()["unreceived_cents"] == 200

    r2 = client.post("/settlements/s-3/payments", json={"amount_cents": 200}, headers=H)
    assert r2.status_code == 200
    assert r2.json()["received_cents"] == 300
    assert r2.json()["unreceived_cents"] == 0

    # 收款不影响订单的已收/已退/已结算金额
    order = client.get("/orders/s-o3", headers=H).json()
    assert order["paid_cents"] == 500 and order["settled_cents"] == 300
    assert order["refunded_cents"] == 0


def test_receipt_cannot_exceed_settlement_amount() -> None:
    _settled_order("s-o4", amount=500)
    _accept("s-4", "s-o4", 200)
    assert client.post("/settlements/s-4/payments", json={"amount_cents": 150}, headers=H).status_code == 200
    over = client.post("/settlements/s-4/payments", json={"amount_cents": 100}, headers=H)
    assert over.status_code == 409
    assert over.json()["detail"] == "receipt exceeds unreceived amount"
    # 结算单不改动
    doc = client.get("/settlements/s-4", headers=H).json()
    assert doc["received_cents"] == 150 and doc["unreceived_cents"] == 50


def test_reverse_settlement_releases_order_balance() -> None:
    _settled_order("s-o5", amount=500)
    _accept("s-5", "s-o5", 300)
    resp = client.post("/settlements/s-5/reverse", headers=H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reversed"
    assert resp.json()["reversed_at"] is not None

    order = client.get("/orders/s-o5", headers=H).json()
    assert order["settled_cents"] == 0
    assert order["settleable_cents"] == 500
    # 收款与退款链路均不变
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0
    assert order["refunded_cents"] == 0

    # 释放后可受理新的结算单用满全额
    assert _accept("s-5b", "s-o5", 500).status_code == 201


# ---------- 受理拒绝原因可区分，且不留部分数据 ----------

def test_settlement_requires_settled_order() -> None:
    _order("s-o6", amount=500)
    client.post("/orders/s-o6/payments", json={"amount_cents": 100}, headers=H)
    resp = _accept("s-6", "s-o6", 100)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "order is not settled"
    assert client.get("/settlements/s-6", headers=H).status_code == 404
    order = client.get("/orders/s-o6", headers=H).json()
    assert order["settled_cents"] == 0 and order["paid_cents"] == 100


def test_settlement_rejected_when_order_has_refunds() -> None:
    _settled_order("s-o7", amount=500)
    client.post(
        "/refunds",
        json={"refund_id": "s-rf7", "order_id": "s-o7", "amount_cents": 100},
        headers=H,
    )
    # 冲正退款使订单重新“无退款占用”之外的场景：先验证有退款时拒绝
    resp = _accept("s-7", "s-o7", 100)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "order has refunds"
    assert client.get("/settlements/s-7", headers=H).status_code == 404
    order = client.get("/orders/s-o7", headers=H).json()
    assert order["settled_cents"] == 0 and order["refunded_cents"] == 100


def test_settlement_cannot_exceed_settleable_balance() -> None:
    _settled_order("s-o8", amount=500)
    assert _accept("s-8a", "s-o8", 300).status_code == 201
    resp = _accept("s-8b", "s-o8", 300)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settlement exceeds settleable amount"
    assert client.get("/settlements/s-8b", headers=H).status_code == 404
    order = client.get("/orders/s-o8", headers=H).json()
    assert order["settled_cents"] == 300  # 仍只有首笔
    assert order["settleable_cents"] == 200


def test_order_not_found_and_cross_tenant_are_indistinguishable() -> None:
    r1 = _accept("s-9a", "missing", 100)
    assert r1.status_code == 404 and r1.json()["detail"] == "order not found"

    _settled_order("s-o9", tenant="t1")
    r2 = _accept("s-9b", "s-o9", 100, tenant="t2")
    assert r2.status_code == 404 and r2.json()["detail"] == "order not found"


# ---------- 幂等、重复与冲突 ----------

def test_duplicate_accept_is_idempotent_and_keeps_first_doc() -> None:
    _settled_order("s-o10", amount=500)
    assert _accept("s-10", "s-o10", 100).status_code == 201
    # 用不同金额重复受理：结论确定，不改变首次单据、不重复占用
    second = _accept("s-10", "s-o10", 400)
    assert second.status_code == 409
    assert second.json()["detail"] == "settlement already accepted"

    doc = client.get("/settlements/s-10", headers=H).json()
    assert doc["amount_cents"] == 100 and doc["status"] == "accepted"
    order = client.get("/orders/s-o10", headers=H).json()
    assert order["settled_cents"] == 100  # 金额只占用一次


def test_reverse_conflicts_are_distinguishable() -> None:
    _settled_order("s-o11", amount=500)
    _accept("s-11", "s-o11", 100)
    # 对未受理的标识冲正
    assert client.post("/settlements/nope/reverse", headers=H).status_code == 404
    # 正常冲正
    assert client.post("/settlements/s-11/reverse", headers=H).status_code == 200
    # 重复冲正
    again = client.post("/settlements/s-11/reverse", headers=H)
    assert again.status_code == 409
    assert again.json()["detail"] == "settlement already reversed"
    # 冲正后同一标识不得再次受理
    reaccept = _accept("s-11", "s-o11", 100)
    assert reaccept.status_code == 409
    assert reaccept.json()["detail"] == "settlement already reversed"
    order = client.get("/orders/s-o11", headers=H).json()
    assert order["settled_cents"] == 0


def test_reverse_rejected_when_any_receipt_exists() -> None:
    _settled_order("s-o12", amount=500)
    _accept("s-12", "s-o12", 200)
    client.post("/settlements/s-12/payments", json={"amount_cents": 1}, headers=H)
    resp = client.post("/settlements/s-12/reverse", headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settlement has receipts"
    # 订单与结算单数据保持不变
    doc = client.get("/settlements/s-12", headers=H).json()
    assert doc["status"] == "accepted" and doc["received_cents"] == 1
    order = client.get("/orders/s-o12", headers=H).json()
    assert order["settled_cents"] == 200


def test_payments_on_unknown_or_reversed_settlement() -> None:
    assert client.post("/settlements/nope/payments", json={"amount_cents": 1}, headers=H).status_code == 404
    _settled_order("s-o13", amount=500)
    _accept("s-13", "s-o13", 100)
    assert client.post("/settlements/s-13/reverse", headers=H).status_code == 200
    rev = client.post("/settlements/s-13/payments", json={"amount_cents": 1}, headers=H)
    assert rev.status_code == 409
    assert rev.json()["detail"] == "settlement already reversed"


def test_cross_tenant_read_and_reverse_are_not_found() -> None:
    _settled_order("s-o14", tenant="t1")
    _accept("s-14", "s-o14", 100, tenant="t1")
    assert client.get("/settlements/s-14", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/settlements/s-14/reverse", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/settlements/s-14/payments", json={"amount_cents": 1},
                       headers={"X-Tenant": "t2"}).status_code == 404
    # 其他租户的操作不改变真实单据与订单金额
    assert client.get("/settlements/s-14", headers=H).json()["status"] == "accepted"
    assert order_store.get("t1", "s-o14")["settled_cents"] == 100


def test_invalid_amount_rejected() -> None:
    _settled_order("s-o15", amount=500)
    resp = _accept("s-15", "s-o15", 0)
    assert resp.status_code == 422
    # 请求体校验先于存储层：即使标识不存在，金额非法同样 422
    assert client.post("/settlements/s-15/payments", json={"amount_cents": 0}, headers=H).status_code == 422


def test_missing_tenant_header_rejected() -> None:
    resp = client.post(
        "/settlements",
        json={"settlement_id": "s-16", "order_id": "s-o16", "amount_cents": 100},
    )
    assert resp.status_code == 400


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
    assert order_store.get("t1", "s-c1")["settled_cents"] == 200  # 金额不重复占用


def test_concurrent_accepts_never_overshoot_settleable() -> None:
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


def test_concurrent_receipts_never_overshoot_settlement() -> None:
    _settled_order("s-c3", amount=1000)
    settlement_store.accept("t1", "s-c3r", "s-c3", 600)

    def call() -> str:
        try:
            settlement_store.receive("t1", "s-c3r", 200)
        except settlement_store.ReceiptExceedsUnreceived:
            return "exceeded"
        return "received"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(8)))

    assert results.count("received") == 3
    assert results.count("exceeded") == 5
    doc = settlement_store.get("t1", "s-c3r")
    assert doc["received_cents"] == 600 and doc["unreceived_cents"] == 0


def test_state_survives_restart_and_migrate_remains_idempotent() -> None:
    _settled_order("s-p1", amount=800)
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
    # 重启后仍可继续收款、受理新结算单；已冲正标识不可再受理
    assert client.post("/settlements/s-p1a/payments", json={"amount_cents": 180},
                       headers=H).status_code == 200
    assert _accept("s-p1c", "s-p1", 200).status_code == 201
    assert _accept("s-p1b", "s-p1", 100).status_code == 409
    order = client.get("/orders/s-p1", headers=H).json()
    # 占用 = s-p1a 300 + s-p1c 200（s-p1b 已冲正释放）
    assert order["settled_cents"] == 500
    assert order["settled_cents"] + order["settleable_cents"] == 800
