from __future__ import annotations

from polymarket_market_maker.strategy.technical_indicators import (
    compute_indicators,
    compute_kdj,
    compute_macd,
    compute_rsi,
)


def test_rsi_insufficient_data_returns_neutral() -> None:
    assert compute_rsi([100.0, 101.0]) == 50.0


def test_rsi_monotonic_rise_is_high() -> None:
    closes = [float(100 + i) for i in range(30)]  # strictly increasing
    rsi = compute_rsi(closes)
    assert rsi > 90.0  # all gains, no losses -> RSI pinned near 100


def test_rsi_monotonic_fall_is_low() -> None:
    closes = [float(200 - i) for i in range(30)]  # strictly decreasing
    rsi = compute_rsi(closes)
    assert rsi < 10.0


def test_macd_insufficient_data_returns_zeros() -> None:
    assert compute_macd([1.0, 2.0, 3.0]) == (0.0, 0.0, 0.0)


def test_macd_uptrend_histogram_positive() -> None:
    closes = [float(100 + i) for i in range(60)]  # steady uptrend
    macd, signal, hist = compute_macd(closes)
    assert macd > 0.0  # fast EMA above slow EMA in an uptrend
    assert hist > 0.0  # macd above its signal line


def test_macd_downtrend_histogram_negative() -> None:
    closes = [float(200 - i) for i in range(60)]
    macd, signal, hist = compute_macd(closes)
    assert macd < 0.0
    assert hist < 0.0


def test_kdj_insufficient_data_returns_neutral() -> None:
    assert compute_kdj([1.0], [1.0], [1.0]) == (50.0, 50.0, 50.0)


def test_kdj_close_at_top_of_range_is_high() -> None:
    # Close near the window high on every bar -> K climbs toward 100.
    highs = [110.0] * 20
    lows = [100.0] * 20
    closes = [109.5] * 20
    k, d, j = compute_kdj(highs, lows, closes)
    assert k > 80.0
    assert j > 80.0  # J tracks K into the overbought zone


def test_kdj_close_at_bottom_of_range_is_low() -> None:
    highs = [110.0] * 20
    lows = [100.0] * 20
    closes = [100.5] * 20
    k, d, j = compute_kdj(highs, lows, closes)
    assert k < 20.0


def test_compute_indicators_aggregates_all() -> None:
    closes = [float(100 + i) for i in range(60)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    snap = compute_indicators(closes, highs, lows)
    assert snap.rsi > 90.0
    assert snap.macd_hist > 0.0
    assert 0.0 <= snap.k <= 100.0
