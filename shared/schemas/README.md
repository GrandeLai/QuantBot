# 共享数据契约

六份 JSON Schema 保留为行情、标的、因子、信号、回测输入和输出的研究契约。对应实现位于 `src/quantbot/common/schemas`、`quant-core/src/schemas` 和 `frontend/src/shared/types`。

旧 monorepo 的生成脚本已去除；其输出目录和工具环境不适用于统一项目。当前类型已冻结迁入，没有声称完成自动生成一致性检查。变更契约时需同步实际消费方并通过相关测试。
