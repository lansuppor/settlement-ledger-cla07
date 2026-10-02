import argparse

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.rules import order_rules, search_rules
from app.store import imports, orders
from app.store.db import connect, migrate
from app.store.imports import ImportFileError
from app.store.orders import LedgerError

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    installment_id: str | None = Field(default=None, min_length=1)

class RefundIn(BaseModel):
    # 金额为正的业务校验在账本内完成，非正金额返回 409 的可区分原因
    amount_cents: int
    installment_id: str | None = Field(default=None, min_length=1)

class InstallmentIn(BaseModel):
    installment_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    due_at: str = Field(min_length=1)

class PlanIn(BaseModel):
    installments: list[InstallmentIn] = Field(min_length=1)

class PaymentImportIn(BaseModel):
    file_path: str = Field(min_length=1)
    originator: str | None = Field(default=None, min_length=1)

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)

@app.get("/orders")
def search_orders(
    x_tenant: str = Header(default=""),
    status: str | None = Query(default=None),
    currency: str | None = Query(default=None),
    amount_min: str | None = Query(default=None),
    amount_max: str | None = Query(default=None),
    originator: str | None = Query(default=None),
    has_installment: str | None = Query(default=None),
    page_size: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> dict:
    """订单账面条件检索：当前租户内按条件过滤，以订单标识为稳定键分页返回账面快照。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        params = search_rules.normalize_query(
            status=status,
            currency=currency,
            amount_min=amount_min,
            amount_max=amount_max,
            originator=originator,
            has_installment=has_installment,
            page_size=page_size,
        )
        after = search_rules.decode_cursor(cursor) if cursor else None
    except search_rules.SearchParamsError as error:
        # 条件不合法：可区分的拒绝原因，且不改动任何数据
        raise HTTPException(status_code=400, detail=str(error))

    page, has_more = orders.search_orders(
        x_tenant,
        status=params["status"],
        currency=params["currency"],
        amount_min=params["amount_min"],
        amount_max=params["amount_max"],
        originator=params["originator"],
        has_installment=params["has_installment"],
        page_size=params["page_size"],
        after_order_id=after,
    )
    # 仅在确有下一页时给出续取标记（指向本页末尾订单）；末页与无结果均为空标记
    next_cursor = search_rules.encode_cursor(page[-1]["order_id"]) if has_more and page else ""
    return {"orders": page, "next_cursor": next_cursor}

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default=""), x_originator: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    originator = x_originator or x_tenant
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents, originator, installment_id=body.installment_id)
    except LedgerError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/installments", status_code=201)
def register_plan(order_id: str, body: PlanIn, x_tenant: str = Header(default="")) -> dict:
    """受理分期计划：整份校验、整份落库；同一订单只能受理一次。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    items = [item.model_dump() for item in body.installments]
    try:
        plan = orders.register_plan(x_tenant, order_id, items)
    except LedgerError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if plan is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "installments": plan}

@app.get("/orders/{order_id}/installments")
def list_installments(order_id: str, x_tenant: str = Header(default="")) -> dict:
    """分期计划查询：返回各期应收、到期时间与收讫状态。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    plan = orders.list_installments(x_tenant, order_id)
    if plan is None:
        # 订单不存在或跨租户，一律按不存在处理
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "installments": plan}

@app.get("/orders/{order_id}/payments")
def list_payments(order_id: str, x_tenant: str = Header(default="")) -> dict:
    """收款流水查询：按发生顺序返回该订单的全部收款与冲正。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    records = orders.list_records(x_tenant, order_id)
    if records is None:
        # 订单不存在或跨租户，一律按不存在处理
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "records": records}

@app.post("/orders/{order_id}/payments/{record_id}/reversal", status_code=201)
def reverse_payment(order_id: str, record_id: str, x_tenant: str = Header(default=""), x_originator: str = Header(default="")) -> dict:
    """冲正一笔已登记的收款。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    originator = x_originator or x_tenant
    try:
        order = orders.reverse_payment(x_tenant, order_id, record_id, originator)
    except LedgerError as error:
        detail = str(error)
        if detail == "payment record not found":
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=409, detail=detail)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments/{record_id}/refund", status_code=201)
def refund_payment(order_id: str, record_id: str, body: RefundIn, x_tenant: str = Header(default=""), x_originator: str = Header(default="")) -> dict:
    """对一笔已登记的收款登记退款（支持部分退款）。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    originator = x_originator or x_tenant
    try:
        order = orders.refund_payment(
            x_tenant, order_id, record_id, body.amount_cents, originator,
            installment_id=body.installment_id,
        )
    except LedgerError as error:
        detail = str(error)
        if detail == "payment record not found":
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=409, detail=detail)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/payments/import")
def import_payment_batch(body: PaymentImportIn, x_tenant: str = Header(default=""), x_originator: str = Header(default="")) -> dict:
    """批量收款导入：按 CSV 文件逐行受理，部分失败只拒绝该行，结果含批次标识与逐行结论。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    # 发起方缺省规则与单笔收款一致：未声明时取租户标识
    originator = body.originator or x_originator or x_tenant
    try:
        return imports.import_payments(x_tenant, body.file_path, originator)
    except ImportFileError as error:
        raise HTTPException(status_code=400, detail=str(error))

@app.get("/payments/import/{batch_id}")
def read_import_batch(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    """凭批次标识查询导入的逐行结论；批次不存在或跨租户返回 404。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    batch = imports.get_batch(x_tenant, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="import batch not found")
    return batch

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
