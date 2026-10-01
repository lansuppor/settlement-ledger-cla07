import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

def settle_order(tenant: str, order_id: str, amount: int) -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})
    client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})

def test_accept_refund_requires_settled_order() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r1-o", "amount_cents": 200, "currency": "CNY"})
    body = {"refund_id": "r1", "order_id": "r1-o", "amount_cents": 100}
    resp = client.post("/refunds", json=body, headers={"X-Tenant": "t1"})
    assert resp.status_code == 409 and resp.json()["detail"] == "order is not settled"
    assert client.get("/refunds/r1", headers={"X-Tenant": "t1"}).status_code == 404

def test_accept_and_read_refund_keeps_conservation() -> None:
    settle_order("t1", "r2-o", 500)
    body = {"refund_id": "r2", "order_id": "r2-o", "amount_cents": 200}
    resp = client.post("/refunds", json=body, headers={"X-Tenant": "t1"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "accepted"
    order = client.get("/orders/r2-o", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 500
    assert order["outstanding_cents"] == 0
    assert order["refunded_cents"] == 200
    assert order["refundable_cents"] == 300
    assert order["refunded_cents"] + order["refundable_cents"] == order["amount_cents"]
    got = client.get("/refunds/r2", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["amount_cents"] == 200

def test_refund_exceeds_refundable_is_refused() -> None:
    settle_order("t1", "r3-o", 100)
    body = {"refund_id": "r3", "order_id": "r3-o", "amount_cents": 150}
    resp = client.post("/refunds", json=body, headers={"X-Tenant": "t1"})
    assert resp.status_code == 409 and resp.json()["detail"] == "refund amount exceeds refundable balance"
    order = client.get("/orders/r3-o", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0 and order["refundable_cents"] == 100

def test_duplicate_accept_is_idempotent_conflict() -> None:
    settle_order("t1", "r4-o", 300)
    body = {"refund_id": "r4", "order_id": "r4-o", "amount_cents": 100}
    assert client.post("/refunds", json=body, headers={"X-Tenant": "t1"}).status_code == 201
    again = client.post("/refunds", json={**body, "amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert again.status_code == 409 and again.json()["detail"] == "refund already accepted"
    refund = client.get("/refunds/r4", headers={"X-Tenant": "t1"}).json()
    assert refund["amount_cents"] == 100
    order = client.get("/orders/r4-o", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 100

def test_missing_and_cross_tenant_order_look_like_not_found() -> None:
    body = {"refund_id": "r5a", "order_id": "nope", "amount_cents": 10}
    assert client.post("/refunds", json=body, headers={"X-Tenant": "t1"}).status_code == 404
    settle_order("t1", "r5-o", 100)
    body = {"refund_id": "r5b", "order_id": "r5-o", "amount_cents": 10}
    assert client.post("/refunds", json=body, headers={"X-Tenant": "t2"}).status_code == 404

def test_cross_tenant_refund_read_is_not_found() -> None:
    settle_order("t1", "r6-o", 100)
    client.post("/refunds", json={"refund_id": "r6", "order_id": "r6-o", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    assert client.get("/refunds/r6", headers={"X-Tenant": "t2"}).status_code == 404

def test_reverse_refund_returns_balance() -> None:
    settle_order("t1", "r7-o", 400)
    client.post("/refunds", json={"refund_id": "r7", "order_id": "r7-o", "amount_cents": 150}, headers={"X-Tenant": "t1"})
    resp = client.post("/refunds/r7/reversal", headers={"X-Tenant": "t1"})
    assert resp.status_code == 200 and resp.json()["status"] == "reversed"
    order = client.get("/orders/r7-o", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0 and order["refundable_cents"] == 400
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 0

def test_repeated_reversal_is_distinct_conflict() -> None:
    settle_order("t1", "r8-o", 400)
    client.post("/refunds", json={"refund_id": "r8", "order_id": "r8-o", "amount_cents": 150}, headers={"X-Tenant": "t1"})
    assert client.post("/refunds/r8/reversal", headers={"X-Tenant": "t1"}).status_code == 200
    again = client.post("/refunds/r8/reversal", headers={"X-Tenant": "t1"})
    assert again.status_code == 409 and again.json()["detail"] == "refund already reversed"

def test_reverse_unknown_is_not_found() -> None:
    assert client.post("/refunds/ghost/reversal", headers={"X-Tenant": "t1"}).status_code == 404

def test_reaccept_after_reversal_is_refused() -> None:
    settle_order("t1", "r9-o", 400)
    body = {"refund_id": "r9", "order_id": "r9-o", "amount_cents": 150}
    client.post("/refunds", json=body, headers={"X-Tenant": "t1"})
    client.post("/refunds/r9/reversal", headers={"X-Tenant": "t1"})
    again = client.post("/refunds", json=body, headers={"X-Tenant": "t1"})
    assert again.status_code == 409 and again.json()["detail"] == "refund already reversed"
    order = client.get("/orders/r9-o", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0

def test_concurrent_accept_only_one_wins() -> None:
    settle_order("t1", "r10-o", 1000)
    body = {"refund_id": "r10", "order_id": "r10-o", "amount_cents": 100}
    def post_once(_: int) -> int:
        local = TestClient(app)
        return local.post("/refunds", json=body, headers={"X-Tenant": "t1"}).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(post_once, range(8)))
    assert statuses.count(201) == 1
    assert all(s in (201, 409) for s in statuses)
    order = client.get("/orders/r10-o", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 100

def test_state_survives_restart() -> None:
    settle_order("t1", "r11-o", 300)
    client.post("/refunds", json={"refund_id": "r11a", "order_id": "r11-o", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/refunds", json={"refund_id": "r11b", "order_id": "r11-o", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    client.post("/refunds/r11b/reversal", headers={"X-Tenant": "t1"})
    migrate()  # 重启时重复执行迁移必须无副作用
    assert client.post("/refunds/r11a/reversal", headers={"X-Tenant": "t1"}).status_code == 200
    resp = client.post(
        "/refunds", json={"refund_id": "r11c", "order_id": "r11-o", "amount_cents": 300}, headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 201
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM refunds WHERE refund_id='r11a'").fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
