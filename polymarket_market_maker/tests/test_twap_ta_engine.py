from __future__ import annotations

from decimal import Decimal
from time import time

from polymarket_market_maker.models import BookLevel, OrderBook
from polymarket_market_maker.providers.crypto_feed import KlineCandle, KlineSnapshot
from polymarket_market_maker.strategy.crypto_directional_shadow import (
    TwapTaProbabilityEngine,
    _indicator_bias,
)
from polymarket_market_maker.strategy.technical_indicators import IndicatorSnapshot


WINDOW_START = 1_800_000_000


def make_snapshot(
    *,
    window_start: int = WINDOW_START,
    open_price: float = 100.0,
    latest_price: float = 100.0,
    drift_per_min: float = 0.0,
    noise: float = 0.05,
    bars: int = 40,
) -> KlineSnapshot:
    """Build a snapshot whose first candle opens near window_start.

    `bars` defaults high enough (40) that MACD (needs 26+9) and KDJ/RSI have data.
    """
    candles_1m = []
    price = open_price
    # Place the window-open candle 10 bars into the history, as in the TWAP tests.
    for i in range(bars):
        c_open = price
        c_close = price + drift_per_min
        candles_1m.append(
            KlineCandle(
                open_time=window_start - (10 - i) * 60,
                open=Decimal(str(c_open)),
                high=Decimal(str(max(c_open, c_close) + noise)),
                low=Decimal(str(min(c_open, c_close) - noise)),
                close=Decimal(str(c_close)),
                volume=Decimal("1000"),
            )
        )
        price = c_close
    candles_5m = (
        KlineCandle(
            open_time=window_start - 300,
            open=Decimal(str(open_price)),
            high=Decimal(str(open_price + 1)),
            low=Decimal(str(open_price - 1)),
            close=Decimal(str(latest_price)),
            volume=Decimal("5000"),
        ),
    )
    return KlineSnapshot(
        symbol="BTCUSDT",
        candles_1m=tuple(candles_1m),
        candles_5m=candles_5m,
        latest_price=Decimal(str(latest_price)),
        timestamp=time(),
    )


def book(ask_price: str) -> OrderBook:
    return OrderBook(
        "token",
        bids=(BookLevel(Decimal("0.40"), Decimal(10)),),
        asks=(BookLevel(Decimal(ask_price), Decimal(10)),),
        tick_size=Decimal("0.01"),
        minimum_size=Decimal(1),
        observed_at=time(),
    )


def test_indicator_bias_bullish_when_all_agree() -> None:
    ind = IndicatorSnapshot(
        macd=1.0, macd_signal=0.5, macd_hist=0.5,  # bullish
        rsi=60.0,  # >55 bullish
        k=80.0, d=60.0, j=90.0,  # K>D bullish crossover, J mid-band
    )
    assert _indicator_bias(ind) > 0.0


def test_indicator_bias_bearish_when_all_agree() -> None:
    ind = IndicatorSnapshot(
        macd=-1.0, macd_signal=-0.5, macd_hist=-0.5,
        rsi=40.0,
        k=20.0, d=40.0, j=10.0,
    )
    assert _indicator_bias(ind) < 0.0


def test_indicator_bias_neutral_abstains() -> None:
    ind = IndicatorSnapshot(
        macd=0.0, macd_signal=0.0, macd_hist=0.0,
        rsi=50.0,
        k=50.0, d=50.0, j=50.0,
    )
    assert _indicator_bias(ind) == 0.0


def test_indicator_bias_overbought_fades() -> None:
    ind = IndicatorSnapshot(
        macd=0.0, macd_signal=0.0, macd_hist=0.0,
        rsi=75.0,  # overbought -> bearish vote
        k=50.0, d=50.0, j=100.0,  # J>=100 overbought -> bearish vote
    )
    assert _indicator_bias(ind) < 0.0


def test_twap_ta_rejects_within_min_remaining() -> None:
    engine = TwapTaProbabilityEngine(Decimal("0.05"), Decimal(10), min_remaining_seconds=15.0)
    snap = make_snapshot(latest_price=100.5)
    result = engine.decide("BTC", snap, WINDOW_START, book("0.50"), book("0.50"), 5.0)
    assert result.outcome is None
    assert "no entry" in result.reason


def test_twap_ta_price_ahead_favors_up() -> None:
    engine = TwapTaProbabilityEngine(Decimal("0.05"), Decimal(10), min_remaining_seconds=15.0)
    snap = make_snapshot(open_price=100.0, latest_price=100.8, drift_per_min=0.02, noise=0.05)
    result = engine.decide("BTC", snap, WINDOW_START, book("0.55"), book("0.55"), 30.0)
    assert result.outcome == "Up"
    assert result.model_probability > Decimal("0.5")
    assert result.momentum_signal.startswith("twap-ta-up")


def test_twap_ta_missing_window_open_is_rejected() -> None:
    engine = TwapTaProbabilityEngine(Decimal("0.05"), Decimal(10), min_remaining_seconds=15.0)
    snap = make_snapshot()
    result = engine.decide("BTC", snap, WINDOW_START + 10_000_000, book("0.50"), book("0.50"), 60.0)
    assert result.outcome is None
    assert "window open price unavailable" in result.reason


def test_twap_ta_nudge_is_bounded() -> None:
    # A dead-flat tape gives P(Up)=0.5; the TA nudge alone must not exceed MAX_TA_NUDGE.
    engine = TwapTaProbabilityEngine(Decimal("0.0"), Decimal(10), min_remaining_seconds=15.0)
    snap = make_snapshot(open_price=100.0, latest_price=100.0, drift_per_min=0.0, noise=0.05)
    result = engine.decide("BTC", snap, WINDOW_START, book("0.40"), book("0.40"), 120.0)
    if result.model_probability is not None:
        # Whichever side is chosen, its prob stays within 0.5 + MAX_TA_NUDGE.
        assert result.model_probability <= Decimal("0.5") + Decimal(str(engine.MAX_TA_NUDGE)) + Decimal("0.0001")
