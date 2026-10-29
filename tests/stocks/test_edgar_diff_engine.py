"""Tests for edgar/diff_engine.py."""
from __future__ import annotations

from datetime import date

import pytest

from quantbot.stocks.edgar.diff_engine import (
    ItemDiff,
    ParagraphDiff,
    _diff_paragraphs,
    _split_paragraphs,
    diff_8k_filings,
)
from quantbot.stocks.edgar.models import EightKFiling, EightKItem


# ── 段落分割测试 ──────────────────────────────────────────────────────────────

class TestSplitParagraphs:
    def test_splits_on_double_newline(self):
        text = "Paragraph one is here.\n\nParagraph two goes here."
        parts = _split_paragraphs(text)
        assert len(parts) == 2

    def test_filters_short_paragraphs(self):
        # < 20 chars should be filtered
        text = "Hi\n\nThis is a longer paragraph that passes the filter."
        parts = _split_paragraphs(text)
        assert len(parts) == 1
        assert "longer paragraph" in parts[0]

    def test_empty_text(self):
        assert _split_paragraphs("") == []

    def test_single_paragraph(self):
        parts = _split_paragraphs("A single paragraph with enough text here.")
        assert len(parts) == 1


# ── 段落差分测试 ──────────────────────────────────────────────────────────────

class TestDiffParagraphs:
    def test_identical_paragraphs_are_unchanged(self):
        para = "The company entered into a merger agreement on January 10, 2024."
        diffs = _diff_paragraphs([para], [para])
        assert len(diffs) == 1
        assert diffs[0].diff_type == "unchanged"
        assert diffs[0].similarity >= 0.95

    def test_added_paragraph(self):
        diffs = _diff_paragraphs([], ["This is a completely new paragraph added to the filing."])
        assert len(diffs) == 1
        assert diffs[0].diff_type == "added"
        assert diffs[0].old_text is None
        assert "completely new" in diffs[0].new_text  # type: ignore[arg-type]

    def test_removed_paragraph(self):
        diffs = _diff_paragraphs(["This paragraph was removed from the filing in the new version."], [])
        assert len(diffs) == 1
        assert diffs[0].diff_type == "removed"
        assert diffs[0].new_text is None

    def test_modified_paragraph(self):
        # Paragraphs sharing ~77% similarity (same structure, different names/dates/amounts)
        old_p = (
            "Pursuant to the Merger Agreement dated January 10, 2024, the Company agreed to "
            "acquire Widget Corp for $500 million in an all-cash transaction expected to "
            "close in Q2 of 2024."
        )
        new_p = (
            "Pursuant to the Amendment Agreement dated March 15, 2025, the Company agreed to "
            "acquire Gadget Holdings for $750 million in a cash-and-stock transaction expected "
            "to close in Q4 of 2025, subject to shareholder approval."
        )
        diffs = _diff_paragraphs([old_p], [new_p])
        assert len(diffs) == 1
        assert diffs[0].diff_type == "modified"
        assert 0.7 <= diffs[0].similarity < 0.95

    def test_empty_inputs(self):
        assert _diff_paragraphs([], []) == []


# ── item diff 测试 ────────────────────────────────────────────────────────────

class TestItemDiff:
    def test_has_material_change_added(self):
        item = ItemDiff(
            item_number="5.02",
            item_title="Director Changes",
            paragraphs=[ParagraphDiff("added", None, "New text here.", 0.0)],
        )
        assert item.has_material_change is True

    def test_has_material_change_removed(self):
        item = ItemDiff(
            item_number="5.02",
            item_title="Director Changes",
            paragraphs=[ParagraphDiff("removed", "Old text here.", None, 0.0)],
        )
        assert item.has_material_change is True

    def test_no_material_change_unchanged(self):
        item = ItemDiff(
            item_number="5.02",
            item_title="Director Changes",
            paragraphs=[ParagraphDiff("unchanged", "Text", "Text", 1.0)],
        )
        assert item.has_material_change is False

    def test_change_score_all_unchanged(self):
        item = ItemDiff(
            item_number="5.02",
            item_title="Director Changes",
            paragraphs=[ParagraphDiff("unchanged", "A", "A", 1.0)],
        )
        assert item.change_score == 0.0

    def test_change_score_all_added(self):
        item = ItemDiff(
            item_number="5.02",
            item_title="Director Changes",
            paragraphs=[ParagraphDiff("added", None, "new paragraph", 0.0)],
        )
        assert item.change_score == 1.0


# ── diff_8k_filings 集成测试 ──────────────────────────────────────────────────

def _make_filing(
    ticker: str,
    accession: str,
    filed_date: date,
    items: list[EightKItem],
) -> EightKFiling:
    return EightKFiling(
        ticker=ticker,
        cik="0000320193",
        accession_number=accession,
        filed_date=filed_date,
        period_of_report=None,
        items=items,
    )


class TestDiff8KFilings:
    def test_identical_filings_have_zero_change_score(self):
        item = EightKItem(
            item_number="5.02",
            item_title="Director Changes",
            text="The CEO resigned effective today.\n\nThe Board will conduct a search.",
        )
        old = _make_filing("AAPL", "ACC-001", date(2024, 1, 1), [item])
        new = _make_filing("AAPL", "ACC-002", date(2024, 3, 1), [item])
        diff = diff_8k_filings(old, new)
        assert diff.overall_change_score == 0.0
        assert not diff.has_material_change

    def test_new_item_detected(self):
        old = _make_filing("AAPL", "ACC-001", date(2024, 1, 1), [])
        new = _make_filing("AAPL", "ACC-002", date(2024, 3, 1), [
            EightKItem(
                item_number="5.02",
                item_title="Director Changes",
                text="This is entirely new content added to the latest filing.",
            )
        ])
        diff = diff_8k_filings(old, new)
        assert diff.overall_change_score > 0
        assert diff.has_material_change
        assert "5.02" in diff.changed_item_numbers

    def test_removed_item_detected(self):
        old = _make_filing("AAPL", "ACC-001", date(2024, 1, 1), [
            EightKItem("8.01", "Other Events", "Some important disclosure was made here.")
        ])
        new = _make_filing("AAPL", "ACC-002", date(2024, 3, 1), [])
        diff = diff_8k_filings(old, new)
        assert diff.has_material_change

    def test_ticker_mismatch_raises(self):
        old = _make_filing("AAPL", "ACC-001", date(2024, 1, 1), [])
        new = _make_filing("MSFT", "ACC-002", date(2024, 3, 1), [])
        with pytest.raises(ValueError, match="Ticker mismatch"):
            diff_8k_filings(old, new)

    def test_changed_item_numbers(self):
        old = _make_filing("AAPL", "ACC-001", date(2024, 1, 1), [
            EightKItem("5.02", "Director Changes", "Old CEO was John Smith."),
        ])
        new = _make_filing("AAPL", "ACC-002", date(2024, 3, 1), [
            EightKItem(
                "5.02",
                "Director Changes",
                "New CEO is Jane Doe, effective immediately upon board approval.",
            )
        ])
        diff = diff_8k_filings(old, new)
        assert isinstance(diff.changed_item_numbers, list)
