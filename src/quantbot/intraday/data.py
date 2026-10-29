"""Bounded public minute-data acquisition and strict session-grid validation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import csv
import gzip
import hashlib
import io
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile

import pandas as pd

from quantbot.ai.feeds import stock_calendar
from quantbot.ai.research_catalog import load
from quantbot.factors.conditional_study import check_ref, file_ref, read, save


FIELDS = ("timestamp", "symbol", "open", "high", "low", "close", "volume")


def _check_policies(policies, snapshots):
    """Resolve explicitly archived policy bytes without redirecting other inputs."""
    if not isinstance(snapshots, (list, tuple)):
        raise ValueError("政策快照映射必须是列表")
    mapped = {}
    for item in snapshots:
        if not isinstance(item, dict) or set(item) != {"original", "snapshot", "current"}:
            raise ValueError("政策快照必须绑定 original、snapshot 和 current")
        original, snapshot, current = (item[k] for k in ("original", "snapshot", "current"))
        if any(not isinstance(ref, dict) or set(ref) != {"path", "sha256"} for ref in
               (original, snapshot, current)):
            raise ValueError("政策快照必须使用完整文件引用")
        if original not in policies:
            raise ValueError("政策快照只能映射当前方案的 policies 引用")
        key = (original["path"], original["sha256"])
        if key in mapped:
            raise ValueError("政策快照不能重复映射同一 original")
        if snapshot["sha256"] != original["sha256"] or current["path"] != original["path"]:
            raise ValueError("政策快照原始摘要或当前路径不一致")
        check_ref(snapshot)
        check_ref(current)
        mapped[key] = snapshot
    for ref in policies:
        check_ref(mapped.get((ref["path"], ref["sha256"]), ref))


def frozen_policy_snapshots(frozen):
    """Verify the explicit mapping document as well as its frozen contents."""
    if "policy_snapshots" not in frozen and "policy_snapshots_input" not in frozen:
        return ()
    if "policy_snapshots" not in frozen or "policy_snapshots_input" not in frozen:
        raise ValueError("冻结政策快照缺少映射或输入引用")
    snapshots = read(check_ref(frozen["policy_snapshots_input"]))
    if not isinstance(snapshots, list):
        raise ValueError("政策快照映射必须是列表")
    if snapshots != frozen["policy_snapshots"]:
        raise ValueError("冻结政策快照映射与输入不一致")
    return snapshots


def checked_protocol(path, *, policy_snapshots=()):
    path = Path(path).resolve()
    sidecar = path.with_suffix(".freeze.json")
    declared = read(sidecar if sidecar.exists() else path.parent / "protocol-freeze.json")["protocol"]
    if file_ref(path) != declared:
        raise ValueError("日内预登记方案已改变")
    protocol = read(path)
    if (
        protocol["id"] not in ("dual_market_intraday_v1", "dual_market_intraday_v2", "dual_market_intraday_v3",
                                  "dual_market_intraday_mining_v1")
        or protocol["trading_enabled"]
        or protocol["formal_admission"]
        or protocol["sealed_holdout_evaluated"]
        or protocol["model_calls_authorized"] != 0
    ):
        raise ValueError("只支持冻结的离线日内研究")
    expected_sampling = {
        "execution_bar_minutes": 5,
        "signal_bar_minutes": 15,
        "opening_range_minutes": 30,
        "entry_end_minutes_before_close": 135,
        "max_holding_minutes": 120,
        "flatten_minutes_before_close": 15,
    }
    if any(protocol["sampling"][k] != v for k, v in expected_sampling.items()):
        raise ValueError("日内执行参数与已实现的冻结方案不一致")
    _check_policies(protocol["policies"], policy_snapshots)
    if protocol["id"] == "dual_market_intraday_mining_v1":
        from .mining import validate_protocol

        parent = checked_protocol(check_ref(protocol["parent_intraday_protocol"]), policy_snapshots=policy_snapshots)
        catalog = load(check_ref(protocol["prior_catalog"]))
        validate_protocol(protocol, parent, catalog)
        return protocol
    mechanisms = protocol["id"] == "dual_market_intraday_v3"
    followup = protocol["id"] != "dual_market_intraday_v1"
    expected_stats = {
        "minimum_feature_coverage": 0.95,
        "minimum_filled_per_window": 5,
        "minimum_total_filled": 30,
        "minimum_positive_windows": 2,
        "bootstrap_replicates": 4999,
        "block_sessions": 20,
        "seed": 1729,
        "familywise_alpha": 0.05,
        "purge_sessions": 20,
        "embargo_sessions": 20,
        "minimum_family_size": 147 if followup else 143,
    }
    if mechanisms:
        # New rounds derive family size from the immutable catalog, not a new source-code number.
        del expected_stats["minimum_family_size"]
    if any(protocol["statistics"][k] != v for k, v in expected_stats.items()):
        raise ValueError("不能降低原统计门槛或改变冻结重采样参数")
    names = (("vwap_trend_reclaim", "failed_breakdown_reversal") if mechanisms else
             ("volume_cost_room", "retest_persistence") if followup else
             ("volume_confirmation", "retest_confirmation"))
    expected_factors = [(m, f) for m in ("crypto", "stocks") for f in names]
    if [(c["market"], c["factor"]) for c in protocol["factors"]] != expected_factors:
        raise ValueError("必须登记双市场全部四项固定因子")
    if followup:
        if not protocol["original_control_required"] or protocol["new_outcomes_seen"]:
            raise ValueError("必须保留原突破对照且在新结果出现前冻结")
        parent = checked_protocol(check_ref(protocol["parent_intraday_protocol"]), policy_snapshots=policy_snapshots)
        if parent["id"] != ("dual_market_intraday_v2" if mechanisms else "dual_market_intraday_v1"):
            raise ValueError("日内研究必须继承对应的上一轮冻结方案")
        for field in ("sampling", "execution_model", "markets", "range", "windows", "policies", "protected_intervals"):
            if protocol[field] != parent[field]:
                raise ValueError("不能改变原日内窗口、止盈止损或执行参数: " + field)
        expected_parameters = {
            "relative_volume_min": 1.5, "opening_range_cost_multiple": 1.0,
            "maximum_extension_range_fraction": 0.5, "persistence_minutes": 15,
        }
        if mechanisms:
            expected_parameters = {
                "earliest_signal_minutes": 60, "range_lookback_bars": 4,
                "lower_wick_fraction_min": 0.5, "vwap_max_extension_cost_multiple": 1.0,
                "vwap_price": "completed_5m_typical_price_volume_weighted_session_anchor",
            }
        if protocol["factor_parameters"] != expected_parameters:
            raise ValueError("新因子参数与冻结实现不一致")
        controls = ["vwap_reclaim", "range_breakdown"] if mechanisms else ["volume_confirmation", "retest_confirmation"]
        if [c["control"] for c in protocol["factors"]] != controls * 2:
            raise ValueError("新因子必须与对应旧因子配对")
        if not mechanisms:
            if (protocol["statistics"]["historical_trials"] != 143
                    or protocol["statistics"]["reserved_new_trials"] != 4):
                raise ValueError("第二轮必须保留此前143项结果")
            attribution = read(check_ref(protocol["attribution"]))
            if attribution["protocol"] != protocol["parent_intraday_protocol"]:
                raise ValueError("亏损归因必须来自继承的日内方案")
            if datetime.fromisoformat(attribution["created_at"]) >= datetime.fromisoformat(protocol["frozen_at"]):
                raise ValueError("必须先完成旧因子归因再冻结新方案")
    execution = protocol["execution_model"]
    expected_execution = {
        "one_way_cost_bps": [15, 30],
        "fee_bps": [10, 20],
        "half_spread_bps": [2.5, 5],
        "slippage_bps": [2.5, 5],
        "impact_bps_at_participation_cap": 5,
        "max_volume_participation": 0.01,
        "latency_seconds": [1, 301],
        "entry_ttl_seconds": 900,
        "quantity_crypto": {"BTC/USDT": "0.00001", "ETH/USDT": "0.0001"},
        "quantity_stocks": "1",
    }
    if any(execution[k] != v for k, v in expected_execution.items()):
        raise ValueError("费用、延迟、容量或精度与冻结模型不一致")
    prior = read(check_ref(protocol["prior_protocol"]))
    catalog = load(check_ref(protocol["prior_catalog"]))
    if mechanisms:
        if protocol["independent_validation"]:
            raise ValueError("本轮沿用已暴露开发窗口，不能标记为独立验证")
        statistics = protocol["statistics"]
        old_count, added = len(catalog["trials"]), len(protocol["factors"])
        if (len(catalog["outcomes"]) != old_count or old_count < 147
                or statistics["historical_trials"] != old_count
                or statistics["reserved_new_trials"] != added
                or statistics["minimum_family_size"] != old_count + added
                or any(card["id"] not in catalog["outcomes"] for card in parent["factors"])):
            raise ValueError("研究家族必须来自完整的上一轮累计账本")
        if read(check_ref(protocol["prior_result"]))["family_size"] != old_count:
            raise ValueError("上一轮结果与累计试验数量不一致")
    if protocol["protected_intervals"] != catalog["protected_intervals"]:
        raise ValueError("原封存边界改变")
    for market, section in protocol["markets"].items():
        original = prior["markets"][market]
        if (
            section["risk"] != original["config"]["risk"]
            or section["strategy"] != original["config"]["strategy"]
            or section["execution"] != original["config"]["execution"]
            or any(section[k] != original["spec"][k] for k in ("max_daily_loss", "max_drawdown"))
        ):
            raise ValueError("原风险、执行配置或准出参数改变")
        start, end = protocol["range"]["start"], protocol["range"]["end_exclusive"]
        if start >= end or end > "2026-07-01":
            raise ValueError("日内数据越过本轮开发截止日期")
        for protected in catalog["protected_intervals"]:
            if protected["market"] == market and start <= protected["end"] and end > protected["start"]:
                raise ValueError("日内数据访问与原封存区间重叠")
    return protocol


def import_minutes(protocol_path, manifest_path, data_path, market, output):
    """Bind an explicitly supplied offline dataset without certifying its PIT origin."""
    protocol = checked_protocol(protocol_path)
    report = read(manifest_path)
    if report["protocol"] != file_ref(protocol_path):
        raise ValueError("导入清单未绑定冻结方案")
    _, _, quality = load_bars(data_path, protocol, market)
    if not quality["complete"]:
        raise ValueError("导入分钟数据不完整，不能替换数据阻断状态")
    report["parent_manifest"] = file_ref(manifest_path)
    report[market] = {
        "status": "imported",
        "data": file_ref(data_path),
        "quality": quality,
        "pit_verified": False,
        "source": "explicit_offline_input",
        "imported_at": datetime.now(UTC).isoformat(),
    }
    save(Path(output), report)
    return report


def _public(url, *, head=False):
    for attempt in range(3):
        try:
            with urlopen(
                Request(
                    url, headers={"User-Agent": "QuantBot-research/0.3"}, method="HEAD" if head else "GET"
                ),
                timeout=25,
            ) as response:
                return response.status, b"" if head else response.read()
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                return exc.code, b""  # Do not consume unbounded error/quote payloads.
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(attempt + 1)
    raise RuntimeError("public request exhausted")


def _archive(task):
    symbol, month, directory = task
    name = f"{symbol}-5m-{month}.zip"
    url = f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/5m/{name}"
    checksum_url = url + ".CHECKSUM"
    path = directory / name
    if path.exists() and Path(str(path) + ".CHECKSUM").exists():
        raw, checksum = path.read_bytes(), Path(str(path) + ".CHECKSUM").read_bytes()
    else:
        status, checksum = _public(checksum_url)
        if status != 200:
            return {
                "symbol": symbol,
                "month": month,
                "url": url,
                "status": "unavailable",
                "http_status": status,
            }
        status, raw = _public(url)
        if status != 200:
            return {
                "symbol": symbol,
                "month": month,
                "url": url,
                "status": "unavailable",
                "http_status": status,
            }
    digest = hashlib.sha256(raw).hexdigest()
    if checksum.decode().split()[0] != digest:
        raise ValueError("Binance archive checksum mismatch: " + name)
    if not path.exists():
        path.write_bytes(raw)
        Path(str(path) + ".CHECKSUM").write_bytes(checksum)
    return {
        "symbol": symbol,
        "month": month,
        "url": url,
        "checksum_url": checksum_url,
        "archive": file_ref(path),
        "checksum": file_ref(str(path) + ".CHECKSUM"),
        "status": "verified",
    }


def acquire(protocol_path, directory):
    protocol = checked_protocol(protocol_path)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "acquisition.json").exists():
        raise ValueError("采集报告已存在；使用新目录或显式导入已冻结文件")
    archives = directory / "archives"
    archives.mkdir(exist_ok=True)
    dates = protocol["range"]
    months = pd.period_range(
        dates["start"], pd.Timestamp(dates["end_exclusive"]) - pd.Timedelta(days=1), freq="M"
    )
    tasks = [
        (s.replace("/", ""), str(m), archives)
        for s in protocol["markets"]["crypto"]["symbols"]
        for m in months
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = list(pool.map(_archive, tasks))
    crypto_path = directory / "crypto-5m.csv.gz"
    complete = all(s["status"] == "verified" for s in sources)
    if complete:
        # Each requested archive is wholly inside the development range.
        with gzip.open(crypto_path, "wt", newline="") as handle:
            writer = csv.DictWriter(handle, FIELDS)
            writer.writeheader()
            for source in sources:
                raw = check_ref(source["archive"]).read_bytes()
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    names = archive.namelist()
                    if names != [Path(source["archive"]["path"]).name.replace(".zip", ".csv")]:
                        raise ValueError("unexpected Binance archive members")
                    for row in csv.reader(io.TextIOWrapper(archive.open(names[0]), encoding="utf-8")):
                        epoch = int(row[0])
                        # Binance spot archives switched from milliseconds to microseconds in 2025.
                        stamp = datetime.fromtimestamp(epoch / (1_000_000 if epoch >= 10**14 else 1000), UTC)
                        if not dates["start"] <= stamp.date().isoformat() < dates["end_exclusive"]:
                            raise ValueError("archive contains out-of-protocol bars")
                        writer.writerow(
                            dict(
                                zip(
                                    FIELDS,
                                    (stamp.isoformat(), source["symbol"].replace("USDT", "/USDT"), *row[1:6]),
                                )
                            )
                        )
    # Availability checks never request the current market window or consume quote metadata.
    stock_checks = []
    for symbol in protocol["markets"]["stocks"]["symbols"]:
        query = urlencode(
            {
                "symbol": symbol,
                "symbols": symbol,
                "timeframe": "5Min",
                "start": "2025-01-02T14:30:00Z",
                "end": "2025-01-02T21:00:00Z",
                "adjustment": "raw",
                "feed": "iex",
                "limit": 1000,
            }
        )
        url = "https://data.alpaca.markets/v2/stocks/bars?" + query
        status, _ = _public(url, head=True)
        stock_checks.append(
            {
                "symbol": symbol,
                "source": "Alpaca",
                "url": url,
                "method": "HEAD",
                "http_status": status,
                "credentials_used": False,
            }
        )
        start = int(datetime(2025, 1, 2, tzinfo=UTC).timestamp())
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?" + urlencode(
            {"period1": start, "period2": start + 86400, "interval": "5m", "includePrePost": "false"}
        )
        status, _ = _public(url, head=True)
        stock_checks.append(
            {
                "symbol": symbol,
                "source": "Yahoo",
                "url": url,
                "method": "HEAD",
                "http_status": status,
                "response_body_read": False,
            }
        )
    report = {
        "protocol": file_ref(protocol_path),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "crypto": {
            "status": "downloaded" if complete else "blocked",
            "sources": sources,
            "data": file_ref(crypto_path) if complete else None,
            "pit_verified": False,
        },
        "stocks": {
            "status": "blocked",
            "data": None,
            "checks": stock_checks,
            "reasons": [
                "no_public_frozen_period_minute_dataset",
                "historic_yahoo_window_outside_60_day_retention",
            ],
            "pit_verified": False,
        },
        "model_calls": 0,
        "broker_calls": 0,
        "trading_enabled": False,
    }
    save(directory / "acquisition.json", report)
    return report


def session_grid(protocol, market):
    start, end = protocol["range"]["start"], protocol["range"]["end_exclusive"]
    result = {}
    if market == "crypto":
        for day in pd.date_range(start, pd.Timestamp(end) - pd.Timedelta(days=1)):
            opening = day.tz_localize("UTC")
            result[str(day.date())] = (opening, opening + pd.Timedelta(days=1))
    else:
        calendar = stock_calendar(start, end)
        for day in calendar.sessions_in_range(start, str((pd.Timestamp(end) - pd.Timedelta(days=1)).date())):
            result[str(day.date())] = (calendar.session_open(day), calendar.session_close(day))
    return result


def load_bars(path, protocol, market):
    frame = pd.read_csv(path, dtype={"symbol": str, **{k: str for k in FIELDS[2:]}})
    if set(frame) != set(FIELDS) or frame.empty:
        raise ValueError("分钟输入必须包含 timestamp,symbol,open,high,low,close,volume")
    # Do not silently localize naive timestamps or truncate unexpected held-out rows.
    stamps = frame["timestamp"].map(datetime.fromisoformat)
    if any(t.tzinfo is None for t in stamps):
        raise ValueError("分钟时点必须带时区")
    frame["timestamp"] = pd.to_datetime(stamps, utc=True)
    if frame.timestamp.min() < pd.Timestamp(
        protocol["range"]["start"], tz="UTC"
    ) or frame.timestamp.max() + pd.Timedelta(minutes=5) > pd.Timestamp(
        protocol["range"]["end_exclusive"], tz="UTC"
    ):
        raise ValueError("分钟文件越过冻结数据窗口，拒绝读取为研究输入")
    symbols = protocol["markets"][market]["symbols"]
    if set(frame.symbol) != set(symbols) or frame.duplicated(["timestamp", "symbol"]).any():
        raise ValueError("标的不完整或分钟数据重复")
    numeric = frame[list(FIELDS[2:])].astype(float)
    import numpy as np

    if (
        not np.isfinite(numeric.to_numpy()).all()
        or (numeric[["open", "high", "low", "close"]] <= 0).any().any()
        or (numeric.volume < 0).any()
        or (numeric.high < numeric[["open", "close", "low"]].max(axis=1)).any()
        or (numeric.low > numeric[["open", "close", "high"]].min(axis=1)).any()
    ):
        raise ValueError("非法分钟 OHLCV；不删行或补造价格")
    frame = frame.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    sessions = session_grid(protocol, market)
    expected = pd.DatetimeIndex(
        [
            at
            for opening, closing in sessions.values()
            for at in pd.date_range(opening, closing, freq="5min", inclusive="left")
        ]
    )
    missing = {}
    for symbol in symbols:
        actual = pd.DatetimeIndex(frame.loc[frame.symbol == symbol, "timestamp"])
        missing[symbol] = {
            "missing": len(expected.difference(actual)),
            "unexpected": len(actual.difference(expected)),
        }
    quality = {
        "rows": len(frame),
        "sessions": len(sessions),
        "expected_rows": len(expected) * len(symbols),
        "by_symbol": missing,
        "complete": all(not v["missing"] and not v["unexpected"] for v in missing.values()),
        "price_basis": "raw_frozen_final_version",
        "pit_verified": False,
        "historical_publication_times_verified": False,
        "corporate_actions_verified": market == "crypto",
    }
    return frame, sessions, quality
