import argparse

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.rules.time_rules import BusinessTimeError, parse_business_time
from app.store import orders
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    # 可选：本次收款的业务发生时间（带时区偏移的完整日期时间，到秒）；缺省由服务按当前时间记账。
    business_time: str | None = None

class ReversalIn(BaseModel):
    reversal_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    # 可选：本次冲正的业务发生时间，取值规则与登记收款一致。
    business_time: str | None = None


def _parse_business_time_or_400(value: str | None):
    # 未提供时返回 None，由存储层按当前时间记账；只有显式给值但非法时才返回 400。
    if value is None:
        return None
    try:
        return parse_business_time(value)
    except BusinessTimeError as error:
        raise HTTPException(status_code=400, detail=str(error))

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
    business_time = _parse_business_time_or_400(body.business_time)
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents, business_time)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/reversals")
def reverse_payment(order_id: str, body: ReversalIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    business_time = _parse_business_time_or_400(body.business_time)
    order, result = orders.reverse_payment(
        x_tenant, order_id, body.reversal_id, body.amount_cents, business_time
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
    x_tenant: str = Header(default=""),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    # 先做全部入参校验，任一不合法都在读取任何订单/流水数据之前以可区分的参数错误返回。
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if start is None:
        raise HTTPException(status_code=400, detail="start is required")
    if end is None:
        raise HTTPException(status_code=400, detail="end is required")
    try:
        start_time = parse_business_time(start)
    except BusinessTimeError:
        raise HTTPException(
            status_code=400,
            detail="start must be a complete date-time with timezone offset, precise to the second",
        )
    try:
        end_time = parse_business_time(end)
    except BusinessTimeError:
        raise HTTPException(
            status_code=400,
            detail="end must be a complete date-time with timezone offset, precise to the second",
        )
    if start_time > end_time:
        raise HTTPException(status_code=400, detail="start must not be later than end")
    flow = orders.list_flow_range(x_tenant, order_id, start_time, end_time)
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
