# 回测研究与可选持续模拟

2026-10-13：在指定交易渠道暂缓的前提下，补齐自动行情、持续任务、本地账户与订单、运行风控、历史评估、候选版本和统一工作台。当前可交付的是本地模拟闭环；不改变现有 Alpaca Paper 或旧渠道的准入边界。

2026-10-15：当前默认流程改为历史回测与复盘，模拟盘接入继续暂缓。一次性回测使用 `quantbot backtest`，完成报告后结束；原有持续模拟控制保留为后续可选能力。

## 使用

安装 `automation` 扩展与网页扩展后运行：

```sh
.venv/bin/python -m pip install -e '.[automation,workbench]'
.venv/bin/quantbot serve
```

运行中心的 AI 页面默认历史回测，上方可直接上传 CSV 运行并下载报告。下方「回测研究与版本对比」可保存候选、做滚动评估；无需启动 worker，也不必启用任务。若使用 AI，先配置模型，再允许本次回测调用。

一次性入口：`quantbot backtest --market stocks|crypto --provider ai|price_action|demo --data <CSV> --last-bars 30`；加密不支持价格行为规则。AI 需加 `--allow-model-calls`。仅 demo 允许省略 CSV，明确使用合成行情；有 CSV 时使用原数据。保存新的报告目录和独立评估记录，不写入 `runtime/automation` 的持续账户。

CLI 的根目录参数放在子命令前，默认与网页一致：`runtime/automation`。模型设置来自创建时的主配置；后续更改主配置不修改已存在任务。

```sh
# 美股规则对照，自动读取 Yahoo 已收盘原始日线。
.venv/bin/quantbot runtime create --name stock-baseline --market stocks --provider price_action --source yahoo --symbols SPY AAPL MSFT

# 加密 AI，行情只读；这一步不调用模型或交易账户。
.venv/bin/quantbot runtime create --name crypto-ai --market crypto --provider ai --source okx_public --bar 15m --symbols BTC/USDT ETH/USDT --initial-cash 1000

.venv/bin/quantbot runtime list
.venv/bin/quantbot runtime evaluate <任务ID> --last-bars 30 --allow-model-calls
.venv/bin/quantbot runtime show <任务ID>
```

当前到报告复盘即完成。以下为以后需要持续模拟时使用的控制，不属于本轮回测要求：

```sh
.venv/bin/quantbot serve --worker
.venv/bin/quantbot runtime resume <任务ID> --allow-model-calls
.venv/bin/quantbot runtime pause <任务ID>
.venv/bin/quantbot runtime cancel <任务ID> <订单ID>
.venv/bin/quantbot runtime exit <任务ID>

# 异常原因处理后恢复；不会重置历史权益高水位。
.venv/bin/quantbot runtime resume <任务ID> --acknowledge-risk

# 用修改后的模型/策略/风险配置创建新候选，重新评估后再启用。
.venv/bin/quantbot runtime clone <任务ID> --config config/quantbot.toml --name candidate-v2

# 单独运行工作进程，不需要同时开启网页。
.venv/bin/quantbot runtime worker
# 或手动只处理一次到期任务。
.venv/bin/quantbot runtime worker --once
```

规则或演示任务不需要 `--allow-model-calls`。`--provider demo` 必须显式指定，只选择确定性动量规则；行情仍来自所选数据源，不会自动补造缺失行情。AI 失败不会转成演示模型。

## 数据与执行契约

| 项目 | 当前行为 |
|---|---|
| 美股数据 | CSV 或 Yahoo 原始 OHLCV；NYSE 日历识别休市与提前收盘，逐标的检查完整交易日。至少满足主策略预热长度。 |
| 加密数据 | CSV 或 OKX 公开 K 线；只接受已完成的 15m/1h/UTC 日线，每个标的至少 35 根且连续。公开接口最多获取最近 300 根。 |
| 更新频率 | 默认每 60 秒检查；每个收盘快照只决策一次。数据发布时间允许默认 900 秒延迟，任务可自定义。 |
| 行情修订 | 与上次快照重叠部分逐行比较，历史变更会暂停；不会用新版价格覆盖已生成的证据。 |
| 股票精度 | 主风控先生成整数股，本地账本按订单数量执行。 |
| 加密精度 | 复用现有 mock USDT 元数据的 Decimal 数量步长；不是交易所实时交易规则。 |
| 模拟撮合 | 信号收盘后排队，下一根 K 线开盘撮合；买价超过预算限价则取消，不假设日内触价成交。成本按主配置估计，记录参考价和含成本价格。 |
| 止损 | 新买入从成交所在 K 线开始检查；跳空止损按开盘价，日内触发按止损价并标记精确时间未知。 |
| 对账 | 账户、订单事件、权益与执行检查点在同一 SQLite 事务提交；执行和历史评估复用同一个本地撮合实现。 |
| 公司行动 | Yahoo 返回的新拆股/分红落在持仓区间时暂停。当前不模拟拆股、分红入账；CSV 提供方需保证价格基准一致。 |

行情协议依据：[exchange_calendars 日历](https://github.com/gerrymanoim/exchange_calendars)、[yfinance API](https://ranaroussi.github.io/yfinance/reference/index.html)、[OKX 公开 K 线说明](https://app.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks)。OKX 请求只使用公开 GET 接口，过滤未完成 K 线，日线指定 UTC 周期。

## 恢复与控制

- 状态和配置保存在 `runtime/automation/runtime.sqlite`，行情证据保存在 `snapshots/`。保留整个目录才能连续恢复同一账户。
- 工作进程互斥；长模型请求期间独立更新心跳。停止网页不清除任务。重新使用 `serve --worker` 或 `runtime worker` 会继续已启用任务。
- 模型请求前提交账户检查点。进程在推理中断时，下次启动将该次运行标为 `interrupted` 并暂停；不自动重复收费请求。
- 模型运行期间的暂停或取消会改变任务版本号；迟到的模型结果只保存记录，不生成新订单。
- 暂停取消所有待执行订单，已有持仓继续做保护止损检查。该检查依赖工作进程和后续完整行情，不是实时托管止损。
- 模拟退出先取消原有订单，排队卖出持仓，下一根有效 K 线才记录成交。所有持仓退出后任务暂停。
- 日内亏损按美股纽约交易日或加密 UTC 日期的前一根权益计算，回撤相对账户历史最高权益计算；阈值随版本冻结。在 K 线粒度检查，不提供逐笔实时熔断。
- 数据异常立即暂停并取消待执行订单；模型错误当次不生成新订单，默认连续三次失败后暂停。恢复的确认不会绕过仍超限的账户。
- 代码变化后旧任务不会自动沿用新执行行为，需要创建并评估新候选。启用同系列另一个版本暂停当前版本，取消其待执行订单；旧持仓仍由自己的账户监控。切换不移交持仓、不重写旧账本。

## 评估与工作台

基础回测使用最近 3..500 根预热后的 K 线，候选与规则使用同一快照、风控、成本、次根开盘撮合。股票对照为价格行为规则，加密对照为动量规则，另列零收益的现金基线。请求区间必须完整，不能静默缩短；准入要求每个可决策时点有效、零模型错误、无账户熔断，不使用正收益作为软件正确性证明。

新增滚动样本外评估，支持同批 1..6 个冻结候选。在每折较早的参考区间比较版本，记录选择后再进入后续测试区间；测试区间互不重叠，支持按 K 线根数设置隔离间隔。所有版本共用行情快照、初始资金与执行成本，模型、策略和仓位限制可不同。逐折使用独立账户，汇总展示窗口平均收益和最差窗口回撤。完整命令、准入约束及证据边界见 [AI-EVALUATION.md](AI-EVALUATION.md)。

报告保存决策、成交、权益曲线、收益、回撤、实现/未实现盈亏、估计成本、调用耗时和供应方报告的 token 用量。置信度分组只记录有正目标仓位时下一收盘涨跌的观察，不把模型自报置信度当作校准概率。没有 token 用量时记为 `null` 并显示「未提供」，同时记录未报告用量的调用次数；部分有用量时仅累计已报告部分，此项不是费用账单。

启用或恢复会核对最新完成的通过报告、源码与配置指纹，以及冻结行情文件的内容指纹。缺失或不一致时拒绝启用。评估期间代码或参与任务的状态变化，整批准入更新撤回。后一次完整评估失败会撤销该候选的先前准入；基础回放仍可用于最小功能准入，并不等价于已通过滚动评估。

网页展示任务状态和心跳、暂停原因、持仓、待执行订单及取消操作、权益曲线、历史运行与成交、告警、评估对比和候选复制。告警保存在本地工作台，不会向外部通讯工具发送消息。

## 仍待验证与后续边界

指定交易渠道的账户、真实回报、部分成交、撤改订单、实际手续费与现金流水，随富途/老虎/OKX 适配一并处理。新运行器不调用这些交易接口，也未接管原有单次 Alpaca Paper。

当前没有订单簿/成交量约束的部分成交模型、分红与拆股记账、组合跨账户风险或真实资金流水。模型训练、自动因子发现、新闻财报输入和长期前瞻验证仍属于后续研究增强；现有 Rust 和研究工具继续保留。

离线与受控协议测试不证明真实模型效果、长期运行稳定性或交易收益。项目本轮的实际检查结果记录在同目录 `AI-RUNTIME-VALIDATION.md`。
