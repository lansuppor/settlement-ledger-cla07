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
- `POST /orders/{order_id}/reversals`：登记一次收款冲正。租户通过请求头 `X-Tenant` 传入；请求字段 `reversal_id`（冲正标识，最小长度 1）与 `amount_cents`（本次冲正金额，最小货币单位整数，必须大于 0）。
  - 成功返回 200 与订单对象（含冲正后的 `paid_cents`、`outstanding_cents` 与按新口径回算的 `status`）；同一 `reversal_id` 重发且订单标识与金额一致时，幂等返回 200，账务只扣减一次。
  - 冲正金额超过当前已收金额、或同一 `reversal_id` 的业务内容与首次不一致，返回 409 且不改变任何账务。
  - 订单不存在或属于其他租户返回 404（不泄漏对象是否存在）；缺少租户请求头返回 400。
- `GET /orders/{order_id}/ledger`：按标识读取订单的收款流水清单。租户通过请求头 `X-Tenant` 传入；成功返回 200 与 `{"order_id", "entries"}`，`entries` 按生效先后排列，每一次成功生效的收款或冲正各出现一次。每条流水包含 `entry_id`（流水标识，同类型多次操作可据此区分）、`kind`（`payment` 收款 / `reversal` 冲正）、`amount_cents`（本次金额）、`paid_cents` / `outstanding_cents` / `status`（本次操作后的账务快照），冲正流水另带 `reversal_id` 可定位到当初那次冲正。重复提交的冲正标识、被拒绝的收款与冲正均不产生流水。订单不存在或属于其他租户返回 404（不泄漏对象是否存在）；缺少租户请求头返回 400。
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期、退款与对账（冲正仅冲减已登记收款，不涉及原路退款）。
