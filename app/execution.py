"""Trader quote/execution helpers for DEX trading strategies."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

# Native SOL mint address used by Jupiter / DexScreener
SOL_NATIVE_MINT = "So11111111111111111111111111111111111111112"

# In-memory cache: mint address → decimals (immutable on-chain, safe to cache forever)
_decimals_cache: Dict[str, int] = {
    SOL_NATIVE_MINT: 9,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
}

_SPL_DEFAULT_DECIMALS = 9

# Maximum sleep between RPC retries (seconds)
_RPC_MAX_RETRY_DELAY = 30.0


def _rpc_retry_delay(
    resp: Optional["httpx.Response"],
    attempt: int,
    base_delay: float,
) -> float:
    """Compute the delay before the next RPC retry attempt.

    For 429 responses: honour the ``Retry-After`` header when present;
    fall back to exponential backoff otherwise.
    For all other cases: exponential backoff (``base_delay * 2^attempt``).
    """
    if resp is not None and resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "")
        try:
            return min(float(retry_after), _RPC_MAX_RETRY_DELAY)
        except (ValueError, TypeError):
            pass
    return min(base_delay * (2 ** attempt), _RPC_MAX_RETRY_DELAY)


async def get_token_decimals(
    mint_address: str,
    rpc_url: str,
    retries: int = 2,
    retry_delay_seconds: float = 5.0,
) -> int:
    """Fetch SPL token decimals from Solana RPC with in-memory caching.

    Decimals are immutable after mint creation, so results are cached forever.
    Falls back to 9 (the SPL default) on RPC/response failures.
    Raises ``ValueError`` when an on-chain lookup is required (cache miss)
    and ``rpc_url`` is missing or whitespace-only.
    """
    if mint_address in _decimals_cache:
        return _decimals_cache[mint_address]

    if not rpc_url or not rpc_url.strip():
        raise ValueError("rpc_url is required for on-chain decimal lookups")
    rpc_url = rpc_url.strip()

    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(retries + 1):
            resp = None
            try:
                resp = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getAccountInfo",
                        "params": [mint_address, {"encoding": "jsonParsed"}],
                    },
                )
                if resp.status_code == 429:
                    if attempt < retries:
                        delay = _rpc_retry_delay(resp, attempt, retry_delay_seconds)
                        logger.debug(
                            "RPC rate-limited (429) fetching decimals for %s, retrying in %.1fs (attempt %d/%d)",
                            mint_address, delay, attempt + 1, retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    break
                resp.raise_for_status()
                data = resp.json()
                decimals = (
                    data.get("result", {})
                    .get("value", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                    .get("decimals")
                )
                if isinstance(decimals, int):
                    _decimals_cache[mint_address] = decimals
                    return decimals
                break  # valid response but no decimals; use default
            except Exception:
                if attempt < retries:
                    delay = _rpc_retry_delay(resp, attempt, retry_delay_seconds)
                    await asyncio.sleep(delay)
                    continue
                break

    logger.warning(
        "Failed to fetch decimals for %s; defaulting to %d",
        mint_address, _SPL_DEFAULT_DECIMALS,
    )
    _decimals_cache[mint_address] = _SPL_DEFAULT_DECIMALS
    return _SPL_DEFAULT_DECIMALS


async def verify_transaction_success(
    tx_hash: str,
    rpc_url: str,
    retries: int = 3,
    retry_delay_seconds: float = 5.0,
) -> Optional[bool]:
    """Check whether a Solana transaction succeeded on-chain.

    Returns ``True`` if the transaction confirmed without error,
    ``False`` if the transaction failed (e.g. slippage exceeded),
    or ``None`` if the status could not be determined (RPC error,
    transaction not yet found, etc.).
    Raises ``ValueError`` when ``rpc_url`` is missing or whitespace-only.
    """
    if not rpc_url or not rpc_url.strip():
        raise ValueError("rpc_url is required for transaction verification")
    rpc_url = rpc_url.strip()

    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(retries + 1):
            resp = None
            try:
                resp = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            tx_hash,
                            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                        ],
                    },
                )
                if resp.status_code == 429:
                    if attempt < retries:
                        delay = _rpc_retry_delay(resp, attempt, retry_delay_seconds)
                        logger.debug(
                            "RPC rate-limited (429) verifying tx %s, retrying in %.1fs (attempt %d/%d)",
                            tx_hash, delay, attempt + 1, retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning(
                        "Could not verify tx %s on-chain (rate-limited after %d attempts); proceeding as unknown",
                        tx_hash, retries + 1,
                    )
                    return None
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result")
                if result is None:
                    return None
                meta = result.get("meta", {})
                return meta.get("err") is None
            except Exception as exc:
                if attempt < retries:
                    delay = _rpc_retry_delay(resp, attempt, retry_delay_seconds)
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "Could not verify tx %s on-chain (RPC error: %s); proceeding as unknown",
                    tx_hash,
                    exc,
                )
                return None


@dataclass
class TraderMethodSet:
    """Resolved trader tool names for quote + execution."""

    quote_method: str
    execute_method: str
    buy_method: str = ""
    sell_method: str = ""

    def execute_for_side(self, side: str) -> str:
        """Return the best execute method for the given trade side."""
        if side == "buy" and self.buy_method:
            return self.buy_method
        if side == "sell" and self.sell_method:
            return self.sell_method
        return self.execute_method


@dataclass
class TradeQuote:
    """Normalized executable quote."""

    price: float
    method: str
    raw: Any
    liquidity_usd: Optional[float] = None
    price_impact_pct: Optional[float] = None


@dataclass
class TradeExecution:
    """Normalized execution response."""

    success: bool
    method: Optional[str]
    raw: Any
    tx_hash: Optional[str] = None
    executed_price: Optional[float] = None
    quantity_token: Optional[float] = None
    error: Optional[str] = None


@dataclass
class AtomicTradeExecution:
    """Result from an atomic buy-and-sell round trip."""

    success: bool
    partial: bool = False  # buy succeeded, sell failed
    buy_tx_hash: Optional[str] = None
    sell_tx_hash: Optional[str] = None
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    quantity_token: Optional[float] = None
    sol_spent: Optional[float] = None
    sol_received: Optional[float] = None
    profit_sol: Optional[float] = None
    profit_usd: Optional[float] = None
    error: Optional[str] = None
    raw: Any = None


class TraderExecutionService:
    """Handles trader tool discovery, quote retrieval, and trade execution."""

    def __init__(
        self,
        mcp_manager: Any,
        chain: str,
        max_slippage_bps: int,
        quote_method_override: str = "",
        execute_method_override: str = "",
        quote_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        rpc_url: str = "",
    ) -> None:
        self.mcp_manager = mcp_manager
        self.chain = chain.strip().lower()
        if self.chain != "solana":
            raise ValueError("TraderExecutionService currently supports only solana chain")
        if not rpc_url or not rpc_url.strip():
            raise ValueError("rpc_url is required for solana trade execution")
        self.max_slippage_bps = max_slippage_bps
        self.quote_method_override = quote_method_override.strip()
        self.execute_method_override = execute_method_override.strip()
        self.quote_mint = quote_mint
        self.rpc_url = rpc_url.strip()
        self._method_cache: Optional[TraderMethodSet] = None

    def _get_trader_client(self) -> Any:
        trader = self.mcp_manager.get_client("trader")
        if trader is None:
            raise RuntimeError("Trader MCP client is not configured")
        return trader

    def _resolve_methods(self) -> TraderMethodSet:
        if self._method_cache is not None:
            return self._method_cache

        trader = self._get_trader_client()
        tool_names = [tool.get("name", "") for tool in (trader.tools or []) if tool.get("name")]
        if not tool_names:
            raise RuntimeError("Trader MCP client has no tools")

        quote_method = self.quote_method_override or self._pick_method(
            tool_names,
            exact_candidates=(
                "get_quote",
                "quote",
                "getQuote",
                "quote_swap",
                "swap_quote",
                "jupiter_quote",
            ),
            contains_candidates=("quote",),
        )
        execute_method = self.execute_method_override or self._pick_method(
            tool_names,
            exact_candidates=(
                "swap",
                "execute_swap",
                "trade",
                "execute_trade",
                "place_order",
            ),
            contains_candidates=("swap", "trade", "order"),
        )

        # Side-specific execute methods for traders that expose buy/sell separately
        buy_method = self._pick_method(
            tool_names,
            exact_candidates=("buy_token", "buy", "buyToken"),
            contains_candidates=("buy",),
        )
        sell_method = self._pick_method(
            tool_names,
            exact_candidates=("sell_token", "sell", "sellToken"),
            contains_candidates=("sell",),
        )

        if not quote_method:
            raise RuntimeError(f"Unable to resolve trader quote method from tools: {tool_names}")
        if not execute_method and not (buy_method and sell_method):
            raise RuntimeError(f"Unable to resolve trader execute method from tools: {tool_names}")

        self._method_cache = TraderMethodSet(
            quote_method=quote_method,
            execute_method=execute_method,
            buy_method=buy_method,
            sell_method=sell_method,
        )
        return self._method_cache

    @staticmethod
    def _pick_method(
        tool_names: Sequence[str],
        exact_candidates: Sequence[str],
        contains_candidates: Sequence[str],
    ) -> str:
        exact_lookup = {name.lower(): name for name in tool_names}
        for candidate in exact_candidates:
            hit = exact_lookup.get(candidate.lower())
            if hit:
                return hit

        for name in tool_names:
            lower_name = name.lower()
            if any(token in lower_name for token in contains_candidates):
                return name

        return ""

    def _get_tool_schema(self, method_name: str) -> Dict[str, Any]:
        trader = self._get_trader_client()
        for tool in trader.tools or []:
            if tool.get("name") == method_name:
                return tool
        return {}

    async def get_quote(
        self,
        token_address: str,
        notional_usd: float,
        side: str = "buy",
        input_price_usd: Optional[float] = None,
        token_decimals: Optional[int] = None,
        quantity_token: Optional[float] = None,
    ) -> TradeQuote:
        """Fetch executable quote from trader MCP."""
        if token_decimals is None:
            token_decimals = await get_token_decimals(token_address, self.rpc_url)
        trader = self._get_trader_client()
        method = self._resolve_methods().quote_method
        tool_schema = self._get_tool_schema(method)
        args = self._build_tool_args(
            tool_schema=tool_schema,
            token_address=token_address,
            notional_usd=notional_usd,
            side=side,
            quantity_token=quantity_token,
            quote_payload=None,
            input_price_usd=input_price_usd,
            token_decimals=token_decimals,
        )
        result = await trader.call_tool(method, args)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                pass
        price = self._extract_price(
            result, side=side, native_price_usd=input_price_usd,
            token_decimals=token_decimals,
        )
        if price is None or price <= 0:
            logger.warning("Trader quote response has no valid price: %s", result)
            raise RuntimeError(f"Trader quote did not include a valid price (method: {method})")

        liquidity = self._extract_first_float(
            result,
            ("liquidityUsd", "liquidity_usd", "liquidity", "liquidityUSD"),
        )
        price_impact = self._extract_first_float(
            result,
            ("priceImpact", "price_impact", "priceImpactPct", "price_impact_pct"),
        )
        # Jupiter returns priceImpact as a string percentage like "0.12" — try parsing
        if price_impact is None and isinstance(result, dict):
            for k in ("priceImpact", "price_impact", "priceImpactPct", "price_impact_pct"):
                v = result.get(k)
                if isinstance(v, str):
                    try:
                        v_stripped = v.rstrip("%")
                        price_impact = float(v_stripped)
                        break
                    except (ValueError, TypeError):
                        pass
        return TradeQuote(
            price=price, method=method, raw=result,
            liquidity_usd=liquidity, price_impact_pct=price_impact,
        )

    async def get_wallet_token_balance(self, token_address: str) -> Optional[float]:
        """Query actual token balance from the trader wallet via get_balance.

        Returns the human-readable ``uiAmount`` if available, or ``None``
        when the MCP doesn't support ``get_balance`` or the call fails.
        """
        trader = self.mcp_manager.get_client("trader")
        if trader is None:
            return None
        tool_names = [t.get("name", "") for t in (trader.tools or []) if t.get("name")]
        if "get_balance" not in tool_names:
            return None
        try:
            result = await trader.call_tool("get_balance", {"token_address": token_address})
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(result, dict):
                tb = result.get("tokenBalance")
                if isinstance(tb, dict):
                    ui = tb.get("uiAmount")
                    if ui is not None:
                        return float(ui)
        except Exception as exc:
            logger.debug("get_balance failed for %s: %s", token_address, exc)
        return None

    async def execute_trade(
        self,
        token_address: str,
        notional_usd: float,
        side: str,
        quantity_token: Optional[float],
        dry_run: bool,
        quote: Optional[TradeQuote],
        input_price_usd: Optional[float] = None,
        token_decimals: Optional[int] = None,
    ) -> TradeExecution:
        """Execute trade through trader MCP or simulate in dry-run mode."""
        if token_decimals is None:
            token_decimals = await get_token_decimals(token_address, self.rpc_url)

        if dry_run:
            executed_price = quote.price if quote else None
            quantity = quantity_token
            if quantity is None and executed_price and executed_price > 0:
                quantity = notional_usd / executed_price
            return TradeExecution(
                success=True,
                method=None,
                raw={"dry_run": True},
                tx_hash=None,
                executed_price=executed_price,
                quantity_token=quantity,
                error=None,
            )

        trader = self._get_trader_client()
        methods = self._resolve_methods()
        method = methods.execute_for_side(side)
        tool_schema = self._get_tool_schema(method)
        args = self._build_tool_args(
            tool_schema=tool_schema,
            token_address=token_address,
            notional_usd=notional_usd,
            side=side,
            quantity_token=quantity_token,
            quote_payload=quote.raw if quote else None,
            input_price_usd=input_price_usd,
            token_decimals=token_decimals,
        )
        result = await trader.call_tool(method, args)

        success = self._extract_success(result)
        error = self._extract_error(result)
        tx_hash = self._extract_tx_hash(result)
        executed_price = self._extract_price(
            result, side=side, native_price_usd=input_price_usd,
            token_decimals=token_decimals,
        )
        executed_qty = self._extract_first_float(
            result,
            (
                "quantity",
                "quantityToken",
                "qty",
                "filledAmount",
                "tokenSold",
                "token_sold",
            ),
        )
        if executed_qty is None:
            # tokenReceived from buy_token is raw smallest units — convert
            raw_received = self._extract_first_float(
                result,
                ("tokenReceived", "token_received", "outputAmount", "outAmount", "amountOut"),
            )
            if raw_received is not None and raw_received > 0:
                executed_qty = raw_received / (10 ** token_decimals)

        if success and executed_qty is None and executed_price and executed_price > 0:
            executed_qty = notional_usd / executed_price

        # Live trades must have a tx_hash to be considered successful
        if success and tx_hash is None:
            success = False
            if error is None:
                error = "No transaction hash in trader response"

        # Verify on-chain success (catches slippage failures, etc.)
        if success and tx_hash:
            on_chain_ok = await verify_transaction_success(tx_hash, self.rpc_url)
            if on_chain_ok is False:
                success = False
                error = f"Transaction {tx_hash} failed on-chain (likely slippage)"
                logger.warning(error)

        if not success and error is None:
            error = f"Trader execute method '{method}' returned unsuccessful response"

        return TradeExecution(
            success=success,
            method=method,
            raw=result,
            tx_hash=tx_hash,
            executed_price=executed_price,
            quantity_token=executed_qty,
            error=error,
        )

    async def get_profitability_quotes(
        self,
        token_address: str,
        notional_usd: float,
        input_price_usd: Optional[float] = None,
        fee_buffer_lamports: int = 0,
        max_price_impact_pct: float = 0.0,
    ) -> tuple[float, float, float]:
        """Get buy and sell quotes and return estimated profit in bps.

        When *fee_buffer_lamports* > 0 the estimated SOL transaction fees
        (base + priority for both legs) are subtracted from the sell price
        before computing profit.

        When *max_price_impact_pct* > 0 and either quote exceeds this
        threshold, profit is forced to ``-inf`` so the caller skips.

        Returns (buy_price, sell_price, estimated_profit_bps).
        """
        buy_quote = await self.get_quote(
            token_address=token_address,
            notional_usd=notional_usd,
            side="buy",
            input_price_usd=input_price_usd,
        )
        quantity = notional_usd / buy_quote.price if buy_quote.price > 0 else 0
        sell_quote = await self.get_quote(
            token_address=token_address,
            notional_usd=notional_usd,
            side="sell",
            input_price_usd=input_price_usd,
            quantity_token=quantity,
        )

        # Log price impact if available
        for label, q in (("buy", buy_quote), ("sell", sell_quote)):
            if q.price_impact_pct is not None:
                logger.info(
                    "Quote %s price impact: %.4f%%", label, q.price_impact_pct,
                )

        # Reject if price impact exceeds threshold
        if max_price_impact_pct > 0:
            for label, q in (("buy", buy_quote), ("sell", sell_quote)):
                if q.price_impact_pct is not None and abs(q.price_impact_pct) > max_price_impact_pct:
                    logger.warning(
                        "Price impact %.4f%% on %s exceeds max %.4f%%",
                        q.price_impact_pct, label, max_price_impact_pct,
                    )
                    return buy_quote.price, sell_quote.price, float("-inf")

        if buy_quote.price <= 0:
            return buy_quote.price, sell_quote.price, 0.0

        # Subtract estimated fees from profit
        fee_usd = 0.0
        if fee_buffer_lamports > 0 and input_price_usd and input_price_usd > 0:
            fee_usd = (fee_buffer_lamports / 1_000_000_000) * input_price_usd

        gross_profit_usd = (sell_quote.price - buy_quote.price) * quantity
        net_profit_usd = gross_profit_usd - fee_usd
        profit_bps = (net_profit_usd / notional_usd) * 10_000 if notional_usd > 0 else 0.0
        return buy_quote.price, sell_quote.price, profit_bps

    async def execute_atomic_trade(
        self,
        token_address: str,
        notional_usd: float,
        dry_run: bool,
        input_price_usd: Optional[float] = None,
        buy_price: Optional[float] = None,
        sell_price: Optional[float] = None,
    ) -> AtomicTradeExecution:
        """Execute atomic buy+sell via trader MCP buy_and_sell tool."""
        if dry_run:
            profit = 0.0
            if buy_price and sell_price and buy_price > 0:
                qty = notional_usd / buy_price
                profit = (sell_price - buy_price) * qty
            return AtomicTradeExecution(
                success=True,
                entry_price=buy_price,
                exit_price=sell_price,
                quantity_token=(notional_usd / buy_price) if buy_price and buy_price > 0 else None,
                profit_usd=profit,
                raw={"dry_run": True},
            )

        trader = self._get_trader_client()
        tool_names = [t.get("name", "") for t in (trader.tools or []) if t.get("name")]
        if "buy_and_sell" not in tool_names:
            return AtomicTradeExecution(
                success=False,
                error="Trader MCP does not expose buy_and_sell tool",
            )

        sol_amount = notional_usd / input_price_usd if input_price_usd and input_price_usd > 0 else notional_usd
        result = await trader.call_tool("buy_and_sell", {
            "token_address": token_address,
            "sol_amount": float(sol_amount),
            "slippage_bps": int(self.max_slippage_bps),
        })
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                pass

        if not isinstance(result, dict):
            return AtomicTradeExecution(
                success=False, error="Unexpected response from buy_and_sell", raw=result,
            )

        status = result.get("status", "")
        native_usd = input_price_usd or 1.0
        sol_spent = self._extract_first_float(result, ("sol_spent", "solSpent"))
        sol_received = self._extract_first_float(result, ("sol_received", "solReceived"))
        token_received_raw = self._extract_first_float(result, ("token_received", "tokenReceived"))
        token_sold = self._extract_first_float(result, ("token_sold", "tokenSold"))
        profit_sol = self._extract_first_float(result, ("profit_sol", "profitSol", "net_sol", "netSol"))

        # Derive prices from SOL amounts
        token_decimals = await get_token_decimals(token_address, self.rpc_url)
        qty = None
        entry_p = None
        exit_p = None
        if token_received_raw and token_received_raw > 0:
            qty = token_received_raw / (10 ** token_decimals)
        if qty and qty > 0 and sol_spent and sol_spent > 0:
            entry_p = (sol_spent * native_usd) / qty
        if token_sold and token_sold > 0 and sol_received and sol_received > 0:
            exit_p = (sol_received * native_usd) / token_sold

        p_usd = (profit_sol * native_usd) if profit_sol is not None else None

        # Log realized slippage vs pre-trade quotes
        if buy_price and entry_p and buy_price > 0:
            entry_slippage_bps = ((entry_p - buy_price) / buy_price) * 10_000
            logger.info(
                "Atomic entry slippage: quoted=%.10f actual=%.10f slippage=%.2f bps",
                buy_price, entry_p, entry_slippage_bps,
            )
        if sell_price and exit_p and sell_price > 0:
            exit_slippage_bps = ((sell_price - exit_p) / sell_price) * 10_000
            logger.info(
                "Atomic exit slippage: quoted=%.10f actual=%.10f slippage=%.2f bps",
                sell_price, exit_p, exit_slippage_bps,
            )

        if status == "partial":
            buy_tx = self._extract_first_value(result, ("buy_transaction",))
            return AtomicTradeExecution(
                success=False,
                partial=True,
                buy_tx_hash=buy_tx if isinstance(buy_tx, str) else None,
                entry_price=entry_p,
                quantity_token=qty,
                sol_spent=sol_spent,
                error=result.get("sell_error", "Sell phase failed"),
                raw=result,
            )

        if status == "error":
            return AtomicTradeExecution(
                success=False, error=result.get("error", "Buy phase failed"), raw=result,
            )

        buy_tx = self._extract_first_value(result, ("buy_transaction",))
        sell_tx = self._extract_first_value(result, ("sell_transaction",))

        return AtomicTradeExecution(
            success=True,
            buy_tx_hash=buy_tx if isinstance(buy_tx, str) else None,
            sell_tx_hash=sell_tx if isinstance(sell_tx, str) else None,
            entry_price=entry_p,
            exit_price=exit_p,
            quantity_token=qty,
            sol_spent=sol_spent,
            sol_received=sol_received,
            profit_sol=profit_sol,
            profit_usd=p_usd,
            raw=result,
        )

    async def probe_slippage(
        self,
        token_address: str,
        probe_usd: float,
        input_price_usd: float,
        max_slippage_pct: float,
    ) -> tuple[bool, Optional[float], Optional[str]]:
        """Execute a tiny buy+sell probe to validate real vs quoted slippage.

        Returns ``(should_abort, actual_slippage_pct, reason)``.
        ``should_abort`` is always ``False`` when the probe infrastructure is
        unavailable — the caller should proceed normally in that case.
        """
        try:
            probe_quote = await self.get_quote(
                token_address=token_address,
                notional_usd=probe_usd,
                side="buy",
                input_price_usd=input_price_usd,
            )
        except Exception as exc:
            logger.warning("Slippage probe: quote failed (%s); skipping probe", exc)
            return False, None, None

        if probe_quote.price <= 0:
            logger.warning("Slippage probe: quote price <= 0; skipping probe")
            return False, None, None

        result = await self.execute_atomic_trade(
            token_address=token_address,
            notional_usd=probe_usd,
            dry_run=False,
            input_price_usd=input_price_usd,
            buy_price=probe_quote.price,
        )

        if not result.success or result.entry_price is None:
            # Probe failed (buy_and_sell unavailable or execution error) — degrade gracefully
            logger.warning(
                "Slippage probe: atomic trade failed (%s); skipping probe",
                result.error,
            )
            return False, None, result.error

        actual_slippage_pct = abs(result.entry_price - probe_quote.price) / probe_quote.price * 100
        logger.info(
            "Slippage probe: quoted=%.10f actual=%.10f deviation=%.2f%%",
            probe_quote.price, result.entry_price, actual_slippage_pct,
        )

        if actual_slippage_pct > max_slippage_pct:
            reason = (
                f"probe slippage {actual_slippage_pct:.1f}% exceeds threshold {max_slippage_pct:.1f}%"
            )
            return True, actual_slippage_pct, reason

        return False, actual_slippage_pct, None

    def _build_tool_args(
        self,
        tool_schema: Dict[str, Any],
        token_address: str,
        notional_usd: float,
        side: str,
        quantity_token: Optional[float],
        quote_payload: Optional[Any],
        input_price_usd: Optional[float] = None,
        token_decimals: int = _SPL_DEFAULT_DECIMALS,
    ) -> Dict[str, Any]:
        input_schema = tool_schema.get("inputSchema", {})
        properties = input_schema.get("properties", {})
        required = input_schema.get("required", [])

        args: Dict[str, Any] = {}

        for key in properties.keys():
            value = self._value_for_param(
                param_name=key,
                token_address=token_address,
                notional_usd=notional_usd,
                side=side,
                quantity_token=quantity_token,
                quote_payload=quote_payload,
                input_price_usd=input_price_usd,
                token_decimals=token_decimals,
            )
            if value is not None:
                args[key] = value

        for key in required:
            if key in args:
                continue
            value = self._value_for_param(
                param_name=key,
                token_address=token_address,
                notional_usd=notional_usd,
                side=side,
                quantity_token=quantity_token,
                quote_payload=quote_payload,
                input_price_usd=input_price_usd,
                token_decimals=token_decimals,
            )
            if value is None:
                raise ValueError(f"Unable to infer required trader argument: {key}")
            args[key] = value

        return args

    def _value_for_param(
        self,
        param_name: str,
        token_address: str,
        notional_usd: float,
        side: str,
        quantity_token: Optional[float],
        quote_payload: Optional[Any],
        input_price_usd: Optional[float] = None,
        token_decimals: int = _SPL_DEFAULT_DECIMALS,
    ) -> Any:
        key = param_name.lower()

        if key in {"chain", "network", "chainid"}:
            return self.chain
        if key in {"side", "action", "direction", "trade_side"}:
            return side
        if "dry" in key and "run" in key:
            return False

        if quote_payload is not None:
            if key in {"quote", "quote_response", "route", "route_plan", "swap_quote"}:
                return quote_payload

        is_tokenish = any(token in key for token in ("mint", "token", "address"))
        is_amount_like = any(token in key for token in ("amount", "size", "qty", "quantity", "decimal"))
        if is_tokenish and not is_amount_like:
            is_input = any(
                token in key
                for token in ("input", "from", "source", "sell", "inmint", "tokenin", "in_token")
            )
            is_output = any(
                token in key
                for token in ("output", "destination", "buy", "outmint", "tokenout", "out_token", "to_mint", "tomint", "to_token", "totoken")
            )
            if is_input:
                # buy_token always uses SOL as input; match that for quotes
                return SOL_NATIVE_MINT if side == "buy" else token_address
            if is_output:
                return token_address if side == "buy" else SOL_NATIVE_MINT
            return token_address

        if "slippage" in key:
            if "bps" in key:
                return int(self.max_slippage_bps)
            return round(self.max_slippage_bps / 100, 4)

        if any(token in key for token in ("notional", "usd")):
            return float(notional_usd)

        if "lamport" in key:
            if input_price_usd and input_price_usd > 0:
                return int((notional_usd / input_price_usd) * 1_000_000_000)
            logger.warning("No input_price_usd for lamport conversion; falling back to raw notional")
            return int(notional_usd * 1_000_000_000)

        if "amount" in key or "size" in key or "qty" in key or "quantity" in key:
            if quantity_token is not None and side == "sell":
                return float(quantity_token)
            if input_price_usd and input_price_usd > 0:
                return float(notional_usd / input_price_usd)
            return float(notional_usd)

        if "decimal" in key:
            is_input_dec = "input" in key or "in_" in key
            if is_input_dec:
                # input_decimals: buy → native (SOL=9), sell → token
                return 9 if side == "buy" else token_decimals
            return token_decimals

        if "symbol" in key:
            return "USDC" if side == "buy" else "TOKEN"

        return None

    @classmethod
    def _extract_success(cls, payload: Any) -> bool:
        if isinstance(payload, str):
            # MCP error responses are plain strings starting with "Error:"
            if payload.strip().lower().startswith("error"):
                return False
            return True
        if isinstance(payload, dict):
            if "success" in payload:
                return bool(payload["success"])
            if "ok" in payload:
                return bool(payload["ok"])
            status = payload.get("status")
            if isinstance(status, str):
                status_l = status.lower()
                if status_l in {"success", "succeeded", "confirmed", "completed"}:
                    return True
                if status_l in {"failed", "error", "rejected"}:
                    return False
            err = payload.get("error")
            if err:
                return False
        return True

    @classmethod
    def _extract_error(cls, payload: Any) -> Optional[str]:
        if isinstance(payload, str):
            if payload.strip().lower().startswith("error"):
                return payload.strip()
            return None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, str):
                return err
            if isinstance(err, dict):
                message = err.get("message")
                if isinstance(message, str):
                    return message
        return None

    @classmethod
    def _extract_tx_hash(cls, payload: Any) -> Optional[str]:
        value = cls._extract_first_value(
            payload,
            ("txHash", "tx_hash", "signature", "transactionHash", "transaction", "txid", "hash"),
        )
        return value if isinstance(value, str) and value else None

    @classmethod
    def _extract_price(
        cls,
        payload: Any,
        side: str,
        native_price_usd: Optional[float] = None,
        native_decimals: int = 9,
        token_decimals: int = 9,
    ) -> Optional[float]:
        """Extract a USD-denominated token price from a trader response.

        When the response only contains raw in/out amounts (lamports / smallest
        token units), ``native_price_usd`` is used to convert to USD.
        """
        # 1. Direct USD price field
        direct_price = cls._extract_first_float(
            payload,
            (
                "price",
                "priceUsd",
                "price_usd",
                "executionPrice",
                "executedPrice",
                "fillPrice",
                "estimatedPrice",
                "estimated_price",
                "expectedPrice",
                "expected_price",
                "quotePrice",
                "quote_price",
                "swapPrice",
                "swap_price",
            ),
        )
        if direct_price and direct_price > 0:
            return direct_price

        # 2. Derive from SOL spent/received (human-readable) fields
        sol_spent = cls._extract_first_float(payload, ("solSpent", "sol_spent"))
        token_received = cls._extract_first_float(
            payload, ("tokenReceived", "token_received"),
        )
        sol_received = cls._extract_first_float(
            payload, ("solReceived", "sol_received"),
        )
        token_sold = cls._extract_first_float(payload, ("tokenSold", "token_sold"))

        if native_price_usd and native_price_usd > 0:
            if side == "buy" and sol_spent and token_received:
                # token_received from buy_token is raw (smallest units)
                token_human = token_received / (10 ** token_decimals)
                if token_human > 0:
                    return (sol_spent * native_price_usd) / token_human
            if side == "sell" and sol_received and token_sold:
                # token_sold is human-readable, sol_received is human-readable
                if token_sold > 0:
                    return (sol_received * native_price_usd) / token_sold

        # 3. Derive from raw in/out amounts
        in_amount = cls._extract_first_float(
            payload,
            (
                "inAmount",
                "inputAmount",
                "amountIn",
                "fromAmount",
                "input_amount",
                "amount_in",
            ),
        )
        out_amount = cls._extract_first_float(
            payload,
            (
                "outAmount",
                "outputAmount",
                "amountOut",
                "toAmount",
                "output_amount",
                "amount_out",
            ),
        )
        if not in_amount or not out_amount:
            return None
        if in_amount <= 0 or out_amount <= 0:
            return None

        if native_price_usd and native_price_usd > 0:
            # Convert raw amounts to human-readable, then to USD per token
            if side == "buy":
                # in = native (SOL lamports), out = token smallest units
                native_human = in_amount / (10 ** native_decimals)
                token_human = out_amount / (10 ** token_decimals)
                if token_human > 0:
                    return (native_human * native_price_usd) / token_human
            else:
                # in = token smallest units, out = native (SOL lamports)
                token_human = in_amount / (10 ** token_decimals)
                native_human = out_amount / (10 ** native_decimals)
                if token_human > 0:
                    return (native_human * native_price_usd) / token_human

        # Fallback: raw ratio (no USD context available)
        if side == "buy":
            return in_amount / out_amount
        return out_amount / in_amount

    @classmethod
    def _extract_first_float(
        cls,
        payload: Any,
        keys: Sequence[str],
    ) -> Optional[float]:
        value = cls._extract_first_value(payload, keys)
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    @classmethod
    def _extract_first_value(
        cls,
        payload: Any,
        keys: Sequence[str],
    ) -> Optional[Any]:
        key_lookup = {key.lower() for key in keys}
        for found_key, found_value in cls._walk_items(payload):
            if found_key.lower() in key_lookup:
                return found_value
        return None

    @classmethod
    def _walk_items(cls, payload: Any) -> Iterable[tuple[str, Any]]:
        if isinstance(payload, dict):
            for key, value in payload.items():
                yield str(key), value
                yield from cls._walk_items(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from cls._walk_items(item)
