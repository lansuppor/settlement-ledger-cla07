# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、订单收款分期登记（逐笔流水）、按流水逐笔冲正、按条件检索收款流水，受理/读取/冲正退款单，以及受理/读取/分期收款/冲正结算单；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`（首次启动自动执行迁移）
- 仅执行数据库迁移：`python3 -m app.entry --migrate`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

订单与收款：

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。返回对象额外暴露：
  - `outstanding_cents`：未收金额 = `amount_cents − paid_cents`。
  - `payments`：该订单全部收款流水（含已冲正），按登记顺序排列；每条记录 `payment_id`、`amount_cents`、`status`（`accepted` 未冲正 / `reversed` 已冲正）、`created_at`（登记时刻）、`reversed_at`。
  - `refunded_cents` / `refundable_cents`：累计已退金额 / 当前可退余额，两者之和等于 `amount_cents`。
  - `settled_cents` / `settleable_cents`：累计已结算金额 / 剩余可结算金额，两者之和等于 `amount_cents`。
- `POST /orders/{order_id}/payments`：分期登记一笔订单收款。请求字段 `amount_cents`（最小货币单位整数，> 0）与可选 `idempotency_key`（超时重试用，同键只命中同一次结论）。累计已收超过订单金额返回 `409 payment exceeds outstanding amount`，且不改动订单与既有流水；订单不存在或跨租户返回 `404 order not found`。成功返回 200 与新生成的收款流水对象（含 `payment_id`、登记时刻）。
- `GET /payments/{payment_id}`：按流水标识读取收款流水。不存在或跨租户返回 `404 payment not found`。
- `POST /payments/{payment_id}/reverse`：逐笔冲正收款流水。仅未冲正流水可冲正，冲正把该笔金额从累计已收中整体退回并标记为 `reversed`，成功返回 200 与该流水；对不存在的流水标识（含跨租户）返回 `404 payment not found`；对已冲正流水重复冲正返回 `409 payment already reversed`。两种拒绝原因可区分，均不改动订单与任何流水。
- `GET /payments`：按租户（请求头）组合筛选本租户收款流水。查询参数（均可组合、均可省略）：
  - `order_id`：订单标识；`min_amount_cents` / `max_amount_cents`：金额闭区间（正整数）。
  - `created_from` / `created_to`：登记时间闭区间（ISO-8601，如 `2026-10-01T10:00:00Z`）。
  - `reversed_only`：`true` 仅已冲正、`false` 仅未冲正、省略则两者都返回。
  - `limit`（默认 50，1–200）与 `cursor`（上一页响应的 `next_cursor`）：键集分页。
  - 响应 `{"items":[流水...], "next_cursor": 整数或 null}`，结果按登记顺序稳定排列；不重不漏，同一查询重复执行结果一致。跨租户检索恒为空。
- `GET /health`：返回服务与数据库状态。

退款单（独立单据，租户同样通过请求头 `X-Tenant` 声明）：

- `POST /refunds`：受理退款单。请求字段 `refund_id`、`order_id`、`amount_cents`（最小货币单位整数，> 0）。成功返回 201 与退款单对象（状态 `accepted`）。
  - 原订单不存在或属于其他租户：`404 order not found`（两者不可区分）。
  - 订单未结清（未收金额 > 0）：`409 order is not settled`。
  - 订单曾结清但因收款流水被冲正使未收重新大于 0：`409 order reopened by payment reversal`（与上一条可区分）。
  - 退款金额超过可退余额（订单金额 − 累计已退金额）：`409 refund exceeds refundable amount`。
  - 同一（租户，退款单标识）重复受理：`409 refund already accepted`，命中首次受理结论，不二次入账、不改变首次单据。
  - 该标识此前已冲正：`409 refund already reversed`，冲正后同一标识不得再次受理。
- `GET /refunds/{refund_id}`：读取退款单。不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/reverse`：冲正退款单。仅 `accepted` 可冲正，成功返回 200 与状态 `reversed` 的退款单；未受理标识（含跨租户）返回 `404 refund not found`，重复冲正返回 `409 refund already reversed`。

结算单（独立单据，租户同样通过请求头 `X-Tenant` 声明）：

- `POST /settlements`：受理结算单。请求字段 `settlement_id`、`order_id`、`amount_cents`（最小货币单位整数，> 0）。成功返回 201 与结算单对象（状态 `accepted`，`received_cents` 为累计已收，`unreceived_cents` 为未收金额）。
  - 原订单不存在或属于其他租户：`404 order not found`（两者不可区分）。
  - 订单未结清（未收金额 > 0）：`409 order is not settled`；曾结清但因收款冲正使未收重新大于 0：`409 order reopened by payment reversal`（可区分）。
  - 订单已发生退款（累计已退金额不为 0）：`409 order has refunds`。
  - 结算金额超过剩余可结算金额（订单金额 − 已受理结算金额之和，不含已冲正部分）：`409 settlement exceeds settleable amount`。
  - 同一（租户，结算单标识）重复受理：`409 settlement already accepted`，命中首次受理结论，不二次入账、不改变首次单据。
  - 该标识此前已冲正：`409 settlement already reversed`，冲正后同一标识不得再次受理。
- `GET /settlements/{settlement_id}`：读取结算单，暴露 `received_cents`（累计已收）与 `unreceived_cents`（未收 = 结算金额 − 累计已收）。不存在或跨租户返回 `404 settlement not found`。
- `POST /settlements/{settlement_id}/payments`：分期收款。请求字段 `amount_cents`（> 0）；累计收款超过结算金额返回 `409 payment exceeds unreceived amount` 且不改动结算单；成功返回 200 与结算单对象。结算单不存在或跨租户返回 `404 settlement not found`；已冲正返回 `409 settlement already reversed`。
- `POST /settlements/{settlement_id}/reverse`：冲正结算单。仅 `accepted` 且累计已收为 0 可冲正，成功返回 200 与状态 `reversed` 的结算单，并整体释放其占用的订单剩余结算金额。未受理标识（含跨租户）返回 `404 settlement not found`；已存在收款返回 `409 settlement has payments`；重复冲正返回 `409 settlement already reversed`。

退款链路的订单守恒：

- `refunded_cents`：累计已退金额（不含已冲正部分）。
- `refundable_cents`：当前可退余额 = `amount_cents − refunded_cents`。
- 守恒关系：`refunded_cents + refundable_cents = amount_cents`；退款不改写 `paid_cents` 与收款流水，`outstanding_cents` 保持为 0。

订单收款分期与冲正：

- 收款以逐笔流水登记：`paid_cents` = 该订单全部未冲正流水金额之和；未收 = `amount_cents − paid_cents`，与订单金额始终闭合。
- 逐笔冲正只整体退回该笔流水的金额并把该流水标记为 `reversed`；已冲正流水不再计入 `paid_cents`，也不能再次冲正。
- 冲正使未收重新大于 0 后，该订单不得再受理退款或结算单（`409 order reopened by payment reversal`），不留部分生效数据；此前已受理的退款单、结算单及其收款、冲正行为与结论保持不变。
- 收款链路不改写退款链路的 `refunded_cents`、退款单，也不改写结算链路的 `settled_cents`、`settleable_cents`、结算单及其收款数据。

结算链路的订单守恒：

- `settled_cents`：累计已结算金额（各已受理结算单金额之和，已冲正部分整体释放）。
- `settleable_cents`：剩余可结算金额 = `amount_cents − settled_cents`。
- 守恒关系：`settled_cents + settleable_cents = amount_cents`；结算链路不改写订单的 `paid_cents`、收款流水、`refunded_cents` 与退款链路的任何数据。结算单的分期收款只累加到结算单自身的 `received_cents`，不改变订单已收金额。

### 调用示例

```bash
T="-H X-Tenant:t1"
# 1. 建单，分两期登记收款（每次返回独立 payment_id；首期带幂等键）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o-1","amount_cents":500,"currency":"CNY"}'
curl -s -X POST localhost:8000/orders/o-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":200,"idempotency_key":"pay-o1-1"}'
curl -s -X POST localhost:8000/orders/o-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":300}'
# 2. 读取订单：paid_cents=500、outstanding_cents=0，payments 含两笔流水（含 payment_id 与 created_at）
curl -s localhost:8000/orders/o-1 $T
# 3. 按条件组合检索流水（订单 + 金额闭区间 + 分页）
curl -s "localhost:8000/payments?order_id=o-1&min_amount_cents=100&max_amount_cents=300&limit=10" $T
# 4. 冲正第一期收款：未收重新变为 200（PID 为第 1 步返回的 payment_id）
curl -s -X POST localhost:8000/payments/$PID/reverse $T
#    此后再受理退款/结算 => 409 order reopened by payment reversal；
#    重复冲正 => 409 payment already reversed；不存在/跨租户 => 404 payment not found
# 5. 重新补齐收款，订单再次结清
curl -s -X POST localhost:8000/orders/o-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":200}'
# 6. 受理退款单
curl -s -X POST localhost:8000/refunds $T -H 'Content-Type: application/json' \
  -d '{"refund_id":"rf-1","order_id":"o-1","amount_cents":200}'
# 7. 读取订单（观察 refunded_cents / refundable_cents）与退款单
curl -s localhost:8000/orders/o-1 $T
curl -s localhost:8000/refunds/rf-1 $T
# 8. 冲正退款单（释放可退余额后，订单可受理结算单）
curl -s -X POST localhost:8000/refunds/rf-1/reverse $T
# 9. 受理结算单
curl -s -X POST localhost:8000/settlements $T -H 'Content-Type: application/json' \
  -d '{"settlement_id":"st-1","order_id":"o-1","amount_cents":300}'
# 10. 读取订单（观察 settled_cents / settleable_cents）与结算单
curl -s localhost:8000/orders/o-1 $T
curl -s localhost:8000/settlements/st-1 $T
# 11. 分两期收款（观察 received_cents / unreceived_cents）
curl -s -X POST localhost:8000/settlements/st-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":100}'
curl -s -X POST localhost:8000/settlements/st-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":200}'
# 12. 有收款时冲正被拒（409 settlement has payments）
curl -s -X POST localhost:8000/settlements/st-1/reverse $T
# 13. 另受理一张未收款的结算单，冲正成功并整体释放其占用
curl -s -X POST localhost:8000/settlements $T -H 'Content-Type: application/json' \
  -d '{"settlement_id":"st-2","order_id":"o-1","amount_cents":200}'
curl -s -X POST localhost:8000/settlements/st-2/reverse $T
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。
- 迁移文件位于 `migrations/`，按文件名顺序执行并在 `schema_migrations` 表记录版本，重启重复执行不生效、不丢数据。

## 一致性说明

- 退款受理的判重、订单校验、退款单落库、累计已退更新在单个 SQLite 立即事务内完成：任一失败整体回滚，不留部分数据；同一退款单标识并发受理最多一个成功，金额绝不重复累计。
- 结算受理的判重、订单校验（已结清/无退款/不超过剩余可结算金额）、结算单落库、累计已结算更新在单个 SQLite 立即事务内完成：同一结算单标识并发受理最多一个成功；不同结算单并发受理时累计已结算绝不会超过订单剩余可结算金额。
- 分期收款的判定与累加、冲正的判定（已受理/累计已收为 0）与订单占用释放，同样各自在单个立即事务内原子完成；收款与冲正并发时写锁串行化，结论唯一、金额自洽。
- 订单收款分期登记的判重（幂等键）/订单校验/流水落库/`paid_cents` 更新，以及逐笔冲正的判定/标记/金额整体退回，各自在单个 SQLite 立即事务内完成：同一流水并发冲正最多一个成功；同一订单并发登记时累计已收绝不超过订单金额；同幂等键超时重试只命中同一条流水、不二次变动金额。
- 三条金额链路相互独立：订单收款（`paid_cents` 与逐笔收款流水）、退款（`refunded_cents`）、结算（`settled_cents` 与结算单自身的 `received_cents`）互不改写；各自冲正只整体减回本链路占用的金额。
- 已受理/已冲正状态持久化，服务重启后流水、已冲正标记与金额仍可判定，可继续登记、冲正或受理新的单据。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优（写事务串行化，依赖 SQLite 行级写锁与重试）。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
