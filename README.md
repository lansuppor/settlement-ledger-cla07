# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额，受理/读取/冲正退款单，以及受理/读取结算单、结算单分期收款与冲正；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `GET /health`：返回服务与数据库状态。

退款单（独立单据，租户同样通过请求头 `X-Tenant` 声明）：

- `POST /refunds`：受理退款单。请求字段 `refund_id`、`order_id`、`amount_cents`（最小货币单位整数，> 0）。成功返回 201 与退款单对象（状态 `accepted`）。
  - 原订单不存在或属于其他租户：`404 order not found`（两者不可区分）。
  - 订单未结清（未收金额 > 0）：`409 order is not settled`。
  - 退款金额超过可退余额（订单金额 − 累计已退金额）：`409 refund exceeds refundable amount`。
  - 同一（租户，退款单标识）重复受理：`409 refund already accepted`，命中首次受理结论，不二次入账、不改变首次单据。
  - 该标识此前已冲正：`409 refund already reversed`，冲正后同一标识不得再次受理。
- `GET /refunds/{refund_id}`：读取退款单。不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/reverse`：冲正退款单。仅 `accepted` 可冲正，成功返回 200 与状态 `reversed` 的退款单；未受理标识（含跨租户）返回 `404 refund not found`，重复冲正返回 `409 refund already reversed`。

受理成功后订单对象额外暴露：

- `refunded_cents`：累计已退金额（不含已冲正部分）。
- `refundable_cents`：当前可退余额 = `amount_cents − refunded_cents`。
- 守恒关系：`refunded_cents + refundable_cents = amount_cents`；退款不改写 `paid_cents` 与收款记录，`outstanding_cents` 保持为 0。

结算单（独立单据，占用订单的剩余可结算金额，租户同样通过请求头 `X-Tenant` 声明）：

- `POST /settlements`：受理结算单。请求字段 `settlement_id`、`order_id`、`amount_cents`（最小货币单位整数，> 0）。成功返回 201 与结算单对象（状态 `accepted`）。
  - 原订单不存在或属于其他租户：`404 order not found`（两者不可区分）。
  - 订单未结清（未收金额 > 0）：`409 order is not settled`。
  - 订单已发生退款（累计已退金额 > 0）：`409 order has refunds`。
  - 结算金额超过剩余可结算金额（订单金额 − 已受理结算金额之和，不含已冲正部分）：`409 settlement exceeds settleable amount`。
  - 同一（租户，结算单标识）重复受理：`409 settlement already accepted`，命中首次受理结论，不二次占用、不改变首次单据。
  - 该标识此前已冲正：`409 settlement already reversed`，冲正后同一标识不得再次受理。
- `GET /settlements/{settlement_id}`：读取结算单，暴露 `received_cents`（累计已收）与 `unreceived_cents`（未收 = 结算金额 − 累计已收）；不存在或跨租户返回 404。
- `POST /settlements/{settlement_id}/payments`：登记一笔分期收款。请求字段 `amount_cents`（> 0）；累计收款超过结算金额返回 `409 receipt exceeds unreceived amount` 且结算单不改动；成功返回 200 与最新累计已收/未收。
- `POST /settlements/{settlement_id}/reverse`：冲正结算单。仅 `accepted` 且累计已收为 0 可冲正，成功返回 200 与状态 `reversed` 的结算单，并整体释放其占用的订单剩余结算金额；未受理标识（含跨租户）返回 `404 settlement not found`，已有收款返回 `409 settlement has receipts`，重复冲正返回 `409 settlement already reversed`。

结算单受理后订单对象额外暴露：

- `settled_cents`：累计已结算金额（已受理结算金额之和，不含已冲正部分）。
- `settleable_cents`：剩余可结算金额 = `amount_cents − settled_cents`。
- 守恒关系：`settled_cents + settleable_cents = amount_cents`；结算链路不改写 `paid_cents`、收款记录、`refunded_cents` 与退款链路任何数据。

### 调用示例

```bash
T="-H X-Tenant:t1"
# 1. 建单并收清
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o-1","amount_cents":500,"currency":"CNY"}'
curl -s -X POST localhost:8000/orders/o-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":500}'
# 2. 受理退款单
curl -s -X POST localhost:8000/refunds $T -H 'Content-Type: application/json' \
  -d '{"refund_id":"rf-1","order_id":"o-1","amount_cents":200}'
# 3. 读取订单（观察 refunded_cents / refundable_cents）与退款单
curl -s localhost:8000/orders/o-1 $T
curl -s localhost:8000/refunds/rf-1 $T
# 4. 冲正
curl -s -X POST localhost:8000/refunds/rf-1/reverse $T

# 5. 受理结算单、分期收款、读取（订单已结清且无退款）
curl -s -X POST localhost:8000/settlements $T -H 'Content-Type: application/json' \
  -d '{"settlement_id":"st-1","order_id":"o-1","amount_cents":300}'
curl -s -X POST localhost:8000/settlements/st-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":120}'
curl -s localhost:8000/settlements/st-1 $T
curl -s localhost:8000/orders/o-1 $T   # 观察 settled_cents / settleable_cents
# st-1 已有收款，冲正会被 409 settlement has receipts 拒绝；
# 仅累计已收为 0 的结算单可整体冲正并释放订单剩余结算金额：
curl -s -X POST localhost:8000/settlements $T -H 'Content-Type: application/json' \
  -d '{"settlement_id":"st-2","order_id":"o-1","amount_cents":100}'
curl -s -X POST localhost:8000/settlements/st-2/reverse $T
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。
- 迁移文件位于 `migrations/`，按文件名顺序执行并在 `schema_migrations` 表记录版本，重启重复执行不生效、不丢数据。

## 一致性说明

- 退款受理的判重、订单校验、退款单落库、累计已退更新在单个 SQLite 立即事务内完成：任一失败整体回滚，不留部分数据；同一退款单标识并发受理最多一个成功，金额绝不重复累计。
- 结算单受理的判重、订单校验（已结清、累计已退为 0、金额不超过剩余可结算金额）、结算单落库与订单累计已结算更新，分期收款的判定/登记，以及冲正的判定/状态推进/金额释放，同样各自在单个立即事务内原子完成：同一结算单标识并发受理最多一个成功；不同结算单并发受理累计已结算绝不超过订单金额；结算单并发分期收款累计已收绝不超过结算金额。
- 收款、退款、结算分属三条金额链路：`paid_cents`/订单收款记录不被退款或结算改写；退款只影响 `refunded_cents`，冲正整体减回；结算只影响 `settled_cents`，冲正整体释放；结算单收款只累计到结算单自身的 `received_cents`。
- 已受理/已冲正状态持久化，服务重启后仍可判定，可继续收款、冲正或受理新的结算单。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优（写事务串行化，依赖 SQLite 行级写锁与重试）。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 订单收款只支持整单登记，未实现分期与对账（结算单支持分期收款）。
