from app.tool_converter import convert_mcp_tools_to_gemini


def test_convert_dexpaprika_tools_are_namespaced():
    tools = [
        {"name": "getNetworks"},
        {"name": "getNetworkDexes"},
        {"name": "getNetworkPools"},
        {"name": "getDexPools"},
        {"name": "getPoolDetails"},
        {"name": "getPoolOHLCV"},
        {"name": "getPoolTransactions"},
        {"name": "getTokenDetails"},
        {"name": "getTokenPools"},
        {"name": "getTokenMultiPrices"},
        {"name": "search"},
    ]

    functions = convert_mcp_tools_to_gemini("dexpaprika", tools)
    names = {fn.name for fn in functions}

    assert "dexpaprika_getNetworks" in names
    assert "dexpaprika_getNetworkDexes" in names
    assert "dexpaprika_getPoolOHLCV" in names
    assert "dexpaprika_search" in names
    # Ensure all tools are preserved
    assert len(functions) == len(tools)
