"""EDGAR 数据模型（Phase F.2）.

核心类型：
  EightKItem        — 8-K 单个 item 段落
  EightKFiling      — 完整 8-K 提交记录
  Form4Transaction  — Form 4 内部人交易记录
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class EightKItem:
    """8-K 单个 item（如 Item 5.02 Director Changes）."""

    item_number: str   # "1.01", "5.02", "8.01"
    item_title: str    # "Entry into a Material Definitive Agreement"
    text: str          # 清洗后的纯文本（去 HTML 标签）


@dataclass
class EightKFiling:
    """完整的 8-K 提交记录."""

    ticker: str
    cik: str                    # 零填充 10 位，如 "0000320193"
    accession_number: str       # "0001234567-24-000001"（原始破折号格式）
    filed_date: date
    period_of_report: date | None
    items: list[EightKItem] = field(default_factory=list)
    raw_html_url: str = ""      # 主文档下载 URL


@dataclass
class Form4Transaction:
    """Form 4 内部人单笔交易记录."""

    ticker: str
    cik: str                    # 发行人 CIK
    insider_name: str
    insider_title: str          # "Chief Executive Officer" 等
    transaction_date: date
    transaction_type: str       # "P" = Purchase, "S" = Sale, "A" = Award
    shares: float               # 交易股数（正数）
    price_per_share: float      # 成交均价（$）
    total_value: float          # shares × price_per_share
    is_10b5_1_plan: bool        # True → 计划性预设交易（预测价值低）
    accession_number: str
