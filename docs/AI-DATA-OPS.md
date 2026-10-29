# 历史版本行情与研究运行看板

现有 CSV、Yahoo、OKX 公开行情仍可用于回测。`vintage` 输入支持按声明的发布时间回放历史修订，适用于美股日线和加密现货 15m/1h/1d。主回测可直接使用 `backtest --vintage-data` 或网页「导入含发布时间的行情 JSON」；创建研究候选时选择「带发布时间的历史版本 JSON」，随后使用已有研究流程。

## 数据契约

JSON 必须包含以下全部顶层字段；记录包含标准 OHLCV 字段及 `available_at`。示例省略了完整预热区间，单条记录不能运行回测。

```json
{
  "schema_version": 1,
  "market": "stocks",
  "provider": "数据供应方名称与版本",
  "captured_at": "2026-10-18T00:00:00+08:00",
  "data_kind": "historical_unverified",
  "price_basis": "raw",
  "rows": [
    {"date":"2025-01-02","symbol":"SPY","open":100,"high":102,"low":99,"close":101,"volume":1000000,"available_at":"2025-01-02T21:00:00+00:00"},
    {"date":"2025-01-02","symbol":"SPY","open":100,"high":102,"low":99,"close":101.1,"volume":1000000,"available_at":"2025-01-03T21:00:00+00:00"}
  ],
  "actions": []
}
```

- 加密记录用 `timestamp`（开盘时刻）替代 `date`，另含 `bar`，如 `15m`。时间必须带时区，系统统一为 UTC。
- 同一标的、同一根行情的修订使用不同的 `available_at`。同时间的重复版本会被拒绝。信号只读取截至决策时点的最后一个可见版本；后来的修订不会改变过去的信号。
- 撮合固定使用首版 OHLCV，不回写过去的成交。当前模型为收盘决策、下一根开盘执行，因此要求首版在收盘时可得；迟发首版会明确报错，不能把后到价格伪装成当时已知。实际延迟、逐笔行情和排队成交仍需后续模型支持。
- 只接受 `raw` 原始价格；未来数据、无效 OHLCV、周期错位、缺根和休市日记录均拒绝。单文件最多 32 MB、100000 个版本。
- 美股公司行动基础标记包含 `symbol/date/type/available_at`，类型为 `split/dividend/merger/delisting/symbol_change`。补齐 `id/ratio` 的拆并股、补齐普通美元分红条款的记录可进入主回测和研究，格式见 [公司行动会计](AI-CORPORATE-ACTIONS.md)。区间包含缺少条款或不支持的行动时拒绝执行。空 `actions` 是供应方声明，不能证明没有遗漏。
- `data_kind` 只接受 `synthetic` 或 `historical_unverified`。文件声明为合成数据时，研究会强制保留该标签，不能通过网页取消勾选而获得“研究达标”。

报告中的 `availability_enforced=true` 表示程序执行了发布时间过滤；`point_in_time_verified=false` 表示尚无独立来源认证。没有自动证明时间声明真实、历史股票池完整、退市覆盖完整或模型训练数据无污染。传统策略已有的签名 PIT bundle 没有因此被替代；它只含收盘等字段，直接用于 AI 的 OHLCV 撮合还需要完整适配。

冻结报告携带全部实际版本数据、采集来源和内容指纹。外部原始文件之后变化不影响已登记研究。

## 持久化研究计划

冻结研究后，可在网页选择本机时区的计划时间。排队 AI 研究必须显式授权模型调用，预算沿用冻结方案。网页查看和轮询不会启动研究，也不会启动交易模拟进程。

```sh
.venv/bin/quantbot runtime create --name "版本行情研究" --market stocks --provider demo --source vintage --data /绝对路径/vintages.json --symbols SPY
.venv/bin/quantbot runtime study-create --plan research-plan.json
.venv/bin/quantbot runtime study-queue <研究ID> --at '2026-10-18T06:00:00+08:00'
.venv/bin/quantbot runtime operations
.venv/bin/quantbot runtime research-worker --once
```

`--once` 最多消费一个到期计划；提前检查不会运行。持续处理需用户单独启动 `runtime research-worker`，Ctrl+C 停止。AI 计划排队时需加 `--allow-model-calls`；执行器仅使用已存的显式授权。

看板展示最近 100 条计划和 50 项研究，全量统计另行汇总：计划/实际开始时间、延迟和耗时、错误、最近完成/失败、逐研究试验数、数据区间及版本状态、预留请求上限、决策次数、已报告 tokens 和缺失用量。历史文件的结束日不会按距今天数误报过期。

- 手动运行和队列共用互斥锁，并发上限 1。占用期间等待计划保留，不把拥塞记为模型失败。
- 仅能取消尚未开始的计划；取消记录保留。研究从其他入口运行后，到期计划记为跳过，不重复使用留出集。
- 异常、损坏证据、未知中断均保留终态；不自动重试。重启执行器后，中断的研究归档为证据不足。尚未开始研究的失败计划可人工重新排队；已开始研究不能重复运行。
- 队列运行完成与研究达标是独立字段。模型决策次数不是 HTTP 请求次数；备用模型重试及供应方账单不能由该数字推出。没有报告 token 用量时显示未知，不按零费用处理。
- 代码修改后，之前冻结的方案仍可查看和导出，但不能作为当前代码重新执行；需创建新版本并登记新研究。

API：`GET /api/quantbot/runtime/operations`，`POST /research/{id}/queue`（相对 runtime 前缀），`POST /research-queue/{id}/cancel`，`POST /research-queue/run`。

## 离线验收

使用一个不存在的输出目录，运行下列一次性验收。它为美股和加密各生成 220 根合成行情与 2 个后续修订，各执行 11 个研究试验，保存完整证据及运行看板快照，不启动持续进程。

```sh
.venv/bin/python docs/examples/validate_data_ops.py --output runs/data-ops-validation-new
```

这些样例仅证明软件流程，模型调用与券商调用均为 0，结论必须为 `insufficient`。真实历史来源认证、真实模型效果及盈利能力仍未验收。

本轮实际测试数量、运行结果与网页操作证据见 [验收记录](AI-DATA-OPS-VALIDATION.md)。
