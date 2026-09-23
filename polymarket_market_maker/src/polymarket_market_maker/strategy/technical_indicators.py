"""Pure-Python technical indicators (MACD / KDJ / RSI) computed from 1m closes.

These are classic momentum/oscillator indicators. They are computed here from
the OHLC series Binance already returns (no extra network calls). Note the
deliberate caution baked into the caller: on 5-minute Up/Down markets the price
path is close to a random walk, so these indicators fire mostly on noise. We
expose them as an *optional*, tagged feature so the shadow harness can A/B them
against the plain TWAP model and let the data decide, rather than assuming they
help.

All math uses float internally (indicator formulas are ratios/EMAs where
Decimal precision buys nothing) and returns floats; callers convert as needed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Computed technical-indicator values from a close-price series."""
    macd: float          # MACD line (fast EMA - slow EMA)
    macd_signal: float   # signal line (EMA of MACD line)
    macd_hist: float     # histogram (macd - signal); >0 bullish, <0 bearish
    rsi: float           # 0-100; >70 overbought, <30 oversold
    k: float             # KDJ %K (0-100)
    d: float             # KDJ %D (0-100)
    j: float             # KDJ %J (3K - 2D)


def _ema_series(values: list[float], period: int) -> list[float]:
    """Exponential moving average series, seeded with the first value."""
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


def compute_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI over the last `period` closes. Returns 50.0 when flat/insufficient."""
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    # Seed with the first `period` deltas.
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder smoothing over the remaining deltas.
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float, float]:
    """MACD line, signal line, histogram from the close series.

    Returns (0, 0, 0) when there aren't enough closes for the slow EMA + signal.
    """
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema_series(macd_line, signal)
    macd = macd_line[-1]
    sig = signal_line[-1]
    return macd, sig, macd - sig


def compute_kdj(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 9,
) -> tuple[float, float, float]:
    """KDJ stochastic oscillator (%K, %D, %J).

    Uses the classic 9-period RSV with 1/3 smoothing (equivalent to EMA-3).
    Returns (50, 50, 50) when insufficient data.
    """
    n = len(closes)
    if n < period or len(highs) < period or len(lows) < period:
        return 50.0, 50.0, 50.0
    k = 50.0
    d = 50.0
    # Walk forward from the first full window so K/D carry smoothing history.
    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1 : i + 1])
        window_low = min(lows[i - period + 1 : i + 1])
        if window_high == window_low:
            rsv = 50.0
        else:
            rsv = (closes[i] - window_low) / (window_high - window_low) * 100.0
        k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
        d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
    j = 3.0 * k - 2.0 * d
    return k, d, j


def compute_indicators(
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> IndicatorSnapshot:
    """Compute all indicators from aligned close/high/low series."""
    macd, macd_signal, macd_hist = compute_macd(closes)
    rsi = compute_rsi(closes)
    k, d, j = compute_kdj(highs, lows, closes)
    return IndicatorSnapshot(
        macd=macd,
        macd_signal=macd_signal,
        macd_hist=macd_hist,
        rsi=rsi,
        k=k,
        d=d,
        j=j,
    )
