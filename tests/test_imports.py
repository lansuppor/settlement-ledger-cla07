import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
import pytest
from fastapi.testclient import TestClient

from app.entry import app
from app.store import imports, orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)


def _new_order(order_id: str, amount: int, tenant: str = "t1") -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201


def _register_plan(order_id: str, items: list, tenant: str = "t1") -> None:
    resp = client.post(f"/orders/{order_id}/installments", json={"installments": items}, headers={"X-Tenant": tenant})
    assert resp.status_code == 201


def _write_csv(tmp_path, rows: list[str], header: bool = True) -> str:
    lines = (["order_id,amount_cents,installment_id"] if header else []) + rows
    path = tmp_path / "payments.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _import(file_path: str, tenant: str = "t1", originator: str | None = None):
    body = {"file_path": file_path}
    headers = {"X-Tenant": tenant}
    if originator is not None:
        body["originator"] = originator
    return client.post("/payments/import", json=body, headers=headers)


def _order(order_id: str, tenant: str = "t1") -> dict:
    resp = client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant})
    assert resp.status_code == 200
    return resp.json()


def _records(order_id: str, tenant: str = "t1") -> list:
    resp = client.get(f"/orders/{order_id}/payments", headers={"X-Tenant": tenant})
    assert resp.status_code == 200
    return resp.json()["records"]


def test_import_all_rows_accepted_in_file_order(tmp_path) -> None:
    _new_order("bi-o1", 1000)
    _new_order("bi-o2", 500)
    path = _write_csv(tmp_path, ["bi-o1,400,", "bi-o2,500,", "bi-o1,600,"])

    resp = _import(path, originator="erp")
    assert resp.status_code == 200, resp.text
    batch = resp.json()
    assert batch["batch_id"] and batch["status"] == "completed"
    results = batch["results"]
    assert [r["line_no"] for r in results] == [1, 2, 3]
    assert all(r["outcome"] == "accepted" for r in results)
    # 成功行给出受理后的已收、未收与状态；同一订单多行按顺序推进
    assert (results[0]["paid_cents"], results[0]["outstanding_cents"], results[0]["order_status"]) == (400, 600, "accepted")
    assert (results[1]["paid_cents"], results[1]["outstanding_cents"], results[1]["order_status"]) == (500, 0, "settled")
    assert (results[2]["paid_cents"], results[2]["outstanding_cents"], results[2]["order_status"]) == (1000, 0, "settled")
    # 流水与单笔收款同构，发起方按声明记入
    assert [r["originator"] for r in _records("bi-o1")] == ["erp", "erp"]
    assert [r["amount_cents"] for r in _records("bi-o1")] == [400, 600]
    assert results[0]["record_id"] == _records("bi-o1")[0]["record_id"]


def test_import_partial_failure_rows_are_independent(tmp_path) -> None:
    _new_order("bi-o3", 300)
    _new_order("bi-o4", 100, tenant="t2")  # 其他租户的订单：按不存在处理
    path = _write_csv(tmp_path, [
        "bi-o3,300,",       # 行 1：成功
        "bi-o3,100,",       # 行 2：超过未收金额
        "ghost,10,",        # 行 3：订单不存在
        "bi-o4,100,",       # 行 4：跨租户，按订单不存在处理
        "bi-o3,abc,",       # 行 5：金额无法解析
    ])

    resp = _import(path, originator="erp")
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["outcome"] for r in results] == ["accepted", "rejected", "rejected", "rejected", "rejected"]
    assert [r["reason"] for r in results[1:]] == [
        "payment exceeds outstanding amount",
        "order not found",
        "order not found",
        "invalid line",
    ]
    # 失败行不影响成功行，也不留任何部分效果
    assert _order("bi-o3")["paid_cents"] == 300
    assert len(_records("bi-o3")) == 1
    assert _order("bi-o4", tenant="t2")["paid_cents"] == 0


def test_import_originator_defaults_to_tenant(tmp_path) -> None:
    _new_order("bi-o5", 100)
    path = _write_csv(tmp_path, ["bi-o5,100,"])
    resp = _import(path)  # 未声明发起方
    assert resp.status_code == 200
    assert resp.json()["originator"] == "t1"
    assert _records("bi-o5")[0]["originator"] == "t1"


def test_import_replay_returns_first_batch_without_rebooking(tmp_path) -> None:
    _new_order("bi-o6", 500)
    path = _write_csv(tmp_path, ["bi-o6,500,"])

    first = _import(path, originator="erp").json()
    replay = _import(path, originator="erp")
    assert replay.status_code == 200
    again = replay.json()
    # 同一文件路径与发起方：同一批次，返回首次结论
    assert again["batch_id"] == first["batch_id"]
    assert again["results"] == first["results"]
    # 不重复记账、不重复留痕
    assert _order("bi-o6")["paid_cents"] == 500
    assert len(_records("bi-o6")) == 1
    # 不同发起方视为不同批次
    other = _import(path, originator="backoffice").json()
    assert other["batch_id"] != first["batch_id"]


def test_import_batch_query_and_cross_tenant_safety(tmp_path) -> None:
    _new_order("bi-o7", 200)
    path = _write_csv(tmp_path, ["bi-o7,200,"])
    batch = _import(path, originator="erp").json()

    resp = client.get(f"/payments/import/{batch['batch_id']}", headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    assert resp.json()["results"] == batch["results"]
    # 跨租户与不存在一律 404，不泄漏批次是否存在
    assert client.get(f"/payments/import/{batch['batch_id']}", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/payments/import/imp_missing", headers={"X-Tenant": "t1"}).status_code == 404
    # 缺少租户头
    assert client.post("/payments/import", json={"file_path": path}).status_code == 400
    assert client.get(f"/payments/import/{batch['batch_id']}").status_code == 400


def test_import_installment_rows_follow_single_payment_rules(tmp_path) -> None:
    _new_order("bi-o8", 1000)
    _register_plan("bi-o8", [
        {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01T00:00:00Z"},
        {"installment_id": "i2", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"},
    ])
    path = _write_csv(tmp_path, [
        "bi-o8,300,i1",     # 行 1：成功
        "bi-o8,700,",       # 行 2：已受理计划必须声明期次
        "bi-o8,300,i1",     # 行 3：期次已收讫
        "bi-o8,100,i2",     # 行 4：金额与该期应收不等
        "bi-o8,700,i9",     # 行 5：期次不存在
        "bi-o8,700,i2",     # 行 6：成功，订单结清
    ])
    resp = _import(path, originator="erp")
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["outcome"] for r in results] == ["accepted", "rejected", "rejected", "rejected", "rejected", "accepted"]
    assert [r["reason"] for r in results[1:6]] == [
        "installment id is required",
        "installment already paid",
        "payment amount does not match installment amount",
        "installment not found",
        None,
    ]
    order = _order("bi-o8")
    assert order["paid_cents"] == 1000 and order["status"] == "settled"
    plan = client.get("/orders/bi-o8/installments", headers={"X-Tenant": "t1"}).json()["installments"]
    assert all(i["status"] == "paid" for i in plan)
    # 分期流水与单笔收款同构：携带期次标识
    assert [r["installment_id"] for r in _records("bi-o8")] == ["i1", "i2"]


def test_import_row_with_installment_but_no_plan_is_rejected(tmp_path) -> None:
    _new_order("bi-o9", 300)
    path = _write_csv(tmp_path, ["bi-o9,300,i1"])
    results = _import(path, originator="erp").json()["results"]
    assert results[0]["outcome"] == "rejected"
    assert results[0]["reason"] == "installment plan not accepted"
    assert _order("bi-o9")["paid_cents"] == 0
    assert _records("bi-o9") == []


def test_import_resume_after_interruption_only_fills_remaining_rows(tmp_path, monkeypatch) -> None:
    _new_order("bi-r1", 1000)
    path = _write_csv(tmp_path, ["bi-r1,100,", "bi-r1,200,", "bi-r1,300,", "bi-r1,400,"])

    real = orders._apply_payment
    calls = {"n": 0}

    def flaky(conn, tenant, order_id, amount_cents, originator, installment_id=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")  # 第 3 行处理到一半服务宕掉
        return real(conn, tenant, order_id, amount_cents, originator, installment_id)

    monkeypatch.setattr(orders, "_apply_payment", flaky)
    with pytest.raises(RuntimeError):
        imports.import_payments("t1", path, "erp")
    monkeypatch.undo()

    # 中断后：批次可凭标识查询到已完成行的结论，状态仍为 running
    conn = connect()
    row = conn.execute(
        "SELECT batch_id, status FROM import_batches WHERE tenant='t1' AND file_path=?", (path,)
    ).fetchone()
    conn.close()
    assert row["status"] == "running"
    partial = imports.get_batch("t1", row["batch_id"])
    assert [r["line_no"] for r in partial["results"]] == [1, 2]
    assert _order("bi-r1")["paid_cents"] == 300  # 只有前两行生效
    assert len(_records("bi-r1")) == 2

    # 续跑同一文件：只补齐剩余行，已生效行不重复记账、不重复留痕
    resumed = _import(path, originator="erp")
    assert resumed.status_code == 200
    batch = resumed.json()
    assert batch["batch_id"] == row["batch_id"] and batch["status"] == "completed"
    assert [r["outcome"] for r in batch["results"]] == ["accepted"] * 4
    assert _order("bi-r1")["paid_cents"] == 1000
    assert _order("bi-r1")["status"] == "settled"
    assert len(_records("bi-r1")) == 4


def test_imported_payments_can_be_reversed_and_refunded(tmp_path) -> None:
    _new_order("bi-o10", 500)
    _new_order("bi-o11", 500)
    path = _write_csv(tmp_path, ["bi-o10,500,", "bi-o11,500,"])
    results = _import(path, originator="erp").json()["results"]

    # 导入的收款流水与单笔收款完全同构：可正常退款（含部分退款）
    refund = client.post(
        f"/orders/bi-o10/payments/{results[0]['record_id']}/refund",
        json={"amount_cents": 200}, headers={"X-Tenant": "t1"},
    )
    assert refund.status_code == 201
    assert refund.json()["paid_cents"] == 300

    # 也可正常冲正：账面与从未发生该笔收款一致
    reversal = client.post(f"/orders/bi-o11/payments/{results[1]['record_id']}/reversal", headers={"X-Tenant": "t1"})
    assert reversal.status_code == 201
    order = reversal.json()
    assert order["paid_cents"] == 0 and order["status"] == "accepted"
    assert [r["record_type"] for r in _records("bi-o10")] == ["payment", "refund"]
    assert [r["record_type"] for r in _records("bi-o11")] == ["payment", "reversal"]


def test_import_missing_file_is_400(tmp_path) -> None:
    resp = _import(str(tmp_path / "no-such.csv"), originator="erp")
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("import file not readable")
