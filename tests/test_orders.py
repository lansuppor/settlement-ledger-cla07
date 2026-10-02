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

def _register_plan(order_id: str, items: list, tenant: str = "t1"):
    return client.post(f"/orders/{order_id}/installments", json={"installments": items}, headers={"X-Tenant": tenant})

def _plan(order_id: str, tenant: str = "t1") -> list:
    resp = client.get(f"/orders/{order_id}/installments", headers={"X-Tenant": tenant})
    assert resp.status_code == 200
    return resp.json()["installments"]

def _pay_installment(order_id: str, installment_id: str, amount: int, tenant: str = "t1"):
    return client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": amount, "installment_id": installment_id},
        headers={"X-Tenant": tenant},
    )

PLAN_ITEMS = [
    {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01T00:00:00Z"},
    {"installment_id": "i2", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"},
]

def _new_order(order_id: str, amount: int = 1000, tenant: str = "t1") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201

def test_register_plan_and_query() -> None:
    _new_order("o20")
    resp = _register_plan("o20", PLAN_ITEMS)
    assert resp.status_code == 201, resp.text
    plan = _plan("o20")
    assert [i["installment_id"] for i in plan] == ["i1", "i2"]
    assert [i["amount_cents"] for i in plan] == [300, 700]
    assert all(i["status"] == "unpaid" and i["paid_record_id"] is None for i in plan)
    assert plan[0]["due_at"] == "2026-11-01T00:00:00Z"

def test_plan_query_before_registration_is_empty_and_order_flow_unchanged() -> None:
    _new_order("o21")
    assert _plan("o21") == []
    # 未受理分期计划前，整单收款行为与现在完全一致
    _pay("o21", 400)
    order = client.get("/orders/o21", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600

def test_duplicate_plan_conflicts_and_keeps_original() -> None:
    _new_order("o22")
    assert _register_plan("o22", PLAN_ITEMS).status_code == 201
    again = _register_plan("o22", [{"installment_id": "x1", "amount_cents": 1000, "due_at": "2027-01-01T00:00:00Z"}])
    assert again.status_code == 409
    assert again.json()["detail"] == "installment plan already accepted"
    # 已受理的计划不被改动
    assert [i["installment_id"] for i in _plan("o22")] == ["i1", "i2"]

def test_plan_sum_mismatch_rejected_atomically() -> None:
    _new_order("o23")
    bad = [dict(PLAN_ITEMS[0]), {"installment_id": "i2", "amount_cents": 600, "due_at": "2026-12-01T00:00:00Z"}]
    resp = _register_plan("o23", bad)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "installment amounts do not add up to order amount"
    assert _plan("o23") == []  # 整份拒绝，不留任何一期

def test_plan_duplicate_installment_id_rejected() -> None:
    _new_order("o24")
    dup = [dict(PLAN_ITEMS[0]), {"installment_id": "i1", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"}]
    resp = _register_plan("o24", dup)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "duplicate installment id"
    assert _plan("o24") == []

def test_plan_non_positive_amount_rejected() -> None:
    _new_order("o25")
    bad = [dict(PLAN_ITEMS[0]), {"installment_id": "i2", "amount_cents": 0, "due_at": "2026-12-01T00:00:00Z"}]
    assert _register_plan("o25", bad).status_code == 422
    assert _plan("o25") == []

def test_plan_cross_tenant_register_and_query_are_not_found() -> None:
    _new_order("o26")
    assert _register_plan("o26", PLAN_ITEMS, tenant="t2").status_code == 404
    assert client.get("/orders/o26/installments", headers={"X-Tenant": "t2"}).status_code == 404
    assert _plan("o26") == []  # t1 视角未受影响

def test_installment_payment_marks_paid_and_advances_books() -> None:
    _new_order("o27")
    _register_plan("o27", PLAN_ITEMS)
    resp = _pay_installment("o27", "i1", 300)
    assert resp.status_code == 200, resp.text
    order = resp.json()
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 700
    assert order["status"] == "accepted"
    plan = {i["installment_id"]: i for i in _plan("o27")}
    assert plan["i1"]["status"] == "paid" and plan["i1"]["paid_record_id"]
    assert plan["i2"]["status"] == "unpaid"
    # 流水记录携带期次标识，因果链可按流水标识重建
    records = _payments("o27")
    assert records[-1]["installment_id"] == "i1"
    assert records[-1]["record_id"] == plan["i1"]["paid_record_id"]

def test_all_installments_paid_settles_order() -> None:
    _new_order("o28")
    _register_plan("o28", PLAN_ITEMS)
    _pay_installment("o28", "i1", 300)
    resp = _pay_installment("o28", "i2", 700)
    assert resp.status_code == 200
    order = resp.json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0
    assert order["status"] == "settled"
    assert all(i["status"] == "paid" for i in _plan("o28"))

def test_installment_payment_failure_reasons_are_distinguishable() -> None:
    _new_order("o29")
    _register_plan("o29", PLAN_ITEMS)
    # 未声明期次
    resp = client.post("/orders/o29/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 409 and resp.json()["detail"] == "installment id is required"
    # 金额与该期应收不等
    resp = _pay_installment("o29", "i1", 200)
    assert resp.status_code == 409 and resp.json()["detail"] == "payment amount does not match installment amount"
    # 期次不存在
    resp = _pay_installment("o29", "i9", 300)
    assert resp.status_code == 409 and resp.json()["detail"] == "installment not found"
    # 任一失败都不改动账面与留痕
    order = client.get("/orders/o29", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0
    assert _payments("o29") == []
    assert all(i["status"] == "unpaid" for i in _plan("o29"))

def test_paid_installment_cannot_be_paid_again() -> None:
    _new_order("o30")
    _register_plan("o30", PLAN_ITEMS)
    assert _pay_installment("o30", "i1", 300).status_code == 200
    # 期次已收讫 / 同一收款请求重放：冲突且不重复留痕
    resp = _pay_installment("o30", "i1", 300)
    assert resp.status_code == 409 and resp.json()["detail"] == "installment already paid"
    assert client.get("/orders/o30", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 300
    assert len(_payments("o30")) == 1

def test_installment_payment_without_plan_is_refused() -> None:
    _new_order("o31")
    resp = _pay_installment("o31", "i1", 300)
    assert resp.status_code == 409 and resp.json()["detail"] == "installment plan not accepted"
    assert client.get("/orders/o31", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert _payments("o31") == []

def test_reversal_of_installment_payment_returns_installment_to_unpaid() -> None:
    _new_order("o32")
    _register_plan("o32", PLAN_ITEMS)
    _pay_installment("o32", "i1", 300)
    _pay_installment("o32", "i2", 700)  # 全部收讫，订单结清
    assert client.get("/orders/o32", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    first_payment = _payments("o32")[0]["record_id"]
    resp = _reverse("o32", first_payment)
    assert resp.status_code == 201, resp.text
    order = resp.json()
    # 与该笔收款从未发生完全一致
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300
    assert order["status"] == "accepted"
    plan = {i["installment_id"]: i for i in _plan("o32")}
    assert plan["i1"]["status"] == "unpaid" and plan["i1"]["paid_record_id"] is None
    assert plan["i2"]["status"] == "paid"
    # 冲正流水仍关联原收款
    reversal = _payments("o32")[-1]
    assert reversal["record_type"] == "reversal" and reversal["related_record_id"] == first_payment
    # 期次退回未收后可再次收讫并重新结清
    assert _pay_installment("o32", "i1", 300).status_code == 200
    again = client.get("/orders/o32", headers={"X-Tenant": "t1"}).json()
    assert again["paid_cents"] == 1000 and again["status"] == "settled"

def test_installment_reversal_replay_only_effective_once() -> None:
    _new_order("o33")
    _register_plan("o33", PLAN_ITEMS)
    _pay_installment("o33", "i1", 300)
    record_id = _payments("o33")[0]["record_id"]
    assert _reverse("o33", record_id).status_code == 201
    replay = _reverse("o33", record_id)
    assert replay.status_code == 409 and replay.json()["detail"] == "payment already reversed"
    plan = {i["installment_id"]: i for i in _plan("o33")}
    assert plan["i1"]["status"] == "unpaid"
    assert client.get("/orders/o33", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert len(_payments("o33")) == 2

def test_concurrent_payment_on_same_installment_only_one_wins() -> None:
    import threading

    from app.store import orders as store
    from app.store.orders import LedgerError

    _new_order("o34")
    _register_plan("o34", PLAN_ITEMS)
    results = []

    def worker() -> None:
        try:
            store.add_payment("t1", "o34", 300, "tester", installment_id="i1")
            results.append("ok")
        except LedgerError as error:
            results.append(str(error))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("ok") == 1
    assert results.count("installment already paid") == 1
    # 不重复留痕、账面只推进一次
    assert client.get("/orders/o34", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 300
    assert len(_payments("o34")) == 1
