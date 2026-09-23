from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from .execution_cost import POLYMARKET_TAKER_FEE_RATE, taker_fee

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
# Region-neutral public market-data mirror first, then primary + numbered mirrors.
# api.binance.com geo-blocks some regions with HTTP 451; we fail over across hosts.
BINANCE_KLINE_HOSTS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)

STANDARD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _get_proxies() -> dict[str, str]:
    http = os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""
    https = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or http
    proxies: dict[str, str] = {}
    if http:
        proxies["http"] = http
    if https:
        proxies["https"] = https
    return proxies


@dataclass
class ShadowTradeRecord:
    trade_id: str
    created_at: str
    asset: str
    slug: str
    condition_id: str
    window_start_ts: int
    window_end_ts: int
    action: str  # "Up", "Down", "NO_TRADE"
    model_probability: float | None
    market_price: float | None  # ask price per share
    quote_size: float  # virtual shares
    taker_fee_rate: float
    taker_fee_paid: float
    total_cost: float  # quote_size * market_price + taker_fee_paid
    edge: float | None
    momentum_signal: str | None
    kline_volatility: float | None
    reason: str | None
    reference_spot_price: float | None
    model: str = "momentum"  # which directional engine produced this: "momentum" | "twap"
    # Resolution fields
    resolved: bool = False
    actual_outcome: str | None = None  # "Up", "Down", or "Cancelled"
    resolution_source: str | None = None  # "gamma", "binance_kline", "manual"
    resolved_at: str | None = None
    won: bool | None = None
    payout: float = 0.0
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    roi_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShadowTradeRecord:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ShadowScorecard:
    total_decisions: int
    no_trade_count: int
    total_trades: int
    up_trades: int
    down_trades: int
    resolved_trades: int
    pending_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_pnl_usd: float
    total_fees_usd: float
    net_pnl_usd: float
    profit_factor: float  # total win $ / total loss $
    avg_trade_pnl_usd: float
    avg_trade_roi_pct: float
    max_drawdown_usd: float
    brier_score: float | None  # mean squared error of model_probability vs actual (0/1)
    asset_breakdown: dict[str, dict[str, Any]]
    taker_fee_rate_pct: float = 7.0  # taker fee rate used, for display
    model_breakdown: dict[str, dict[str, Any]] | None = None  # per-engine (momentum/twap) stats

    def summary_text(self) -> str:
        lines = [
            "============================================================",
            "        POLYMARKET 5-MIN DIRECTIONAL SHADOW SCORECARD       ",
            "============================================================",
            f"Total Market Decisions Evaluated : {self.total_decisions}",
            f"  - Filtered / Skipped (NO_TRADE): {self.no_trade_count}",
            f"  - Virtual Trades Taken        : {self.total_trades} (Up: {self.up_trades}, Down: {self.down_trades})",
            "------------------------------------------------------------",
            f"Resolved Trades                  : {self.resolved_trades}",
            f"Pending Settlement               : {self.pending_trades}",
            f"Win / Loss Count                 : {self.wins}W / {self.losses}L",
            f"Hit Rate (Win Rate)              : {self.win_rate_pct:.1f}%",
            "------------------------------------------------------------",
            f"Gross PnL                        : ${self.gross_pnl_usd:+.2f}",
            f"Taker Fees Paid (@ {self.taker_fee_rate_pct:.0f}% rate)      : ${self.total_fees_usd:.2f}",
            f"Net PnL (After Taker Fees)       : ${self.net_pnl_usd:+.2f}",
            f"Profit Factor                    : {self.profit_factor:.2f}" if self.profit_factor != float("inf") else "Profit Factor                    : N/A (no losses)",
            f"Avg PnL per Trade                : ${self.avg_trade_pnl_usd:+.2f} ({self.avg_trade_roi_pct:+.1f}%)",
            f"Max Drawdown                     : ${self.max_drawdown_usd:.2f}",
        ]
        if self.brier_score is not None:
            lines.append(f"Brier Score (Calibration error)  : {self.brier_score:.4f} (lower is better, <0.25 is predictive)")

        if self.model_breakdown and len(self.model_breakdown) >= 1:
            lines.append("------------------------------------------------------------")
            lines.append("MODEL BREAKDOWN (momentum vs twap):")
            for model_name, stats in self.model_breakdown.items():
                brier = stats.get("brier")
                brier_str = f" | Brier: {brier:.3f}" if brier is not None else ""
                lines.append(
                    f"  [{model_name}] Trades: {stats['trades']} | "
                    f"Wins: {stats['wins']}/{stats['resolved']} ({stats['win_rate_pct']:.1f}%) | "
                    f"Net PnL: ${stats['net_pnl']:+.2f}{brier_str}"
                )

        if self.asset_breakdown:
            lines.append("------------------------------------------------------------")
            lines.append("ASSET BREAKDOWN:")
            for asset, stats in self.asset_breakdown.items():
                lines.append(
                    f"  [{asset}] Trades: {stats['trades']} | "
                    f"Wins: {stats['wins']}/{stats['resolved']} ({stats['win_rate_pct']:.1f}%) | "
                    f"Net PnL: ${stats['net_pnl']:+.2f}"
                )
        lines.append("============================================================")
        return "\n".join(lines)


class ShadowValidator:
    """Persistent validation harness for recording, resolving, and scoring shadow trades."""

    def __init__(
        self,
        state_dir: Path,
        taker_fee_rate: Decimal = POLYMARKET_TAKER_FEE_RATE,
        default_quote_size: Decimal = Decimal("5.0"),
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_file = self.state_dir / "shadow_decisions.jsonl"
        self.trades_file = self.state_dir / "shadow_trades.jsonl"
        self.taker_fee_rate = taker_fee_rate
        self.default_quote_size = default_quote_size
        self._proxies = _get_proxies()

    def record_decision(
        self,
        asset: str,
        slug: str,
        condition_id: str,
        window_start_ts: int,
        action: str,  # "Up", "Down", "NO_TRADE"
        model_probability: Decimal | float | None = None,
        market_price: Decimal | float | None = None,
        quote_size: Decimal | float | None = None,
        edge: Decimal | float | None = None,
        momentum_signal: str | None = None,
        kline_volatility: Decimal | float | None = None,
        reason: str | None = None,
        reference_spot_price: Decimal | float | None = None,
        model: str = "momentum",
        custom_timestamp: str | None = None,
    ) -> ShadowTradeRecord:
        """Record a shadow decision and persist to disk immediately."""
        now_iso = custom_timestamp or datetime.now(UTC).isoformat()
        size_dec = Decimal(str(quote_size)) if quote_size is not None else self.default_quote_size
        price_dec = Decimal(str(market_price)) if market_price is not None else Decimal(0)
        fee_rate_dec = self.taker_fee_rate

        if action in ("Up", "Down") and price_dec > 0:
            fee_dec = taker_fee(size_dec, price_dec, fee_rate_dec)
            total_cost_dec = size_dec * price_dec + fee_dec
        else:
            fee_dec = Decimal(0)
            total_cost_dec = Decimal(0)

        window_end_ts = window_start_ts + 300
        # Include model: parallel A/B scores several engines on the same slug in the
        # same second, so slug+action+ts alone is not unique across models.
        trade_id = f"{slug}_{model}_{action}_{int(datetime.now(UTC).timestamp())}"

        record = ShadowTradeRecord(
            trade_id=trade_id,
            created_at=now_iso,
            asset=asset.upper(),
            slug=slug,
            condition_id=condition_id,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            action=action,
            model_probability=float(model_probability) if model_probability is not None else None,
            market_price=float(market_price) if market_price is not None else None,
            quote_size=float(size_dec),
            taker_fee_rate=float(fee_rate_dec),
            taker_fee_paid=float(fee_dec),
            total_cost=float(total_cost_dec),
            edge=float(edge) if edge is not None else None,
            momentum_signal=momentum_signal,
            kline_volatility=float(kline_volatility) if kline_volatility is not None else None,
            reason=reason,
            reference_spot_price=float(reference_spot_price) if reference_spot_price is not None else None,
            model=model,
        )

        # Append to all decisions log
        self._append_jsonl(self.decisions_file, record.to_dict())

        # If it was an actionable virtual trade, also track in trades file
        if action in ("Up", "Down"):
            self._append_jsonl(self.trades_file, record.to_dict())

        return record

    def load_trades(self) -> list[ShadowTradeRecord]:
        """Load all recorded shadow trades from disk."""
        if not self.trades_file.exists():
            return []
        trades = []
        with open(self.trades_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    trades.append(ShadowTradeRecord.from_dict(data))
                except Exception as exc:
                    logger.warning("Failed to parse trade line: %s (%s)", line, exc)
        return trades

    def load_all_decisions(self) -> list[ShadowTradeRecord]:
        """Load all decisions (including NO_TRADE) from disk."""
        if not self.decisions_file.exists():
            return []
        decisions = []
        with open(self.decisions_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    decisions.append(ShadowTradeRecord.from_dict(data))
                except Exception as exc:
                    logger.warning("Failed to parse decision line: %s (%s)", line, exc)
        return decisions

    def save_trades(self, trades: list[ShadowTradeRecord]) -> None:
        """Atomically overwrite trades file with updated records."""
        with tempfile.NamedTemporaryFile("w", dir=self.state_dir, delete=False, encoding="utf-8") as tmp:
            for t in trades:
                tmp.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
            temp_path = Path(tmp.name)
        temp_path.replace(self.trades_file)

    def resolve_pending(self, current_ts: int | None = None) -> list[ShadowTradeRecord]:
        """Check and resolve all pending trades whose 5-minute window has elapsed."""
        trades = self.load_trades()
        if not trades:
            return []

        now_ts = current_ts if current_ts is not None else int(datetime.now(UTC).timestamp())
        resolved_any = False
        newly_resolved: list[ShadowTradeRecord] = []

        for trade in trades:
            if trade.resolved or trade.action not in ("Up", "Down"):
                continue
            # Allow 30 seconds after window close for settlement data to propagate
            if now_ts < trade.window_end_ts + 15:
                continue

            # Attempt 1: Polymarket Gamma API
            outcome, source = self._resolve_from_gamma(trade.slug, trade.condition_id)

            # Attempt 2: Binance Kline TWAP Fallback if Gamma hasn't resolved yet but window is in past
            if outcome is None and now_ts >= trade.window_end_ts + 60:
                outcome, source = self._resolve_from_binance(trade.asset, trade.window_start_ts, trade.window_end_ts)

            if outcome is not None:
                self._apply_resolution(trade, outcome, source)
                resolved_any = True
                newly_resolved.append(trade)
                logger.info(
                    "Resolved shadow trade %s: Asset=%s Action=%s Outcome=%s Won=%s NetPnL=$%.2f",
                    trade.trade_id, trade.asset, trade.action, outcome, trade.won, trade.pnl_net
                )

        if resolved_any:
            self.save_trades(trades)

        return newly_resolved

    def _resolve_from_gamma(self, slug: str, condition_id: str) -> tuple[str | None, str | None]:
        """Query Polymarket Gamma API for official market outcome."""
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                headers=STANDARD_HEADERS,
                proxies=self._proxies,
                timeout=(5, 10),
            )
            if r.status_code != 200:
                return None, None
            data = r.json()
            if not isinstance(data, list) or not data:
                return None, None
            market = data[0]

            # Check if market is closed/resolved
            closed = market.get("closed", False)
            uma_status = market.get("umaResolutionStatus")

            # Parse tokens and winner flags
            tokens = market.get("tokens", [])
            for tok in tokens:
                if tok.get("winner") is True:
                    outcome = tok.get("outcome", "").strip()
                    if outcome.lower() in ("up", "down"):
                        return outcome.capitalize(), "gamma"

            # Parse outcomePrices (e.g. ["1", "0"] or ["0", "1"])
            outcome_prices = market.get("outcomePrices")
            outcomes = market.get("outcomes")
            if closed and outcome_prices and outcomes:
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except Exception:
                        pass
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        pass
                if len(outcome_prices) == 2 and len(outcomes) == 2:
                    p0 = float(outcome_prices[0])
                    p1 = float(outcome_prices[1])
                    if p0 > 0.9:
                        return outcomes[0].strip().capitalize(), "gamma"
                    if p1 > 0.9:
                        return outcomes[1].strip().capitalize(), "gamma"

        except Exception as exc:
            logger.debug("Gamma resolution lookup error for %s: %s", slug, exc)

        return None, None

    def _resolve_from_binance(self, asset: str, start_ts: int, end_ts: int) -> tuple[str | None, str | None]:
        """Compute window outcome using Binance 1-minute klines.

        Fails over across region-neutral / primary hosts because api.binance.com
        returns HTTP 451 in geo-blocked regions.
        """
        symbol = f"{asset.upper()}USDT"
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": start_ts * 1000,
            "endTime": end_ts * 1000,
            "limit": 10,
        }
        for host in BINANCE_KLINE_HOSTS:
            try:
                r = requests.get(
                    f"{host}/api/v3/klines",
                    params=params,
                    headers=STANDARD_HEADERS,
                    proxies=self._proxies,
                    timeout=(5, 10),
                )
                if r.status_code in (451, 403):
                    continue  # geo/legal block — try next host
                if r.status_code != 200:
                    continue
                klines = r.json()
                if not isinstance(klines, list) or len(klines) < 4:
                    continue

                # First candle open = start price
                open_price = float(klines[0][1])
                # Last candle close = end price
                close_price = float(klines[-1][4])

                outcome = "Up" if close_price >= open_price else "Down"
                return outcome, "binance_kline"
            except Exception as exc:  # noqa: BLE001 - fail over to the next host
                logger.debug("Binance host %s failed for %s: %s", host, asset, exc)
                continue
        return None, None

    def _apply_resolution(self, trade: ShadowTradeRecord, outcome: str, source: str) -> None:
        trade.resolved = True
        trade.actual_outcome = outcome
        trade.resolution_source = source
        trade.resolved_at = datetime.now(UTC).isoformat()

        trade.won = (trade.action.lower() == outcome.lower())
        trade.payout = trade.quote_size * 1.0 if trade.won else 0.0

        # Gross PnL = Payout - Gross Purchase Cost
        gross_cost = trade.quote_size * (trade.market_price or 0.0)
        trade.pnl_gross = trade.payout - gross_cost

        # Net PnL = Gross PnL - Taker Fees Paid
        trade.pnl_net = trade.pnl_gross - trade.taker_fee_paid

        if trade.total_cost > 0:
            trade.roi_pct = (trade.pnl_net / trade.total_cost) * 100.0
        else:
            trade.roi_pct = 0.0

    def compute_scorecard(self) -> ShadowScorecard:
        """Calculate comprehensive performance metrics across all shadow records."""
        decisions = self.load_all_decisions()
        trades = self.load_trades()

        total_decisions = len(decisions)
        no_trade_count = sum(1 for d in decisions if d.action == "NO_TRADE")
        total_trades = len(trades)
        up_trades = sum(1 for t in trades if t.action == "Up")
        down_trades = sum(1 for t in trades if t.action == "Down")

        resolved_trades_list = [t for t in trades if t.resolved]
        resolved_count = len(resolved_trades_list)
        pending_count = total_trades - resolved_count

        wins = sum(1 for t in resolved_trades_list if t.won is True)
        losses = sum(1 for t in resolved_trades_list if t.won is False)
        win_rate_pct = (wins / resolved_count * 100.0) if resolved_count > 0 else 0.0

        gross_pnl_usd = sum(t.pnl_gross for t in resolved_trades_list)
        total_fees_usd = sum(t.taker_fee_paid for t in resolved_trades_list)
        net_pnl_usd = sum(t.pnl_net for t in resolved_trades_list)

        total_win_dollars = sum(t.pnl_net for t in resolved_trades_list if t.pnl_net > 0)
        total_loss_dollars = abs(sum(t.pnl_net for t in resolved_trades_list if t.pnl_net < 0))

        if total_loss_dollars > 0:
            profit_factor = total_win_dollars / total_loss_dollars
        elif total_win_dollars > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        avg_trade_pnl = (net_pnl_usd / resolved_count) if resolved_count > 0 else 0.0
        avg_trade_roi = (sum(t.roi_pct for t in resolved_trades_list) / resolved_count) if resolved_count > 0 else 0.0

        # Calculate max drawdown
        cumulative_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        for t in resolved_trades_list:
            cumulative_pnl += t.pnl_net
            if cumulative_pnl > peak_pnl:
                peak_pnl = cumulative_pnl
            dd = peak_pnl - cumulative_pnl
            if dd > max_drawdown:
                max_drawdown = dd

        # Calculate Brier score
        brier_errors = []
        for t in resolved_trades_list:
            if t.model_probability is not None and t.actual_outcome is not None:
                # Target: 1.0 if actual outcome was "Up", 0.0 if "Down"
                # If model predicted P(Up), error is (P(Up) - actual_is_up)^2
                # Note: If action was Down, model_probability is P(Down)
                actual_val = 1.0 if t.won else 0.0
                pred_val = t.model_probability
                brier_errors.append((pred_val - actual_val) ** 2)

        brier_score = (sum(brier_errors) / len(brier_errors)) if brier_errors else None

        # Per-asset breakdown
        asset_map: dict[str, list[ShadowTradeRecord]] = {}
        for t in resolved_trades_list:
            asset_map.setdefault(t.asset, []).append(t)

        asset_breakdown = {}
        for asset, a_trades in asset_map.items():
            a_res = len(a_trades)
            a_wins = sum(1 for t in a_trades if t.won is True)
            a_wr = (a_wins / a_res * 100.0) if a_res > 0 else 0.0
            a_net = sum(t.pnl_net for t in a_trades)
            asset_breakdown[asset] = {
                "trades": len([t for t in trades if t.asset == asset]),
                "resolved": a_res,
                "wins": a_wins,
                "losses": a_res - a_wins,
                "win_rate_pct": a_wr,
                "net_pnl": a_net,
            }

        # Per-model (momentum vs twap) breakdown for A/B comparison
        model_map: dict[str, list[ShadowTradeRecord]] = {}
        for t in resolved_trades_list:
            model_map.setdefault(getattr(t, "model", "momentum") or "momentum", []).append(t)
        model_breakdown: dict[str, dict[str, Any]] = {}
        for model_name, m_trades in model_map.items():
            m_res = len(m_trades)
            m_wins = sum(1 for t in m_trades if t.won is True)
            m_wr = (m_wins / m_res * 100.0) if m_res > 0 else 0.0
            m_net = sum(t.pnl_net for t in m_trades)
            m_briers = [
                (t.model_probability - (1.0 if t.won else 0.0)) ** 2
                for t in m_trades
                if t.model_probability is not None
            ]
            model_breakdown[model_name] = {
                "trades": len([t for t in trades if (getattr(t, "model", "momentum") or "momentum") == model_name]),
                "resolved": m_res,
                "wins": m_wins,
                "losses": m_res - m_wins,
                "win_rate_pct": m_wr,
                "net_pnl": m_net,
                "brier": (sum(m_briers) / len(m_briers)) if m_briers else None,
            }

        return ShadowScorecard(
            total_decisions=total_decisions,
            no_trade_count=no_trade_count,
            total_trades=total_trades,
            up_trades=up_trades,
            down_trades=down_trades,
            resolved_trades=resolved_count,
            pending_trades=pending_count,
            wins=wins,
            losses=losses,
            win_rate_pct=win_rate_pct,
            gross_pnl_usd=gross_pnl_usd,
            total_fees_usd=total_fees_usd,
            net_pnl_usd=net_pnl_usd,
            profit_factor=profit_factor,
            avg_trade_pnl_usd=avg_trade_pnl,
            avg_trade_roi_pct=avg_trade_roi,
            max_drawdown_usd=max_drawdown,
            brier_score=brier_score,
            asset_breakdown=asset_breakdown,
            taker_fee_rate_pct=float(self.taker_fee_rate) * 100.0,
            model_breakdown=model_breakdown,
        )

    def _append_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
