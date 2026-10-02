import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as store
from app.store.db import migrate
from app.store.orders import LedgerError

migrate()
client = TestClient(app)

PLAN_ITEMS = [
    {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01T00:00:00Z"},
    {"installment_id": "i2", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"},
]


def _new_order(order_id: str, amount: int = 1000, tenant: str = "t1") -> None:
    resp = client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    )
    assert resp.status_code == 201


def _payments(order_id: str, tenant: str = "t1") -> list:
    resp = client.get(f"/orders/{order_id}/payments", headers={"X-Tenant": tenant})
    assert resp.status_code == 200
    return resp.json()["records"]


def _register_plan(order_id: str, tenant: str = "t1") -> None:
    resp = client.post(
        f"/orders/{order_id}/installments",
        json={"installments": PLAN_ITEMS},
        headers={"X-Tenant": tenant},
    )
    assert resp.status_code == 201, resp.text


def _idem_pay(order_id: str, amount: int, key: str, tenant: str = "t1", *, installment_id: str | None = None):
    body = {"amount_cents": amount, "idempotency_key": key}
    if installment_id is not None:
        body["installment_id"] = installment_id
    return client.post(f"/orders/{order_id}/payments", json=body, headers={"X-Tenant": tenant})


# ---------- 基本幂等语义 ----------

def test_first_payment_succeeds_and_returns_snapshot_with_record_id() -> None:
    _new_order("e1")
    resp = _idem_pay("e1", 400, "key-e1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paid_cents"] == 400 and body["outstanding_cents"] == 600 and body["status"] == "accepted"
    assert body["record_id"].startswith("pay_")
    # 正常入账，恰好一条收款流水
    records = _payments("e1")
    assert len(records) == 1 and records[0]["record_id"] == body["record_id"]


def test_replay_returns_same_result_without_double_entry() -> None:
    _new_order("e2")
    first = _idem_pay("e2", 400, "key-e2")
    assert first.status_code == 200
    for _ in range(3):
        again = _idem_pay("e2", 400, "key-e2")
        assert again.status_code == 200
        assert again.json() == first.json()  # 账面快照与流水标识与首次完全一致

    order = client.get("/orders/e2", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600
    records = _payments("e2")
    assert len(records) == 1 and records[0]["record_id"] == first.json()["record_id"]


def test_replay_returns_first_snapshot_even_after_refund_changes_books() -> None:
    _new_order("e3")
    first = _idem_pay("e3", 400, "key-e3").json()
    record_id = first["record_id"]
    # 首次收款之后该笔被退款，订单当前账面已变化
    refund = client.post(
        f"/orders/e3/payments/{record_id}/refund",
        json={"amount_cents": 400},
        headers={"X-Tenant": "t1"},
    )
    assert refund.status_code == 201, refund.text
    assert client.get("/orders/e3", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0

    # 重放仍返回首次成功时的快照与首次流水标识，且不补登记收款
    replay = _idem_pay("e3", 400, "key-e3")
    assert replay.status_code == 200
    assert replay.json() == first
    assert [r["record_type"] for r in _payments("e3")] == ["payment", "refund"]


def test_replay_after_order_settled_by_other_payments_still_returns_first_snapshot() -> None:
    _new_order("e4")
    first = _idem_pay("e4", 300, "key-e4").json()
    assert client.post("/orders/e4/payments", json={"amount_cents": 700},
                       headers={"X-Tenant": "t1"}).status_code == 200
    assert client.get("/orders/e4", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    replay = _idem_pay("e4", 300, "key-e4")
    assert replay.status_code == 200
    assert replay.json() == first  # 首次快照（未结清、paid=300），不随当前账面变化
    assert len(_payments("e4")) == 2


# ---------- 冲突：同键但意图不同 ----------

def test_same_key_with_different_amount_is_rejected() -> None:
    _new_order("e5")
    assert _idem_pay("e5", 400, "key-e5").status_code == 200
    resp = _idem_pay("e5", 300, "key-e5")
    assert resp.status_code == 409 and resp.json()["detail"] == "idempotency key conflict"
    # 冲突不改动任何数据
    order = client.get("/orders/e5", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 400
    assert len(_payments("e5")) == 1


def test_same_key_pointing_to_another_order_is_rejected() -> None:
    _new_order("e6a")
    _new_order("e6b")
    assert _idem_pay("e6a", 100, "key-e6").status_code == 200
    # 同一幂等键指向不同订单：冲突，两个订单账面都不变
    resp = _idem_pay("e6b", 100, "key-e6")
    assert resp.status_code == 409 and resp.json()["detail"] == "idempotency key conflict"
    assert client.get("/orders/e6a", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 100
    assert client.get("/orders/e6b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert len(_payments("e6a")) == 1 and _payments("e6b") == []


def test_installment_declaration_conflict_is_rejected() -> None:
    _new_order("e7")
    _register_plan("e7")
    assert _idem_pay("e7", 300, "key-e7", installment_id="i1").status_code == 200
    # 同键但期次声明不同：冲突，i2 不被收讫
    resp = _idem_pay("e7", 700, "key-e7", installment_id="i2")
    assert resp.status_code == 409 and resp.json()["detail"] == "idempotency key conflict"
    plan = {i["installment_id"]: i for i in client.get("/orders/e7/installments",
                                                       headers={"X-Tenant": "t1"}).json()["installments"]}
    assert plan["i1"]["status"] == "paid" and plan["i2"]["status"] == "unpaid"
    # 金额与期次都不一致同样冲突
    resp = _idem_pay("e7", 300, "key-e7", installment_id="i2")
    assert resp.status_code == 409 and resp.json()["detail"] == "idempotency key conflict"
    assert len(_payments("e7")) == 1


# ---------- 失败不占用幂等键 ----------

def test_failed_request_does_not_occupy_key_and_corrected_request_succeeds() -> None:
    _new_order("e8")
    # 金额超过未收金额：被既有校验拒绝，不占用幂等键
    bad = _idem_pay("e8", 1200, "key-e8")
    assert bad.status_code == 409 and bad.json()["detail"] == "payment exceeds outstanding amount"
    assert client.get("/orders/e8", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert _payments("e8") == []
    # 校正参数后同键成功登记
    ok = _idem_pay("e8", 400, "key-e8")
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 400
    assert len(_payments("e8")) == 1


def test_already_paid_installment_does_not_occupy_key() -> None:
    _new_order("e9")
    _register_plan("e9")
    assert _idem_pay("e9", 300, "occupier", installment_id="i1").status_code == 200
    # 期次已收讫：拒绝且不占用新键
    bad = _idem_pay("e9", 300, "key-e9", installment_id="i1")
    assert bad.status_code == 409 and bad.json()["detail"] == "installment already paid"
    # i2 是另一期，同键可成功（键尚未被占用）
    ok = _idem_pay("e9", 700, "key-e9", installment_id="i2")
    assert ok.status_code == 200 and ok.json()["status"] == "settled"
    assert len(_payments("e9")) == 2


def test_unknown_order_with_key_is_not_found_and_key_remains_usable() -> None:
    resp = _idem_pay("e-missing", 100, "key-em")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"
    # 失败不占键：之后受理同标识订单并用同键登记可以成功
    _new_order("e-missing")
    ok = _idem_pay("e-missing", 100, "key-em")
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 100


# ---------- 无键行为不变 ----------

def test_request_without_key_keeps_existing_behavior() -> None:
    _new_order("e10")
    first = client.post("/orders/e10/payments", json={"amount_cents": 400}, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and "record_id" not in first.json()
    # 无键重复提交按既有规则处理：第二笔正常入账（不做幂等去重）
    second = client.post("/orders/e10/payments", json={"amount_cents": 400}, headers={"X-Tenant": "t1"})
    assert second.status_code == 200
    assert client.get("/orders/e10", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 800
    assert len(_payments("e10")) == 2


def test_existing_installment_rules_are_not_bypassed_by_key() -> None:
    _new_order("e11")
    _register_plan("e11")
    # 有分期计划却不声明期次：既有校验照常拒绝，键不改变规则
    resp = _idem_pay("e11", 300, "key-e11")
    assert resp.status_code == 409 and resp.json()["detail"] == "installment id is required"
    # 金额不等于该期应收：拒绝且不占键
    resp = _idem_pay("e11", 200, "key-e11", installment_id="i1")
    assert resp.status_code == 409 and resp.json()["detail"] == "payment amount does not match installment amount"
    ok = _idem_pay("e11", 300, "key-e11", installment_id="i1")
    assert ok.status_code == 200 and ok.json()["record_id"]
    assert len(_payments("e11")) == 1


# ---------- 租户隔离 ----------

def test_same_key_across_tenants_is_independent() -> None:
    _new_order("e12", tenant="t1")
    _new_order("e12", tenant="t2")
    t1 = _idem_pay("e12", 300, "shared-key", tenant="t1")
    t2 = _idem_pay("e12", 400, "shared-key", tenant="t2")
    assert t1.status_code == 200 and t2.status_code == 200
    assert t1.json()["record_id"] != t2.json()["record_id"]
    # 各自只看到自己的一笔收款，互不可读、互不可借用
    assert client.get("/orders/e12", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 300
    assert client.get("/orders/e12", headers={"X-Tenant": "t2"}).json()["paid_cents"] == 400
    assert len(_payments("e12", tenant="t1")) == 1 and len(_payments("e12", tenant="t2")) == 1
    # 任一方重放只返回自己的首次结果
    assert _idem_pay("e12", 300, "shared-key", tenant="t1").json() == t1.json()
    assert _idem_pay("e12", 400, "shared-key", tenant="t2").json() == t2.json()


def test_cross_tenant_order_access_still_not_found_with_key() -> None:
    _new_order("e13", tenant="t1")
    resp = _idem_pay("e13", 100, "key-e13", tenant="t2")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"
    assert client.get("/orders/e13", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0


# ---------- 并发 ----------

def test_concurrent_same_key_requests_only_one_wins() -> None:
    _new_order("e14")
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            result = store.add_payment("t1", "e14", 400, "tester", idempotency_key="key-e14")
            with lock:
                outcomes.append(f"ok:{result['record_id']}")
        except LedgerError as error:
            with lock:
                outcomes.append(f"err:{error}")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 至多一笔成功入账，其余请求重放首次结果（同 record_id），无被拒绝为冲突的相同请求
    record_ids = {value.split(":", 1)[1] for value in outcomes if value.startswith("ok:")}
    assert len(record_ids) == 1
    assert not any(value.startswith("err:") for value in outcomes)
    assert len(outcomes) == 4
    order = client.get("/orders/e14", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600
    assert len(_payments("e14")) == 1


def test_concurrent_same_key_with_different_payload_is_explicitly_rejected() -> None:
    _new_order("e15")
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(amount: int) -> None:
        try:
            store.add_payment("t1", "e15", amount, "tester", idempotency_key="key-e15")
            with lock:
                outcomes.append("ok")
        except LedgerError as error:
            with lock:
                outcomes.append(str(error))

    threads = [
        threading.Thread(target=worker, args=(400,)),
        threading.Thread(target=worker, args=(500,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 只有一个金额意图能赢，另一个被明确拒绝为冲突；账面、流水闭合
    assert outcomes.count("ok") == 1
    assert outcomes.count("idempotency key conflict") == 1
    order = client.get("/orders/e15", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] in (400, 500)
    assert len(_payments("e15")) == 1


# ---------- 冲正 / 退款语义不受幂等键影响 ----------

def test_reversal_and_refund_semantics_unaffected_and_chain_intact() -> None:
    _new_order("e16")
    paid = _idem_pay("e16", 400, "key-e16").json()
    record_id = paid["record_id"]
    # 冲正走既有入口，不受幂等键影响：成功后重放仍是 409
    reversal = client.post(f"/orders/e16/payments/{record_id}/reversal", headers={"X-Tenant": "t1"})
    assert reversal.status_code == 201
    replay_rev = client.post(f"/orders/e16/payments/{record_id}/reversal", headers={"X-Tenant": "t1"})
    assert replay_rev.status_code == 409 and replay_rev.json()["detail"] == "payment already reversed"

    # 再用同一幂等键重放原始收款：仍返回首次快照（该流水已被冲正），不重新入账
    replay = _idem_pay("e16", 400, "key-e16")
    assert replay.status_code == 200 and replay.json() == paid
    records = _payments("e16")
    assert [r["record_type"] for r in records] == ["payment", "reversal"]
    # 因果链：冲正记录指向原收款流水
    assert records[1]["related_record_id"] == record_id
    # 账面当前与从未收到 400 一致
    assert client.get("/orders/e16", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0


def test_installment_idempotent_payment_refund_keeps_chain_and_state_closed() -> None:
    _new_order("e17")
    _register_plan("e17")
    paid = _idem_pay("e17", 300, "key-e17-i1", installment_id="i1").json()
    assert paid["record_id"] == _payments("e17")[-1]["record_id"]
    # 分期退款保持既有语义：该期回到未收
    refund = client.post(
        f"/orders/e17/payments/{paid['record_id']}/refund",
        json={"amount_cents": 300, "installment_id": "i1"},
        headers={"X-Tenant": "t1"},
    )
    assert refund.status_code == 201
    plan = {i["installment_id"]: i for i in client.get("/orders/e17/installments",
                                                       headers={"X-Tenant": "t1"}).json()["installments"]}
    assert plan["i1"]["status"] == "unpaid" and plan["i1"]["paid_record_id"] is None
    # 流水按发生顺序：收款、退款，因果链可重建
    records = _payments("e17")
    assert [r["record_type"] for r in records] == ["payment", "refund"]
    assert records[1]["related_record_id"] == paid["record_id"]
    # 重放幂等收款仍返回首次快照，不把已退期次重新置为收讫
    assert _idem_pay("e17", 300, "key-e17-i1", installment_id="i1").json() == paid
    plan = {i["installment_id"]: i for i in client.get("/orders/e17/installments",
                                                       headers={"X-Tenant": "t1"}).json()["installments"]}
    assert plan["i1"]["status"] == "unpaid"
    assert len(_payments("e17")) == 2
