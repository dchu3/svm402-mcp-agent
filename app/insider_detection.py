"""Insider / sniper detection for Solana tokens via direct RPC calls."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_RPC_TIMEOUT = 15
_RPC_MAX_RETRY_DELAY = 30.0
_RPC_CONCURRENCY = 3
_TOP_HOLDERS_TO_ANALYSE = 10
_DUMP_CHECK_COUNT = 5


class InsiderRisk(str, Enum):
    """Risk level from insider analysis."""

    REJECT = "reject"
    WARN = "warn"
    CLEAN = "clean"


@dataclass
class InsiderAnalysis:
    """Result of insider/sniper analysis for a token."""

    risk: InsiderRisk = InsiderRisk.CLEAN
    top_holder_concentration_pct: float = 0.0
    creator_holding_pct: float = 0.0
    creator_address: Optional[str] = None
    dumping_holders: int = 0
    total_top_holders_checked: int = 0
    summary: str = ""
    errors: List[str] = field(default_factory=list)


def _rpc_retry_delay(
    resp: Optional[httpx.Response],
    attempt: int,
    base_delay: float = 2.0,
) -> float:
    """Compute delay before next RPC retry (honours Retry-After for 429)."""
    if resp is not None and resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "")
        try:
            return min(float(retry_after), _RPC_MAX_RETRY_DELAY)
        except (ValueError, TypeError):
            pass
    return min(base_delay * (2 ** attempt), _RPC_MAX_RETRY_DELAY)


async def _rpc_call(
    client: httpx.AsyncClient,
    rpc_url: str,
    method: str,
    params: list,
    retries: int = 2,
) -> Optional[Any]:
    """Execute a single Solana JSON-RPC call with retry logic."""
    for attempt in range(retries + 1):
        resp = None
        try:
            resp = await client.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            if resp.status_code == 429:
                if attempt < retries:
                    delay = _rpc_retry_delay(resp, attempt)
                    logger.debug(
                        "RPC rate limited for %s (attempt %s/%s), retrying in %.1fs",
                        method,
                        attempt + 1,
                        retries + 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("result")
        except Exception as exc:
            if attempt < retries:
                delay = _rpc_retry_delay(resp, attempt)
                logger.debug(
                    "RPC call failed for %s (attempt %s/%s): %s; retrying in %.1fs",
                    method,
                    attempt + 1,
                    retries + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning("RPC call failed for %s after retries: %s", method, exc)
            return None
    return None


async def _get_token_largest_accounts(
    client: httpx.AsyncClient,
    mint: str,
    rpc_url: str,
) -> List[Dict[str, Any]]:
    """Fetch top token holders via getTokenLargestAccounts."""
    result = await _rpc_call(client, rpc_url, "getTokenLargestAccounts", [mint])
    if result and isinstance(result.get("value"), list):
        return result["value"]
    return []


async def _get_token_supply(
    client: httpx.AsyncClient,
    mint: str,
    rpc_url: str,
) -> Optional[float]:
    """Fetch total token supply via getTokenSupply."""
    result = await _rpc_call(client, rpc_url, "getTokenSupply", [mint])
    if result and isinstance(result.get("value"), dict):
        try:
            return float(result["value"].get("uiAmount", 0))
        except (TypeError, ValueError):
            pass
    return None


async def _get_mint_first_signature(
    client: httpx.AsyncClient,
    mint: str,
    rpc_url: str,
) -> Optional[str]:
    """Get the earliest signature on the mint account (token creation tx).

    getSignaturesForAddress returns newest-first, so we fetch a large batch
    and take the last element (oldest in the batch).  For tokens with >1000
    mint-account signatures the result may not be the true creation tx, but
    it is a reasonable heuristic.
    """
    result = await _rpc_call(
        client,
        rpc_url,
        "getSignaturesForAddress",
        [mint, {"limit": 1000, "commitment": "finalized"}],
    )
    if isinstance(result, list) and result:
        return result[-1].get("signature")
    return None


async def _get_transaction(
    client: httpx.AsyncClient,
    signature: str,
    rpc_url: str,
) -> Optional[Dict[str, Any]]:
    """Fetch a parsed transaction."""
    result = await _rpc_call(
        client,
        rpc_url,
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if isinstance(result, dict):
        return result
    return None


def _extract_creator_from_tx(tx: Dict[str, Any]) -> Optional[str]:
    """Extract the likely creator (fee payer) from a token creation transaction."""
    try:
        message = tx.get("transaction", {}).get("message", {})
        account_keys = message.get("accountKeys", [])
        if account_keys:
            first = account_keys[0]
            if isinstance(first, dict):
                return first.get("pubkey")
            if isinstance(first, str):
                return first
    except (AttributeError, IndexError, TypeError):
        pass
    return None


def _holder_is_actively_trading(
    signatures: List[Dict[str, Any]],
) -> bool:
    """Heuristic: check if a top holder is actively trading.

    We look for transactions with no errors (successful) — a high volume of
    recent activity from a top holder often indicates trading/dumping.
    With basic signature data we can't distinguish buy from sell, but if a
    top holder has many recent transactions (>5 in the last batch) it's a
    signal of active trading rather than holding.
    """
    successful = [s for s in signatures if s.get("err") is None]
    return len(successful) > 5


async def analyse_insiders(
    mint: str,
    rpc_url: str,
    max_concentration_pct: float = 50.0,
    max_creator_pct: float = 30.0,
    warn_concentration_pct: float = 30.0,
    warn_creator_pct: float = 10.0,
) -> InsiderAnalysis:
    """Run insider/sniper analysis for a Solana token.

    Returns an InsiderAnalysis with risk level, holder concentration,
    creator holding %, and dump-behaviour signals.
    """
    analysis = InsiderAnalysis()

    if not rpc_url or not str(rpc_url).strip():
        raise ValueError("rpc_url is required for insider analysis")
    rpc_url = rpc_url.strip()

    sem = asyncio.Semaphore(_RPC_CONCURRENCY)

    async def _bounded_rpc(method: str, params: list) -> Optional[Any]:
        async with sem:
            return await _rpc_call(client, rpc_url, method, params)

    async def _bounded_call(coro: Any) -> Any:
        async with sem:
            return await coro

    async with httpx.AsyncClient(timeout=_RPC_TIMEOUT) as client:
        # --- Step 1: fetch top holders + total supply in parallel ---
        holders_task = _bounded_call(_get_token_largest_accounts(client, mint, rpc_url))
        supply_task = _bounded_call(_get_token_supply(client, mint, rpc_url))
        holders, total_supply = await asyncio.gather(holders_task, supply_task)

        if not holders or not total_supply or total_supply <= 0:
            analysis.summary = "Insufficient on-chain data for insider analysis"
            if not holders:
                analysis.errors.append("getTokenLargestAccounts returned no data")
            if not total_supply or total_supply <= 0:
                analysis.errors.append("getTokenSupply returned no data")
            return analysis

        # --- Step 2: compute top-holder concentration ---
        top_n = holders[:_TOP_HOLDERS_TO_ANALYSE]
        concentration = 0.0
        for h in top_n:
            try:
                amount = float(h.get("uiAmount", 0) or 0)
                concentration += amount
            except (TypeError, ValueError):
                continue
        analysis.top_holder_concentration_pct = (concentration / total_supply) * 100.0
        analysis.total_top_holders_checked = len(top_n)

        # --- Step 3: detect creator wallet ---
        first_sig = await _bounded_call(_get_mint_first_signature(client, mint, rpc_url))
        if first_sig:
            tx = await _bounded_call(_get_transaction(client, first_sig, rpc_url))
            if tx:
                creator = _extract_creator_from_tx(tx)
                if creator:
                    analysis.creator_address = creator

        # --- Step 3b: resolve token account owners for creator matching ---
        creator_pct = 0.0
        if analysis.creator_address and top_n:
            owner_tasks = [
                _bounded_rpc(
                    "getAccountInfo",
                    [h.get("address", ""), {"encoding": "jsonParsed"}],
                )
                for h in top_n
            ]
            owner_results = await asyncio.gather(*owner_tasks)
            for h, result in zip(top_n, owner_results):
                if result is None:
                    continue
                try:
                    owner = (
                        result.get("value", {})
                        .get("data", {})
                        .get("parsed", {})
                        .get("info", {})
                        .get("owner", "")
                    )
                except AttributeError:
                    continue
                if owner == analysis.creator_address:
                    try:
                        amount = float(h.get("uiAmount", 0) or 0)
                        creator_pct += (amount / total_supply) * 100.0
                    except (TypeError, ValueError):
                        continue
        analysis.creator_holding_pct = creator_pct

        # --- Step 4: detect dump behaviour from top holders ---
        dump_check_holders = top_n[:_DUMP_CHECK_COUNT]
        sig_tasks = [
            _bounded_rpc(
                "getSignaturesForAddress",
                [h.get("address", ""), {"limit": 10}],
            )
            for h in dump_check_holders
        ]
        sig_results = await asyncio.gather(*sig_tasks)
        dumping = 0
        for sigs in sig_results:
            parsed = sigs if isinstance(sigs, list) else []
            if _holder_is_actively_trading(parsed):
                dumping += 1
        analysis.dumping_holders = dumping

    # --- Step 5: classify risk ---
    reasons: List[str] = []

    if analysis.top_holder_concentration_pct > max_concentration_pct:
        analysis.risk = InsiderRisk.REJECT
        reasons.append(
            f"top-{_TOP_HOLDERS_TO_ANALYSE} hold "
            f"{analysis.top_holder_concentration_pct:.1f}% (>{max_concentration_pct:.0f}%)"
        )
    if analysis.creator_holding_pct > max_creator_pct:
        analysis.risk = InsiderRisk.REJECT
        reasons.append(
            f"creator holds {analysis.creator_holding_pct:.1f}% (>{max_creator_pct:.0f}%)"
        )

    if analysis.risk != InsiderRisk.REJECT:
        if analysis.top_holder_concentration_pct > warn_concentration_pct:
            analysis.risk = InsiderRisk.WARN
            reasons.append(
                f"top-{_TOP_HOLDERS_TO_ANALYSE} hold "
                f"{analysis.top_holder_concentration_pct:.1f}% (>{warn_concentration_pct:.0f}%)"
            )
        if analysis.creator_holding_pct > warn_creator_pct:
            analysis.risk = InsiderRisk.WARN
            reasons.append(
                f"creator holds {analysis.creator_holding_pct:.1f}% (>{warn_creator_pct:.0f}%)"
            )
        if analysis.dumping_holders >= 3:
            analysis.risk = InsiderRisk.WARN
            reasons.append(
                f"{analysis.dumping_holders}/{len(dump_check_holders)} "
                "top holders showing active trading"
            )

    if reasons:
        analysis.summary = "; ".join(reasons)
    else:
        analysis.summary = (
            f"Clean: top-{_TOP_HOLDERS_TO_ANALYSE} hold "
            f"{analysis.top_holder_concentration_pct:.1f}%, "
            f"creator {analysis.creator_holding_pct:.1f}%"
        )

    return analysis
