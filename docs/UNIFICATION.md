# QuantBot 0.3.0 收敛说明

QuantBot 是唯一维护入口。PABot 的价格行为、StockBot 的多因子和执行账本、QuantPilot 的股票研究工作台、CoinBot 的加密策略统一到本项目。

## 合并与删除

| 原资产 | 统一归属 | 决定 |
|---|---|---|
| QuantPilot common / CoinBot common | `quantbot.common` | 采用股票研究所需的完整共享设施；券商类型与加密工具只保留一份 |
| QuantPilot stock | `quantbot.stocks` | 保留股票、期权、宏观、因子、风控研究与账户读取 |
| QuantPilot 旧 crypto / CoinBot crypto | `quantbot.crypto` | 加密代码归 CoinBot 所有者；去掉股票包内的重复模块和投顾处理函数 |
| CoinBot 新策略运行层 | `quantbot.crypto.runtime` | 保留 13 个策略、回测、验证、复盘和演化工具 |
| QuantPilot model agent | `quantbot.models` | 迁入路由、重试、熔断；结构化交易复用路由，研究与交易共用配置 |
| 多套 Python API 主程序 | `quantbot.web.app` | 一个 HTTP 服务；兼容旧查询路径和 `/api` 前缀 |
| 多套 React 前端 | `frontend` | 一个工作台；删除无调用方的第二套投顾卡片；运行中心改为统一决策与加密策略 |
| 两套 Rust 后端 | `quant-core` | 采用带 ONNX 预测接口的 QuantPilot 超集，只保留一个 crate |
| monorepo 生成脚本、旧运行目录 | 新目录与运行配置 | 删除失效生成脚本、没有对应后端的模型训练/AutoPilot 网页操作，输出移到 `runtime`；不迁入缓存、账户、密钥、旧账本或环境 |

JSON 血缘记录见 `unification-lineage.json`。原文件 SHA-256 说明来源，不意味着重命名后仍与原项目完全相同。核心新增行为有专门集成测试。

## 统一的范围

主模型设置为 `llm.base_url/model/api_key_env/fallback_models`。研究文本通过 LiteLLM 调用兼容接口；交易通过严格结构化协议返回权重，必须绑定指定快照和时间。两条路径共享设置与路由基础设施，只有交易目标经过专属风控后才能进入执行。研究文本中的 BUY/HOLD/SELL 不构成订单授权。

价格行为是一类输入和子策略。美股 AI 使用原 PABot 特征和 StockBot 因子；价格行为模式作为对照。加密 AI 现货使用完整 OHLCV、ATR、动量和趋势信息，不把整数股账户混作币量账户。

股票和加密资产共享决策语义，但数量、收盘时钟、成本、账户和撮合各自明确：

| 入口 | 数据与行为 | 边界 |
|---|---|---|
| `decide` / 网页美股计划 | 完整日线、整数股、确定性风控 | 生成计划，不提交订单 |
| `simulate` | 历史 AI 或规则决策，次日开盘成交、保护止损 | 历史回放，可能存在模型训练记忆影响 |
| `paper` | 固定 Alpaca Paper 地址、真实账户事实、恢复门禁 | 需要显式确认；本次没有连接真实 Paper 服务 |
| `crypto ai` | 离线 USDT 现货、Decimal 数量、严格 AI 决策 | 新建计划或本地 paper；不读取交易所账户、不发交易所订单 |
| 网页加密策略 | 13 个策略，最多 3 轮、仅 mock paper | 报价机会 `net_profit` 属估算；策略专属纸上成交另行标记 |
| CLI 加密 Demo 验证 | 原安全门禁和专属 OKX Demo 执行路径 | 需显式配置与环境变量；本次未作真实 Demo 验收 |
| Rust 研究内核 | 回测、指标、优化、Walk-Forward、ONNX | 研究证据，不代替交易恢复账本 |

网页的 `/trading/orders`、`/crypto/orders`、`/crypto/futures/orders` 等旧直接写入接口提前拒绝，富途、Longbridge、旧 OKX 适配器也停用其写入方法。旧 `agent execute-live` 入口拒绝执行。原 StockBot 研究与诊断工具继续保留历史契约，不因此获得交易准入。

未执行删除 GitHub 源仓库、改名远端仓库、推送或发布。源仓库继续用于溯源；若未来归档，应先验收真实模型与目标模拟券商。
