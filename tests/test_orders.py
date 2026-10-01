import os, re, tempfile
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

def _flow(order_id: str, tenant: str = "t1"):
    return client.get(f"/orders/{order_id}/flow", headers={"X-Tenant": tenant})

def test_flow_empty_for_new_order() -> None:
    _new_order("f0", 500)
    resp = _flow("f0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "f0" and body["entries"] == []

def test_flow_records_payments_and_reversals_in_order_with_snapshots() -> None:
    _new_order("f1", 500)
    client.post("/orders/f1/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    client.post("/orders/f1/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    client.post("/orders/f1/reversals", json={"reversal_id": "frv1", "amount_cents": 150}, headers={"X-Tenant": "t1"})
    entries = _flow("f1").json()["entries"]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert [e["entry_type"] for e in entries] == ["payment", "payment", "reversal"]
    assert [e["amount_cents"] for e in entries] == [200, 300, 150]
    # 同类型多次操作以订单内唯一的流水标识区分
    assert [e["entry_id"] for e in entries] == ["pay-1", "pay-2", "rev-3"]
    # 每条流水保留本次操作后的账务快照
    assert (entries[0]["paid_cents"], entries[0]["outstanding_cents"], entries[0]["status"]) == (200, 300, "accepted")
    assert (entries[1]["paid_cents"], entries[1]["outstanding_cents"], entries[1]["status"]) == (500, 0, "settled")
    assert (entries[2]["paid_cents"], entries[2]["outstanding_cents"], entries[2]["status"]) == (350, 150, "accepted")
    # 冲正流水关联当次冲正标识，收款流水该字段为空
    assert entries[0]["reversal_id"] is None
    assert entries[2]["reversal_id"] == "frv1"

def test_flow_records_each_success_once_ignoring_duplicates_and_failures() -> None:
    _new_order("f2", 500)
    client.post("/orders/f2/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    payload = {"reversal_id": "frv2", "amount_cents": 100}
    first = client.post("/orders/f2/reversals", json=payload, headers={"X-Tenant": "t1"})
    second = client.post("/orders/f2/reversals", json=payload, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and second.status_code == 200
    # 同标识不同金额（409）、超额冲正（409）、超额收款（409）与非法金额（422）都不产生流水
    assert client.post("/orders/f2/reversals", json={"reversal_id": "frv2", "amount_cents": 200}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/f2/reversals", json={"reversal_id": "frv2b", "amount_cents": 999}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/f2/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/f2/payments", json={"amount_cents": 0}, headers={"X-Tenant": "t1"}).status_code == 422
    entries = _flow("f2").json()["entries"]
    assert [(e["entry_type"], e["amount_cents"]) for e in entries] == [("payment", 500), ("reversal", 100)]
    assert [e["seq"] for e in entries] == [1, 2]
    # 失败请求不改变账务
    assert client.get("/orders/f2", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400

def test_flow_query_missing_foreign_order_and_tenant_header() -> None:
    assert _flow("does-not-exist").status_code == 404
    _new_order("f3", 500)
    assert _flow("f3", tenant="t2").status_code == 404
    assert client.get("/orders/f3/flow").status_code == 400
    assert _flow("f3").status_code == 200

def test_flow_is_scoped_per_order_and_tenant() -> None:
    _new_order("f4a", 500, tenant="t1")
    _new_order("f4a", 500, tenant="t2")
    client.post("/orders/f4a/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/f4a/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t2"})
    a = _flow("f4a", "t1").json()["entries"]
    b = _flow("f4a", "t2").json()["entries"]
    assert [(e["seq"], e["amount_cents"]) for e in a] == [(1, 100)]
    assert [(e["seq"], e["amount_cents"]) for e in b] == [(1, 200)]

def test_flow_survives_restart_and_keeps_append_order() -> None:
    from app.store.db import connect
    _new_order("f5", 500)
    client.post("/orders/f5/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/f5/reversals", json={"reversal_id": "frv5", "amount_cents": 40}, headers={"X-Tenant": "t1"})
    # 以全新连接模拟服务重启：流水仍在，顺序、金额与快照不变
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT seq, entry_type, amount_cents, paid_cents, outstanding_cents, status, reversal_id"
            " FROM order_flow WHERE tenant='t1' AND order_id='f5' ORDER BY seq"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "payment", 100, 100, 400, "accepted", None),
            (2, "reversal", 40, 60, 440, "accepted", "frv5"),
        ]
    finally:
        conn.close()
    # 重启后的新操作在末尾追加，不重排也不改写已有流水
    client.post("/orders/f5/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    entries = _flow("f5").json()["entries"]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[2]["entry_id"] == "pay-3"
    assert (entries[2]["paid_cents"], entries[2]["outstanding_cents"], entries[2]["status"]) == (260, 240, "accepted")
    assert entries[0]["amount_cents"] == 100 and entries[1]["reversal_id"] == "frv5"

_OCCURRED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

def test_flow_entries_carry_business_time_defaulted_by_server() -> None:
    _new_order("t0", 500)
    client.post("/orders/t0/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    client.post("/orders/t0/reversals", json={"reversal_id": "trv0", "amount_cents": 40}, headers={"X-Tenant": "t1"})
    entries = _flow("t0").json()["entries"]
    assert len(entries) == 2
    for entry in entries:
        assert _OCCURRED_AT.match(entry["occurred_at"]), entry["occurred_at"]

def test_flow_entries_use_caller_provided_business_time() -> None:
    _new_order("t1x", 500)
    client.post("/orders/t1x/payments", json={"amount_cents": 100, "occurred_at": "2026-01-05T10:00:00+08:00"}, headers={"X-Tenant": "t1"})
    client.post("/orders/t1x/reversals", json={"reversal_id": "trv1", "amount_cents": 40, "occurred_at": "2026-03-01T12:30:00+08:00"}, headers={"X-Tenant": "t1"})
    entries = _flow("t1x").json()["entries"]
    assert [e["occurred_at"] for e in entries] == ["2026-01-05T10:00:00+08:00", "2026-03-01T12:30:00+08:00"]

def test_invalid_business_time_is_rejected_without_side_effects() -> None:
    _new_order("t2x", 500)
    for bad in ["2026-01-05", "2026-01-05 10:00:00", "2026-01-05T10:00+08:00", "not-a-time", "2026-13-01T10:00:00+08:00"]:
        assert client.post("/orders/t2x/payments", json={"amount_cents": 100, "occurred_at": bad}, headers={"X-Tenant": "t1"}).status_code == 400
        assert client.post("/orders/t2x/reversals", json={"reversal_id": "trv2", "amount_cents": 10, "occurred_at": bad}, headers={"X-Tenant": "t1"}).status_code == 400
    # 被拒绝的请求不产生流水、不改变账务，也不占用冲正标识
    assert _flow("t2x").json()["entries"] == []
    assert client.get("/orders/t2x", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    client.post("/orders/t2x/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/t2x/reversals", json={"reversal_id": "trv2", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 200

def _flow_range(order_id: str, start=None, end=None, tenant: str = "t1"):
    params = {}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return client.get(f"/orders/{order_id}/flow/range", params=params, headers={"X-Tenant": tenant})

def _range_order(order_id: str) -> None:
    _new_order(order_id, 1000)
    client.post(f"/orders/{order_id}/payments", json={"amount_cents": 200, "occurred_at": "2026-01-10T09:00:00+08:00"}, headers={"X-Tenant": "t1"})
    client.post(f"/orders/{order_id}/payments", json={"amount_cents": 300, "occurred_at": "2026-02-10T09:00:00+08:00"}, headers={"X-Tenant": "t1"})
    client.post(f"/orders/{order_id}/reversals", json={"reversal_id": f"rv-{order_id}", "amount_cents": 100, "occurred_at": "2026-03-10T09:00:00+08:00"}, headers={"X-Tenant": "t1"})

def test_flow_range_returns_entries_within_bounds_in_order() -> None:
    _range_order("t3a")
    resp = _flow_range("t3a", "2026-02-01T00:00:00+08:00", "2026-03-01T00:00:00+08:00")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "t3a"
    entries = body["entries"]
    assert [(e["seq"], e["entry_type"], e["amount_cents"]) for e in entries] == [(2, "payment", 300)]
    # 各条流水内容与清单返回完全一致
    full = _flow("t3a").json()["entries"]
    assert entries == [full[1]]

def test_flow_range_bounds_are_inclusive_and_cover_all_entries() -> None:
    _range_order("t3b")
    entries = _flow_range("t3b", "2026-01-10T09:00:00+08:00", "2026-03-10T09:00:00+08:00").json()["entries"]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert [e["occurred_at"] for e in entries] == [
        "2026-01-10T09:00:00+08:00", "2026-02-10T09:00:00+08:00", "2026-03-10T09:00:00+08:00",
    ]

def test_flow_range_empty_when_nothing_in_window() -> None:
    _range_order("t3c")
    resp = _flow_range("t3c", "2026-06-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00")
    assert resp.status_code == 200 and resp.json()["entries"] == []

def test_flow_range_compares_instants_across_offsets() -> None:
    _range_order("t3d")
    # 2026-02-10T09:00:00+08:00 即 2026-02-10T01:00:00Z，落在以 UTC 表示的区间内
    entries = _flow_range("t3d", "2026-02-10T00:00:00+00:00", "2026-02-10T02:00:00+00:00").json()["entries"]
    assert [e["seq"] for e in entries] == [2]

def test_flow_range_rejects_invalid_params_before_reading() -> None:
    _range_order("t3e")
    assert _flow_range("t3e").status_code == 400  # 缺少起点与终点
    assert _flow_range("t3e", start="2026-01-01T00:00:00+08:00").status_code == 400  # 缺少终点
    assert _flow_range("t3e", end="2026-01-01T00:00:00+08:00").status_code == 400  # 缺少起点
    assert _flow_range("t3e", "bad", "2026-01-01T00:00:00+08:00").status_code == 400  # 起点非法
    assert _flow_range("t3e", "2026-01-01T00:00:00+08:00", "2026-01-01").status_code == 400  # 终点非法
    assert _flow_range("t3e", "2026-05-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00").status_code == 400  # 起点晚于终点

def test_flow_range_missing_or_foreign_order_is_not_found() -> None:
    _range_order("t3f")
    assert _flow_range("nope", "2026-01-01T00:00:00+08:00", "2026-12-01T00:00:00+08:00").status_code == 404
    assert _flow_range("t3f", "2026-01-01T00:00:00+08:00", "2026-12-01T00:00:00+08:00", tenant="t2").status_code == 404
    assert client.get("/orders/t3f/flow/range", params={"start": "2026-01-01T00:00:00+08:00", "end": "2026-12-01T00:00:00+08:00"}).status_code == 400

def test_business_time_survives_restart() -> None:
    from app.store.db import connect
    _new_order("t4x", 500)
    client.post("/orders/t4x/payments", json={"amount_cents": 100, "occurred_at": "2026-01-05T10:00:00+08:00"}, headers={"X-Tenant": "t1"})
    # 以全新连接模拟服务重启：业务发生时间保持不变
    conn = connect()
    try:
        row = conn.execute("SELECT occurred_at FROM order_flow WHERE tenant='t1' AND order_id='t4x'").fetchone()
        assert row["occurred_at"] == "2026-01-05T10:00:00+08:00"
    finally:
        conn.close()
    entries = _flow("t4x").json()["entries"]
    assert entries[0]["occurred_at"] == "2026-01-05T10:00:00+08:00"

def test_duplicate_reversal_does_not_add_flow_entry_or_change_time() -> None:
    _new_order("t5x", 500)
    client.post("/orders/t5x/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    payload = {"reversal_id": "trv5", "amount_cents": 100, "occurred_at": "2026-02-01T08:00:00+08:00"}
    first = client.post("/orders/t5x/reversals", json=payload, headers={"X-Tenant": "t1"})
    second = client.post("/orders/t5x/reversals", json=payload, headers={"X-Tenant": "t1"})
    assert first.status_code == 200 and first.json() == second.json()
    entries = _flow("t5x").json()["entries"]
    assert [e["entry_type"] for e in entries] == ["payment", "reversal"]
    assert entries[1]["occurred_at"] == "2026-02-01T08:00:00+08:00"

