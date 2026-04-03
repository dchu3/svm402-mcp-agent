"""Wash trading and wallet manipulation detection for token analysis."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.mcp_client import MCPManager

logger = logging.getLogger(__name__)

LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]

# Maximum number of recent signatures to fetch from the pool
MAX_SIGNATURES = 100
# Maximum number of transactions to parse in detail
MAX_TX_SAMPLE = 30
# Wallet must have >1 buy to count as a repeat buyer
REPEAT_BUY_THRESHOLD = 2
# Minimum successfully-parsed swaps required to produce a score
MIN_SAMPLE_SIZE = 5
# Limit concurrent RPC calls to avoid cascade MCP client failures
RPC_CONCURRENCY_LIMIT = 3
# SPL Token program IDs
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = {SPL_TOKEN_PROGRAM, SPL_TOKEN_2022_PROGRAM}


@dataclass
class ParsedSwap:
    """A parsed DEX swap extracted from a Solana transaction."""

    signature: str
    wallet: str  # fee payer / initiating wallet
    direction: str  # "buy" or "sell"
    token_amount: Optional[float] = None
    block_time: Optional[int] = None


@dataclass
class WalletActivity:
    """Aggregated activity for a single wallet."""

    wallet: str
    buy_count: int = 0
    sell_count: int = 0
    total_bought: float = 0.0
    first_seen: Optional[int] = None
    last_seen: Optional[int] = None


@dataclass
class WashTradingResult:
    """Result of wash trading / manipulation analysis."""

    manipulation_score: Optional[float] = None  # 0-10 scale, None = analysis unavailable
    manipulation_level: str = "unknown"  # clean, moderate, suspicious, critical
    unique_wallets: int = 0
    total_transactions_sampled: int = 0
    repeat_buyers: List[Dict[str, Any]] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "manipulation_score": round(self.manipulation_score, 1) if self.manipulation_score is not None else None,
            "manipulation_level": self.manipulation_level,
            "unique_wallets": self.unique_wallets,
            "total_transactions_sampled": self.total_transactions_sampled,
            "repeat_buyers": self.repeat_buyers[:10],
            "flags": self.flags[:10],
        }


class WashTradingDetector:
    """Detects wash trading and manipulation patterns in token transactions."""

    def __init__(
        self,
        mcp_manager: "MCPManager",
        verbose: bool = False,
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.verbose = verbose
        self.log_callback = log_callback

    def _log(
        self, level: str, message: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        if self.verbose and self.log_callback:
            self.log_callback(level, message, data)

    async def analyze(
        self,
        token_address: str,
        pool_address: str,
    ) -> WashTradingResult:
        """Analyze recent pool transactions for wash trading patterns.

        Args:
            token_address: The SPL token mint address.
            pool_address: The DEX pool/pair address to scan.

        Returns:
            WashTradingResult with manipulation score and flags.
        """
        client = self.mcp_manager.get_client("solana")
        if not client:
            self._log("info", "Solana RPC client not available for wash trading analysis")
            return WashTradingResult(
                manipulation_level="unknown",
                flags=["Solana RPC client not available"],
            )

        # Fetch recent transaction signatures for the pool
        signatures = await self._fetch_pool_signatures(pool_address)
        if not signatures:
            self._log("info", f"No signatures found for pool {pool_address}")
            return WashTradingResult(
                manipulation_level="unknown",
                flags=[f"No transaction signatures found for pool {pool_address[:16]}..."],
            )

        # Parse a sample of transactions
        sampled = signatures[:MAX_TX_SAMPLE]
        swaps, fetch_errors = await self._fetch_and_parse_transactions(
            sampled, token_address
        )

        if not swaps:
            self._log("info", "No swaps parsed from pool transactions")
            flags = [
                f"0/{len(sampled)} sampled transactions contained parseable swaps for this token"
            ]
            if fetch_errors > 0:
                flags.append(
                    f"{fetch_errors}/{len(sampled)} transaction fetches failed with errors"
                )
            return WashTradingResult(
                total_transactions_sampled=len(sampled),
                manipulation_level="unknown",
                flags=flags,
            )

        return self._detect_patterns(swaps, len(sampled))

    async def _fetch_pool_signatures(
        self, pool_address: str
    ) -> List[Dict[str, Any]]:
        """Fetch recent transaction signatures for a pool address."""
        client = self.mcp_manager.get_client("solana")
        if not client:
            return []

        try:
            self._log(
                "tool",
                f"→ solana_getSignaturesForAddress({pool_address}, limit={MAX_SIGNATURES})",
            )
            result = await client.call_tool(
                "getSignaturesForAddress",
                {"address": pool_address, "limit": MAX_SIGNATURES},
            )
            self._log("tool", "✓ solana_getSignaturesForAddress")

            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, ValueError):
                    self._log("warning", f"Failed to parse signatures response")
                    return []

            if isinstance(result, list):
                return result
            return []

        except Exception as e:
            self._log("error", f"Failed to fetch pool signatures: {e}")
            return []

    async def _fetch_and_parse_transactions(
        self,
        signatures: List[Dict[str, Any]],
        token_address: str,
    ) -> tuple[List[ParsedSwap], int]:
        """Fetch and parse a batch of transactions in parallel.

        Returns:
            A tuple of (parsed_swaps, fetch_error_count).
        """
        client = self.mcp_manager.get_client("solana")
        if not client:
            return [], 0

        sig_strings = []
        for sig_entry in signatures:
            if isinstance(sig_entry, dict):
                sig = sig_entry.get("signature")
                if sig and isinstance(sig, str):
                    sig_strings.append(sig)
            elif isinstance(sig_entry, str):
                sig_strings.append(sig_entry)

        if not sig_strings:
            return [], 0

        fetch_errors = 0

        async def fetch_one(sig: str) -> Optional[ParsedSwap]:
            nonlocal fetch_errors
            async with semaphore:
                try:
                    result = await client.call_tool(
                        "getTransaction",
                        {"signature": sig, "maxSupportedTransactionVersion": 0},
                    )
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except (json.JSONDecodeError, ValueError):
                            return None
                    if isinstance(result, dict):
                        return self._parse_transaction(result, token_address, sig)
                except Exception as e:
                    fetch_errors += 1
                    self._log("error", f"Failed to fetch tx {sig[:16]}...: {e}")
                return None

        semaphore = asyncio.Semaphore(RPC_CONCURRENCY_LIMIT)

        self._log(
            "tool",
            f"→ Fetching {len(sig_strings)} transactions for wash trading analysis",
        )
        results = await asyncio.gather(
            *[fetch_one(sig) for sig in sig_strings],
            return_exceptions=True,
        )
        self._log("tool", "✓ Transaction fetch complete")

        swaps = []
        for r in results:
            if isinstance(r, ParsedSwap):
                swaps.append(r)
            elif isinstance(r, BaseException):
                fetch_errors += 1

        self._log(
            "info",
            f"Wash trading parse result: {len(swaps)}/{len(sig_strings)} swaps parsed"
            + (f" ({fetch_errors} fetch errors)" if fetch_errors else ""),
        )

        return swaps, fetch_errors

    def _parse_transaction(
        self,
        tx_data: Dict[str, Any],
        token_mint: str,
        signature: str,
    ) -> Optional[ParsedSwap]:
        """Parse a transaction to extract swap information.

        Looks for SPL Token transfer/transferChecked instructions involving
        the target token mint and determines buy/sell direction relative to
        the fee payer (initiating wallet).
        """
        if not isinstance(tx_data, dict):
            return None

        # Unwrap JSON-RPC envelope if the MCP server returned the raw RPC
        # response (e.g. {"jsonrpc": "2.0", "result": {…tx…}}).
        if "result" in tx_data and isinstance(tx_data.get("result"), dict) and "meta" not in tx_data:
            tx_data = tx_data["result"]

        # Skip failed transactions
        meta = tx_data.get("meta")
        if not isinstance(meta, dict):
            return None
        if meta.get("err") is not None:
            return None

        transaction = tx_data.get("transaction")
        if not isinstance(transaction, dict):
            return None

        message = transaction.get("message")
        if not isinstance(message, dict):
            return None

        # Get the fee payer (first account key / first signer)
        fee_payer = self._extract_fee_payer(message)
        if not fee_payer:
            return None

        block_time = tx_data.get("blockTime")

        # Filter out non-swap transactions (e.g. LP adds/removes)
        if not self._is_likely_swap(meta, fee_payer):
            return None

        # Scan all token transfers (inner instructions + top-level)
        token_in = 0.0  # tokens flowing TO fee payer
        token_out = 0.0  # tokens flowing FROM fee payer

        # Build a set of token accounts owned by the fee payer
        pre_token_balances = meta.get("preTokenBalances", [])
        post_token_balances = meta.get("postTokenBalances", [])

        payer_token_accounts = set()
        all_token_balances = (pre_token_balances or []) + (post_token_balances or [])
        for bal in all_token_balances:
            if not isinstance(bal, dict):
                continue
            if bal.get("mint") != token_mint:
                continue
            owner = bal.get("owner")
            if owner == fee_payer:
                account_index = bal.get("accountIndex")
                if account_index is not None:
                    payer_token_accounts.add(account_index)

        # If the fee payer has no token accounts for this mint in this tx,
        # they might not be interacting with this token directly
        if not payer_token_accounts:
            # Fallback: check pre/post balance changes
            return self._parse_from_balance_changes(
                meta, token_mint, fee_payer, signature, block_time
            )

        # Compute net change from pre/post balances for the fee payer's token accounts
        pre_amounts: Dict[int, float] = {}
        post_amounts: Dict[int, float] = {}

        for bal in (pre_token_balances or []):
            if not isinstance(bal, dict):
                continue
            if bal.get("mint") != token_mint:
                continue
            idx = bal.get("accountIndex")
            if idx in payer_token_accounts:
                ui_amount = self._extract_ui_amount(bal)
                if ui_amount is not None:
                    pre_amounts[idx] = ui_amount

        for bal in (post_token_balances or []):
            if not isinstance(bal, dict):
                continue
            if bal.get("mint") != token_mint:
                continue
            idx = bal.get("accountIndex")
            if idx in payer_token_accounts:
                ui_amount = self._extract_ui_amount(bal)
                if ui_amount is not None:
                    post_amounts[idx] = ui_amount

        net_change = 0.0
        all_indices = payer_token_accounts
        for idx in all_indices:
            pre = pre_amounts.get(idx, 0.0)
            post = post_amounts.get(idx, 0.0)
            net_change += post - pre

        if abs(net_change) < 1e-9:
            return None

        direction = "buy" if net_change > 0 else "sell"
        return ParsedSwap(
            signature=signature,
            wallet=fee_payer,
            direction=direction,
            token_amount=abs(net_change),
            block_time=block_time,
        )

    def _parse_from_balance_changes(
        self,
        meta: Dict[str, Any],
        token_mint: str,
        fee_payer: str,
        signature: str,
        block_time: Optional[int],
    ) -> Optional[ParsedSwap]:
        """Fallback: detect buy/sell from pre/post token balance changes."""
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])

        # Build owner→amount maps for the target mint
        pre_map: Dict[str, float] = {}
        post_map: Dict[str, float] = {}

        for bal in (pre_balances or []):
            if not isinstance(bal, dict) or bal.get("mint") != token_mint:
                continue
            owner = bal.get("owner", "")
            amount = self._extract_ui_amount(bal)
            if owner and amount is not None:
                pre_map[owner] = pre_map.get(owner, 0.0) + amount

        for bal in (post_balances or []):
            if not isinstance(bal, dict) or bal.get("mint") != token_mint:
                continue
            owner = bal.get("owner", "")
            amount = self._extract_ui_amount(bal)
            if owner and amount is not None:
                post_map[owner] = post_map.get(owner, 0.0) + amount

        # Check if the fee payer gained or lost tokens
        pre_amount = pre_map.get(fee_payer, 0.0)
        post_amount = post_map.get(fee_payer, 0.0)
        net = post_amount - pre_amount

        if abs(net) < 1e-9:
            # Fee payer has no direct token balance change — try to identify the
            # actual trader from ALL balance changes.  Some DEX programs (e.g.
            # pumpswap) route tokens through intermediary PDAs, so the fee payer
            # signs the transaction but a different wallet receives the tokens.
            all_owners = set(pre_map.keys()) | set(post_map.keys())

            # Check for newly appearing token accounts (buy signal): a wallet
            # that has tokens in post but was absent in pre.
            best_new_amount = 0.0
            for owner in all_owners:
                pre_amt = pre_map.get(owner, 0.0)
                post_amt = post_map.get(owner, 0.0)
                if pre_amt < 1e-9 and post_amt > best_new_amount and owner not in pre_map:
                    best_new_amount = post_amt
            if best_new_amount > 1e-9:
                return ParsedSwap(
                    signature=signature,
                    wallet=fee_payer,
                    direction="buy",
                    token_amount=best_new_amount,
                    block_time=block_time,
                )

            # Check for disappearing token accounts (sell signal): a wallet
            # that had tokens in pre but is absent or empty in post.
            best_closed_amount = 0.0
            for owner in all_owners:
                pre_amt = pre_map.get(owner, 0.0)
                post_amt = post_map.get(owner, 0.0)
                if pre_amt > best_closed_amount and post_amt < 1e-9 and owner not in post_map:
                    best_closed_amount = pre_amt
            if best_closed_amount > 1e-9:
                return ParsedSwap(
                    signature=signature,
                    wallet=fee_payer,
                    direction="sell",
                    token_amount=best_closed_amount,
                    block_time=block_time,
                )

            # Last resort: wallet with the largest absolute delta
            best_owner: Optional[str] = None
            best_abs_net = 0.0
            best_net_val = 0.0
            for owner in all_owners:
                owner_net = post_map.get(owner, 0.0) - pre_map.get(owner, 0.0)
                if abs(owner_net) > best_abs_net:
                    best_abs_net = abs(owner_net)
                    best_net_val = owner_net
                    best_owner = owner
            if best_owner and best_abs_net > 1e-9:
                direction = "buy" if best_net_val > 0 else "sell"
                return ParsedSwap(
                    signature=signature,
                    wallet=fee_payer,
                    direction=direction,
                    token_amount=best_abs_net,
                    block_time=block_time,
                )
            return None

        direction = "buy" if net > 0 else "sell"
        return ParsedSwap(
            signature=signature,
            wallet=fee_payer,
            direction=direction,
            token_amount=abs(net),
            block_time=block_time,
        )

    def _extract_fee_payer(self, message: Dict[str, Any]) -> Optional[str]:
        """Extract the fee payer (first signer) from a transaction message."""
        account_keys = message.get("accountKeys", [])
        if account_keys:
            first = account_keys[0]
            if isinstance(first, dict):
                return first.get("pubkey")
            if isinstance(first, str):
                return first
        return None

    def _extract_ui_amount(self, balance_entry: Dict[str, Any]) -> Optional[float]:
        """Extract UI amount from a token balance entry."""
        ui_info = balance_entry.get("uiTokenAmount", {})
        if not isinstance(ui_info, dict):
            return None

        ui_amount_str = ui_info.get("uiAmountString")
        if ui_amount_str not in (None, ""):
            try:
                return float(ui_amount_str)
            except (ValueError, TypeError):
                pass

        ui_amount = ui_info.get("uiAmount")
        if ui_amount is not None:
            try:
                return float(ui_amount)
            except (ValueError, TypeError):
                pass

        return None

    def _is_likely_swap(self, meta: Dict[str, Any], fee_payer: str) -> bool:
        """Check if transaction is likely a swap vs LP add/remove.

        Swaps produce mixed-direction balance changes across token mints
        (one token in, another out). LP operations change all tokens in the
        same direction. When only one mint appears in token balances, we
        assume SOL is the other side (not tracked in token balances) and
        treat it as a swap.
        """
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])

        pre_by_mint: Dict[str, float] = {}
        post_by_mint: Dict[str, float] = {}

        for bal in (pre_balances or []):
            if not isinstance(bal, dict):
                continue
            owner = bal.get("owner")
            if owner != fee_payer:
                continue
            mint = bal.get("mint", "")
            amount = self._extract_ui_amount(bal)
            if mint and amount is not None:
                pre_by_mint[mint] = pre_by_mint.get(mint, 0.0) + amount

        for bal in (post_balances or []):
            if not isinstance(bal, dict):
                continue
            owner = bal.get("owner")
            if owner != fee_payer:
                continue
            mint = bal.get("mint", "")
            amount = self._extract_ui_amount(bal)
            if mint and amount is not None:
                post_by_mint[mint] = post_by_mint.get(mint, 0.0) + amount

        all_mints = set(pre_by_mint.keys()) | set(post_by_mint.keys())
        if len(all_mints) < 2:
            # Single token mint — other side is likely SOL (native lamports)
            return True

        directions = []
        for mint in all_mints:
            pre = pre_by_mint.get(mint, 0.0)
            post = post_by_mint.get(mint, 0.0)
            net = post - pre
            if abs(net) > 1e-9:
                directions.append(net > 0)

        if not directions:
            return True

        # All same direction → LP add (all negative) or LP remove (all positive)
        if all(d == directions[0] for d in directions):
            return False

        return True

    def _detect_patterns(
        self, swaps: List[ParsedSwap], total_sampled: int
    ) -> WashTradingResult:
        """Analyze swap list for wash trading patterns and generate a score."""
        if not swaps:
            return WashTradingResult(
                total_transactions_sampled=total_sampled,
                manipulation_level="unknown",
            )

        wallets: Dict[str, WalletActivity] = {}

        for swap in swaps:
            if swap.wallet not in wallets:
                wallets[swap.wallet] = WalletActivity(wallet=swap.wallet)
            activity = wallets[swap.wallet]

            if swap.direction == "buy":
                activity.buy_count += 1
                activity.total_bought += swap.token_amount or 0.0
            else:
                activity.sell_count += 1

            if swap.block_time:
                if activity.first_seen is None or swap.block_time < activity.first_seen:
                    activity.first_seen = swap.block_time
                if activity.last_seen is None or swap.block_time > activity.last_seen:
                    activity.last_seen = swap.block_time

        return self._calculate_score(wallets, swaps, total_sampled)

    def _calculate_score(
        self,
        wallets: Dict[str, WalletActivity],
        swaps: List[ParsedSwap],
        total_sampled: int,
    ) -> WashTradingResult:
        """Calculate manipulation score from wallet activity patterns."""
        result = WashTradingResult(
            unique_wallets=len(wallets),
            total_transactions_sampled=total_sampled,
        )

        if not swaps or not wallets:
            result.manipulation_score = 0.0
            result.manipulation_level = "clean"
            return result

        total_swaps = len(swaps)
        buy_count = sum(1 for s in swaps if s.direction == "buy")
        sell_count = total_swaps - buy_count

        # Confidence flags
        if total_swaps < MIN_SAMPLE_SIZE:
            result.flags.append(
                f"Low sample size: {total_swaps} swaps parsed from {total_sampled} sampled transactions"
            )
        if total_sampled > 0 and total_swaps / total_sampled < 0.2:
            result.flags.append(
                f"Low parse rate: {total_swaps}/{total_sampled} transactions identified as swaps"
            )

        # Identify repeat buyers
        repeat_buyers = [
            w for w in wallets.values() if w.buy_count >= REPEAT_BUY_THRESHOLD
        ]
        repeat_buyers.sort(key=lambda w: w.buy_count, reverse=True)

        result.repeat_buyers = [
            {
                "wallet": w.wallet,
                "buy_count": w.buy_count,
                "sell_count": w.sell_count,
            }
            for w in repeat_buyers[:10]
        ]

        # --- Scoring factors ---
        score = 0.0

        # Factor 1: Repeat buyer ratio (0-3 points)
        # What fraction of total buy txs come from repeat buyers?
        repeat_buy_txs = sum(w.buy_count for w in repeat_buyers)
        if buy_count > 0:
            repeat_ratio = repeat_buy_txs / buy_count
            score += repeat_ratio * 3.0
            if repeat_ratio > 0.5:
                result.flags.append(
                    f"{repeat_ratio:.0%} of buys from repeat wallets"
                )

        # Factor 2: Max single-wallet buys (0-2.5 points)
        max_buys = max((w.buy_count for w in wallets.values()), default=0)
        if max_buys >= 5:
            score += 2.5
            result.flags.append(
                f"Single wallet made {max_buys} purchases"
            )
        elif max_buys >= 3:
            score += 1.5
            result.flags.append(
                f"Single wallet made {max_buys} purchases"
            )
        elif max_buys >= 2:
            score += 0.5

        # Factor 3: Wallet diversity (0-2 points)
        # Low unique wallets relative to transactions = suspicious
        if total_swaps > 0:
            diversity = len(wallets) / total_swaps
            if diversity < 0.3:
                score += 2.0
                result.flags.append(
                    f"Low wallet diversity: {len(wallets)} unique wallets in {total_swaps} txs"
                )
            elif diversity < 0.5:
                score += 1.0

        # Factor 4: Buy/sell asymmetry (0-1.5 points)
        # Heavy buying with little selling suggests accumulation before dump
        if total_swaps > 2:
            buy_ratio = buy_count / total_swaps
            if buy_ratio > 0.85:
                score += 1.5
                result.flags.append(
                    f"Extreme buy pressure: {buy_ratio:.0%} buys vs {1-buy_ratio:.0%} sells"
                )
            elif buy_ratio > 0.7:
                score += 0.75

        # Factor 5: Rapid trading from same wallet (0-1 point)
        for w in wallets.values():
            total_actions = w.buy_count + w.sell_count
            if (
                total_actions >= 3
                and w.first_seen
                and w.last_seen
                and w.first_seen != w.last_seen
            ):
                time_span = w.last_seen - w.first_seen
                if time_span > 0 and time_span < 300:  # 5 minutes
                    score += 1.0
                    result.flags.append(
                        f"Wallet {w.wallet[:8]}... made {total_actions} trades in {time_span}s"
                    )
                    break  # Only flag once

        # Clamp score to 0-10
        result.manipulation_score = min(10.0, max(0.0, round(score, 1)))

        # Classification
        if result.manipulation_score <= 2.0:
            result.manipulation_level = "clean"
        elif result.manipulation_score <= 5.0:
            result.manipulation_level = "moderate"
        elif result.manipulation_score <= 8.0:
            result.manipulation_level = "suspicious"
        else:
            result.manipulation_level = "critical"

        return result
