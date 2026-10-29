# 主回测公司行动

主回测、滚动评估和冻结研究支持带完整条款的整股拆并股及普通美元现金分红。候选策略与规则对照使用相同会计规则。数据供应方、历史可得时间与行动完整性仍属导入声明，`point_in_time_verified` 保持 `false`。

## 输入与运行

复用 [历史版本 JSON](AI-DATA-OPS.md)，顶层 `price_basis="raw"`；价格必须为未复权原始 OHLCV。以下是 `actions` 字段示例，日期须位于真实交易日，完整行情另含指标预热区间。

```json
[
  {"id":"split-example","symbol":"SPY","type":"split","date":"2025-10-15",
   "available_at":"2025-10-14T20:00:00+00:00","ratio":"2"},
  {"id":"dividend-example","symbol":"SPY","type":"dividend","date":"2025-10-20",
   "available_at":"2025-10-17T20:00:00+00:00","amount_per_share":"1",
   "pay_date":"2025-10-24","currency":"USD","distribution":"ordinary_cash"}
]
```

示例条款为虚构数据，不代表 SPY 的实际公司行动。`ratio` 表示新股数 / 旧股数，`2` 为一拆二，`0.5` 为二合一；金额和比例可用字符串保留精度。`date` 是拆并股生效日或分红除息日，`pay_date` 是支付日；程序不从登记日推断除息日。

每项行动的 `id` 唯一，条款须在生效日开盘前可得，发布时间不晚于数据采集时间。同标的同日多个行动、条款修订和迟发条款暂不支持。旧式只有 `symbol/date/type/available_at` 的标记仍可读取，从输入首根到回测终点出现这类记录则拒绝继续；该检查包含指标预热历史，避免未处理的拆股污染信号。

```sh
.venv/bin/quantbot backtest --market stocks --provider demo \
  --vintage-data data/stocks-vintages.json --last-bars 30 \
  --output runs/corporate-actions-new
```

`--vintage-data` 与 `--data` 互斥。网页主回测选择「导入含发布时间的行情 JSON」；接口以 `vintage_data` 对象替代 `csv_data`。文件最大 32 MB，支持从报告的 `input_snapshot.vintage_input` 完整重放。AI 模式沿用单次模型调用授权；`demo` 只运行规则。该输入不用于计划预览或持续模拟。

## 会计与特征

| 事件 | 处理 |
|---|---|
| 拆股/合股开盘 | 数量乘比例、每股平均成本和内部保护止损除以比例；总成本不变。结果必须是整数股 |
| 除息日开盘 | 按行动前已有持仓登记应收分红；增加分红权益并调整内部止损。当天开盘后的买入不获得本次权益 |
| 除息后卖出 | 已登记的应收分红继续保留，持仓清空不丢失权益 |
| 支付日 | 只有日期没有到账时刻，按支付日或之后首个交易日收盘入现金，之后才可用于新决策 |
| 回测结束前未支付 | 保留待到账分红，计入账户权益，不虚构现金到账 |
| 行动当日旧计划 | 任一挂单涉及行动标的时撤回同一目标计划的全部挂单；直接受影响订单记 `corporate_action_replan`，同批其他订单记 `corporate_action_plan_replan` 并关联行动 ID。收盘重新决策；没有挂单涉及行动标的时其他计划继续执行，旧保护单仍独立生效 |

账户权益为现金 + 股票市值 + 应收分红。`dividend_income` 是累计已取得的分红权益，`dividend_receivable` 是尚未支付部分；支付只转换资产形式，不重复产生收益。股票交易的已实现/未实现损益保留原有平均成本口径。分红按输入的税前金额计，不模拟预扣税、货币兑换或费用。

模型和规则的历史特征按截至该次决策已生效且已知的拆股重新换算 OHLCV；不提前使用未来拆股，也不修改首版原始撮合价格。现金分红不构造总回报复权特征，除息价格变化仍可出现在价格特征中；跨越公司行动的单根原始价格涨跌不纳入置信度诊断。

账户内部保护阈值与本地旧计划处理是明确的回测假设，不等同于券商各类 GTC 订单的处理。FINRA 的开放订单规则包含分红、拆股、合股及不同订单类型的区别，真实渠道应另行适配。[FINRA Rule 5330](https://www.finra.org/rules-guidance/rulebooks/finra-rules/5330)

普通分红的权益以除息日前持股为依据。特殊大额分配的除息日期和权利转移可能采用不同规则，因此这里只接收明确声明的普通现金分红，拒绝达到前收盘价 25% 的金额以及支付早于除息的记录。[SEC Investor.gov：Ex-Dividend Dates](https://www.investor.gov/introduction-investing/investing-basics/glossary/ex-dividend-dates-when-are-you-entitled-stock-and)

## 当前边界与验收

尚不支持碎股现金替代、特殊分红/due bills、资本返还、非美元分红、并购换股、退市清算、代码变更与期权合约调整；缺少这些发行人条款时明确失败。一般股票买卖结算、部分成交、市场容量和真实税费仍待后续实现。独立期权研究和 SEC 事件窗口工具不因本次修改自动获得这些公司行动能力。

```sh
.venv/bin/python docs/examples/validate_corporate_actions.py \
  --output runs/corporate-actions-validation-new
```

样例生成 220 根合成美股行情、一次拆股和两次普通分红，其中一次分红在回测结束后支付。它比较 CLI、网页 API 与冻结数据重放，独立核验候选和对照的 6 份日志、180 个账户时点，并阻止网络请求。输出 `stocks-vintages.json`、三份完整报告与 `manifest.json`，可直接作为网页导入样例。

专项测试另覆盖手算正向/反向拆股、除息后卖出、待到账权益、碎股拒绝、未来拆股不可前视、非法条款、损坏账目、失败下载和冻结研究。当前验收为离线软件行为验证，没有真实模型/券商执行，也不能作为盈利证明。

公司行动下换仓两腿的修复、真实历史复现和新基准研究见 [跨资产因子研究](CROSS-ASSET-FACTOR-RESEARCH.md)。整批撤回只处理公司行动引起的旧计划失效，不代表跨标的原子成交。
