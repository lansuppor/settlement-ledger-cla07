# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、订单收款的分期登记/逐笔冲正/按条件检索并核对未收金额，受理/读取/冲正退款单，以及受理/读取/分期收款/冲正结算单；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
  - `refunded_cents` / `refundable_cents`：累计已退金额 / 当前可退余额，两者之和等于 `amount_cents`。
  - `settled_cents` / `settleable_cents`：累计已结算金额 / 剩余可结算金额，两者之和等于 `amount_cents`。
  - `payments`：该订单的收款流水列表，按登记顺序排列，每条含 `payment_id`、`amount_cents`、`status`（`accepted`/`reversed`）、`reversed`、`seq`、`created_at`、`reversed_at`。
- `POST /orders/{order_id}/payments`：分期登记一笔收款。请求字段 `amount_cents`（最小货币单位整数，> 0）与可选 `idempotency_key`。成功返回 201 与收款流水（含服务端生成的 `payment_id`、登记时刻 `created_at`）。
  - 订单不存在或跨租户：`404 order not found`（不可区分）。
  - 本次收款会使累计已收超过订单金额：`409 payment exceeds outstanding amount`，不改动订单与既有收款、不产生流水。
  - 携带相同 `idempotency_key` 重试：`409 payment already registered`，命中首次结论，不产生第二条流水、不二次入账。
- `POST /payments/{payment_id}/reverse`：逐笔冲正收款流水。仅未冲正流水可冲正，成功返回 200 与状态 `reversed` 的流水（含 `reversed_at`），并把该笔金额从累计已收中整体退回。
  - 流水不存在（含跨租户）：`404 payment not found`。
  - 已冲正流水重复冲正：`409 payment already reversed`。
  - 两种拒绝原因与 404 可区分，且不改动订单与任何流水。
- `GET /payments`：按组合条件检索收款流水，租户由 `X-Tenant` 限定（跨租户一律查不到）。查询参数均可组合、均可空：
  - `order_id`（订单标识）、`min_amount` / `max_amount`（金额区间，闭区间，正整数）、`created_from` / `created_to`（登记时间区间，ISO 8601，闭区间）、`reversed`（`true` 仅已冲正 / `false` 仅未冲正 / 缺省全部）。
  - 游标分页：`limit`（1–200，默认 50）、`after_seq`（上一页响应的 `next_seq`）。返回 `{ "items": [...], "next_seq": <int|null> }`，按登记顺序（`seq` 升序）稳定排列，翻页不重不漏；同一查询重复执行结果一致。
- `GET /health`：返回服务与数据库状态。

退款单（独立单据，租户同样通过请求头 `X-Tenant` 声明）：

- `POST /refunds`：受理退款单。请求字段 `refund_id`、`order_id`、`amount_cents`（最小货币单位整数，> 0）。成功返回 201 与退款单对象（状态 `accepted`）。
  - 原订单不存在或属于其他租户：`404 order not found`（两者不可区分）。
  - 订单未结清且从未结清（未收金额 > 0）：`409 order is not settled`。
  - 订单曾结清、但收款被冲正导致未收重新大于 0：`409 order payment reversed`（与上一条可区分）。
  - 退款金额超过可退余额（订单金额 − 累计已退金额）：`409 refund exceeds refundable amount`。
  - 同一（租户，退款单标识）重复受理：`409 refund already accepted`，命中首次受理结论，不二次入账、不改变首次单据。
  - 该标识此前已冲正：`409 refund already reversed`，冲正后同一标识不得再次受理。
- `GET /refunds/{refund_id}`：读取退款单。不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/reverse`：冲正退款单。仅 `accepted` 可冲正，成功返回 200 与状态 `reversed` 的退款单；未受理标识（含跨租户）返回 `404 refund not found`，重复冲正返回 `409 refund already reversed`。

结算单（独立单据，租户同样通过请求头 `X-Tenant` 声明）：

- `POST /settlements`：受理结算单。请求字段 `settlement_id`、`order_id`、`amount_cents`（最小货币单位整数，> 0）。成功返回 201 与结算单对象（状态 `accepted`，`received_cents` 为累计已收，`unreceived_cents` 为未收金额）。
  - 原订单不存在或属于其他租户：`404 order not found`（两者不可区分）。
  - 订单未结清且从未结清（未收金额 > 0）：`409 order is not settled`。
  - 订单曾结清、但收款被冲正导致未收重新大于 0：`409 order payment reversed`（与上一条可区分）。
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
- 守恒关系：`refunded_cents + refundable_cents = amount_cents`；退款不改写 `paid_cents` 与收款记录，`outstanding_cents` 保持为 0。

结算链路的订单守恒：

- `settled_cents`：累计已结算金额（各已受理结算单金额之和，已冲正部分整体释放）。
- `settleable_cents`：剩余可结算金额 = `amount_cents − settled_cents`。
- 守恒关系：`settled_cents + settleable_cents = amount_cents`；结算链路不改写订单的 `paid_cents`、收款记录、`refunded_cents` 与退款链路的任何数据。结算单的分期收款只累加到结算单自身的 `received_cents`，不改变订单已收金额。

收款链路的订单守恒：

- `paid_cents`：累计已收 = 该订单全部**未冲正**收款流水金额之和。
- `outstanding_cents`：未收金额 = `amount_cents − paid_cents`，恒有 `paid_cents + outstanding_cents = amount_cents`。
- 冲正一笔流水只整体减回该笔金额并把流水标记为 `reversed`，不影响其他流水；冲正不改写 `refunded_cents`、退款单，也不改写 `settled_cents`、`settleable_cents`、结算单及其收款数据。
- 冲正使未收重新大于 0 后，订单不再满足“已结清”：新的退款单、结算单被拒（`409 order payment reversed`）；此前已受理的退款单、结算单及其收款、冲正行为与结论保持不变。

### 调用示例

```bash
T="-H X-Tenant:t1"
# 1. 建单，分两期收款（每条流水返回 payment_id）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o-1","amount_cents":500,"currency":"CNY"}'
curl -s -X POST localhost:8000/orders/o-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":200,"idempotency_key":"pay-1"}'
curl -s -X POST localhost:8000/orders/o-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":300}'
# 2. 读取订单（观察 paid_cents / outstanding_cents 与 payments 流水）
curl -s localhost:8000/orders/o-1 $T
# 3. 条件检索流水（订单 + 金额区间 + 仅未冲正，游标分页）
curl -s -G localhost:8000/payments -H 'X-Tenant: t1' \
  --data-urlencode order_id=o-1 --data-urlencode min_amount=100 --data-urlencode reversed=false
# 4. 冲正第一期收款（金额整体退回，未收重新变为 200）
PID=$(curl -s -G localhost:8000/payments -H 'X-Tenant: t1' --data-urlencode order_id=o-1 \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["items"][0]["payment_id"])')
curl -s -X POST localhost:8000/payments/$PID/reverse $T
# 5. 重复冲正 -> 409 payment already reversed；对不存在/跨租户流水冲正 -> 404 payment not found
curl -s -X POST localhost:8000/payments/$PID/reverse $T
curl -s -X POST localhost:8000/payments/nope/reverse $T
# 6. 冲正后订单未结清：受理退款/结算单被拒（409 order payment reversed）
curl -s -X POST localhost:8000/refunds $T -H 'Content-Type: application/json' \
  -d '{"refund_id":"rf-1","order_id":"o-1","amount_cents":100}'
# 7. 重新收清后恢复可受理
curl -s -X POST localhost:8000/orders/o-1/payments $T -H 'Content-Type: application/json' \
  -d '{"amount_cents":200}'
curl -s -X POST localhost:8000/refunds $T -H 'Content-Type: application/json' \
  -d '{"refund_id":"rf-1","order_id":"o-1","amount_cents":200}'
```

> 退款单 / 结算单的完整受理、读取、分期收款与冲正示例见下文相应接口，链路守恒保持不变。

<details><summary>退款单与结算单调用示例（既有能力）</summary>

```bash
# 受理/读取/冲正退款单
curl -s -X POST localhost:8000/refunds $T -H 'Content-Type: application/json' \
  -d '{"refund_id":"rf-1","order_id":"o-1","amount_cents":200}'
curl -s localhost:8000/refunds/rf-1 $T
curl -s -X POST localhost:8000/refunds/rf-1/reverse $T
# 受理结算单并分两期收款
curl -s -X POST localhost:8000/settlements $T -H 'Content-Type: application/json' \
  -d '{"settlement_id":"st-1","order_id":"o-1","amount_cents":300}'
curl -s -X POST localhost:8000/settlements/st-1/payments $T -H 'Content-Type: application/json' -d '{"amount_cents":100}'
curl -s -X POST localhost:8000/settlements/st-1/payments $T -H 'Content-Type: application/json' -d '{"amount_cents":200}'
# 有收款时冲正被拒（409 settlement has payments）
curl -s -X POST localhost:8000/settlements/st-1/reverse $T
```

</details>

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。
- 迁移文件位于 `migrations/`，按文件名顺序执行并在 `schema_migrations` 表记录版本，重启重复执行不生效、不丢数据。

## 一致性说明

- 退款受理的判重、订单校验、退款单落库、累计已退更新在单个 SQLite 立即事务内完成：任一失败整体回滚，不留部分数据；同一退款单标识并发受理最多一个成功，金额绝不重复累计。
- 结算受理的判重、订单校验（已结清/无退款/不超过剩余可结算金额）、结算单落库、累计已结算更新在单个 SQLite 立即事务内完成：同一结算单标识并发受理最多一个成功；不同结算单并发受理时累计已结算绝不会超过订单剩余可结算金额。
- 分期收款的判定与累加、冲正的判定（已受理/累计已收为 0）与订单占用释放，同样各自在单个立即事务内原子完成；收款与冲正并发时写锁串行化，结论唯一、金额自洽。
- 订单收款分期登记的判重（幂等键）、订单校验（累计已收不超过订单金额）、流水落库、`paid_cents` 累加在单个 SQLite 立即事务内完成：同一订单并发登记累计已收绝不超过订单金额；逐笔冲正的状态判定、流水置为 `reversed`、`paid_cents` 整体减回也在单个立即事务内完成，同一流水并发冲正最多一个成功；携带相同 `idempotency_key` 的超时重试只命中同一次结论，不产生第二条流水或第二次金额变动。
- 三条金额链路相互独立：订单收款（`paid_cents`）、退款（`refunded_cents`）、结算（`settled_cents` 与结算单自身的 `received_cents`）互不改写；各自冲正只整体减回本链路占用的金额。收款冲正不触碰退款单/结算单及其收款数据。
- 已受理/已冲正状态、收款流水与登记时刻持久化，服务重启后仍可判定，可继续登记、冲正或检索；游标分页基于单调 `seq`，重复查询结果一致。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优（写事务串行化，依赖 SQLite 行级写锁与重试）。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款流水标识由服务端按租户单调序号生成，调用方不可自定义；登记幂等需显式携带 `idempotency_key`，未携带时按独立登记处理。
