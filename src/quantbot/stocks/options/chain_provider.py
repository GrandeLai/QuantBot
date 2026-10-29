"""期权链 Provider — yfinance MVP 实现.

只依赖 yfinance（已是 quantpilot-common 依赖，无需 API key）。
未来可加 Tradier provider 作为实时替代。

Usage:
    contracts = fetch_chain_yfinance("SPY", max_dte=45)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal


@dataclass
class OptionsContract:
    """单张期权合约的快照数据."""

    ticker: str
    expiry: date
    strike: float
    option_type: Literal["call", "put"]
    open_interest: int
    implied_volatility: float  # 年化，e.g. 0.20 = 20%
    last_price: float
    bid: float
    ask: float
    volume: int
    dte: int  # days-to-expiry from today


def fetch_chain_yfinance(
    ticker: str,
    *,
    max_dte: int = 45,
    min_oi: int = 0,
) -> list[OptionsContract]:
    """从 yfinance 拉取期权链（call + put），过滤 DTE 和 OI.

    Args:
        ticker:  标的代码（如 "SPY", "QQQ", "IWM"）
        max_dte: 最大到期天数（含），默认 45
        min_oi:  最小 OI 过滤，默认 0（不过滤）

    Returns:
        合并所有符合条件到期日的 call + put 合约列表

    Raises:
        RuntimeError: 如果 yfinance 无法获取期权数据
    """
    import yfinance as yf  # lazy import — yfinance 只在需要时加载

    yf_ticker = yf.Ticker(ticker)
    try:
        expiry_strings: tuple[str, ...] = yf_ticker.options
    except Exception as exc:
        raise RuntimeError(f"无法获取 {ticker} 期权到期日列表: {exc}") from exc

    if not expiry_strings:
        raise RuntimeError(f"{ticker} 没有可用期权数据")

    today = datetime.now(tz=timezone.utc).date()
    contracts: list[OptionsContract] = []

    for expiry_str in expiry_strings:
        expiry = date.fromisoformat(expiry_str)
        dte = (expiry - today).days
        if dte < 0 or dte > max_dte:
            continue

        try:
            chain = yf_ticker.option_chain(expiry_str)
        except Exception:
            continue

        for opt_type, df in (("call", chain.calls), ("put", chain.puts)):
            for row in df.itertuples(index=False):
                oi = int(getattr(row, "openInterest", 0) or 0)
                iv = float(getattr(row, "impliedVolatility", 0.0) or 0.0)
                if oi < min_oi or iv <= 0.0:
                    continue
                contracts.append(
                    OptionsContract(
                        ticker=ticker.upper(),
                        expiry=expiry,
                        strike=float(row.strike),
                        option_type=opt_type,  # type: ignore[arg-type]
                        open_interest=oi,
                        implied_volatility=iv,
                        last_price=float(getattr(row, "lastPrice", 0.0) or 0.0),
                        bid=float(getattr(row, "bid", 0.0) or 0.0),
                        ask=float(getattr(row, "ask", 0.0) or 0.0),
                        volume=int(getattr(row, "volume", 0) or 0),
                        dte=dte,
                    )
                )

    return contracts
