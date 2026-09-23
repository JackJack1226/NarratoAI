from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _decimal(name: str, default: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default).strip())
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _integer(name: str, default: str, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, default).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    private_key: str | None
    funder: str | None
    signature_type: int
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    relayer_api_key: str | None
    relayer_api_address: str | None
    token_ids: tuple[str, ...]
    execution_token_ids: tuple[str, ...]
    approved_categories: tuple[str, ...]
    approved_crypto_assets: tuple[str, ...]
    max_order_loss: Decimal
    min_post_cost_edge: Decimal
    crypto_entry_cutoff_seconds: Decimal
    crypto_entry_start_seconds: Decimal
    shadow_crypto_volatility: Decimal
    directional_model: str  # "momentum" or "twap"
    twap_min_remaining_seconds: Decimal
    auto_discover_crypto: bool
    dynamic_execution: bool
    max_entries_per_hour: int
    max_discovery_window_minutes: Decimal
    discovery_refresh_seconds: Decimal
    crypto_market_duration_minutes: Decimal
    strategy_type: str
    crypto_min_arbitrage_margin: Decimal
    weather_min_edge: Decimal
    weather_markets: tuple[str, ...]  # raw WEATHER_MARKETS env string, parsed downstream
    dry_run: bool
    live_trading: bool
    max_total_exposure: Decimal
    max_market_exposure: Decimal
    max_one_sided_inventory: Decimal
    max_drawdown: Decimal
    quote_size: Decimal
    quote_interval_seconds: Decimal
    min_spread: Decimal
    fee_buffer: Decimal
    taker_fee_rate: Decimal
    inventory_skew_per_dollar: Decimal
    max_book_age_seconds: Decimal
    min_book_level_size: Decimal
    replace_price_threshold: Decimal
    max_consecutive_errors: int
    state_dir: Path
    log_dir: Path

    @property
    def has_explicit_api_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    def public_summary(self) -> dict[str, object]:
        return {
            "token_ids": self.token_ids,
            "execution_token_ids": self.execution_token_ids,
            "approved_categories": self.approved_categories,
            "approved_crypto_assets": self.approved_crypto_assets,
            "max_order_loss": str(self.max_order_loss),
            "min_post_cost_edge": str(self.min_post_cost_edge),
            "crypto_min_arbitrage_margin": str(self.crypto_min_arbitrage_margin),
            "weather_min_edge": str(self.weather_min_edge),
            "weather_market_count": len(self.weather_markets.split("\n")) if self.weather_markets else 0,
            "shadow_only": self.dry_run or self.strategy_type == "shadow",
            "auto_discover_crypto": self.auto_discover_crypto,
            "directional_model": self.directional_model,
            "strategy_type": self.strategy_type,
            "dynamic_execution": self.dynamic_execution,
            "max_entries_per_hour": self.max_entries_per_hour,
            "discovery_refresh_seconds": str(self.discovery_refresh_seconds),
            "crypto_market_duration_minutes": str(self.crypto_market_duration_minutes),
            "dry_run": self.dry_run,
            "live_trading": self.live_trading,
            "max_total_exposure": str(self.max_total_exposure),
            "max_market_exposure": str(self.max_market_exposure),
            "max_one_sided_inventory": str(self.max_one_sided_inventory),
            "taker_fee_rate": str(self.taker_fee_rate),
            "private_key": "<redacted>" if self.private_key else None,
            "api_credentials": "configured" if self.has_explicit_api_credentials else "derived at runtime",
            "relayer_api": "configured" if self.relayer_api_key else "not configured",
        }


def _token_list(name: str) -> tuple[str, ...]:
    return tuple(token.strip() for token in os.getenv(name, "").split(",") if token.strip())


def _choice_list(name: str, default: str, allowed: set[str]) -> tuple[str, ...]:
    values = tuple(item.strip().lower() for item in os.getenv(name, default).split(",") if item.strip())
    invalid = sorted(set(values) - allowed)
    if invalid:
        raise ConfigError(f"{name} contains unsupported values: {invalid}")
    if len(set(values)) != len(values):
        raise ConfigError(f"{name} cannot contain duplicates")
    return values


def load_settings(env_file: Path | None = None) -> Settings:
    if env_file:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv(Path(".env"), override=False)

    # Ensure proxy environment variables take effect for all requests calls
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        val = os.getenv(proxy_var, "").strip()
        if val:
            os.environ[proxy_var] = val
    token_ids = _token_list("POLYMARKET_TOKEN_IDS")
    execution_token_ids = _token_list("EXECUTION_TOKEN_IDS")
    approved_categories = _choice_list(
        "APPROVED_MARKET_CATEGORIES",
        "crypto_5m_directional,weather_bucket_global",
        {"crypto_5m_directional", "weather_bucket_global"},
    )
    approved_crypto_assets = _choice_list("APPROVED_CRYPTO_ASSETS", "btc,eth,sol", {"btc", "eth", "sol"})
    auto_discover = _boolean("AUTO_DISCOVER_CRYPTO_5MIN", True)
    dynamic_execution = _boolean("DYNAMIC_EXECUTION", False)
    if not token_ids and not auto_discover:
        raise ConfigError("POLYMARKET_TOKEN_IDS 为空时必须启用 AUTO_DISCOVER_CRYPTO_5MIN")

    dry_run = _boolean("DRY_RUN", True)
    live_trading = _boolean("LIVE_TRADING", False)
    strategy_type = os.getenv("STRATEGY_TYPE", "classic").strip().lower()
    if not dry_run and not live_trading:
        raise ConfigError("DRY_RUN=false requires LIVE_TRADING=true")
    if live_trading and not os.getenv("POLYMARKET_PRIVATE_KEY", "").strip() and not (
        os.getenv("POLYMARKET_API_KEY", "").strip() and
        os.getenv("POLYMARKET_API_SECRET", "").strip() and
        os.getenv("POLYMARKET_API_PASSPHRASE", "").strip()
    ):
        raise ConfigError("LIVE_TRADING requires either POLYMARKET_PRIVATE_KEY or API credentials")
    # Relayer API is optional but recommended for gasless trading
    # Automatic discovery can feed execution in shadow mode; live requires explicit approval at startup
    if (
        live_trading
        and not execution_token_ids
        and not dynamic_execution
        and strategy_type not in {"shadow", "crypto_5min"}
    ):
        raise ConfigError("实盘交易必须显式配置 EXECUTION_TOKEN_IDS 或启用 DYNAMIC_EXECUTION，自动发现的市场不会自动下单")
    if dynamic_execution and not live_trading:
        raise ConfigError("DYNAMIC_EXECUTION=true requires LIVE_TRADING=true")
    if strategy_type not in {"classic", "crypto_5min", "hybrid", "weather_observe", "shadow"}:
        raise ConfigError("STRATEGY_TYPE must be classic, crypto_5min, hybrid, weather_observe, or shadow")
    # Auto-discovery mode doesn't require manual token IDs
    unauthorized = [token for token in execution_token_ids if token_ids and token not in token_ids]
    if unauthorized:
        raise ConfigError(f"EXECUTION_TOKEN_IDS 含未在 POLYMARKET_TOKEN_IDS 中授权的市场：{unauthorized}")

    directional_model = os.getenv("DIRECTIONAL_MODEL", "momentum").strip().lower()
    if directional_model not in {"momentum", "twap", "twap_ta"}:
        raise ConfigError("DIRECTIONAL_MODEL must be 'momentum', 'twap', or 'twap_ta'")

    signature_type = _integer("POLYMARKET_SIGNATURE_TYPE", "0", minimum=0)
    quote_size = _decimal("QUOTE_SIZE", "2", minimum=Decimal("0.01"))
    min_spread = _decimal("MIN_SPREAD", "0.04", minimum=Decimal(0))
    fee_buffer = _decimal("FEE_BUFFER", "0.01", minimum=Decimal(0))
    # Crypto taker rate is 0.07 per https://docs.polymarket.com/trading/fees
    # (fee = shares * rate * price * (1-price)). Override per market category.
    taker_fee_rate = _decimal("POLYMARKET_TAKER_FEE_RATE", "0.07", minimum=Decimal(0))
    if min_spread <= fee_buffer:
        raise ConfigError("MIN_SPREAD must exceed FEE_BUFFER")

    settings = Settings(
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY", "").strip() or None,
        funder=os.getenv("POLYMARKET_FUNDER", "").strip() or None,
        signature_type=signature_type,
        api_key=os.getenv("POLYMARKET_API_KEY", "").strip() or None,
        api_secret=os.getenv("POLYMARKET_API_SECRET", "").strip() or None,
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", "").strip() or None,
        relayer_api_key=os.getenv("POLYMARKET_RELAYER_API_KEY", "").strip() or None,
        relayer_api_address=os.getenv("POLYMARKET_RELAYER_API_ADDRESS", "").strip() or None,
        token_ids=token_ids,
        execution_token_ids=execution_token_ids,
        approved_categories=approved_categories,
        approved_crypto_assets=approved_crypto_assets,
        max_order_loss=_decimal("MAX_ORDER_LOSS_USD", "1.00", minimum=Decimal("0.01")),
        min_post_cost_edge=_decimal("MIN_POST_COST_EDGE", "0.05", minimum=Decimal(0)),
        crypto_entry_cutoff_seconds=_decimal("CRYPTO_ENTRY_CUTOFF_SECONDS", "10", minimum=Decimal(1)),
        crypto_entry_start_seconds=_decimal("CRYPTO_ENTRY_START_SECONDS", "60", minimum=Decimal(10)),
        shadow_crypto_volatility=_decimal("SHADOW_CRYPTO_VOLATILITY", "0.005", minimum=Decimal("0.0001")),
        directional_model=directional_model,
        twap_min_remaining_seconds=_decimal("TWAP_MIN_REMAINING_SECONDS", "15", minimum=Decimal(1)),
        auto_discover_crypto=auto_discover,
        dynamic_execution=dynamic_execution,
        max_entries_per_hour=_integer("MAX_ENTRIES_PER_HOUR", "3", minimum=1),
        max_discovery_window_minutes=_decimal("MAX_DISCOVERY_WINDOW_MINUTES", "20", minimum=Decimal(1)),
        discovery_refresh_seconds=_decimal("DISCOVERY_REFRESH_SECONDS", "30", minimum=Decimal("1")),
        crypto_market_duration_minutes=_decimal("CRYPTO_MARKET_DURATION_MINUTES", "5", minimum=Decimal(1)),
        strategy_type=strategy_type,
        crypto_min_arbitrage_margin=_decimal("CRYPTO_MIN_ARBITRAGE_MARGIN", "0.04", minimum=Decimal(0)),
        weather_min_edge=_decimal("WEATHER_MIN_EDGE", "0.08", minimum=Decimal(0)),
        weather_markets=os.getenv("WEATHER_MARKETS", "").strip(),
        dry_run=dry_run,
        live_trading=live_trading,
        max_total_exposure=_decimal("MAX_TOTAL_EXPOSURE_USD", "3.00", minimum=Decimal(0)),
        max_market_exposure=_decimal("MAX_MARKET_EXPOSURE_USD", "1.50", minimum=Decimal(0)),
        max_one_sided_inventory=_decimal("MAX_ONE_SIDED_INVENTORY_USD", "1.00", minimum=Decimal(0)),
        max_drawdown=_decimal("MAX_DRAWDOWN_USD", "1.00", minimum=Decimal(0)),
        quote_size=quote_size,
        quote_interval_seconds=_decimal("QUOTE_INTERVAL_SECONDS", "3", minimum=Decimal("0.1")),
        min_spread=min_spread,
        fee_buffer=fee_buffer,
        taker_fee_rate=taker_fee_rate,
        inventory_skew_per_dollar=_decimal("INVENTORY_SKEW_PER_DOLLAR", "0.002", minimum=Decimal(0)),
        max_book_age_seconds=_decimal("MAX_BOOK_AGE_SECONDS", "10", minimum=Decimal(1)),
        min_book_level_size=_decimal("MIN_BOOK_LEVEL_SIZE", "5", minimum=Decimal(0)),
        replace_price_threshold=_decimal("REPLACE_PRICE_THRESHOLD", "0.01", minimum=Decimal(0)),
        max_consecutive_errors=_integer("MAX_CONSECUTIVE_ERRORS", "3", minimum=1),
        state_dir=Path(os.getenv("STATE_DIR", "state")),
        log_dir=Path(os.getenv("LOG_DIR", "logs")),
    )
    if settings.max_order_loss > settings.max_total_exposure:
        raise ConfigError("MAX_ORDER_LOSS_USD cannot exceed MAX_TOTAL_EXPOSURE_USD")
    if settings.max_market_exposure > settings.max_total_exposure:
        raise ConfigError("MAX_MARKET_EXPOSURE_USD cannot exceed MAX_TOTAL_EXPOSURE_USD")
    if settings.max_one_sided_inventory > settings.max_market_exposure:
        raise ConfigError("MAX_ONE_SIDED_INVENTORY_USD cannot exceed MAX_MARKET_EXPOSURE_USD")
    return settings
