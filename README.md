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
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /refunds`：受理退款单。租户经 `X-Tenant` 头传入；请求字段 `refund_id`、`order_id`、`amount_cents`（正整数）。订单须已结清（未收为 0），退款金额不得超过可退余额（`订单金额 − 累计已退`）。成功返回 201 与退款单（`status=accepted`）；原订单不存在或属其他租户返回 404；未结清、超可退余额、同标识重复受理（含已冲正后再次受理）返回 409，`detail` 给出可区分原因。
- `GET /refunds/{refund_id}`：按标识读取退款单；不存在与跨租户均返回 404。
- `POST /refunds/{refund_id}/reversal`：冲正退款单。仅 `accepted` 可冲正，成功返回 200（`status=reversed`）并把金额整体退回订单累计已退；标识不存在返回 404；已冲正再冲正返回 409（`refund already reversed`）。
- `GET /health`：返回服务与数据库状态。

订单读取对象同时包含 `refunded_cents`（累计已退）与 `refundable_cents`（当前可退余额），两者之和恒等于 `amount_cents`；退款不改动 `paid_cents` 与 `outstanding_cents`。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账。
