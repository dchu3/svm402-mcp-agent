"""Async client for Helius Solana-specific APIs (DAS, Enhanced Transactions, Priority Fees)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

HELIUS_API_BASE = "https://api.helius.xyz"
HELIUS_RPC_BASE = "https://mainnet.helius-rpc.com"

# Timeout for Helius API calls
DEFAULT_TIMEOUT = 30.0


@dataclass
class HeliusAsset:
    """Parsed asset data from Helius DAS API."""

    id: str  # mint address
    name: Optional[str] = None
    symbol: Optional[str] = None
    token_standard: Optional[str] = None  # "Fungible", "NonFungible", "FungibleAsset", etc.
    supply: Optional[float] = None
    decimals: Optional[int] = None
    metadata_uri: Optional[str] = None
    description: Optional[str] = None
    mutable: Optional[bool] = None
    frozen: Optional[bool] = None
    owner: Optional[str] = None
    # Token-specific
    price_info: Optional[Dict[str, Any]] = None
    # Collection info (NFTs)
    collection_key: Optional[str] = None
    collection_verified: Optional[bool] = None


@dataclass
class HeliusEnhancedTransaction:
    """Parsed enhanced transaction from Helius."""

    signature: str
    type: str  # "SWAP", "TRANSFER", "NFT_SALE", etc.
    source: str  # "RAYDIUM", "JUPITER", "ORCA", etc.
    fee_payer: str
    timestamp: Optional[int] = None
    description: Optional[str] = None
    # Token transfers within the tx
    token_transfers: List[Dict[str, Any]] = field(default_factory=list)
    # Native SOL transfers
    native_transfers: List[Dict[str, Any]] = field(default_factory=list)
    # Account data changes
    account_data: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class HeliusPriorityFeeEstimate:
    """Priority fee estimate from Helius."""

    min: Optional[int] = None
    low: Optional[int] = None
    medium: Optional[int] = None
    high: Optional[int] = None
    very_high: Optional[int] = None


class HeliusClient:
    """Async HTTP client for Helius Solana APIs."""

    def __init__(self, api_key: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("Helius API key is required")
        self._api_key = api_key
        self._timeout = timeout
        self._rpc_url = f"{HELIUS_RPC_BASE}/?api-key={api_key}"
        self._api_url = f"{HELIUS_API_BASE}/v0"
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── DAS API Methods ──

    async def get_asset(self, asset_id: str) -> Optional[HeliusAsset]:
        """Get detailed asset info via DAS API (getAsset).

        Works for SPL tokens, Token-2022, NFTs, and compressed NFTs.
        """
        # DAS methods accept a dict param (not wrapped in a list)
        result = await self._das_request("getAsset", {"id": asset_id})
        if not result:
            return None
        return self._parse_asset(result)

    async def get_assets_by_owner(
        self, owner: str, *, page: int = 1, limit: int = 100
    ) -> List[HeliusAsset]:
        """Get all assets owned by a wallet via DAS API."""
        result = await self._das_request(
            "getAssetsByOwner",
            {
                "ownerAddress": owner,
                "page": page,
                "limit": limit,
                "displayOptions": {"showFungible": True, "showNativeBalance": True},
            },
        )
        if not result:
            return []
        items = result.get("items", [])
        return [a for a in (self._parse_asset(item) for item in items if isinstance(item, dict)) if a is not None]

    async def get_token_accounts(
        self, mint: str, *, page: int = 1, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get token accounts for a specific mint via DAS API.

        Returns top holders sorted by amount descending.
        """
        result = await self._das_request(
            "getTokenAccounts",
            {
                "mint": mint,
                "page": page,
                "limit": limit,
            },
        )
        if not result:
            return []
        return result.get("token_accounts", [])

    # ── Enhanced Transactions API ──

    async def get_parsed_transactions(
        self, signatures: List[str]
    ) -> List[HeliusEnhancedTransaction]:
        """Get enhanced/parsed transactions by signature.

        Helius parses raw Solana transactions into human-readable format
        with labeled swap types, token transfers, and program interactions.
        Max 100 signatures per request.
        """
        if not signatures:
            return []
        # Batch in groups of 100
        all_txs: List[HeliusEnhancedTransaction] = []
        for i in range(0, len(signatures), 100):
            batch = signatures[i : i + 100]
            try:
                client = await self._get_client()
                url = f"{self._api_url}/transactions/?api-key={self._api_key}"
                resp = await client.post(url, json={"transactions": batch})
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for tx in data:
                        if isinstance(tx, dict):
                            parsed = self._parse_enhanced_transaction(tx)
                            if parsed:
                                all_txs.append(parsed)
            except httpx.HTTPStatusError as e:
                logger.warning("Helius enhanced tx API error: %s", e)
            except Exception as e:
                logger.warning("Helius enhanced tx request failed: %s", e)
        return all_txs

    async def get_transaction_history(
        self,
        address: str,
        *,
        before: Optional[str] = None,
        limit: int = 100,
    ) -> List[HeliusEnhancedTransaction]:
        """Get parsed transaction history for an address.

        Uses Helius enhanced transactions endpoint with address filter.
        """
        try:
            client = await self._get_client()
            params: Dict[str, Any] = {"api-key": self._api_key, "limit": limit}
            if before:
                params["before"] = before
            url = f"{self._api_url}/addresses/{address}/transactions"
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            txs: List[HeliusEnhancedTransaction] = []
            if isinstance(data, list):
                for tx in data:
                    if isinstance(tx, dict):
                        parsed = self._parse_enhanced_transaction(tx)
                        if parsed:
                            txs.append(parsed)
            return txs
        except httpx.HTTPStatusError as e:
            logger.warning("Helius tx history API error: %s", e)
            return []
        except Exception as e:
            logger.warning("Helius tx history request failed: %s", e)
            return []

    # ── Priority Fee API ──

    async def get_priority_fee_estimate(
        self,
        account_keys: Optional[List[str]] = None,
        serialized_transaction: Optional[str] = None,
    ) -> Optional[HeliusPriorityFeeEstimate]:
        """Get priority fee estimates from Helius.

        Can estimate by account keys or by serialized transaction (more accurate).
        """
        params: Dict[str, Any] = {}
        if serialized_transaction:
            params["transaction"] = serialized_transaction
        elif account_keys:
            params["accountKeys"] = account_keys
        else:
            return None

        params["options"] = {"includeAllPriorityFeeLevels": True}

        result = await self._rpc_request("getPriorityFeeEstimate", [params])
        if not result:
            return None

        levels = result.get("priorityFeeLevels", {})
        return HeliusPriorityFeeEstimate(
            min=self._safe_int(levels.get("min")),
            low=self._safe_int(levels.get("low")),
            medium=self._safe_int(levels.get("medium")),
            high=self._safe_int(levels.get("high")),
            very_high=self._safe_int(levels.get("veryHigh")),
        )

    # ── Internal Helpers ──

    async def _das_request(
        self, method: str, params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Make a DAS JSON-RPC request to Helius.

        DAS methods accept params as a direct dict (not wrapped in a list).
        """
        return await self._rpc_request(method, params)

    async def _rpc_request(
        self, method: str, params: Any
    ) -> Optional[Dict[str, Any]]:
        """Make a JSON-RPC request to the Helius RPC endpoint."""
        try:
            client = await self._get_client()
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }
            resp = await client.post(self._rpc_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.warning("Helius RPC error for %s: %s", method, data["error"])
                return None
            return data.get("result")
        except httpx.HTTPStatusError as e:
            logger.warning("Helius RPC HTTP error for %s: %s", method, e)
            return None
        except Exception as e:
            logger.warning("Helius RPC request failed for %s: %s", method, e)
            return None

    def _parse_asset(self, data: Dict[str, Any]) -> Optional[HeliusAsset]:
        """Parse a DAS API asset response into a HeliusAsset."""
        if not data or not isinstance(data, dict):
            return None
        content = data.get("content", {})
        metadata = content.get("metadata", {})
        token_info = data.get("token_info", {})
        grouping = data.get("grouping", [])

        collection_key = None
        collection_verified = None
        for group in grouping:
            if isinstance(group, dict) and group.get("group_key") == "collection":
                collection_key = group.get("group_value")
                collection_verified = group.get("verified")

        return HeliusAsset(
            id=data.get("id", ""),
            name=metadata.get("name"),
            symbol=metadata.get("symbol") or token_info.get("symbol"),
            token_standard=data.get("token_standard"),
            supply=self._safe_float(token_info.get("supply")),
            decimals=self._safe_int(token_info.get("decimals")),
            metadata_uri=content.get("json_uri"),
            description=metadata.get("description"),
            mutable=data.get("mutable"),
            frozen=data.get("frozen"),
            owner=data.get("ownership", {}).get("owner"),
            price_info=token_info.get("price_info"),
            collection_key=collection_key,
            collection_verified=collection_verified,
        )

    def _parse_enhanced_transaction(
        self, data: Dict[str, Any]
    ) -> Optional[HeliusEnhancedTransaction]:
        """Parse a Helius enhanced transaction response."""
        if not data or not isinstance(data, dict):
            return None
        signature = data.get("signature")
        if not signature:
            return None

        return HeliusEnhancedTransaction(
            signature=signature,
            type=data.get("type", "UNKNOWN"),
            source=data.get("source", "UNKNOWN"),
            fee_payer=data.get("feePayer", ""),
            timestamp=data.get("timestamp"),
            description=data.get("description"),
            token_transfers=data.get("tokenTransfers", []),
            native_transfers=data.get("nativeTransfers", []),
            account_data=data.get("accountData", []),
        )

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
