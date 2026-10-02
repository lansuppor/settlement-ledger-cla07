# 收款登记请求级幂等

登记收款时可声明一个调用方自己生成的 **幂等键**（`idempotency_key`），用于消除网络重试与并发重放导致的重复入账。

- 幂等键在**租户内**唯一标识一次收款意图，作用域为（`X-Tenant`, `idempotency_key`）。
- 幂等键由**调用方提供**并在重试间保持稳定；收款流水标识 `record_id` 由服务在入账成功后给出。两者相互独立。
- 未声明幂等键的收款登记行为完全不变。

## 调用方式

入口不变：`POST /orders/{order_id}/payments`，在请求体中增加可选字段 `idempotency_key`（非空字符串）。

```bash
# 首次请求：键随请求一起声明
curl -s -XPOST localhost:8000/orders/o1/payments \
  -H 'X-Tenant: t1' -H 'X-Originator: alice' \
  -H 'Content-Type: application/json' \
  -d '{"amount_cents": 400, "idempotency_key": "biz-receipt-20261003-0001"}'
# 200
# {"order_id":"o1","amount_cents":1000,"currency":"CNY",
#  "paid_cents":400,"outstanding_cents":600,"status":"accepted",
#  "record_id":"pay_ab12cd34..."}

# 超时/出错后用同一个键原样重发任意次：返回与首次完全一致的结果，不重复入账
```

## 行为约定

| 场景 | 结果 |
| --- | --- |
| 首次成功 | 正常校验入账，写一条 `payment` 流水；响应为订单账面快照并附带首次 `record_id` |
| 同键相同请求重放（含并发） | 200，返回首次的账面快照与首次 `record_id`；不重复入账、不重复写流水 |
| 同键但金额、期次或订单不同 | 409 `idempotency key conflict`，不改动任何数据 |
| 同键指向不同订单 | 409 `idempotency key conflict`，不改动任何数据 |
| 请求被既有校验拒绝（超额、期次已收讫等） | 返回各自既有的 409 原因；不占用幂等键、不留痕，改对参数后同键可成功 |
| 订单不存在（含跨租户） | 404 `order not found`；不占用幂等键 |
| 跨租户使用相同键 | 互不影响，各自独立判定 |
| 不带 `idempotency_key` | 现有行为不变（响应不附带 `record_id`） |

说明：

- 重放返回的是**首次成功那一刻**的账面快照（`paid_cents`/`outstanding_cents`/`status`），即使首次收款后来被冲正或退款；当前账面以 `GET /orders/{order_id}` 为准。
- 幂等键不绕过任何既有规则：分期订单仍必须声明 `installment_id`、金额必须等于该期应收、同一期次至多收讫一笔。
- 冲正、退款各有自己的重放语义（见 README），与收款幂等键无关；流水查询仍按发生顺序返回收款、冲正、退款，因果链可据此重建。
