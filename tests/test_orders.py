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


def test_explicit_business_time_is_stored_as_utc_but_read_back_in_registered_offset() -> None:
    _new_order("bt1", 500)
    # 东八区 2026-03-10 08:00:00 == UTC 2026-03-10 00:00:00：基准按 UTC 记账，读回保留 +08:00 写法
    resp = client.post(
        "/orders/bt1/payments",
        json={"amount_cents": 100, "business_time": "2026-03-10T08:00:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 200
    entry = _flow("bt1").json()["entries"][0]
    assert entry["business_time"] == "2026-03-10T08:00:00+08:00"
    # 仍是带时区偏移的完整日期时间、到秒，表示同一业务时刻
    parsed = datetime.fromisoformat(entry["business_time"])
    assert parsed == datetime(2026, 3, 10, 0, 0, 0, tzinfo=UTC)
    # 冲正同样保留登记写法；Z 与 +00:00 为同一零偏移，统一以 +00:00 返回
    rev = client.post(
        "/orders/bt1/reversals",
        json={"reversal_id": "bt1r", "amount_cents": 40, "business_time": "2026-03-15T23:59:59Z"},
        headers={"X-Tenant": "t1"},
    )
    assert rev.status_code == 200
    rev_entry = _flow("bt1").json()["entries"][1]
    assert rev_entry["entry_type"] == "reversal"
    assert rev_entry["business_time"] == "2026-03-15T23:59:59+00:00"


def test_same_instant_different_offset_writings_are_distinct_entries_but_equal_instants() -> None:
    # 不同写法只影响展示，不影响时刻比较：区间查询先换算到同一时刻再比较
    _new_order("bt1b", 500)
    client.post(
        "/orders/bt1b/payments",
        json={"amount_cents": 100, "business_time": "2026-03-10T08:00:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        "/orders/bt1b/payments",
        json={"amount_cents": 100, "business_time": "2026-03-10T00:00:00+00:00"},
        headers={"X-Tenant": "t1"},
    )
    entries = _flow("bt1b").json()["entries"]
    # 两条流水各自保留登记写法、互不覆盖
    assert [e["business_time"] for e in entries] == [
        "2026-03-10T08:00:00+08:00",
        "2026-03-10T00:00:00+00:00",
    ]
    # 以任一写法表达同一时刻作闭区间边界，都只命中这同一时刻的流水
    hit = _range("bt1b", "2026-03-10T08:00:00+08:00", "2026-03-10T08:00:00+08:00").json()["entries"]
    assert [e["seq"] for e in hit] == [1, 2]


def test_service_clocked_business_time_uses_server_local_offset() -> None:
    _new_order("bt1c", 500)
    resp = client.post("/orders/bt1c/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    entry = _flow("bt1c").json()["entries"][0]
    parsed = datetime.fromisoformat(entry["business_time"])
    # 服务自行记账的记录以服务当前时间的偏移写法返回
    assert parsed.utcoffset() == datetime.now().astimezone().utcoffset()


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
    # 全新连接模拟重启：业务发生时间原样保留
    conn = connect()
    try:
        row = conn.execute(
            "SELECT business_time, business_time_offset FROM order_flow"
            " WHERE tenant='t1' AND order_id='bt3' AND seq=1"
        ).fetchone()
    finally:
        conn.close()
    assert row["business_time"] == "2026-04-01T04:00:00+00:00"
    # 登记时偏移随流水持久化，重启后写法保持不变
    assert row["business_time_offset"] == 8 * 60
    # 此后新操作继续在末尾追加，各自携带自身时间
    client.post(
        "/orders/bt3/payments",
        json={"amount_cents": 200, "business_time": "2026-04-02T12:00:00+08:00"},
        headers={"X-Tenant": "t1"},
    )
    entries = _flow("bt3").json()["entries"]
    # 读回保留登记时的偏移写法；基准时刻仍是各自换算到 UTC 的同一时刻
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
    # 冲正按其自身业务时间（带偏移换算后）落入区间；读回保留登记时的 +08:00 写法
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


# ---------- 按日汇总（对账时区偏移分组） ----------

def _summary(order_id: str, start: str, end: str, offset: str, tenant: str = "t1"):
    return client.get(
        f"/orders/{order_id}/flow/daily-summary",
        params={"start": start, "end": end, "offset": offset},
        headers={"X-Tenant": tenant},
    )


def _seed_summary_order(order_id: str = "ds1") -> None:
    # 四条流水刻意跨越不同写法的偏移与日历日边界（金额足够小，账务恒合法）
    _new_order(order_id, 100000)
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 100, "business_time": "2026-05-01T23:30:00+00:00"},  # +08:00 为 05-02 07:30
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 200, "business_time": "2026-05-02T01:00:00+08:00"},  # UTC 为 05-01 17:00
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/reversals",
        json={"reversal_id": f"{order_id}-r", "amount_cents": 50,
              "business_time": "2026-05-02T15:00:00+00:00"},  # +08:00 为 05-02 23:00
        headers={"X-Tenant": "t1"},
    )
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": 300, "business_time": "2026-05-03T00:30:00+08:00"},  # UTC 为 05-02 16:30
        headers={"X-Tenant": "t1"},
    )


_FULL_YEAR = ("2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z")


def test_daily_summary_groups_by_reconciliation_offset_east() -> None:
    _seed_summary_order("ds1")
    resp = _summary("ds1", *_FULL_YEAR, "+08:00")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "ds1" and body["offset"] == "+08:00"
    groups = body["groups"]
    # 无流水的日期不出现，结果按日期升序；按 +08:00 归组为 05-02（三笔）与 05-03（一笔）
    assert [g["date"] for g in groups] == ["2026-05-02", "2026-05-03"]
    assert groups[0]["payment_cents"] == 300 and groups[0]["reversal_cents"] == 50
    assert groups[0]["entry_count"] == 3
    assert groups[0]["first_business_time"] == "2026-05-02T01:00:00+08:00"
    assert groups[0]["last_business_time"] == "2026-05-02T23:00:00+08:00"
    assert groups[1]["payment_cents"] == 300 and groups[1]["reversal_cents"] == 0
    assert groups[1]["entry_count"] == 1
    assert groups[1]["first_business_time"] == groups[1]["last_business_time"] == "2026-05-03T00:30:00+08:00"
    # 与流水清单逐条闭合：组数条数之和等于区间流水条数，合计等于各流水金额之和
    assert sum(g["entry_count"] for g in groups) == 4
    assert sum(g["payment_cents"] for g in groups) == 600
    assert sum(g["reversal_cents"] for g in groups) == 50


def test_daily_summary_same_instants_group_differently_under_utc() -> None:
    _seed_summary_order("ds2")
    groups = _summary("ds2", *_FULL_YEAR, "Z").json()["groups"]
    # 同一批流水换算到 +00:00 后归组不同：05-01（两笔收款）、05-02（冲正 + 收款）
    assert [g["date"] for g in groups] == ["2026-05-01", "2026-05-02"]
    assert groups[0]["payment_cents"] == 300 and groups[0]["reversal_cents"] == 0
    assert groups[0]["entry_count"] == 2
    assert groups[0]["first_business_time"] == "2026-05-01T17:00:00+00:00"
    assert groups[0]["last_business_time"] == "2026-05-01T23:30:00+00:00"
    assert groups[1]["payment_cents"] == 300 and groups[1]["reversal_cents"] == 50
    assert groups[1]["entry_count"] == 2
    assert groups[1]["first_business_time"] == "2026-05-02T15:00:00+00:00"
    assert groups[1]["last_business_time"] == "2026-05-02T16:30:00+00:00"
    # 跨时区、跨日归组不重不漏：两种偏移下条数与金额合计完全一致
    east = _summary("ds2", *_FULL_YEAR, "+08:00").json()["groups"]
    for key in ("entry_count", "payment_cents", "reversal_cents"):
        assert sum(g[key] for g in groups) == sum(g[key] for g in east)


def test_daily_summary_is_a_closed_interval_and_closes_with_range_query() -> None:
    _seed_summary_order("ds3")
    # 最早一笔为 UTC 2026-05-01T17:00Z（登记写法 05-02T01:00+08:00），
    # 最晚一笔为 UTC 2026-05-02T16:30Z（登记写法 05-03T00:30+08:00）：
    # 起点终点恰为这两个边界值时都计入，四条流水全在区间内。
    resp = _summary("ds3", "2026-05-01T17:00:00Z", "2026-05-02T16:30:00Z", "+08:00")
    groups = resp.json()["groups"]
    assert sum(g["entry_count"] for g in groups) == 4
    # 早于起点一秒：最早一笔不计入任何分组
    groups = _summary("ds3", "2026-05-01T17:00:01Z", "2026-05-02T16:30:00Z", "+08:00").json()["groups"]
    assert sum(g["entry_count"] for g in groups) == 3
    assert sum(g["payment_cents"] for g in groups) == 400
    # 晚于终点一秒：最晚一笔不计入
    groups = _summary("ds3", "2026-05-01T17:00:00Z", "2026-05-02T16:29:59Z", "+08:00").json()["groups"]
    assert sum(g["entry_count"] for g in groups) == 3
    assert sum(g["payment_cents"] for g in groups) == 300 and sum(g["reversal_cents"] for g in groups) == 50
    # 与区间查询逐条闭合：同一区间的汇总条数恰等于区间流水条数，金额分别相等
    ranged = _range("ds3", "2026-05-01T17:00:00Z", "2026-05-02T16:30:00Z").json()["entries"]
    groups = _summary("ds3", "2026-05-01T17:00:00Z", "2026-05-02T16:30:00Z", "-11:00").json()["groups"]
    assert sum(g["entry_count"] for g in groups) == len(ranged) == 4
    assert sum(g["payment_cents"] for g in groups) == sum(
        e["amount_cents"] for e in ranged if e["entry_type"] == "payment"
    )
    assert sum(g["reversal_cents"] for g in groups) == sum(
        e["amount_cents"] for e in ranged if e["entry_type"] == "reversal"
    )


def test_daily_summary_empty_window_and_order_without_entries() -> None:
    _seed_summary_order("ds4")
    resp = _summary("ds4", "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z", "+08:00")
    assert resp.status_code == 200 and resp.json()["groups"] == []
    _new_order("ds4empty", 500)
    resp = _summary("ds4empty", *_FULL_YEAR, "Z")
    assert resp.status_code == 200 and resp.json()["groups"] == []


def test_daily_summary_requires_valid_params_and_distinguishes_errors() -> None:
    _seed_summary_order("ds5")
    base = {"start": "2026-05-01T00:00:00Z", "end": "2026-05-31T23:59:59Z", "offset": "+08:00"}
    resp = client.get("/orders/ds5/flow/daily-summary", params={"end": base["end"], "offset": "+08:00"}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "start is required"
    resp = client.get("/orders/ds5/flow/daily-summary", params={"start": base["start"], "offset": "+08:00"}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "end is required"
    resp = client.get("/orders/ds5/flow/daily-summary", params={"start": base["start"], "end": base["end"]}, headers={"X-Tenant": "t1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "offset is required"
    # 起点/终点非法仍返回各自可区分的错误
    resp = client.get(
        "/orders/ds5/flow/daily-summary",
        params={"start": "2026-05-01", "end": base["end"], "offset": "+08:00"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400 and resp.json()["detail"].startswith("start ")
    resp = client.get(
        "/orders/ds5/flow/daily-summary",
        params={"start": base["start"], "end": "2026-05-31T23:59:59", "offset": "+08:00"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400 and resp.json()["detail"].startswith("end ")
    # 偏移非法（缺正负号/位数不对/偏移写法不合法）
    for bad_offset in ("08:00", "+8:00", "abc", "+0800"):
        resp = client.get(
            "/orders/ds5/flow/daily-summary",
            params={"start": base["start"], "end": base["end"], "offset": bad_offset},
            headers={"X-Tenant": "t1"},
        )
        assert resp.status_code == 400 and resp.json()["detail"].startswith("offset "), bad_offset
    # Z 作为合法零偏移
    assert client.get(
        "/orders/ds5/flow/daily-summary",
        params={"start": base["start"], "end": base["end"], "offset": "Z"},
        headers={"X-Tenant": "t1"},
    ).status_code == 200
    # 起点晚于终点
    resp = _summary("ds5", "2026-05-31T23:59:59Z", "2026-05-01T00:00:00Z", "+08:00")
    assert resp.status_code == 400 and resp.json()["detail"] == "start must not be later than end"
    # 缺租户头
    resp = client.get("/orders/ds5/flow/daily-summary", params=base)
    assert resp.status_code == 400 and resp.json()["detail"] == "tenant header is required"
    # 参数非法时不读任何数据：订单不存在也先返回参数错误
    resp = client.get(
        "/orders/ds-nope/flow/daily-summary",
        params={"start": "bad", "end": base["end"], "offset": "+08:00"},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 400


def test_daily_summary_on_missing_or_foreign_order_is_not_found() -> None:
    resp = _summary("ds-nope", *_FULL_YEAR, "+08:00")
    assert resp.status_code == 404
    _seed_summary_order("ds6")
    resp = _summary("ds6", *_FULL_YEAR, "+08:00", tenant="t2")
    assert resp.status_code == 404


def test_daily_summary_is_scoped_per_tenant() -> None:
    _new_order("ds7", 500, tenant="t1")
    _new_order("ds7", 500, tenant="t2")
    client.post(
        "/orders/ds7/payments",
        json={"amount_cents": 100, "business_time": "2026-05-01T00:00:00Z"},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        "/orders/ds7/payments",
        json={"amount_cents": 200, "business_time": "2026-05-02T00:00:00Z"},
        headers={"X-Tenant": "t2"},
    )
    a = _summary("ds7", *_FULL_YEAR, "Z", tenant="t1").json()["groups"]
    b = _summary("ds7", *_FULL_YEAR, "Z", tenant="t2").json()["groups"]
    assert [(g["date"], g["payment_cents"], g["entry_count"]) for g in a] == [("2026-05-01", 100, 1)]
    assert [(g["date"], g["payment_cents"], g["entry_count"]) for g in b] == [("2026-05-02", 200, 1)]
