from __future__ import annotations

import logging
import os
import signal
import time
from collections import deque
from contextlib import AbstractContextManager
from decimal import Decimal
from pathlib import Path
from typing import Self

from .approved_market_registry import ApprovedMarketRegistry
from .config import Settings
from .exchange.client import Exchange, PolymarketClobExchange
from .exchange.order_manager import OrderManager
from .exchange.readonly import PublicReadOnlyExchange
from .execution_allowlist import ExecutionAllowlist
from .execution_cost import CostEstimationError, estimate_buy_cost, taker_fee
from .live_approval import LiveApproval
from .market_resolver import ActiveMarketResolver, VerifiedMarket
from .models import DesiredOrder, Inventory, Side
from .providers.crypto_feed import CryptoFeed
from .providers.gamma_feed import GammaMarketFeed, window_start_from_slug
from .providers.weather_feed import WeatherFeed, WeatherMarketSpec
from .providers.weather_market_feed import WeatherMarketFeed
from .risk import CircuitBreaker, RiskLimits, RiskManager
from .shadow_validator import ShadowValidator
from .state import StateStore
from .strategy.crypto_5min import Crypto5MinParameters
from .strategy.crypto_directional_shadow import (
    DirectionalShadowDecision,
    KlineDirectionalEngine,
    TwapProbabilityEngine,
    TwapTaProbabilityEngine,
)
from .strategy.directional_execution import plan_directional_order
from .strategy.hybrid import HybridParameters, HybridStrategy
from .strategy.market_maker import QuoteParameters
from .strategy.weather import WeatherParameters
from .weather_market_config import WeatherMarketConfig, load_weather_markets

logger = logging.getLogger(__name__)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                # Check exit code to make sure it hasn't exited
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    STILL_ACTIVE = 259
                    is_active = (exit_code.value == STILL_ACTIVE)
                    kernel32.CloseHandle(handle)
                    return is_active
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, Exception):
        return False


class ProcessLock(AbstractContextManager["ProcessLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            # Check if the lock belongs to a dead or invalid process
            try:
                content = self.path.read_text().strip()
                pid = int(content) if content.isdigit() else None
            except Exception:
                pid = None

            is_alive = _is_pid_alive(pid) if pid is not None else False
            if not is_alive:
                logger.info("Cleaning up stale lock %s (pid=%s)", self.path, pid)
                self.path.unlink(missing_ok=True)
                try:
                    self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    raise RuntimeError(f"another bot process owns {self.path}") from exc
            else:
                raise RuntimeError(f"another bot process (PID {pid}) owns {self.path}") from exc
        os.write(self._fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)


class MarketMakerRunner:
    # Every directional engine scored in parallel per market for a fair shadow A/B.
    DIRECTIONAL_MODELS = ("momentum", "twap", "twap_ta")

    def __init__(self, settings: Settings, exchange: Exchange | None = None, approval: LiveApproval | None = None) -> None:
        self.settings = settings
        self.approval = approval
        self.exchange = exchange or self._default_exchange(settings, approval)
        self.risk = RiskManager(
            RiskLimits(
                settings.max_total_exposure,
                settings.max_market_exposure,
                settings.max_one_sided_inventory,
                settings.max_drawdown,
            )
        )
        self.strategy = HybridStrategy(
            HybridParameters(
                classic_params=QuoteParameters(
                    settings.quote_size,
                    settings.min_spread,
                    settings.fee_buffer,
                    settings.inventory_skew_per_dollar,
                    settings.max_book_age_seconds,
                    settings.min_book_level_size,
                ),
                crypto_params=Crypto5MinParameters(
                    quote_size=settings.quote_size,
                    min_arbitrage_margin=settings.crypto_min_arbitrage_margin,
                    max_book_age=settings.max_book_age_seconds,
                    taker_fee_rate=settings.taker_fee_rate,
                ),
                weather_params=WeatherParameters(
                    quote_size=settings.quote_size,
                    min_edge=settings.weather_min_edge,
                    max_book_age=settings.max_book_age_seconds,
                    taker_fee_rate=settings.taker_fee_rate,
                ),
            )
        )
        # Shared with OrderManager: dynamic mode registers a rotating market's
        # token at entry time and revokes it once the market expires.
        self.allowlist = ExecutionAllowlist(settings.execution_token_ids)
        self.orders = OrderManager(
            self.exchange,
            self.risk,
            settings.dry_run,
            settings.replace_price_threshold,
            self.allowlist,
        )
        self.state = StateStore(settings.state_dir)
        self.registry = ApprovedMarketRegistry(settings)
        self.directional_shadow = KlineDirectionalEngine(settings.min_post_cost_edge, settings.max_book_age_seconds)
        self.twap_engine = TwapProbabilityEngine(
            settings.min_post_cost_edge,
            settings.max_book_age_seconds,
            min_remaining_seconds=float(settings.twap_min_remaining_seconds),
        )
        self.twap_ta_engine = TwapTaProbabilityEngine(
            settings.min_post_cost_edge,
            settings.max_book_age_seconds,
            min_remaining_seconds=float(settings.twap_min_remaining_seconds),
        )
        self.directional_model = settings.directional_model
        self.shadow_validator = ShadowValidator(
            settings.state_dir,
            taker_fee_rate=settings.taker_fee_rate,
            default_quote_size=settings.quote_size,
        )
        self.crypto_feed = CryptoFeed()
        self._shadow_opening_prices: dict[str, Decimal] = {}
        self._entered_markets: set[str] = set()
        self._entry_timestamps: deque[float] = deque()
        self._registered_tokens: set[str] = set()
        self.resolver = ActiveMarketResolver(self.exchange, GammaMarketFeed()) if settings.auto_discover_crypto else None
        self.weather_feed = WeatherFeed()
        self.weather_market_feed = WeatherMarketFeed()
        self.weather_configs: tuple[WeatherMarketConfig, ...] = load_weather_markets(settings.weather_markets)
        self._last_weather_discovery_at = 0.0
        self._last_discovery_at = 0.0
        self._stop_requested = False

    #: Strategy types that route decisions through the dynamic execution path.
    DYNAMIC_STRATEGIES = frozenset({"shadow", "crypto_5min"})

    @staticmethod
    def _armed(settings: Settings, approval: LiveApproval | None) -> bool:
        """True only when every explicit live-execution switch is intentionally set."""
        return (
            settings.dynamic_execution
            and settings.live_trading
            and settings.strategy_type in MarketMakerRunner.DYNAMIC_STRATEGIES
            and approval is not None
            and bool(approval.scope_assets)
        )

    @staticmethod
    def _weather_armed(settings: Settings, approval: LiveApproval | None) -> bool:
        """Weather execution armed: dynamic_execution + weather specs present."""
        return (
            settings.dynamic_execution
            and settings.live_trading
            and bool(settings.weather_markets)
            and approval is not None
        )

    @property
    def weather_armed(self) -> bool:
        return self._weather_armed(self.settings, self.approval)

    @property
    def dynamic_armed(self) -> bool:
        return self._armed(self.settings, self.approval)

    @staticmethod
    def _default_exchange(settings: Settings, approval: LiveApproval | None = None) -> Exchange:
        armed = MarketMakerRunner._armed(settings, approval)
        if settings.dry_run or (settings.strategy_type in MarketMakerRunner.DYNAMIC_STRATEGIES and not armed):
            return PublicReadOnlyExchange()
        # Live mode requires approval
        if approval is None:
            raise RuntimeError("Live mode requires a valid LiveApproval object")
        return PolymarketClobExchange(settings)

    def run(self, once: bool = False) -> None:
        with ProcessLock(self.settings.state_dir / "runner.lock"):
            self._install_signal_handlers()
            self.state.record("started", self.settings.public_summary())
            try:
                while not self._stop_requested and not self.risk.is_paused:
                    self._cycle()
                    if once:
                        break
                    time.sleep(float(self.settings.quote_interval_seconds))
            except KeyboardInterrupt:
                self._stop_requested = True
            except Exception:
                logger.exception("runner failed closed")
                self.risk.trip(CircuitBreaker.UNEXPECTED)
            finally:
                if self.risk.is_paused or self._stop_requested:
                    self._cancel_configured_orders()
                self.state.record("stopped", {"reason": self.risk.paused_reason.value if self.risk.paused_reason else "normal"})
                self.state.close()

    def _refresh_discovery(self) -> tuple:
        if self.resolver is None:
            return ()
        now = time.time()
        if now - self._last_discovery_at < float(self.settings.discovery_refresh_seconds):
            verified = self.resolver.prune_expired()
            self._sync_registered_tokens(verified)
            return verified
        logger.info("Crypto discovery stage started")
        try:
            resolved = self.resolver.resolve(
                max_markets=6,
                max_window_minutes=float(self.settings.max_discovery_window_minutes),
                expected_duration_minutes=float(self.settings.crypto_market_duration_minutes),
                assets=self.settings.approved_crypto_assets,
            )
        except Exception as exc:  # noqa: BLE001 - network glitches should not crash the runner
            logger.warning("Market discovery resolution failed: %s", exc)
            resolved = self.resolver.cached
        logger.info("Crypto discovery stage finished: candidates=%d", len(resolved))
        selected: dict[str, VerifiedMarket] = {}
        for market in resolved:
            if self.registry.policy.permits_crypto_asset(market.market.asset):
                if market.market.seconds_to_expiry is not None and market.market.seconds_to_expiry > 0:
                    if market.market.asset not in selected or (
                        market.market.seconds_to_expiry < selected[market.market.asset].market.seconds_to_expiry
                    ):
                        selected[market.market.asset] = market
        verified = tuple(selected.values())
        self._last_discovery_at = now
        self._sync_registered_tokens(verified)
        self.state.record(
            "crypto_discovery",
            {"markets": len(verified), "assets": [item.market.asset for item in verified]},
        )
        return verified

    def _refresh_weather_discovery(self) -> tuple:
        """Discover and filter weather markets by operator-configured specs."""
        if not self.weather_configs:
            return ()
        now = time.time()
        if now - self._last_weather_discovery_at < float(self.settings.discovery_refresh_seconds):
            return ()
        try:
            discovered = self.weather_market_feed.discover_weather_markets(limit=50, max_age_days=30)
        except Exception as exc:  # noqa: BLE001 - source failures must stay non-executable.
            logger.warning("Weather market discovery failed: %s", exc)
            return ()

        # Filter to configs whose condition_id matches a discovered market
        from .market_resolver import VerifiedMarket
        valid: list[VerifiedMarket] = []
        for dist in discovered:
            config = next((c for c in self.weather_configs if c.condition_id == dist.condition_id), None)
            if config is None:
                continue
            # Verify books are available
            try:
                books = tuple(self.exchange.get_book(tid) for tid in (config.token_yes, config.token_no))
                if len(books) != 2:
                    continue
            except Exception:
                continue
            # Re-attach config to the verified market via a fake wrapper
            verified = VerifiedMarket(
                market=dist,
                books=books,
                verified_at=time(),
            )
            valid.append(verified)

        self._last_weather_discovery_at = now
        self.state.record("weather_discovery", {"markets": len(valid), "configs": len(self.weather_configs)})
        return tuple(valid)

    def _sync_registered_tokens(self, verified: tuple) -> None:
        """Revoke execution authorization for tokens no longer in a live market."""
        live_tokens = {token_id for market in verified for token_id in getattr(market, "token_ids", ())}
        stale = self._registered_tokens - live_tokens
        if stale:
            self.allowlist.revoke(stale)
            self._registered_tokens -= stale
            self.state.record("execution_tokens_revoked", {"tokens": sorted(stale)})

    def _block_reason_for_model(
        self, model: str, asset: str, seconds_to_expiry: float | None
    ) -> str | None:
        """Per-model entry-window gate. The momentum model only trades the last
        10-60s; the TWAP-family models can enter any time they have edge, so they
        use a wider window bounded only by a floor (settlement noise) and the full
        market duration. Evaluated per model so a parallel A/B judges every engine
        by its own rules on the same market."""
        if model == "momentum":
            return self.registry.crypto_block_reason(asset, seconds_to_expiry)
        # TWAP (and TWAP+TA): permit the whole window down to the engine's min-remaining floor.
        if not self.registry.policy.permits_crypto_asset(asset):
            return f"{asset} is outside the approved crypto category"
        if seconds_to_expiry is None:
            return "market expiry time unavailable"
        floor = float(self.settings.twap_min_remaining_seconds)
        if seconds_to_expiry < floor:
            return f"within {floor:.0f}s of close — entry closed"
        # Guard against a mis-tagged long-dated market leaking into the 5m path.
        if seconds_to_expiry > float(self.settings.crypto_market_duration_minutes) * 60 + 30:
            return "market not yet in entry window"
        return None

    def _directional_block_reason(self, asset: str, seconds_to_expiry: float | None) -> str | None:
        """Entry-window gate for the *primary* (execution) model."""
        return self._block_reason_for_model(self.directional_model, asset, seconds_to_expiry)

    def _apply_shadow_cost_caps(
        self,
        decision: DirectionalShadowDecision,
        up_book: object,
        down_book: object,
    ) -> tuple[DirectionalShadowDecision, object | None]:
        """Apply the model-agnostic worst-case-cost cap and post-fee edge floor.

        Returns the (possibly downgraded to NO_TRADE) decision and the cost model.
        Identical to the single-model logic that used to be inline, so every
        engine in the parallel A/B is judged by the same risk gates.
        """
        selected_book = up_book if decision.outcome == "Up" else down_book if decision.outcome == "Down" else None
        cost = (
            estimate_buy_cost(
                selected_book,
                max(self.settings.quote_size, selected_book.minimum_size),
                fee_rate=self.settings.taker_fee_rate,
            )
            if selected_book is not None
            else None
        )
        if cost is not None and cost.worst_case_cost > self.registry.policy.max_order_loss:
            decision = type(decision)(
                decision.asset,
                None,
                decision.model_probability,
                decision.market_price,
                decision.edge,
                f"worst-case shadow cost {cost.worst_case_cost} (incl. taker fee @ {self.settings.taker_fee_rate*100:.0f}% rate) exceeds ${self.registry.policy.max_order_loss} limit",
            )
        elif cost is not None and decision.outcome is not None:
            # Fee-adjusted edge: use all_in_price_per_share (gross + fee) / size
            post_cost_edge = decision.model_probability - cost.all_in_price_per_share
            if post_cost_edge < self.registry.policy.min_post_cost_edge:
                decision = type(decision)(
                    decision.asset,
                    None,
                    decision.model_probability,
                    decision.market_price,
                    post_cost_edge,
                    "post-cost edge below shadow threshold (after taker fee)",
                )
        return decision, cost

    def _decide_one_model(
        self,
        model: str,
        market: object,
        snapshot: object,
        up_book: object,
        down_book: object,
        w_start_ts: int,
    ) -> tuple[DirectionalShadowDecision, object | None]:
        """Run a single engine on shared data + apply its window gate and cost caps."""
        asset = market.market.asset
        seconds = market.market.seconds_to_expiry
        block = self._block_reason_for_model(model, asset, seconds)
        if block:
            return DirectionalShadowDecision(asset, None, None, None, None, block), None
        try:
            if model == "twap":
                decision = self.twap_engine.decide(asset, snapshot, w_start_ts, up_book, down_book, seconds)
            elif model == "twap_ta":
                decision = self.twap_ta_engine.decide(asset, snapshot, w_start_ts, up_book, down_book, seconds)
            else:
                signal = self.directional_shadow.compute_signal(snapshot)
                if signal is None:
                    return (
                        DirectionalShadowDecision(
                            asset, None, None, None, None,
                            "kline signal unavailable (stale/insufficient data)",
                        ),
                        None,
                    )
                decision = self.directional_shadow.decide(asset, signal, up_book, down_book, seconds)
        except CostEstimationError as exc:
            return DirectionalShadowDecision(asset, None, None, None, None, str(exc)), None
        return self._apply_shadow_cost_caps(decision, up_book, down_book)

    def _record_shadow_directional_decision(self, market: object) -> tuple | None:
        """Score *every* directional model on the same market simultaneously.

        Each engine is recorded to the shadow harness under its own ``model`` tag,
        so the scorecard's MODEL BREAKDOWN compares them on the identical stream of
        markets — a fair A/B — rather than across time-separated runs. Only the
        configured primary model (`self.directional_model`) drives execution.
        """
        from .market_resolver import VerifiedMarket

        if not isinstance(market, VerifiedMarket):
            return None
        asset = market.market.asset
        w_start = window_start_from_slug(market.market.slug)
        w_start_ts = int(w_start.timestamp()) if w_start else (int(time.time()) // 300) * 300

        # Per-model window gates. If every model is blocked, skip the network hit.
        block_reasons = {
            m: self._block_reason_for_model(m, asset, market.market.seconds_to_expiry)
            for m in self.DIRECTIONAL_MODELS
        }
        if all(block_reasons.values()):
            for m, reason in block_reasons.items():
                self.shadow_validator.record_decision(
                    asset=asset,
                    slug=market.market.slug,
                    condition_id=market.market.condition_id,
                    window_start_ts=w_start_ts,
                    action="NO_TRADE",
                    reason=reason,
                    model=m,
                )
            self.state.record(
                "shadow_decision",
                {"asset": asset, "action": "NO_TRADE", "reason": block_reasons[self.directional_model]},
            )
            return None

        try:
            by_outcome = {book.outcome.strip().lower(): book.token_id for book in market.books}
            up_token, down_token = by_outcome["up"], by_outcome["down"]
            logger.info("Shadow book stage started: asset=%s", asset)
            up_book = self.exchange.get_book(up_token)
            down_book = self.exchange.get_book(down_token)
            logger.info("Shadow book stage finished: asset=%s", asset)
            symbol = f"{asset}USDT"
            logger.info("Binance snapshot stage started: symbol=%s", symbol)
            snapshot = self.crypto_feed.get_snapshot(symbol)
            logger.info("Binance snapshot stage finished: symbol=%s", symbol)

            decisions: dict[str, DirectionalShadowDecision] = {}
            for m in self.DIRECTIONAL_MODELS:
                decision, cost = self._decide_one_model(m, market, snapshot, up_book, down_book, w_start_ts)
                decisions[m] = decision
                self.shadow_validator.record_decision(
                    asset=asset,
                    slug=market.market.slug,
                    condition_id=market.market.condition_id,
                    window_start_ts=w_start_ts,
                    action=decision.outcome or "NO_TRADE",
                    model_probability=decision.model_probability,
                    market_price=decision.market_price,
                    quote_size=self.settings.quote_size,
                    edge=decision.edge,
                    momentum_signal=decision.momentum_signal,
                    kline_volatility=decision.volatility,
                    reason=decision.reason,
                    reference_spot_price=snapshot.latest_price if snapshot else None,
                    model=m,
                )

            primary = decisions[self.directional_model]
            self.state.record(
                "shadow_decision",
                {
                    "asset": asset,
                    "condition_id": market.market.condition_id,
                    "slug": market.market.slug,
                    "primary_model": self.directional_model,
                    "action": primary.outcome or "NO_TRADE",
                    "model_probability": str(primary.model_probability) if primary.model_probability else None,
                    "market_price": str(primary.market_price) if primary.market_price else None,
                    "edge": str(primary.edge) if primary.edge else None,
                    "reference_source": "Binance kline snapshot; not Polymarket settlement-aligned",
                    "fee_status": "unverified; shadow-only estimate excludes venue fee",
                    "momentum_signal": primary.momentum_signal,
                    "reason": primary.reason,
                    "ab_models": {m: (d.outcome or "NO_TRADE") for m, d in decisions.items()},
                    "policy_hash": self.registry.policy.manifest_hash(),
                },
            )

            return primary, up_token, down_token, up_book, down_book
        except (KeyError, CostEstimationError) as exc:
            logger.warning("Shadow decision failed asset=%s: %s", asset, exc)
            self.state.record("shadow_decision", {"asset": asset, "action": "NO_TRADE", "reason": str(exc)})
            return None
        except Exception as exc:  # noqa: BLE001 - source failures must stay non-executable.
            logger.warning("Shadow data stage failed asset=%s: %s", asset, exc)
            self.state.record("shadow_decision", {"asset": asset, "action": "NO_TRADE", "reason": str(exc)})
            return None

    def _record_paper_pair_decision(self, market: object) -> tuple | None:
        """Record the paired-book decision; return execution context when it is tradable."""
        from .market_resolver import VerifiedMarket

        if not isinstance(market, VerifiedMarket):
            return None
        try:
            books = tuple(self.exchange.get_book(token_id) for token_id in market.token_ids)
            if len(books) != 2:
                raise RuntimeError("market pair book count is invalid")
            decision = self.strategy.crypto_engine.decide_pair(market, books, self.settings.max_total_exposure)
            self.state.record(
                "crypto_paper_decision",
                {
                    "asset": decision.asset,
                    "token_ids": decision.token_ids,
                    "combined_ask_cost": str(decision.combined_ask_cost) if decision.combined_ask_cost else None,
                    "combined_fee_cost": str(decision.combined_fee_cost) if decision.combined_fee_cost else None,
                    "all_in_combined_cost": str(decision.all_in_combined_cost) if decision.all_in_combined_cost else None,
                    "arb_profit_per_share": str(decision.arb_profit_per_share) if decision.arb_profit_per_share else None,
                    "minimum_pair_notional": str(decision.minimum_pair_notional) if decision.minimum_pair_notional else None,
                    "paper_orders": len(decision.paper_orders),
                    "reason": decision.reason,
                },
            )
        except Exception as exc:  # noqa: BLE001 - any source fault suppresses the pair decision.
            self.state.record("crypto_paper_suppressed", {"asset": market.market.asset, "reason": str(exc)})
            return None
        if decision.reason or len(decision.paper_orders) != 2:
            return None
        return decision, books

    def _enter_pair(self, market: object, decision: object, books: tuple) -> None:
        """Place a hedged two-leg entry, rate-limited and once per market."""
        from .market_resolver import VerifiedMarket

        if not isinstance(market, VerifiedMarket):
            return
        condition_id = market.market.condition_id
        if condition_id in self._entered_markets:
            return

        now = time.time()
        while self._entry_timestamps and now - self._entry_timestamps[0] > 3600.0:
            self._entry_timestamps.popleft()
        if len(self._entry_timestamps) >= self.settings.max_entries_per_hour:
            self.state.record("pair_entry_skipped", {"asset": market.market.asset, "reason": "hourly entry cap reached"})
            return

        leg_books = {book.token_id: book for book in books}
        try:
            # Thinner leg first: if its depth vanished, fail before exposing the other side.
            ordered = sorted(decision.paper_orders, key=lambda order: leg_books[order.token_id].best_ask.size)
            tick_sizes = {order.token_id: leg_books[order.token_id].tick_size for order in ordered}
        except (KeyError, AttributeError) as exc:
            self.state.record("pair_entry_skipped", {"asset": market.market.asset, "reason": f"leg book unavailable: {exc}"})
            return

        self._entered_markets.add(condition_id)
        tokens = [order.token_id for order in ordered]
        self.allowlist.register(tokens)
        self._registered_tokens.update(tokens)

        try:
            inventories = {token: Inventory(token, self.exchange.get_inventory(token)) for token in tokens}
            result = self.orders.enter_pair(ordered[0], ordered[1], inventories, tick_sizes)
        except Exception as exc:  # noqa: BLE001 - a failed entry must not crash the runner.
            self.state.record("pair_entry_failed", {"asset": market.market.asset, "reason": str(exc)})
            logger.warning("pair entry failed asset=%s: %s", market.market.asset, exc)
            return

        self._entry_timestamps.append(now)
        self.state.record(
            "pair_entry",
            {
                "asset": market.market.asset,
                "condition_id": condition_id,
                "combined_ask_cost": str(decision.combined_ask_cost),
                "all_in_combined_cost": str(decision.all_in_combined_cost),
                "arb_profit_per_share": str(decision.arb_profit_per_share),
                "minimum_pair_notional": str(decision.minimum_pair_notional),
                "placed_legs": list(result.placed),
                "rolled_back": result.rolled_back,
                "reason": result.reason,
            },
        )
        if len(result.placed) == 1:
            logger.critical("PAIR ENTRY LEFT NAKED EXPOSURE asset=%s: %s", market.market.asset, result.reason)
        elif result.rolled_back:
            logger.warning("pair entry rolled back asset=%s: %s", market.market.asset, result.reason)

    def _cycle(self) -> None:
        if self.settings.strategy_type in {"crypto_5min", "hybrid", "shadow"} and self.resolver is not None:
            # Resolve any expired shadow trades
            if self.settings.strategy_type == "shadow":
                try:
                    newly_resolved = self.shadow_validator.resolve_pending()
                    if newly_resolved:
                        card = self.shadow_validator.compute_scorecard()
                        logger.info(
                            "Shadow Scorecard Updated: %dW/%dL (WinRate=%.1f%%) | NetPnL=$%.2f | TotalTrades=%d",
                            card.wins,
                            card.losses,
                            card.win_rate_pct,
                            card.net_pnl_usd,
                            card.total_trades,
                        )
                except Exception as exc:
                    logger.debug("Shadow trade resolution check failed: %s", exc)

            for market in self._refresh_discovery():
                if self.settings.strategy_type == "shadow":
                    context = self._record_shadow_directional_decision(market)
                    if context is not None and self.dynamic_armed:
                        self._enter_directional(market, *context)
                else:
                    context = self._record_paper_pair_decision(market)
                    if context is not None and self.dynamic_armed:
                        self._enter_pair(market, *context)

        if self.settings.strategy_type in {"crypto_5min", "weather_observe", "shadow"}:
            if self.settings.strategy_type == "shadow" and "weather_bucket_global" in self.settings.approved_categories:
                self.state.record(
                    "weather_shadow_decision",
                    {
                        "action": "NO_TRADE",
                        "reason": "global weather execution is blocked until market resolution contracts and source-compatible forecast adapters are verified",
                        "policy_hash": self.registry.policy.manifest_hash(),
                    },
                )
            self.state.record(
                "heartbeat",
                {"mode": self.settings.strategy_type, "desired_orders": 0, "policy": self.registry.summary()},
            )
            return

        # Weather execution branch
        if self.weather_armed:
            for market in self._refresh_weather_discovery():
                self._record_weather_decision(market)
                if self.dynamic_armed:
                    context = self._plan_weather_entry(market)
                    if context is not None:
                        self._enter_weather(market, *context)

        for token_id in self.settings.token_ids:
            try:
                book = self.exchange.get_book(token_id)
                self.state.record(
                    "book_snapshot",
                    {
                        "token_id": token_id,
                        "best_bid": str(book.best_bid.price) if book.best_bid else None,
                        "best_ask": str(book.best_ask.price) if book.best_ask else None,
                        "observed_at": book.observed_at,
                    },
                )
                inventory = Inventory(token_id, self.exchange.get_inventory(token_id))
                decision = self.strategy.decide(book, inventory)
                if decision.reason:
                    logger.warning("quote suppressed token=%s: %s", token_id, decision.reason)
                    if "stale" in decision.reason:
                        self.risk.trip(CircuitBreaker.STALE_BOOK)
                        return
                else:
                    self.orders.synchronize(token_id, decision.desired_orders, inventory, book.tick_size)
                    self.state.record(
                        "quote_decision",
                        {"token_id": token_id, "desired_orders": len(decision.desired_orders), "reason": decision.reason},
                    )
                    self.state.record("heartbeat", {"token_id": token_id, "desired_orders": len(decision.desired_orders)})
                    self.state.record("error_count", {"count": 0})
            except Exception as exc:  # noqa: BLE001 - all exchange faults must fail closed.
                logger.warning("cycle error token=%s: %s", token_id, exc)
                errors = (self.state.latest("error_count") or {}).get("count", 0) + 1
                self.state.record("error_count", {"count": errors})
                if errors >= self.settings.max_consecutive_errors:
                    self.risk.trip(CircuitBreaker.ERROR_BURST)
                    return

    def _enter_directional(self, market: object, decision: object, up_token: str, down_token: str, up_book: object, down_book: object) -> None:
        """Place at most one taker entry per rotating market, rate-limited per hour."""
        from .market_resolver import VerifiedMarket

        if not isinstance(market, VerifiedMarket):
            return
        condition_id = market.market.condition_id
        if condition_id in self._entered_markets:
            return

        now = time.time()
        while self._entry_timestamps and now - self._entry_timestamps[0] > 3600.0:
            self._entry_timestamps.popleft()
        if len(self._entry_timestamps) >= self.settings.max_entries_per_hour:
            self.state.record("directional_entry_skipped", {"asset": market.market.asset, "reason": "hourly entry cap reached"})
            return

        plan = plan_directional_order(
            decision,
            up_token=up_token,
            down_token=down_token,
            up_book=up_book,
            down_book=down_book,
            quote_size=self.settings.quote_size,
            max_order_loss=self.registry.policy.max_order_loss,
            max_market_exposure=self.settings.max_market_exposure,
            min_post_cost_edge=self.registry.policy.min_post_cost_edge,
            taker_fee_rate=self.settings.taker_fee_rate,
        )
        if plan is None:
            self.state.record("directional_entry_skipped", {"asset": market.market.asset, "reason": "no risk-bounded plan"})
            return

        # Mark as attempted before any remote write so a partial failure does not
        # cause a second entry into the same market on the next cycle.
        self._entered_markets.add(condition_id)
        self.allowlist.register([plan.token_id])
        self._registered_tokens.add(plan.token_id)
        book = up_book if plan.token_id == up_token else down_book
        try:
            # Taker entry uses FOK (Fill or Kill) to avoid being locked in unexecuted limit orders
            self.orders.synchronize(
                plan.token_id,
                (DesiredOrder(plan.token_id, plan.side, plan.price, plan.size),),
                Inventory(plan.token_id, self.exchange.get_inventory(plan.token_id)),
                book.tick_size,
                order_type="FOK",
            )
        except Exception as exc:  # noqa: BLE001 - a failed entry must not crash the runner.
            self.state.record("directional_entry_failed", {"asset": market.market.asset, "reason": str(exc)})
            logger.warning("directional entry failed asset=%s: %s", market.market.asset, exc)
            return
        self._entry_timestamps.append(now)
        self.state.record(
            "directional_entry",
            {
                "asset": market.market.asset,
                "condition_id": condition_id,
                "outcome": decision.outcome,
                "token_id": plan.token_id,
                "price": str(plan.price),
                "size": str(plan.size),
                "worst_case_cost": str(plan.worst_case_cost),
                "edge": str(plan.edge),
            },
        )

    def _cancel_configured_orders(self) -> None:
        if self.settings.dry_run:
            logger.info("dry-run shutdown: no remote cancellation attempted")
            return
        tokens = set(self.settings.token_ids) | set(self._registered_tokens)
        try:
            for token_id in tokens:
                order_ids = [order.order_id for order in self.exchange.get_open_orders(token_id)]
                self.exchange.cancel_orders(order_ids)
            logger.warning("configured market orders cancelled")
        except Exception as exc:  # noqa: BLE001 - record every cancellation failure for the operator.
            self.state.record("cancellation_failed", {"error": str(exc)})
            logger.critical("FAILED TO CANCEL CONFIGURED ORDERS: %s", exc)

    def _record_weather_decision(self, market: object) -> tuple | None:
        """Record a weather decision based on forecast vs market price."""
        from .market_resolver import VerifiedMarket
        if not isinstance(market, VerifiedMarket):
            return None

        # Find matching config
        config = next((c for c in self.weather_configs if c.condition_id == market.market.condition_id), None)
        if config is None:
            self.state.record("weather_decision", {
                "condition_id": market.market.condition_id,
                "action": "NO_TRADE",
                "reason": "no matching operator specification"
            })
            return None

        try:
            forecast = self.weather_feed.get_forecast(config.city, config.target_date)
        except Exception as exc:
            self.state.record("weather_decision", {
                "condition_id": market.market.condition_id,
                "action": "NO_TRADE",
                "reason": f"forecast fetch failed: {exc}"
            })
            return None

        # Calculate model probability
        model_prob = forecast.bucket_probability(WeatherMarketSpec(
            token_id=market.market.condition_id,
            city=config.city,
            target_date=config.target_date,
            minimum_temp_c=config.min_temp_c,
            maximum_temp_c=config.max_temp_c,
            resolution_source=config.resolution_source,
        ))

        # Get market prices
        books = {book.token_id: book for book in market.books}
        yes_token = config.token_yes if market.books[0].token_id == config.token_yes else config.token_no
        no_token = config.token_no if yes_token == config.token_yes else config.token_yes

        yes_book = books.get(yes_token)
        no_book = books.get(no_token)

        if not yes_book or not no_book:
            return None

        yes_ask = yes_book.best_ask
        no_ask = no_book.best_ask

        if not yes_ask or not no_ask:
            return None

        # Fee-adjusted per-share cost using the official formula:
        # per-share fee = feeRate * price * (1 - price). all-in = price + that fee.
        yes_all_in = yes_ask.price + taker_fee(Decimal(1), yes_ask.price, self.settings.taker_fee_rate)
        no_all_in = no_ask.price + taker_fee(Decimal(1), no_ask.price, self.settings.taker_fee_rate)

        yes_model_prob = model_prob
        no_model_prob = Decimal(1) - model_prob

        yes_edge = yes_model_prob - yes_all_in
        no_edge = no_model_prob - no_all_in

        # Pick best side with sufficient edge
        chosen_token = None
        chosen_ask = None
        chosen_edge = None
        chosen_prob = None

        if yes_edge >= self.settings.weather_min_edge:
            chosen_token = yes_token
            chosen_ask = yes_ask.price
            chosen_edge = yes_edge
            chosen_prob = yes_model_prob
        elif no_edge >= self.settings.weather_min_edge:
            chosen_token = no_token
            chosen_ask = no_ask.price
            chosen_edge = no_edge
            chosen_prob = no_model_prob

        if chosen_token is None:
            self.state.record("weather_decision", {
                "condition_id": market.market.condition_id,
                "city": config.city,
                "date": config.target_date.isoformat(),
                "action": "NO_TRADE",
                "model_prob_yes": str(yes_model_prob),
                "model_prob_no": str(no_model_prob),
                "yes_ask": str(yes_ask.price),
                "no_ask": str(no_ask.price),
                "yes_fee_adjusted_edge": str(yes_edge),
                "no_fee_adjusted_edge": str(no_edge),
                "reason": "fee-adjusted edge below threshold"
            })
            return None

        self.state.record("weather_decision", {
            "condition_id": market.market.condition_id,
            "city": config.city,
            "date": config.target_date.isoformat(),
            "action": "BUY",
            "token_id": chosen_token,
            "model_prob": str(chosen_prob),
            "market_price": str(chosen_ask),
            "fee_adjusted_edge": str(chosen_edge),
            "resolution_source": config.resolution_source,
            "forecast_temp": str(forecast.predicted_temp_c),
        })

        return (chosen_token, chosen_ask, chosen_prob, chosen_edge, yes_book, no_book)

    def _plan_weather_entry(self, market: object) -> tuple | None:
        """Plan a weather entry with size and cost checks."""
        from .models import DesiredOrder
        context = self._record_weather_decision(market)
        if context is None:
            return None

        token_id, ask_price, _model_prob, _edge, yes_book, no_book = context

        # Size and worst-case cost check including taker fees
        min_size = max(yes_book.minimum_size, no_book.minimum_size, self.settings.quote_size)
        gross_cost = ask_price * min_size
        worst_case_cost = gross_cost + taker_fee(min_size, ask_price, self.settings.taker_fee_rate)

        if worst_case_cost > self.settings.max_order_loss:
            self.state.record("weather_plan_rejected", {
                "token_id": token_id,
                "reason": f"worst_case_cost {worst_case_cost:.4f} > max_order_loss {self.settings.max_order_loss}"
            })
            return None

        return (DesiredOrder(token_id, Side.BUY, ask_price, min_size), yes_book, no_book)

    def _enter_weather(self, market: object, order: object, yes_book: object, no_book: object) -> None:
        """Place a weather entry order."""
        from .market_resolver import VerifiedMarket
        if not isinstance(market, VerifiedMarket):
            return

        condition_id = market.market.condition_id
        if condition_id in self._entered_markets:
            return

        now = time.time()
        while self._entry_timestamps and now - self._entry_timestamps[0] > 3600.0:
            self._entry_timestamps.popleft()
        if len(self._entry_timestamps) >= self.settings.max_entries_per_hour:
            self.state.record("weather_entry_skipped", {"reason": "hourly cap reached"})
            return

        # Validate order
        try:
            token_id = order.token_id
            price = order.price
            size = order.size
        except AttributeError:
            return

        # Check allowlist
        if token_id not in self.allowlist:
            self.allowlist.register([token_id])
            self._registered_tokens.add(token_id)

        # Place order
        book = yes_book if token_id == yes_book.token_id else no_book
        try:
            self.orders.synchronize(
                token_id,
                (order,),
                Inventory(token_id, self.exchange.get_inventory(token_id)),
                book.tick_size,
            )
        except Exception as exc:
            self.state.record("weather_entry_failed", {"reason": str(exc)})
            logger.warning("weather entry failed: %s", exc)
            return

        self._entered_markets.add(condition_id)
        self._entry_timestamps.append(now)
        self.state.record("weather_entry", {
            "condition_id": condition_id,
            "token_id": token_id,
            "price": str(price),
            "size": str(size),
            "edge": str(order.edge if hasattr(order, 'edge') else None),
        })

    def _install_signal_handlers(self) -> None:
        def stop_handler(*_: object) -> None:
            self._stop_requested = True

        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)
