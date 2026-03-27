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


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Return cached Settings instance, raising a helpful message on failure."""
    try:
        return Settings()
    except ValidationError as exc:
        raise RuntimeError(f"Invalid configuration: {exc}") from exc


__all__ = ["Settings", "load_settings"]
