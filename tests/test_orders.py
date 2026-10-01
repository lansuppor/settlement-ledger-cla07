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

def _new_order(order_id: str, amount: int = 500, tenant: str = "t1") -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})

def test_reversal_reduces_paid_and_reopens_order() -> None:
    _new_order("r1", 500)
    client.post("/orders/r1/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    settled = client.get("/orders/r1", headers={"X-Tenant": "t1"}).json()
    assert settled["paid_cents"] == 500 and settled["status"] == "settled"
    resp = client.post("/orders/r1/reversals", json={"reversal_id": "rv1", "amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["paid_cents"] == 300
    assert body["outstanding_cents"] == 200
    assert body["status"] == "accepted"

def test_full_reversal_can_pay_again_and_settle() -> None:
    _new_order("r2", 300)
    client.post("/orders/r2/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r2/reversals", json={"reversal_id": "rv2", "amount_cents": 300}, headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    repaid = client.post("/orders/r2/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    assert repaid.status_code == 200 and repaid.json()["status"] == "settled"

def test_reversal_cannot_exceed_paid() -> None:
    _new_order("r3", 500)
    client.post("/orders/r3/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r3/reversals", json={"reversal_id": "rv3", "amount_cents": 101}, headers={"X-Tenant": "t1"}).status_code == 409
    # 账务不变，且失败的冲正不占用冲正标识
    order = client.get("/orders/r3", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 400
    assert client.post("/orders/r3/reversals", json={"reversal_id": "rv3", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200

def test_reversal_id_is_idempotent() -> None:
    _new_order("r4", 500)
    client.post("/orders/r4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    payload = {"reversal_id": "rv4", "amount_cents": 200}
    first = client.post("/orders/r4/reversals", json=payload, headers={"X-Tenant": "t1"})
    second = client.post("/orders/r4/reversals", json=payload, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    order = client.get("/orders/r4", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 200

def test_same_reversal_id_with_different_amount_is_conflict() -> None:
    _new_order("r5", 500)
    client.post("/orders/r5/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r5/reversals", json={"reversal_id": "rv5", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/r5/reversals", json={"reversal_id": "rv5", "amount_cents": 200}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.get("/orders/r5", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400

def test_same_reversal_id_against_different_order_is_conflict() -> None:
    _new_order("r6a", 500)
    _new_order("r6b", 500)
    client.post("/orders/r6a/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    client.post("/orders/r6b/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r6a/reversals", json={"reversal_id": "rv6", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/r6b/reversals", json={"reversal_id": "rv6", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.get("/orders/r6b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 500

def test_reversal_on_missing_or_foreign_order_is_not_found() -> None:
    assert client.post("/orders/nope/reversals", json={"reversal_id": "rvx", "amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 404
    _new_order("r7", 500)
    assert client.post("/orders/r7/reversals", json={"reversal_id": "rv7", "amount_cents": 1}, headers={"X-Tenant": "t2"}).status_code == 404
    # 跨租户失败后，本租户仍可用同一冲正标识成功冲正（标识空间按租户隔离）
    client.post("/orders/r7/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r7/reversals", json={"reversal_id": "rv7", "amount_cents": 50}, headers={"X-Tenant": "t1"}).status_code == 200

def test_reversal_requires_tenant_header_and_positive_amount() -> None:
    _new_order("r8", 500)
    assert client.post("/orders/r8/reversals", json={"reversal_id": "rv8", "amount_cents": 1}).status_code == 400
    assert client.post("/orders/r8/reversals", json={"reversal_id": "rv8", "amount_cents": 0}, headers={"X-Tenant": "t1"}).status_code == 422

def test_reversal_survives_restart() -> None:
    from app.store.db import connect
    _new_order("r9", 500)
    client.post("/orders/r9/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    client.post("/orders/r9/reversals", json={"reversal_id": "rv9", "amount_cents": 250}, headers={"X-Tenant": "t1"})
    # 以全新连接模拟服务重启：冲正记录仍在，重发不会二次扣减
    conn = connect()
    try:
        stored = conn.execute("SELECT amount_cents FROM payment_reversals WHERE tenant='t1' AND reversal_id='rv9'").fetchone()
        assert stored is not None and stored["amount_cents"] == 250
    finally:
        conn.close()
    assert client.post("/orders/r9/reversals", json={"reversal_id": "rv9", "amount_cents": 250}, headers={"X-Tenant": "t1"}).json()["paid_cents"] == 250

def test_ledger_records_payments_and_reversals_in_order() -> None:
    _new_order("L1", 500)
    client.post("/orders/L1/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    client.post("/orders/L1/reversals", json={"reversal_id": "rL1", "amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/L1/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    resp = client.get("/orders/L1/ledger", headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert [e["kind"] for e in entries] == ["payment", "reversal", "payment"]
    assert [e["amount_cents"] for e in entries] == [500, 200, 100]
    assert [e["paid_cents"] for e in entries] == [500, 300, 400]
    assert [e["outstanding_cents"] for e in entries] == [0, 200, 100]
    assert [e["status"] for e in entries] == ["settled", "accepted", "accepted"]
    assert entries[1]["reversal_id"] == "rL1"
    assert entries[0]["reversal_id"] is None and entries[2]["reversal_id"] is None
    # 流水标识互不相同且按生效先后递增
    ids = [e["entry_id"] for e in entries]
    assert len(set(ids)) == 3 and ids == sorted(ids)

def test_ledger_skips_failed_and_duplicate_operations() -> None:
    _new_order("L2", 500)
    client.post("/orders/L2/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    # 超额收款、超额冲正、同标识不同金额的冲正都被拒绝，不产生流水
    assert client.post("/orders/L2/payments", json={"amount_cents": 1}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/L2/reversals", json={"reversal_id": "rL2x", "amount_cents": 600}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/L2/reversals", json={"reversal_id": "rL2", "amount_cents": 200}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/L2/reversals", json={"reversal_id": "rL2", "amount_cents": 300}, headers={"X-Tenant": "t1"}).status_code == 409
    # 重复提交同一冲正标识：幂等返回成功，但不新增流水
    assert client.post("/orders/L2/reversals", json={"reversal_id": "rL2", "amount_cents": 200}, headers={"X-Tenant": "t1"}).status_code == 200
    entries = client.get("/orders/L2/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert [e["kind"] for e in entries] == ["payment", "reversal"]
    assert entries[1]["paid_cents"] == 300

def test_ledger_requires_tenant_and_hides_foreign_orders() -> None:
    _new_order("L3", 500)
    client.post("/orders/L3/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.get("/orders/L3/ledger").status_code == 400
    assert client.get("/orders/nope/ledger", headers={"X-Tenant": "t1"}).status_code == 404
    # 跨租户读取按不存在处理，不泄漏流水
    assert client.get("/orders/L3/ledger", headers={"X-Tenant": "t2"}).status_code == 404

def test_ledger_survives_restart() -> None:
    _new_order("L4", 500)
    client.post("/orders/L4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    client.post("/orders/L4/reversals", json={"reversal_id": "rL4", "amount_cents": 250}, headers={"X-Tenant": "t1"})
    before = client.get("/orders/L4/ledger", headers={"X-Tenant": "t1"}).json()
    # 以全新连接模拟服务重启：流水内容、顺序与账务快照保持不变
    from app.store.db import connect
    conn = connect()
    conn.close()
    after = client.get("/orders/L4/ledger", headers={"X-Tenant": "t1"}).json()
    assert after == before
    # 重启后新的操作继续在末尾追加，不改写已有流水
    client.post("/orders/L4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    entries = client.get("/orders/L4/ledger", headers={"X-Tenant": "t1"}).json()["entries"]
    assert entries[:2] == before["entries"]
    assert len(entries) == 3 and entries[2]["kind"] == "payment" and entries[2]["paid_cents"] == 350

