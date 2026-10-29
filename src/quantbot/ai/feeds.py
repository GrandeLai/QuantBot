"""只读行情更新、交易日历校验与不可变快照；不访问交易账户。"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
import io
import json
from pathlib import Path

from quantbot import __version__
from quantbot.crypto.ai import INTERVALS, candles_from_rows, market_context
from quantbot.evidence import hash_payload

from .market import DailyMarketData, EASTERN, FIELDS
from .domain import strict_json
from .transport import JsonTransport


def stock_calendar(start, end):
    try:
        import exchange_calendars as calendars
    except ImportError:
        raise ValueError("美股自动运行需要安装 quantbot[automation] 的交易日历依赖") from None
    lower = date.fromisoformat(str(start)) - timedelta(days=14)
    upper = date.fromisoformat(str(end)) + timedelta(days=14)
    return calendars.get_calendar("XNYS", start=str(lower), end=str(upper))


@dataclass
class Snapshot:
    market: str
    rows: list[dict]
    config: dict
    source: str
    calendar: object = None
    actions: list[dict] | None = None
    vintages: list[dict] | None = None
    provenance: dict | None = None
    sec_events: object = None

    def __post_init__(self):
        self._contexts = {}
        if self.market == "stocks":
            self.data = DailyMarketData(self.rows, self.config)
            self.times = [
                self.calendar.session_close(str(day)).to_pydatetime() for day in self.data.common_dates
            ]
        else:
            self.data = candles_from_rows(self.rows)
            self.times = sorted({c.timestamp + INTERVALS[c.bar] for c in self.data})

    def context(self, at=None):
        at = at or self.times[-1]
        context = self._price_context(at)
        if self.sec_events is not None:
            events = self.sec_events.at(context.as_of, context.features)
            return replace(context, sec_events=events,
                           snapshot_id=hash_payload({"market": context.snapshot_id, "sec_events": events}))
        return context

    def _price_context(self, at):
        if at in self._contexts:
            return self._contexts[at]
        cutoff = at.astimezone(UTC).isoformat()
        splits = [a for a in self.actions or [] if a.get("type") == "split" and "ratio" in a
                  and a["date"] <= at.astimezone(EASTERN).date().isoformat() and a["available_at"] <= cutoff]
        if self.vintages or splits:
            if at not in self._contexts:
                visible = {}
                for row in self.vintages or self.rows:
                    if not self.vintages or row["available_at"] <= cutoff:
                        visible[(row.get("date", row.get("timestamp")), row["symbol"])] = {
                            k: v for k, v in row.items() if k != "available_at"
                        }
                # 特征按当时已生效的拆股换算历史单位；成交仍使用冻结的原始 OHLCV。
                for action in splits:
                    ratio = float(action["ratio"])
                    for row in visible.values():
                        if row["symbol"] == action["symbol"] and row["date"] < action["date"]:
                            for field in ("open", "high", "low", "close"):
                                row[field] = float(row[field]) / ratio
                            row["volume"] = float(row["volume"]) * ratio
                data = Snapshot(self.market, list(visible.values()), self.config, self.source,
                                self.calendar)
                context = data.context(at)
                if splits:
                    context = replace(context,
                        features={s: {**values, "history_price_basis": "split_adjusted_as_of"} for s, values in context.features.items()},
                        snapshot_id=hash_payload({"market": context.snapshot_id, "effective_splits": splits}))
                self._contexts[at] = context
            return self._contexts[at]
        if self.market == "stocks":
            context = self.data.context(at.astimezone(EASTERN).date())
            context = replace(
                context,
                as_of=at,
                snapshot_id=hash_payload(
                    {
                        "data": context.snapshot_id,
                        "close": at.isoformat(),
                        "source": self.source,
                    }
                ),
            )
        else:
            context = market_context(
                [c for c in self.data if c.timestamp + INTERVALS[c.bar] <= at],
                self.config["market"]["symbols"],
                now=at,
                source=self.source,
                atr_window=self.config["strategy"]["atr_window"],
                atr_stop_mult=self.config["strategy"]["atr_stop_mult"],
            )[0]
        # 同一冻结快照被多个策略/成本档复用；with_config 会创建独立缓存。
        self._contexts[at] = context
        return context

    def bars(self, after, until):
        """完整的跨次运行区间，用于次根开盘撮合与保护止损。"""
        if self.market == "stocks":
            return [
                (
                    at,
                    {
                        s: {k: float(v) for k, v in vars(b).items() if k in FIELDS[2:]}
                        for s, b in self.data.bars(at.astimezone(EASTERN).date()).items()
                    },
                )
                for at in self.times
                if after < at <= until
            ]
        batches = {}
        for c in self.data:
            at = c.timestamp + INTERVALS[c.bar]
            if after < at <= until:
                batches.setdefault(at, {})[c.symbol] = {k: str(getattr(c, k)) for k in FIELDS[2:]}
        return sorted(batches.items())

    def row_hashes(self):
        key = "date" if self.market == "stocks" else "timestamp"
        return {f"{r['symbol']}|{r[key]}": hash_payload(r) for r in self.rows}

    def with_config(self, config):
        return replace(self, config=config)

    def quality(self):
        return {
            "status": "passed", "source": self.source, "rows": len(self.rows),
            "start": self.times[0].isoformat(), "end": self.times[-1].isoformat(),
            "checks": ["OHLCV 与重复记录", "标的与周期", "交易日历/连续性", "未来与未完成行情"],
            "freshness_basis": "historical_window_coverage_not_latest_market_date",
            "point_in_time_verified": False,
            "availability_enforced": bool(self.vintages),
            "revision_count": len(self.vintages) - len(self.rows) if self.vintages else 0,
            "execution_price_basis": "first_published_bar" if self.vintages else "frozen_final_version",
            "provenance": self.provenance,
            "corporate_action_count": len(self.actions or []),
            "sec_events": self.sec_events.quality() if self.sec_events is not None else {"enabled": False},
            "limitations": ["供应方声明的发布时间和公司行动完整性尚未经独立认证", "未证明股票池无存活偏差及模型训练数据无污染"],
        }

    def freeze(self, directory):
        payload = {
            "market": self.market,
            "source": self.source,
            "rows": self.rows,
            "actions": self.actions or [],
        }
        if self.vintages:
            payload["vintage_input"] = {**self.provenance, "rows": self.vintages,
                                        "actions": self.actions or []}
        if self.sec_events is not None:
            payload["sec_event_input"] = self.sec_events.input
        digest = hash_payload(payload)
        path = Path(directory) / (digest + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
        try:
            with path.open("x") as f:
                f.write(encoded)
        except FileExistsError:
            if path.read_text() != encoded:
                raise ValueError("行情快照内容与指纹不一致") from None
        return {
            "hash": digest,
            "path": str(path),
            "as_of": self.times[-1].isoformat(),
            "source": self.source,
            "rows": len(self.rows),
        }


def validate_rows(rows, spec, config, now, *, fresh=True, actions=None):
    """拒绝缺根、错位、未来数据；自动下载方必须先去除正在形成的 K 线。"""
    if now.tzinfo is None:
        raise ValueError("行情校验时间必须带时区")
    if not rows or len(rows) > 100_000:
        raise ValueError("行情必须为 1..100000 行")
    symbols = spec["symbols"]
    if {r["symbol"] for r in rows} != set(symbols):
        raise ValueError("行情标的与任务股票池不一致")
    calendar = None
    grace = timedelta(seconds=spec["data_delay_seconds"])
    if spec["market"] == "stocks":
        first = min(r["date"] for r in rows)
        last = max(r["date"] for r in rows)
        calendar = stock_calendar(first, (now + timedelta(days=7)).date())
        expected = {d.date().isoformat() for d in calendar.sessions_in_range(first, last)}
        for symbol in symbols:
            if {r["date"] for r in rows if r["symbol"] == symbol} != expected:
                raise ValueError(f"{symbol} 缺少交易日或包含休市日行情")
        snapshot = Snapshot("stocks", rows, config, spec["source"], calendar, actions)
        if fresh:
            closed = calendar.schedule[calendar.schedule["close"] <= now - grace]
            if closed.empty or last < closed.index[-1].date().isoformat():
                raise ValueError("美股行情过期：尚未覆盖最新已收盘交易日")
    else:
        if any(r["bar"] != spec["bar"] for r in rows):
            raise ValueError("行情周期与任务配置不一致")
        snapshot = Snapshot("crypto", rows, config, spec["source"])
        interval = INTERVALS[spec["bar"]]
        if any(datetime.fromisoformat(r["timestamp"]).timestamp() % interval.total_seconds() for r in rows):
            raise ValueError("加密 K 线必须对齐 UTC 周期边界")
        expected = int((now - grace).timestamp() // interval.total_seconds()) * interval.total_seconds()
        if fresh and snapshot.times[-1].timestamp() < expected:
            raise ValueError("加密行情过期：尚未覆盖最新已收盘 K 线")
    if snapshot.times[-1] > now:
        raise ValueError("行情包含尚未收盘或未来的 K 线")
    snapshot.context()  # 同时校验数值、预热长度和每个标的的完整区间。
    return snapshot


def fetch_snapshot(spec, config, now, directory, *, fresh=True):
    from .sec_context import attach_sec_context

    sec_input = None
    if spec.get("sec_context_path"):
        if fresh:
            raise ValueError("SEC 公告输入当前仅支持回测与研究")
        path = Path(spec["sec_context_path"])
        if path.stat().st_size > 64_000_000:
            raise ValueError("AI 公告输入超过 64 MB")
        sec_input = strict_json(path.read_text(encoding="utf-8"))
    source, actions = spec["source"], []
    if source == "vintage":
        from .vintages import load_vintages

        path = Path(spec["data_path"])
        if path.stat().st_size > 32_000_000:
            raise ValueError("历史版本 JSON 超过 32 MB")
        snapshot = load_vintages(strict_json(path.read_text(encoding="utf-8")), spec, config, now, fresh=fresh)
        return attach_sec_context(snapshot, sec_input) if spec.get("sec_context_path") else snapshot
    if source == "csv":
        path = Path(spec["data_path"])
        if path.stat().st_size > 16_000_000:
            raise ValueError("行情 CSV 超过 16 MB")
        content = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        fields = FIELDS if spec["market"] == "stocks" else ("timestamp", *FIELDS[1:], "bar")
        if set(reader.fieldnames or []) != set(fields):
            raise ValueError("行情 CSV 列必须为 " + ",".join(fields))
        rows = list(reader)
    elif source == "yahoo":
        import yfinance as yf

        cache = Path(directory) / "yahoo-cache"
        cache.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(cache))
        calendar = stock_calendar(spec["start"], (now + timedelta(days=7)).date())
        closed = calendar.schedule[calendar.schedule["close"] <= now]
        last = closed.index[-1].date()
        rows = []
        for symbol in spec["symbols"]:
            frame = yf.Ticker(symbol).history(
                start=spec["start"],
                end=str(last + timedelta(days=1)),
                auto_adjust=False,
                actions=True,
                timeout=20,
            )
            if frame.empty:
                raise ValueError(f"未取得 {symbol} 行情")
            for at, bar in frame.iterrows():
                rows.append(
                    {
                        "date": str(at.date()),
                        "symbol": symbol,
                        **{k: float(bar[k.capitalize()]) for k in FIELDS[2:]},
                    }
                )
                if bar.get("Stock Splits", 0) or bar.get("Dividends", 0):
                    actions.append({"symbol": symbol, "date": str(at.date())})
    elif source == "okx_public":
        rows = []
        interval = INTERVALS[spec["bar"]]
        bar = {"15m": "15m", "1h": "1H", "1d": "1Dutc"}[spec["bar"]]
        for symbol in spec["symbols"]:
            params = {"instId": symbol.replace("/", "-"), "bar": bar, "limit": 300}
            response = JsonTransport(timeout=20).get_json(
                "https://www.okx.com/api/v5/market/candles",
                headers={"User-Agent": f"QuantBot/{__version__}"},
                params=params,
            )
            if response.get("code") != "0" or not isinstance(response.get("data"), list):
                raise ValueError("公开行情接口返回失败")
            for values in response["data"]:
                if not isinstance(values, list) or len(values) != 9:
                    raise ValueError("公开 K 线字段不符合契约")
                at = datetime.fromtimestamp(int(values[0]) / 1000, UTC)
                if values[8] == "1" and at + interval <= now:
                    rows.append(
                        {
                            "timestamp": at.isoformat(),
                            "symbol": symbol,
                            "bar": spec["bar"],
                            **dict(zip(FIELDS[2:], values[1:6])),
                        }
                    )
    else:
        raise ValueError("未知自动行情来源")
    # 统一数值表示，避免 CSV 文本格式差异被误判为历史修订。
    for row in rows:
        for key in FIELDS[2:]:
            row[key] = float(row[key])
        if spec["market"] == "crypto":
            timestamp = datetime.fromisoformat(row["timestamp"])
            if timestamp.tzinfo is None:
                raise ValueError("加密行情 timestamp 必须带时区")
            row["timestamp"] = timestamp.astimezone(UTC).isoformat()
    rows.sort(key=lambda r: (r.get("date", r.get("timestamp")), r["symbol"]))
    snapshot = validate_rows(rows, spec, config, now, fresh=fresh, actions=actions)
    return attach_sec_context(snapshot, sec_input) if spec.get("sec_context_path") else snapshot
