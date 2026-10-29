"""Form 4 集群信号引擎（Phase F.2）.

从 Form4Transaction 列表中检测**集群买入信号**：
  - 90 天窗口内 ≥2 个不同 insider 同向买入
  - 仅关键职位（CEO/CFO/Director 等）
  - 排除 10b5-1 预设计划单
  - 单笔金额 ≥ $10,000

盈利逻辑：内部人集群买入历史超额 6-10% / 180 日（Cohen et al. 2012）。

signal_strength 公式：
  role_bonus  = 0.3 if CEO or CFO in key_roles else 0.0
  count_score = min(1.0, (insider_count - 1) / 4)        # 2→0.25, 5+→1.0
  value_score = min(1.0, log10(total/$10k) / 3)           # $10k→0, $10M→1
  strength    = 0.4 × count_score + 0.3 × value_score + 0.3 × role_bonus
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from quantbot.stocks.edgar.models import Form4Transaction

# ── 职位匹配 ──────────────────────────────────────────────────────────────────

_KEY_ROLE_RE = re.compile(
    r"(CEO|CFO|President|Chairman|Chief\s+Executive|Chief\s+Financial|"
    r"Chief\s+Operating|Chief\s+Technology|Chief\s+Legal|"
    r"Director|VP|Vice\s+President|EVP|SVP|COO|CTO|CLO|General\s+Counsel)",
    re.IGNORECASE,
)

_SENIOR_ROLE_RE = re.compile(r"(CEO|CFO|Chief\s+Executive|Chief\s+Financial)", re.IGNORECASE)


def _is_key_insider(title: str) -> bool:
    """True 当且仅当职位属于关键内部人."""
    return bool(_KEY_ROLE_RE.search(title))


def _extract_role_label(title: str) -> str:
    """提取简短职位标签（如 CEO / CFO / Director），按优先级取最高职位."""
    # 按优先级从高到低逐一检测（不依赖 re.search 的 leftmost 语义）
    t = title.lower()
    if re.search(r"chief\s+executive|(?<!\w)ceo(?!\w)", t):
        return "CEO"
    if re.search(r"chief\s+financial|(?<!\w)cfo(?!\w)", t):
        return "CFO"
    if re.search(r"chief\s+operating|(?<!\w)coo(?!\w)", t):
        return "COO"
    if re.search(r"chief\s+technology|(?<!\w)cto(?!\w)", t):
        return "CTO"
    if re.search(r"chief\s+legal|(?<!\w)clo(?!\w)", t):
        return "CLO"
    if re.search(r"general\s+counsel", t):
        return "General Counsel"
    if re.search(r"(?<!\w)president(?!\w)", t):
        return "President"
    if re.search(r"(?<!\w)chairman(?!\w)", t):
        return "Chairman"
    if re.search(r"(?<!\w)evp(?!\w)|executive\s+vice\s+president", t):
        return "EVP"
    if re.search(r"(?<!\w)svp(?!\w)|senior\s+vice\s+president", t):
        return "SVP"
    if re.search(r"(?<!\w)vp(?!\w)|vice\s+president", t):
        return "VP"
    if re.search(r"(?<!\w)director(?!\w)", t):
        return "Director"
    m = _KEY_ROLE_RE.search(title)
    return m.group(0).strip() if m else title[:20]


# ── 数据模型 ──────────────────────────────────────────────────────────────────


@dataclass
class InsiderCluster:
    """一个集群买入信号（90 天窗口内多位内部人同向买入）."""

    ticker: str
    window_start: date
    window_end: date
    insider_count: int                        # 涉及几位不同 insider
    total_value: float                        # 所有买入总金额（$）
    avg_price: float                          # OI-加权平均成交价（$）
    transactions: list[Form4Transaction] = field(default_factory=list)
    signal_strength: float = 0.0             # 0-1
    key_roles: list[str] = field(default_factory=list)  # CEO/CFO/Director 等


# ── 工具函数 ──────────────────────────────────────────────────────────────────


def _compute_signal_strength(
    insider_count: int,
    total_value: float,
    key_roles: list[str],
) -> float:
    """计算集群信号强度（0-1）."""
    # count_score: 2 insiders → 0.25, 5+ → 1.0
    count_score = min(1.0, (insider_count - 1) / 4.0)

    # value_score: $10k → 0, $10M → 1 (log scale)
    if total_value <= 0:
        value_score = 0.0
    else:
        value_score = min(1.0, math.log10(max(1.0, total_value / 10_000.0)) / 3.0)

    # role_bonus: CEO 或 CFO 出现时加成
    has_senior = any(
        bool(_SENIOR_ROLE_RE.search(r)) for r in key_roles
    )
    role_bonus = 0.3 if has_senior else 0.0

    return round(0.4 * count_score + 0.3 * value_score + 0.3 * role_bonus, 4)


# ── 公共 API ──────────────────────────────────────────────────────────────────


def detect_clusters(
    transactions: list[Form4Transaction],
    *,
    window_days: int = 90,
    min_insiders: int = 2,
    min_single_value: float = 10_000.0,
) -> list[InsiderCluster]:
    """从 Form4Transaction 列表中检测集群买入信号.

    过滤条件：
      1. transaction_type == "P" (Purchase)
      2. is_10b5_1_plan == False
      3. _is_key_insider(insider_title)
      4. total_value >= min_single_value

    集群定义：
      在任意 window_days 天窗口内，≥ min_insiders 位不同 insider 满足上述条件。

    算法：
      - 以每笔交易日期为窗口右端，向前看 window_days 天
      - 取该窗口内所有符合条件的交易，去重计 insider 数

    Args:
        transactions:      Form4Transaction 列表（任意顺序）
        window_days:       滚动窗口（天数），默认 90
        min_insiders:      最少不同 insider 数，默认 2
        min_single_value:  单笔最低金额（$），默认 $10,000

    Returns:
        InsiderCluster 列表，按 window_end 倒序
    """
    # 过滤
    valid: list[Form4Transaction] = [
        t for t in transactions
        if t.transaction_type == "P"
        and not t.is_10b5_1_plan
        and _is_key_insider(t.insider_title)
        and t.total_value >= min_single_value
    ]

    if len(valid) < min_insiders:
        return []

    # 按日期排序（新→旧）
    valid.sort(key=lambda t: t.transaction_date, reverse=True)

    clusters: list[InsiderCluster] = []
    seen_windows: set[tuple[date, date]] = set()

    for i, anchor in enumerate(valid):
        window_end = anchor.transaction_date
        window_start = window_end - timedelta(days=window_days)

        if (window_start, window_end) in seen_windows:
            continue
        seen_windows.add((window_start, window_end))

        # 找窗口内所有交易
        window_txns = [
            t for t in valid
            if window_start <= t.transaction_date <= window_end
        ]

        # 去重 insider（按 name）
        unique_insiders: dict[str, list[Form4Transaction]] = {}
        for t in window_txns:
            unique_insiders.setdefault(t.insider_name, []).append(t)

        if len(unique_insiders) < min_insiders:
            continue

        total_value = sum(t.total_value for t in window_txns)
        total_shares = sum(t.shares for t in window_txns)
        avg_price = total_value / total_shares if total_shares > 0 else 0.0

        key_roles = sorted({
            _extract_role_label(t.insider_title)
            for t in window_txns
        })

        strength = _compute_signal_strength(
            len(unique_insiders), total_value, key_roles
        )

        clusters.append(InsiderCluster(
            ticker=anchor.ticker.upper(),
            window_start=window_start,
            window_end=window_end,
            insider_count=len(unique_insiders),
            total_value=total_value,
            avg_price=avg_price,
            transactions=window_txns,
            signal_strength=strength,
            key_roles=key_roles,
        ))

    # 去重：按 (window_end, insider_count) 去除完全重叠的集群
    # 保留每个 window_end 中 insider_count 最大的
    best: dict[date, InsiderCluster] = {}
    for c in clusters:
        if c.window_end not in best or c.insider_count > best[c.window_end].insider_count:
            best[c.window_end] = c

    return sorted(best.values(), key=lambda c: c.window_end, reverse=True)
