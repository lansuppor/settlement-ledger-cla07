import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import imports as import_store
from app.store.db import migrate

migrate()
client = TestClient(app)

TENANT = "ti"


def _new_order(order_id: str, amount: int = 1000, tenant: str = TENANT) -> None:
    resp = client.post("/orders", json={"tenant": tenant, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"})
    assert resp.status_code == 201, resp.text


def _register_plan(order_id: str, items: list, tenant: str = TENANT) -> None:
    resp = client.post(f"/orders/{order_id}/installments", json={"installments": items},
                       headers={"X-Tenant": tenant})
    assert resp.status_code == 201, resp.text


def _write_csv(tmp_path, name: str, rows: list[str]) -> str:
    path = tmp_path / name
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return str(path)


def _import(path: str, *, tenant: str = TENANT, originator: str | None = "alice"):
    headers = {"X-Tenant": tenant}
    if originator is not None:
        headers["X-Originator"] = originator
    return client.post("/payment-imports", json={"file_path": path}, headers=headers)


def _payments(order_id: str, tenant: str = TENANT) -> list:
    resp = client.get(f"/orders/{order_id}/payments", headers={"X-Tenant": tenant})
    assert resp.status_code == 200
    return resp.json()["records"]


PLAN = [
    {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01T00:00:00Z"},
    {"installment_id": "i2", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"},
]


# ---------- 正常路径 ----------

def test_import_accepts_lines_in_order_and_reports_books(tmp_path) -> None:
    _new_order("bi1", 1000)
    _new_order("bi2", 500)
    path = _write_csv(tmp_path, "ok.csv", [
        "order_id,amount_cents",
        "bi1,400",
        "bi2,500",
    ])
    resp = _import(path)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_id"].startswith("imp_")
    assert body["status"] == "completed"
    assert body["total_lines"] == 2
    assert body["accepted_count"] == 2 and body["rejected_count"] == 0

    lines = body["results"]
    assert [line["line_no"] for line in lines] == [1, 2]
    assert lines[0]["result"] == "accepted"
    assert lines[0]["order_id"] == "bi1"
    assert lines[0]["paid_cents"] == 400 and lines[0]["outstanding_cents"] == 600
    assert lines[0]["order_status"] == "accepted" and lines[0]["record_id"]
    assert lines[1]["paid_cents"] == 500 and lines[1]["outstanding_cents"] == 0
    assert lines[1]["order_status"] == "settled"

    # 成功行写入的收款流水与单笔收款完全同构，且记录发起方标识
    records = _payments("bi1")
    assert len(records) == 1
    assert records[0]["record_type"] == "payment" and records[0]["amount_cents"] == 400
    assert records[0]["originator"] == "alice"
    assert records[0]["record_id"] == lines[0]["record_id"]


def test_import_default_originator_falls_back_to_tenant(tmp_path) -> None:
    _new_order("bi3", 300)
    path = _write_csv(tmp_path, "default_originator.csv", ["order_id,amount_cents", "bi3,100"])
    resp = _import(path, originator=None)
    assert resp.status_code == 201, resp.text
    assert _payments("bi3")[0]["originator"] == TENANT


def test_import_optional_installment_column_round_trips(tmp_path) -> None:
    _new_order("bi4", 1000)
    _register_plan("bi4", PLAN)
    path = _write_csv(tmp_path, "plan.csv", [
        "order_id,amount_cents,installment_id",
        "bi4,300,i1",
        "bi4,700,i2",
    ])
    resp = _import(path)
    assert resp.status_code == 201, resp.text
    lines = resp.json()["results"]
    assert [line["result"] for line in lines] == ["accepted", "accepted"]
    order = client.get("/orders/bi4", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 1000 and order["status"] == "settled"
    records = _payments("bi4")
    assert [r["installment_id"] for r in records] == ["i1", "i2"]


# ---------- 部分失败：逐行结论与单笔收款一致 ----------

def test_import_partial_failure_is_explainable_and_leaves_no_partial_effect(tmp_path) -> None:
    _new_order("bi10", 1000)
    _new_order("bi11", 1000)
    # 跨租户订单：tX 的订单对本租户不存在
    _new_order("bi12", 1000, tenant="tX")
    path = _write_csv(tmp_path, "partial.csv", [
        "order_id,amount_cents",
        "bi10,400",       # 成功
        "bi10,900",       # 超过未收（剩余 600）
        "bi12,100",       # 跨租户：order not found
        "missing,100",    # 订单不存在
        "bi11,200",       # 成功：失败行不影响同批其他行
        ",100",           # 行级格式错误：缺订单标识
        "bi11,abc",       # 行级格式错误：金额不可解析
        "bi11,0",         # 行级格式错误：金额非正
    ])
    resp = _import(path)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["accepted_count"] == 2 and body["rejected_count"] == 6
    lines = body["results"]

    assert lines[0]["result"] == "accepted" and lines[0]["paid_cents"] == 400
    assert lines[1]["result"] == "rejected"
    assert lines[1]["reject_reason"] == "payment exceeds outstanding amount"
    assert lines[2]["reject_reason"] == "order not found"  # 跨租户不泄漏
    assert lines[3]["reject_reason"] == "order not found"
    assert lines[4]["result"] == "accepted" and lines[4]["paid_cents"] == 200
    assert lines[5]["reject_reason"] == "order id is required"
    assert lines[6]["reject_reason"] == "invalid amount_cents"
    assert lines[7]["reject_reason"] == "invalid amount_cents"
    for line in lines:
        if line["result"] == "rejected":
            assert line["record_id"] is None
            assert line["paid_cents"] is None and line["order_status"] is None

    # 失败行无半行残留：账面只计入成功行，失败行不留痕
    order = client.get("/orders/bi10", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 400 and order["status"] == "accepted"
    assert len(_payments("bi10")) == 1
    assert len(_payments("bi11")) == 1
    # 跨租户对象的账面在其属主视角下完全未动
    assert client.get("/orders/bi12", headers={"X-Tenant": "tX"}).json()["paid_cents"] == 0


def test_import_installment_reject_reasons_match_single_payment(tmp_path) -> None:
    _new_order("bi20", 1000)
    _register_plan("bi20", PLAN)
    _new_order("bi21", 1000)  # 未受理计划
    path = _write_csv(tmp_path, "installment_reject.csv", [
        "order_id,amount_cents,installment_id",
        "bi20,300,",       # 已受理计划却未声明期次
        "bi20,200,i1",     # 金额不等于该期应收
        "bi20,300,i9",     # 期次不存在
        "bi21,300,i1",     # 未受理计划却按期次收款
        "bi20,300,i1",     # 成功
        "bi20,300,i1",     # 期次已收讫
    ])
    resp = _import(path)
    assert resp.status_code == 201, resp.text
    reasons = [line["reject_reason"] for line in resp.json()["results"]]
    assert reasons == [
        "installment id is required",
        "payment amount does not match installment amount",
        "installment not found",
        "installment plan not accepted",
        None,
        "installment already paid",
    ]
    order = client.get("/orders/bi20", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 300
    assert len(_payments("bi20")) == 1


def test_same_order_on_multiple_lines_advances_sequentially(tmp_path) -> None:
    _new_order("bi30", 1000)
    path = _write_csv(tmp_path, "sequential.csv", [
        "order_id,amount_cents",
        "bi30,300",
        "bi30,300",
        "bi30,300",
        "bi30,300",
    ])
    body = _import(path).json()
    lines = body["results"]
    assert [line["result"] for line in lines] == ["accepted", "accepted", "accepted", "rejected"]
    assert [line["paid_cents"] for line in lines[:3]] == [300, 600, 900]
    assert lines[3]["reject_reason"] == "payment exceeds outstanding amount"
    assert len(_payments("bi30")) == 3


# ---------- 幂等重放 ----------

def test_resubmit_same_file_and_originator_returns_first_import(tmp_path) -> None:
    _new_order("bi40", 1000)
    path = _write_csv(tmp_path, "idempotent.csv", [
        "order_id,amount_cents",
        "bi40,400",
        "bi40,900",  # 首次即拒绝
    ])
    first = _import(path)
    assert first.status_code == 201
    first_body = first.json()

    second = _import(path)
    assert second.status_code == 201
    assert second.json()["batch_id"] == first_body["batch_id"]
    assert second.json()["results"] == first_body["results"]

    # 不重复记账、不重复留痕
    order = client.get("/orders/bi40", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 400
    assert len(_payments("bi40")) == 1


def test_same_file_different_originator_is_separate_batch_but_not_double_booked(tmp_path) -> None:
    _new_order("bi41", 500)
    path = _write_csv(tmp_path, "originator.csv", ["order_id,amount_cents", "bi41,500"])
    first = _import(path, originator="alice").json()
    second = _import(path, originator="bob")
    assert second.status_code == 201
    assert second.json()["batch_id"] != first["batch_id"]
    # 第二批重放同一收款：账面规则不变，超过未收被拒，不重复留痕
    assert second.json()["results"][0]["reject_reason"] == "payment exceeds outstanding amount"
    records = _payments("bi41")
    assert len(records) == 1 and records[0]["originator"] == "alice"


# ---------- 批次查询 ----------

def test_batch_query_by_id_and_cross_tenant_is_not_found(tmp_path) -> None:
    _new_order("bi50", 1000)
    path = _write_csv(tmp_path, "query.csv", ["order_id,amount_cents", "bi50,100"])
    batch_id = _import(path).json()["batch_id"]

    got = client.get(f"/payment-imports/{batch_id}", headers={"X-Tenant": TENANT})
    assert got.status_code == 200 and got.json()["batch_id"] == batch_id
    assert got.json()["results"][0]["result"] == "accepted"

    # 跨租户查询：按不存在处理，不泄漏批次是否存在
    other = client.get(f"/payment-imports/{batch_id}", headers={"X-Tenant": "tZ"})
    assert other.status_code == 404 and other.json()["detail"] == "import batch not found"
    missing = client.get("/payment-imports/imp_nope", headers={"X-Tenant": TENANT})
    assert missing.status_code == 404


# ---------- 中断续跑 ----------

def test_interrupted_batch_is_queryable_and_resume_only_fills_remaining(tmp_path) -> None:
    _new_order("bi60", 1000)
    _new_order("bi61", 1000)
    _new_order("bi62", 1000)
    path = _write_csv(tmp_path, "resume.csv", [
        "order_id,amount_cents",
        "bi60,100",
        "bi61,200",
        "bi62,300",
        "bi60,9999",  # 将在续跑阶段被拒
    ])

    # 模拟服务在受理两行后中断：批次已建、逐行结论已持久化
    created = import_store.create_import_batch(TENANT, path, "alice")
    import_store.process_pending_lines(TENANT, created["batch_id"], limit=2)

    midway = client.get(f"/payment-imports/{created['batch_id']}", headers={"X-Tenant": TENANT})
    assert midway.status_code == 200
    body = midway.json()
    assert body["status"] == "processing"
    assert [line["result"] for line in body["results"]] == [
        "accepted", "accepted", "pending", "pending",
    ]

    # 重新提交同一文件+发起方：视为同一批次，只补齐剩余行
    resumed = _import(path)
    assert resumed.status_code == 201
    final = resumed.json()
    assert final["batch_id"] == created["batch_id"] and final["status"] == "completed"
    assert [line["result"] for line in final["results"]] == [
        "accepted", "accepted", "accepted", "rejected",
    ]
    assert final["results"][3]["reject_reason"] == "payment exceeds outstanding amount"

    # 已生效的行未重复记账、未重复留痕
    assert len(_payments("bi60")) == 1
    assert client.get("/orders/bi60", headers={"X-Tenant": TENANT}).json()["paid_cents"] == 100
    assert len(_payments("bi61")) == 1
    assert len(_payments("bi62")) == 1

    # 完成后再次提交：仍返回同一结论
    assert _import(path).json()["results"] == final["results"]


# ---------- 文件级错误 ----------

def test_missing_or_malformed_file_is_400(tmp_path) -> None:
    resp = _import(str(tmp_path / "does-not-exist.csv"))
    assert resp.status_code == 400 and resp.json()["detail"] == "import file not found"

    bad_header = _write_csv(tmp_path, "bad_header.csv", ["id,amount", "bi,100"])
    resp = _import(bad_header)
    assert resp.status_code == 400 and resp.json()["detail"].startswith("invalid import file")


def test_import_requires_tenant_header(tmp_path) -> None:
    path = _write_csv(tmp_path, "no_tenant.csv", ["order_id,amount_cents", "x,1"])
    resp = client.post("/payment-imports", json={"file_path": path})
    assert resp.status_code == 400


# ---------- 与冲正/退款互操作 ----------

def test_imported_payments_are_reversible_and_refundable(tmp_path) -> None:
    _new_order("bi70", 1000)
    _new_order("bi71", 1000)
    _register_plan("bi71", PLAN)
    path = _write_csv(tmp_path, "interop.csv", [
        "order_id,amount_cents,installment_id",
        "bi70,400,",
        "bi71,300,i1",
    ])
    lines = _import(path).json()["results"]
    whole_record = lines[0]["record_id"]
    installment_record = lines[1]["record_id"]

    # 整单收款可冲正：账面与从未发生一致
    resp = client.post(f"/orders/bi70/payments/{whole_record}/reversal",
                       headers={"X-Tenant": TENANT, "X-Originator": "ops"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["paid_cents"] == 0 and resp.json()["status"] == "accepted"

    # 分期收款可退款：期次回到未收，账面净额减少
    resp = client.post(f"/orders/bi71/payments/{installment_record}/refund",
                       headers={"X-Tenant": TENANT, "X-Originator": "ops"},
                       json={"amount_cents": 300, "installment_id": "i1"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["paid_cents"] == 0
    plan = {i["installment_id"]: i for i in
            client.get("/orders/bi71/installments", headers={"X-Tenant": TENANT}).json()["installments"]}
    assert plan["i1"]["status"] == "unpaid" and plan["i1"]["paid_record_id"] is None

    # 冲正/退款后再跑同一批次不会补账：批次结论不可变
    again = _import(path).json()
    assert [line["result"] for line in again["results"]] == ["accepted", "accepted"]
