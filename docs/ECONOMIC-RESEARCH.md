# 经济归因与研究登记

更新日期：2026-10-23。

本阶段补齐双市场的研究诊断和有限试验登记。保留现有风险、执行和准出系统，不启动模型、模拟盘或实盘。

## 经济归因

`economic_study` 从已冻结的相对领先研究协议定位修复后的 16 份基准，先独立对账，再输出每个窗口、每档成本的 JSON。加密与美股各有三个开发分窗及一个连续账户，费用为单边 15/30 bps。

```sh
PYTHONPATH=src .venv/bin/python -m quantbot.ai.economic_study freeze \
  --protocol /absolute/path/PROTOCOL.json --output /absolute/path/new-study
PYTHONPATH=src .venv/bin/python -m quantbot.ai.economic_study run \
  --study /absolute/path/new-study
```

协议必须包含 `id=dual_market_economic_attribution_v1`、模块中的 `METHOD`、历史协议与研究目录的 SHA-256 引用，以及关闭模型、交易、准出和留出评估的标志。输出目录必须不存在；冻结后修改源码或输入会拒绝运行。失败记录单独保留，不能覆盖失败目录重跑。

输出包括：

- 收益核算：同一已实现成交路径上加回费用的毛贡献、执行费用、净收益、期末估计平仓费、净清算收益。加回费用不等于零费用条件下重新运行策略。
- 资金占用：收盘持仓比例、平均持仓市值占权益比例、持仓时的平均敞口。它们不是日内时间加权敞口；当日入场又离场可能没有持仓收盘点。
- 决策漏斗：策略零目标、提交买单、正目标但未成单；买单的目标名义额、计划名义额、成交/取消原因；卖出成交但替代买入取消的完整计划。
- 逐持仓周期：多次买入、部分卖出、分红权益、最后退出原因和未平仓收益；在成交点及仍持仓的收盘点计算含估计平仓费的可观察最大浮盈/浮亏、首次转正时点。
- 退出后诊断：从退出所在交易时点的**收盘**起算，观察随后 5/10 个实际交易时点的含普通分红总收益。即便账户已卖出，也使用发行人分红信息；没有足够后续开发数据时标为右侧截断，不读取留出补足。

日线不能证明止损前后的日内价格顺序。因此输出明确 `intraday_extrema_verified=false`，不把整根 K 线的高低价当成持仓期间的 MFE/MAE。观察到止损后上涨也不能证明放宽止损会赚钱。持仓周期可重叠，退出后均值没有独立性或显著性保证。

目标到订单的缩量同时包含风险预算、可用现金和数量舍入；原日志没有完整限制因子分解。策略的零目标还可能包含内部冷却或趋势过滤。报告不臆造这些原因各自贡献，也不把资金占用低当成提高风险限额的依据。

## 跨目录登记

`research_catalog` 复用 `ExperimentRegistry` 的试验身份与计数规则，用不可覆盖的 JSON 版本保存已知试验、旧开发数据、保护区间和读取声明。后续版本引用父版本哈希；删掉失败、改变既有定义或修改父文件会被拒绝。副本文件名和文件哈希变化不能绕过同市场、同标的、同日期的已读区间检查。数据区间包含预热。

```sh
PYTHONPATH=src .venv/bin/python -m quantbot.ai.research_catalog import-leadership \
  --protocol /absolute/path/leadership/PROTOCOL.json \
  --completed /absolute/path/leadership/study/completed.json \
  --output /absolute/path/catalog-001.json

PYTHONPATH=src .venv/bin/python -m quantbot.ai.research_catalog reserve \
  --catalog /absolute/path/catalog-001.json --plans /absolute/path/RESEARCH-PLANS.json \
  --output /absolute/path/catalog-002.json

PYTHONPATH=src .venv/bin/python -m quantbot.ai.research_catalog check-window \
  --catalog /absolute/path/catalog-002.json --market crypto --symbols BTC/USDT ETH/USDT \
  --start 2019-01-01 --end 2026-07-01 --role independent

PYTHONPATH=src .venv/bin/python -m quantbot.ai.research_catalog claim-development \
  --catalog /absolute/path/catalog-002.json --market crypto --symbols BTC/USDT ETH/USDT \
  --start 2019-01-01 --end 2026-07-01 --purpose descriptive-attribution \
  --output /absolute/path/catalog-003.json
```

读取声明在执行前保存，所以失败的研究仍属于已暴露历史。`check-window` 返回 `allowed_by_index=false` 表示拒绝；`true` 只表示该目录未发现冲突，`independence_certified` 始终为 false。新目录、新标的或改名都不能自动授予独立验证资格。原加密封存留出和美股未释放价格区间仍保护。

初次导入的是已知累计 130 项历史记录，包括保守占位；这些占位计入试验数，但不能当成有原始执行证据的成功或完成记录。历史导入时间记录为汇总协议时间，不伪造各次原始登记时间。预登记四项新机制后至少按 134 项计数；未来新增试验必须继续累计。

该目录是可追溯的本地研究索引，不是签名防篡改安全服务，不替代正式 HMAC 数据访问/准出账本。当前经济归因入口强制检查它，其他历史运行器和数据库尚未全部统一接入。它也不是全部旧项目试验的完整普查。

## 四项研究卡片

| 市场 | 冻结机制 | 第一阶段证据 | 尚缺条件 |
|---|---|---|---|
| 加密 | 60 日正动量且收盘在 120 日均线上方 | 相对 20 日动量的配对扣费净增量 | 实现固定期限、同风险的局部执行探针；冻结精确输入与原点 |
| 加密 | 低趋势效率区间内的负向偏离回归 | 相对不加区间约束的回归对照 | 同上；BTC/ETH 共同检验，不能挑盈利标的 |
| 美股 | 月末跨资产正趋势配置 | 相对仅股票 ETF 对照的扣费增量 | IEF/GLD 历史行情及分配/拆股口径；执行探针 |
| 美股 | 月末行业 ETF 相对强度 | 相对同池等权对照的扣费增量 | 九只行业 ETF 历史行情与公司行动；执行探针 |

这些卡片冻结机制、参数、对照、风险、成本和淘汰规则；`data_freeze_complete=false` 表示尚未具备执行输入。下一阶段先补数据、校验可得性，再冻结代码、数据哈希和具体事件原点，执行有限因子检验。只有稳定扣费增量通过后，才能另行构建和回放完整策略；旧历史上的合格也不等于独立验证或准出。

新增 ETF 属于开发研究范围，不改变正式交易白名单。原止损、止盈、风险预算和准出阈值继续保留；本轮没有评估封存留出或调用策略模型。
