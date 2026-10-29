"""Point-in-time daily universes for intraday research and backtests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class DailyUniverseSnapshot:
    """Immutable stable-security-ID to same-day ticker mapping.

    Tickers are execution aliases and can change between sessions.  The
    ``security_id`` keys remain the identity used to aggregate results across
    renames and ticker reuse.
    """

    trading_day: date
    ticker_by_security_id: Mapping[str, str]

    def __post_init__(self) -> None:
        if isinstance(self.trading_day, datetime) or not isinstance(
            self.trading_day,
            date,
        ):
            raise ValueError("trading_day must be a date")
        if not isinstance(self.ticker_by_security_id, Mapping):
            raise TypeError("ticker_by_security_id must be a mapping")
        normalized: dict[str, str] = {}
        ticker_owners: dict[str, str] = {}
        for raw_security_id, raw_ticker in self.ticker_by_security_id.items():
            security_id = _text(raw_security_id, "security_id")
            ticker = _text(raw_ticker, "ticker").upper()
            if security_id in normalized:
                raise ValueError("duplicate normalized security_id")
            owner = ticker_owners.get(ticker)
            if owner is not None:
                raise ValueError(
                    "a daily ticker cannot map to multiple security_ids: " + ticker
                )
            normalized[security_id] = ticker
            ticker_owners[ticker] = security_id
        object.__setattr__(
            self,
            "ticker_by_security_id",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

    @property
    def day(self) -> date:
        """Compatibility alias for code that names sessions simply ``day``."""

        return self.trading_day

    @property
    def effective_date(self) -> date:
        """Compatibility alias matching point-in-time security-master snapshots."""

        return self.trading_day

    @property
    def security_ids(self) -> tuple[str, ...]:
        return tuple(self.ticker_by_security_id)

    @property
    def active_tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self.ticker_by_security_id.values()))

    @property
    def active_symbols(self) -> tuple[str, ...]:
        return self.active_tickers

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.active_tickers

    @property
    def security_id_by_ticker(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                ticker: security_id
                for security_id, ticker in self.ticker_by_security_id.items()
            }
        )

    @property
    def security_id_by_symbol(self) -> Mapping[str, str]:
        return self.security_id_by_ticker

    def ticker_for(self, security_id: str) -> str:
        return self.ticker_by_security_id[_text(security_id, "security_id")]

    def security_id_for(self, ticker: str) -> str:
        return self.security_id_by_ticker[_text(ticker, "ticker").upper()]


__all__ = ("DailyUniverseSnapshot",)
