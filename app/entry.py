import argparse
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from app.config import tenant_header
from app.store import orders
from app.store.db import connect, migrate
from app.rules import order_rules

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    occurred_at: str | None = None

class ReversalIn(BaseModel):
    reversal_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    occurred_at: str | None = None

def _business_time(value: str | None) -> str | None:
    """校验调用方提供的业务发生时间；未提供时返回 None（由服务按当前时间记账）。"""
    if value is None:
        return None
    try:
        return order_rules.format_business_time(order_rules.parse_business_time(value))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None

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
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents, _business_time(body.occurred_at))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/reversals")
def reverse_payment(order_id: str, body: ReversalIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order, result = orders.reverse_payment(
        x_tenant, order_id, body.reversal_id, body.amount_cents, _business_time(body.occurred_at)
    )
    if result == "not_found":
        raise HTTPException(status_code=404, detail="order not found")
    if result == "conflict":
        raise HTTPException(status_code=409, detail="reversal conflicts with current ledger state")
    return order

@app.get("/orders/{order_id}/flow")
def read_order_flow(order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    flow = orders.list_flow(x_tenant, order_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "entries": flow}

@app.get("/orders/{order_id}/flow/range")
def read_order_flow_range(
    order_id: str,
    start: str | None = None,
    end: str | None = None,
    x_tenant: str = Header(default=""),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    # 先校验时间范围，无效时不读取任何数据。
    if start is None or end is None:
        raise HTTPException(status_code=400, detail="start and end query parameters are required")
    try:
        start_at = order_rules.parse_business_time(start)
        end_at = order_rules.parse_business_time(end)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    if start_at > end_at:
        raise HTTPException(status_code=400, detail="start must not be later than end")
    flow = orders.list_flow_range(x_tenant, order_id, start_at, end_at)
    if flow is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "entries": flow}

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
