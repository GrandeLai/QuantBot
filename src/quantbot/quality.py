"""Data quality validation and quarantine helpers."""

from dataclasses import dataclass
from typing import Iterable, List


class OHLCVSchemaValidator:
    def validate(self, row: dict) -> List[str]:
        errors = []
        required = ("date", "ticker", "adjusted_close", "volume")
        for key in required:
            if row.get(key) is None:
                errors.append("%s_missing" % key)

        for key in ("open", "high", "low", "close", "adjusted_close"):
            value = row.get(key)
            if value is not None and value < 0:
                errors.append("negative_%s" % key)

        volume = row.get("volume")
        if volume is not None and volume < 0:
            errors.append("negative_volume")

        high = row.get("high")
        low = row.get("low")
        if high is not None and low is not None and high < low:
            errors.append("high_below_low")

        close = row.get("close")
        if high is not None and close is not None and close > high:
            errors.append("close_above_high")
        if low is not None and close is not None and close < low:
            errors.append("close_below_low")
        return errors


@dataclass(frozen=True)
class QuarantineResult:
    clean_rows: list
    quarantined: list


def quarantine_invalid_rows(rows: Iterable[dict], validator: OHLCVSchemaValidator) -> QuarantineResult:
    clean_rows = []
    quarantined = []
    for row in rows:
        errors = validator.validate(row)
        if errors:
            bad = dict(row)
            bad["errors"] = errors
            quarantined.append(bad)
        else:
            clean_rows.append(dict(row))
    return QuarantineResult(clean_rows=clean_rows, quarantined=quarantined)
