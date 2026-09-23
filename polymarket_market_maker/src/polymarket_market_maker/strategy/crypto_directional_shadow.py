from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from time import time

from ..models import OrderBook
from ..providers.crypto_feed import KlineSnapshot
from .technical_indicators import IndicatorSnapshot, compute_indicators


def _normal_cdf(x: float) -> float:
    """Standard normal CDF Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class DirectionalShadowDecision:
    asset: str
    outcome: str | None
    model_probability: Decimal | None
    market_price: Decimal | None
    edge: Decimal | None
    reason: str | None = None
    momentum_signal: str | None = None  # "bullish", "bearish", "neutral"
    volatility: Decimal | None = None   # ATR as ratio


@dataclass(frozen=True)
class KlineSignal:
    """Computed signal from kline data."""
    momentum: Decimal  # price change over lookback window, signed
    volatility_1m: Decimal  # ATR/1m as ratio of open
    volatility_5m: Decimal  # ATR/5m as ratio of open
    volume_trend: str  # "expanding", "shrinking", "neutral"
    close_vs_open_1m: Decimal  # last 1m close relative to open


class KlineDirectionalEngine:
    """Kline-based directional decision engine for shadow mode.

    Uses 1m and 5m Binance klines to estimate short-horizon directional
    probability. All calculations are shadow-only; no orders are placed.
    """

    VERSION = "shadow-directional-kline-v1"

    # Lookback windows
    MOMENTUM_WINDOW_1M = 5  # last 5 1m closes vs their first
    VOLATILITY_WINDOW_1M = 10  # ATR over last 10 1m candles
    VOLATILITY_WINDOW_5M = 3  # ATR over last 3 5m candles
    VOLUME_WINDOW = 5  # volume MA comparison window

    def __init__(self, min_post_cost_edge: Decimal, max_source_age_seconds: Decimal) -> None:
        self.min_post_cost_edge = min_post_cost_edge
        self.max_source_age_seconds = float(max_source_age_seconds)

    def compute_signal(self, snapshot: KlineSnapshot) -> KlineSignal | None:
        """Compute kline signal from a fresh Binance snapshot. Returns None if data is stale."""
        now = time()
        if now - snapshot.timestamp > self.max_source_age_seconds:
            return None

        candles_1m = snapshot.candles_1m
        candles_5m = snapshot.candles_5m

        if len(candles_1m) < self.MOMENTUM_WINDOW_1M or len(candles_5m) < 1:
            return None

        # Momentum: % change from first 1m candle close to last
        first_close = candles_1m[0].close
        last_close = candles_1m[-1].close
        momentum = (last_close - first_close) / first_close if first_close > 0 else Decimal(0)

        # 1m ATR (average true range as ratio)
        atr_1m_values = []
        for c in candles_1m[-self.VOLATILITY_WINDOW_1M:]:
            tr = max(c.high - c.low, abs(c.high - c.open), abs(c.low - c.open))
            atr_1m_values.append(tr / c.open if c.open > 0 else Decimal(0))
        volatility_1m = sum(atr_1m_values) / len(atr_1m_values) if atr_1m_values else Decimal(0)

        # 5m ATR
        atr_5m_values = []
        for c in candles_5m[-self.VOLATILITY_WINDOW_5M:]:
            tr = max(c.high - c.low, abs(c.high - c.open), abs(c.low - c.open))
            atr_5m_values.append(tr / c.open if c.open > 0 else Decimal(0))
        volatility_5m = sum(atr_5m_values) / len(atr_5m_values) if atr_5m_values else Decimal(0)

        # Volume trend
        recent_volumes = [c.volume for c in candles_1m[-self.VOLUME_WINDOW:]]
        if len(recent_volumes) >= 2:
            ma_prev = sum(recent_volumes[:-1]) / (len(recent_volumes) - 1)
            ma_curr = sum(recent_volumes) / len(recent_volumes)
            if ma_prev > 0:
                vol_ratio = ma_curr / ma_prev
                if vol_ratio > 1.3:
                    volume_trend = "expanding"
                elif vol_ratio < 0.7:
                    volume_trend = "shrinking"
                else:
                    volume_trend = "neutral"
            else:
                volume_trend = "neutral"
        else:
            volume_trend = "neutral"

        # Last 1m candle close vs open
        last_1m = candles_1m[-1]
        close_vs_open = (last_1m.close - last_1m.open) / last_1m.open if last_1m.open > 0 else Decimal(0)

        return KlineSignal(
            momentum=momentum,
            volatility_1m=volatility_1m,
            volatility_5m=volatility_5m,
            volume_trend=volume_trend,
            close_vs_open_1m=close_vs_open,
        )

    def decide(
        self,
        asset: str,
        signal: KlineSignal,
        up_book: OrderBook,
        down_book: OrderBook,
        remaining_seconds: float,
    ) -> DirectionalShadowDecision:
        """Decide direction based on kline signal + order book."""
        if up_book.best_ask is None or down_book.best_ask is None:
            return DirectionalShadowDecision(asset, None, None, None, None, "missing ask in outcome book")
        if remaining_seconds <= 10:
            return DirectionalShadowDecision(asset, None, None, None, None, "within 10s of market close — no entry")
        if signal.volatility_1m > Decimal("0.005"):  # >0.5% 1m ATR → too volatile
            return DirectionalShadowDecision(
                asset, None, None, None, None,
                f"1m volatility {signal.volatility_1m:.4f} exceeds 0.5% threshold",
            )

        # Score: momentum (primary) + close-vs-open (secondary confirmation)
        momentum_score = float(signal.momentum) * 1000  # scale to ~units
        close_score = float(signal.close_vs_open_1m) * 1000

        # Combine: up if positive momentum + green candle, down if negative + red
        combined_score = momentum_score + close_score * 0.5

        if combined_score > 0:
            outcome = "Up"
            model_prob = Decimal(str(min(0.95, 0.5 + float(combined_score) * 0.1)))
            momentum_label = "bullish"
        elif combined_score < 0:
            outcome = "Down"
            model_prob = Decimal(str(min(0.95, 0.5 + abs(float(combined_score)) * 0.1)))
            momentum_label = "bearish"
        else:
            return DirectionalShadowDecision(
                asset, None, None, None, None,
                f"no directional signal (momentum={float(signal.momentum):.4f})",
            )

        if outcome == "Up":
            market_price = up_book.best_ask.price
        else:
            market_price = down_book.best_ask.price

        edge = model_prob - market_price
        if edge < self.min_post_cost_edge:
            return DirectionalShadowDecision(
                asset, None, model_prob, market_price, edge,
                "post-cost edge below shadow threshold",
            )

        return DirectionalShadowDecision(
            asset=asset,
            outcome=outcome,
            model_probability=model_prob,
            market_price=market_price,
            edge=edge,
            reason=None,
            momentum_signal=momentum_label,
            volatility=signal.volatility_1m,
        )


class TwapProbabilityEngine:
    """Brownian-bridge directional probability model for 5-minute Up/Down markets.

    Unlike the momentum engine (which only reacts to recent price *direction*),
    this model estimates the true probability that the settlement price at the
    window close is at or above the window's opening price, given:
      - r_now = ln(P_now / P_start): how far ahead/behind we are right now
      - the remaining time in the window
      - σ (per-minute log-return volatility) estimated from recent 1m klines

    Model: over the remaining time, the log price moves by Δ ~ N(0, σ_rem²)
    with σ_rem = σ_1m · √(remaining_minutes) and negligible drift over ~5 min.
    The market resolves Up iff r_now + Δ ≥ 0, so
        P(Up) = Φ(r_now / σ_rem).
    As time runs out (σ_rem → 0), a positive lead → P→1 and a deficit → P→0,
    which is exactly why we can enter *early* the moment edge exceeds fees
    instead of waiting for the final seconds.

    Shadow-only: computes probabilities and edges; never places orders.
    """

    VERSION = "shadow-directional-twap-v1"

    # Volatility estimation windows.
    VOL_WINDOW_1M = 15  # 1m candles used to estimate per-minute log-return σ

    def __init__(
        self,
        min_post_cost_edge: Decimal,
        max_source_age_seconds: Decimal,
        min_remaining_seconds: float = 15.0,
        min_sigma_1m: float = 0.0002,
    ) -> None:
        self.min_post_cost_edge = min_post_cost_edge
        self.max_source_age_seconds = float(max_source_age_seconds)
        # Below this many seconds left, settlement TWAP noise dominates — skip.
        self.min_remaining_seconds = float(min_remaining_seconds)
        # Volatility floor so a flat recent tape doesn't produce σ_rem≈0 → P∈{0,1}.
        self.min_sigma_1m = float(min_sigma_1m)

    def _window_open_price(self, snapshot: KlineSnapshot, window_start_ts: int) -> float | None:
        """Recover the window's opening price from the 1m candle at window start."""
        for candle in snapshot.candles_1m:
            # open_time is seconds; the window's first 1m candle opens at window_start_ts.
            if abs(candle.open_time - window_start_ts) < 30:
                return float(candle.open)
        return None

    def _sigma_1m(self, snapshot: KlineSnapshot) -> float:
        """Estimate per-minute log-return volatility from recent 1m closes."""
        candles = snapshot.candles_1m[-self.VOL_WINDOW_1M:]
        closes = [float(c.close) for c in candles if c.close > 0]
        if len(closes) < 3:
            return self.min_sigma_1m
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 2:
            return self.min_sigma_1m
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return max(math.sqrt(var), self.min_sigma_1m)

    def decide(
        self,
        asset: str,
        snapshot: KlineSnapshot,
        window_start_ts: int,
        up_book: OrderBook,
        down_book: OrderBook,
        remaining_seconds: float,
    ) -> DirectionalShadowDecision:
        """Decide direction from the Brownian-bridge probability vs. the market ask."""
        if up_book.best_ask is None or down_book.best_ask is None:
            return DirectionalShadowDecision(asset, None, None, None, None, "missing ask in outcome book")
        if remaining_seconds <= self.min_remaining_seconds:
            return DirectionalShadowDecision(
                asset, None, None, None, None,
                f"within {self.min_remaining_seconds:.0f}s of close — settlement noise, no entry",
            )
        if snapshot is None or (time() - snapshot.timestamp) > self.max_source_age_seconds:
            return DirectionalShadowDecision(asset, None, None, None, None, "stale or missing Binance snapshot")

        p_start = self._window_open_price(snapshot, window_start_ts)
        if p_start is None or p_start <= 0:
            return DirectionalShadowDecision(
                asset, None, None, None, None, "window open price unavailable in kline history",
            )
        p_now = float(snapshot.latest_price)
        if p_now <= 0:
            return DirectionalShadowDecision(asset, None, None, None, None, "invalid current price")

        r_now = math.log(p_now / p_start)
        sigma_1m = self._sigma_1m(snapshot)
        sigma_rem = sigma_1m * math.sqrt(max(remaining_seconds, 1.0) / 60.0)
        if sigma_rem <= 0:
            return DirectionalShadowDecision(asset, None, None, None, None, "degenerate volatility estimate")

        p_up = _normal_cdf(r_now / sigma_rem)
        # Choose the side the model favors; model_prob is that side's probability.
        if p_up >= 0.5:
            outcome = "Up"
            model_prob = Decimal(str(p_up))
            market_price = up_book.best_ask.price
            label = "twap-up"
        else:
            outcome = "Down"
            model_prob = Decimal(str(1.0 - p_up))
            market_price = down_book.best_ask.price
            label = "twap-down"

        edge = model_prob - market_price
        if edge < self.min_post_cost_edge:
            return DirectionalShadowDecision(
                asset, None, model_prob, market_price, edge,
                "post-cost edge below shadow threshold",
            )

        return DirectionalShadowDecision(
            asset=asset,
            outcome=outcome,
            model_probability=model_prob,
            market_price=market_price,
            edge=edge,
            reason=None,
            momentum_signal=label,
            volatility=Decimal(str(sigma_1m)),
        )


def _indicator_bias(ind: IndicatorSnapshot) -> float:
    """Signed [-1, 1] directional bias from MACD/KDJ/RSI, by simple voting.

    Each indicator votes bullish (+) or bearish (-) with unit weight; the mean
    is the bias. Neutral zones (RSI≈50, KDJ mid-band, flat MACD) abstain so we
    don't manufacture a signal out of noise — the whole point of A/B-ing this is
    to see whether these votes carry any edge on a near-random 5-minute walk.
    """
    votes: list[float] = []

    # MACD: histogram sign is the fast-vs-signal momentum.
    if ind.macd_hist > 0:
        votes.append(1.0)
    elif ind.macd_hist < 0:
        votes.append(-1.0)

    # RSI: lean with the 50 line, but treat extremes as mean-reversion (fade).
    if ind.rsi >= 70.0:
        votes.append(-1.0)  # overbought → fade
    elif ind.rsi <= 30.0:
        votes.append(1.0)   # oversold → fade
    elif ind.rsi > 55.0:
        votes.append(1.0)
    elif ind.rsi < 45.0:
        votes.append(-1.0)

    # KDJ: J relative to the mid-band; extremes fade like RSI.
    if ind.j >= 100.0:
        votes.append(-1.0)  # overbought → fade
    elif ind.j <= 0.0:
        votes.append(1.0)   # oversold → fade
    elif ind.k > ind.d:
        votes.append(1.0)   # bullish crossover
    elif ind.k < ind.d:
        votes.append(-1.0)  # bearish crossover

    if not votes:
        return 0.0
    return sum(votes) / len(votes)


class TwapTaProbabilityEngine(TwapProbabilityEngine):
    """TWAP Brownian-bridge model *nudged* by MACD/KDJ/RSI indicators.

    This is the plain :class:`TwapProbabilityEngine` with one addition: after
    computing the principled probability P(Up)=Φ(r_now/σ_rem), we shift it by a
    small, capped amount in the direction the technical indicators agree on.

    The nudge is deliberately bounded (``MAX_TA_NUDGE``) so the Brownian-bridge
    stays the backbone and the indicators can only tilt marginal calls — they
    can't override a strong statistical read. Tagged as a distinct model so the
    shadow scorecard's MODEL BREAKDOWN A/B-tests it against plain ``twap``. If
    the indicators add nothing (the likely outcome on a 5-min random walk), the
    two models' win rates converge and we drop this one.
    """

    VERSION = "shadow-directional-twap-ta-v1"

    # Max probability shift the indicator vote may apply, in absolute terms.
    MAX_TA_NUDGE = 0.06

    def _ta_bias(self, snapshot: KlineSnapshot) -> float:
        """Indicator bias in [-1, 1] from the 1m OHLC series, 0.0 if insufficient."""
        closes = [float(c.close) for c in snapshot.candles_1m if c.close > 0]
        highs = [float(c.high) for c in snapshot.candles_1m if c.high > 0]
        lows = [float(c.low) for c in snapshot.candles_1m if c.low > 0]
        if len(closes) < 15:
            return 0.0
        ind = compute_indicators(closes, highs, lows)
        return _indicator_bias(ind)

    def decide(
        self,
        asset: str,
        snapshot: KlineSnapshot,
        window_start_ts: int,
        up_book: OrderBook,
        down_book: OrderBook,
        remaining_seconds: float,
    ) -> DirectionalShadowDecision:
        """Same gating/edge logic as TWAP, but tilt P(Up) by the indicator vote."""
        if up_book.best_ask is None or down_book.best_ask is None:
            return DirectionalShadowDecision(asset, None, None, None, None, "missing ask in outcome book")
        if remaining_seconds <= self.min_remaining_seconds:
            return DirectionalShadowDecision(
                asset, None, None, None, None,
                f"within {self.min_remaining_seconds:.0f}s of close — settlement noise, no entry",
            )
        if snapshot is None or (time() - snapshot.timestamp) > self.max_source_age_seconds:
            return DirectionalShadowDecision(asset, None, None, None, None, "stale or missing Binance snapshot")

        p_start = self._window_open_price(snapshot, window_start_ts)
        if p_start is None or p_start <= 0:
            return DirectionalShadowDecision(
                asset, None, None, None, None, "window open price unavailable in kline history",
            )
        p_now = float(snapshot.latest_price)
        if p_now <= 0:
            return DirectionalShadowDecision(asset, None, None, None, None, "invalid current price")

        r_now = math.log(p_now / p_start)
        sigma_1m = self._sigma_1m(snapshot)
        sigma_rem = sigma_1m * math.sqrt(max(remaining_seconds, 1.0) / 60.0)
        if sigma_rem <= 0:
            return DirectionalShadowDecision(asset, None, None, None, None, "degenerate volatility estimate")

        p_up = _normal_cdf(r_now / sigma_rem)
        # Apply the bounded indicator nudge, then clamp back into (0, 1).
        bias = self._ta_bias(snapshot)
        p_up = min(1.0, max(0.0, p_up + bias * self.MAX_TA_NUDGE))

        if p_up >= 0.5:
            outcome = "Up"
            model_prob = Decimal(str(p_up))
            market_price = up_book.best_ask.price
            label = f"twap-ta-up(bias={bias:+.2f})"
        else:
            outcome = "Down"
            model_prob = Decimal(str(1.0 - p_up))
            market_price = down_book.best_ask.price
            label = f"twap-ta-down(bias={bias:+.2f})"

        edge = model_prob - market_price
        if edge < self.min_post_cost_edge:
            return DirectionalShadowDecision(
                asset, None, model_prob, market_price, edge,
                "post-cost edge below shadow threshold",
            )

        return DirectionalShadowDecision(
            asset=asset,
            outcome=outcome,
            model_probability=model_prob,
            market_price=market_price,
            edge=edge,
            reason=None,
            momentum_signal=label,
            volatility=Decimal(str(sigma_1m)),
        )
