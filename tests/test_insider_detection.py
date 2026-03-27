"""Tests for insider / sniper detection module."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from app.insider_detection import (
    InsiderAnalysis,
    InsiderRisk,
    _extract_creator_from_tx,
    _holder_is_actively_trading,
    analyse_insiders,
)


# ---------------------------------------------------------------------------
# Helpers to build mock RPC responses
# ---------------------------------------------------------------------------

def _make_holder(address: str, ui_amount: float) -> Dict[str, Any]:
    return {"address": address, "uiAmount": ui_amount, "amount": str(int(ui_amount * 1e9))}


def _make_supply(ui_amount: float) -> Dict[str, Any]:
    return {"value": {"uiAmount": ui_amount, "amount": str(int(ui_amount * 1e9)), "decimals": 9}}


def _make_account_info(owner: str) -> Dict[str, Any]:
    return {
        "value": {
            "data": {
                "parsed": {
                    "info": {"owner": owner, "mint": "MINT", "tokenAmount": {}},
                    "type": "account",
                },
                "program": "spl-token",
            }
        }
    }


def _make_signatures(count: int, errors: int = 0) -> List[Dict[str, Any]]:
    sigs = []
    for i in range(count):
        err = {"err": "SomeError"} if i < errors else {"err": None}
        sigs.append({**err, "signature": f"sig_{i}"})
    return sigs


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestExtractCreatorFromTx:
    def test_extracts_fee_payer_dict(self):
        tx = {
            "transaction": {
                "message": {
                    "accountKeys": [
                        {"pubkey": "CREATOR_WALLET", "signer": True},
                        {"pubkey": "OTHER"},
                    ]
                }
            }
        }
        assert _extract_creator_from_tx(tx) == "CREATOR_WALLET"

    def test_extracts_fee_payer_string(self):
        tx = {
            "transaction": {
                "message": {
                    "accountKeys": ["CREATOR_STR", "OTHER"]
                }
            }
        }
        assert _extract_creator_from_tx(tx) == "CREATOR_STR"

    def test_returns_none_on_empty(self):
        assert _extract_creator_from_tx({}) is None
        assert _extract_creator_from_tx({"transaction": {}}) is None
        assert _extract_creator_from_tx({"transaction": {"message": {}}}) is None
        assert _extract_creator_from_tx({"transaction": {"message": {"accountKeys": []}}}) is None


class TestHolderIsActivelyTrading:
    def test_active_trader(self):
        sigs = _make_signatures(8, errors=1)  # 7 successful
        assert _holder_is_actively_trading(sigs) is True

    def test_quiet_holder(self):
        sigs = _make_signatures(3, errors=0)  # 3 successful
        assert _holder_is_actively_trading(sigs) is False

    def test_mostly_errors(self):
        sigs = _make_signatures(10, errors=8)  # only 2 successful
        assert _holder_is_actively_trading(sigs) is False

    def test_empty(self):
        assert _holder_is_actively_trading([]) is False


# ---------------------------------------------------------------------------
# Integration tests for analyse_insiders (mocked RPC)
# ---------------------------------------------------------------------------

def _build_rpc_mock(
    holders: Optional[List[Dict[str, Any]]] = None,
    supply: Optional[Dict[str, Any]] = None,
    first_sig: Optional[List[Dict[str, Any]]] = None,
    tx: Optional[Dict[str, Any]] = None,
    account_infos: Optional[Dict[str, Dict[str, Any]]] = None,
    holder_sigs: Optional[List[List[Dict[str, Any]]]] = None,
):
    """Return an async mock for _rpc_call that routes by method."""
    call_count = {"getAccountInfo": 0, "getSignaturesForAddress_holder": 0}

    async def mock_rpc_call(client, rpc_url, method, params, retries=2):
        if method == "getTokenLargestAccounts":
            if holders is not None:
                return {"value": holders}
            return None
        if method == "getTokenSupply":
            return supply
        if method == "getSignaturesForAddress":
            address = params[0] if params else ""
            opts = params[1] if len(params) > 1 else {}
            # First sig request is for the mint (limit=1000)
            if opts.get("limit") == 1000:
                return first_sig
            # Subsequent are for holders
            idx = call_count["getSignaturesForAddress_holder"]
            call_count["getSignaturesForAddress_holder"] += 1
            if holder_sigs and idx < len(holder_sigs):
                return holder_sigs[idx]
            return []
        if method == "getTransaction":
            return tx
        if method == "getAccountInfo":
            addr = params[0] if params else ""
            idx = call_count["getAccountInfo"]
            call_count["getAccountInfo"] += 1
            if account_infos and addr in account_infos:
                return account_infos[addr]
            return None
        return None

    return mock_rpc_call


class TestAnalyseInsiders:
    @pytest.mark.asyncio
    async def test_clean_token(self):
        """Token with well-distributed holders should be CLEAN."""
        holders = [_make_holder(f"holder_{i}", 100.0) for i in range(10)]
        supply = _make_supply(10000.0)
        # 10 holders × 100 = 1000 / 10000 = 10%
        mock = _build_rpc_mock(
            holders=holders,
            supply=supply,
            first_sig=None,  # no creator detected
            holder_sigs=[_make_signatures(2)] * 5,
        )

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders("MINT_ADDR", rpc_url="https://test-rpc")

        assert result.risk == InsiderRisk.CLEAN
        assert result.top_holder_concentration_pct == pytest.approx(10.0)
        assert result.creator_holding_pct == 0.0
        assert result.dumping_holders == 0

    @pytest.mark.asyncio
    async def test_reject_high_concentration(self):
        """Top holders owning >50% should trigger REJECT."""
        holders = [_make_holder(f"holder_{i}", 600.0) for i in range(10)]
        supply = _make_supply(10000.0)
        # 10 × 600 = 6000 / 10000 = 60%
        mock = _build_rpc_mock(
            holders=holders,
            supply=supply,
            first_sig=None,
            holder_sigs=[_make_signatures(2)] * 5,
        )

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders("MINT_ADDR", rpc_url="https://test-rpc")

        assert result.risk == InsiderRisk.REJECT
        assert result.top_holder_concentration_pct == pytest.approx(60.0)
        assert "top-10 hold 60.0%" in result.summary

    @pytest.mark.asyncio
    async def test_reject_creator_holding(self):
        """Creator holding >30% should trigger REJECT."""
        holders = [
            _make_holder("creator_token_acct", 3500.0),
            *[_make_holder(f"holder_{i}", 50.0) for i in range(9)],
        ]
        supply = _make_supply(10000.0)
        # creator: 3500/10000 = 35%, total top-10: (3500+450)/10000 = 39.5%

        first_sig = [{"signature": "creation_sig"}]
        tx = {
            "transaction": {
                "message": {
                    "accountKeys": [{"pubkey": "CREATOR_WALLET"}]
                }
            }
        }
        # Map creator_token_acct to CREATOR_WALLET
        account_infos = {
            "creator_token_acct": _make_account_info("CREATOR_WALLET"),
        }
        # Other holders have different owners
        for i in range(9):
            account_infos[f"holder_{i}"] = _make_account_info(f"wallet_{i}")

        mock = _build_rpc_mock(
            holders=holders,
            supply=supply,
            first_sig=first_sig,
            tx=tx,
            account_infos=account_infos,
            holder_sigs=[_make_signatures(2)] * 5,
        )

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders("MINT_ADDR", rpc_url="https://test-rpc")

        assert result.risk == InsiderRisk.REJECT
        assert result.creator_holding_pct == pytest.approx(35.0)
        assert result.creator_address == "CREATOR_WALLET"

    @pytest.mark.asyncio
    async def test_warn_concentration(self):
        """Concentration between 30-50% should be WARN."""
        holders = [_make_holder(f"holder_{i}", 350.0) for i in range(10)]
        supply = _make_supply(10000.0)
        # 10 × 350 = 3500 / 10000 = 35%
        mock = _build_rpc_mock(
            holders=holders,
            supply=supply,
            first_sig=None,
            holder_sigs=[_make_signatures(2)] * 5,
        )

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders("MINT_ADDR", rpc_url="https://test-rpc")

        assert result.risk == InsiderRisk.WARN
        assert 30.0 < result.top_holder_concentration_pct < 50.0

    @pytest.mark.asyncio
    async def test_warn_active_dumping(self):
        """Many active top holders should trigger WARN."""
        holders = [_make_holder(f"holder_{i}", 100.0) for i in range(10)]
        supply = _make_supply(10000.0)
        # 10% concentration — below warn, but 4/5 dumping
        mock = _build_rpc_mock(
            holders=holders,
            supply=supply,
            first_sig=None,
            holder_sigs=[_make_signatures(8)] * 5,  # 8 successful txs → active
        )

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders("MINT_ADDR", rpc_url="https://test-rpc")

        assert result.risk == InsiderRisk.WARN
        assert result.dumping_holders >= 3

    @pytest.mark.asyncio
    async def test_insufficient_data_returns_clean(self):
        """Missing RPC data should return CLEAN with errors (fail-open)."""
        mock = _build_rpc_mock(holders=None, supply=None)

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders("MINT_ADDR", rpc_url="https://test-rpc")

        assert result.risk == InsiderRisk.CLEAN
        assert len(result.errors) > 0
        assert "Insufficient" in result.summary

    @pytest.mark.asyncio
    async def test_custom_thresholds(self):
        """Custom thresholds should be respected."""
        holders = [_make_holder(f"holder_{i}", 250.0) for i in range(10)]
        supply = _make_supply(10000.0)
        # 25% concentration — below default 30% warn, but above custom 20%
        mock = _build_rpc_mock(
            holders=holders,
            supply=supply,
            first_sig=None,
            holder_sigs=[_make_signatures(2)] * 5,
        )

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders(
                "MINT_ADDR",
                rpc_url="https://test-rpc",
                warn_concentration_pct=20.0,
            )

        assert result.risk == InsiderRisk.WARN

    @pytest.mark.asyncio
    async def test_zero_supply_returns_clean(self):
        """Zero supply should fail gracefully."""
        holders = [_make_holder("h1", 100.0)]
        supply = _make_supply(0.0)
        mock = _build_rpc_mock(holders=holders, supply=supply)

        with patch("app.insider_detection._rpc_call", side_effect=mock):
            result = await analyse_insiders("MINT_ADDR", rpc_url="https://test-rpc")

        assert result.risk == InsiderRisk.CLEAN
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_raises_value_error_when_rpc_url_blank(self):
        with pytest.raises(ValueError, match="rpc_url is required"):
            await analyse_insiders("MINT_ADDR", rpc_url="   ")
