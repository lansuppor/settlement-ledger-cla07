import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from datetime import UTC, datetime

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



# ---------- 业务发生时间 ----------

def test_flow_entries_carry_business_time_defaulting_to_now() -> None:
    from datetime import timedelta
    _new_order("bt0", 500)
    before = datetime.now(UTC) - timedelta(seconds=1)
    resp = client.post("/orders/bt0/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    after = datetime.now(UTC) + timedelta(seconds=1)
    assert resp.status_code == 200
    entry = _flow("bt0").json()["entries"][0]
    # 返回带时区偏移的完整日期时间（到秒）
    bt = datetime.fromisoformat(entry["business_time"])
    assert bt.tzinfo is not None and bt.microsecond == 0
    assert before <= bt <= after


def test_explicit_business_time_keeps_registered_offset_on_read() -> None:
    _new_order("bt1", 500)
    # 东八区 2026-03-10 08:00:00 == UTC 2026-03-10 00:00:00；读回保留登记时的 +08:00 写法
    resp = client.post(
        "/orders/bt1/payments",
        json={"amount_cents": 100, "business_time": "2026-03-10T08:00:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 200
    entry = _flow("bt1").json()["entries"][0]
    assert entry["business_time"] == "2026-03-10T08:00:00+08:00"
    # 冲正同样支持显式业务发生时间；Z 写法规范化为等价的 +00:00
    rev = client.post(
        "/orders/bt1/reversals",
        json={"reversal_id": "bt1r", "amount_cents": 40, "business_time": "2026-03-15T23:59:59Z"},
        headers={"X-Tenant": "t1"},
    )
    assert rev.status_code == 200
    rev_entry = _flow("bt1").json()["entries"][1]
    assert rev_entry["entry_type"] == "reversal"
    assert rev_entry["business_time"] == "2026-03-15T23:59:59+00:00"


def test_invalid_business_time_is_400_and_changes_nothing() -> None:
    _new_order("bt2", 500)
    bad_values = [
        "2026-03-10",                 # 只有日期
        "2026-03-10T08:00",           # 只到分钟
        "2026-03-10T08:00:00",        # 缺时区偏移
        "2026-03-10T08:00:00.5+08:00",  # 小数秒（精度超过秒）
        "2026-02-30T08:00:00+08:00",  # 历法不存在
        "not-a-time",
    ]
    for bad in bad_values:
        resp = client.post(
            "/orders/bt2/payments",
            json={"amount_cents": 100, "business_time": bad},
            headers={"X-Tenant": "t1"},
        )
        assert resp.status_code == 400, bad
    # 冲正入参同样校验
    client.post("/orders/bt2/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    resp = client.post(
        "/orders/bt2/reversals",
        json={"reversal_id": "bt2r", "amount_cents": 50, "business_time": "2026-03-10T08:00:00"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400
    # 被拒绝的请求不改变账务、不产生流水
    order = client.get("/orders/bt2", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100
    assert [e["entry_type"] for e in _flow("bt2").json()["entries"]] == ["payment"]
    # 被拒绝的冲正不占用冲正标识
    ok = client.post(
        "/orders/bt2/reversals",
        json={"reversal_id": "bt2r", "amount_cents": 50, "business_time": "2026-03-10T08:00:00Z"},
        headers={"X-Tenant": "t1"},
    )
    assert ok.status_code == 200


def test_business_time_survives_restart() -> None:
    from app.store.db import connect
    _new_order("bt3", 500)
    client.post(
        "/orders/bt3/payments",
        json={"amount_cents": 100, "business_time": "2026-04-01T12:00:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    # 全新连接模拟重启：业务发生时间原样保留（比较用 UTC 文本与登记写法文本都在）
    conn = connect()
    try:
        row = conn.execute(
            "SELECT business_time, business_time_text FROM order_flow"
            " WHERE tenant='t1' AND order_id='bt3' AND seq=1"
        ).fetchone()
    finally:
        conn.close()
    assert row["business_time"] == "2026-04-01T04:00:00+00:00"
    assert row["business_time_text"] == "2026-04-01T12:00:00+08:00"
    # 此后新操作继续在末尾追加，各自携带自身时间，读回均保留登记时的偏移写法
    client.post(
        "/orders/bt3/payments",
        json={"amount_cents": 200, "business_time": "2026-04-02T12:00:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    entries = _flow("bt3").json()["entries"]
    assert [e["business_time"] for e in entries] == [
        "2026-04-01T12:00:00+08:00",
        "2026-04-02T12:00:00+08:00",
    ]


# ---------- 时间范围查询 ----------

def _range(order_id: str, start: str, end: str, tenant: str = "t1"):
    return client.get(
        f"/orders/{order_id}/flow/range",
        params={"start": start, "end": end},
        headers={"X-Tenant": tenant},
    )


def _seed_ranged_order(order_id: str = "rg1") -> None:
    _new_order(order_id, 1000)
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 100, "business_time": "2026-05-01T00:00:00Z"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 200, "business_time": "2026-05-10T12:30:45Z"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/reversals",
        json={"reversal_id": f"{order_id}-r", "amount_cents": 50, "business_time": "2026-05-20T08:15:30+08:00"},
        headers={"X-Tenant": "t1"},
    )  # 冲正时间换算 UTC 为 2026-05-20T00:15:30Z
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 300, "business_time": "2026-06-01T23:59:59Z"},
        headers={"X-Tenant": "t1"},
    )


def test_range_query_returns_entries_within_window_by_effective_order() -> None:
    _seed_ranged_order("rg1")
    resp = _range("rg1", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    # 区间内为前两笔收款与一笔冲正；早于起点（无）与晚于终点（6 月那笔）不混入
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert [e["entry_type"] for e in entries] == ["payment", "payment", "reversal"]
    assert [e["amount_cents"] for e in entries] == [100, 200, 50]
    # 各条内容与流水清单完全一致
    full = {e["seq"]: e for e in _flow("rg1").json()["entries"]}
    for entry in entries:
        assert entry == full[entry["seq"]]
    # 冲正按其自身业务时间（带偏移换算后）落入区间，读回保留登记时的 +08:00 写法
    assert entries[2]["business_time"] == "2026-05-20T08:15:30+08:00"
    assert entries[2]["reversal_id"] == "rg1-r"


def test_range_query_is_inclusive_on_both_bounds() -> None:
    _seed_ranged_order("rg2")
    # 起点恰等于第一笔、终点恰等于最后一笔：边界流水都应返回
    resp = _range("rg2", "2026-05-01T00:00:00Z", "2026-06-01T23:59:59Z")
    assert [e["seq"] for e in resp.json()["entries"]] == [1, 2, 3, 4]
    # 早于起点一秒：第一笔被排除
    resp = _range("rg2", "2026-05-01T00:00:01Z", "2026-06-01T23:59:59Z")
    assert [e["seq"] for e in resp.json()["entries"]] == [2, 3, 4]
    # 晚于终点一秒：最后一笔被排除
    resp = _range("rg2", "2026-05-01T00:00:00Z", "2026-06-01T23:59:58Z")
    assert [e["seq"] for e in resp.json()["entries"]] == [1, 2, 3]
    # 偏移换算后比较：以 +08:00 表达同一时刻，等价命中
    resp = _range("rg2", "2026-05-01T08:00:00+08:00", "2026-05-01T08:00:00+08:00")
    assert [e["seq"] for e in resp.json()["entries"]] == [1]


def test_range_query_empty_window_and_order_without_entries() -> None:
    _seed_ranged_order("rg3")
    # 区间合法但无流水落入：200 与空数组（订单存在）
    resp = _range("rg3", "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
    assert resp.status_code == 200 and resp.json()["entries"] == []
    # 尚无任何收款/冲正的订单，空结果同样为 200 空数组
    _new_order("rg3empty", 500)
    resp = _range("rg3empty", "2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z")
    assert resp.status_code == 200 and resp.json()["entries"] == []


def test_range_query_requires_valid_bounds_and_distinguishes_errors() -> None:
    _seed_ranged_order("rg4")
    base = {"start": "2026-05-01T00:00:00Z", "end": "2026-05-31T23:59:59Z"}
    # 缺少起点/终点
    resp = client.get("/orders/rg4/flow/range", params={"end": base["end"]}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "start is required"
    resp = client.get("/orders/rg4/flow/range", params={"start": base["start"]}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "end is required"
    # 起点/终点非法（不同 detail，可区分）
    resp = client.get(
        "/orders/rg4/flow/range",
        params={"start": "2026-05-01", "end": base["end"]},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400 and resp.json()["detail"].startswith("start ")
    resp = client.get(
        "/orders/rg4/flow/range",
        params={"start": base["start"], "end": "2026-05-31T23:59:59"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400 and resp.json()["detail"].startswith("end ")
    # 起点晚于终点
    resp = _range("rg4", "2026-05-31T23:59:59Z", "2026-05-01T00:00:00Z")
    assert resp.status_code == 400 and resp.json()["detail"] == "start must not be later than end"
    # 缺租户头
    resp = client.get("/orders/rg4/flow/range", params=base)
    assert resp.status_code == 400 and resp.json()["detail"] == "tenant header is required"


def test_range_query_on_missing_or_foreign_order_is_not_found() -> None:
    params = {"start": "2026-01-01T00:00:00Z", "end": "2026-12-31T23:59:59Z"}
    # 订单不存在：参数全部合法后才读数据，返回 404
    resp = client.get("/orders/rg-nope/flow/range", params=params, headers={"X-Tenant": "t1"})
    assert resp.status_code == 404
    # 跨租户：同样按不存在处理，不泄漏对象是否存在
    _seed_ranged_order("rg5")
    resp = client.get("/orders/rg5/flow/range", params=params, headers={"X-Tenant": "t2"})
    assert resp.status_code == 404
    # 参数非法时不读任何数据：即使订单不存在也返回参数错误而非 404
    resp = client.get(
        "/orders/rg-nope/flow/range",
        params={"start": "bad", "end": params["end"]},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400
    resp = client.get(
        "/orders/rg5/flow/range",
        params={"start": "2026-12-31T00:00:00Z", "end": "2026-01-01T00:00:00Z"},  # 起点晚于终点
        headers={"X-Tenant": "t2"},
    )
    assert resp.status_code == 400


def test_range_query_is_scoped_per_tenant_and_order() -> None:
    _new_order("rg6", 500, tenant="t1")
    _new_order("rg6", 500, tenant="t2")
    client.post(
        "/orders/rg6/payments",
        json={"amount_cents": 100, "business_time": "2026-05-01T00:00:00Z"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        "/orders/rg6/payments",
        json={"amount_cents": 200, "business_time": "2026-05-02T00:00:00Z"},
        headers={"X-Tenant": "t2"},
    )
    params = {"start": "2026-01-01T00:00:00Z", "end": "2026-12-31T23:59:59Z"}
    a = client.get("/orders/rg6/flow/range", params=params, headers={"X-Tenant": "t1"}).json()["entries"]
    b = client.get("/orders/rg6/flow/range", params=params, headers={"X-Tenant": "t2"}).json()["entries"]
    assert [(e["seq"], e["amount_cents"]) for e in a] == [(1, 100)]
    assert [(e["seq"], e["amount_cents"]) for e in b] == [(1, 200)]


# ---------- 按对账时区的按日汇总 ----------

def _summary(order_id: str, start: str, end: str, tz: str, tenant: str = "t1"):
    return client.get(
        f"/orders/{order_id}/flow/summary",
        params={"start": start, "end": end, "tz": tz},
        headers={"X-Tenant": tenant},
    )


def _seed_summary_order(order_id: str = "sm1") -> None:
    _new_order(order_id, 2000)
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 100, "business_time": "2026-05-01T10:00:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 200, "business_time": "2026-05-01T23:30:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    # UTC 口径属 5 月 1 日，+08:00 口径属 5 月 2 日：用于区分分组时区
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 300, "business_time": "2026-05-02T00:30:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/reversals",
        json={"reversal_id": f"{order_id}-r", "amount_cents": 50, "business_time": "2026-05-02T09:00:00Z"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 400, "business_time": "2026-06-01T00:00:00Z"},
        headers={"X-Tenant": "t1"},
    )


def test_summary_groups_by_reconciliation_timezone() -> None:
    _seed_summary_order("sm1")
    resp = _summary("sm1", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z", "+08:00")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "sm1" and body["tz"] == "+08:00"
    days = body["days"]
    # 按 +08:00 归组：第三笔收款（UTC 5 月 1 日 16:30）归入 5 月 2 日；6 月那笔不在区间内
    assert [d["date"] for d in days] == ["2026-05-01", "2026-05-02"]
    assert days[0]["payment_cents"] == 300 and days[0]["reversal_cents"] == 0
    assert days[0]["entry_count"] == 2
    assert days[0]["first_business_time"] == "2026-05-01T10:00:00+08:00"
    assert days[0]["last_business_time"] == "2026-05-01T23:30:00+08:00"
    assert days[1]["payment_cents"] == 300 and days[1]["reversal_cents"] == 50
    assert days[1]["entry_count"] == 2
    assert days[1]["first_business_time"] == "2026-05-02T00:30:00+08:00"
    assert days[1]["last_business_time"] == "2026-05-02T09:00:00+00:00"
    # 同一批流水按 UTC 归组则归属不同：分组以调用方给出的偏移为准
    resp = _summary("sm1", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z", "Z")
    days = resp.json()["days"]
    assert resp.json()["tz"] == "+00:00"
    assert [d["date"] for d in days] == ["2026-05-01", "2026-05-02"]
    assert (days[0]["payment_cents"], days[0]["entry_count"]) == (600, 3)
    assert (days[1]["reversal_cents"], days[1]["entry_count"]) == (50, 1)


def test_summary_is_inclusive_and_closes_with_flow_range() -> None:
    _seed_summary_order("sm2")
    # 边界值恰在起点/终点时计入；早于起点或晚于终点不计入
    resp = _summary("sm2", "2026-05-01T02:00:00Z", "2026-06-01T00:00:00Z", "+08:00")
    days = resp.json()["days"]
    assert sum(d["entry_count"] for d in days) == 5
    # 起点/终点各提前/延后一秒，边界流水被排除
    resp = _summary("sm2", "2026-05-01T02:00:01Z", "2026-06-01T00:00:00Z", "+08:00")
    assert sum(d["entry_count"] for d in resp.json()["days"]) == 4
    resp = _summary("sm2", "2026-05-01T02:00:00Z", "2026-05-31T23:59:59Z", "+08:00")
    assert sum(d["entry_count"] for d in resp.json()["days"]) == 4
    # 与流水区间查询逐条闭合：条数之和等于区间内流水条数，合计等于对应流水金额之和
    start, end = "2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z"
    entries = _range("sm2", start, end).json()["entries"]
    days = _summary("sm2", start, end, "+08:00").json()["days"]
    assert sum(d["entry_count"] for d in days) == len(entries)
    assert sum(d["payment_cents"] for d in days) == sum(
        e["amount_cents"] for e in entries if e["entry_type"] == "payment"
    )
    assert sum(d["reversal_cents"] for d in days) == sum(
        e["amount_cents"] for e in entries if e["entry_type"] == "reversal"
    )


def test_summary_empty_window_and_order_without_entries() -> None:
    _seed_summary_order("sm3")
    resp = _summary("sm3", "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z", "+08:00")
    assert resp.status_code == 200 and resp.json()["days"] == []
    _new_order("sm3empty", 500)
    resp = _summary("sm3empty", "2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z", "+08:00")
    assert resp.status_code == 200 and resp.json()["days"] == []


def test_summary_requires_valid_params_and_distinguishes_errors() -> None:
    _seed_summary_order("sm4")
    base = {"start": "2026-05-01T00:00:00Z", "end": "2026-05-31T23:59:59Z", "tz": "+08:00"}
    url = "/orders/sm4/flow/summary"
    # 缺少起点/终点/对账时区
    resp = client.get(url, params={"end": base["end"], "tz": base["tz"]}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "start is required"
    resp = client.get(url, params={"start": base["start"], "tz": base["tz"]}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "end is required"
    resp = client.get(url, params={"start": base["start"], "end": base["end"]}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "tz is required"
    # 起点/终点/时区非法（不同 detail，可区分）
    resp = client.get(url, params={**base, "start": "2026-05-01"}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"].startswith("start ")
    resp = client.get(url, params={**base, "end": "2026-05-31"}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"].startswith("end ")
    for bad_tz in ["abc", "+25:00", "+08:60", "08:00", ""]:
        resp = client.get(url, params={**base, "tz": bad_tz}, headers={"X-Tenant": "t1"})
        assert resp.status_code == 400 and resp.json()["detail"].startswith("tz "), bad_tz
    # 起点晚于终点
    resp = _summary("sm4", "2026-05-31T23:59:59Z", "2026-05-01T00:00:00Z", "+08:00")
    assert resp.status_code == 400 and resp.json()["detail"] == "start must not be later than end"
    # 缺租户头
    resp = client.get(url, params=base)
    assert resp.status_code == 400 and resp.json()["detail"] == "tenant header is required"


def test_summary_on_missing_or_foreign_order_is_not_found() -> None:
    params = {"start": "2026-01-01T00:00:00Z", "end": "2026-12-31T23:59:59Z", "tz": "+08:00"}
    # 订单不存在：参数全部合法后才读数据，返回 404
    resp = client.get("/orders/sm-nope/flow/summary", params=params, headers={"X-Tenant": "t1"})
    assert resp.status_code == 404
    # 跨租户：同样按不存在处理，不泄漏对象是否存在
    _seed_summary_order("sm5")
    resp = client.get("/orders/sm5/flow/summary", params=params, headers={"X-Tenant": "t2"})
    assert resp.status_code == 404
    # 参数非法时不读任何数据：即使订单不存在也返回参数错误而非 404
    resp = client.get(
        "/orders/sm-nope/flow/summary",
        params={"start": "bad", "end": params["end"], "tz": "+08:00"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400
    resp = client.get(
        "/orders/sm5/flow/summary",
        params={"start": params["start"], "end": params["end"], "tz": "bad"},
        headers={"X-Tenant": "t2"},
    )
    assert resp.status_code == 400


def test_summary_survives_restart_and_keeps_day_assignment() -> None:
    from app.store.db import connect
    _seed_summary_order("sm6")
    before = _summary("sm6", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z", "+08:00").json()
    # 以全新连接模拟服务重启：时间写法与归属日期保持不变
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT business_time, business_time_text FROM order_flow"
            " WHERE tenant='t1' AND order_id='sm6' ORDER BY seq"
        ).fetchall()
        assert [r["business_time_text"] for r in rows] == [
            "2026-05-01T10:00:00+08:00",
            "2026-05-01T23:30:00+08:00",
            "2026-05-02T00:30:00+08:00",
            "2026-05-02T09:00:00+00:00",
            "2026-06-01T00:00:00+00:00",
        ]
    finally:
        conn.close()
    after = _summary("sm6", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z", "+08:00").json()
    assert after == before
