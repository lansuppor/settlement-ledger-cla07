import os, tempfile
os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_flows.sqlite"))
from fastapi.testclient import TestClient
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1", "X-Actor": "cashier-1"}


def _make_order(order_id: str, amount: int = 500, tenant: str = "t1") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201


def _pay(order_id: str, amount: int, headers: dict | None = None) -> dict:
    resp = client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers=headers or H)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _flow_id_of(index: int = 0) -> str:
    resp = client.get("/orders/r1/payments", headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    return resp.json()["flows"][index]["flow_id"]


# ---------- 正常路径 ----------

def test_payment_is_recorded_as_flow() -> None:
    _make_order("r1", 500)
    _pay("r1", 200)
    resp = client.get("/orders/r1/payments", headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    flows = resp.json()["flows"]
    assert len(flows) == 1
    assert flows[0]["type"] == "payment"
    assert flows[0]["amount_cents"] == 200
    assert flows[0]["origin_flow_id"] is None
    assert flows[0]["actor_id"] == "cashier-1"
    assert flows[0]["flow_id"]
    assert flows[0]["created_at"]


def test_reversal_restores_state_as_if_payment_never_happened() -> None:
    _make_order("r2", 500)
    _pay("r2", 200, headers={**H, "X-Actor": "a"})
    flow = client.get("/orders/r2/payments", headers={"X-Tenant": "t1"}).json()["flows"][0]

    resp = client.post(f"/orders/r2/payments/{flow['flow_id']}/reversal", headers={**H, "X-Actor": "b"})
    assert resp.status_code == 200, resp.text
    order = resp.json()
    assert order["paid_cents"] == 0
    assert order["outstanding_cents"] == 500
    assert order["status"] == "accepted"

    flows = client.get("/orders/r2/payments", headers={"X-Tenant": "t1"}).json()["flows"]
    assert [f["type"] for f in flows] == ["payment", "reversal"]
    reversal = flows[1]
    assert reversal["amount_cents"] == 200
    assert reversal["origin_flow_id"] == flow["flow_id"]
    assert reversal["actor_id"] == "b"
    assert reversal["flow_id"] != flow["flow_id"]


def test_reversal_after_settlement_returns_to_outstanding() -> None:
    _make_order("r3", 300)
    _pay("r3", 100)
    _pay("r3", 200)
    settled = client.get("/orders/r3", headers={"X-Tenant": "t1"}).json()
    assert settled["status"] == "settled" and settled["outstanding_cents"] == 0

    first = client.get("/orders/r3/payments", headers={"X-Tenant": "t1"}).json()["flows"][0]
    resp = client.post(f"/orders/r3/payments/{first['flow_id']}/reversal", headers=H)
    assert resp.status_code == 200
    order = resp.json()
    assert order["status"] == "accepted"
    assert order["paid_cents"] == 200
    assert order["outstanding_cents"] == 100


def test_flows_returned_in_occurrence_order_with_full_causal_chain() -> None:
    _make_order("r4", 1000)
    _pay("r4", 100)
    _pay("r4", 200)
    flows = client.get("/orders/r4/payments", headers={"X-Tenant": "t1"}).json()["flows"]
    reversed_id = flows[0]["flow_id"]
    client.post(f"/orders/r4/payments/{reversed_id}/reversal", headers=H)
    _pay("r4", 50)

    flows = client.get("/orders/r4/payments", headers={"X-Tenant": "t1"}).json()["flows"]
    assert [(f["type"], f["amount_cents"]) for f in flows] == [
        ("payment", 100),
        ("payment", 200),
        ("reversal", 100),
        ("payment", 50),
    ]
    assert flows[2]["origin_flow_id"] == reversed_id
    # 时间戳单调不减（seq 决定顺序）。
    assert [f["created_at"] for f in flows] == sorted(f["created_at"] for f in flows)


# ---------- 失败路径 ----------

def test_reverse_unknown_order_is_not_found() -> None:
    resp = client.post("/orders/missing/payments/nope/reversal", headers=H)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "order not found"


def test_list_flows_unknown_order_is_not_found() -> None:
    assert client.get("/orders/missing/payments", headers={"X-Tenant": "t1"}).status_code == 404


def test_reverse_unknown_flow_is_not_found_and_books_unchanged() -> None:
    _make_order("r5", 300)
    _pay("r5", 100)
    resp = client.post("/orders/r5/payments/does-not-exist/reversal", headers=H)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "payment flow not found"
    order = client.get("/orders/r5", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100 and order["status"] == "accepted"
    assert len(client.get("/orders/r5/payments", headers={"X-Tenant": "t1"}).json()["flows"]) == 1


def test_cross_tenant_access_acts_as_not_found() -> None:
    _make_order("r6", 300, tenant="t1")
    flow = _pay("r6", 100)  # noqa: F841
    flow_id = client.get("/orders/r6/payments", headers={"X-Tenant": "t1"}).json()["flows"][0]["flow_id"]

    # 查流水：订单对 t2 不存在
    assert client.get("/orders/r6/payments", headers={"X-Tenant": "t2"}).status_code == 404
    # 冲正：同样按订单不存在处理，无法借冲正探测他租户订单或流水
    resp = client.post(f"/orders/r6/payments/{flow_id}/reversal", headers={"X-Tenant": "t2", "X-Actor": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "order not found"
    # 原租户账面与流水不变
    order = client.get("/orders/r6", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100
    assert len(client.get("/orders/r6/payments", headers={"X-Tenant": "t1"}).json()["flows"]) == 1


def test_flow_of_other_order_cannot_be_reversed_under_this_order() -> None:
    _make_order("r7a", 300)
    _make_order("r7b", 300)
    _pay("r7a", 100)
    flow_id = client.get("/orders/r7a/payments", headers={"X-Tenant": "t1"}).json()["flows"][0]["flow_id"]
    # 用 r7a 的流水去请求 r7b 的冲正入口
    resp = client.post(f"/orders/r7b/payments/{flow_id}/reversal", headers=H)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "payment flow not found"
    assert client.get("/orders/r7b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0
    assert client.get("/orders/r7a", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 100


def test_duplicate_reversal_conflicts_and_keeps_books() -> None:
    _make_order("r8", 400)
    _pay("r8", 100)
    flow_id = client.get("/orders/r8/payments", headers={"X-Tenant": "t1"}).json()["flows"][0]["flow_id"]
    assert client.post(f"/orders/r8/payments/{flow_id}/reversal", headers=H).status_code == 200

    # 重复冲正 / 请求重放：冲突且账面与流水只反映一次冲正
    resp = client.post(f"/orders/r8/payments/{flow_id}/reversal", headers=H)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "payment already reversed"
    order = client.get("/orders/r8", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 0 and order["status"] == "accepted"
    flows = client.get("/orders/r8/payments", headers={"X-Tenant": "t1"}).json()["flows"]
    assert [f["type"] for f in flows] == ["payment", "reversal"]


def test_reversal_failure_is_atomic() -> None:
    _make_order("r9", 300)
    _pay("r9", 100)
    before = client.get("/orders/r9", headers={"X-Tenant": "t1"}).json()
    flows_before = client.get("/orders/r9/payments", headers={"X-Tenant": "t1"}).json()["flows"]

    bad = client.post("/orders/r9/payments/ghost/reversal", headers=H)
    assert bad.status_code == 404
    after = client.get("/orders/r9", headers={"X-Tenant": "t1"}).json()
    flows_after = client.get("/orders/r9/payments", headers={"X-Tenant": "t1"}).json()["flows"]
    assert after["paid_cents"] == before["paid_cents"]
    assert after["status"] == before["status"]
    assert len(flows_after) == len(flows_before)


def test_tenant_header_required_for_new_endpoints() -> None:
    _make_order("r10", 100)
    _pay("r10", 100)
    flow_id = client.get("/orders/r10/payments", headers={"X-Tenant": "t1"}).json()["flows"][0]["flow_id"]
    assert client.get("/orders/r10/payments").status_code == 400
    assert client.post(f"/orders/r10/payments/{flow_id}/reversal").status_code == 400


# ---------- 既有语义回归 ----------

def test_payment_still_cannot_exceed_outstanding_after_reversal() -> None:
    _make_order("r11", 300)
    _pay("r11", 300)
    flow_id = client.get("/orders/r11/payments", headers={"X-Tenant": "t1"}).json()["flows"][0]["flow_id"]
    assert client.post(f"/orders/r11/payments/{flow_id}/reversal", headers=H).status_code == 200
    # 冲正释放额度后仍不得超额收款
    assert client.post("/orders/r11/payments", json={"amount_cents": 301}, headers=H).status_code == 409
    assert client.post("/orders/r11/payments", json={"amount_cents": 300}, headers=H).status_code == 200
    assert client.get("/orders/r11", headers={"X-Tenant": "t1"}).json()["status"] == "settled"
