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
- `POST /orders/{order_id}/reversals`：冲正一次已登记的收款。租户通过请求头 `X-Tenant` 传入；请求字段 `reversal_id`（冲正标识，同租户下唯一）、`amount_cents`（最小货币单位整数，必须大于 0）。成功返回 200 与冲正后的订单对象（含 `paid_cents`、`outstanding_cents`、`status`）。失败结果：
  - 订单不存在或属于其他租户：404（不泄漏对象是否存在）。
  - 冲正金额大于当前已收金额：409，账务不发生任何变化。
  - 冲正标识曾成功使用、但本次订单标识或金额与首次不一致：409，账务不发生任何变化。
  - 冲正标识重复提交且内容一致：返回 200 与首次一致的订单结果，不重复扣减。
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款支持整单/分次登记与按冲正标识幂等的收款冲正，未实现分期计划与外部对账。
