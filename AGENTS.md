# QuantBot 协作约定

- 使用简体中文。不使用 CCM Harness。
- 主包是 `quantbot`，主命令是 `quantbot`，网页源只有 `frontend`，Rust 内核只有 `quant-core`。
- 主模型配置来自 `config/quantbot.toml` 与明确的环境变量；不要自动读取密钥文件或隐式启用演示模型。
- AI 只提出目标持仓；保持严格输出验证、独立风控和市场专属执行精度。
- 美股整数股执行与加密现货 Decimal 执行分别维护。不要用历史收益、报价机会估算或合成回放冒充实际成交收益。
- 旧直接下单入口已停用。没有用户明确授权，不推送、删除或归档源仓库，不调用实际模型和券商执行接口。
- 测试使用 `python -m pytest -q`、`frontend` 中的 `npm test` / `npm run build` 和 `cargo test --workspace`。只对改动范围执行静态检查；不顺手改动历史代码风格。

## Agent 驱动量化

- 用户要求 Agent 运行量化研究时，先阅读 `docs/AI-CODEX-AGENT.md`，复用 `model-check`、`backtest` 和 `runtime` 命令及其 JSON 输出。
- `llm.backend = "codex_cli"` 使用本机 Codex 登录和 QuantBot 中显式冻结的模型；外层 Agent 编排与内层独立策略决策分开。
- 当前以回测和独立对账结束。依照用户明确的本轮模型调用预算执行，保留失败与证据；不要把安装或登录检查当作真实模型运行证明。
