"""Agentic planner using Gemini native function calling."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Callable, Dict, List, Optional

from google import genai
from google.genai import types

from app.mcp_client import MCPManager
from app.types import MAX_TOOL_RESULT_CHARS as _MAX_TOOL_RESULT_CHARS, PlannerResult
from app.tool_converter import parse_function_call_name


@dataclass
class ToolCall:
    """Represents a single tool call made by the model."""

    client: str
    method: str
    params: Dict[str, Any]
    result: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class AgenticContext:
    """Tracks state across iterations of the agentic loop."""

    iteration: int = 0
    total_tool_calls: int = 0
    tool_calls: List[ToolCall] = field(default_factory=list)
    tokens_found: List[Dict[str, str]] = field(default_factory=list)
    malformed_retries: int = 0  # Track recovery attempts for malformed function calls
    original_query: str = ""  # Store original query for recovery context


_TOOL_RESULT_SAMPLE_ITEMS = 25
_TOOL_RESULT_PREVIEW_ITEMS = 5
_TOOL_RESULT_PREVIEW_STRING_CHARS = 200

AGENTIC_SYSTEM_PROMPT_BASE = """You are a crypto/DeFi assistant that helps users find token and pool information on Solana.

## Your Capabilities
You can call tools to:
- Search tokens and get prices (dexscreener)
- Get pool/liquidity data across DEXs (dexpaprika)
- Check Solana token safety via rugcheck (rugcheck) - ONLY for solana chain

## CRITICAL: Always Use Tools for Data
You MUST call tools to get real-time data. NEVER respond without calling tools first when:
- User asks about any token (search for it)
- User asks for "more info" or "details" about something (search and get details)
- User mentions a token name, symbol, or address (search for it)
- User asks about prices, pools, volume, liquidity (use appropriate tools)

## IMPORTANT: Use Only Available Tools
You MUST only call tools that are listed in the "Available Tools" section below.
Do NOT invent or guess tool names. If a tool doesn't exist, use an alternative or explain the limitation.

## Multi-Step Query Handling
For complex queries like "analyze [token]" or "get more info on [token]", break into steps:
1. Search for the token by name/symbol to get its address and chain
2. Get token details (price, volume, market data)
3. Get token pools (liquidity info)
4. Check safety: rugcheck for solana tokens

If a tool fails or doesn't exist, try alternative approaches using available tools.

## Token Safety Checks - CRITICAL
- For tokens on **solana**: call rugcheck tools to get token safety summary
- Call safety checks in parallel for efficiency when showing multiple tokens
- For tokens on other chains: mark as "Unverified" without calling any tool
- If a safety check fails or returns an error: mark the token as "Unverified" in your response
- Never let a safety check failure block your main response - just mark as Unverified

## Solana Focused
- This bot is focused on Solana tokens
- If user asks about tokens on other chains, let them know the bot is Solana-focused

## Response Format - USE TABLES

For multiple tokens/pools, use horizontal tables:

| Token | Price | 24h Change | Volume | Safety |
|-------|-------|------------|--------|--------|
| BONK/SOL | $0.00001234 | +15.2% | $1.2M | ✅ Safe |

For single token details, use a compact vertical format:

| Field | Value |
|-------|-------|
| Token | BONK |
| Address | DezXAZ8z... |

Safety column values:
- ✅ Safe - safety check passed
- ⚠️ Risky - safety check shows concerns
- ❌ Dangerous/Rug - confirmed dangerous, avoid
- Unverified - chain not supported or check failed

## Guidelines
1. Call tools to get real data - don't make up prices or stats
2. Format numbers nicely (use K, M, B suffixes)
3. Include relevant links when available
4. If a tool fails, explain what happened and suggest alternatives
5. Be concise but informative

## Handling Complex Queries
For multi-step queries:
1. Break down into sequential steps - complete ONE step before moving to the next
2. Start with data retrieval (search, get prices)
3. Use ONE tool call at a time if you're having issues with multiple calls
4. Ensure all required parameters are filled before calling a tool
"""

# Type alias for log callback
LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]


class AgenticPlanner:
    """Gemini-based agentic planner with native function calling."""

    def __init__(
        self,
        api_key: str,
        mcp_manager: MCPManager,
        model_name: str = "gemini-2.5-flash",
        max_iterations: int = 8,
        max_tool_calls: int = 30,
        timeout_seconds: int = 90,
        verbose: bool = False,
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.model_name = model_name
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds
        self.verbose = verbose
        self.log_callback = log_callback

        # Initialize the client
        self.client = genai.Client(api_key=api_key)

        # Get tools from MCP servers
        self.gemini_tools = mcp_manager.get_gemini_functions()
        
        # Build dynamic system prompt with actual tool names
        tools_summary = mcp_manager.format_tools_for_system_prompt()
        if tools_summary.strip():
            self.system_prompt = (
                AGENTIC_SYSTEM_PROMPT_BASE
                + "\n## Available Tools\n"
                + tools_summary
            )
        else:
            self.system_prompt = (
                AGENTIC_SYSTEM_PROMPT_BASE
                + "\n## Available Tools\n"
                + "No external tools are currently available. "
                + "Answer using your own knowledge and inform the user that tool-based data is unavailable."
            )

    def _log(self, level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Log a message if verbose mode is enabled."""
        if self.verbose and self.log_callback:
            self.log_callback(level, message, data)

    def _truncate_result(self, result: Any) -> Any:
        """Truncate large tool results to conserve context window tokens."""
        if isinstance(result, str) and len(result) > _MAX_TOOL_RESULT_CHARS:
            content_budget = _MAX_TOOL_RESULT_CHARS
            omitted = len(result) - content_budget
            suffix = f"\n... [truncated {omitted} chars]"
            content_budget = max(0, _MAX_TOOL_RESULT_CHARS - len(suffix))
            omitted = len(result) - content_budget
            suffix = f"\n... [truncated {omitted} chars]"
            return result[:content_budget] + suffix
        if isinstance(result, (dict, list)):
            if self._should_truncate_structured_result(result):
                return self._build_truncated_preview(result)
        return result

    def _should_truncate_structured_result(self, result: Any) -> bool:
        """Estimate structured payload size without serializing the full object."""
        if isinstance(result, dict):
            sample_items = list(islice(result.items(), _TOOL_RESULT_SAMPLE_ITEMS))
            sample: Any = dict(sample_items)
            sampled_count = len(sample_items)
            total_items = len(result)
        elif isinstance(result, list):
            sample = result[:_TOOL_RESULT_SAMPLE_ITEMS]
            sampled_count = len(sample)
            total_items = len(result)
        else:
            return False

        if sampled_count == 0:
            return False

        try:
            sample_size = len(json.dumps(sample, default=str))
        except (TypeError, ValueError):
            return True
        if sample_size > _MAX_TOOL_RESULT_CHARS:
            return True

        if total_items > sampled_count:
            estimated_size = (sample_size / sampled_count) * total_items
            return estimated_size > _MAX_TOOL_RESULT_CHARS

        return False

    def _build_truncated_preview(self, result: Any) -> Dict[str, Any]:
        """Build a JSON-serializable shallow preview for large structured payloads."""
        if isinstance(result, dict):
            preview = {
                str(key): self._preview_truncated_value(value)
                for key, value in islice(result.items(), _TOOL_RESULT_PREVIEW_ITEMS)
            }
            total_items = len(result)
        elif isinstance(result, list):
            preview = [self._preview_truncated_value(value) for value in result[:_TOOL_RESULT_PREVIEW_ITEMS]]
            total_items = len(result)
        else:
            preview = self._preview_truncated_value(result)
            total_items = 1

        preview_items = len(preview) if isinstance(preview, (dict, list)) else 1
        return {
            "_truncated": True,
            "_type": type(result).__name__,
            "_total_items": total_items,
            "_preview_items": preview_items,
            "_omitted_items": max(0, total_items - preview_items),
            "_preview": preview,
        }

    def _preview_truncated_value(self, value: Any) -> Any:
        """Convert values into JSON-safe shallow previews."""
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > _TOOL_RESULT_PREVIEW_STRING_CHARS:
                return (
                    value[:_TOOL_RESULT_PREVIEW_STRING_CHARS]
                    + f"... [truncated {len(value) - _TOOL_RESULT_PREVIEW_STRING_CHARS} chars]"
                )
            return value
        if isinstance(value, dict):
            return {
                "_type": "dict",
                "_total_items": len(value),
                "_keys": [str(key) for key in islice(value.keys(), _TOOL_RESULT_PREVIEW_ITEMS)],
            }
        if isinstance(value, list):
            return {
                "_type": "list",
                "_total_items": len(value),
            }
        return str(value)

    def _is_malformed_response(self, response: Any) -> bool:
        """Check if response indicates a malformed function call."""
        if not response.candidates:
            return False
        for candidate in response.candidates:
            if hasattr(candidate, 'finish_reason'):
                finish_reason = str(candidate.finish_reason)
                if 'MALFORMED' in finish_reason:
                    return True
        return False

    def _build_recovery_message(self, original_query: str, attempt: int) -> str:
        """Build guidance message for malformed function call recovery."""
        if attempt == 1:
            return (
                "Your function call was malformed. Let's break this down step by step.\n"
                f"Original query: {original_query}\n\n"
                "Please:\n"
                "1. Start with ONE tool call at a time\n"
                "2. Ensure all required parameters are provided\n"
                "3. Use proper data types (strings for addresses, numbers for prices)\n\n"
                "Try calling the first relevant tool now to get the data you need."
            )
        else:
            return (
                "Still having issues with function calls. Please respond with TEXT only explaining:\n"
                "1. What data you need to answer the query\n"
                "2. Which tools you would use and with what parameters\n\n"
                "I'll help reformulate the request."
            )

    async def run(
        self, message: str, context: Optional[Dict[str, Any]] = None
    ) -> PlannerResult:
        """Execute a query using the agentic loop."""
        context = context or {}
        agentic_ctx = AgenticContext(original_query=message)

        self._log("info", f"Starting query: {message}")
        self._log("debug", f"Model: {self.model_name}, Tools: {len(self.gemini_tools)}")

        # Build conversation history
        history = context.get("conversation_history", [])
        
        # Create chat config with tools
        # Wrap FunctionDeclarations in a Tool object
        tool_config = None
        if self.gemini_tools:
            tool_config = [types.Tool(functionDeclarations=self.gemini_tools)]
        
        config = types.GenerateContentConfig(
            systemInstruction=self.system_prompt,
            tools=tool_config,
        )
        
        # Start chat session
        chat = self.client.chats.create(
            model=self.model_name,
            config=config,
            history=self._convert_history(history),
        )

        try:
            return await asyncio.wait_for(
                self._agentic_loop(chat, message, agentic_ctx),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._build_timeout_result(agentic_ctx)
        except Exception as e:
            self._log("error", f"Query failed: {str(e)}")
            return PlannerResult(message=f"Error: {str(e)}")

    async def _agentic_loop(
        self,
        chat: Any,
        message: str,
        ctx: AgenticContext,
    ) -> PlannerResult:
        """Main agentic reasoning loop."""
        max_malformed_retries = 2
        
        try:
            response = await asyncio.to_thread(chat.send_message, message)
        except Exception as e:
            if "MALFORMED_FUNCTION_CALL" in str(e):
                self._log("error", f"Malformed function call: {str(e)}")
                ctx.malformed_retries += 1
                recovery_msg = self._build_recovery_message(ctx.original_query, ctx.malformed_retries)
                response = await asyncio.to_thread(chat.send_message, recovery_msg)
            else:
                raise

        while ctx.iteration < self.max_iterations:
            ctx.iteration += 1
            self._log("info", f"Iteration {ctx.iteration}/{self.max_iterations}")

            # Check for malformed response via finish_reason (not exception)
            if self._is_malformed_response(response):
                self._log("warning", "Detected malformed function call via finish_reason")
                ctx.malformed_retries += 1
                
                if ctx.malformed_retries <= max_malformed_retries:
                    self._log("info", f"Recovery attempt {ctx.malformed_retries}/{max_malformed_retries}")
                    recovery_msg = self._build_recovery_message(ctx.original_query, ctx.malformed_retries)
                    try:
                        response = await asyncio.to_thread(chat.send_message, recovery_msg)
                        continue  # Re-enter loop with new response
                    except Exception as e:
                        self._log("error", f"Recovery failed: {str(e)}")
                else:
                    # Max retries exceeded - return helpful message
                    self._log("error", "Max malformed retries exceeded")
                    return PlannerResult(
                        message=(
                            "I'm having trouble processing this complex query. "
                            "Please try breaking it into smaller requests:\n"
                            "1. First, search for tokens (e.g., 'find solana tokens above 900k market cap')\n"
                            "2. Then, ask for more details (e.g., 'tell me more about TOKEN_SYMBOL')\n"
                            "3. Check safety (e.g., 'is TOKEN_SYMBOL safe?')"
                        ),
                        tokens=ctx.tokens_found,
                    )

            # Check if model wants to call tools
            function_calls = self._extract_function_calls(response)

            if not function_calls:
                # No more tool calls - return final response
                self._log("info", f"Complete. Total tool calls: {ctx.total_tool_calls}")
                # Debug: log response structure if empty
                if response.candidates:
                    for i, candidate in enumerate(response.candidates):
                        if candidate.content and candidate.content.parts:
                            part_types = [type(p).__name__ for p in candidate.content.parts]
                            self._log("debug", f"Candidate {i} parts: {part_types}")
                        else:
                            self._log("debug", f"Candidate {i}: no content or parts")
                            if candidate.finish_reason:
                                self._log("debug", f"Finish reason: {candidate.finish_reason}")
                return PlannerResult(
                    message=self._extract_text(response),
                    tokens=ctx.tokens_found,
                )

            self._log("info", f"Tool calls requested: {len(function_calls)}")
            for fc in function_calls:
                self._log("tool", f"→ {fc['name']}", {"args": fc["args"]})

            # Check tool call limits
            if ctx.total_tool_calls + len(function_calls) > self.max_tool_calls:
                return self._build_limit_result(ctx, "tool call limit")

            # Execute tool calls in parallel
            tool_results = await self._execute_tool_calls(function_calls, ctx)

            # Send results back to model, handle malformed function calls
            try:
                response = await asyncio.to_thread(chat.send_message, tool_results)
            except Exception as e:
                if "MALFORMED_FUNCTION_CALL" in str(e):
                    self._log("error", f"Malformed function call on retry: {str(e)}")
                    ctx.malformed_retries += 1
                    if ctx.malformed_retries <= max_malformed_retries:
                        recovery_msg = self._build_recovery_message(ctx.original_query, ctx.malformed_retries)
                        response = await asyncio.to_thread(chat.send_message, recovery_msg)
                    else:
                        return PlannerResult(
                            message=(
                                "I'm having trouble with tool calls for this query. "
                                "Please try a simpler request or break it into steps."
                            ),
                            tokens=ctx.tokens_found,
                        )
                else:
                    raise

        return self._build_limit_result(ctx, "iteration limit")

    def _extract_function_calls(self, response: Any) -> List[Dict[str, Any]]:
        """Extract function calls from Gemini response."""
        calls = []
        if not response.candidates:
            return calls
        for candidate in response.candidates:
            if not candidate.content or not candidate.content.parts:
                continue
            for part in candidate.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    # Ensure name is stripped of whitespace
                    name = fc.name.strip() if fc.name else ""
                    if name:
                        calls.append({
                            "name": name,
                            "args": dict(fc.args) if fc.args else {},
                        })
        return calls

    def _extract_text(self, response: Any) -> str:
        """Extract text from Gemini response."""
        texts = []
        thoughts = []
        
        if not response.candidates:
            # Log why we have no response
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                return f"Response blocked: {response.prompt_feedback}"
            return "No response generated. The model returned no candidates."
        
        for candidate in response.candidates:
            # Check for finish reason that might indicate issues
            if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
                finish_reason = str(candidate.finish_reason)
                if 'SAFETY' in finish_reason or 'BLOCK' in finish_reason:
                    return f"Response blocked due to safety filters: {finish_reason}"
            
            if not candidate.content or not candidate.content.parts:
                continue
            
            for part in candidate.content.parts:
                # Extract regular text
                if hasattr(part, "text") and part.text:
                    texts.append(part.text)
                # Also check for thought content (model thinking)
                if hasattr(part, "thought") and part.thought:
                    thoughts.append(f"[Thought: {part.thought}]")
        
        if texts:
            return "\n".join(texts)
        if thoughts:
            # Model only returned thoughts, no actionable response
            self._log("debug", f"Model returned only thoughts: {thoughts}")
            return "The model is processing but returned no actionable response. Please try rephrasing your query."
        
        return "No response generated. The model returned empty content."

    async def _execute_tool_calls(
        self, function_calls: List[Dict[str, Any]], ctx: AgenticContext
    ) -> List[types.Part]:
        """Execute tool calls and return results for Gemini."""
        tasks = []
        for fc in function_calls:
            tasks.append(self._execute_single_tool(fc, ctx))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Build response parts
        parts = []
        for fc, result in zip(function_calls, results):
            if isinstance(result, Exception):
                response_data = {"error": str(result)}
            else:
                response_data = self._truncate_result(result)

            parts.append(
                types.Part.from_function_response(
                    name=fc["name"],
                    response={"result": response_data},
                )
            )

        return parts

    async def _execute_single_tool(
        self, fc: Dict[str, Any], ctx: AgenticContext
    ) -> Any:
        """Execute a single tool call."""
        name = fc["name"]
        args = fc["args"]

        client_name, method = parse_function_call_name(name)
        client = self.mcp_manager.get_client(client_name)

        if not client:
            raise ValueError(f"Unknown MCP client: {client_name}")

        ctx.total_tool_calls += 1

        tool_call = ToolCall(
            client=client_name,
            method=method,
            params=args,
        )
        ctx.tool_calls.append(tool_call)

        try:
            result = await client.call_tool(method, args)

            tool_call.result = result

            # Log success
            result_preview = self._preview_result(result)
            self._log("tool", f"✓ {name}", {"result_preview": result_preview})

            # Extract tokens for context
            self._extract_tokens(result, ctx)

            return result
        except Exception as e:
            tool_call.error = str(e)
            self._log("error", f"✗ {name}: {str(e)}")
            raise

    def _preview_result(self, result: Any) -> str:
        """Create a short preview of a result for logging."""
        if isinstance(result, dict):
            if "pairs" in result:
                return f"{len(result['pairs'])} pairs"
            if "pools" in result:
                return f"{len(result['pools'])} pools"
            keys = list(result.keys())[:3]
            return f"dict with keys: {keys}"
        if isinstance(result, list):
            return f"list with {len(result)} items"
        return str(result)[:50]

    def _extract_tokens(self, result: Any, ctx: AgenticContext) -> None:
        """Extract token info from results for context tracking."""
        if not isinstance(result, dict):
            return

        # From dexscreener pairs
        pairs = result.get("pairs", [])
        for pair in pairs[:5]:
            base = pair.get("baseToken", {})
            if base.get("address") and base.get("symbol"):
                ctx.tokens_found.append({
                    "address": base["address"],
                    "symbol": base["symbol"],
                    "chainId": pair.get("chainId", "unknown"),
                })

        # From dexpaprika pools
        pools = result.get("pools", [])
        for pool in pools[:5]:
            tokens = pool.get("tokens", [])
            for token in tokens:
                if token.get("id") and token.get("symbol"):
                    ctx.tokens_found.append({
                        "address": token["id"],
                        "symbol": token["symbol"],
                        "chainId": pool.get("chain") or pool.get("network", "unknown"),
                    })

    def _convert_history(
        self, history: List[Dict[str, str]]
    ) -> List[types.Content]:
        """Convert conversation history to Gemini format."""
        contents = []
        for msg in history:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=msg.get("content", ""))],
                )
            )
        return contents

    def _build_timeout_result(self, ctx: AgenticContext) -> PlannerResult:
        """Build result when timeout occurs."""
        lines = ["⏱️ Request timed out."]
        if ctx.tool_calls:
            lines.append(f"\nCompleted {len(ctx.tool_calls)} tool calls before timeout.")
        return PlannerResult(message="\n".join(lines), tokens=ctx.tokens_found)

    def _build_limit_result(self, ctx: AgenticContext, reason: str) -> PlannerResult:
        """Build result when limits are reached."""
        lines = [f"⚠️ Reached {reason}."]
        if ctx.tool_calls:
            lines.append(f"\nCompleted {len(ctx.tool_calls)} tool calls.")
        return PlannerResult(message="\n".join(lines), tokens=ctx.tokens_found)
