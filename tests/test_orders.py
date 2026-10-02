import os
import tempfile

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

def _payments(order_id: str, tenant: str = "t1") -> list:
    resp = client.get(f"/orders/{order_id}/payments", headers={"X-Tenant": tenant})
    assert resp.status_code == 200
    return resp.json()["records"]

def _pay(order_id: str, amount: int, tenant: str = "t1", originator: str | None = None) -> None:
    headers = {"X-Tenant": tenant}
    if originator:
        headers["X-Originator"] = originator
    assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers=headers).status_code == 200

def _reverse(order_id: str, record_id: str, tenant: str = "t1"):
    return client.post(f"/orders/{order_id}/payments/{record_id}/reversal", headers={"X-Tenant": tenant})

def test_ledger_lists_records_in_order_with_originator() -> None:
    body = {"tenant": "t1", "order_id": "o10", "amount_cents": 1000, "currency": "CNY"}
    client.post("/orders", json=body)
    _pay("o10", 300, originator="alice")
    _pay("o10", 200, originator="bob")
    records = _payments("o10")
    assert [r["record_type"] for r in records] == ["payment", "payment"]
    assert [r["amount_cents"] for r in records] == [300, 200]
    assert records[0]["originator"] == "alice"
    assert records[1]["originator"] == "bob"
    for r in records:
        assert r["record_id"] and r["created_at"]
        assert r["related_record_id"] is None

def test_ledger_cross_tenant_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o11", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.get("/orders/o11/payments", headers={"X-Tenant": "t2"}).status_code == 404

def test_reversal_restores_books_as_if_payment_never_happened() -> None:
    body = {"tenant": "t1", "order_id": "o12", "amount_cents": 1000, "currency": "CNY"}
    client.post("/orders", json=body)
    _pay("o12", 400)
    _pay("o12", 600)  # 结清
    settled = client.get("/orders/o12", headers={"X-Tenant": "t1"}).json()
    assert settled["status"] == "settled" and settled["paid_cents"] == 1000

    first_payment = _payments("o12")[0]["record_id"]
    resp = _reverse("o12", first_payment)
    assert resp.status_code == 201, resp.text
    order = resp.json()
    # 冲正后与从未发生该笔收款完全一致：回到未结清，金额正确
    assert order["paid_cents"] == 600
    assert order["outstanding_cents"] == 400
    assert order["status"] == "accepted"

    records = _payments("o12")
    assert [r["record_type"] for r in records] == ["payment", "payment", "reversal"]
    reversal = records[-1]
    assert reversal["amount_cents"] == 400
    assert reversal["related_record_id"] == first_payment  # 因果链可重建
    pay_indices = {r["record_id"]: i for i, r in enumerate(records)}
    assert pay_indices[first_payment] < len(records) - 1

    # 冲正释放额度后可再次收款并重新结清
    assert client.post("/orders/o12/payments", json={"amount_cents": 400}, headers={"X-Tenant": "t1"}).status_code == 200
    again = client.get("/orders/o12", headers={"X-Tenant": "t1"}).json()
    assert again["paid_cents"] == 1000 and again["status"] == "settled"

def test_duplicate_reversal_conflicts_and_keeps_books() -> None:
    body = {"tenant": "t1", "order_id": "o13", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body)
    _pay("o13", 500)
    record_id = _payments("o13")[0]["record_id"]

    assert _reverse("o13", record_id).status_code == 201
    # 重复冲正 / 请求重放：冲突且账面、流水不变
    replay = _reverse("o13", record_id)
    assert replay.status_code == 409
    assert replay.json()["detail"] == "payment already reversed"
    order = client.get("/orders/o13", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["status"] == "accepted"
    records = _payments("o13")
    assert len(records) == 2 and records[-1]["record_type"] == "reversal"

def test_reverse_unknown_record_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o14", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body)
    _pay("o14", 100)
    resp = _reverse("o14", "pay_does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "payment record not found"
    # 失败不留痕、不动账
    order = client.get("/orders/o14", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100
    assert len(_payments("o14")) == 1

def test_reverse_unknown_order_is_not_found() -> None:
    resp = _reverse("missing-order", "pay_whatever")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "order not found"

def test_reversal_cross_tenant_is_not_found_and_leaks_nothing() -> None:
    body = {"tenant": "t1", "order_id": "o15", "amount_cents": 500, "currency": "CNY"}
    client.post("/orders", json=body)
    _pay("o15", 500)
    record_id = _payments("o15")[0]["record_id"]
    # 跨租户冲正：按订单不存在处理，且不改动 t1 的账面与流水
    assert _reverse("o15", record_id, tenant="t2").status_code == 404
    order = client.get("/orders/o15", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 500 and order["status"] == "settled"
    assert all(r["record_type"] == "payment" for r in _payments("o15"))

def test_record_belongs_to_other_order_is_not_found() -> None:
    for oid in ("o16a", "o16b"):
        client.post("/orders", json={"tenant": "t1", "order_id": oid, "amount_cents": 500, "currency": "CNY"})
    _pay("o16a", 100)
    record_id = _payments("o16a")[0]["record_id"]
    # 用别的订单路径冲正该流水：流水不属于该订单，按不存在处理
    resp = _reverse("o16b", record_id)
    assert resp.status_code == 404
    assert client.get("/orders/o16a", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 100
