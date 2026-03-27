"""Tests for MCP Manager configuration."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_client import MCPClient, MCPManager, MCPTimeoutError


def test_mcp_manager_basic_init():
    """Test MCPManager initializes with core clients."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
    )
    
    assert manager.dexscreener is not None
    assert manager.dexpaprika is not None


def test_mcp_manager_get_client_returns_none_for_unknown():
    """Test get_client returns None for unknown client names."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
    )
    
    assert manager.get_client("honeypot") is None
    assert manager.get_client("blockscout") is None
    assert manager.get_client("nonexistent") is None


def test_format_tools_for_system_prompt_with_tools():
    """Test format_tools_for_system_prompt generates correct output with tools."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
    )
    
    # Simulate tools being loaded
    manager.dexscreener._tools = [
        {
            "name": "search_pairs",
            "description": "Search for token pairs by query",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "get_token_info",
            "description": "Get token information",
            "inputSchema": {
                "type": "object",
                "properties": {"address": {"type": "string"}},
                "required": [],
            },
        },
    ]
    manager.dexpaprika._tools = []
    
    result = manager.format_tools_for_system_prompt()
    
    assert "### dexscreener tools:" in result
    assert "dexscreener_search_pairs" in result
    assert "[REQUIRED: query:string]" in result
    assert "dexscreener_get_token_info" in result
    # get_token_info has no required params, so no [REQUIRED: ...] tag
    assert "- dexscreener_get_token_info: Get token information" in result


def test_format_tools_for_system_prompt_empty_tools():
    """Test format_tools_for_system_prompt returns empty string when no tools."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
    )
    
    # No tools loaded
    manager.dexscreener._tools = []
    manager.dexpaprika._tools = []
    
    result = manager.format_tools_for_system_prompt()
    
    assert result == ""


def test_format_tools_for_system_prompt_description_truncation():
    """Test that long descriptions are truncated at word boundaries."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
    )
    
    long_description = "This is a very long description that should be truncated at a word boundary to avoid cutting words in half"
    manager.dexscreener._tools = [
        {
            "name": "testTool",
            "description": long_description,
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
    ]
    manager.dexpaprika._tools = []
    
    result = manager.format_tools_for_system_prompt()
    
    # Should be truncated and end with ...
    assert "..." in result
    # Should not contain the full description
    assert long_description not in result
    # Should not cut mid-word
    assert "bounda..." not in result


def test_truncate_description_short():
    """Test _truncate_description returns short descriptions unchanged."""
    result = MCPManager._truncate_description("Short description", max_length=100)
    assert result == "Short description"


def test_truncate_description_at_word_boundary():
    """Test _truncate_description truncates at word boundary."""
    desc = "This is a test description that is longer than the maximum allowed length"
    result = MCPManager._truncate_description(desc, max_length=30)
    
    assert result.endswith("...")
    assert len(result) <= 33  # 30 + "..."
    # Should break at word boundary
    assert result in ["This is a test description...", "This is a test..."]


def test_mcp_manager_with_trader():
    """Test MCPManager initializes trader client when cmd is provided."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        trader_cmd="echo trader",
    )

    assert manager.trader is not None
    assert manager.trader.name == "trader"


def test_mcp_manager_forwards_trader_extra_env():
    """MCPManager passes trader_env through to trader client extra_env."""
    trader_env = {
        "SOLANA_PRIVATE_KEY": "test-private-key",
        "SOLANA_RPC_URL": "https://rpc.example",
        "JUPITER_API_BASE": "https://api.jup.ag/swap/v1",
        "JUPITER_API_KEY": "test-jupiter-key",
    }
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        trader_cmd="echo trader",
        trader_env=trader_env,
    )

    assert manager.trader is not None
    assert manager.trader._extra_env == trader_env


def test_mcp_manager_without_trader():
    """Test MCPManager skips trader client when cmd is empty."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        trader_cmd="",
    )

    assert manager.trader is None


def test_mcp_manager_get_client_trader():
    """Test get_client returns trader when configured."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        trader_cmd="echo trader",
    )

    client = manager.get_client("trader")
    assert client is not None
    assert client.name == "trader"


def test_mcp_manager_get_client_without_trader():
    """Test get_client returns None when trader is not configured."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        trader_cmd="",
    )

    client = manager.get_client("trader")
    assert client is None


# ---------------------------------------------------------------------------
# get_gemini_functions_for — filtered tool getter
# ---------------------------------------------------------------------------


def _manager_with_tools() -> MCPManager:
    """Helper: manager with simulated tool schemas on dexscreener and rugcheck."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        rugcheck_cmd="echo rugcheck",
        trader_cmd="echo trader",
    )
    manager.dexscreener._tools = [
        {
            "name": "search_pairs",
            "description": "Search pairs",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        }
    ]
    manager.dexpaprika._tools = [
        {
            "name": "get_pool",
            "description": "Get pool",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }
    ]
    manager.rugcheck._tools = [
        {
            "name": "get_token_summary",
            "description": "Token safety",
            "inputSchema": {"type": "object", "properties": {"token_address": {"type": "string"}}, "required": ["token_address"]},
        }
    ]
    manager.trader._tools = [
        {
            "name": "execute_trade",
            "description": "Execute trade",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }
    ]
    return manager


def test_get_gemini_functions_for_returns_only_requested_clients():
    """Only tools from the named clients are returned."""
    manager = _manager_with_tools()
    functions = manager.get_gemini_functions_for(["dexscreener", "rugcheck"])
    names = [f.name for f in functions]
    assert "dexscreener_search_pairs" in names
    assert "rugcheck_get_token_summary" in names
    # dexpaprika and trader must be excluded
    assert not any("dexpaprika" in n for n in names)
    assert not any("trader" in n for n in names)


def test_get_gemini_functions_for_unknown_name_skipped():
    """Unknown client names are silently ignored."""
    manager = _manager_with_tools()
    functions = manager.get_gemini_functions_for(["dexscreener", "nonexistent_client"])
    names = [f.name for f in functions]
    assert "dexscreener_search_pairs" in names
    assert len(names) == 1


def test_get_gemini_functions_for_empty_list_returns_empty():
    """Empty client list returns no functions."""
    manager = _manager_with_tools()
    assert manager.get_gemini_functions_for([]) == []


def test_get_gemini_functions_for_skips_unconfigured_optional_client():
    """Requesting an optional client that was not configured returns nothing for it."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        rugcheck_cmd="",  # not configured
    )
    manager.dexscreener._tools = [
        {
            "name": "search_pairs",
            "description": "Search pairs",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }
    ]
    functions = manager.get_gemini_functions_for(["dexscreener", "rugcheck"])
    names = [f.name for f in functions]
    assert "dexscreener_search_pairs" in names
    assert not any("rugcheck" in n for n in names)


# ---------------------------------------------------------------------------
# MCPClient.call_tool — timeout, retry, and call_timeout tests
# ---------------------------------------------------------------------------

def _make_client(call_timeout: float = 90.0, retry_on_timeout: bool = True) -> MCPClient:
    """Return an MCPClient with a fake command that won't actually be spawned."""
    return MCPClient("test", "echo test", call_timeout=call_timeout, retry_on_timeout=retry_on_timeout)


def test_mcp_client_stores_call_timeout():
    """call_timeout is stored and defaults to 90.0."""
    c_default = MCPClient("test", "echo test")
    assert c_default._call_timeout == 90.0

    c_custom = _make_client(call_timeout=120.0)
    assert c_custom._call_timeout == 120.0


def test_mcp_client_retry_on_timeout_defaults_true():
    """retry_on_timeout defaults to True."""
    client = MCPClient("test", "echo test")
    assert client._retry_on_timeout is True


def test_mcp_client_retry_on_timeout_false():
    """retry_on_timeout=False is stored correctly."""
    client = _make_client(retry_on_timeout=False)
    assert client._retry_on_timeout is False


def test_mcp_client_has_restart_lock():
    """MCPClient has a _restart_lock for concurrency-safe restarts."""
    import asyncio
    client = _make_client()
    assert isinstance(client._restart_lock, type(asyncio.Lock()))


@pytest.mark.asyncio
async def test_call_tool_success_on_first_attempt():
    """call_tool returns the result when _call_tool_once succeeds immediately."""
    client = _make_client()
    expected = {"token": "data"}

    with patch.object(client, "_call_tool_once", new=AsyncMock(return_value=expected)):
        result = await client.call_tool("my_method", {"arg": 1})

    assert result == expected


@pytest.mark.asyncio
async def test_call_tool_non_timeout_error_propagates_without_retry():
    """Non-timeout RuntimeErrors are re-raised immediately; stop/start not called."""
    client = _make_client()

    with (
        patch.object(client, "_call_tool_once", new=AsyncMock(side_effect=RuntimeError("some other error"))),
        patch.object(client, "stop", new=AsyncMock()) as mock_stop,
        patch.object(client, "start", new=AsyncMock()) as mock_start,
    ):
        with pytest.raises(RuntimeError, match="some other error"):
            await client.call_tool("method", {})

    mock_stop.assert_not_called()
    mock_start.assert_not_called()


@pytest.mark.asyncio
async def test_call_tool_retries_after_timeout_and_succeeds():
    """On timeout, stop+start are called and the retry succeeds."""
    client = _make_client()
    timeout_error = MCPTimeoutError("MCP request timed out: tools/call (test: echo test)")
    expected = {"ok": True}

    call_once = AsyncMock(side_effect=[timeout_error, expected])

    with (
        patch.object(client, "_call_tool_once", new=call_once),
        patch.object(client, "stop", new=AsyncMock()) as mock_stop,
        patch.object(client, "start", new=AsyncMock()) as mock_start,
    ):
        result = await client.call_tool("method", {})

    assert result == expected
    assert call_once.call_count == 2
    mock_stop.assert_called_once()
    mock_start.assert_called_once()


@pytest.mark.asyncio
async def test_call_tool_retry_also_times_out_raises():
    """If the retry also times out, the error is propagated and no further retry occurs."""
    client = _make_client()
    timeout_error = MCPTimeoutError("MCP request timed out: tools/call (test: echo test)")

    call_once = AsyncMock(side_effect=[timeout_error, timeout_error])

    with (
        patch.object(client, "_call_tool_once", new=call_once),
        patch.object(client, "stop", new=AsyncMock()),
        patch.object(client, "start", new=AsyncMock()),
    ):
        with pytest.raises(MCPTimeoutError):
            await client.call_tool("method", {})

    assert call_once.call_count == 2


@pytest.mark.asyncio
async def test_call_tool_no_retry_on_timeout_false():
    """With retry_on_timeout=False, process is restarted but error is re-raised without retry."""
    client = _make_client(retry_on_timeout=False)
    timeout_error = MCPTimeoutError("MCP request timed out: tools/call (test: echo test)")

    call_once = AsyncMock(side_effect=[timeout_error])

    with (
        patch.object(client, "_call_tool_once", new=call_once),
        patch.object(client, "stop", new=AsyncMock()) as mock_stop,
        patch.object(client, "start", new=AsyncMock()) as mock_start,
    ):
        with pytest.raises(MCPTimeoutError):
            await client.call_tool("method", {})

    # Restart happens to clean up the stuck process, but no retry
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
    assert call_once.call_count == 1


@pytest.mark.asyncio
async def test_call_tool_logs_warning_on_timeout(caplog):
    """A warning is logged when a timeout triggers the restart-retry path."""
    import logging
    client = _make_client()
    timeout_error = MCPTimeoutError("MCP request timed out: tools/call (test: echo test)")
    expected = {"ok": True}

    with (
        patch.object(client, "_call_tool_once", new=AsyncMock(side_effect=[timeout_error, expected])),
        patch.object(client, "stop", new=AsyncMock()),
        patch.object(client, "start", new=AsyncMock()),
        caplog.at_level(logging.WARNING, logger="app.mcp_client"),
    ):
        await client.call_tool("method", {})

    assert any("timed out" in r.message.lower() for r in caplog.records)


def test_mcp_manager_call_timeout_applied_to_all_clients():
    """MCPManager propagates call_timeout to all configured clients."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        rugcheck_cmd="echo rugcheck",
        solana_rpc_cmd="echo solana",
        trader_cmd="echo trader",
        call_timeout=120.0,
    )

    clients = [
        manager.dexscreener,
        manager.dexpaprika,
        manager.rugcheck,
        manager.solana,
        manager.trader,
    ]
    for client in clients:
        assert client is not None
        assert client._call_timeout == 120.0


@pytest.mark.asyncio
async def test_mcp_client_start_passes_merged_extra_env():
    client = MCPClient(
        "solana",
        "echo solana",
        extra_env={"SOLANA_RPC_URL": "https://rpc.example"},
    )

    process = AsyncMock()
    process.returncode = None
    process.stdout = type("S", (), {"_limit": 0})()
    process.stderr = type("S", (), {"_limit": 0})()

    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as mock_spawn,
        patch.object(client, "_ensure_initialized", new=AsyncMock()),
        patch("asyncio.create_task") as mock_create_task,
    ):
        def _consume_coro(coro):
            coro.close()
            return AsyncMock()

        mock_create_task.side_effect = _consume_coro
        await client.start()

    kwargs = mock_spawn.call_args.kwargs
    assert "env" in kwargs
    assert kwargs["env"] is not None
    assert kwargs["env"]["SOLANA_RPC_URL"] == "https://rpc.example"


def test_mcp_manager_trader_retry_on_timeout_disabled():
    """MCPManager creates the trader client with retry_on_timeout=False to prevent double trades."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        trader_cmd="echo trader",
    )
    assert manager.trader is not None
    assert manager.trader._retry_on_timeout is False


def test_mcp_manager_non_trader_clients_retry_on_timeout_enabled():
    """Non-trader MCP clients retain the default retry_on_timeout=True."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        rugcheck_cmd="echo rugcheck",
    )
    assert manager.dexscreener._retry_on_timeout is True
    assert manager.dexpaprika._retry_on_timeout is True
    assert manager.rugcheck is not None
    assert manager.rugcheck._retry_on_timeout is True


# ---------------------------------------------------------------------------
# MCPClient._call_semaphore — concurrency limiting
# ---------------------------------------------------------------------------


def test_mcp_client_default_call_semaphore():
    """MCPClient has a _call_semaphore with default limit of 8."""
    client = MCPClient("test", "echo test")
    assert isinstance(client._call_semaphore, asyncio.Semaphore)
    # Semaphore internal value reflects the limit
    assert client._call_semaphore._value == 8


def test_mcp_client_custom_max_concurrent():
    """MCPClient accepts a custom max_concurrent parameter."""
    client = MCPClient("test", "echo test", max_concurrent=4)
    assert client._call_semaphore._value == 4


def test_mcp_manager_propagates_max_concurrent():
    """MCPManager propagates max_concurrent_per_server to all clients."""
    manager = MCPManager(
        dexscreener_cmd="echo dexscreener",
        dexpaprika_cmd="echo dexpaprika",
        rugcheck_cmd="echo rugcheck",
        trader_cmd="echo trader",
        max_concurrent_per_server=5,
    )

    for client in [manager.dexscreener, manager.dexpaprika, manager.rugcheck, manager.trader]:
        assert client is not None
        assert client._call_semaphore._value == 5


def test_mcp_client_rejects_zero_max_concurrent():
    """MCPClient raises ValueError when max_concurrent is 0."""
    with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
        MCPClient("test", "echo test", max_concurrent=0)


def test_mcp_client_rejects_negative_max_concurrent():
    """MCPClient raises ValueError when max_concurrent is negative."""
    with pytest.raises(ValueError, match="max_concurrent must be >= 1"):
        MCPClient("test", "echo test", max_concurrent=-1)


def test_mcp_manager_rejects_zero_max_concurrent_per_server():
    """MCPManager raises ValueError when max_concurrent_per_server is 0."""
    with pytest.raises(ValueError, match="max_concurrent_per_server must be >= 1"):
        MCPManager(
            dexscreener_cmd="echo dexscreener",
            dexpaprika_cmd="echo dexpaprika",
            max_concurrent_per_server=0,
        )


@pytest.mark.asyncio
async def test_call_tool_semaphore_limits_concurrency():
    """call_tool uses _call_semaphore to limit concurrent in-flight requests."""
    client = MCPClient("test", "echo test", max_concurrent=2)
    max_concurrent_observed = 0
    current_concurrent = 0
    lock = asyncio.Lock()

    async def _slow_call_tool_once(method, arguments):
        nonlocal max_concurrent_observed, current_concurrent
        async with lock:
            current_concurrent += 1
            if current_concurrent > max_concurrent_observed:
                max_concurrent_observed = current_concurrent
        await asyncio.sleep(0.05)
        async with lock:
            current_concurrent -= 1
        return {"ok": True}

    with patch.object(client, "_call_tool_once", new=_slow_call_tool_once):
        tasks = [client.call_tool("method", {}) for _ in range(6)]
        await asyncio.gather(*tasks)

    # Should never exceed the semaphore limit of 2
    assert max_concurrent_observed <= 2
