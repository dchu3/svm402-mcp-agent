"""Tests for the HeliusClient class."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.helius_client import (
    HeliusClient,
    HeliusAsset,
    HeliusEnhancedTransaction,
    HeliusPriorityFeeEstimate,
)


# ---------------------------------------------------------------------------
# Sample response fixtures
# ---------------------------------------------------------------------------

SAMPLE_DAS_ASSET = {
    "id": "So11111111111111111111111111111111111111112",
    "content": {
        "metadata": {
            "name": "Wrapped SOL",
            "symbol": "SOL",
            "description": "Wrapped Solana",
        },
        "json_uri": "",
    },
    "token_info": {
        "symbol": "SOL",
        "supply": 1000000,
        "decimals": 9,
        "price_info": {"price_per_token": 150.5},
    },
    "token_standard": "Fungible",
    "mutable": False,
    "frozen": False,
    "ownership": {"owner": "wallet123"},
    "grouping": [],
}

SAMPLE_NFT_ASSET = {
    "id": "NFTmint111111111111111111111111111111111111",
    "content": {
        "metadata": {"name": "Cool NFT #42", "symbol": "CNFT", "description": "An NFT"},
        "json_uri": "https://arweave.net/abc123",
    },
    "token_info": {},
    "token_standard": "NonFungible",
    "mutable": True,
    "frozen": False,
    "ownership": {"owner": "nft_owner_wallet"},
    "grouping": [{"group_key": "collection", "group_value": "CollectionMint123"}],
}

SAMPLE_TOKEN2022_ASSET = {
    "id": "Token2022Mint11111111111111111111111111111",
    "content": {
        "metadata": {"name": "Token-2022 Token", "symbol": "T22"},
    },
    "token_info": {
        "symbol": "T22",
        "supply": 500000,
        "decimals": 6,
        "price_info": {"price_per_token": 0.05},
        "token_program": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    },
    "token_standard": "Fungible",
    "mutable": False,
    "frozen": False,
    "ownership": {"owner": "wallet_t22"},
    "grouping": [],
}

SAMPLE_ENHANCED_TX = {
    "signature": "5abc123def456ghi789jkl012mno345pqr678stu901vwx234yz",
    "type": "SWAP",
    "source": "RAYDIUM",
    "feePayer": "wallet123",
    "timestamp": 1700000000,
    "description": "wallet123 swapped 1 SOL for 100 TOKEN",
    "tokenTransfers": [
        {
            "fromUserAccount": "wallet123",
            "toUserAccount": "pool456",
            "mint": "SOL_MINT",
            "tokenAmount": 1.0,
        }
    ],
    "nativeTransfers": [
        {
            "fromUserAccount": "wallet123",
            "toUserAccount": "pool456",
            "amount": 1000000000,
        }
    ],
    "accountData": [],
}

SAMPLE_PRIORITY_FEE_RESPONSE = {
    "jsonrpc": "2.0",
    "result": {
        "priorityFeeLevels": {
            "min": 0,
            "low": 100,
            "medium": 1000,
            "high": 10000,
            "veryHigh": 100000,
        }
    },
    "id": 1,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rpc_response(result):
    """Wrap a result in a JSON-RPC 2.0 response envelope."""
    return {"jsonrpc": "2.0", "result": result, "id": 1}


def _make_rpc_error(code: int = -32600, message: str = "Invalid request"):
    """Create a JSON-RPC error response."""
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": 1}


def _mock_httpx_response(json_data, status_code: int = 200):
    """Create a mock httpx.Response with the given JSON payload."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


def _make_client(api_key: str = "test-api-key") -> HeliusClient:
    """Create a HeliusClient with a test API key."""
    return HeliusClient(api_key=api_key)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHeliusClientInit:
    """Tests for HeliusClient constructor."""

    def test_creates_with_valid_api_key(self):
        client = HeliusClient(api_key="my-key")
        assert client is not None

    def test_raises_on_empty_api_key(self):
        with pytest.raises(ValueError):
            HeliusClient(api_key="")

    def test_raises_on_whitespace_api_key(self):
        with pytest.raises(ValueError):
            HeliusClient(api_key="   ")

    def test_custom_timeout(self):
        client = HeliusClient(api_key="key", timeout=60.0)
        assert client is not None


class TestGetAsset:
    """Tests for HeliusClient.get_asset (DAS getAsset)."""

    @pytest.mark.asyncio
    async def test_returns_helius_asset_on_success(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(_make_rpc_response(SAMPLE_DAS_ASSET))

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_asset("So11111111111111111111111111111111111111112")

        assert result is not None
        assert isinstance(result, HeliusAsset)
        assert result.id == "So11111111111111111111111111111111111111112"

    @pytest.mark.asyncio
    async def test_parses_asset_fields_correctly(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(_make_rpc_response(SAMPLE_DAS_ASSET))

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_asset("So11111111111111111111111111111111111111112")

        assert result.name == "Wrapped SOL"
        assert result.symbol == "SOL"

    @pytest.mark.asyncio
    async def test_returns_none_on_rpc_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(_make_rpc_error())

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_asset("BadMint")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response({}, status_code=500)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_asset("SomeMint")

        assert result is None


class TestGetAssetsByOwner:
    """Tests for HeliusClient.get_assets_by_owner (DAS getAssetsByOwner)."""

    @pytest.mark.asyncio
    async def test_returns_list_of_assets(self):
        client = _make_client()
        rpc_result = {"total": 2, "items": [SAMPLE_DAS_ASSET, SAMPLE_NFT_ASSET]}
        mock_resp = _mock_httpx_response(_make_rpc_response(rpc_result))

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_assets_by_owner("wallet123")

        assert len(results) == 2
        assert all(isinstance(a, HeliusAsset) for a in results)
        assert results[0].id == "So11111111111111111111111111111111111111112"
        assert results[1].id == "NFTmint111111111111111111111111111111111111"

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_no_items(self):
        client = _make_client()
        rpc_result = {"total": 0, "items": []}
        mock_resp = _mock_httpx_response(_make_rpc_response(rpc_result))

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_assets_by_owner("empty_wallet")

        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_rpc_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(_make_rpc_error())

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_assets_by_owner("wallet123")

        assert results == []

    @pytest.mark.asyncio
    async def test_passes_pagination_params(self):
        client = _make_client()
        rpc_result = {"total": 0, "items": []}
        mock_resp = _mock_httpx_response(_make_rpc_response(rpc_result))

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            await client.get_assets_by_owner("wallet123", page=3, limit=50)

        call_args = mock_http.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        params = body["params"]
        assert params["page"] == 3
        assert params["limit"] == 50


class TestGetTokenAccounts:
    """Tests for HeliusClient.get_token_accounts (DAS getTokenAccounts)."""

    @pytest.mark.asyncio
    async def test_returns_token_accounts(self):
        client = _make_client()
        rpc_result = {
            "token_accounts": [
                {"address": "acct1", "mint": "mint1", "amount": 1000},
                {"address": "acct2", "mint": "mint2", "amount": 2000},
            ]
        }
        mock_resp = _mock_httpx_response(_make_rpc_response(rpc_result))

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_token_accounts("mint1")

        assert len(results) == 2
        assert results[0]["address"] == "acct1"

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(_make_rpc_error())

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_token_accounts("bad_mint")

        assert results == []


class TestGetParsedTransactions:
    """Tests for HeliusClient.get_parsed_transactions (Enhanced Tx API)."""

    @pytest.mark.asyncio
    async def test_parses_single_transaction(self):
        client = _make_client()
        mock_resp = _mock_httpx_response([SAMPLE_ENHANCED_TX])

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_parsed_transactions(["5abc123"])

        assert len(results) == 1
        assert isinstance(results[0], HeliusEnhancedTransaction)
        assert results[0].signature == SAMPLE_ENHANCED_TX["signature"]
        assert results[0].type == "SWAP"
        assert results[0].source == "RAYDIUM"

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_empty_input(self):
        client = _make_client()

        results = await client.get_parsed_transactions([])

        assert results == []

    @pytest.mark.asyncio
    async def test_batches_large_signature_lists(self):
        """Signatures > 100 should be batched into multiple requests."""
        client = _make_client()
        signatures = [f"sig{i}" for i in range(150)]

        mock_resp = _mock_httpx_response([SAMPLE_ENHANCED_TX])

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_parsed_transactions(signatures)

        # Should have made 2 POST calls (100 + 50)
        assert mock_http.post.call_count == 2
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_http_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response([], status_code=500)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_parsed_transactions(["sig1"])

        assert results == []

    @pytest.mark.asyncio
    async def test_posts_to_enhanced_tx_endpoint(self):
        """Should POST to /v0/transactions/ not the RPC endpoint."""
        client = _make_client()
        mock_resp = _mock_httpx_response([SAMPLE_ENHANCED_TX])

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            await client.get_parsed_transactions(["sig1"])

        call_args = mock_http.post.call_args
        url = str(call_args[0][0]) if call_args[0] else str(call_args.kwargs.get("url", ""))
        assert "/v0/transactions" in url


class TestGetTransactionHistory:
    """Tests for HeliusClient.get_transaction_history (Enhanced Tx API GET)."""

    @pytest.mark.asyncio
    async def test_returns_parsed_transactions(self):
        client = _make_client()
        mock_resp = _mock_httpx_response([SAMPLE_ENHANCED_TX])

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_transaction_history("wallet123")

        assert len(results) == 1
        assert isinstance(results[0], HeliusEnhancedTransaction)
        assert results[0].fee_payer == "wallet123"

    @pytest.mark.asyncio
    async def test_uses_get_request(self):
        """Should use GET, not POST, for transaction history."""
        client = _make_client()
        mock_resp = _mock_httpx_response([SAMPLE_ENHANCED_TX])

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            await client.get_transaction_history("wallet123")

        mock_http.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_includes_address_in_url(self):
        client = _make_client()
        mock_resp = _mock_httpx_response([])

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            await client.get_transaction_history("wallet123")

        call_args = mock_http.get.call_args
        url = str(call_args[0][0]) if call_args[0] else str(call_args.kwargs.get("url", ""))
        assert "wallet123" in url
        assert "/v0/addresses/" in url

    @pytest.mark.asyncio
    async def test_passes_before_param(self):
        client = _make_client()
        mock_resp = _mock_httpx_response([])

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            await client.get_transaction_history("wallet123", before="old_sig")

        call_args = mock_http.get.call_args
        params = call_args.kwargs.get("params") or {}
        assert params.get("before") == "old_sig"

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_http_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response([], status_code=500)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            results = await client.get_transaction_history("wallet123")

        assert results == []


class TestGetPriorityFeeEstimate:
    """Tests for HeliusClient.get_priority_fee_estimate."""

    @pytest.mark.asyncio
    async def test_returns_priority_fee_estimate(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(SAMPLE_PRIORITY_FEE_RESPONSE)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_priority_fee_estimate(
                account_keys=["wallet123"]
            )

        assert result is not None
        assert isinstance(result, HeliusPriorityFeeEstimate)
        assert result.min == 0
        assert result.low == 100
        assert result.medium == 1000
        assert result.high == 10000
        assert result.very_high == 100000

    @pytest.mark.asyncio
    async def test_accepts_serialized_transaction(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(SAMPLE_PRIORITY_FEE_RESPONSE)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_priority_fee_estimate(
                serialized_transaction="base64txdata"
            )

        assert result is not None
        assert result.medium == 1000

    @pytest.mark.asyncio
    async def test_returns_none_on_rpc_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response(_make_rpc_error())

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_priority_fee_estimate(
                account_keys=["wallet123"]
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        client = _make_client()
        mock_resp = _mock_httpx_response({}, status_code=429)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_priority_fee_estimate(
                account_keys=["wallet123"]
            )

        assert result is None


class TestErrorHandling:
    """Tests for graceful error handling across methods."""

    @pytest.mark.asyncio
    async def test_get_asset_handles_connection_error(self):
        client = _make_client()

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_get_client.return_value = mock_http

            result = await client.get_asset("SomeMint")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_assets_by_owner_handles_timeout(self):
        client = _make_client()

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=httpx.ReadTimeout("Timeout"))
            mock_get_client.return_value = mock_http

            results = await client.get_assets_by_owner("wallet123")

        assert results == []

    @pytest.mark.asyncio
    async def test_get_parsed_transactions_handles_connection_error(self):
        client = _make_client()

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_get_client.return_value = mock_http

            results = await client.get_parsed_transactions(["sig1"])

        assert results == []

    @pytest.mark.asyncio
    async def test_get_transaction_history_handles_timeout(self):
        client = _make_client()

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=httpx.ReadTimeout("Timeout"))
            mock_get_client.return_value = mock_http

            results = await client.get_transaction_history("wallet123")

        assert results == []

    @pytest.mark.asyncio
    async def test_rpc_error_in_response_body(self):
        """RPC errors embedded in 200 responses should be handled gracefully."""
        client = _make_client()
        error_body = {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": "Method not found"},
            "id": 1,
        }
        mock_resp = _mock_httpx_response(error_body)

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_asset("SomeMint")

        assert result is None

    @pytest.mark.asyncio
    async def test_rate_limit_returns_none(self):
        """429 rate-limit responses should not crash."""
        client = _make_client()
        mock_resp = _mock_httpx_response(
            {"error": "Rate limit exceeded"}, status_code=429
        )

        with patch.object(client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_get_client.return_value = mock_http

            result = await client.get_asset("SomeMint")

        assert result is None


class TestParseAsset:
    """Direct unit tests on HeliusClient._parse_asset."""

    def setup_method(self):
        self.client = _make_client()

    def test_parse_fungible_token(self):
        asset = self.client._parse_asset(SAMPLE_DAS_ASSET)
        assert isinstance(asset, HeliusAsset)
        assert asset.id == "So11111111111111111111111111111111111111112"
        assert asset.name == "Wrapped SOL"
        assert asset.symbol == "SOL"
        assert asset.token_standard == "Fungible"
        assert asset.decimals == 9
        assert asset.supply == 1000000
        assert asset.price_info == {"price_per_token": 150.5}
        assert asset.owner == "wallet123"
        assert asset.frozen is False
        assert asset.mutable is False

    def test_parse_nft(self):
        asset = self.client._parse_asset(SAMPLE_NFT_ASSET)
        assert asset.id == "NFTmint111111111111111111111111111111111111"
        assert asset.name == "Cool NFT #42"
        assert asset.symbol == "CNFT"
        assert asset.token_standard == "NonFungible"
        assert asset.owner == "nft_owner_wallet"
        assert asset.mutable is True

    def test_parse_token2022(self):
        asset = self.client._parse_asset(SAMPLE_TOKEN2022_ASSET)
        assert asset.id == "Token2022Mint11111111111111111111111111111"
        assert asset.symbol == "T22"
        assert asset.decimals == 6
        assert asset.supply == 500000
        assert asset.price_info == {"price_per_token": 0.05}

    def test_parse_minimal_asset(self):
        """An asset with minimal fields should still parse without errors."""
        minimal = {"id": "MinimalMint123"}
        asset = self.client._parse_asset(minimal)
        assert asset.id == "MinimalMint123"

    def test_parse_none_returns_none(self):
        result = self.client._parse_asset(None)
        assert result is None

    def test_parse_empty_dict(self):
        """Empty dict should either return a HeliusAsset with empty id or None."""
        result = self.client._parse_asset({})
        # Depending on implementation, this might be None or an asset with empty id
        assert result is None or isinstance(result, HeliusAsset)

    def test_parse_asset_missing_content(self):
        """Asset without content block should still produce a HeliusAsset."""
        data = {"id": "MintNoContent", "token_info": {"decimals": 9}}
        asset = self.client._parse_asset(data)
        assert asset is not None
        assert asset.id == "MintNoContent"

    def test_parse_asset_missing_token_info(self):
        """Asset without token_info should still produce a HeliusAsset."""
        data = {
            "id": "MintNoToken",
            "content": {"metadata": {"name": "Test", "symbol": "TST"}},
        }
        asset = self.client._parse_asset(data)
        assert asset is not None
        assert asset.name == "Test"
        assert asset.symbol == "TST"

    def test_parse_asset_missing_price_info(self):
        """token_info without price_info should leave price as None."""
        data = {
            "id": "MintNoPrice",
            "token_info": {"symbol": "NP", "decimals": 6, "supply": 100},
        }
        asset = self.client._parse_asset(data)
        assert asset is not None
        assert asset.price_info is None


class TestParseEnhancedTransaction:
    """Direct unit tests on HeliusClient._parse_enhanced_transaction."""

    def setup_method(self):
        self.client = _make_client()

    def test_parse_swap_transaction(self):
        tx = self.client._parse_enhanced_transaction(SAMPLE_ENHANCED_TX)
        assert isinstance(tx, HeliusEnhancedTransaction)
        assert tx.signature == SAMPLE_ENHANCED_TX["signature"]
        assert tx.type == "SWAP"
        assert tx.source == "RAYDIUM"
        assert tx.fee_payer == "wallet123"
        assert tx.timestamp == 1700000000
        assert tx.description == "wallet123 swapped 1 SOL for 100 TOKEN"
        assert len(tx.token_transfers) == 1
        assert tx.token_transfers[0]["mint"] == "SOL_MINT"
        assert len(tx.native_transfers) == 1

    def test_parse_transaction_minimal_fields(self):
        """Transaction with only signature should parse."""
        data = {"signature": "sig_only"}
        tx = self.client._parse_enhanced_transaction(data)
        assert tx is not None
        assert tx.signature == "sig_only"

    def test_parse_none_returns_none(self):
        result = self.client._parse_enhanced_transaction(None)
        assert result is None

    def test_parse_empty_dict(self):
        result = self.client._parse_enhanced_transaction({})
        assert result is None or isinstance(result, HeliusEnhancedTransaction)

    def test_parse_transaction_with_empty_transfers(self):
        data = {
            "signature": "sig_empty_transfers",
            "type": "TRANSFER",
            "source": "SYSTEM_PROGRAM",
            "feePayer": "sender",
            "timestamp": 1700000001,
            "tokenTransfers": [],
            "nativeTransfers": [],
            "accountData": [],
        }
        tx = self.client._parse_enhanced_transaction(data)
        assert tx is not None
        assert tx.token_transfers == []
        assert tx.native_transfers == []

    def test_parse_transaction_with_multiple_transfers(self):
        data = {
            "signature": "sig_multi",
            "type": "SWAP",
            "source": "JUPITER",
            "feePayer": "wallet_multi",
            "timestamp": 1700000002,
            "tokenTransfers": [
                {"fromUserAccount": "a", "toUserAccount": "b", "mint": "m1", "tokenAmount": 10.0},
                {"fromUserAccount": "b", "toUserAccount": "c", "mint": "m2", "tokenAmount": 20.0},
            ],
            "nativeTransfers": [
                {"fromUserAccount": "a", "toUserAccount": "b", "amount": 5000},
            ],
            "accountData": [{"account": "a", "nativeBalanceChange": -5000}],
        }
        tx = self.client._parse_enhanced_transaction(data)
        assert tx is not None
        assert len(tx.token_transfers) == 2
        assert len(tx.native_transfers) == 1

    def test_parse_transaction_unknown_type(self):
        """Non-standard transaction types should still parse."""
        data = {
            "signature": "sig_unknown",
            "type": "UNKNOWN",
            "source": "UNKNOWN_PROGRAM",
            "feePayer": "wallet_unknown",
            "timestamp": 1700000003,
        }
        tx = self.client._parse_enhanced_transaction(data)
        assert tx is not None
        assert tx.type == "UNKNOWN"


class TestClose:
    """Tests for HeliusClient.close."""

    @pytest.mark.asyncio
    async def test_close_does_not_raise(self):
        """Closing a client should not raise even if never used."""
        client = _make_client()
        await client.close()
