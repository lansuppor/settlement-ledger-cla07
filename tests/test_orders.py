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


# ---------- 分期收款 ----------

def _new_order(order_id: str, amount: int, tenant: str = "t1") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201, resp.text

def _register_plan(order_id: str, installments: list, tenant: str = "t1"):
    return client.post(f"/orders/{order_id}/installments", json={"installments": installments}, headers={"X-Tenant": tenant})

def _plan(order_id: str, tenant: str = "t1"):
    return client.get(f"/orders/{order_id}/installments", headers={"X-Tenant": tenant})

def _pay_installment(order_id: str, installment_id: str, amount: int, tenant: str = "t1"):
    return client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": amount, "installment_id": installment_id},
        headers={"X-Tenant": tenant},
    )

def test_register_plan_and_query() -> None:
    _new_order("o20", 1000)
    resp = _register_plan("o20", [
        {"installment_id": "i1", "amount_cents": 400, "due_at": "2026-11-01"},
        {"installment_id": "i2", "amount_cents": 600, "due_at": "2026-12-01"},
    ])
    assert resp.status_code == 201, resp.text
    plan = _plan("o20")
    assert plan.status_code == 200
    items = plan.json()["installments"]
    assert [i["installment_id"] for i in items] == ["i1", "i2"]
    assert [i["amount_cents"] for i in items] == [400, 600]
    assert [i["due_at"] for i in items] == ["2026-11-01", "2026-12-01"]
    assert all(i["paid"] is False for i in items)

def test_duplicate_plan_conflicts_and_keeps_original() -> None:
    _new_order("o21", 500)
    assert _register_plan("o21", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}]).status_code == 201
    replay = _register_plan("o21", [{"installment_id": "j1", "amount_cents": 500, "due_at": "2026-11-01"}])
    assert replay.status_code == 409
    assert replay.json()["detail"] == "installment plan already accepted"
    items = _plan("o21").json()["installments"]
    assert [i["installment_id"] for i in items] == ["i1"]  # 已受理的计划不被改动

def test_plan_sum_must_equal_order_amount() -> None:
    _new_order("o22", 500)
    resp = _register_plan("o22", [
        {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01"},
        {"installment_id": "i2", "amount_cents": 100, "due_at": "2026-12-01"},
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"] == "installment amounts do not add up to order amount"
    # 整份计划被拒绝，不留任何一期；订单仍按整单收款
    assert _plan("o22").status_code == 404
    assert client.post("/orders/o22/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 200

def test_plan_rejects_duplicate_installment_ids() -> None:
    _new_order("o23", 500)
    resp = _register_plan("o23", [
        {"installment_id": "i1", "amount_cents": 200, "due_at": "2026-11-01"},
        {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-12-01"},
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"] == "duplicate installment_id"
    assert _plan("o23").status_code == 404

def test_plan_rejects_non_positive_amount() -> None:
    _new_order("o24", 500)
    resp = _register_plan("o24", [
        {"installment_id": "i1", "amount_cents": 0, "due_at": "2026-11-01"},
        {"installment_id": "i2", "amount_cents": 500, "due_at": "2026-12-01"},
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"] == "installment amount must be positive"
    assert _plan("o24").status_code == 404

def test_plan_missing_or_cross_tenant_order_is_not_found() -> None:
    _new_order("o25", 500)
    assert _register_plan("missing-order", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}]).status_code == 404
    # 跨租户受理与查询一律按不存在处理
    assert _register_plan("o25", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}], tenant="t2").status_code == 404
    assert _register_plan("o25", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}]).status_code == 201
    assert _plan("o25", tenant="t2").status_code == 404

def test_installment_payment_flow_to_settlement() -> None:
    _new_order("o26", 1000)
    _register_plan("o26", [
        {"installment_id": "i1", "amount_cents": 400, "due_at": "2026-11-01"},
        {"installment_id": "i2", "amount_cents": 600, "due_at": "2026-12-01"},
    ])
    resp = _pay_installment("o26", "i1", 400)
    assert resp.status_code == 200, resp.text
    assert resp.json()["paid_cents"] == 400 and resp.json()["outstanding_cents"] == 600
    items = _plan("o26").json()["installments"]
    assert [i["paid"] for i in items] == [True, False]
    resp = _pay_installment("o26", "i2", 600)
    assert resp.status_code == 200
    assert resp.json()["status"] == "settled" and resp.json()["paid_cents"] == 1000
    # 流水中可看到每笔收款对应的期次
    records = _payments("o26")
    assert [r["installment_id"] for r in records] == ["i1", "i2"]

def test_installment_order_requires_installment_id() -> None:
    _new_order("o27", 500)
    _register_plan("o27", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}])
    resp = client.post("/orders/o27/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "installment_id is required for installment order"
    assert client.get("/orders/o27", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert _payments("o27") == []

def test_installment_payment_amount_must_match() -> None:
    _new_order("o28", 500)
    _register_plan("o28", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}])
    resp = _pay_installment("o28", "i1", 400)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "payment amount does not match installment amount"
    assert _plan("o28").json()["installments"][0]["paid"] is False
    assert _payments("o28") == []

def test_installment_unknown_is_not_found() -> None:
    _new_order("o29", 500)
    _register_plan("o29", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}])
    resp = _pay_installment("o29", "nope", 500)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "installment not found"
    assert _payments("o29") == []

def test_installment_replay_conflicts_without_double_recording() -> None:
    _new_order("o30", 500)
    _register_plan("o30", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}])
    assert _pay_installment("o30", "i1", 500).status_code == 200
    replay = _pay_installment("o30", "i1", 500)
    assert replay.status_code == 409
    assert replay.json()["detail"] == "installment already paid"
    assert len(_payments("o30")) == 1  # 不重复留痕

def test_installment_payment_without_plan_refused() -> None:
    _new_order("o31", 500)
    resp = _pay_installment("o31", "i1", 500)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "installment plan not accepted"
    assert client.get("/orders/o31", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0

def test_reversal_returns_installment_to_unpaid() -> None:
    _new_order("o32", 1000)
    _register_plan("o32", [
        {"installment_id": "i1", "amount_cents": 400, "due_at": "2026-11-01"},
        {"installment_id": "i2", "amount_cents": 600, "due_at": "2026-12-01"},
    ])
    _pay_installment("o32", "i1", 400)
    _pay_installment("o32", "i2", 600)
    assert client.get("/orders/o32", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    first = _payments("o32")[0]
    assert first["installment_id"] == "i1"
    resp = _reverse("o32", first["record_id"])
    assert resp.status_code == 201, resp.text
    order = resp.json()
    # 账面与该笔收款从未发生完全一致
    assert order["paid_cents"] == 600 and order["outstanding_cents"] == 400 and order["status"] == "accepted"
    # 对应期次回到未收
    items = _plan("o32").json()["installments"]
    assert [i["paid"] for i in items] == [False, True]
    # 因果链完整：冲正记录关联原收款，原收款仍标记其期次
    records = _payments("o32")
    assert records[-1]["record_type"] == "reversal" and records[-1]["related_record_id"] == first["record_id"]
    # 期次退回未收后可再次收讫并重新结清
    assert _pay_installment("o32", "i1", 400).status_code == 200
    again = client.get("/orders/o32", headers={"X-Tenant": "t1"}).json()
    assert again["status"] == "settled" and again["paid_cents"] == 1000

def test_concurrent_installment_payment_allows_only_one() -> None:
    import threading

    from app.store import orders as store

    _new_order("o33", 500)
    _register_plan("o33", [{"installment_id": "i1", "amount_cents": 500, "due_at": "2026-11-01"}])
    results = []

    def worker() -> None:
        try:
            store.add_payment("t1", "o33", 500, "tester", "i1")
            results.append("ok")
        except store.LedgerError:
            results.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict", "ok"]  # 同一期次并发收款至多一笔成功
    assert len(_payments("o33")) == 1
