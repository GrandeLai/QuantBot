"""Qualitative LLM signal filter for earnings and event risk."""

import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Set


NEGATIVE_KEYWORDS = {
    "guidance cut",
    "guidance lowered",
    "negative_flag",
    "material weakness",
    "sec investigation",
}


@dataclass(frozen=True)
class QualitativeSignal:
    sentiment_score: float
    flag_keywords: Sequence[str]
    confidence: str = "unknown"


def filter_by_qualitative_signals(
    tickers: Iterable[str],
    signals: Mapping[str, QualitativeSignal],
    min_sentiment: float = -0.3,
):
    selected = []
    for ticker in tickers:
        signal = signals.get(ticker)
        if signal is None:
            selected.append(ticker)
            continue
        if signal.sentiment_score <= min_sentiment:
            continue
        lowered_flags = {flag.lower() for flag in signal.flag_keywords}
        if lowered_flags.intersection(NEGATIVE_KEYWORDS):
            continue
        selected.append(ticker)
    return selected


def score_earnings_call(
    transcript: str,
    llm_client,
    max_chars: int = 3000,
) -> QualitativeSignal:
    excerpt = transcript[:max_chars]
    prompt = (
        "Analyze this earnings call transcript and return ONLY JSON with "
        "sentiment_score or sentiment between -1 and 1, guidance, confidence, and red_flags. "
        "Transcript excerpt: %s"
    ) % excerpt
    payload = json.loads(_extract_json_object(str(_complete(llm_client, prompt))))
    sentiment = max(-1.0, min(1.0, float(payload.get("sentiment_score", payload.get("sentiment", 0.0)))))
    flags = [
        str(flag).strip()
        for flag in _as_list(payload.get("red_flags", payload.get("flag_keywords", [])))
        if str(flag).strip()
    ]
    guidance = str(payload.get("guidance", payload.get("guidance_change", ""))).strip().lower()
    if guidance in {"lowered", "lower", "cut", "negative", "down"}:
        flags.append("guidance lowered")
    confidence = str(payload.get("confidence", "unknown")).strip().lower() or "unknown"
    return QualitativeSignal(sentiment_score=sentiment, flag_keywords=flags, confidence=confidence)


def negative_event_tickers_from_sec_filings(
    filings_by_ticker: Mapping[str, Sequence[Mapping[str, object]]],
    negative_items: Set[str] = None,
) -> Set[str]:
    negative_items = negative_items or {"1.03", "2.05", "2.06", "3.01", "4.01", "4.02"}
    flagged = set()
    for ticker, filings in filings_by_ticker.items():
        for filing in filings:
            if str(filing.get("form", "")).upper() != "8-K":
                continue
            items = _filing_item_tokens(filing.get("items", ""))
            if items.intersection(negative_items):
                flagged.add(ticker)
                break
    return flagged


def _complete(llm_client, prompt: str):
    if hasattr(llm_client, "complete"):
        return llm_client.complete(prompt)
    if callable(llm_client):
        return llm_client(prompt)
    raise TypeError("llm_client must be callable or expose complete(prompt)")


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM response did not contain a JSON object")
    return text[start:end + 1]


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _filing_item_tokens(value) -> Set[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value).replace(";", ",").split(",")
    return {str(item).strip() for item in raw_items if str(item).strip()}
