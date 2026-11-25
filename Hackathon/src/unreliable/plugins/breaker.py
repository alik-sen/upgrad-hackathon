"""Idempotent circuit breaker (TOOL_DISPATCH, priority 10).

Trips when the same (tool, canonical_args_hash) fails >= max_identical times (default 2): the loop transitions to HALTED_BREAKER and the
user gets a structured remediation prompt. Also keeps a short-lived *open* registry (tenant-scoped) so repeated hopeless calls fail fast;
the Streamlit workbench can reset it.
"""
from __future__ import annotations

import time

from .base import hookimpl


class CircuitBreaker:
    OPEN_SECONDS = 30.0

    def __init__(self, max_identical: int = 2):
        self.max = max_identical
        self.open: dict[tuple[str, str, str], float] = {}
        self.trips = 0

    @hookimpl
    def tool_dispatch(self, state, call):
        key = (state.ctx.tenant_id, call["tool"], call["args_hash"])
        if state.breaker_fail.get((call["tool"], call["args_hash"]), 0) >= self.max:
            return self._trip(state, call, key)
        t = self.open.get(key)
        if t and t > time.time():
            return self._trip(state, call, key)
        return None

    def _trip(self, state, call, key):
        self.open[key] = time.time() + self.OPEN_SECONDS
        self.trips += 1
        state.halted = "HALTED_BREAKER"
        return {"ok": False, "error": {"code": -32003, "name": "BREAKER_OPEN", "msg": f"{call['tool']} failed repeatedly with identical arguments"}, "breaker": True}

    @hookimpl
    def tool_result(self, state, call, result):
        k = (call["tool"], call["args_hash"])
        if result.get("ok"):
            state.breaker_fail.pop(k, None)
        else:
            state.breaker_fail[k] = state.breaker_fail.get(k, 0) + 1
            if state.breaker_fail[k] >= self.max:   # trip right after the 2nd identical failure
                err = (result.get("error") or {}).get("name")
                if err not in ("TOOL_TIMEOUT", "INTERNAL"):     # capacity/transient failures end *this* loop but must not block other users for 30 s
                    self.open[(state.ctx.tenant_id, call["tool"], call["args_hash"])] = time.time() + self.OPEN_SECONDS
                self.trips += 1
                state.halted = "HALTED_BREAKER"
                state.meta["breaker_tool"] = call["tool"]

    def reset(self, tenant_id: str | None = None):
        for k in [k for k in self.open if tenant_id is None or k[0] == tenant_id]:
            self.open.pop(k, None)

    def snapshot(self) -> list[dict]:
        now = time.time()
        return [{"tenant": k[0], "tool": k[1], "args_hash": k[2], "reopens_in_s": round(v - now, 1)} for k, v in self.open.items() if v > now]
