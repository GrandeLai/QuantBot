"""EDGAR 数据模块 — SEC 8-K/Form 4 事件流（Phase F.2）.

公开接口：
  models      — 核心数据类型
  client      — EDGAR REST API 客户端
  diff_engine — 8-K 文本差分
  form4_engine— Form 4 集群信号
"""
from quantbot.stocks.edgar.models import (
    EightKFiling,
    EightKItem,
    Form4Transaction,
)

__all__ = [
    "EightKFiling",
    "EightKItem",
    "Form4Transaction",
]
