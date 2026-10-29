"""冻结的日线状态与入场因子；只构造研究面板，不改变交易快照。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from quantbot.ai.feeds import Snapshot


FACTOR_DEFINITIONS = (
    {"id": "volume_confirmation", "operator": ">=", "threshold": 1.0,
     "formula": "volume / volume.shift(1).rolling(20, min_periods=20).mean()"},
    {"id": "close_location", "operator": ">=", "threshold": 0.75,
     "formula": "(close - low) / (high - low)"},
    {"id": "volatility_compression", "operator": "<=", "threshold": 1.0,
     "formula": "close.pct_change(fill_method=None).rolling(5).std(ddof=1) / rv20"},
    {"id": "trend_persistence", "operator": ">=", "threshold": 0.8,
     "formula": "mean(close > EMA20(adjust=False,min_periods=20), last 10 complete observations)"},
)
FACTOR_IDS = tuple(definition["id"] for definition in FACTOR_DEFINITIONS)
_DEFINITIONS = {definition["id"]: definition for definition in FACTOR_DEFINITIONS}


def factor_pass(factor_id: str, value: float | None) -> bool:
    """缺失或非有限数值不通过；未冻结的因子 ID 是调用错误。"""
    if factor_id not in _DEFINITIONS:
        raise ValueError(f"未登记的入场因子：{factor_id}")
    if value is None or pd.isna(value) or not math.isfinite(value):
        return False
    definition = _DEFINITIONS[factor_id]
    return bool(value >= definition["threshold"] if definition["operator"] == ">="
                else value <= definition["threshold"])


def _asset_panel(frame: pd.DataFrame) -> pd.DataFrame:
    close, high, low, volume = (frame[name] for name in ("close", "high", "low", "volume"))
    returns = close.pct_change(fill_method=None)
    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
    trend_valid = close.notna() & ema50.notna() & ema200.notna()
    trend = pd.Series("unavailable", index=frame.index)
    trend.loc[trend_valid] = "mixed"
    trend.loc[trend_valid & (close > ema50) & (ema50 > ema200)] = "up"
    trend.loc[trend_valid & (close < ema50) & (ema50 < ema200)] = "down"
    rv20 = returns.rolling(20, min_periods=20).std(ddof=1)
    prior_volume = volume.shift(1).rolling(20, min_periods=20).mean()
    price_range = high - low
    above_ema20 = (close > ema20).astype(float).where(close.notna() & ema20.notna())
    result = pd.DataFrame({
        "asset_trend": trend,
        "rv20": rv20,
        "prior_rv20_median126": rv20.shift(1).rolling(126, min_periods=126).median(),
        "volume_confirmation": volume / prior_volume.where(prior_volume != 0),
        "close_location": (close - low) / price_range.where(price_range != 0),
        "volatility_compression": returns.rolling(5, min_periods=5).std(ddof=1) / rv20.where(rv20 != 0),
        "trend_persistence": above_ema20.rolling(10, min_periods=10).mean(),
    }, index=frame.index)
    for factor_id in FACTOR_IDS:
        valid = result[factor_id].map(lambda value: pd.notna(value) and math.isfinite(value))
        result[factor_id] = result[factor_id].where(valid)
        result[f"{factor_id}_valid"] = valid
    return result


def build_entry_panel(snapshot: Snapshot) -> pd.DataFrame:
    """每个配置标的、每个 snapshot.times 一行；as_of 是规范 UTC 收盘时间。

    股票按快照的实际交易日收盘（包含早收盘/DST）对齐；加密只接受日线，
    行情 timestamp 是开盘时间。缺失行情不前填，状态和因子均保留预热缺口。
    本面板使用快照所给历史价格单位，不重新复权，也不读未来收益标签。
    """
    if snapshot.market not in ("stocks", "crypto"):
        raise ValueError("入场面板只支持 stocks/crypto")
    symbols = snapshot.config["market"]["symbols"]
    benchmark = "SPY" if snapshot.market == "stocks" else "BTC/USDT"
    if benchmark not in symbols:
        raise ValueError(f"入场面板缺少冻结基准 {benchmark}")
    times = pd.DatetimeIndex(snapshot.times).tz_convert("UTC")
    rows = pd.DataFrame(snapshot.rows)
    if snapshot.market == "stocks":
        closes = {at.tz_convert("America/New_York").date(): at for at in times}
        rows["as_of"] = pd.to_datetime(rows["date"]).dt.date.map(closes)
    else:
        if not rows["bar"].eq("1d").all():
            raise ValueError("入场面板的加密行情必须为 1d")
        rows["as_of"] = pd.to_datetime(rows["timestamp"], utc=True) + pd.Timedelta(days=1)
    rows = rows.loc[rows["as_of"].isin(times)]
    panels = {}
    for symbol in symbols:
        frame = rows.loc[rows["symbol"] == symbol].set_index("as_of")
        frame = frame[["close", "high", "low", "volume"]].astype(float).reindex(times)
        panels[symbol] = _asset_panel(frame)
    base = panels[benchmark]
    state = pd.Series("unavailable", index=times)
    valid = (base["asset_trend"] != "unavailable") & base["rv20"].notna() \
        & base["prior_rv20_median126"].notna()
    volatility = pd.Series("normal", index=times)
    volatility.loc[base["rv20"] > base["prior_rv20_median126"]] = "high"
    state.loc[valid] = base.loc[valid, "asset_trend"] + "_" + volatility.loc[valid]
    output = []
    for symbol, panel in panels.items():
        panel.insert(0, "market_state", state)
        panel.insert(0, "symbol", symbol)
        panel.insert(0, "as_of", [at.isoformat() for at in times])
        output.append(panel.reset_index(drop=True))
    return pd.concat(output, ignore_index=True).sort_values(["as_of", "symbol"]).reset_index(drop=True)
