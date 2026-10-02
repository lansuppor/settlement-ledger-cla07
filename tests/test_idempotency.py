import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
import threading

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


def _register_plan(order_id: str, tenant: str = "t1") -> None:
    resp = client.post(
        f"/orders/{order_id}/installments",
        json={"installments": PLAN_ITEMS},
        headers={"X-Tenant": tenant},
    )
    assert resp.status_code == 201


def _payments(order_id: str, tenant: str = "t1") -> list:
    resp = client.get(f"/orders/{order_id}/payments", headers={"X-Tenant": tenant})
    assert resp.status_code == 200
    return resp.json()["records"]


def _idem_pay(order_id: str, amount: int, key: str, tenant: str = "t1", installment_id: str | None = None,
              originator: str | None = None):
    headers = {"X-Tenant": tenant}
    if originator:
        headers["X-Originator"] = originator
    body: dict = {"amount_cents": amount, "idempotency_key": key}
    if installment_id is not None:
        body["installment_id"] = installment_id
    return client.post(f"/orders/{order_id}/payments", json=body, headers=headers)


# ---------- 基本重放语义 ----------

def test_idempotent_payment_replays_first_result_without_double_booking() -> None:
    _new_order("o60")
    first = _idem_pay("o60", 400, "key-o60")
    assert first.status_code == 200, first.text
    snapshot = first.json()
    assert snapshot["record_id"] and snapshot["order_id"] == "o60"
    assert snapshot["paid_cents"] == 400 and snapshot["outstanding_cents"] == 600
    assert snapshot["status"] == "accepted"

    # 同键重试多少次都只登记一笔：返回与首次一致的流水标识与账面快照
    for _ in range(3):
        replay = _idem_pay("o60", 400, "key-o60")
        assert replay.status_code == 200
        assert replay.json() == snapshot

    order = client.get("/orders/o60", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600
    records = _payments("o60")
    assert len(records) == 1 and records[0]["record_id"] == snapshot["record_id"]


def test_replay_returns_first_snapshot_even_after_later_activity() -> None:
    _new_order("o61")
    first = _idem_pay("o61", 300, "key-o61").json()
    # 首次之后发生的其他收款不改变幂等响应：它永远是首次成功后的账面快照
    assert client.post("/orders/o61/payments", json={"amount_cents": 300},
                       headers={"X-Tenant": "t1"}).status_code == 200
    replay = _idem_pay("o61", 300, "key-o61")
    assert replay.status_code == 200
    assert replay.json() == first
    assert replay.json()["paid_cents"] == 300 and replay.json()["outstanding_cents"] == 700
    assert len(_payments("o61")) == 2  # 重放不补流水


def test_replay_after_reversal_keeps_first_result_and_adds_no_trace() -> None:
    _new_order("o70")
    first = _idem_pay("o70", 400, "key-o70").json()
    assert client.post(f"/orders/o70/payments/{first['record_id']}/reversal",
                       headers={"X-Tenant": "t1"}).status_code == 201
    # 冲正不改变幂等结论：仍返回首次流水标识与首次账面快照，不重复入账
    replay = _idem_pay("o70", 400, "key-o70")
    assert replay.status_code == 200 and replay.json() == first
    assert client.get("/orders/o70", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert [r["record_type"] for r in _payments("o70")] == ["payment", "reversal"]


# ---------- 冲突必须拒绝且不改动数据 ----------

def test_same_key_with_different_amount_is_rejected() -> None:
    _new_order("o62")
    first = _idem_pay("o62", 400, "key-o62")
    assert first.status_code == 200
    # 幂等键相同但金额声明与首次不同：明确拒绝，不改动任何数据
    conflict = _idem_pay("o62", 500, "key-o62")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency key request does not match the original request"
    assert client.get("/orders/o62", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400
    assert len(_payments("o62")) == 1
    # 与首次一致的请求仍可重放成功
    assert _idem_pay("o62", 400, "key-o62").json() == first.json()


def test_same_key_with_different_installment_is_rejected_before_installment_rules() -> None:
    _new_order("o63")
    _register_plan("o63")
    first = _idem_pay("o63", 300, "key-o63", installment_id="i1")
    assert first.status_code == 200
    # 期次声明不同：幂等冲突优先于“期次已收讫”等既有校验，不改动期次状态与流水
    conflict = _idem_pay("o63", 700, "key-o63", installment_id="i2")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency key request does not match the original request"
    missing = _idem_pay("o63", 300, "key-o63")  # 首次声明了 i1，本次不声明
    assert missing.status_code == 409
    assert missing.json()["detail"] == "idempotency key request does not match the original request"
    plan = {i["installment_id"]: i for i in client.get("/orders/o63/installments",
                                                       headers={"X-Tenant": "t1"}).json()["installments"]}
    assert plan["i1"]["status"] == "paid" and plan["i2"]["status"] == "unpaid"
    assert len(_payments("o63")) == 1
    # 参数校正回与首次一致后照常重放
    assert _idem_pay("o63", 300, "key-o63", installment_id="i1").json() == first.json()


def test_same_key_pointing_at_another_order_is_rejected_and_changes_nothing() -> None:
    _new_order("o64a")
    _new_order("o64b")
    assert _idem_pay("o64a", 400, "key-o64").status_code == 200
    # 同一幂等键对应同一订单：指向别的订单即冲突
    conflict = _idem_pay("o64b", 400, "key-o64")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency key was used for a different order"
    assert client.get("/orders/o64b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert _payments("o64b") == []
    assert client.get("/orders/o64a", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400


# ---------- 失败请求不占用幂等键 ----------

def test_rejected_request_does_not_consume_key() -> None:
    _new_order("o65")
    # 金额超过未收金额：被拒绝，不留痕、不占用幂等键
    too_much = _idem_pay("o65", 1200, "key-o65")
    assert too_much.status_code == 409 and too_much.json()["detail"] == "payment exceeds outstanding amount"
    # 校正请求参数后同一幂等键可以成功登记
    ok = _idem_pay("o65", 400, "key-o65")
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 400
    assert len(_payments("o65")) == 1
    # 键已绑定首次成功：再次用错误参数仍被幂等冲突拒绝
    assert _idem_pay("o65", 500, "key-o65").status_code == 409


def test_rejected_installment_request_does_not_consume_key() -> None:
    _new_order("o66")
    _register_plan("o66")
    bad = _idem_pay("o66", 999, "key-o66", installment_id="i1")
    assert bad.status_code == 409 and bad.json()["detail"] == "payment amount does not match installment amount"
    ok = _idem_pay("o66", 300, "key-o66", installment_id="i1")
    assert ok.status_code == 200 and ok.json()["record_id"]
    assert len(_payments("o66")) == 1


def test_unknown_order_with_key_is_not_found_and_key_remains_usable() -> None:
    resp = _idem_pay("missing-idem-order", 100, "key-o67")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"
    _new_order("o67")
    ok = _idem_pay("o67", 100, "key-o67")
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 100


# ---------- 跨租户隔离 ----------

def test_same_key_across_tenants_is_independent() -> None:
    # 两个租户各自受理同号订单，并用完全相同的幂等键收款：互不影响
    _new_order("o68", tenant="t1")
    _new_order("o68", tenant="t2")
    first_t1 = _idem_pay("o68", 400, "shared-key", tenant="t1")
    first_t2 = _idem_pay("o68", 400, "shared-key", tenant="t2")
    assert first_t1.status_code == 200 and first_t2.status_code == 200
    assert first_t1.json()["record_id"] != first_t2.json()["record_id"]

    # 任一方重放只取回自己的首次结果，不能读取或借用另一租户的请求身份
    assert _idem_pay("o68", 400, "shared-key", tenant="t1").json() == first_t1.json()
    assert _idem_pay("o68", 400, "shared-key", tenant="t2").json() == first_t2.json()
    # t2 用自己已绑定的键指向另一订单（即便该订单不存在）：按租户内的订单冲突拒绝，
    # 该结论只来自 t2 自己的绑定，不涉及 t1，也不改任何账面
    mismatch = _idem_pay("o68b", 400, "shared-key", tenant="t2")
    assert mismatch.status_code == 409 and mismatch.json()["detail"] == "idempotency key was used for a different order"
    assert len(_payments("o68", "t1")) == 1
    assert len(_payments("o68", "t2")) == 1


# ---------- 并发 ----------

def test_concurrent_same_key_requests_all_share_single_booking() -> None:
    _new_order("o72")
    outcomes: list = []

    def worker() -> None:
        try:
            outcomes.append(("ok", store.add_payment("t1", "o72", 400, "t1", idempotency_key="key-o72")))
        except LedgerError as error:
            outcomes.append(("conflict", str(error)))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 至多一笔成功入账；其余请求都拿到与首次一致的结果
    assert len(outcomes) == 6 and all(kind == "ok" for kind, _ in outcomes)
    record_ids = {payload["record_id"] for _, payload in outcomes}
    assert record_ids == {outcomes[0][1]["record_id"]}
    assert client.get("/orders/o72", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400
    assert len(_payments("o72")) == 1


def test_concurrent_same_key_with_differing_amounts_everyone_replays_or_is_rejected() -> None:
    _new_order("o73")
    # 先确定性地让 400 成为首次请求，再并发到达：同额重放、异额被明确拒绝
    seeded = store.add_payment("t1", "o73", 400, "t1", idempotency_key="key-o73")
    assert seeded["record_id"]
    outcomes: list = []

    def worker(amount: int) -> None:
        try:
            payload = store.add_payment("t1", "o73", amount, "t1", idempotency_key="key-o73")
            outcomes.append(("ok", payload["record_id"]))
        except LedgerError as error:
            outcomes.append(("conflict", str(error)))

    threads = [threading.Thread(target=worker, args=(400,)) for _ in range(4)]
    threads += [threading.Thread(target=worker, args=(500,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 仍只有一笔 400 落账：同额请求全部重放首次结果，异额请求全部被明确拒绝
    assert client.get("/orders/o73", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400
    records = _payments("o73")
    assert len(records) == 1 and records[0]["amount_cents"] == 400
    winners = [record_id for kind, record_id in outcomes if kind == "ok"]
    assert len(winners) == 4 and set(winners) == {seeded["record_id"]}
    rejected = [detail for kind, detail in outcomes if kind == "conflict"]
    assert len(rejected) == 4
    assert all(detail == "idempotency key request does not match the original request" for detail in rejected)


# ---------- 未声明幂等键：行为完全不变 ----------

def test_request_without_key_keeps_existing_behavior() -> None:
    _new_order("o74")
    first = client.post("/orders/o74/payments", json={"amount_cents": 400}, headers={"X-Tenant": "t1"})
    assert first.status_code == 200
    body = first.json()
    assert "record_id" not in body and "idempotency_key" not in body  # 仍是订单账面快照
    assert body["paid_cents"] == 400 and body["outstanding_cents"] == 600
    # 无幂等键的重复请求各自入账（既有语义不变）
    second = client.post("/orders/o74/payments", json={"amount_cents": 400}, headers={"X-Tenant": "t1"})
    assert second.status_code == 200 and second.json()["paid_cents"] == 800
    assert len(_payments("o74")) == 2


def test_installment_payment_with_key_replays_and_keeps_installment_closed() -> None:
    _new_order("o75")
    _register_plan("o75")
    first = _idem_pay("o75", 300, "key-o75", installment_id="i1")
    assert first.status_code == 200
    replay = _idem_pay("o75", 300, "key-o75", installment_id="i1")
    assert replay.status_code == 200 and replay.json() == first.json()
    plan = {i["installment_id"]: i for i in client.get("/orders/o75/installments",
                                                       headers={"X-Tenant": "t1"}).json()["installments"]}
    assert plan["i1"]["status"] == "paid" and plan["i1"]["paid_record_id"] == first.json()["record_id"]
    assert len(_payments("o75")) == 1
