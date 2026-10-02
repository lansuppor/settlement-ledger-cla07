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


# ---------- 退款 ----------

def _refund(order_id: str, record_id: str, amount: int, tenant: str = "t1", installment_id: str | None = None,
            originator: str | None = None):
    headers = {"X-Tenant": tenant}
    if originator:
        headers["X-Originator"] = originator
    body = {"amount_cents": amount}
    if installment_id is not None:
        body["installment_id"] = installment_id
    return client.post(f"/orders/{order_id}/payments/{record_id}/refund", json=body, headers=headers)


def test_partial_refund_advances_books_and_writes_trace() -> None:
    _new_order("o40")
    _pay("o40", 500, originator="alice")
    payment_id = _payments("o40")[0]["record_id"]

    resp = _refund("o40", payment_id, 200)
    assert resp.status_code == 201, resp.text
    order = resp.json()
    # 已收按净额减少，未收按“订单金额−净收款”重算
    assert order["paid_cents"] == 300
    assert order["outstanding_cents"] == 700
    assert order["status"] == "accepted"

    records = _payments("o40")
    assert [r["record_type"] for r in records] == ["payment", "refund"]
    refund = records[-1]
    assert refund["amount_cents"] == 200
    assert refund["related_record_id"] == payment_id  # 因果链：退款关联原收款
    assert refund["installment_id"] is None
    assert refund["originator"] == "t1" and refund["record_id"] and refund["created_at"]


def test_refund_releases_settled_order_and_full_refund_returns_to_unpaid() -> None:
    _new_order("o41")
    _pay("o41", 400)
    _pay("o41", 600)  # 结清
    assert client.get("/orders/o41", headers={"X-Tenant": "t1"}).json()["status"] == "settled"
    first_payment = _payments("o41")[0]["record_id"]

    # 未收变为正数后不再视为结清
    resp = _refund("o41", first_payment, 400)
    order = resp.json()
    assert order["paid_cents"] == 600 and order["outstanding_cents"] == 400
    assert order["status"] == "accepted"

    # 净收款全部退完：回到未收款状态，之后可再次收款并重新结清
    second_payment = _payments("o41")[1]["record_id"]
    fully = _refund("o41", second_payment, 600)
    assert fully.json()["paid_cents"] == 0
    assert fully.json()["outstanding_cents"] == 1000
    assert fully.json()["status"] == "accepted"
    _pay("o41", 1000)  # 内部断言 200
    assert client.get("/orders/o41", headers={"X-Tenant": "t1"}).json()["status"] == "settled"


def test_multiple_partial_refunds_on_same_payment() -> None:
    _new_order("o42")
    _pay("o42", 500)
    payment_id = _payments("o42")[0]["record_id"]

    assert _refund("o42", payment_id, 200).status_code == 201
    assert _refund("o42", payment_id, 100).status_code == 201  # 不同金额的第二笔部分退款
    order = client.get("/orders/o42", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 800
    # 超过该收款剩余可退余额（200）：冲突
    resp = _refund("o42", payment_id, 250)
    assert resp.status_code == 409 and resp.json()["detail"] == "refund exceeds refundable amount"
    assert client.get("/orders/o42", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 200
    assert len([r for r in _payments("o42") if r["record_type"] == "refund"]) == 2


def test_refund_cannot_exceed_order_net_collected() -> None:
    _new_order("o43")
    _pay("o43", 600)
    _pay("o43", 400)
    first_payment = _payments("o43")[0]["record_id"]
    # 该笔原收款只剩 600 可退；尝试超过订单净收款/可退余额被拒
    resp = _refund("o43", first_payment, 700)
    assert resp.status_code == 409 and resp.json()["detail"] == "refund exceeds refundable amount"
    order = client.get("/orders/o43", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 1000 and order["status"] == "settled"
    assert all(r["record_type"] == "payment" for r in _payments("o43"))


def test_refund_non_positive_amount_is_conflict() -> None:
    _new_order("o44")
    _pay("o44", 500)
    payment_id = _payments("o44")[0]["record_id"]
    for bad in (0, -100):
        resp = _refund("o44", payment_id, bad)
        assert resp.status_code == 409 and resp.json()["detail"] == "refund amount must be positive"
    order = client.get("/orders/o44", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 500
    assert all(r["record_type"] == "payment" for r in _payments("o44"))


def test_refund_replay_is_effective_only_once() -> None:
    _new_order("o45")
    _pay("o45", 500)
    payment_id = _payments("o45")[0]["record_id"]
    assert _refund("o45", payment_id, 200).status_code == 201
    # 同一退款请求重放（同订单、同原收款、同金额）：冲突且不重复扣减、不重复留痕
    replay = _refund("o45", payment_id, 200)
    assert replay.status_code == 409 and replay.json()["detail"] == "refund already applied"
    order = client.get("/orders/o45", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 300
    assert len([r for r in _payments("o45") if r["record_type"] == "refund"]) == 1


def test_refund_unknown_order_or_record_is_not_found() -> None:
    resp = _refund("missing-order", "pay_whatever", 10)
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"

    _new_order("o46")
    _pay("o46", 100)
    resp = _refund("o46", "pay_does_not_exist", 10)
    assert resp.status_code == 404 and resp.json()["detail"] == "payment record not found"
    order = client.get("/orders/o46", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100 and len(_payments("o46")) == 1


def test_refund_record_of_other_order_or_tenant_is_not_found() -> None:
    for oid in ("o47a", "o47b"):
        client.post("/orders", json={"tenant": "t1", "order_id": oid, "amount_cents": 500, "currency": "CNY"})
    _pay("o47a", 100)
    record_id = _payments("o47a")[0]["record_id"]
    # 流水不属于路径上的订单：按不存在处理
    assert _refund("o47b", record_id, 50).status_code == 404
    # 跨租户：一律按订单不存在处理，t1 账面与流水不变
    assert _refund("o47a", record_id, 50, tenant="t2").status_code == 404
    assert client.get("/orders/o47a", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 100
    assert all(r["record_type"] == "payment" for r in _payments("o47a"))


def test_records_list_payments_refunds_reversals_in_order_for_full_causal_chain() -> None:
    _new_order("o48")
    _pay("o48", 600)
    first_payment = _payments("o48")[0]["record_id"]
    _refund("o48", first_payment, 200)  # 部分退款：净收款 400
    _pay("o48", 400)  # 再收一笔：净收款 800
    second_payment = _payments("o48")[-1]["record_id"]
    assert _reverse("o48", second_payment).status_code == 201  # 冲正第二笔：净收款 400

    records = _payments("o48")
    types = [r["record_type"] for r in records]
    assert types == ["payment", "refund", "payment", "reversal"]
    # 退款与冲正都通过原流水标识关联原收款，可重建收讫到退回的完整因果链
    assert records[1]["related_record_id"] == first_payment
    assert records[3]["related_record_id"] == second_payment
    assert client.get("/orders/o48", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400


def test_refund_on_reversed_whole_payment_is_refused() -> None:
    _new_order("o49")
    _pay("o49", 500)
    payment_id = _payments("o49")[0]["record_id"]
    assert _reverse("o49", payment_id).status_code == 201
    # 冲正后净收款为 0：退款超过可退余额，账面与流水不变
    resp = _refund("o49", payment_id, 100)
    assert resp.status_code == 409 and resp.json()["detail"] == "refund exceeds refundable amount"
    assert client.get("/orders/o49", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert [r["record_type"] for r in _payments("o49")] == ["payment", "reversal"]


def test_installment_refund_returns_installment_to_unpaid_and_clears_record_ref() -> None:
    _new_order("o50")
    _register_plan("o50", PLAN_ITEMS)
    _pay_installment("o50", "i1", 300)
    _pay_installment("o50", "i2", 700)  # 全部收讫，订单结清
    i1_payment = _payments("o50")[0]["record_id"]

    resp = _refund("o50", i1_payment, 300, installment_id="i1")
    assert resp.status_code == 201, resp.text
    order = resp.json()
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300
    assert order["status"] == "accepted"

    plan = {i["installment_id"]: i for i in _plan("o50")}
    # 该期回到未收，收讫流水引用清空，可再次收讫
    assert plan["i1"]["status"] == "unpaid" and plan["i1"]["paid_record_id"] is None
    assert plan["i2"]["status"] == "paid"

    refund = _payments("o50")[-1]
    assert refund["record_type"] == "refund"
    assert refund["related_record_id"] == i1_payment and refund["installment_id"] == "i1"

    assert _pay_installment("o50", "i1", 300).status_code == 200
    assert client.get("/orders/o50", headers={"X-Tenant": "t1"}).json()["status"] == "settled"


def test_installment_refund_can_target_new_payment_after_cycle() -> None:
    _new_order("o51")
    _register_plan("o51", PLAN_ITEMS)
    _pay_installment("o51", "i1", 300)
    first_payment = _payments("o51")[0]["record_id"]
    assert _refund("o51", first_payment, 300, installment_id="i1").status_code == 201
    # 重放旧退款：期次已回到未收 → 可区分的“期次未收讫”，账面不变
    replay = _refund("o51", first_payment, 300, installment_id="i1")
    assert replay.status_code == 409 and replay.json()["detail"] == "installment is not paid"

    # 再次收讫后是一笔全新收款，可对其退款
    _pay_installment("o51", "i1", 300)
    new_payment = _payments("o51")[-1]["record_id"]
    assert new_payment != first_payment
    assert _refund("o51", new_payment, 300, installment_id="i1").status_code == 201
    assert {i["installment_id"]: i for i in _plan("o51")}["i1"]["status"] == "unpaid"


def test_installment_refund_failure_reasons_are_distinguishable() -> None:
    _new_order("o52")
    _register_plan("o52", PLAN_ITEMS)
    _pay_installment("o52", "i1", 300)
    i1_payment = _payments("o52")[0]["record_id"]

    # 分期收款的退款未声明期次
    resp = _refund("o52", i1_payment, 300)
    assert resp.status_code == 409 and resp.json()["detail"] == "installment id is required"
    # 期次不存在
    resp = _refund("o52", i1_payment, 300, installment_id="i9")
    assert resp.status_code == 409 and resp.json()["detail"] == "installment not found"
    # 只能退该期已收金额（金额不等）
    resp = _refund("o52", i1_payment, 200, installment_id="i1")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "refund amount does not match installment amount"
    # 声明的期次与原收款不属同一期（i2 未收讫）
    resp = _refund("o52", i1_payment, 700, installment_id="i2")
    assert resp.status_code == 409 and resp.json()["detail"] == "installment is not paid"

    # 任一失败都不改账面、期次状态与流水
    order = client.get("/orders/o52", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 300
    plan = {i["installment_id"]: i for i in _plan("o52")}
    assert plan["i1"]["status"] == "paid" and plan["i1"]["paid_record_id"] == i1_payment
    assert all(r["record_type"] == "payment" for r in _payments("o52"))


def test_whole_payment_refund_must_not_declare_installment() -> None:
    _new_order("o53")
    _pay("o53", 500)  # 未受理分期计划的整单收款
    payment_id = _payments("o53")[0]["record_id"]
    resp = _refund("o53", payment_id, 100, installment_id="i1")
    assert resp.status_code == 409 and resp.json()["detail"] == "installment plan not accepted"
    assert client.get("/orders/o53", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 500
    assert all(r["record_type"] == "payment" for r in _payments("o53"))


def test_installment_refund_requires_tenant_header_and_is_cross_tenant_safe() -> None:
    _new_order("o54")
    _register_plan("o54", PLAN_ITEMS)
    _pay_installment("o54", "i1", 300)
    payment_id = _payments("o54")[0]["record_id"]
    body = {"amount_cents": 300, "installment_id": "i1"}
    assert client.post(f"/orders/o54/payments/{payment_id}/refund", json=body).status_code == 400
    # 跨租户退款按订单不存在处理，t1 账面、期次、流水不变
    assert _refund("o54", payment_id, 300, installment_id="i1", tenant="t2").status_code == 404
    order = client.get("/orders/o54", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 300
    assert {i["installment_id"]: i for i in _plan("o54")}["i1"]["status"] == "paid"
    assert all(r["record_type"] == "payment" for r in _payments("o54"))
