import polars as pl
from quantbot.stocks.screener.scoring import ScoreBreakdown, ScoringEngine

def _make_df(n: int = 60) -> pl.DataFrame:
    import numpy as np
    rng = np.random.default_rng(42)
    closes = 10.0 + np.cumsum(rng.normal(0, 0.1, n))
    volumes = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pl.DataFrame({
        "close": closes, "open": closes * 0.99,
        "high": closes * 1.01, "low": closes * 0.98,
        "volume": volumes,
    })

def test_score_returns_breakdown():
    engine = ScoringEngine()
    df = _make_df()
    result = engine.score(df)
    assert isinstance(result, ScoreBreakdown)
    assert 0 <= result.total <= 100

def test_breakdown_components_sum():
    engine = ScoringEngine()
    df = _make_df()
    result = engine.score(df)
    weighted = (
        result.trend * 0.30 + result.bias * 0.20 + result.volume * 0.15
        + result.support * 0.10 + result.macd * 0.15 + result.rsi * 0.10
    )
    assert abs(weighted - result.total) < 0.01

def test_score_range():
    engine = ScoringEngine()
    for _ in range(5):
        df = _make_df()
        r = engine.score(df)
        for v in [r.trend, r.bias, r.volume, r.support, r.macd, r.rsi]:
            assert 0 <= v <= 100

def test_score_details_present():
    engine = ScoringEngine()
    df = _make_df()
    r = engine.score(df)
    assert "ema_slope" in r.details
    assert "volume_ratio" in r.details
    assert "rsi" in r.details

def test_insufficient_data():
    import numpy as np
    rng = np.random.default_rng(0)
    closes = 10.0 + np.cumsum(rng.normal(0, 0.1, 19))  # 19 rows < 20
    volumes = rng.integers(1_000_000, 5_000_000, 19).astype(float)
    df = pl.DataFrame({"close": closes, "open": closes * 0.99,
                       "high": closes * 1.01, "low": closes * 0.98, "volume": volumes})
    engine = ScoringEngine()
    r = engine.score(df)
    assert r.total == 0.0
    assert r.trend == 0.0
    assert r.details == {}
