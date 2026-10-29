# 本地 Codex 驱动 QuantBot

更新日期：2026-10-22。

目标是让本地 Agent 完成“理解研究目标 → 准备行情 → 提出候选 → 调用回测 → 检查风控和账目 → 复盘”全过程，并用本机 Codex 的模型产生目标仓位。加密现货按 [分级准出](CRYPTO-ADMISSION.md) 顺序推进：回测达标后才准备 OKX 模拟盘，模拟盘再独立验收；当前仍在回测阶段。

## 分工

| 环节 | 承担者 | 产物 |
|---|---|---|
| 目标、假设、候选与有限迭代 | 当前本地 Codex Agent | 研究卡片、配置、命令执行记录与复盘 |
| 每个历史时点的策略决策 | QuantBot 调用独立 `codex exec` | 标的、目标权重、置信度、理由 |
| 行情和历史可得性裁剪 | QuantBot | 冻结行情快照与特征 |
| 风控、数量、次根开盘撮合、成本 | QuantBot | 通过约束的计划与模拟成交 |
| 独立对账与证据保存 | QuantBot | 原始决策、账户、报告与逐步日志 |

复用既有 CLI 和研究协议，不新增一套自动交易引擎。外层 Agent 可以编排流程；内层每次只接收指定时点输入，关闭浏览与命令等工具，拒绝使用工具的结果，不传入项目文件与当前聊天。隔离不会消除模型训练数据中的历史知识污染，历史回测不能据此证明前瞻效果。

因子研究可先运行 `quantbot factor-study register/run`，冻结 12 个日线因子、开发数据和 1/5/10 日标签，产出可追溯的时序检验与空池/入池结论；见 [加密因子研究](CRYPTO-FACTOR-RESEARCH.md)。该流程由本地 Agent 编排、确定性程序计算，真实策略模型调用为 0。结果只用于开发研究，空池时应总结失败原因，不能直接追加无限搜索或进入模拟盘。因子到目标权重、组合消融和独立留出仍需后续策略验证。

## 配置与检查

需要支持 `--no-daemon` 及 `exec --ignore-user-config --ephemeral --output-schema --output-last-message` 的 Codex CLI。本机核对版本为 `0.162.0`；旧版本若缺少这些参数，需要先更新。登录由 Codex 管理，QuantBot 不读取、复制或保存登录凭据。

```toml
[llm]
backend = "codex_cli"
model = "填写本机 Codex 有权限使用的模型名称"
timeout_seconds = 300
fallback_models = []

[codex]
executable = "codex"
reasoning_effort = ""
```

保存为 `config/quantbot.codex.toml`，或将内容合入主配置 `config/quantbot.toml`。`executable` 是单个可执行文件名或绝对路径，不接受带参数的 shell 命令。`model` 必须显式填写；`reasoning_effort` 留空表示模型默认，也可填写该模型支持的强度。

```sh
.venv/bin/quantbot model-check --config config/quantbot.codex.toml
```

返回 JSON 中 `ready=true` 只表示配置、CLI 参数支持和本机登录检查通过；`model_access_verified=false` 明确表示尚未请求模型。调用实际模型会使用该 Codex 登录对应的权限与用量。当前聊天窗口临时选择的模型、上下文和推理强度不会自动继承。

为使输入可追溯，决策进程忽略用户 `config.toml` 的工具配置，仅使用 QuantBot 显式传入的模型和推理强度。自定义 Codex 网关、配置 profile 或其他模型供应方的继承不在本次接入范围，不能假设自动沿用。

## Agent 执行一次加密回测

已有 CSV 时：

```sh
.venv/bin/quantbot backtest --config config/quantbot.codex.toml \
  --market crypto --provider ai --data data/crypto.csv \
  --symbols BTC/USDT ETH/USDT --initial-cash 1000 \
  --last-bars 3 --allow-model-calls --output runs/codex-crypto-first
```

输出目录必须不存在。CSV 列为 `timestamp,symbol,open,high,low,close,volume,bar`，每个标的至少需要 `35 + last_bars - 1` 根完整连续行情（默认指标窗口）。支持 15 分钟、1 小时、UTC 日线。首轮 3 根回测 K 线只需 2 个决策时点，每个时点一次请求覆盖整个标的池；30 根对应 29 个时点。

没有 CSV、需要读取 OKX 公开行情时，使用现有候选入口：

```sh
.venv/bin/quantbot runtime --root runs/codex-crypto-runtime create \
  --config config/quantbot.codex.toml --name codex-crypto \
  --market crypto --provider ai --source okx_public --bar 1h \
  --symbols BTC/USDT ETH/USDT --initial-cash 1000

# 使用上一条返回的任务 ID。该步骤读取公开行情并调用模型。
.venv/bin/quantbot runtime --root runs/codex-crypto-runtime evaluate \
  <任务ID> --last-bars 3 --allow-model-calls
```

创建任务时保持暂停；评估结束也不会启动持续任务。公开行情不需要 OKX 账户密钥，仅读取最近区间，不是完整历史数据服务。

OKX 公开行情请求带 `QuantBot/<版本>` 客户端标识。2026-10-21 本机实测中，缺少该标识的请求被边缘服务拒绝；补齐后完成公开行情读取。该修复不涉及账户认证，也不能保证所有网络环境均可访问。

外层 Agent 应读取返回的报告，而不是根据命令退出码判断策略成功：

1. 检查 `execution_completed`、每个决策时点、错误和风控熔断；软件运行完整性不等于研究达标。
2. 比较候选、动量规则和现金基线；报告实际成交数量、成本、回撤及持仓。
3. 对候选和基准各运行一次 `quantbot runtime replay-show <日志路径> --reconcile`。
4. 保存数据来源、配置、实际模型、会话标识、供应方报告的用量和证据路径。
5. 若未获更大预算，完成报告后停止。失败不自动增加模型调用或绕过风控。

可直接交给本地 Agent 的任务示例：

> 使用本机 Codex 作为决策模型，对 BTC/USDT 和 ETH/USDT 的 1 小时公开行情完成一次回测。初始资金 1000 USDT，先检查配置，再执行 3 根回测 K 线，最多 2 个策略决策时点。保存报告并独立对账，解释与动量基准的差异。以回测结束。

## 候选迭代与研究

需要低成本排查策略假设时，可先使用 `backtest --market crypto --provider crypto_rules --config config/crypto-rules.example.toml --data <开发段CSV>`，或在 `runtime create` 中选择同一 provider。固定候选和持仓规则见 [加密规则研究](CRYPTO-RULES-RESEARCH.md)。该入口不调用模型；规则结果不能当作 Codex 决策收益。新增的 EMA、RSI、短期动量和前序通道特征也会出现在以后获授权的 AI 决策快照中。

迭代前登记有限候选、费用和淘汰规则；仅向筛选流程提供开发段数据。比较逐窗口结果及同一开发期的连续持仓表现，不能靠反复重置账户掩盖风险或仅挑盈利窗口。已看过的开发区间即使重新划分，也不变成独立留出。修改特征或规则后创建新版本，不覆盖旧研究记录，也不挪用已结束研究的剩余模型预算。

针对震荡和频繁重入，可使用 `config/crypto-rotation.example.toml` 研究 `trend_filtered` / `strength_rotation`。新增特征包括 10 根趋势效率与两根趋势确认；账户输入保留保护价和最近完全离场的 K 线时间。冷却与止盈止损由独立风控/执行器控制，不由模型自行取消或改写。费用、跳空及同根双触发规则见 [加密规则研究](CRYPTO-RULES-RESEARCH.md)。

Agent 可以使用已有 `runtime clone`、`walk-forward`、`study-create`、`study-run` 编排多个候选；研究卡片、预登记、开发区间、一次留出和压力测试沿用 [研究协议](AI-RESEARCH.md)。`model_request_budget` 限制策略调用数量，不能将其当作供应方实际 HTTP 请求或 token 上限；CLI 内部连接重试和未完成调用的用量可能未知。

模型名、强度、成本、风控等变更应创建新候选，不修改已冻结候选，也不能看完留出区间后反复调参再宣称样本外有效。当前 Agent 可以提出参数与提示词改进，但程序没有获得自动修改源代码、提高风险限额、无限迭代或部署交易的授权。

## 故障和证据

- 每次调用新建临时目录和独立会话；使用 `--no-daemon` 避免任务转入共享后台进程，不使用 `resume`，不传入未来行情、旧聊天、完整项目目录或账户密钥。
- 只读沙箱，关闭外部搜索、shell、插件、应用、记忆等功能；最终输出受 JSON Schema 和本地决策校验双重约束，出现工具事件就拒绝结果。
- CLI 0.162.0 会将“Code Mode 执行器已禁用，功能保持关闭”的固定启动提示表示成 `error` 条目。仅在模型回合开始前精确识别这条提示；其他警告、模型切换、工具及未知事件仍拒绝，不为消除提示开启执行器。
- 超时结束进程组；非零退出、缺失完成事件、空结果、非法 JSON、错误快照或标的均失败，不自动重试、换模型或切换演示。
- 外层运行进程需要访问 Codex 本地运行资源。明确的系统权限退出会报告“本机权限限制”；QuantBot 不自动提权。外层沙箱权限和内层 `codex exec --sandbox read-only` 是两层约束，前者初始化成功不意味着放宽后者。
- 成功决策中的 `provider=codex_cli`、`model`、`response_id`、`usage` 进入既有日志。用量缺失不补成零，失败后不要自行推断未计费。
- 不保存 CLI 推理文本和原始 stderr；保留可审计的最终结构化决策与程序错误。研究问答使用同一模型来源，完成后返回整段文本。

## 验证范围

2026-10-21 已完成真实 `gpt-6.1-sol` / `max` 的 BTC/ETH 历史回测：2 个决策时点、2 笔回测成交，候选与基准各 3 个账户时点独立对账无差异，成功调用报告 38,396 token。相关回归 54 项通过。前两次失败尝试与未知用量均保留，完整结果和边界见 [真实 Codex 回测验收](AI-CODEX-VALIDATION.md)。

2026-10-20 使用受控本地 CLI 进程验证协议、完整加密回测、网页入口、研究问答、错误与超时路径，并检查本机真实 CLI 版本和登录。该批验收没有发起真实 Codex 模型推理，也没有调用交易账户；不能用该批结果证明真实模型可运行。

2026-10-20 本机验收记录：

| 检查 | 结果 |
|---|---|
| 量化、回测、研究、运行与对账相关 Python 回归 | 167 项通过 |
| 研究 Agent 与模型路由回归 | 19 项通过 |
| 最终参数及配置校验修正后的定向复验 | 34 项通过，包含全部 14 项 Codex 接入用例、19 项研究用例及 1 项兼容接口 CLI 用例；与上述批次重叠，不重复累计 |
| 前端测试与生产构建 | 39 项通过，TypeScript / Vite 构建通过 |
| 网页资源同步 | 54 个打包资源与构建目录逐文件校验一致 |
| 改动范围静态检查 | Ruff 通过 |
| 真实本机接入检查 | `codex-cli 0.162.0`，`cli_compatible=true`，`authenticated=true`，`model_called=false`，`model_access_verified=false` |

本机主配置已设为 `codex_cli`、`gpt-6.1-sol`、推理强度 `max`，取自开发时本机的显式选择；这不是仓库默认模型，2026-10-20 的检查也未证明该模型的调用权限。未配置备用模型。最终复验时兼容接口测试使用单独的测试配置，避免读取本机主配置后调用错误的供应方。

复验命令（不会调用真实模型）：

```sh
PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m pytest \
  tests/test_codex_provider.py tests/stocks/agent/test_agent.py \
  tests/stocks/agent/test_router.py \
  tests/test_quantbot_ai.py::test_real_http_compatibility_transport_and_cli -q -p no:cacheprovider
```

调用方式依据 [OpenAI 官方非交互文档](https://learn.chatgpt.com/docs/non-interactive-mode)、[认证说明](https://learn.chatgpt.com/docs/auth) 和 [配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)，并与本机 CLI 帮助核对。
