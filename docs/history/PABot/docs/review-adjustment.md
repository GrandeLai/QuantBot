# 价格行为复盘与调整制度

## 1. 不可变宗旨

PABot 的方向性决策只能来自已完成 K 线上的价格行为。价格行为包括：摆动
高低点、趋势结构、通道突破、回调与反转 K 线、ATR/波动率、量价流动性，
以及由这些价格数据派生的大盘状态。

风险与执行层可以拒绝、减仓、撤单或冻结交易，但不能借风控或恢复之名
创建方向、反向持仓或增加绝对风险暴露。财报日期、新闻、基本面、主观判断
不允许成为生产入场信号。

代码将这条宗旨落实为三道边界：

1. `DecisionIntent`：任何增加暴露的意图必须来自 `PRICE_ACTION` 并携带证据；
2. `validate_exposure_change`：风险覆盖只能减仓，执行恢复不得增加暴露；
3. `load_config(..., production=True)`：生产入口拒绝非价格行为或已证伪模块。

## 2. 复盘事实源

`review.journal_path` 是 SQLite 账本路径模板；默认实际路径为
`logs/runtime/<mode>/<账户SHA256>/trading_journal.sqlite`。状态、锁、缓存和报告
使用相同分区规则。它使用 WAL、完整同步、外键和禁止
UPDATE/DELETE 的触发器。CSV 仅是兼容导出物，不是复盘事实源。

每日 runner 会记录：

- 运行日期、模式、配置哈希、源码版本和完整行情输入哈希；
- 每个候选的接受/拒绝原因与当时可知的价格行为特征；
- 下单前的订单意图、确定性 `client_order_id`、券商订单号与终态；
- 实际成交、滑点、建仓/加仓/减仓/平仓、止损移动；
- 执行时刻权益、现金、购买力和持仓数（`phase=execution`）。

正式 Paper 在当天收盘后用独立 `paper-close` 记录收盘后实际账户快照和
`MATCH / DRIFT / UNKNOWN` 三态对账。开盘记录不能充当收盘证据。

同一业务幂等键重复写入相同内容会返回同一 UUID；重复键对应不同内容会
失败。订单在调用券商前先落 `planned` 事件，进程在提交后崩溃时会以同一个
客户端订单号只读恢复，不猜测性重发。

每次 paper/live/sim 启动时，系统在行情刷新和任何交易写入之前检查所有
未决账本订单及券商工作订单。部分成交、状态未知、外部订单、券商读取失败，
`filled` 后尚未记录 `state_committed` 的成交，以及本地状态文件与账本中
最近 SHA-256 指纹不一致都会进入 `HOLD`。
状态指纹写入独立的只追加 checkpoint 表，以 SQLite AUTOINCREMENT 序号判断
先后，避免 NTP 或人工调时后回退到旧指纹。订单交接与 checkpoint 同事务提交；
文件或父目录 fsync 失败时不会写入交接事实。
`--force` 只影响日期/当日补跑判断，不能绕过恢复门禁。系统不会自动补单、
撤单或篡改历史事实；事实未判清前，冻结比“猜一个最可能状态”更安全。

## 3. 日常复盘

```bash
# 生成账本内最近交易日的中文复盘
.venv/bin/python main.py review --mode paper

# 指定交易日和输出文件
.venv/bin/python main.py review --mode paper --date 2026-08-07 \
  --output reports/review/2026-08-07.md

# 强制刷新实盘缓存后，对理论与券商的标的、方向、准确股数进行对账
.venv/bin/python main.py reconcile --mode paper
```

`UNKNOWN` 不是空仓，也不是一致。券商、行情或回测任一侧不可用时，对账
必须返回 `UNKNOWN` 并告警，不允许使用 `live_state.json` 冒充券商事实。
本地模拟盘使用 `--mode sim`；多个 Paper 账户时，复盘和报告要求传匿名
`--account-fingerprint`。影子对账只分析模型偏差，不能替代 Paper 收盘对账。

日报用于回答四个问题：

1. 当天使用的数据和代码能否定位、行情是否新鲜；
2. 信号为什么产生，候选为什么被拒绝；
3. 订单是否按计划成交，是否存在部分成交、未知状态或异常滑点；
4. 持仓和权益如何变化，券商事实是否与理论一致。

缺少记录只能解释为“证据不足”，不能解释为“没有异常”。

### 3.1 正式 Paper campaign

普通 Paper 运行用于联调，不自动获得发布证据资格。正式取证必须先冻结：

- campaign ID、候选配置哈希、源码哈希；
- 匿名账户指纹（只保存 SHA-256，不保存账户号或密钥）；
- 来自权威 NYSE 日历、经人工复核的执行日/精确信号日序列；
- 最低连续合格交易日数。

```bash
.venv/bin/python main.py paper-account-fingerprint
.venv/bin/python main.py paper-campaign-init \
  --config reports/governance/candidates/<candidate-config-hash>/candidate.config.json \
  --campaign-id pa-breakout-60-v1 \
  --account-fingerprint <匿名SHA256> \
  --trading-dates trading-dates.json
.venv/bin/python main.py paper \
  --config reports/governance/candidates/<candidate-config-hash>/candidate.config.json \
  --campaign reports/governance/paper/pa-breakout-60-v1.json
# 当天实际收盘后单独执行
.venv/bin/python main.py paper-close \
  --config reports/governance/candidates/<candidate-config-hash>/candidate.config.json \
  --campaign reports/governance/paper/pa-breakout-60-v1.json
.venv/bin/python main.py paper-audit \
  --config reports/governance/candidates/<candidate-config-hash>/candidate.config.json \
  --campaign reports/governance/paper/pa-breakout-60-v1.json
```

日期文件是对象数组，例如：

```json
[
  {"execution_date": "2026-08-10", "signal_date": "2026-08-07"},
  {"execution_date": "2026-08-11", "signal_date": "2026-08-10"}
]
```

每个执行日使用绑定 campaign 完整 SHA-256 的独立 `attempt_id`，并把真实执行日
与前一交易日的信号日分开。后一项信号日必须等于前一项执行日，正式 campaign
禁止 `--force`。
当天必须有完成的运行、闭合订单/成交链、收盘权益，以及持仓、工作订单、账户
三类且仅一条 `MATCH` 对账。旧版账本、缺日、重复/冲突记录、未知状态或身份
漂移均不合格。最终证据只统计序列末尾连续合格日期，不能从历史中挑选 60 天。

执行时用 Alpaca calendar 核对当日及前一交易日并冻结带时区的开收盘时间。
`paper-close` 只在该交易日实际收盘后读取事实，再以一个事务追加权益和三类
对账。审计同时检查实际观察时间、账本写入时间、相同会话、账户和 attempt。
提前收市按日历判断，过了美东午夜不得补采前日，不能通过指定日期或 `--force`
回填。首次 UNKNOWN/DRIFT 会保留，重跑不能覆盖；存储失败没有完成的事务可重试。
源码变更后应冻结新的候选与 campaign，不能把旧源码的天数拼入新版本。

## 4. 调整流程

参数调整禁止直接修改生产配置后观察结果。标准流程固定为：

```text
复盘发现可重复问题
  → 预注册一个可证伪、单变量价格行为假设
  → 同一冻结快照上双跑基线与候选
  → 配对统计 / Walk-forward / 压力窗口 / 完整测试
  → Shadow（不下单）
  → Paper 前瞻观察至少 60 个交易日
  → 人工签署 ReleaseManifest
  → 才允许手工更新生产配置
```

### 4.1 预注册

```bash
.venv/bin/python main.py hypothesis-init \
  --hypothesis-id h-breakout-60 \
  --title "延长突破确认" \
  --rationale "只改变前高突破窗口，检验更严格的价格结构是否改善样本外收益" \
  --change strategy.breakout_window=60
```

命令会冻结基线/候选配置哈希、数据快照、唯一改动、主指标、效应量门槛和
失败条件；不会修改 `config.yaml`，目标文件存在时也不会覆盖。

一次实验只能改一个白名单标量。数据源、成本假设、券商模式、财报过滤、
时间止损、做空、底仓和杠杆不能借实验入口进入生产候选。

### 4.2 执行实验

```bash
.venv/bin/python main.py experiment \
  --spec reports/governance/hypotheses/h-breakout-60.json
```

正式入口会先逐文件校验快照 SHA-256，再在同一个数据对象和完全相同日期
窗口上运行基线与候选。实验 JSON 原子写入且禁止覆盖，包含：

- 假设、唯一改动、配置哈希和源码指纹；
- 快照 ID、manifest 哈希、文件数和全量校验状态；
- 基线/候选指标、差值和配对 block-bootstrap；
- 后续 Walk-forward、压力、测试、Shadow、Paper 证据状态。

离线双跑成功不代表可以发布。后续证据默认全部为失败，必须由独立流程
补齐，禁止把“尚未验证”写成“通过”。

### 4.3 候选包

实验通过离线条件后，先生成不可覆盖候选包：

```bash
.venv/bin/python main.py candidate-bundle \
  --experiment-artifact reports/governance/experiments/<experiment-id>.json
.venv/bin/python main.py candidate-verify \
  --bundle reports/governance/candidates/<candidate-config-hash>
```

候选包精确保存候选配置、当前基线回滚配置、原始实验 JSON、源码身份、快照
身份及每个文件的字节 SHA-256。生成和发布都采用临时目录加原子改名，目标
存在即拒绝覆盖。验证会重新计算所有文件、配置语义、当前源码和完整快照；
Git 无干净 HEAD 时可以产出审计包，但 `eligible=false`。该流程从不写
`config.yaml`，也不提供自动 apply 命令。

### 4.4 发布门禁

候选必须同时满足：

- 属于价格行为宗旨，所有契约和候选配置哈希闭环；
- 已验证候选包完整，候选/回滚配置、实验、源码和快照身份全部闭环；
- 源码是干净 Git HEAD，发布清单绑定该 HEAD 与候选包摘要；
- 冻结快照在发布时再次全量校验，manifest SHA-256 与实验、候选包一致；
- 候选相对基线 CAGR 至少改善 0.5 个百分点；
- 配对 `p < 0.10`，90% 区间上下界均大于 0；
- Walk-forward、压力测试、完整测试、Shadow 全部通过；
- `PaperEvidenceSummary` 与冻结 campaign 哈希闭环，末尾连续至少 60 个
  合格交易日，且 observation 发生在实验之后、发布清单生效之前；
- 发布清单绑定源码指纹、回滚配置哈希和明确人工批准人。

`evaluate_release` 会从候选包路径重新执行只读验证，以包内解析出的
`HypothesisSpec/ExperimentResult` 为唯一阈值与统计来源，并从 SQLite 原始账本重新运行 Paper
campaign 审计；不接受调用方提供的“已验证对象”或 Paper 摘要。它是只读门禁，
不会写生产配置；最终配置更新始终是一个显式、可审计的人工动作。

## 5. 当前阻断项（2026-08-09）

现有 `20260726/20260727` 缺少新版 `manifest_version`，当前读取入口会直接
拒绝；即使按旧 v1 解释，二者 `sectors.json` 的实际 SHA-256 也与 manifest
不一致。旧报告可用于解释历史结论，但两份快照不能再用于回测、新实验或发布。

正确处理方式是从可信缓存重新创建快照并重新冻结基线。禁止修改 manifest
去迎合现状，也禁止关闭全量校验。

此外，仓库当前尚无 Git commit/remote。源码哈希可以定位本地代码，但在建立
首个版本基线前，仍不具备常规的审计、回滚与部署复现能力。

现有账本是在 campaign/attempt/account 证据字段落地前产生，不能追认为正式
Paper 天数；当前也尚无完整 campaign 证据摘要。启动恢复已能 fail-closed，
但未知订单的人工判定与解除 HOLD 流程仍需单独实现，不能靠删除账本或使用
`--force` 绕过。

最后，系统会严格执行用户提供的冻结交易日，但尚未内置权威 NYSE 日历来源
证明。正式 campaign 的 `trading-dates.json` 必须从权威日历导出并人工复核。

Walk-forward、压力测试、完整测试和 Shadow 尚未绑定不可覆盖的原始产物；
候选包校验固定返回 `forward_evidence_verified=false`，所以这些字段即使填写为
通过，发布门禁仍会拒绝。下一轮需要把四类事实做成内容寻址证据包，并由发布
入口从原始文件重新验证。
