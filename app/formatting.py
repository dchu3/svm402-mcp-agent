"""Shared formatting helpers for prices and large numbers."""

from __future__ import annotations

from typing import Optional


def format_price(price: Optional[float]) -> str:
    """Format price with appropriate precision.

    Returns ``"N/A"`` for *None*, otherwise scales decimal places based
    on magnitude so very small token prices remain readable.
    """
    if price is None:
        return "N/A"
    if price >= 1:
        return f"${price:,.4f}"
    elif price >= 0.0001:
        return f"${price:.6f}"
    else:
        return f"${price:.10f}"


def format_large_number(value: Optional[float]) -> str:
    """Format large numbers with K/M/B suffix."""
    if value is None:
        return "N/A"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    elif value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value / 1_000:.2f}K"
    else:
        return f"${value:,.0f}"


# Aliases that match the old _format_market_cap / _format_liquidity signatures
# (both are identical to format_large_number but accepted non-Optional floats).
format_market_cap = format_large_number
format_liquidity = format_large_number
