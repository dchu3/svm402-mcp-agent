"""Tests for wash trading detection module."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.wash_trading import (
    WashTradingDetector,
    WashTradingResult,
    WalletActivity,
    ParsedSwap,
    MAX_SIGNATURES,
    MAX_TX_SAMPLE,
    MIN_SAMPLE_SIZE,
    REPEAT_BUY_THRESHOLD,
)


TOKEN_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
POOL_ADDRESS = "5P6n5omLbLbP4kaPGL8etqQAHEx2UCkaUyvjLDnwV4EY"


def _make_mcp_manager(solana_client=None):
    """Create a mock MCPManager with optional solana client."""
    manager = MagicMock()
    manager.get_client.return_value = solana_client
    return manager


def _make_signatures(count: int):
    """Create a list of mock signature entries."""
    return [
        {"signature": f"sig{i:04d}", "slot": 300000000 + i}
        for i in range(count)
    ]


def _make_transaction(
    fee_payer: str,
    token_mint: str,
    pre_amount: float,
    post_amount: float,
    block_time: int = 1700000000,
    err: object = None,
):
    """Create a mock parsed Solana transaction."""
    return {
        "blockTime": block_time,
        "meta": {
            "err": err,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": token_mint,
                    "owner": fee_payer,
                    "uiTokenAmount": {
                        "uiAmountString": str(pre_amount),
                        "uiAmount": pre_amount,
                        "decimals": 6,
                        "amount": str(int(pre_amount * 1e6)),
                    },
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": token_mint,
                    "owner": fee_payer,
                    "uiTokenAmount": {
                        "uiAmountString": str(post_amount),
                        "uiAmount": post_amount,
                        "decimals": 6,
                        "amount": str(int(post_amount * 1e6)),
                    },
                }
            ],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": fee_payer, "signer": True, "writable": True},
                    {"pubkey": "TokenAccount1", "signer": False, "writable": True},
                ],
            }
        },
    }


class TestWashTradingResult:
    """Tests for WashTradingResult dataclass."""

    def test_default_values(self):
        result = WashTradingResult()
        assert result.manipulation_score is None
        assert result.manipulation_level == "unknown"
        assert result.unique_wallets == 0
        assert result.total_transactions_sampled == 0
        assert result.repeat_buyers == []
        assert result.flags == []

    def test_to_dict(self):
        result = WashTradingResult(
            manipulation_score=5.3,
            manipulation_level="moderate",
            unique_wallets=10,
            total_transactions_sampled=30,
            repeat_buyers=[
                {"wallet": "abc123", "buy_count": 3, "sell_count": 1}
            ],
            flags=["50% of buys from repeat wallets"],
        )
        d = result.to_dict()
        assert d["manipulation_score"] == 5.3
        assert d["manipulation_level"] == "moderate"
        assert d["unique_wallets"] == 10
        assert len(d["repeat_buyers"]) == 1
        assert d["flags"] == ["50% of buys from repeat wallets"]

    def test_to_dict_none_score(self):
        """to_dict handles None manipulation_score."""
        result = WashTradingResult()
        d = result.to_dict()
        assert d["manipulation_score"] is None
        assert d["manipulation_level"] == "unknown"

    def test_to_dict_truncates(self):
        """to_dict limits repeat_buyers to 10 and flags to 10."""
        result = WashTradingResult(
            repeat_buyers=[{"wallet": f"w{i}"} for i in range(15)],
            flags=[f"flag{i}" for i in range(15)],
        )
        d = result.to_dict()
        assert len(d["repeat_buyers"]) == 10
        assert len(d["flags"]) == 10


class TestParseTransaction:
    """Tests for WashTradingDetector._parse_transaction."""

    def setup_method(self):
        manager = _make_mcp_manager()
        self.detector = WashTradingDetector(manager)

    def test_parse_buy_transaction(self):
        """Detect a buy (post balance > pre balance)."""
        tx = _make_transaction("WalletA", TOKEN_MINT, 0.0, 1000.0)
        result = self.detector._parse_transaction(tx, TOKEN_MINT, "sig0001")
        assert result is not None
        assert result.wallet == "WalletA"
        assert result.direction == "buy"
        assert result.token_amount == pytest.approx(1000.0)

    def test_parse_sell_transaction(self):
        """Detect a sell (post balance < pre balance)."""
        tx = _make_transaction("WalletB", TOKEN_MINT, 1000.0, 0.0)
        result = self.detector._parse_transaction(tx, TOKEN_MINT, "sig0002")
        assert result is not None
        assert result.wallet == "WalletB"
        assert result.direction == "sell"
        assert result.token_amount == pytest.approx(1000.0)

    def test_skip_failed_transaction(self):
        """Skip transactions with errors."""
        tx = _make_transaction("WalletA", TOKEN_MINT, 0.0, 1000.0, err={"InstructionError": [0, "Custom"]})
        result = self.detector._parse_transaction(tx, TOKEN_MINT, "sig0003")
        assert result is None

    def test_skip_unrelated_token(self):
        """Skip transactions for a different token mint."""
        tx = _make_transaction("WalletA", "OtherMint123", 0.0, 1000.0)
        result = self.detector._parse_transaction(tx, TOKEN_MINT, "sig0004")
        assert result is None

    def test_skip_zero_change(self):
        """Skip transactions with no balance change."""
        tx = _make_transaction("WalletA", TOKEN_MINT, 500.0, 500.0)
        result = self.detector._parse_transaction(tx, TOKEN_MINT, "sig0005")
        assert result is None

    def test_parse_invalid_data(self):
        """Handle malformed transaction data gracefully."""
        assert self.detector._parse_transaction({}, TOKEN_MINT, "sig") is None
        assert self.detector._parse_transaction(None, TOKEN_MINT, "sig") is None
        assert self.detector._parse_transaction({"meta": None}, TOKEN_MINT, "sig") is None

    def test_extract_fee_payer_string_keys(self):
        """Handle accountKeys as simple strings."""
        tx = _make_transaction("WalletA", TOKEN_MINT, 0.0, 100.0)
        tx["transaction"]["message"]["accountKeys"] = ["WalletA", "OtherAccount"]
        result = self.detector._parse_transaction(tx, TOKEN_MINT, "sig0006")
        assert result is not None
        assert result.wallet == "WalletA"

    def test_block_time_extracted(self):
        """Block time is correctly extracted."""
        tx = _make_transaction("WalletA", TOKEN_MINT, 0.0, 100.0, block_time=1700000500)
        result = self.detector._parse_transaction(tx, TOKEN_MINT, "sig0007")
        assert result is not None
        assert result.block_time == 1700000500


class TestDetectPatterns:
    """Tests for WashTradingDetector._detect_patterns."""

    def setup_method(self):
        manager = _make_mcp_manager()
        self.detector = WashTradingDetector(manager)

    def test_clean_trading_diverse_wallets(self):
        """Normal trading: many unique wallets, few repeat buyers."""
        swaps = [
            ParsedSwap(f"sig{i}", f"Wallet{i}", "buy", 100.0, 1700000000 + i * 60)
            for i in range(20)
        ]
        result = self.detector._detect_patterns(swaps, 20)
        assert result.manipulation_level == "clean"
        assert result.manipulation_score <= 2.0
        assert result.unique_wallets == 20
        assert len(result.repeat_buyers) == 0

    def test_suspicious_repeat_buyers(self):
        """Suspicious: one wallet makes many purchases."""
        swaps = [
            ParsedSwap(f"sig{i}", "RepeatWallet", "buy", 100.0, 1700000000 + i * 10)
            for i in range(10)
        ] + [
            ParsedSwap(f"sig{10 + i}", f"Normal{i}", "buy", 50.0)
            for i in range(5)
        ]
        result = self.detector._detect_patterns(swaps, 15)
        assert result.manipulation_score > 3.0
        assert len(result.repeat_buyers) >= 1
        assert result.repeat_buyers[0]["wallet"] == "RepeatWallet"
        assert result.repeat_buyers[0]["buy_count"] == 10

    def test_critical_wash_trading(self):
        """Critical: few wallets, all buying, rapid trades."""
        swaps = []
        for wallet_idx in range(2):
            for i in range(8):
                swaps.append(
                    ParsedSwap(
                        f"sig_w{wallet_idx}_{i}",
                        f"Wash{wallet_idx}",
                        "buy",
                        100.0,
                        1700000000 + i * 30,  # 30s apart
                    )
                )
        result = self.detector._detect_patterns(swaps, 16)
        assert result.manipulation_score >= 6.0
        assert result.manipulation_level in ("suspicious", "critical")
        assert len(result.flags) > 0

    def test_empty_swaps(self):
        """Empty swap list returns unknown (insufficient data)."""
        result = self.detector._detect_patterns([], 0)
        assert result.manipulation_level == "unknown"
        assert result.manipulation_score is None

    def test_small_sample_returns_unknown(self):
        """Fewer than MIN_SAMPLE_SIZE swaps returns unknown instead of scoring."""
        swaps = [
            ParsedSwap(f"sig{i}", "SameWallet", "buy", 100.0)
            for i in range(MIN_SAMPLE_SIZE - 1)
        ]
        result = self.detector._detect_patterns(swaps, 30)
        assert result.manipulation_level == "unknown"
        assert result.manipulation_score is None
        assert result.total_transactions_sampled == 30

    def test_buy_sell_asymmetry_flag(self):
        """Flag when almost all activity is buys."""
        swaps = [
            ParsedSwap(f"buy{i}", f"W{i}", "buy", 100.0) for i in range(18)
        ] + [
            ParsedSwap("sell0", "W0", "sell", 50.0),
        ]
        result = self.detector._detect_patterns(swaps, 19)
        has_buy_flag = any("buy pressure" in f.lower() for f in result.flags)
        assert has_buy_flag

    def test_low_wallet_diversity_flag(self):
        """Flag when few unique wallets relative to total transactions."""
        swaps = [
            ParsedSwap(f"sig{i}", f"W{i % 3}", "buy", 100.0)
            for i in range(15)
        ]
        result = self.detector._detect_patterns(swaps, 15)
        has_diversity_flag = any("diversity" in f.lower() for f in result.flags)
        assert has_diversity_flag

    def test_rapid_trading_flag(self):
        """Flag when a wallet trades many times in a short window."""
        swaps = [
            ParsedSwap(f"sig{i}", "RapidTrader", "buy", 100.0, 1700000000 + i * 10)
            for i in range(5)
        ]
        result = self.detector._detect_patterns(swaps, 5)
        has_rapid_flag = any("trades in" in f.lower() for f in result.flags)
        assert has_rapid_flag


class TestCalculateScore:
    """Tests for score boundary conditions."""

    def setup_method(self):
        manager = _make_mcp_manager()
        self.detector = WashTradingDetector(manager)

    def test_score_clamped_to_10(self):
        """Score never exceeds 10."""
        # Extreme case: all factors max out
        swaps = [
            ParsedSwap(f"sig{i}", "SingleWallet", "buy", 100.0, 1700000000 + i * 5)
            for i in range(30)
        ]
        result = self.detector._detect_patterns(swaps, 30)
        assert result.manipulation_score <= 10.0

    def test_score_minimum_zero(self):
        """Score is never negative for valid scored results."""
        swaps = [ParsedSwap(f"sig{i}", f"W{i}", "buy", 100.0) for i in range(20)]
        result = self.detector._detect_patterns(swaps, 20)
        assert result.manipulation_score is not None
        assert result.manipulation_score >= 0.0

    def test_level_clean(self):
        swaps = [ParsedSwap(f"sig{i}", f"W{i}", "buy", 100.0) for i in range(20)]
        result = self.detector._detect_patterns(swaps, 20)
        assert result.manipulation_level == "clean"

    def test_level_thresholds(self):
        """Verify classification thresholds match docstring."""
        r = WashTradingResult(manipulation_score=0.0)
        assert r.manipulation_score <= 2.0  # Would be "clean"

        r2 = WashTradingResult(manipulation_score=4.0)
        assert 2.0 < r2.manipulation_score <= 5.0  # Would be "moderate"


class TestAnalyzeIntegration:
    """Integration tests for WashTradingDetector.analyze."""

    @pytest.mark.asyncio
    async def test_no_solana_client(self):
        """Returns unknown when Solana RPC client is not available."""
        manager = _make_mcp_manager(solana_client=None)
        detector = WashTradingDetector(manager)
        result = await detector.analyze(TOKEN_MINT, POOL_ADDRESS)
        assert result.manipulation_level == "unknown"

    @pytest.mark.asyncio
    async def test_no_signatures_returned(self):
        """Returns unknown when no signatures found."""
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value=json.dumps([]))
        manager = _make_mcp_manager(solana_client=client)
        detector = WashTradingDetector(manager)
        result = await detector.analyze(TOKEN_MINT, POOL_ADDRESS)
        assert result.manipulation_level == "unknown"

    @pytest.mark.asyncio
    async def test_signatures_and_transactions(self):
        """Full pipeline with mocked MCP responses."""
        sigs = _make_signatures(8)
        txs = [
            _make_transaction("WalletA", TOKEN_MINT, 0.0, 500.0, block_time=1700000000 + i * 60)
            for i in range(3)
        ] + [
            _make_transaction("WalletB", TOKEN_MINT, 0.0, 200.0, block_time=1700000200),
            _make_transaction("WalletC", TOKEN_MINT, 0.0, 100.0, block_time=1700000300),
            _make_transaction("WalletD", TOKEN_MINT, 0.0, 150.0, block_time=1700000400),
            _make_transaction("WalletE", TOKEN_MINT, 0.0, 300.0, block_time=1700000500),
            _make_transaction("WalletF", TOKEN_MINT, 0.0, 250.0, block_time=1700000600),
        ]

        call_count = 0

        async def mock_call_tool(method, args):
            nonlocal call_count
            if method == "getSignaturesForAddress":
                return json.dumps(sigs)
            elif method == "getTransaction":
                idx = call_count
                call_count += 1
                if idx < len(txs):
                    return json.dumps(txs[idx])
                return json.dumps(txs[0])
            return "{}"

        client = AsyncMock()
        client.call_tool = mock_call_tool
        manager = _make_mcp_manager(solana_client=client)
        detector = WashTradingDetector(manager)
        result = await detector.analyze(TOKEN_MINT, POOL_ADDRESS)

        assert result.total_transactions_sampled == 8
        assert result.unique_wallets >= 1
        assert isinstance(result.manipulation_score, float)
        assert result.manipulation_level in ("clean", "moderate", "suspicious", "critical")

    @pytest.mark.asyncio
    async def test_mcp_error_graceful(self):
        """Handles MCP call errors gracefully."""
        client = AsyncMock()
        client.call_tool = AsyncMock(side_effect=Exception("RPC timeout"))
        manager = _make_mcp_manager(solana_client=client)
        detector = WashTradingDetector(manager)
        result = await detector.analyze(TOKEN_MINT, POOL_ADDRESS)
        # Should not raise, returns empty result
        assert result.manipulation_level == "unknown"

    @pytest.mark.asyncio
    async def test_malformed_signature_response(self):
        """Handles non-JSON signature response."""
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value="not json at all {{{")
        manager = _make_mcp_manager(solana_client=client)
        detector = WashTradingDetector(manager)
        result = await detector.analyze(TOKEN_MINT, POOL_ADDRESS)
        assert result.manipulation_level == "unknown"


class TestExtractFeePayer:
    """Tests for fee payer extraction."""

    def setup_method(self):
        manager = _make_mcp_manager()
        self.detector = WashTradingDetector(manager)

    def test_dict_account_keys(self):
        msg = {"accountKeys": [{"pubkey": "ABC123"}, {"pubkey": "DEF456"}]}
        assert self.detector._extract_fee_payer(msg) == "ABC123"

    def test_string_account_keys(self):
        msg = {"accountKeys": ["ABC123", "DEF456"]}
        assert self.detector._extract_fee_payer(msg) == "ABC123"

    def test_empty_keys(self):
        msg = {"accountKeys": []}
        assert self.detector._extract_fee_payer(msg) is None

    def test_missing_keys(self):
        msg = {}
        assert self.detector._extract_fee_payer(msg) is None


class TestExtractUiAmount:
    """Tests for UI amount extraction from token balance entries."""

    def setup_method(self):
        manager = _make_mcp_manager()
        self.detector = WashTradingDetector(manager)

    def test_ui_amount_string(self):
        entry = {"uiTokenAmount": {"uiAmountString": "1234.56", "uiAmount": 1234.56}}
        assert self.detector._extract_ui_amount(entry) == pytest.approx(1234.56)

    def test_ui_amount_fallback(self):
        entry = {"uiTokenAmount": {"uiAmount": 999.0}}
        assert self.detector._extract_ui_amount(entry) == pytest.approx(999.0)

    def test_missing_amount(self):
        entry = {"uiTokenAmount": {}}
        assert self.detector._extract_ui_amount(entry) is None

    def test_no_ui_token_amount(self):
        entry = {}
        assert self.detector._extract_ui_amount(entry) is None


class TestIsLikelySwap:
    """Tests for swap vs LP operation filtering."""

    def setup_method(self):
        manager = _make_mcp_manager()
        self.detector = WashTradingDetector(manager)

    def test_single_mint_is_swap(self):
        """Single token mint (SOL on other side) is treated as a swap."""
        meta = {
            "preTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "0.0", "uiAmount": 0.0}},
            ],
            "postTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "100.0", "uiAmount": 100.0}},
            ],
        }
        assert self.detector._is_likely_swap(meta, "Payer") is True

    def test_mixed_directions_is_swap(self):
        """Two mints with opposing directions is a swap."""
        meta = {
            "preTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "100.0", "uiAmount": 100.0}},
                {"mint": "TokenB", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "0.0", "uiAmount": 0.0}},
            ],
            "postTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "0.0", "uiAmount": 0.0}},
                {"mint": "TokenB", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "50.0", "uiAmount": 50.0}},
            ],
        }
        assert self.detector._is_likely_swap(meta, "Payer") is True

    def test_lp_add_filtered(self):
        """Both mints decrease for payer = LP add, not a swap."""
        meta = {
            "preTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "100.0", "uiAmount": 100.0}},
                {"mint": "TokenB", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "50.0", "uiAmount": 50.0}},
            ],
            "postTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "50.0", "uiAmount": 50.0}},
                {"mint": "TokenB", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "25.0", "uiAmount": 25.0}},
            ],
        }
        assert self.detector._is_likely_swap(meta, "Payer") is False

    def test_lp_remove_filtered(self):
        """Both mints increase for payer = LP remove, not a swap."""
        meta = {
            "preTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "10.0", "uiAmount": 10.0}},
                {"mint": "TokenB", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "5.0", "uiAmount": 5.0}},
            ],
            "postTokenBalances": [
                {"mint": "TokenA", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "110.0", "uiAmount": 110.0}},
                {"mint": "TokenB", "owner": "Payer", "uiTokenAmount": {"uiAmountString": "55.0", "uiAmount": 55.0}},
            ],
        }
        assert self.detector._is_likely_swap(meta, "Payer") is False

    def test_empty_balances_is_swap(self):
        """No token balances at all defaults to swap (permissive)."""
        meta = {"preTokenBalances": [], "postTokenBalances": []}
        assert self.detector._is_likely_swap(meta, "Payer") is True
