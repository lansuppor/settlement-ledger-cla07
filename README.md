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

- `GET /orders`：订单账面条件检索与稳定分页（收款受理查询入口）。租户通过请求头 `X-Tenant` 传入，只在当前租户内生效，任何条件下都不返回其他租户订单、不泄漏对象是否存在。查询参数全部可选，条件为空时返回全部订单，多个条件同时给定时按同时满足（AND）处理：
  - `status`：订单状态，`accepted`=未结清（未收金额为正）、`settled`=已结清（未收金额为零）；按查询当下账面判定，退款或冲正使未收转正后自然回到未结清。
  - `currency`：按币种精确过滤（如 `CNY`）。
  - `amount_min` / `amount_max`：按订单应收金额闭区间过滤（非负整数，下限不得大于上限）。
  - `originator`：发起过收款的收款流水发起方标识（存在该发起方的 `payment` 流水即命中；冲正/退款不改变“曾发起”事实）。
  - `has_installment`：是否受理过分期计划，`true` / `false`。
  - `page_size`：单页条数，正整数，缺省 20。
  - `cursor`：上一页响应给出的续取标记（不透明字符串，缺省取首页）。
  - 响应 `200`：`{"orders": [...], "next_cursor": "..."}`；结果按 `order_id` 升序，每条含 `order_id`、`amount_cents`（订单金额）、`paid_cents`（已收）、`outstanding_cents`（未收）、`currency`、`status`。以订单标识为稳定排序键，翻页过程中新增或变更的单据不会造成已返回订单重复返回、也不会跳过符合条件的订单；无结果或已是末页时返回空列表与空的 `next_cursor`。
  - 参数不合法返回 `400` 且可区分：金额下限大于上限（`amount_min must not be greater than amount_max`）、`page_size` 非正或非整数（`page_size must be a positive integer`）、续取标记无法解析（`cursor is not parseable`）、状态取值非法（`unsupported status filter`）、金额边界非非负整数、`has_installment` 非 `true/false`；任一拒绝都不改动任何数据。缺少租户头返回 `400`（`tenant header is required`）。
- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`，同时写入一条类型为 `payment` 的收款流水（发起方可由请求头 `X-Originator` 声明，缺省取租户标识）。
- `GET /orders/{order_id}/payments`：收款流水查询。按发生顺序返回该订单的全部收款、冲正与退款记录；每条记录含 `record_id`（业务流水标识）、`record_type`（`payment` 收款 / `reversal` 冲正 / `refund` 退款）、`amount_cents`、`created_at`、`originator`（发起方标识），冲正与退款记录另含 `related_record_id` 指向原收款流水（退款记录另含 `installment_id`，整单退款为 `null`）。订单不存在或跨租户一律返回 404，不泄漏订单是否存在。
- `POST /orders/{order_id}/payments/{record_id}/reversal`：冲正一笔已登记的收款。冲正金额等于原收款金额（不支持部分冲正），成功返回 201 与冲正后的订单对象，并写入一条 `related_record_id` 关联原流水的 `reversal` 记录。订单不存在返回 404（跨租户同样按不存在处理）；流水不存在或不属于该订单返回 404（`payment record not found`）；重复冲正返回 409（`payment already reversed`）；冲正后账面不合法返回 409。任一失败都不改动已收金额、状态与流水。若原收款是分期收款，冲正会连同期次一起退回未收，该期可再次收讫。
- `POST /orders/{order_id}/payments/{record_id}/refund`：对一笔已登记的收款登记退款（支持部分退款）。请求字段 `amount_cents`，分期收款必须同时声明 `installment_id`。成功返回 201 与退款后的订单对象，并写入一条 `related_record_id` 关联原收款流水的 `refund` 记录。退款后订单已收金额按净收款（收款−冲正−退款）相应减少，未收金额按“订单金额−净收款”重算：未收转为正数的订单不再结清，净收款全部退完的订单回到未收款状态。失败原因可区分：订单不存在或跨租户返回 404（`order not found`，不泄漏对象是否存在）；原收款流水不存在或不属于该订单/租户返回 404（`payment record not found`）；金额非正返回 409（`refund amount must be positive`）；超过该订单可退余额（当前净收款）或该笔收款剩余可退额返回 409（`refund exceeds refundable amount`）；分期退款未声明期次 409（`installment id is required`）、期次不存在 409（`installment not found`）、该期未收讫 409（`installment is not paid`）、退款金额不等于该期已收金额 409（`refund amount does not match installment amount`）；整单退款声明期次返回 409（`installment plan not accepted`）；同一退款请求（同订单、同原收款、同金额、同期次）重放返回 409（`refund already applied`）。任一失败都不改动已收、未收、状态、期次状态与流水。分期退款成功后该期回到未收并清空 `paid_record_id`，可再次收讫。
- `POST /orders/{order_id}/installments`：受理分期计划。请求字段 `installments` 为若干期明细，每期含 `installment_id`（期次标识）、`amount_cents`（应收金额）、`due_at`（到期时间）。成功返回 201 与完整计划；各期金额之和必须等于订单金额、期次标识不得重复、金额必须为正，任一不合法整份拒绝且不留任何一期（金额求和不符 409 `installment amounts do not add up to order amount`，期次重复 409 `duplicate installment id`，非正金额 422）；同一订单重复受理返回 409（`installment plan already accepted`）且不改动已受理计划；订单不存在或跨租户返回 404。
- `GET /orders/{order_id}/installments`：分期计划查询。返回该订单各期的 `installment_id`、`amount_cents`、`due_at`、`status`（`unpaid` / `paid`）与 `paid_record_id`（收讫该期的收款流水标识）；未受理计划时返回空列表；订单不存在或跨租户返回 404。
- 分期订单的收款：受理分期计划后，`POST /orders/{order_id}/payments` 必须声明 `installment_id`，且 `amount_cents` 必须等于该期应收。失败原因可区分：未声明期次 409（`installment id is required`）、金额不等 409（`payment amount does not match installment amount`）、期次不存在 409（`installment not found`）、期次已收讫 409（`installment already paid`）、未受理计划却按期次收款 409（`installment plan not accepted`）；任一失败都不改动账面与留痕。同一期次并发收款至多一笔成功。全部期次收讫时订单结清。收款流水的 `installment_id` 字段记录对应期次（整单收款为 `null`）。
- `POST /payments/import`：批量收款导入。请求字段 `file_path`（CSV 文件路径）与可选 `originator`（发起方标识，缺省取 `X-Originator` 头，再缺省取租户标识，与单笔收款一致）。CSV 每行给出 `order_id,amount_cents[,installment_id]`（首行可为表头）。导入按文件行序逐行受理，每行的校验与账面推进完全等同于单笔收款；任一行不合法只拒绝该行（含 `invalid line`、`order not found` 及单笔收款的全部可区分原因），不影响同批其他行，也不留部分效果。响应 200 含 `batch_id`（导入批次标识）与 `results` 逐行结论：成功行给出该订单受理后的 `paid_cents`、`outstanding_cents`、`order_status` 与收款流水 `record_id`（流水与单笔收款同构，可正常冲正与退款），失败行给出 `reason`。同一（租户, 文件路径, 发起方标识）重复提交视为同一批次：已完成的批次直接返回首次结论，不重复记账、不重复留痕；中途宕机的批次续跑同一文件只补齐缺失行。文件不可读返回 400；跨租户订单一律按 `order not found` 处理，不泄漏对象是否存在。
- `GET /payments/import/{batch_id}`：凭批次标识查询导入的逐行结论（含中断批次的已完成行，`status` 为 `running` / `completed`）。批次不存在或跨租户返回 404，不泄漏批次是否存在。
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

# 对一笔收款退款（支持部分退款；金额为正且不超过可退余额）
curl -s -XPOST localhost:8000/orders/o1/payments/pay_ab12.../refund \
  -H 'X-Tenant: t1' -H 'X-Originator: alice' \
  -H 'Content-Type: application/json' -d '{"amount_cents": 100}'

# 分期收款的退款必须声明对应期次，且只能退该期已收金额
curl -s -XPOST localhost:8000/orders/o2/payments/pay_cd34.../refund \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"amount_cents": 300, "installment_id": "i1"}'

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

# 批量收款导入：CSV 每行 order_id,amount_cents[,installment_id]
# 响应含 batch_id 与逐行结论；同（文件路径, 发起方）重复提交返回首次结论，不重复记账
curl -s -XPOST localhost:8000/payments/import \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"file_path": "fixtures/payments.csv", "originator": "erp"}'

# 凭批次标识查询逐行结论（含中断批次的已完成行）
curl -s localhost:8000/payments/import/imp_ab12... -H 'X-Tenant: t1'

# 订单账面条件检索：无条件取当前租户全部订单（首页，默认每页 20 条）
curl -s 'localhost:8000/orders?page_size=2' -H 'X-Tenant: t1'

# 多条件同时满足：未结清 + 币种 + 应收区间 + 某发起方发起过收款 + 未受理分期
curl -s 'localhost:8000/orders?status=accepted&currency=CNY&amount_min=100&amount_max=1000&originator=alice&has_installment=false' \
  -H 'X-Tenant: t1'

# 用上一页响应中的 next_cursor 稳定续取下一页（过滤条件原样带上）
curl -s 'localhost:8000/orders?page_size=2&cursor=<上一页的next_cursor>' -H 'X-Tenant: t1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 冲正仅支持整笔撤销，不支持部分冲正。
