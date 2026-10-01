import os, tempfile
os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

def test_accept_and_read_order() -> None:
    body = {"tenant": "t1", "order_id": "o1", "amount_cents": 500, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    got = client.get("/orders/o1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500

def test_duplicate_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "o2", "amount_cents": 100, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    assert client.post("/orders", json=body).status_code == 409

def test_cross_tenant_read_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o3", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.get("/orders/o3", headers={"X-Tenant": "t2"}).status_code == 404

def test_payment_cannot_exceed_outstanding() -> None:
    body = {"tenant": "t1", "order_id": "o4", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409

def test_reversal_reduces_paid_and_reopens_order() -> None:
    body = {"tenant": "t1", "order_id": "o5", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    paid = client.post("/orders/o5/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    assert paid.status_code == 200 and paid.json()["status"] == "settled"
    rev = client.post(
        "/orders/o5/reversals",
        json={"reversal_id": "r1", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert rev.status_code == 200
    data = rev.json()
    assert data["paid_cents"] == 200
    assert data["outstanding_cents"] == 100
    assert data["status"] == "accepted"

def test_reversal_cannot_exceed_paid() -> None:
    body = {"tenant": "t1", "order_id": "o6", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o6/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    rev = client.post(
        "/orders/o6/reversals",
        json={"reversal_id": "r2", "amount_cents": 200},
        headers={"X-Tenant": "t1"},
    )
    assert rev.status_code == 409
    got = client.get("/orders/o6", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 100 and got["outstanding_cents"] == 200

def test_reversal_duplicate_id_is_idempotent() -> None:
    body = {"tenant": "t1", "order_id": "o7", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o7/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    payload = {"reversal_id": "r3", "amount_cents": 100}
    first = client.post("/orders/o7/reversals", json=payload, headers={"X-Tenant": "t1"})
    second = client.post("/orders/o7/reversals", json=payload, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    got = client.get("/orders/o7", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 200
    client.post("/orders/o7/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    replayed = client.post("/orders/o7/reversals", json=payload, headers={"X-Tenant": "t1"})
    assert replayed.status_code == 200
    assert replayed.json()["paid_cents"] == 200
    assert client.get("/orders/o7", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 300

def test_reversal_same_id_different_content_rejected() -> None:
    body = {"tenant": "t1", "order_id": "o8", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o8/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    first = client.post(
        "/orders/o8/reversals",
        json={"reversal_id": "r4", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert first.status_code == 200
    other_amount = client.post(
        "/orders/o8/reversals",
        json={"reversal_id": "r4", "amount_cents": 50},
        headers={"X-Tenant": "t1"},
    )
    assert other_amount.status_code == 409
    other_order = client.post(
        "/orders/o9/reversals",
        json={"reversal_id": "r4", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert other_order.status_code == 409
    got = client.get("/orders/o8", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 200

def test_reversal_unknown_order_is_not_found_across_tenants() -> None:
    assert client.post(
        "/orders/missing/reversals",
        json={"reversal_id": "r5", "amount_cents": 1},
        headers={"X-Tenant": "t1"},
    ).status_code == 404
    body = {"tenant": "t1", "order_id": "o10", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.post(
        "/orders/o10/reversals",
        json={"reversal_id": "r6", "amount_cents": 1},
        headers={"X-Tenant": "t2"},
    ).status_code == 404

def test_reversal_then_payment_recloses_ledger() -> None:
    body = {"tenant": "t1", "order_id": "o11", "amount_cents": 200, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o11/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post(
        "/orders/o11/reversals",
        json={"reversal_id": "r7", "amount_cents": 200},
        headers={"X-Tenant": "t1"},
    )
    got = client.get("/orders/o11", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 0 and got["outstanding_cents"] == 200 and got["status"] == "accepted"
    repaid = client.post("/orders/o11/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert repaid.json()["status"] == "settled" and repaid.json()["outstanding_cents"] == 0

def test_reversal_requires_tenant_header_and_positive_amount() -> None:
    body = {"tenant": "t1", "order_id": "o12", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.post(
        "/orders/o12/reversals", json={"reversal_id": "r8", "amount_cents": 10}
    ).status_code == 400
    assert client.post(
        "/orders/o12/reversals",
        json={"reversal_id": "r9", "amount_cents": 0},
        headers={"X-Tenant": "t1"},
    ).status_code == 422
