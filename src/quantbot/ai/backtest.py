"""一次性历史回测入口；复用候选评估，保存证据后结束，不启用持续任务。"""

from __future__ import annotations

import csv
from datetime import date, timedelta
import io
import json
from pathlib import Path

from .evaluation import evaluate
from .feeds import stock_calendar
from .market import FIELDS
from .models import SYSTEM_PROMPT
from .runtime import RuntimeStore, prepare
from .sec_context import SECEventContext
from .replay_evidence import inspect_replay


class BacktestFailure(RuntimeError):
    """回测异常及可供 CLI/网页取回的失败证据位置。"""

    def __init__(self, reason, report_path):
        self.reason = reason
        self.report_path = Path(report_path)
        super().__init__(f"{reason}；失败证据：{self.report_path}")


class BacktestValidationFailure(ValueError):
    """已创建回测证据后的校验失败；保留 ValueError 契约与报告位置。"""

    def __init__(self, reason, report_path):
        self.reason = reason
        self.report_path = Path(report_path)
        super().__init__(f"{reason}；失败证据：{self.report_path}")


def demo_rows(market, config, symbols):
    if market == "stocks":
        from quantbot.cli import _demo_rows

        rows = _demo_rows(config)
        dates = sorted({r["date"] for r in rows})
        start = str(date.fromisoformat(dates[0]) - timedelta(days=len(dates)))
        calendar = stock_calendar(start, dates[-1])
        sessions = calendar.sessions_in_range(start, dates[-1])[-len(dates):]
        mapping = dict(zip(dates, (str(day.date()) for day in sessions), strict=True))
        return [{**r, "date": mapping[r["date"]]} for r in rows]
    from quantbot.crypto.runtime.exchanges.mock import MockExchange

    return [
        {
            "timestamp": candle.timestamp.isoformat(),
            "symbol": symbol,
            "bar": candle.bar,
            **{key: str(getattr(candle, key)) for key in FIELDS[2:]},
        }
        for symbol in symbols
        for candle in MockExchange().get_candles(symbol, limit=100)
    ]


def backtest(config, *, output, market="stocks", provider="ai", csv_data="", symbols=None,
             last_bars=30, allow_model_calls=False, sec_context=None, vintage_data=None):
    if provider == "ai" and not allow_model_calls:
        raise ValueError("AI 回测需要明确允许模型调用；每个可决策时点都会调用模型")
    if type(last_bars) is not int or not 3 <= last_bars <= 500:
        raise ValueError("回测区间必须为 3..500 根 K 线")
    if csv_data and vintage_data is not None:
        raise ValueError("CSV 与历史版本 JSON 只能选择一种行情输入")
    if not csv_data and vintage_data is None and provider != "demo":
        raise ValueError("回测需要历史 CSV 或历史版本 JSON；仅明确选择 demo 时使用合成行情")
    if sec_context is not None and market != "stocks":
        raise ValueError("SEC 公告输入当前仅支持美股回测与研究")
    fields = FIELDS if market == "stocks" else ("timestamp", *FIELDS[1:], "bar")
    rows = None
    vintage_json = None
    if vintage_data is not None:
        if not isinstance(vintage_data, dict):
            raise ValueError("历史版本输入必须为 JSON 对象")
        vintage_json = json.dumps(vintage_data, ensure_ascii=False, allow_nan=False)
        if len(vintage_json.encode("utf-8")) > 32_000_000:
            raise ValueError("历史版本输入超过 32 MB")
        rows = vintage_data.get("rows")
        if not isinstance(rows, list) or not rows or any(
            not isinstance(r, dict) or not isinstance(r.get("symbol"), str) for r in rows
        ):
            raise ValueError("历史版本 rows 必须包含带 symbol 的行情记录")
        if symbols is None:
            symbols = sorted({r["symbol"] for r in rows})
    if csv_data:
        if len(csv_data.encode("utf-8")) > 16_000_000:
            raise ValueError("行情 CSV 超过 16 MB")
        reader = csv.DictReader(io.StringIO(csv_data.removeprefix("\ufeff")))
        if set(reader.fieldnames or []) != set(fields):
            raise ValueError("行情 CSV 列必须为 " + ",".join(fields))
        rows = list(reader)
        if not rows:
            raise ValueError("行情 CSV 不能为空")
        if symbols is None:
            symbols = sorted({r["symbol"] for r in rows})
    directory = Path(output).resolve()
    spec, selected = prepare({
        "name": "历史回测",
        "market": market,
        "provider": provider,
        "symbols": symbols,
        "source": "vintage" if vintage_data is not None else "csv",
        "data_path": str(directory / ("input-vintages.json" if vintage_data is not None else "input.csv")),
        "sec_context_path": str(directory / "sec-context.json") if sec_context is not None else "",
        "bar": "1d" if market == "stocks" else rows[0].get("bar", "") if rows else "15m",
    }, config)
    if rows is None:
        rows = demo_rows(market, selected, spec["symbols"])
    if sec_context is not None:
        SECEventContext(sec_context, spec["symbols"])
    # 每次回测使用全新目录和独立账户，不能覆盖先前报告或触碰持续运行账户。
    if directory.exists():
        raise ValueError("回测输出目录已存在，请使用新目录保存证据")
    directory.mkdir(parents=True)
    if sec_context is not None:
        (directory / "sec-context.json").write_text(json.dumps(sec_context, ensure_ascii=False, allow_nan=False))
    if vintage_json is not None:
        (directory / "input-vintages.json").write_text(vintage_json, encoding="utf-8")
    else:
        with (directory / "input.csv").open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            writer.writerows(rows)
    store = RuntimeStore(directory / "evaluation")
    job = store.create(spec, selected)
    try:
        report = evaluate(store, job["id"], last_bars=last_bars, allow_model_calls=allow_model_calls)
    except Exception as exc:
        # 即使结果未能正常返回，已 fsync 的调用前输入、决策和成交仍可独立取回。
        path = directory / "failure.json"
        failure = {"status": "failed", "execution_completed": False, "error": str(exc), "error_type": type(exc).__name__,
                   "job_id": job["id"], "spec": spec, "config": selected,
                   "input_data": {"rows": rows, "vintage_input": vintage_data, "sec_event_input": sec_context},
                   "system_prompt": SYSTEM_PROMPT,
                   "history": store.history(job["id"]),
                   "replays": [inspect_replay(p) for p in sorted((store.root / "evaluations").glob("*/*.jsonl"))]}
        path.write_text(json.dumps(failure, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        if isinstance(exc, ValueError):
            raise BacktestValidationFailure(str(exc), path) from exc
        raise BacktestFailure(str(exc), path) from exc
    result = {
        **report,
        "market": market,
        "mode": "simulate",
        "status": "completed" if report["passed"] else "failed",
        "data_source": "vintage" if vintage_data is not None else "offline_csv" if csv_data else "mock_fixture",
        "model_called": provider == "ai" and report["candidate"]["provider_calls"] > 0,
        "job_id": job["id"],
        "report_path": str(directory / "report.json"),
        "input_snapshot": json.loads(Path(report["snapshot"]["path"]).read_text()),
        "config": selected, "spec": spec, "system_prompt": SYSTEM_PROMPT,
    }
    with (directory / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")
    return result
