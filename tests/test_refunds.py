import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_refunds.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store import refunds as refund_store
from app.store.db import migrate

migrate()
client = TestClient(app)


def _settled_order(order_id: str, tenant: str = "t1", amount: int = 1000) -> None:
    client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    )
    client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})


# ---------- 正常受理 / 读取 / 冲正 ----------

def test_accept_read_refund() -> None:
    _settled_order("r-o1", amount=500)
    resp = client.post(
        "/refunds",
        json={"refund_id": "r-1", "order_id": "r-o1", "amount_cents": 200},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["amount_cents"] == 200
    assert body["order_id"] == "r-o1"
    assert body["reversed_at"] is None

    got = client.get("/refunds/r-1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["refund_id"] == "r-1"


def test_order_exposes_refunded_and_refundable_and_conserves() -> None:
    _settled_order("r-o2", amount=500)
    client.post(
        "/refunds",
        json={"refund_id": "r-2", "order_id": "r-o2", "amount_cents": 200},
        headers={"X-Tenant": "t1"},
    )
    order = client.get("/orders/r-o2", headers={"X-Tenant": "t1"}).json()
    # 收款链路不被改写：未收仍为 0，已收仍为 500
    assert order["paid_cents"] == 500
    assert order["outstanding_cents"] == 0
    assert order["refunded_cents"] == 200
    assert order["refundable_cents"] == 300
    # 累计已退 + 可退余额 与订单金额守恒
    assert order["refunded_cents"] + order["refundable_cents"] == order["amount_cents"]


def test_reverse_refund_returns_amount_back() -> None:
    _settled_order("r-o3", amount=500)
    client.post(
        "/refunds",
        json={"refund_id": "r-3", "order_id": "r-o3", "amount_cents": 300},
        headers={"X-Tenant": "t1"},
    )
    resp = client.post("/refunds/r-3/reverse", headers={"X-Tenant": "t1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reversed"
    assert resp.json()["reversed_at"] is not None

    order = client.get("/orders/r-o3", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0
    assert order["refundable_cents"] == 500
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0


# ---------- 拒绝原因可区分，且不留部分数据 ----------

def test_refund_requires_settled_order() -> None:
    client.post(
        "/orders",
        json={"tenant": "t1", "order_id": "r-o4", "amount_cents": 500, "currency": "CNY"},
    )
    client.post("/orders/r-o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    resp = client.post(
        "/refunds",
        json={"refund_id": "r-4", "order_id": "r-o4", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "order is not settled"
    # 不留部分数据
    assert client.get("/refunds/r-4", headers={"X-Tenant": "t1"}).status_code == 404
    order = client.get("/orders/r-o4", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0 and order["paid_cents"] == 100


def test_refund_cannot_exceed_refundable_balance() -> None:
    _settled_order("r-o5", amount=500)
    client.post(
        "/refunds",
        json={"refund_id": "r-5a", "order_id": "r-o5", "amount_cents": 300},
        headers={"X-Tenant": "t1"},
    )
    resp = client.post(
        "/refunds",
        json={"refund_id": "r-5b", "order_id": "r-o5", "amount_cents": 300},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "refund exceeds refundable amount"
    assert client.get("/refunds/r-5b", headers={"X-Tenant": "t1"}).status_code == 404
    order = client.get("/orders/r-o5", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 300  # 仍只有首笔


def test_order_not_found_and_cross_tenant_are_indistinguishable() -> None:
    # 不存在的订单
    r1 = client.post(
        "/refunds",
        json={"refund_id": "r-6a", "order_id": "missing", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert r1.status_code == 404 and r1.json()["detail"] == "order not found"

    # 属于其他租户的订单：同样按不存在处理
    _settled_order("r-o6", tenant="t1")
    r2 = client.post(
        "/refunds",
        json={"refund_id": "r-6b", "order_id": "r-o6", "amount_cents": 100},
        headers={"X-Tenant": "t2"},
    )
    assert r2.status_code == 404 and r2.json()["detail"] == "order not found"


# ---------- 幂等、重复与冲突 ----------

def test_duplicate_accept_is_idempotent_and_keeps_first_doc() -> None:
    _settled_order("r-o7", amount=500)
    first = client.post(
        "/refunds",
        json={"refund_id": "r-7", "order_id": "r-o7", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert first.status_code == 201
    # 用不同金额、不同原订单重复受理：结论确定，不改变首次单据
    second = client.post(
        "/refunds",
        json={"refund_id": "r-7", "order_id": "r-o7", "amount_cents": 400},
        headers={"X-Tenant": "t1"},
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "refund already accepted"

    doc = client.get("/refunds/r-7", headers={"X-Tenant": "t1"}).json()
    assert doc["amount_cents"] == 100 and doc["status"] == "accepted"
    order = client.get("/orders/r-o7", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 100  # 金额只变动一次


def test_reverse_conflicts_are_distinguishable() -> None:
    _settled_order("r-o8", amount=500)
    client.post(
        "/refunds",
        json={"refund_id": "r-8", "order_id": "r-o8", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    # 对未受理的标识冲正
    assert client.post("/refunds/nope/reverse", headers={"X-Tenant": "t1"}).status_code == 404
    # 正常冲正
    assert client.post("/refunds/r-8/reverse", headers={"X-Tenant": "t1"}).status_code == 200
    # 重复冲正
    again = client.post("/refunds/r-8/reverse", headers={"X-Tenant": "t1"})
    assert again.status_code == 409
    assert again.json()["detail"] == "refund already reversed"
    # 冲正后同一标识不得再次受理
    reaccept = client.post(
        "/refunds",
        json={"refund_id": "r-8", "order_id": "r-o8", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert reaccept.status_code == 409
    assert reaccept.json()["detail"] == "refund already reversed"
    order = client.get("/orders/r-o8", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0


def test_cross_tenant_refund_read_is_not_found() -> None:
    _settled_order("r-o9", tenant="t1")
    client.post(
        "/refunds",
        json={"refund_id": "r-9", "order_id": "r-o9", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    assert client.get("/refunds/r-9", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/refunds/r-9/reverse", headers={"X-Tenant": "t2"}).status_code == 404
    # 其他租户的冲正不改变真实单据
    assert client.get("/refunds/r-9", headers={"X-Tenant": "t1"}).json()["status"] == "accepted"


def test_invalid_amount_rejected() -> None:
    _settled_order("r-o10", amount=500)
    resp = client.post(
        "/refunds",
        json={"refund_id": "r-10", "order_id": "r-o10", "amount_cents": 0},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 422


# ---------- 并发 / 重试 / 重启 ----------

def test_concurrent_accept_same_id_succeeds_at_most_once() -> None:
    _settled_order("r-c1", amount=1000)

    def call() -> tuple[str, dict | None]:
        return refund_store.accept("t1", "r-c1r", "r-c1", 200)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: call(), range(12)))

    accepted = [r for r in results if r[0] == "accepted"]
    assert len(accepted) == 1
    assert len(results) - len(accepted) == 11  # 其余全部 duplicate
    order = order_store.get("t1", "r-c1")
    assert order["refunded_cents"] == 200  # 账务不重复


def test_concurrent_accepts_never_overshoot_balance() -> None:
    _settled_order("r-c2", amount=1000)

    def call(i: int) -> str:
        try:
            result, _ = refund_store.accept("t1", f"r-c2r-{i}", "r-c2", 200)
        except refund_store.RefundExceedsBalance:
            return "exceeded"
        return result

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(call, range(8)))

    assert results.count("accepted") == 5  # 5 × 200 = 1000 恰好用尽可退余额
    assert results.count("exceeded") == 3
    order = order_store.get("t1", "r-c2")
    assert order["refunded_cents"] == 1000
    assert order["refundable_cents"] == 0


def test_state_survives_restart_and_migrate_remains_idempotent() -> None:
    _settled_order("r-p1", amount=800)
    client.post(
        "/refunds",
        json={"refund_id": "r-p1a", "order_id": "r-p1", "amount_cents": 300},
        headers={"X-Tenant": "t1"},
    )
    client.post(
        "/refunds",
        json={"refund_id": "r-p1b", "order_id": "r-p1", "amount_cents": 100},
        headers={"X-Tenant": "t1"},
    )
    client.post("/refunds/r-p1b/reverse", headers={"X-Tenant": "t1"})

    # 模拟重启：迁移重复执行不报错、不丢数据
    migrate()
    migrate()

    a = client.get("/refunds/r-p1a", headers={"X-Tenant": "t1"}).json()
    b = client.get("/refunds/r-p1b", headers={"X-Tenant": "t1"}).json()
    assert a["status"] == "accepted"
    assert b["status"] == "reversed"
    # 重启后仍可继续冲正 / 受理新退款
    assert client.post("/refunds/r-p1a/reverse", headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post(
        "/refunds",
        json={"refund_id": "r-p1c", "order_id": "r-p1", "amount_cents": 200},
        headers={"X-Tenant": "t1"},
    ).status_code == 201
    order = client.get("/orders/r-p1", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 200
    assert order["refunded_cents"] + order["refundable_cents"] == 800
