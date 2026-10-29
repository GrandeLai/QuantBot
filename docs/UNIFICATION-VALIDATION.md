# QuantBot 0.3.0 合并验收

完成日期：2026-10-07。已安装目录：`/Users/bytedance/code/QuantBot`。

QuantPilot、CoinBot 已融合到现有 QuantBot；PABot、StockBot 继续由统一项目承接。主包、主命令、网页和 Rust 内核各一套，共 550 个业务 Python 模块。股票与加密现货使用同一大模型设置、目标权重协议和独立风控，账户精度与撮合分别明确。

## 已验证

| 检查 | 结果 |
|---|---|
| Python 完整回归 | 3197 passed、471 subtests passed，0 失败，228.44 秒 |
| 末轮模型、API、加密 paper 集成 | 106 passed |
| Rust 全套与文档测试 | 62 passed |
| 网页测试 | 32 passed |
| TypeScript + 网页构建 | 通过 |
| Rust release 构建 | 通过 |
| 合并/新增模块 Ruff | 通过 |
| 安装版本及依赖一致性 | QuantBot 0.3.0；pip check 通过 |
| 原 QuantBot 源文件覆盖前检查 | 355 个文件指纹全吻合 |

实际网页验收了美股离线计划、加密现货本地 paper、13 个策略目录和一轮规则策略 paper。真实本机 HTTP 验收了统一服务转发 Rust SMA 与 ONNX 示例模型；ONNX `[1,2]` 示例输出约 1.2。这些属于接口与流程验证。

已安装命令额外完成了股票离线回放（34 个决策日、26 次模拟成交、0 次真实模型调用）和加密 Decimal paper（BTC 数量 0.0039）。原 PABot/StockBot 保持溯源指纹；源目录与 GitHub 仓库未删除。

末轮修复保证网页与 CLI 对同一 Alpaca Paper 账户使用同一主运行目录和账户账本；网页模拟资金字段不能代替券商账户事实。Rust 模型目录不再绑定构建机器的绝对源码路径。

## 精简结果

- 合并 common、数据路由、券商类型和加密工具；股票包不再拥有重复的 crypto 模块。
- 删除多套独立主程序、重复网页卡片、失效 monorepo 生成脚本，以及公开源码中没有后端的模型训练/全流程 AutoPilot 网页操作。
- 旧直接下单 HTTP 入口和旧券商适配器写入路径停用；旧 `agent execute-live` 入口拒绝执行。
- 策略报价机会收益明确标记 `estimated_opportunity`；没有把它当实际成交收益。

## 验证边界

没有调用真实大模型推理、Alpaca Paper 或 OKX Demo，也没有实盘成交。模型协议与切换通过本机模拟 HTTP 服务验证；演示和历史回放不是前瞻收益证据。第三方研究数据服务与可选深度学习依赖没有逐一作真实联调。历史代码的风格告警没有批量改写，本次相关模块静态检查已通过。

没有推送 GitHub、创建或归档远端仓库。0.2.0 源码包继续保留为回退备份。

## 使用

```sh
cd /Users/bytedance/code/QuantBot
.venv/bin/quantbot serve
# 打开 http://127.0.0.1:8001
.venv/bin/quantbot core
```

接入模型、CSV 和各模拟路径的说明见 README。价格行为仍属于 AI 的行情证据/子策略，也可以独立作为规则对照。
