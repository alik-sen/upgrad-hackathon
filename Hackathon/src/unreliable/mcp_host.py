"""MCP host client: persistent JSON-RPC sessions (stdio for local workers, Streamable HTTP for state-store), signed-manifest check,
per-server bulkheads, timeouts, auto-reconnect, payload inspector ring buffer, and overhead metering (KPI-30)."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections import deque
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

import pydantic_core
from mcp import Client, StdioServerParameters

from .config import settings
from .manifest import verify_manifest
from .telemetry import tele


class MCPCallError(Exception):
    def __init__(self, code: int, name: str, msg: str):
        super().__init__(f"{name}: {msg}")
        self.code, self.name, self.msg = code, name, msg


class ServerConn:
    def __init__(self, name: str, conf: dict, pool: int):
        self.name, self.conf = name, conf
        self.client: Client | None = None
        self.stack: AsyncExitStack | None = None
        self.up = False
        self.sem = asyncio.Semaphore(pool)
        self.lock = asyncio.Lock()
        self.calls = self.errors = self.timeouts = 0
        self.inflight = 0
        self.ewma_ms = 300.0 if name == "hybrid-rag" else 20.0   # prior service time until measured
        self.last_ms = 0.0
        self.last_error = ""
        self.proc: subprocess.Popen | None = None


class MCPHost:
    def __init__(self, config_path=None, fake: dict[str, Callable[[str, dict], Awaitable[dict]]] | None = None):
        cfg = json.loads((config_path or settings.root / "mcp_config.json").read_text())
        self.cfg = cfg["mcpServers"]
        pools = {"hybrid-rag": settings.rag_pool, "state-store": settings.db_pool, "graphify-vault": settings.rag_pool}
        self.conns = {n: ServerConn(n, c, pools.get(n, 32)) for n, c in self.cfg.items()}
        self.payloads: deque = deque(maxlen=100)
        self.inspect = False
        self.fake = fake  # tests: {server: async fn(tool, args)->dict}
        self.overhead: deque = deque(maxlen=50_000)      # round-trip wall time minus server compute (IPC + queueing + serialization)
        self.serde: deque = deque(maxlen=50_000)          # client-side JSON-RPC (de)serialization only (KPI-30)
        self.spawn_state = os.environ.get("SPAWN_STATE", "1") == "1"
        self._n = 0

    # ---------------- lifecycle ----------------
    async def start(self):
        if self.fake:
            for c in self.conns.values():
                c.up = True
            return
        await asyncio.gather(*(self._connect(c) for c in self.conns.values()), return_exceptions=True)

    async def stop(self):
        for c in self.conns.values():
            if c.stack:
                try:
                    await c.stack.aclose()
                except BaseException:  # noqa: BLE001
                    pass
            if c.proc:
                c.proc.terminate()

    @staticmethod
    def _port_open(url: str) -> bool:
        host, port = url.split("//")[1].split("/")[0].split(":")
        with socket.socket() as s:
            s.settimeout(0.2)
            return s.connect_ex((host, int(port))) == 0

    async def _connect(self, c: ServerConn):
        async with c.lock:
            if c.up:
                return
            try:
                if c.conf["transport"] == "stdio":
                    script = settings.root / c.conf["args"][0]
                    env = {**os.environ, "PYTHONPATH": str(settings.root / "src"), "MCP_CHILD": "1"}
                    target: Any = StdioServerParameters(command=sys.executable, args=[str(script)], env=env, cwd=str(settings.root))
                else:
                    url = settings.state_url
                    if self.spawn_state and not self._port_open(url):
                        port = url.split(":")[-1].split("/")[0]
                        c.proc = subprocess.Popen([sys.executable, str(settings.root / "mcp_servers" / "state_server.py"), port],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(settings.root), env={**os.environ, "PYTHONPATH": str(settings.root / "src")})
                        for _ in range(100):
                            if self._port_open(url):
                                break
                            await asyncio.sleep(0.1)
                    target = url
                c.stack = AsyncExitStack()
                c.client = await c.stack.enter_async_context(Client(target))
                m = await c.client.call_tool("manifest", {})
                mf = _payload(m)
                if not verify_manifest(mf, c.name, c.conf["tools"]):
                    raise MCPCallError(-32008, "MANIFEST_INVALID", f"{c.name} manifest signature/tool list mismatch")
                c.up = True
                c.last_error = ""
            except BaseException as e:  # noqa: BLE001
                c.up = False
                c.last_error = f"{type(e).__name__}: {e}"[:200]
                if c.stack:
                    try:
                        await c.stack.aclose()
                    except BaseException:  # noqa: BLE001
                        pass
                    c.stack = None
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise

    async def _reconnect_bg(self, c: ServerConn):
        await asyncio.sleep(0.2)
        await self._connect(c)

    # ---------------- calls ----------------
    async def call(self, server: str, tool: str, args: dict, timeout_ms: float | None = None) -> dict:
        c = self.conns[server]
        timeout = (timeout_ms or settings.mcp_timeout_ms) / 1000
        if self.fake:
            return await self.fake[server](tool, args)
        if not c.up:
            asyncio.create_task(self._reconnect_bg(c))
            raise MCPCallError(-32005, "TOOL_TIMEOUT", f"{server} unavailable")
        ts = time.perf_counter()
        _ser = pydantic_core.to_json({"inp": args})       # request envelope serialization (what the SDK performs on the wire)
        ser_ms = (time.perf_counter() - ts) * 1000
        t0 = time.perf_counter()
        c.inflight += 1
        try:
            async with c.sem:
                res = await asyncio.wait_for(c.client.call_tool(tool, {"inp": args} if tool != "graph_stats" else {}), timeout)
        except asyncio.TimeoutError:
            c.timeouts += 1; c.errors += 1
            tele.inc(f"mcp.{server}.timeout")
            raise MCPCallError(-32005, "TOOL_TIMEOUT", f"{server}.{tool} exceeded {timeout*1000:.0f} ms") from None
        except asyncio.CancelledError:   # caller's step timeout / client disconnect: the session itself is still healthy
            c.timeouts += 1
            raise
        except BaseException as e:  # noqa: BLE001  (EOF/closed/crash)
            c.errors += 1; c.up = False; c.last_error = f"{type(e).__name__}"
            asyncio.create_task(self._reconnect_bg(c))
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            raise MCPCallError(-32005, "TOOL_TIMEOUT", f"{server} connection lost") from None
        finally:
            c.inflight -= 1
        ms = (time.perf_counter() - t0) * 1000
        c.calls += 1; c.last_ms = ms
        if tool in ("hybrid_retrieve",):
            c.ewma_ms = 0.8 * c.ewma_ms + 0.2 * min(ms, 1500)
        if res.is_error:
            raise MCPCallError(-32602, "INVALID_PARAMS", (res.content[0].text if res.content else "tool error")[:300])
        td = time.perf_counter()
        data = _payload(res)
        self.serde.append(ser_ms + (time.perf_counter() - td) * 1000)
        srv = float(data.get("_srv_ms", 0.0)) if isinstance(data, dict) else 0.0
        self.overhead.append(max(0.0, ms - srv))
        tele.record("mcp.call", ms)
        tele.record("mcp.overhead", max(0.0, ms - srv))
        self._n += 1
        if self.inspect or self._n % 100 == 0:
            self.payloads.append({"ts": time.time(), "server": server, "method": "tools/call", "tool": tool, "ms": round(ms, 2),
                                  "request": {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool, "arguments": {"inp": _trunc(args)}}},
                                  "response": _trunc(data)})
        return data

    def queue_penalty_ms(self, server: str) -> float:
        """Load-aware deadline extension: each request already in flight on a CPU-bound server costs about one service time of queueing."""
        c = self.conns.get(server)
        return max(0, c.inflight - 1) * c.ewma_ms if c else 0.0

    def status(self) -> list[dict]:
        return [{"server": n, "transport": c.conf["transport"], "up": c.up, "calls": c.calls, "errors": c.errors, "timeouts": c.timeouts,
                 "last_ms": round(c.last_ms, 2), "last_error": c.last_error, "tools": c.conf["tools"]} for n, c in self.conns.items()]


def _payload(res) -> dict:
    """Tool result -> dict. `-> dict` tools arrive as JSON text content; typed tools as structured content."""
    if res.structured_content:
        d = res.structured_content
        return d["result"] if set(d) == {"result"} and isinstance(d["result"], dict) else d
    try:
        return json.loads(res.content[0].text)
    except Exception:  # noqa: BLE001
        return {}


def _trunc(o: Any, n: int = 700) -> Any:
    s = json.dumps(o, default=str)
    return o if len(s) <= n else {"_truncated": s[:n] + "..."}
