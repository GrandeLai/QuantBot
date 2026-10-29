# 真实 Codex 加密回测验收

日期：2026-10-21。已完成一次真实 Codex 决策驱动的加密现货历史回测，并通过候选与规则基准的独立对账。这是有限区间的工程验收，尚不能证明策略有效或可盈利。

## 输入与执行结果

| 项目 | 本次记录 |
|---|---|
| 模型来源 | 本机已登录的 `codex-cli 0.162.0`，`codex_cli` 后端 |
| 显式模型与强度 | `gpt-6.1-sol` / `max`；未配置备用模型 |
| 行情 | OKX 公开现货 K 线，BTC/USDT、ETH/USDT，1 小时 |
| 冻结数据 | 598 条完整 K 线，每个标的 299 条；通过连续性、OHLCV、重复和完整收盘检查 |
| 回测区间 | 2026-10-21 12:00、13:00 UTC 两个决策时点，14:00 UTC 最终估值 |
| 初始资金 | 1000 USDT，本地历史账户 |
| 运行完整性 | `execution_completed=true`；完整窗口、完整决策、无模型错误、无风险熔断四项均通过 |
| 成功调用 | 2 次独立 Codex 会话，2 个有效结构化决策，0 个模型错误 |
| 成交 | 2 笔回测买入，按下一根开盘及 5 bps 估计成本撮合 |
| AI 与规则对账 | 各 3 个账户时点、2 笔回测成交，均为 `reconciled`，差异 0 |
| 结束状态 | 候选 `paused` / `evaluated`，持续运行周期数 0 |

模型两次均提出 BTC 与 ETH 各 20% 的目标权重。程序根据数量步长及风险约束生成订单；第二个时点按数量步长取整后的目标持仓没有变化，未生成新订单。最终持有 BTC 0.0023、ETH 0.080，现金 610.21404442 USDT。

| 指标 | AI 候选 | 动量规则基准 |
|---|---:|---:|
| 最终权益（USDT） | 1000.85435442 | 1000.85435442 |
| 区间权益收益 | +0.0854354% | +0.0854354% |
| 最大回撤幅度 | 0.0196176% | 0.0196176% |
| 估计执行成本（USDT） | 0.19479558 | 0.19479558 |
| 已实现损益（USDT） | 0 | 0 |

本次 AI 与简单规则的实际持仓和结果相同，没有显示超额收益。末尾持仓按市值估值，未虚构平仓；上述数字均不是交易所实际成交或已兑现利润。

成功运行报告的模型用量为输入 35,870、输出 2,526，合计 **38,396 token**，两次用量均有记录。它是 CLI 报告的 token 数，不是费用账单。

## 失败保留与修复

本轮共执行三次独立回测尝试，每次两个策略时点；程序没有自动重试，后两次均在取得新的诊断证据后显式启动。原报告全部保留：

1. `backtest/`：两个 Codex 进程均以代码 1 退出；候选决策未完成。随后同参数的无推理初始化诊断在外层受限沙箱中复现系统权限错误，在正常本机权限下通过。原始调用的 stderr 未保留，不能把后续诊断当作其完整原日志。
2. `backtest-local/`：正常本机权限下，两个返回均被旧事件校验拒绝。离线固定响应复现了 CLI 把“Code Mode 执行器已禁用”的启动提示包装成 `error` 条目的行为。
3. `backtest-verified/`：修复事件兼容后，保持同一冻结行情、模型和风险参数完成验收。两个会话均保留完成事件、用量与最终结构化决策，未出现工具事件。

六次策略供应方调用尝试中，两次成功、四次失败。失败调用的用量均未知，不能计作零；38,396 token 只覆盖最后成功的两次。规则基准调用本地规则，不请求模型。离线 CLI 诊断使用隔离目录和本机固定响应，不读取或复制登录凭据，不消耗真实模型调用。

本轮代码改动：

- OKX 公开行情请求补齐真实的 `QuantBot/<版本>` 客户端标识，解决本机实测的 HTTP 403；不涉及私有接口、账户密钥或订单。
- Codex 只精确兼容模型回合开始前的已禁用执行器固定提示；其他 CLI 警告、模型切换、工具事件、未知类型及非法结构仍拒绝。未开启执行器或放宽只读沙箱。
- 明确区分本机权限退出、CLI 警告和工具事件；错误报告不暴露原始 stderr、命令内容或推理文本。

CLI 将警告表示成错误条目的协议依据可参阅 [OpenAI 事件处理实现](https://github.com/openai/codex/blob/main/codex-rs/exec/src/event_processor_with_jsonl_output.rs)；本机 0.162.0 的实际复现记录是本次兼容处理的直接证据。

## 验证与证据位置

相关回归 **54 项通过**，覆盖 Codex CLI/网页回测、研究问答、调用授权、超时、错误事件、权限分类、OKX 完整 K 线筛选和回测运行。改动范围 Ruff 通过。第三方依赖有弃用警告，没有测试失败。本轮未修改前端或 Rust，未重新构建发行包。

```sh
PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest \
  tests/test_codex_provider.py tests/test_ai_runtime.py tests/test_ai_backtest.py \
  -q -p no:cacheprovider
```

本机产物目录：

```text
/Users/bytedance/Documents/Codex/2026-10-05/du-wi/codex-real-backtest-20261021T144144Z/
```

- `backtest-verified/report.json`：成功回测报告、冻结配置、完整行情、决策、成本和账目。
- `backtest-verified-candidate-reconciliation.json`、`backtest-verified-baseline-reconciliation.json`：独立对账。
- `data-provenance.json`、`crypto.csv`：公开数据来源、质量检查与冻结数据。
- `model-execution*.json`、`verified-call-1.json`、`verified-call-2.json`：逐次运行、源码身份、会话标识、事件类型及用量，不含推理文本。
- `offline-cli-events-before.json`、`offline-cli-events.json`：相同固定启动提示在修复前被拒绝、修复后通过的离线证据。
- `verified-job-state.json`：任务保持暂停，未启动持续运行。
- `changes.patch`、`delivery-manifest.json`、`review.md`：本轮改动、文件校验值及交付索引。

成功运行源码 SHA-256：`5ff5750dff831ed074dff1b0954ee42f775d76a709bbe4550bddd417a2a83a5f`。输入 CSV SHA-256：`470245538dd757205bb47db3fa578e1f44e07330c0e01cc4c616197c53be2777`。

行情来自一次真实公开接口读取，但历史可得时间与修订来源未经独立认证，`point_in_time_verified=false`。本次未检验更长区间、独立留出、不同市场状态、模型训练知识污染或长期运行稳定性；未接入模拟盘或实盘。
