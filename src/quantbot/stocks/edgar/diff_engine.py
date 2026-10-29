"""8-K 文本差分引擎（Phase F.2）.

对同一公司前后两份 8-K 在 item 维度做段落级文本差分，
使用 rapidfuzz.fuzz.ratio 做相似度匹配，识别新增/删除/修改段落。

公式约定：
  similarity ≥ 0.95 → unchanged
  0.70 ≤ similarity < 0.95 → modified
  no match ≥ 0.70 → added (in new) / removed (in old)

change_score = weighted average of (1 - similarity) across all paragraph pairs
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from rapidfuzz import fuzz

from quantbot.stocks.edgar.models import EightKFiling

# ── 阈值 ─────────────────────────────────────────────────────────────────────

_UNCHANGED_THRESHOLD = 0.95   # similarity ≥ this → unchanged
_MATCH_THRESHOLD = 0.70       # similarity ≥ this → modified (not new/deleted)

# ── 数据模型 ─────────────────────────────────────────────────────────────────


@dataclass
class ParagraphDiff:
    """单段落差分结果."""

    diff_type: Literal["added", "removed", "modified", "unchanged"]
    old_text: str | None    # "removed" / "modified" 时有值
    new_text: str | None    # "added" / "modified" 时有值
    similarity: float       # 0.0 = 新增/删除；0-1 = 修改程度


@dataclass
class ItemDiff:
    """单 item 的完整差分结果."""

    item_number: str
    item_title: str
    paragraphs: list[ParagraphDiff] = field(default_factory=list)

    @property
    def has_material_change(self) -> bool:
        """True 当且仅当有新增、删除段落，或修改段落相似度 < 0.85."""
        for p in self.paragraphs:
            if p.diff_type in ("added", "removed"):
                return True
            if p.diff_type == "modified" and p.similarity < 0.85:
                return True
        return False

    @property
    def change_score(self) -> float:
        """0-1，段落差异程度加权平均；0 = 完全相同，1 = 完全不同."""
        if not self.paragraphs:
            return 0.0
        total = 0.0
        for p in self.paragraphs:
            if p.diff_type in ("added", "removed"):
                total += 1.0
            elif p.diff_type == "modified":
                total += 1.0 - p.similarity
            # unchanged → 0
        return total / len(self.paragraphs)


@dataclass
class EightKDiff:
    """两份 8-K 之间的完整差分快照."""

    ticker: str
    old_accession: str
    new_accession: str
    old_filed_date: date
    new_filed_date: date
    item_diffs: list[ItemDiff] = field(default_factory=list)

    @property
    def overall_change_score(self) -> float:
        """所有 item change_score 的最大值（对应单 item 最大变化程度）."""
        if not self.item_diffs:
            return 0.0
        return max(d.change_score for d in self.item_diffs)

    @property
    def has_material_change(self) -> bool:
        return any(d.has_material_change for d in self.item_diffs)

    @property
    def changed_item_numbers(self) -> list[str]:
        return [d.item_number for d in self.item_diffs if d.has_material_change]


# ── 内部工具 ──────────────────────────────────────────────────────────────────

_PARA_SEP = re.compile(r"\n{2,}|\r\n{2,}")


def _split_paragraphs(text: str) -> list[str]:
    """将 item 文本按空行分割为段落，过滤过短的噪音段落（< 20 字符）."""
    parts = _PARA_SEP.split(text)
    return [p.strip() for p in parts if len(p.strip()) >= 20]


def _similarity(a: str, b: str) -> float:
    """计算两段文本相似度（0-1）."""
    return fuzz.ratio(a, b) / 100.0


def _diff_paragraphs(old_paras: list[str], new_paras: list[str]) -> list[ParagraphDiff]:
    """贪心对齐算法：O(n×m)，item 内段落数一般 < 50.

    对每个 old 段落，找 new 中相似度最高的未匹配段落；
    反之亦然，找孤立的 new 段落为 added。
    """
    if not old_paras and not new_paras:
        return []

    # 如果一方为空
    if not old_paras:
        return [ParagraphDiff("added", None, p, 0.0) for p in new_paras]
    if not new_paras:
        return [ParagraphDiff("removed", p, None, 0.0) for p in old_paras]

    used_new: set[int] = set()
    result: list[ParagraphDiff] = []

    for old_p in old_paras:
        best_sim = 0.0
        best_j = -1
        for j, new_p in enumerate(new_paras):
            if j in used_new:
                continue
            sim = _similarity(old_p, new_p)
            if sim > best_sim:
                best_sim = sim
                best_j = j

        if best_j >= 0 and best_sim >= _MATCH_THRESHOLD:
            used_new.add(best_j)
            if best_sim >= _UNCHANGED_THRESHOLD:
                result.append(ParagraphDiff("unchanged", old_p, new_paras[best_j], best_sim))
            else:
                result.append(ParagraphDiff("modified", old_p, new_paras[best_j], best_sim))
        else:
            result.append(ParagraphDiff("removed", old_p, None, 0.0))

    # 剩余未匹配的 new 段落 → added
    for j, new_p in enumerate(new_paras):
        if j not in used_new:
            result.append(ParagraphDiff("added", None, new_p, 0.0))

    return result


# ── 公共 API ──────────────────────────────────────────────────────────────────


def diff_8k_filings(old: EightKFiling, new: EightKFiling) -> EightKDiff:
    """对两份 8-K 做 item-level 差分，返回 EightKDiff.

    如果两份 8-K 属于不同 ticker，则抛出 ValueError。
    item 按 item_number 对齐；old 中有而 new 中无的 item → 全删除，反之全新增。

    Args:
        old: 较旧的 EightKFiling（时间在前）
        new: 较新的 EightKFiling（时间在后）

    Returns:
        EightKDiff（含逐 item 的 ParagraphDiff 列表）
    """
    if old.ticker.upper() != new.ticker.upper():
        raise ValueError(
            f"Ticker mismatch: {old.ticker} vs {new.ticker}"
        )

    old_items = {item.item_number: item for item in old.items}
    new_items = {item.item_number: item for item in new.items}
    all_item_numbers = sorted(set(old_items) | set(new_items))

    item_diffs: list[ItemDiff] = []
    for num in all_item_numbers:
        old_item = old_items.get(num)
        new_item = new_items.get(num)

        title = (new_item or old_item).item_title  # type: ignore[union-attr]

        if old_item is None:
            # 全新 item
            new_paras = _split_paragraphs(new_item.text)  # type: ignore[union-attr]
            diffs = [ParagraphDiff("added", None, p, 0.0) for p in new_paras]
        elif new_item is None:
            # item 被完整删除
            old_paras = _split_paragraphs(old_item.text)
            diffs = [ParagraphDiff("removed", p, None, 0.0) for p in old_paras]
        else:
            old_paras = _split_paragraphs(old_item.text)
            new_paras = _split_paragraphs(new_item.text)
            diffs = _diff_paragraphs(old_paras, new_paras)

        item_diffs.append(ItemDiff(
            item_number=num,
            item_title=title,
            paragraphs=diffs,
        ))

    return EightKDiff(
        ticker=old.ticker.upper(),
        old_accession=old.accession_number,
        new_accession=new.accession_number,
        old_filed_date=old.filed_date,
        new_filed_date=new.filed_date,
        item_diffs=item_diffs,
    )
