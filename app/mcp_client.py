"""MCP client management for DexScreener and DexPaprika servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import uuid
from asyncio.subprocess import Process
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from google.genai import types

logger = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"
DEFAULT_PROTOCOL_VERSION = "2024-10-07"
CLIENT_INFO = {
    "name": "dex-agentic-bot",
    "version": "0.1.0",
}


class MCPTimeoutError(RuntimeError):
    """Raised when an MCP request times out waiting for a response."""


class MCPClient:
    """Lightweight JSON-over-stdio client for an MCP server process."""

    def __init__(
        self,
        name: str,
        command: str,
        call_timeout: float = 90.0,
        retry_on_timeout: bool = True,
        extra_env: Optional[Dict[str, str]] = None,
        max_concurrent: int = 8,
    ) -> None:
        self.name = name
        self.command = command
        try:
            self._command_args = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"Invalid MCP command for {name!r}: {command}") from exc
        if not self._command_args:
            raise ValueError(f"Empty MCP command for {name!r}")
        self._command_repr = " ".join(self._command_args)
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1 for {name!r}, got {max_concurrent}")
        self._call_timeout = call_timeout
        self._retry_on_timeout = retry_on_timeout
        self._extra_env = extra_env
        self._cwd = self._resolve_cwd()
        self.process: Optional[Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._init_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future[Any]] = {}
        self._call_semaphore = asyncio.Semaphore(max_concurrent)
        self._initialized = False
        self._tools: list[Dict[str, Any]] = []

    def _resolve_cwd(self) -> Optional[str]:
        """Derive working directory from the script path in the command.

        For commands like ``node /path/to/project/dist/index.js``, walk up
        from the script path until a ``package.json`` (Node) or ``pyproject.toml``
        (Python) is found and use that directory as cwd.  This lets MCP servers
        load their own ``.env`` files via ``dotenv/config`` or similar.
        """
        for arg in self._command_args:
            p = Path(arg)
            if not p.is_absolute() or not p.is_file():
                continue
            for parent in p.parents:
                if (parent / "package.json").is_file() or (
                    parent / "pyproject.toml"
                ).is_file():
                    return str(parent)
        return None

    @property
    def tools(self) -> list[Dict[str, Any]]:
        """Return the list of tools available on this server."""
        return self._tools

    def to_gemini_functions(self) -> List["types.FunctionDeclaration"]:
        """Convert MCP tools to Gemini function declarations."""
        from app.tool_converter import convert_mcp_tools_to_gemini
        return convert_mcp_tools_to_gemini(self.name, self._tools)

    async def start(self) -> None:
        """Launch the MCP server process if it is not already running."""
        if self.process and self.process.returncode is None:
            await self._ensure_initialized()
            return

        print(f"  Starting MCP server: {self.name}")
        env = {**os.environ, **self._extra_env} if self._extra_env else None
        self.process = await asyncio.create_subprocess_exec(
            *self._command_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
        )
        if self.process and self.process.returncode is not None:
            code = self.process.returncode
            await self.stop()
            raise RuntimeError(
                f"MCP server {self.name} exited immediately with code {code}"
            )
        self._tune_stream_limits()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._log_stderr())
        await self._ensure_initialized()

    def _tune_stream_limits(self) -> None:
        """Increase asyncio stream limits for large MCP payloads."""
        process = self.process
        if not process:
            return

        target_limit = 1_048_576  # 1 MiB

        try:
            stdout = getattr(process, "stdout", None)
            if stdout is not None and hasattr(stdout, "_limit"):
                current = getattr(stdout, "_limit", 0) or 0
                if current < target_limit:
                    setattr(stdout, "_limit", target_limit)

            stderr = getattr(process, "stderr", None)
            if stderr is not None and hasattr(stderr, "_limit"):
                current = getattr(stderr, "_limit", 0) or 0
                if current < target_limit:
                    setattr(stderr, "_limit", target_limit)
        except Exception:
            pass

    async def stop(self) -> None:
        """Terminate the process gracefully."""
        if not self.process:
            return
        print(f"  Stopping MCP server: {self.name}")
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        self._fail_pending(f"MCP server '{self.name}' stopped.")
        self._initialized = False
        self.process = None

    async def _read_stdout(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return

        while True:
            try:
                line = await process.stdout.readline()
                if not line:
                    break
                await self._handle_message(line.decode("utf-8").strip())
            except asyncio.CancelledError:
                break
            except Exception:
                break

        self._fail_pending(f"MCP stdout closed for {self.name}")

    async def _log_stderr(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return

        while True:
            try:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    print(f"[{self.name} stderr] {text}", file=sys.stderr)
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def _handle_message(self, raw: str) -> None:
        if not raw:
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_id = msg.get("id")
        if msg_id:
            future = self._pending.pop(msg_id, None)
            if future is not None and not future.done():
                if "error" in msg:
                    future.set_exception(RuntimeError(str(msg["error"])))
                else:
                    future.set_result(msg.get("result"))

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending.clear()

    async def _ensure_initialized(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return

            # Send initialize request
            init_result = await self._request(
                "initialize",
                {
                    "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                    "clientInfo": CLIENT_INFO,
                    "capabilities": {},
                },
                timeout=60.0,
            )

            # Send initialized notification
            await self._notify("notifications/initialized", {})

            # Fetch tools
            tools_result = await self._request("tools/list", {}, timeout=60.0)
            self._tools = tools_result.get("tools", [])

            self._initialized = True
            print(f"  ✓ {self.name}: {len(self._tools)} tools available")

    async def _request(
        self, method: str, params: Dict[str, Any], timeout: float = 30.0
    ) -> Any:
        request_id = str(uuid.uuid4())
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        async with self._lock:
            await self._write_locked(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise MCPTimeoutError(
                f"MCP request timed out: {method} ({self.name}: {self._command_repr})"
            )
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        async with self._lock:
            await self._write_locked(
                {
                    "jsonrpc": JSONRPC_VERSION,
                    "method": method,
                    "params": params,
                }
            )

    async def call_tool(self, method: str, arguments: Dict[str, Any]) -> Any:
        """Call an MCP tool and return the result.

        On timeout, acquires a restart lock and restarts the MCP subprocess so that
        subsequent calls start with a clean process.  If ``retry_on_timeout`` is
        ``True`` (the default), the call is retried once after the restart.  Set
        ``retry_on_timeout=False`` for non-idempotent operations (e.g. trade
        execution) to avoid duplicate side-effects from a double submission.
        """
        async with self._call_semaphore:
            try:
                return await self._call_tool_once(method, arguments)
            except MCPTimeoutError:
                logger.warning(
                    "MCP tool call timed out (%s/%s), restarting process%s.",
                    self.name,
                    method,
                    " and retrying once" if self._retry_on_timeout else "",
                )
                async with self._restart_lock:
                    await self.stop()
                    await self.start()
                if not self._retry_on_timeout:
                    raise
                return await self._call_tool_once(method, arguments)

    async def _call_tool_once(self, method: str, arguments: Dict[str, Any]) -> Any:
        """Single attempt to call an MCP tool."""
        result = await self._request(
            "tools/call",
            {"name": method, "arguments": arguments},
            timeout=self._call_timeout,
        )

        # Extract text content from result
        content = result.get("content", [])
        text = self._extract_content_text(content)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return result

    async def _write_locked(self, message: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError(f"MCP process {self.name} is not running")
        data = (json.dumps(message) + "\n").encode("utf-8")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    @staticmethod
    def _extract_content_text(content: Any) -> Optional[str]:
        if not isinstance(content, list):
            return None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
        return None


class MCPManager:
    """Shared registry for configured MCP clients."""

    def __init__(
        self,
        dexscreener_cmd: str,
        dexpaprika_cmd: str,
        rugcheck_cmd: str = "",
        solana_rpc_cmd: str = "",
        trader_cmd: str = "",
        call_timeout: float = 90.0,
        solana_rpc_url: str = "",
        trader_env: Optional[Dict[str, str]] = None,
        max_concurrent_per_server: int = 8,
    ) -> None:
        if max_concurrent_per_server < 1:
            raise ValueError(f"max_concurrent_per_server must be >= 1, got {max_concurrent_per_server}")
        mc = max_concurrent_per_server
        self.dexscreener = MCPClient("dexscreener", dexscreener_cmd, call_timeout=call_timeout, max_concurrent=mc)
        self.dexpaprika = MCPClient("dexpaprika", dexpaprika_cmd, call_timeout=call_timeout, max_concurrent=mc)
        self.rugcheck = MCPClient("rugcheck", rugcheck_cmd, call_timeout=call_timeout, max_concurrent=mc) if rugcheck_cmd else None
        solana_extra_env = {"SOLANA_RPC_URL": solana_rpc_url} if solana_rpc_url else None
        self.solana = MCPClient("solana", solana_rpc_cmd, call_timeout=call_timeout, extra_env=solana_extra_env, max_concurrent=mc) if solana_rpc_cmd else None
        # retry_on_timeout=False: trader executes swaps; retrying on timeout risks a double submission.
        self.trader = MCPClient("trader", trader_cmd, call_timeout=call_timeout, retry_on_timeout=False, extra_env=trader_env, max_concurrent=mc) if trader_cmd else None
        self._gemini_functions_cache: Optional[List["types.FunctionDeclaration"]] = None

    async def start(self) -> None:
        tasks = [
            self.dexscreener.start(),
            self.dexpaprika.start(),
        ]
        if self.rugcheck:
            tasks.append(self.rugcheck.start())
        if self.solana:
            tasks.append(self.solana.start())
        if self.trader:
            tasks.append(self.trader.start())
        await asyncio.gather(*tasks)
        self._gemini_functions_cache = None  # Invalidate after (re)start

    async def shutdown(self) -> None:
        tasks = [
            self.dexscreener.stop(),
            self.dexpaprika.stop(),
        ]
        if self.rugcheck:
            tasks.append(self.rugcheck.stop())
        if self.solana:
            tasks.append(self.solana.stop())
        if self.trader:
            tasks.append(self.trader.stop())
        await asyncio.gather(*tasks)
        self._gemini_functions_cache = None  # Invalidate on shutdown

    def get_gemini_functions(self) -> List["types.FunctionDeclaration"]:
        """Get all MCP tools as Gemini function declarations (cached)."""
        if self._gemini_functions_cache is not None:
            return self._gemini_functions_cache

        all_functions: List["types.FunctionDeclaration"] = []
        clients = [self.dexscreener, self.dexpaprika]
        if self.rugcheck:
            clients.append(self.rugcheck)
        if self.solana:
            clients.append(self.solana)
        if self.trader:
            clients.append(self.trader)
        for client in clients:
            all_functions.extend(client.to_gemini_functions())
        self._gemini_functions_cache = all_functions
        return all_functions

    def get_gemini_functions_for(self, client_names: List[str]) -> List["types.FunctionDeclaration"]:
        """Get Gemini function declarations for a specific subset of MCP clients.

        Filters the cached full function list by the ``{client}_{method}`` naming
        convention. Unknown or unavailable client names are silently skipped.
        Use this to restrict tool access to only what is needed for a given task,
        avoiding unintended calls to rate-limited or dangerous endpoints.
        """
        # Normalize to a set to avoid processing the same client multiple times.
        requested_clients = {name for name in client_names if isinstance(name, str)}
        if not requested_clients:
            return []

        # Tools are named using the convention "{client}_{method}", so filter
        # the cached full function list by prefix rather than re-converting.
        prefixes = tuple(f"{name}_" for name in requested_clients)
        all_functions = self.get_gemini_functions()

        functions: List["types.FunctionDeclaration"] = []
        seen_names: set[str] = set()
        for fn in all_functions:
            fn_name = getattr(fn, "name", None)
            if not isinstance(fn_name, str):
                continue
            if not fn_name.startswith(prefixes):
                continue
            if fn_name in seen_names:
                continue
            seen_names.add(fn_name)
            functions.append(fn)
        return functions

    def format_tools_for_system_prompt(self) -> str:
        """Format all available tools as a string for inclusion in the system prompt."""
        lines = []
        clients = [self.dexscreener, self.dexpaprika]
        if self.rugcheck:
            clients.append(self.rugcheck)
        if self.solana:
            clients.append(self.solana)
        if self.trader:
            clients.append(self.trader)
        for client in clients:
            if client.tools:
                lines.append(f"\n### {client.name} tools:")
                for tool in client.tools:
                    name = tool.get("name", "unknown")
                    desc = tool.get("description", "No description")
                    # Truncate long descriptions at word boundary
                    desc = self._truncate_description(desc, max_length=100)
                    
                    # Extract required parameters from inputSchema
                    input_schema = tool.get("inputSchema", {})
                    required_params = input_schema.get("required", [])
                    properties = input_schema.get("properties", {})
                    
                    param_info = ""
                    if required_params:
                        param_details = []
                        for param in required_params:
                            param_type = properties.get(param, {}).get("type", "string")
                            param_details.append(f"{param}:{param_type}")
                        param_info = f" [REQUIRED: {', '.join(param_details)}]"
                    
                    lines.append(f"- {client.name}_{name}: {desc}{param_info}")
        
        return "\n".join(lines)

    @staticmethod
    def _truncate_description(desc: str, max_length: int = 100) -> str:
        """Truncate description at word boundary."""
        if len(desc) <= max_length:
            return desc
        # Find last space before max_length
        truncated = desc[:max_length]
        last_space = truncated.rfind(" ")
        if last_space > max_length // 2:
            return truncated[:last_space] + "..."
        return truncated + "..."

    def get_client(self, name: str) -> Optional[Any]:
        """Get an MCP client or tool provider by name."""
        clients: Dict[str, Optional[Any]] = {
            "dexscreener": self.dexscreener,
            "dexpaprika": self.dexpaprika,
            "rugcheck": self.rugcheck,
            "solana": self.solana,
            "trader": self.trader,
        }
        return clients.get(name)
