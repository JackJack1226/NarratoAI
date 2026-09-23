"""Backtest: replay past shadow trades through twap vs twap_ta using HISTORICAL
Binance klines from the decision moment.

For each resolved shadow trade we fetch the 1m klines that were available at
that time (from Binance historical kline endpoint), reconstruct the
KlineSnapshot, and run BOTH engines on the same data.  This gives a fair
side-by-side comparison on identical market conditions.

Usage:
    python scripts/backtest_twap_vs_twap_ta.py [--state-dir state] [--top-N]
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polymarket_market_maker.providers.crypto_feed import CryptoFeed, KlineSnapshot, KlineCandle
from polymarket_market_maker.strategy.crypto_directional_shadow import (
    TwapProbabilityEngine,
    TwapTaProbabilityEngine,
)
from polymarket_market_maker.models import OrderBook, BookLevel
from polymarket_market_maker.shadow_validator import ShadowValidator


# ── Binance historical kline fetcher ─────────────────────────────────────────

BINANCE_HOSTS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)


def _get_proxies() -> dict:
    proxies = {}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        v = __import__("os").environ.get(k)
        if v:
            proxies["http" if "http" in k else "https"] = v
    return proxies


def fetch_historical_klines(symbol: str, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict] | None:
    """Fetch historical 1m klines from Binance for a given time range."""
    sym = symbol.upper()
    last_exc = None
    for host in BINANCE_HOSTS:
        try:
            params = {"symbol": sym, "interval": "1m", "startTime": start_ms, "limit": limit}
            if end_ms:
                params["endTime"] = end_ms
            resp = requests.get(
                f"{host}/api/v3/klines",
                params=params,
                timeout=(5, 15),
            )
            if resp.status_code in (451, 403):
                last_exc = Exception(f"{resp.status_code} from {host}")
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            continue
    print(f"  [WARN] all Binance hosts failed for {sym}: {last_exc}")
    return None


def build_snapshot_from_klines(symbol: str, klines_raw: list[dict], current_price: D) -> KlineSnapshot | None:
    """Convert raw Binance klines to a KlineSnapshot."""
    if not klines_raw:
        return None
    candles = []
    for row in klines_raw:
        if len(row) < 6:
            continue
        candles.append(KlineCandle(
            open_time=row[0] / 1000.0,
            open=D(str(row[1])),
            high=D(str(row[2])),
            low=D(str(row[3])),
            close=D(str(row[4])),
            volume=D(str(row[5])),
        ))
    if not candles:
        return None
    # Find the 5m candle that contains the decision time
    # (we approximate: use the last 1m candle's window)
    return KlineSnapshot(
        symbol=symbol,
        candles_1m=tuple(candles),
        candles_5m=tuple(),
        latest_price=current_price,
        timestamp=time.time(),
    )


def make_book(price: float) -> OrderBook:
    p = D(str(max(0.01, price)))
    return OrderBook(
        token_id="dummy",
        bids=(BookLevel(D(str(max(0.01, price - 0.01))), D("10")),),
        asks=(BookLevel(p, D("10")),),
        tick_size=D("0.01"),
        minimum_size=D("1"),
        observed_at=time.time(),
    )


# ── Replay logic ─────────────────────────────────────────────────────────────

def replay_trade(record, feed: CryptoFeed | None = None) -> dict | None:
    """Replay one shadow trade using historical klines from the decision moment."""
    import datetime
    asset = record.asset
    symbol = f"{asset}USDT"
    window_start_ts = int(record.window_start_ts)
    window_end_ts = int(record.window_end_ts)

    # Decision was made near window_end_ts (a few seconds before expiry)
    decision_ts = window_end_ts - 10  # 10 seconds before expiry
    decision_ms = decision_ts * 1000

    # For historical trades, we need to fetch klines that include the window period
    # Fetch from window start - 1 hour to decision time
    start_ms = max(0, window_start_ts * 1000 - 60 * 60 * 1000)  # 1 hour before window start
    end_ms = decision_ms

    klines_raw = fetch_historical_klines(symbol, start_ms, end_ms=end_ms, limit=200)
    if not klines_raw:
        return {"error": "no klines", "asset": asset, "slug": record.slug}

    # Filter to klines within the window period (window_start to decision time)
    klines_window = [k for k in klines_raw if window_start_ts * 1000 <= k[0] <= decision_ms]
    if len(klines_window) < 15:
        # Fallback: use all available klines up to decision time
        klines_window = [k for k in klines_raw if k[0] <= decision_ms]

    if len(klines_window) < 15:
        return {"error": f"insufficient klines: {len(klines_window)}", "asset": asset, "slug": record.slug}

    # Build snapshot using the LAST known price before decision
    last_close = D(str(klines_window[-1][4]))
    snapshot = build_snapshot_from_klines(symbol, klines_window, last_close)
    if snapshot is None:
        return {"error": "snapshot build failed", "asset": asset, "slug": record.slug}

    # For historical replay, simulate being 30s from expiry
    remaining = 30.0

    up_book = make_book(0.5)
    down_book = make_book(0.5)

    twap_engine = TwapProbabilityEngine(
        min_post_cost_edge=D("0.05"),
        max_source_age_seconds=D("10"),
        min_remaining_seconds=15.0,
    )
    twap_ta_engine = TwapTaProbabilityEngine(
        min_post_cost_edge=D("0.05"),
        max_source_age_seconds=D("10"),
        min_remaining_seconds=15.0,
    )

    twap_result = twap_engine.decide(asset, snapshot, window_start_ts, up_book, down_book, remaining)
    ta_result = twap_ta_engine.decide(asset, snapshot, window_start_ts, up_book, down_book, remaining)

    # Debug: why was the decision NO_TRADE?
    if twap_result.outcome is None and ta_result.outcome is None:
        print(f"  [{i+1}/{len(trades)}] {asset} {record.slug[-10:]}: twap_reason={twap_result.reason!r}, ta_reason={ta_result.reason!r}")
        errors += 1
        return None

    return {
        "asset": asset,
        "slug": record.slug,
        "twap_outcome": twap_result.outcome,
        "twap_prob": float(twap_result.model_probability) if twap_result.model_probability else None,
        "twap_edge": float(twap_result.edge) if twap_result.edge else None,
        "twap_signal": twap_result.momentum_signal,
        "ta_outcome": ta_result.outcome,
        "ta_prob": float(ta_result.model_probability) if ta_result.model_probability else None,
        "ta_edge": float(ta_result.edge) if ta_result.edge else None,
        "ta_signal": ta_result.momentum_signal,
        "original_outcome": record.action,
        "original_won": record.won,
        "original_pnl": float(record.pnl_net) if record.pnl_net else None,
        "klines_count": len(klines_window),
    }


def summarize(results: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("TWAP vs TWAP_TA BACKTEST (replayed on historical klines)")
    print("=" * 72)

    for label, key in [("TWAP", "twap_outcome"), ("TWAP_TA", "ta_outcome")]:
        traded = [r for r in results if r.get(key) in ("Up", "Down")]
        won = [r for r in traded if r.get("original_won")]
        n, w = len(traded), len(won)
        wr = w / n * 100 if n else 0
        pnl = sum(r.get("original_pnl", 0) for r in won) - sum(
            abs(r.get("original_pnl", 0)) for r in traded if r not in won
        )
        avg_prob = sum(r.get(f"{key.lower()}_prob", 0) or 0 for r in traded) / n if n else 0
        print(f"\n[{label:8s}] trades={n:3d}, wins={w:2d}/{n}, win_rate={wr:5.1f}%, "
              f"pnl=${pnl:+.2f}, avg_prob={avg_prob:.4f}")

    # Agreement
    both = [r for r in results if r.get("twap_outcome") and r.get("ta_outcome")]
    agreed = [r for r in both if r["twap_outcome"] == r["ta_outcome"]]
    n_both = len(both)
    print(f"\nModel agreement: {len(agreed)}/{n_both} ({len(agreed)/n_both*100 if n_both else 0:.1f}%)")

    # Disagreements
    disagreed = [r for r in both if r["twap_outcome"] != r["ta_outcome"]]
    if disagreed:
        print(f"\nTA changed {len(disagreed)} calls:")
        for r in disagreed:
            status = "WON" if r.get("original_won") else "LOST"
            ta_sig = r.get("ta_signal", "")
            bias_str = f" [{ta_sig}]" if ta_sig and "bias=" in ta_sig else ""
            print(f"  {r['asset']:4s} twap={r['twap_outcome']:4s} -> ta={r['ta_outcome']:4s} | orig: {status}{bias_str}")

    # Per-asset
    print("\n--- Per Asset ---")
    by_asset = defaultdict(list)
    for r in results:
        by_asset[r.get("asset", "?")].append(r)
    for asset in sorted(by_asset):
        sub = by_asset[asset]
        for label, key in [("TWAP", "twap_outcome"), ("TWAP_TA", "ta_outcome")]:
            traded = [r for r in sub if r.get(key) in ("Up", "Down")]
            won = [r for r in traded if r.get("original_won")]
            n, w = len(traded), len(won)
            if n:
                print(f"  {asset:4s} {label:8s}: {w:2d}/{n:2d} ({w/n*100:4.1f}%)")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--top-n", type=int, default=0, help="Only replay last N resolved trades")
    args = parser.parse_args()

    validator = ShadowValidator(args.state_dir)
    trades = validator.load_trades()
    trades = [t for t in trades if t.resolved and t.action in ("Up", "Down")]
    if args.top_n > 0:
        trades = trades[-args.top_n:]
    print(f"Replaying {len(trades)} resolved trades on historical klines...\n")

    results: list[dict] = []
    errors = 0
    for i, record in enumerate(trades):
        r = replay_trade(record, None)
        if r and "error" in r:
            errors += 1
            print(f"  [{i+1}/{len(trades)}] {r['asset']} {r['slug'][-10:]}: {r['error']}")
            continue
        if r:
            results.append(r)
        if (i + 1) % 10 == 0:
            print(f"  replayed {i+1}/{len(trades)} ({len(results)} OK, {errors} errors)...")

    print(f"\nCompleted: {len(results)} replayed, {errors} errors")
    if results:
        summarize(results)
    else:
        print("No results — check network/connectivity to Binance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
