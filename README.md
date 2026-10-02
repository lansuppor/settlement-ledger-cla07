# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`，同时写入一条类型为 `payment` 的收款流水（发起方可由请求头 `X-Originator` 声明，缺省取租户标识）。
- `GET /orders/{order_id}/payments`：收款流水查询。按发生顺序返回该订单的全部收款与冲正记录；每条记录含 `record_id`（业务流水标识）、`record_type`（`payment` 收款 / `reversal` 冲正）、`amount_cents`、`created_at`、`originator`（发起方标识），冲正记录另含 `related_record_id` 指向原收款流水。订单不存在或跨租户一律返回 404，不泄漏订单是否存在。
- `POST /orders/{order_id}/payments/{record_id}/reversal`：冲正一笔已登记的收款。冲正金额等于原收款金额（不支持部分冲正），成功返回 201 与冲正后的订单对象，并写入一条 `related_record_id` 关联原流水的 `reversal` 记录。订单不存在返回 404（跨租户同样按不存在处理）；流水不存在或不属于该订单返回 404（`payment record not found`）；重复冲正返回 409（`payment already reversed`）；冲正后账面不合法返回 409。任一失败都不改动已收金额、状态与流水。若原收款是分期收款，冲正会连同期次一起退回未收，该期可再次收讫。
- `POST /orders/{order_id}/installments`：受理分期计划。请求字段 `installments` 为若干期明细，每期含 `installment_id`（期次标识）、`amount_cents`（应收金额）、`due_at`（到期时间）。成功返回 201 与完整计划；各期金额之和必须等于订单金额、期次标识不得重复、金额必须为正，任一不合法整份拒绝且不留任何一期（金额求和不符 409 `installment amounts do not add up to order amount`，期次重复 409 `duplicate installment id`，非正金额 422）；同一订单重复受理返回 409（`installment plan already accepted`）且不改动已受理计划；订单不存在或跨租户返回 404。
- `GET /orders/{order_id}/installments`：分期计划查询。返回该订单各期的 `installment_id`、`amount_cents`、`due_at`、`status`（`unpaid` / `paid`）与 `paid_record_id`（收讫该期的收款流水标识）；未受理计划时返回空列表；订单不存在或跨租户返回 404。
- 分期订单的收款：受理分期计划后，`POST /orders/{order_id}/payments` 必须声明 `installment_id`，且 `amount_cents` 必须等于该期应收。失败原因可区分：未声明期次 409（`installment id is required`）、金额不等 409（`payment amount does not match installment amount`）、期次不存在 409（`installment not found`）、期次已收讫 409（`installment already paid`）、未受理计划却按期次收款 409（`installment plan not accepted`）；任一失败都不改动账面与留痕。同一期次并发收款至多一笔成功。全部期次收讫时订单结清。收款流水的 `installment_id` 字段记录对应期次（整单收款为 `null`）。
- `GET /health`：返回服务与数据库状态。

### 调用示例

```bash
# 登记收款（响应仍是订单对象；新流水通过流水查询获取 record_id）
curl -s -XPOST localhost:8000/orders/o1/payments \
  -H 'X-Tenant: t1' -H 'X-Originator: alice' \
  -H 'Content-Type: application/json' -d '{"amount_cents": 400}'

# 查询该订单的全部收款/冲正流水
curl -s localhost:8000/orders/o1/payments -H 'X-Tenant: t1'

# 用上一步得到的 record_id 冲正该笔收款
curl -s -XPOST localhost:8000/orders/o1/payments/pay_ab12.../reversal \
  -H 'X-Tenant: t1' -H 'X-Originator: alice'

# 受理分期计划（各期金额之和须等于订单金额）
curl -s -XPOST localhost:8000/orders/o2/installments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"installments": [
        {"installment_id": "i1", "amount_cents": 300, "due_at": "2026-11-01T00:00:00Z"},
        {"installment_id": "i2", "amount_cents": 700, "due_at": "2026-12-01T00:00:00Z"}
      ]}'

# 按期次收款（金额须等于该期应收）
curl -s -XPOST localhost:8000/orders/o2/payments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"amount_cents": 300, "installment_id": "i1"}'

# 查询分期计划与各期收讫状态
curl -s localhost:8000/orders/o2/installments -H 'X-Tenant: t1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 冲正仅支持整笔撤销，不支持部分冲正。
