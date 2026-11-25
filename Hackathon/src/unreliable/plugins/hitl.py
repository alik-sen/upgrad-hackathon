"""Tier-3 HITL gate (TOOL_DISPATCH, priority 50, fail-closed): T3 calls are *never* executed inline.

The call is rewritten into a PENDING_APPROVAL request; the loop halts. Execution only happens via decide_hitl with an authorised reviewer.
"""
from __future__ import annotations

from .base import hookimpl


class HITLGate:
    @hookimpl
    def tool_dispatch(self, state, call):
        if call.get("tier") == "TIER_3_MUTATE":
            call["mode"] = "request"          # server-side tools only create a PENDING_APPROVAL record
            state.meta["hitl_intercepts"] = state.meta.get("hitl_intercepts", 0) + 1
        return None

    @hookimpl
    def tool_result(self, state, call, result):
        if call.get("tier") == "TIER_3_MUTATE":
            d = result.get("data") or {}
            if result.get("ok") and d.get("status") == "PENDING_APPROVAL":
                state.hitl = d
                state.halted = "PENDING_APPROVAL"
            elif result.get("ok"):  # an executed T3 would be a policy violation: fail closed
                state.blocked, state.status, state.block_reason = True, "BLOCKED_PLUGIN_ERROR", "T3 executed without approval"
