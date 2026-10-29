# 已有持仓的期权增强与对冲回测

QuantBot 可以导入已有股票持仓，在同一份历史数据上比较直接持股、备兑看涨、保护性看跌和领口。备兑和领口另有除息前提前指派压力情景，共六组结果。当前是可由本地 Agent 调用的确定性研究工具，不调用大模型或券商，不连接模拟盘。

## 使用

网页「运行中心 → AI 量化交易 → 持仓期权研究」导入历史数据，填写现金和股数，再设置剩余天数、行权价比例、覆盖比例、权利金预算和换月规则。可以先点「加载合成样例」理解流程。报告包含权益对比、损益对账、现金、剩余持仓、Greeks 近似、拒绝和错误记录；下载同时包含冻结输入。

一次离线验收会产生数据、持仓、参数及六组完整结果，目录必须全新：

```bash
.venv/bin/python docs/examples/validate_options_overlay.py --output runs/my-options-example

.venv/bin/quantbot options-backtest \
  --data runs/my-options-example/data.json \
  --portfolio runs/my-options-example/portfolio.json \
  --policy runs/my-options-example/policy.json \
  --output runs/my-options-replay
```

初始持仓格式为 `{"cash": 5000, "positions": {"AAPL": 100}}`。首个交易日收盘为起点，股票按该日市价入账；不计入窗口前的历史成本、已有期权或应收分红。第一份交易计划在首日收盘后生成，下一交易日开盘窗口成交。

API：`POST /api/quantbot/options/backtest`，请求为 `data`、`portfolio`、`policy`；`GET /api/quantbot/options/example` 返回明确标记的合成样例。响应的 `download_url` 导出完整证据。

## 输入契约

JSON 顶层必须包含 `schema_version=1`、`source`、`data_kind`、`captured_at`、`price_basis="raw"`、`bars`、`contracts`、`quotes`、`dividends`、`actions`。`data_kind` 为 `synthetic` 或 `historical_unverified`。输入上限 32 MB、20 个标的、3～1000 个完整美股交易日。

| 数组 | 每行必需字段 |
|---|---|
| bars | date、symbol、open、high、low、close、volume、available_at |
| contracts | id、symbol、kind（call/put）、strike、expiry、last_trade_date、multiplier、deliverable_shares、exercise_style、settlement、currency、available_at |
| quotes | contract_id、date、phase（open/close）、bid、ask、bid_size、ask_size、open_interest、iv、quote_at、available_at |
| dividends | id、symbol、ex_date、pay_date、amount、available_at |
| actions | 当前必须为空；遇到拆股、并购、退市或交付物调整，拒绝按普通合约处理 |

时间须有时区。报价可含后来修订，回放只选择在对应决策/成交截止前可得的版本。默认收盘后 15 分钟决策、次日开盘 60 秒内成交，报价距开收盘不超过 300 秒。持有合约缺少可得收盘报价会中止该组回放并保留失败；禁止用理论价格补齐。IV 缺失用 `null`，账本仍可回放，Greeks 标为未知。

目前只支持 USD、美式、实物交付、每份 100 股的标准股票期权；合约调整不能简单沿用 100 股。标准规格及交付规则参考 [OCC 股票期权规格](https://www.theocc.com/clearance-and-settlement/clearing/equity-options-product-specifications)。实时 yfinance 期权链仍用于现有研究查询，不作为历史回测输入。

## 会计与成交约定

- 同一标的多腿作为净额包：买入按 ask，卖出按 bid，计每份单边佣金；任一腿缺报价、价差过宽、可见数量不足或资金不足，整包拒绝。默认不超过可见买卖数量的 10%，不模拟排队和冲击。
- 短看涨必须有股票覆盖，长看跌不超过对应股票数；不足一份的股票保留。不同标的共享当天的新付权利金预算，默认占前日权益的 5%，不是每个标的分别使用 5%。
- 正交易现金 T+1 才成为可再次使用的现金；负现金流需已结算资金，拒绝隐式借款。分红按除息日前持仓确认应收，支付日入现金，避免除息价格变动和分红重复计算。
- 到期按收盘内在价值与预登记门槛（默认每股 0.01 美元）处理：短看涨指派、长看跌行权时交付已有股票；款项 T+1 可用。没有足够股票的看跌只能按历史报价平仓，不能隐式创建股票空头。未考虑盘后变动和相反行权指令，实际行权并非只由该门槛决定，见 [OIC 行权说明](https://www.optionseducation.org/referencelibrary/faq/options-exercise)。
- 提前指派仅是独立情景：除息前、价内看涨且分红超过剩余时间价值时按全部指派模拟，取消后续相应分红权益。它不预测实际指派概率，实际指派的不确定性见 [OIC 指派说明](https://www.optionseducation.org/referencelibrary/faq/options-assignment)。
- 临近到期时生成换月计划，仍到下一开盘执行；没有合格替代合约时保留原头寸直至到期。指派/行权后不自动回购股票；剩余持仓和现金直接展示。默认末日只估值，可事先选择末日按收盘报价平期权，不能成交就报告失败。
- 每天核对 `权益变化 = 股票损益 + 期权损益 + 分红 - 费用`。期权按可平仓的一侧估值；权益中包含未支付分红。Greeks 是欧式 Black-Scholes 近似，只分解前后持仓相同且 IV 完整的区间，交易、指派、价差和其他差异留在残差。

## 证据边界

`input.json` 在回放前冻结参数、持仓、实际输入和源码哈希；`report.json` 保存六组独立曲线和事件。`execution_completed` 只表示六组完整运行；合成数据始终 `research_conclusion="insufficient"`，导入历史数据为 `not_assessed`。输入声明的可得时点被强制执行，来源、完整性、公司行动声明尚未独立认证，不能标为完整 point-in-time 数据。

期权研究尚未接入通用研究卡片的开发窗口、最终留出和 DSR/PBO，也没有自主 LLM 期权选择器、生产行情授权或真实经纪商适配。税费、利息、冲击、借贷、组合保证金、不同结算产品和复杂交付物暂未建模；多腿按历史双边报价同时成交是研究假设。

当前隔离验收见 [AI-OPTIONS-VALIDATION.md](AI-OPTIONS-VALIDATION.md)。
