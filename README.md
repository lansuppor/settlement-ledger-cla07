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
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。可用请求头 `X-Actor` 标记发起方，留痕在收款流水的 `actor_id`（缺省为 `unknown`）。
- `GET /orders/{order_id}/payments`：查询订单的全部收款与冲正流水，按发生顺序返回。每条记录含 `flow_id`（业务流水标识）、`type`（`payment`/`reversal`）、`amount_cents`、`created_at`、`actor_id`，冲正记录另含 `origin_flow_id` 指向原收款；订单不存在或跨租户返回 404。
- `POST /orders/{order_id}/payments/{flow_id}/reversal`：冲正指定收款（全额，不支持部分冲正）。冲正后订单的已收/未收金额与状态与该笔收款从未发生一致（已结清也会回到未结清）。可用 `X-Actor` 标记冲正发起方。拒绝原因：订单不存在/跨租户 → 404 `order not found`；流水不存在或不属于该订单 → 404 `payment flow not found`；重复冲正（含重放）→ 409 `payment already reversed`。任一失败账面与流水均不变。
- `GET /health`：返回服务与数据库状态。

## 调用示例

```bash
# 受理订单并登记两笔收款（X-Actor 为发起方标识，可选）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o1","amount_cents":500,"currency":"CNY"}'
curl -s -X POST localhost:8000/orders/o1/payments \
  -H 'X-Tenant: t1' -H 'X-Actor: cashier-1' -H 'Content-Type: application/json' \
  -d '{"amount_cents":200}'

# 查询来龙去脉：收款与冲正按发生顺序返回
curl -s localhost:8000/orders/o1/payments -H 'X-Tenant: t1'

# 冲正第一笔收款（flow_id 取自查询结果）
curl -s -X POST localhost:8000/orders/o1/payments/<flow_id>/reversal \
  -H 'X-Tenant: t1' -H 'X-Actor: cashier-2'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账；支持全额收款冲正，不支持部分冲正。
