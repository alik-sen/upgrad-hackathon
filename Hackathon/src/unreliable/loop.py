"""Bounded ReAct loop: deterministic state machine INIT -> TRIAGE -> REASONING -> TOOL_CALL -> OBSERVATION -> VERIFY -> DONE/HALTED.

Hard limits: max_iterations=5, per_step_timeout, total_loop_budget, and no repeat of an identical successful call without state mutation.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import settings
from .context import compress_observation
from .harness import ToolExecutionHarness, ToolResult
from .plugins.base import RequestState
from .telemetry import tele


@dataclass
class Action:
    tool: str
    args: dict


@dataclass
class Obs:
    step: int
    action: Action
    res: ToolResult
    compressed: str


class Planner(Protocol):
    def next_action(self, obs: list[Obs]) -> Action | None: ...


@dataclass
class LoopResult:
    status: str                      # DONE | HALTED_BREAKER | HALTED_MAX_ITER | HALTED_BUDGET | PENDING_APPROVAL | HALTED_RECURSION
    steps: int
    obs: list[Obs] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


class ReActLoop:
    def __init__(self, harness: ToolExecutionHarness):
        self.h = harness

    async def run(self, state: RequestState, planner: Planner, allowed: set[str], query: str) -> LoopResult:
        t0 = time.perf_counter()
        r = LoopResult("DONE", 0, transitions=["INIT", "TRIAGE"])
        budget = settings.total_loop_budget_ms / 1000
        seen_ok: set[tuple[str, str]] = set()
        for it in range(settings.max_iterations + 1):
            r.transitions.append("REASONING")
            action = planner.next_action(r.obs)
            if action is None:
                break
            if it >= settings.max_iterations:
                r.status = "HALTED_MAX_ITER"; r.transitions.append("HALTED"); break
            remaining = budget - (time.perf_counter() - t0)
            if remaining <= 0.005:
                r.status = "HALTED_BUDGET"; r.transitions.append("HALTED"); break
            r.transitions.append("TOOL_CALL")
            from .harness import TOOLS
            penalty = self.h.host.queue_penalty_ms(TOOLS[action.tool].server) / 1000 if action.tool in TOOLS else 0.0
            step_to = min(settings.per_step_timeout_ms / 1000 + penalty, remaining)   # deadline = base + queueing behind in-flight work, never beyond the loop budget
            ts = time.perf_counter()
            with tele.span("agent.react.step", step=it + 1, tool=action.tool):
                try:
                    res = await asyncio.wait_for(self.h.call(state, action.tool, action.args, allowed, step_to * 1000), step_to)
                except asyncio.TimeoutError:
                    res = ToolResult(False, action.tool, error={"code": -32005, "name": "TOOL_TIMEOUT", "msg": f"step exceeded {step_to*1000:.0f} ms"})
                    # a step timeout counts as a failure for the breaker
                    from .util import args_hash
                    call = {"tool": action.tool, "args_hash": res.args_hash or args_hash(action.tool, action.args)}
                    self.h.plugins.call("tool_result", state, call, res.as_dict())
            r.transitions.append("OBSERVATION")
            comp = compress_observation(res.data if res.ok else res.error, query)
            r.obs.append(Obs(it + 1, action, res, comp))
            r.steps += 1
            state.steps = r.steps
            sig = (action.tool, res.args_hash)
            if res.ok:
                if sig in seen_ok:   # identical successful call with no state mutation -> recursion guard
                    r.status = "HALTED_RECURSION"; r.transitions.append("HALTED"); break
                seen_ok.add(sig)
            if state.halted:
                r.status = "PENDING_APPROVAL" if state.halted == "PENDING_APPROVAL" else state.halted
                r.transitions.append("HALTED"); break
            if state.blocked:
                r.status = "HALTED_BLOCKED"; r.transitions.append("HALTED"); break
        r.transitions.append("VERIFY")
        if not r.status.startswith("HALTED") and r.status != "PENDING_APPROVAL":
            r.transitions.append("DONE")
        r.elapsed_ms = (time.perf_counter() - t0) * 1000
        return r
