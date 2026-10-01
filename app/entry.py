import argparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, refunds, settlements
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    refund_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class SettlementIn(BaseModel):
    settlement_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

def _require_tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant

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
    tenant = _require_tenant(x_tenant)
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        order = orders.add_payment(tenant, order_id, body.amount_cents)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/refunds", status_code=201)
def accept_refund(body: RefundIn, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        result, refund = refunds.accept(tenant, body.refund_id, body.order_id, body.amount_cents)
    except refunds.OrderNotSettled:
        # 未收金额不为 0：订单尚未结清
        raise HTTPException(status_code=409, detail="order is not settled")
    except refunds.RefundExceedsBalance:
        # 超过 可退余额 = 订单金额 − 累计已退金额
        raise HTTPException(status_code=409, detail="refund exceeds refundable amount")
    if result == "order_not_found":
        # 订单不存在或属于其他租户，一律按不存在处理
        raise HTTPException(status_code=404, detail="order not found")
    if result == "duplicate":
        # 幂等：重复受理命中首次受理结论，不二次入账
        raise HTTPException(status_code=409, detail="refund already accepted")
    if result == "already_reversed":
        # 已冲正的退款单标识不得再次受理
        raise HTTPException(status_code=409, detail="refund already reversed")
    return refund

@app.get("/refunds/{refund_id}")
def read_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    refund = refunds.get(tenant, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/reverse")
def reverse_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        refund = refunds.reverse(tenant, refund_id)
    except refunds.RefundAlreadyReversed:
        raise HTTPException(status_code=409, detail="refund already reversed")
    if refund is None:
        # 标识未受理或属于其他租户，一律按不存在处理
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

# ---------- 结算单（独立单据，租户同样通过请求头 X-Tenant 声明） ----------

@app.post("/settlements", status_code=201)
def accept_settlement(body: SettlementIn, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        result, settlement = settlements.accept(
            tenant, body.settlement_id, body.order_id, body.amount_cents
        )
    except settlements.OrderNotSettled:
        # 未收金额不为 0：订单尚未结清
        raise HTTPException(status_code=409, detail="order is not settled")
    except settlements.OrderHasRefunds:
        # 累计已退金额不为 0：订单已发生退款
        raise HTTPException(status_code=409, detail="order has refunds")
    except settlements.SettlementExceedsBalance:
        # 超过 剩余可结算金额 = 订单金额 − 已受理结算金额之和（不含已冲正部分）
        raise HTTPException(status_code=409, detail="settlement exceeds settleable amount")
    if result == "order_not_found":
        # 订单不存在或属于其他租户，一律按不存在处理
        raise HTTPException(status_code=404, detail="order not found")
    if result == "duplicate":
        # 幂等：重复受理命中首次受理结论，不二次入账
        raise HTTPException(status_code=409, detail="settlement already accepted")
    if result == "already_reversed":
        # 已冲正的结算单标识不得再次受理
        raise HTTPException(status_code=409, detail="settlement already reversed")
    return settlement

@app.get("/settlements/{settlement_id}")
def read_settlement(settlement_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    settlement = settlements.get(tenant, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="settlement not found")
    return settlement

@app.post("/settlements/{settlement_id}/payments")
def add_settlement_payment(
    settlement_id: str, body: PaymentIn, x_tenant: str = Header(default="")
) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        settlement = settlements.add_payment(tenant, settlement_id, body.amount_cents)
    except settlements.PaymentExceedsUnreceived:
        # 累计收款不得超过结算金额
        raise HTTPException(status_code=409, detail="payment exceeds unreceived amount")
    except settlements.SettlementAlreadyReversed:
        raise HTTPException(status_code=409, detail="settlement already reversed")
    if settlement is None:
        # 标识未受理或属于其他租户，一律按不存在处理
        raise HTTPException(status_code=404, detail="settlement not found")
    return settlement

@app.post("/settlements/{settlement_id}/reverse")
def reverse_settlement(settlement_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        settlement = settlements.reverse(tenant, settlement_id)
    except settlements.SettlementAlreadyReversed:
        raise HTTPException(status_code=409, detail="settlement already reversed")
    except settlements.SettlementHasPayments:
        # 存在任何收款时不允许冲正
        raise HTTPException(status_code=409, detail="settlement has payments")
    if settlement is None:
        # 标识未受理或属于其他租户，一律按不存在处理
        raise HTTPException(status_code=404, detail="settlement not found")
    return settlement

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
