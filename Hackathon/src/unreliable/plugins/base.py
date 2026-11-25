"""Plugin framework: pluggy hookspecs + ordered registry driven by mcp_config.json (Gate 0)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pluggy

from ..config import settings

hookspec = pluggy.HookspecMarker("unreliable")
hookimpl = pluggy.HookimplMarker("unreliable")


@dataclass
class RequestState:
    """Mutable per-request state shared by plugins, loop and subagents."""
    ctx: Any
    raw_len: int = 0
    query: str = ""                    # redacted/normalised query (raw text is never stored)
    route: str = ""
    status: str = "OK"
    answer: str = ""
    blocked: bool = False
    block_reason: str = ""
    pii_found: dict = field(default_factory=dict)
    risk_score: float = 0.0
    quarantined_chunks: list = field(default_factory=list)
    evidence: list = field(default_factory=list)       # [{id, text, note_id, score}]
    faithfulness: float = 1.0
    unsupported: list = field(default_factory=list)
    steps: int = 0
    halted: str = ""
    hitl: dict | None = None
    trace: list = field(default_factory=list)
    idem_keys: dict = field(default_factory=dict)
    breaker_fail: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


class Hookspecs:
    @hookspec
    def pre_execute(self, state: RequestState, session_risk: float):
        """PRE_EXECUTION: sanitise/inspect the query. May set state.blocked."""

    @hookspec
    def chunks_retrieved(self, state: RequestState, chunks: list) -> list:
        """PRE_GENERATION (indirect injection): return the list of chunks to keep."""

    @hookspec
    def tool_dispatch(self, state: RequestState, call: dict) -> dict | None:
        """TOOL_DISPATCH (pre): return a ToolResult-like dict to short-circuit the call."""

    @hookspec
    def tool_result(self, state: RequestState, call: dict, result: dict):
        """TOOL_DISPATCH (post): observe the result."""

    @hookspec
    def post_execute(self, state: RequestState):
        """POST_EXECUTION: audit the final answer; may veto."""


class PluginRegistry:
    def __init__(self, config_path: Path | None = None):
        self.pm = pluggy.PluginManager("unreliable")
        self.pm.add_hookspecs(Hookspecs)
        cfg = json.loads((config_path or settings.root / "mcp_config.json").read_text())
        self.cfg = {p["name"]: p for p in cfg["plugins"]}
        self.plugins: dict[str, Any] = {}
        self.enabled: dict[str, bool] = {}

    def register(self, name: str, plugin: Any):
        conf = self.cfg.get(name, {"name": name, "stage": "PRE_EXECUTION", "priority": 50})
        plugin.name, plugin.conf = name, conf
        self.plugins[name] = plugin
        self.enabled[name] = True
        self.pm.register(plugin, name=name)

    def set_enabled(self, name: str, on: bool):
        self.enabled[name] = on
        pl = self.plugins[name]
        if hasattr(pl, "on_toggle"):
            pl.on_toggle(on)

    def ordered(self, hook: str):
        impls = []
        for name, pl in self.plugins.items():
            if self.enabled.get(name) and hasattr(pl, hook):
                impls.append((self.cfg.get(name, {}).get("priority", 50), name, getattr(pl, hook)))
        return sorted(impls, key=lambda t: t[0])

    def call(self, hook: str, state: RequestState, *args, **kw):
        """Run all enabled plugins in priority order. fail_closed plugin errors block the request."""
        out = None
        for _, name, fn in self.ordered(hook):
            try:
                r = fn(state, *args, **kw)
                if hook == "chunks_retrieved":
                    args = (r,)
                    out = r
                elif r is not None and out is None:
                    out = r
            except Exception as e:  # noqa: BLE001
                if self.cfg.get(name, {}).get("fail_closed"):
                    state.blocked, state.status, state.block_reason = True, "BLOCKED_PLUGIN_ERROR", f"{name} failed closed: {type(e).__name__}"
                    return out
        return out

    def status(self) -> list[dict]:
        return [{"name": n, "stage": self.cfg.get(n, {}).get("stage"), "priority": self.cfg.get(n, {}).get("priority"),
                 "fail_closed": bool(self.cfg.get(n, {}).get("fail_closed")), "enabled": self.enabled[n]} for n in self.plugins]
