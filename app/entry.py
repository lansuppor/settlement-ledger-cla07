import argparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
from app.store.db import connect, migrate
from app.store.orders import LedgerError

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    installment_id: str | None = None

class InstallmentIn(BaseModel):
    installment_id: str = Field(min_length=1)
    amount_cents: int
    due_at: str = Field(min_length=1)

class PlanIn(BaseModel):
    installments: list[InstallmentIn]

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
        order = orders.add_payment(x_tenant, order_id, body.amount_cents, originator, body.installment_id)
    except LedgerError as error:
        detail = str(error)
        if detail == orders.INSTALLMENT_NOT_FOUND:
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=409, detail=detail)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/installments", status_code=201)
def register_installments(order_id: str, body: PlanIn, x_tenant: str = Header(default="")) -> dict:
    """受理订单的分期计划：各期应收之和必须等于订单金额，整份计划原子落库。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        plan = orders.register_plan(x_tenant, order_id, [item.model_dump() for item in body.installments])
    except LedgerError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if plan is None:
        raise HTTPException(status_code=404, detail="order not found")
    return plan

@app.get("/orders/{order_id}/installments")
def read_installments(order_id: str, x_tenant: str = Header(default="")) -> dict:
    """查询订单的分期计划与每期收讫状态；跨租户一律按不存在处理。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    plan = orders.get_plan(x_tenant, order_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="order not found")
    if not plan["installments"]:
        raise HTTPException(status_code=404, detail="installment plan not found")
    return plan

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
