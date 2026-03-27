"""Tests for TraderExecutionService.probe_slippage()."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from app.execution import AtomicTradeExecution, TradeQuote, TraderExecutionService


# ---------------------------------------------------------------------------
# Minimal mock MCP manager
# ---------------------------------------------------------------------------


class _MockTraderClient:
    def __init__(self, price: float = 0.01) -> None:
        self.price = price
        self.tools: List[Dict[str, Any]] = [
            {
                "name": "getQuote",
                "inputSchema": {
                    "type": "object",
                    "required": ["chain", "inputMint", "outputMint", "amountUsd", "slippageBps", "side"],
                    "properties": {
                        "chain": {"type": "string"},
                        "inputMint": {"type": "string"},
                        "outputMint": {"type": "string"},
                        "amountUsd": {"type": "number"},
                        "slippageBps": {"type": "integer"},
                        "side": {"type": "string"},
                    },
                },
            },
        ]

    async def call_tool(self, method: str, arguments: Dict[str, Any]) -> Any:
        if method == "getQuote":
            return {"priceUsd": str(self.price), "liquidityUsd": 100_000}
        raise ValueError(f"Unexpected method: {method}")


class _MockMCPManager:
    def __init__(self, trader: _MockTraderClient) -> None:
        self._trader = trader

    def get_client(self, name: str) -> Any:
        if name == "trader":
            return self._trader
        return None


def _make_service(price: float = 0.01) -> TraderExecutionService:
    trader = _MockTraderClient(price=price)
    manager = _MockMCPManager(trader=trader)
    return TraderExecutionService(
        mcp_manager=manager,
        chain="solana",
        max_slippage_bps=300,
        rpc_url="https://test-rpc",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTraderExecutionServiceInit:
    def test_requires_rpc_url_for_solana(self):
        trader = _MockTraderClient(price=0.01)
        manager = _MockMCPManager(trader=trader)
        with pytest.raises(ValueError, match="rpc_url is required"):
            TraderExecutionService(
                mcp_manager=manager,
                chain="solana",
                max_slippage_bps=300,
                rpc_url="   ",
            )

    def test_rejects_non_solana_chain(self):
        trader = _MockTraderClient(price=0.01)
        manager = _MockMCPManager(trader=trader)
        with pytest.raises(ValueError, match="supports only solana"):
            TraderExecutionService(
                mcp_manager=manager,
                chain="ethereum",
                max_slippage_bps=300,
                rpc_url="https://test-rpc",
            )


class TestProbeSlippage:
    """Unit tests for TraderExecutionService.probe_slippage()."""

    @pytest.mark.asyncio
    async def test_acceptable_slippage_returns_no_abort(self):
        """Probe succeeds with slippage within threshold — should_abort is False."""
        svc = _make_service(price=0.01)
        quoted_price = 0.01
        # actual entry matches quoted exactly → 0% deviation
        actual_entry = quoted_price

        with patch.object(svc, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(
                svc, "execute_atomic_trade", new_callable=AsyncMock
            ) as mock_atomic:
            from app.execution import TradeQuote
            mock_quote.return_value = TradeQuote(price=quoted_price, method="mock", raw={})
            mock_atomic.return_value = AtomicTradeExecution(
                success=True, entry_price=actual_entry
            )
            should_abort, slippage_pct, reason = await svc.probe_slippage(
                token_address="TokenAAA",
                probe_usd=0.50,
                input_price_usd=180.0,
                max_slippage_pct=5.0,
            )

        assert should_abort is False
        assert slippage_pct == pytest.approx(0.0, abs=1e-9)
        assert reason is None

    @pytest.mark.asyncio
    async def test_excessive_slippage_returns_abort(self):
        """Probe actual price deviates >threshold — should_abort is True."""
        svc = _make_service(price=0.01)
        quoted_price = 0.01
        # 10% worse entry price
        actual_entry = quoted_price * 1.10

        with patch.object(svc, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(
                svc, "execute_atomic_trade", new_callable=AsyncMock
            ) as mock_atomic:
            from app.execution import TradeQuote
            mock_quote.return_value = TradeQuote(price=quoted_price, method="mock", raw={})
            mock_atomic.return_value = AtomicTradeExecution(
                success=True, entry_price=actual_entry
            )
            should_abort, slippage_pct, reason = await svc.probe_slippage(
                token_address="TokenAAA",
                probe_usd=0.50,
                input_price_usd=180.0,
                max_slippage_pct=5.0,
            )

        assert should_abort is True
        assert slippage_pct == pytest.approx(10.0, rel=1e-3)
        assert reason is not None
        assert "10.0%" in reason

    @pytest.mark.asyncio
    async def test_atomic_trade_failure_degrades_gracefully(self):
        """If buy_and_sell fails, probe returns should_abort=False (don't block trade)."""
        svc = _make_service(price=0.01)

        with patch.object(svc, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(
                svc, "execute_atomic_trade", new_callable=AsyncMock
            ) as mock_atomic:
            from app.execution import TradeQuote
            mock_quote.return_value = TradeQuote(price=0.01, method="mock", raw={})
            mock_atomic.return_value = AtomicTradeExecution(
                success=False, error="buy_and_sell not available"
            )
            should_abort, slippage_pct, reason = await svc.probe_slippage(
                token_address="TokenAAA",
                probe_usd=0.50,
                input_price_usd=180.0,
                max_slippage_pct=5.0,
            )

        assert should_abort is False
        assert slippage_pct is None

    @pytest.mark.asyncio
    async def test_quote_failure_degrades_gracefully(self):
        """If get_quote raises, probe returns should_abort=False."""
        svc = _make_service(price=0.01)

        with patch.object(svc, "get_quote", new_callable=AsyncMock) as mock_quote:
            mock_quote.side_effect = RuntimeError("quote failed")
            should_abort, slippage_pct, reason = await svc.probe_slippage(
                token_address="TokenAAA",
                probe_usd=0.50,
                input_price_usd=180.0,
                max_slippage_pct=5.0,
            )

        assert should_abort is False
        assert slippage_pct is None

    @pytest.mark.asyncio
    async def test_slippage_below_threshold_is_allowed(self):
        """Slippage just below the threshold is not aborted."""
        svc = _make_service(price=0.01)
        quoted_price = 0.01
        actual_entry = quoted_price * 1.049  # ~4.9%, below 5% threshold

        with patch.object(svc, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(
                svc, "execute_atomic_trade", new_callable=AsyncMock
            ) as mock_atomic:
            from app.execution import TradeQuote
            mock_quote.return_value = TradeQuote(price=quoted_price, method="mock", raw={})
            mock_atomic.return_value = AtomicTradeExecution(
                success=True, entry_price=actual_entry
            )
            should_abort, slippage_pct, reason = await svc.probe_slippage(
                token_address="TokenAAA",
                probe_usd=0.50,
                input_price_usd=180.0,
                max_slippage_pct=5.0,
            )

        assert should_abort is False
        assert slippage_pct is not None
        assert slippage_pct < 5.0


# ---------------------------------------------------------------------------
# verify_transaction_success tests
# ---------------------------------------------------------------------------


class TestVerifyTransactionSuccess:
    """Unit tests for verify_transaction_success()."""

    @pytest.mark.asyncio
    async def test_returns_true_when_tx_confirmed(self):
        """Returns True when transaction confirmed with no error."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.execution import verify_transaction_success

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"result": {"meta": {"err": None}}}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=0)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_tx_has_error(self):
        """Returns False when meta.err is set (tx failed on-chain)."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.execution import verify_transaction_success

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"result": {"meta": {"err": {"InstructionError": [0, "SlippageToleranceExceeded"]}}}}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=0)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_none_when_tx_not_found(self):
        """Returns None when RPC returns null result (tx not yet indexed)."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.execution import verify_transaction_success

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"result": None}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=0)

        assert result is None

    @pytest.mark.asyncio
    async def test_retries_on_rpc_error_then_succeeds(self):
        """Retries after RPC failure and returns True on subsequent success."""
        from unittest.mock import AsyncMock, MagicMock, call, patch
        from app.execution import verify_transaction_success

        success_response = MagicMock()
        success_response.raise_for_status = MagicMock()
        success_response.json.return_value = {"result": {"meta": {"err": None}}}

        call_count = 0

        async def _post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("RPC timeout")
            return success_response

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = _post
            mock_client_cls.return_value = mock_client

            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=0.0)

        assert result is True
        assert call_count == 2
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_after_all_retries_exhausted(self):
        """Returns None (not raises) when all retry attempts fail."""
        from unittest.mock import AsyncMock, patch
        from app.execution import verify_transaction_success

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=ConnectionError("RPC down"))
            mock_client_cls.return_value = mock_client

            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=0.0)

        assert result is None

    @pytest.mark.asyncio
    async def test_429_with_retry_after_header_respects_header(self):
        """429 response with Retry-After header: sleep duration uses the header value."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.execution import verify_transaction_success

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "7"}

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.raise_for_status = MagicMock()
        success_response.json.return_value = {"result": {"meta": {"err": None}}}

        responses = iter([rate_limited, success_response])

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=lambda *a, **kw: next(responses))
            mock_client_cls.return_value = mock_client

            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=5.0)

        assert result is True
        mock_sleep.assert_called_once_with(7.0)

    @pytest.mark.asyncio
    async def test_429_without_retry_after_uses_exponential_backoff(self):
        """429 without Retry-After header: sleep uses exponential backoff (base * 2^attempt)."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.execution import verify_transaction_success

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {}  # no Retry-After

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.raise_for_status = MagicMock()
        success_response.json.return_value = {"result": {"meta": {"err": None}}}

        responses = iter([rate_limited, success_response])

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=lambda *a, **kw: next(responses))
            mock_client_cls.return_value = mock_client

            # attempt=0, base=5.0 → delay = min(5.0 * 2^0, 30) = 5.0
            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=5.0)

        assert result is True
        mock_sleep.assert_called_once_with(5.0)

    @pytest.mark.asyncio
    async def test_429_all_retries_exhausted_returns_none(self):
        """Returns None when all retries are 429 responses."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.execution import verify_transaction_success

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {}

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=rate_limited)
            mock_client_cls.return_value = mock_client

            result = await verify_transaction_success("tx123", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=0.0)

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_value_error_when_rpc_url_blank(self):
        from app.execution import verify_transaction_success

        with pytest.raises(ValueError, match="rpc_url is required"):
            await verify_transaction_success("tx123", rpc_url="   ", retries=0)


# ---------------------------------------------------------------------------
# _rpc_retry_delay unit tests
# ---------------------------------------------------------------------------


class TestRpcRetryDelay:
    """Unit tests for the _rpc_retry_delay helper."""

    def test_uses_retry_after_header_for_429(self):
        from unittest.mock import MagicMock
        from app.execution import _rpc_retry_delay

        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "10"}
        assert _rpc_retry_delay(resp, attempt=0, base_delay=5.0) == 10.0

    def test_caps_retry_after_at_max_delay(self):
        from unittest.mock import MagicMock
        from app.execution import _rpc_retry_delay

        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "999"}
        assert _rpc_retry_delay(resp, attempt=0, base_delay=5.0) == 30.0

    def test_exponential_backoff_without_retry_after(self):
        from unittest.mock import MagicMock
        from app.execution import _rpc_retry_delay

        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {}
        # attempt 0: 5 * 2^0 = 5.0
        assert _rpc_retry_delay(resp, attempt=0, base_delay=5.0) == 5.0
        # attempt 1: 5 * 2^1 = 10.0
        assert _rpc_retry_delay(resp, attempt=1, base_delay=5.0) == 10.0
        # attempt 2: 5 * 2^2 = 20.0
        assert _rpc_retry_delay(resp, attempt=2, base_delay=5.0) == 20.0

    def test_exponential_backoff_for_non_429(self):
        from unittest.mock import MagicMock
        from app.execution import _rpc_retry_delay

        resp = MagicMock()
        resp.status_code = 500
        resp.headers = {}
        assert _rpc_retry_delay(resp, attempt=0, base_delay=5.0) == 5.0
        assert _rpc_retry_delay(resp, attempt=1, base_delay=5.0) == 10.0

    def test_handles_none_resp(self):
        """Network error (no response) uses exponential backoff."""
        from app.execution import _rpc_retry_delay

        assert _rpc_retry_delay(None, attempt=0, base_delay=5.0) == 5.0
        assert _rpc_retry_delay(None, attempt=1, base_delay=5.0) == 10.0

    def test_invalid_retry_after_falls_back_to_exponential(self):
        """Non-numeric Retry-After header falls back to exponential backoff."""
        from unittest.mock import MagicMock
        from app.execution import _rpc_retry_delay

        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"}
        # Should fall back to base * 2^0 = 5.0
        assert _rpc_retry_delay(resp, attempt=0, base_delay=5.0) == 5.0


# ---------------------------------------------------------------------------
# get_token_decimals tests
# ---------------------------------------------------------------------------


class TestGetTokenDecimals:
    """Tests for get_token_decimals() 429-aware retry behavior."""

    def _make_success_response(self, decimals: int):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "result": {
                "value": {
                    "data": {
                        "parsed": {"info": {"decimals": decimals}}
                    }
                }
            }
        }
        return resp

    def _make_429_response(self, retry_after: str = ""):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": retry_after} if retry_after else {}
        return resp

    @pytest.mark.asyncio
    async def test_429_with_retry_after_retries_then_returns_decimals(self):
        """429 with Retry-After: retries after the specified delay and returns correct decimals."""
        from unittest.mock import AsyncMock, patch
        from app.execution import get_token_decimals

        rate_limited = self._make_429_response(retry_after="4")
        success = self._make_success_response(decimals=6)
        responses = iter([rate_limited, success])

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("app.execution._decimals_cache", {}):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=lambda *a, **kw: next(responses))
            mock_client_cls.return_value = mock_client

            result = await get_token_decimals("FakeMint_429A", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=5.0)

        assert result == 6
        mock_sleep.assert_called_once_with(4.0)

    @pytest.mark.asyncio
    async def test_429_all_retries_exhausted_returns_default(self):
        """429 on every attempt: returns SPL default (9) after exhausting retries."""
        from unittest.mock import AsyncMock, patch
        from app.execution import get_token_decimals, _SPL_DEFAULT_DECIMALS

        rate_limited = self._make_429_response()

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.execution._decimals_cache", {}):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=rate_limited)
            mock_client_cls.return_value = mock_client

            result = await get_token_decimals("FakeMint_429B", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=0.0)

        assert result == _SPL_DEFAULT_DECIMALS

    @pytest.mark.asyncio
    async def test_network_error_retries_then_returns_decimals(self):
        """Network error on first attempt: retries with exponential backoff and succeeds."""
        from unittest.mock import AsyncMock, patch
        from app.execution import get_token_decimals

        success = self._make_success_response(decimals=9)
        call_count = 0

        async def _post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("RPC timeout")
            return success

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("app.execution._decimals_cache", {}):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = _post
            mock_client_cls.return_value = mock_client

            result = await get_token_decimals("FakeMint_429C", rpc_url="https://test-rpc", retries=2, retry_delay_seconds=5.0)

        assert result == 9
        assert call_count == 2
        mock_sleep.assert_called_once_with(5.0)  # base * 2^0 = 5.0

    @pytest.mark.asyncio
    async def test_raises_value_error_when_rpc_url_blank(self):
        from app.execution import get_token_decimals

        with pytest.raises(ValueError, match="rpc_url is required"):
            await get_token_decimals("FakeMint", rpc_url="   ")
