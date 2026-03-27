"""Application configuration management."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment or `.env`."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    gemini_model: str = Field(
        default="gemini-3-flash-preview",
        alias="GEMINI_MODEL",
    )

    mcp_dexscreener_cmd: str = Field(
        default="node /path/to/mcp-dexscreener/index.js",
        alias="MCP_DEXSCREENER_CMD",
    )
    mcp_dexpaprika_cmd: str = Field(
        default="dexpaprika-mcp",
        alias="MCP_DEXPAPRIKA_CMD",
    )
    mcp_rugcheck_cmd: str = Field(
        default="",
        alias="MCP_RUGCHECK_CMD",
    )
    mcp_solana_rpc_cmd: str = Field(
        default="",
        alias="MCP_SOLANA_RPC_CMD",
    )
    mcp_trader_cmd: str = Field(
        default="",
        alias="MCP_TRADER_CMD",
    )

    agentic_max_iterations: int = Field(
        default=15, alias="AGENTIC_MAX_ITERATIONS", ge=1, le=25
    )
    agentic_max_tool_calls: int = Field(
        default=30, alias="AGENTIC_MAX_TOOL_CALLS", ge=1, le=100
    )
    agentic_timeout_seconds: int = Field(
        default=120, alias="AGENTIC_TIMEOUT_SECONDS", ge=10, le=300
    )
    mcp_call_timeout: int = Field(
        default=90, alias="MCP_CALL_TIMEOUT", ge=10, le=600
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Price cache settings
    price_cache_ttl_seconds: int = Field(
        default=30, alias="PRICE_CACHE_TTL_SECONDS", ge=5, le=300
    )

    # Telegram settings
    telegram_bot_token: str = Field(
        default="", alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_chat_id: str = Field(
        default="", alias="TELEGRAM_CHAT_ID"
    )
    telegram_alerts_enabled: bool = Field(
        default=False, alias="TELEGRAM_ALERTS_ENABLED"
    )
    telegram_private_mode: bool = Field(
        default=True, alias="TELEGRAM_PRIVATE_MODE"
    )
    telegram_subscribers_db_path: Path = Field(
        default=Path.home() / ".dex-bot" / "telegram_subscribers.db",
        alias="TELEGRAM_SUBSCRIBERS_DB_PATH",
    )

    # Solana RPC URL for on-chain lookups (e.g. token decimals)
    solana_rpc_url: str = Field(
        default="https://api.mainnet-beta.solana.com",
        alias="SOLANA_RPC_URL",
    )

    # Internal API secret — shared between the MCP gateway and the FastAPI analysis
    # service.  Must be set to a strong random value in production.  When set, the
    # FastAPI /analyze endpoint rejects any request that does not carry the matching
    # X-Internal-API-Key header, preventing direct bypass of x402 payment enforcement.
    internal_api_secret: str = Field(default="", alias="INTERNAL_API_SECRET")

    # Trader MCP environment — forwarded to the trader subprocess
    solana_private_key: str = Field(default="", alias="SOLANA_PRIVATE_KEY")
    jupiter_api_base: str = Field(default="", alias="JUPITER_API_BASE")
    jupiter_api_key: str = Field(default="", alias="JUPITER_API_KEY")

    # Portfolio strategy settings (discover → hold → exit)
    portfolio_enabled: bool = Field(
        default=False, alias="PORTFOLIO_ENABLED"
    )
    portfolio_dry_run: bool = Field(
        default=True, alias="PORTFOLIO_DRY_RUN"
    )
    portfolio_chain: str = Field(
        default="solana", alias="PORTFOLIO_CHAIN"
    )
    portfolio_max_positions: int = Field(
        default=5, alias="PORTFOLIO_MAX_POSITIONS", ge=1, le=50
    )
    portfolio_position_size_usd: float = Field(
        default=5.0, alias="PORTFOLIO_POSITION_SIZE_USD", ge=0.01
    )
    portfolio_take_profit_pct: float = Field(
        default=0.0, alias="PORTFOLIO_TAKE_PROFIT_PCT", ge=0.0, le=500.0
    )
    portfolio_stop_loss_pct: float = Field(
        default=17.0, alias="PORTFOLIO_STOP_LOSS_PCT", ge=0.1, le=100.0
    )
    portfolio_trailing_stop_pct: float = Field(
        default=11.0, alias="PORTFOLIO_TRAILING_STOP_PCT", ge=0.1, le=100.0
    )
    portfolio_sell_pct: float = Field(
        default=45.0, alias="PORTFOLIO_SELL_PCT", ge=0.1, le=100.0
    )
    portfolio_max_hold_hours: int = Field(
        default=24, alias="PORTFOLIO_MAX_HOLD_HOURS", ge=1, le=720
    )
    portfolio_discovery_interval_mins: int = Field(
        default=20, alias="PORTFOLIO_DISCOVERY_INTERVAL_MINS", ge=5, le=1440
    )
    portfolio_price_check_seconds: int = Field(
        default=40, alias="PORTFOLIO_PRICE_CHECK_SECONDS", ge=10, le=3600
    )
    portfolio_daily_loss_limit_usd: float = Field(
        default=50.0, alias="PORTFOLIO_DAILY_LOSS_LIMIT_USD", ge=0
    )
    portfolio_min_volume_usd: float = Field(
        default=380000.0, alias="PORTFOLIO_MIN_VOLUME_USD", ge=0
    )
    portfolio_min_liquidity_usd: float = Field(
        default=245000.0, alias="PORTFOLIO_MIN_LIQUIDITY_USD", ge=0
    )
    portfolio_min_market_cap_usd: float = Field(
        default=1650000.0, alias="PORTFOLIO_MIN_MARKET_CAP_USD", ge=0
    )
    portfolio_min_token_age_hours: float = Field(
        default=11.0, alias="PORTFOLIO_MIN_TOKEN_AGE_HOURS", ge=0
    )
    portfolio_max_token_age_hours: float = Field(
        default=0.0, alias="PORTFOLIO_MAX_TOKEN_AGE_HOURS", ge=0
    )
    portfolio_cooldown_seconds: int = Field(
        default=300, alias="PORTFOLIO_COOLDOWN_SECONDS", ge=0, le=86400
    )
    portfolio_min_momentum_score: float = Field(
        default=54.0, alias="PORTFOLIO_MIN_MOMENTUM_SCORE", ge=0, le=100
    )
    portfolio_max_slippage_bps: int = Field(
        default=300, alias="PORTFOLIO_MAX_SLIPPAGE_BPS", ge=1, le=5000
    )
    portfolio_quote_mint: str = Field(
        default="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        alias="PORTFOLIO_QUOTE_MINT",
    )
    portfolio_quote_method: str = Field(
        default="", alias="PORTFOLIO_QUOTE_METHOD"
    )
    portfolio_execute_method: str = Field(
        default="", alias="PORTFOLIO_EXECUTE_METHOD"
    )
    portfolio_slippage_probe_enabled: bool = Field(
        default=False, alias="PORTFOLIO_SLIPPAGE_PROBE_ENABLED"
    )
    portfolio_slippage_probe_usd: float = Field(
        default=0.50, alias="PORTFOLIO_SLIPPAGE_PROBE_USD", ge=0.10
    )
    portfolio_slippage_probe_max_slippage_pct: float = Field(
        default=5.0, alias="PORTFOLIO_SLIPPAGE_PROBE_MAX_SLIPPAGE_PCT", ge=0.1, le=100.0
    )
    # SOL trend gate: skip discovery when SOL drops too fast
    portfolio_sol_dump_threshold_pct: float = Field(
        default=-5.0, alias="PORTFOLIO_SOL_DUMP_THRESHOLD_PCT", le=0.0
    )
    portfolio_sol_trend_lookback_mins: int = Field(
        default=60, alias="PORTFOLIO_SOL_TREND_LOOKBACK_MINS", ge=5, le=1440
    )
    # Insider / sniper detection: analyse top holders before buying
    portfolio_insider_check_enabled: bool = Field(
        default=True, alias="PORTFOLIO_INSIDER_CHECK_ENABLED"
    )
    portfolio_insider_max_concentration_pct: float = Field(
        default=50.0, alias="PORTFOLIO_INSIDER_MAX_CONCENTRATION_PCT", ge=0, le=100
    )
    portfolio_insider_max_creator_pct: float = Field(
        default=30.0, alias="PORTFOLIO_INSIDER_MAX_CREATOR_PCT", ge=0, le=100
    )
    portfolio_insider_warn_concentration_pct: float = Field(
        default=30.0, alias="PORTFOLIO_INSIDER_WARN_CONCENTRATION_PCT", ge=0, le=100
    )
    portfolio_insider_warn_creator_pct: float = Field(
        default=10.0, alias="PORTFOLIO_INSIDER_WARN_CREATOR_PCT", ge=0, le=100
    )
    # Shadow audit: record approved candidates without trading for outcome comparison
    portfolio_shadow_audit_enabled: bool = Field(
        default=False, alias="PORTFOLIO_SHADOW_AUDIT_ENABLED"
    )
    portfolio_shadow_check_minutes: int = Field(
        default=30, alias="PORTFOLIO_SHADOW_CHECK_MINUTES", ge=5, le=1440
    )
    # Decision logging: persist per-candidate reason codes for pipeline observability
    portfolio_decision_log_enabled: bool = Field(
        default=False, alias="PORTFOLIO_DECISION_LOG_ENABLED"
    )

    @model_validator(mode="after")
    def _validate_insider_thresholds(self) -> "Settings":
        """Ensure insider WARN thresholds remain below MAX thresholds."""
        if (
            self.portfolio_insider_warn_concentration_pct
            >= self.portfolio_insider_max_concentration_pct
        ):
            raise ValueError(
                "PORTFOLIO_INSIDER_WARN_CONCENTRATION_PCT must be lower than "
                "PORTFOLIO_INSIDER_MAX_CONCENTRATION_PCT"
            )
        if self.portfolio_insider_warn_creator_pct >= self.portfolio_insider_max_creator_pct:
            raise ValueError(
                "PORTFOLIO_INSIDER_WARN_CREATOR_PCT must be lower than "
                "PORTFOLIO_INSIDER_MAX_CREATOR_PCT"
            )
        return self


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Return cached Settings instance, raising a helpful message on failure."""
    try:
        return Settings()
    except ValidationError as exc:
        raise RuntimeError(f"Invalid configuration: {exc}") from exc


__all__ = ["Settings", "load_settings"]
