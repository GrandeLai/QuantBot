# 研究闭环验收记录

日期：2026-10-18。范围：冻结研究卡片、分段开发、独立最终留出、稳健性检查、研究结论、CLI/API/网页与完整证据导出。

## 自动检查

- 相关回归命令：`.venv/bin/python -m pytest -q tests/test_ai_research.py tests/test_ai_evaluation.py tests/test_ai_backtest.py tests/test_ai_runtime.py tests/test_quantbot_ai.py tests/test_stockbot_statistical_validation.py tests/test_stockbot_cscv_partition.py tests/test_stockbot_experiment_registry.py`，首次 **122 passed、9 subtests passed**。
- 此后补充基础回测留出暴露检查与合成数据标记，最终 `.venv/bin/python -m pytest -q tests/test_ai_research.py`：**12 passed**。它与上述回归集合有重叠，不能相加为 134 项。
- 网页最终 `npm test`：**34 passed**；`npm run build` 成功。
- 对本次变更的 Python 源码与新增测试运行 Ruff：全部通过。
- 既有第三方依赖与价格行为代码仍有弃用告警，本次没有扩展修改这些历史部分。Rust 未修改，未重新执行 Rust 检查。

验证覆盖：两市场独立账户、冻结 CSV 后原文件变化不影响结果、指纹损坏拒绝执行、模型调用显式授权与预算、先选候选再运行留出、缓存决策压力测试、失败候选保留、重复留出识别（含已有基础回测）、中断后不重试、合成数据不能成为研究达标、API/CLI 使用同一账本与报告。

## 完整流程样例

最终产物根目录：`runs/research-validation-20261018-final/`，摘要见其中 `manifest.json`。

| 市场 | 输入 | 开发窗口 | 留出 | 总试验记录 | 运行结果 | 研究结论 |
|---|---|---|---|---|---|---|
| 美股 | SPY，220 根合成日线 | 3 × 30 根 | 40 根 | 11 | 完整完成 | 证据不足：合成行情 |
| 加密现货 | BTC/USDT，220 根合成 15 分钟线 | 3 × 30 根 | 40 根 | 11 | 完整完成 | 证据不足：合成行情等未满足项 |

每组 11 个记录包括：3 个开发窗口、1 个最终留出、1 个规则对照、3 个成本/仓位压力测试、3 个固定种子随机对照。原始决策、订单、成本、权益、参数、提示词和全部 220 行输入均包含在导出的 JSON 中。

实际报告：

- 美股：`automation/research/a027e1ce6a31432bb4f7bf9069ff5077/report.json`
- 加密：`automation/research/4ef3c41b58464c7ebba8bafe9ba9a3db/report.json`

已复查两份方案的源码指纹与最终源码一致。两次运行均为确定性规则，**真实模型调用 0、券商调用 0**；候选持续账户 `as_of` 仍为 `null`，没有启动持续模拟 worker。

## 网页检查

本机临时服务中验证了：研究选择、执行与结果展示、完整 JSON 下载，以及最终版网页填写研究卡片、勾选合成数据、冻结方案。最终界面明确区分“全部试验完成”和“证据不足”，并显示“合成数据：结果仅供流程验证”。临时服务验收后停止。

## 证据边界

这些证据证明功能流程与控制逻辑，不证明模型效果、真实历史策略收益或任何交易渠道能力。历史数据 PIT、公司行动全覆盖、真实模型稳定性、长期前瞻效果尚未验收。运维调度看板、期权组合回测和事件驱动数据接入仍属于后续工作。
