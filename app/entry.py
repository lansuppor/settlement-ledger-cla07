import argparse
import base64
import json

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.rules import order_rules
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

def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _parse_int_param(name: str, raw: str | None) -> int | None:
    """解析整型查询参数；缺省返回 None，无法解析给出可区分的拒绝原因。"""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _bad_request(f"{name} must be an integer")


def _encode_cursor(after_order_id: str) -> str:
    """续取标记对调用方不透明：base64url(JSON)，只承载稳定排序键。"""
    return base64.urlsafe_b64encode(
        json.dumps({"after_order_id": after_order_id}, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _decode_cursor(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise _bad_request("continuation token is not parseable")
    after = payload.get("after_order_id") if isinstance(payload, dict) else None
    if not isinstance(after, str) or not after:
        raise _bad_request("continuation token is not parseable")
    return after


@app.get("/orders")
def search_orders(
    x_tenant: str = Header(default=""),
    status: str | None = Query(default=None),
    currency: str | None = Query(default=None),
    min_amount_cents: str | None = Query(default=None),
    max_amount_cents: str | None = Query(default=None),
    payment_originator: str | None = Query(default=None),
    has_installment_plan: str | None = Query(default=None),
    page_size: str | None = Query(default=None),
    continuation_token: str | None = Query(default=None),
) -> dict:
    """订单账面条件检索：多条件同时满足，按订单标识升序稳定分页，仅当前租户可见。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")

    if status is not None and status not in (orders.SEARCH_STATUS_SETTLED, orders.SEARCH_STATUS_UNSETTLED):
        raise _bad_request("status must be 'settled' or 'unsettled'")
    if currency is not None:
        if not (len(currency) == 3 and currency.isalpha() and currency.isupper()):
            raise _bad_request("currency must be a 3-letter uppercase code")
        try:
            order_rules.assert_currency(currency)
        except ValueError:
            raise _bad_request("unsupported currency")
    if has_installment_plan is not None and has_installment_plan not in ("true", "false"):
        raise _bad_request("has_installment_plan must be 'true' or 'false'")
    amount_min = _parse_int_param("min_amount_cents", min_amount_cents)
    amount_max = _parse_int_param("max_amount_cents", max_amount_cents)
    if amount_min is not None and amount_min < 0:
        raise _bad_request("min_amount_cents must not be negative")
    if amount_max is not None and amount_max < 0:
        raise _bad_request("max_amount_cents must not be negative")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        # 下限大于上限：可区分的拒绝原因，不改动任何数据
        raise _bad_request("min_amount_cents must not be greater than max_amount_cents")
    size = _parse_int_param("page_size", page_size) if page_size is not None else 50
    if size is None or size <= 0:
        raise _bad_request("page_size must be a positive integer")
    after_order_id = _decode_cursor(continuation_token)

    items, has_more = orders.search_orders(
        x_tenant,
        status=status,
        currency=currency,
        min_amount_cents=amount_min,
        max_amount_cents=amount_max,
        payment_originator=payment_originator,
        has_installment_plan=(has_installment_plan == "true" if has_installment_plan is not None else None),
        page_size=size,
        after_order_id=after_order_id,
    )
    # 还有更多时以本页末单标识签发下一令牌；末页（含无结果）令牌为空
    next_token = _encode_cursor(items[-1]["order_id"]) if has_more and items else ""
    return {"orders": items, "continuation_token": next_token}


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
