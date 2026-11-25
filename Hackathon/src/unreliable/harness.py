"""ToolExecutionHarness (Gate 4): every MCP tool call passes
(1) Pydantic v2 strict validation -> (2) deterministic regex auto-repair -> (3) RBAC tier + subagent allow-list -> (4) UUIDv4 idempotency
-> plugin hooks (circuit breaker, HITL gate) -> MCP dispatch -> result hooks."""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .config import settings
from .mcp_host import MCPCallError, MCPHost
from .models import (ROLE_ALLOWED_TIERS, ComputeShelfLifeIn, EmbedIn, ExpandIn, HybridRetrieveIn, LookupLotIn, LookupSkuIn, ReleaseBatchIn, SubmitFormIn,
                     Tier, ToolCtx, ToolSpec, UpdateLotStatusIn, ValidateFormIn)
from .plugins.base import PluginRegistry, RequestState
from .telemetry import tele
from .util import LOT_LOOSE, SKU_LOOSE, args_hash

TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec(name="hybrid_retrieve", server="hybrid-rag", tier=Tier.T1, model=HybridRetrieveIn),
    ToolSpec(name="embed_query", server="hybrid-rag", tier=Tier.T1, model=EmbedIn),
    ToolSpec(name="expand_subgraph", server="graphify-vault", tier=Tier.T1, model=ExpandIn),
    ToolSpec(name="lookup_sku", server="state-store", tier=Tier.T1, model=LookupSkuIn),
    ToolSpec(name="lookup_lot", server="state-store", tier=Tier.T1, model=LookupLotIn),
    ToolSpec(name="compute_shelf_life", server="state-store", tier=Tier.T2, model=ComputeShelfLifeIn),
    ToolSpec(name="validate_form", server="state-store", tier=Tier.T2, model=ValidateFormIn),
    ToolSpec(name="update_lot_status", server="state-store", tier=Tier.T3, model=UpdateLotStatusIn, needs_idem=True),
    ToolSpec(name="release_batch", server="state-store", tier=Tier.T3, model=ReleaseBatchIn, needs_idem=True),
    ToolSpec(name="submit_form", server="state-store", tier=Tier.T3, model=SubmitFormIn, needs_idem=True),
]}
CTX_TOOLS = {n for n, t in TOOLS.items() if "ctx" in t.model.model_fields}
RETRIEVAL_ALLOWED = {"hybrid_retrieve", "embed_query", "expand_subgraph"}                       # read-only; no state-mutating tools
EXECUTION_ALLOWED = {"lookup_sku", "lookup_lot", "compute_shelf_life", "validate_form", "update_lot_status", "release_batch", "submit_form"}  # no RAG / web


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: Any = None
    error: dict | None = None
    repaired: bool = False
    first_pass_valid: bool = True
    args_hash: str = ""
    ms: float = 0.0
    breaker: bool = False

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def repair_args(args: dict) -> tuple[dict, bool]:
    """Deterministic regex auto-repair: SKU99->SKU-099, 'lot 44'->LOT-00044. Only fully-matching string values are touched."""
    out, changed = dict(args), False
    for k, v in args.items():
        if not isinstance(v, str):
            continue
        s = v.strip()
        m = SKU_LOOSE.fullmatch(s)
        if m and k == "sku":
            nv = f"SKU-{int(m.group(1)):03d}"
        else:
            m = LOT_LOOSE.fullmatch(s)
            nv = f"LOT-{int(m.group(1)):05d}" if (m and k == "lot") else v
        if nv != v:
            out[k], changed = nv, True
    return out, changed


class ToolExecutionHarness:
    def __init__(self, host: MCPHost, plugins: PluginRegistry):
        self.host, self.plugins = host, plugins

    def _build(self, spec: ToolSpec, ctx: ToolCtx, args: dict):
        full = dict(args)
        if "ctx" in spec.model.model_fields:
            full["ctx"] = ctx
        if "tenant_id" in spec.model.model_fields and "tenant_id" not in full:
            full["tenant_id"] = ctx.tenant_id
        return full

    async def call(self, state: RequestState, tool: str, args: dict, allowed: set[str], timeout_ms: float | None = None) -> ToolResult:
        t0 = time.perf_counter()
        spec = TOOLS.get(tool)
        ctx: ToolCtx = state.ctx
        if spec is None or tool not in allowed:
            return ToolResult(False, tool, error={"code": -32001, "name": "TIER_DENIED", "msg": f"{tool} not permitted for this subagent"})
        tele.inc("harness.calls")
        fixed, changed = repair_args(args)                      # pure + deterministic -> canonical hash is stable across typos
        ah = args_hash(tool, fixed)
        call = {"tool": tool, "args_hash": ah, "tier": spec.tier.value, "server": spec.server, "mode": "execute"}

        def done(res: ToolResult, hooks: bool = True) -> ToolResult:
            res.args_hash, res.ms = ah, (time.perf_counter() - t0) * 1000
            if hooks:
                self.plugins.call("tool_result", state, call, res.as_dict())
            return res

        # plugins (circuit breaker, HITL gate) run first so even invalid-argument retries are counted
        short = self.plugins.call("tool_dispatch", state, call)
        if state.blocked:
            return done(ToolResult(False, tool, error={"code": -32007, "name": "HITL_REQUIRED", "msg": state.block_reason}), hooks=False)
        if short:
            return done(ToolResult(False, tool, error=short["error"], breaker=bool(short.get("breaker"))), hooks=False)
        # (1) strict validation
        full = self._build(spec, ctx, args)
        if spec.needs_idem:
            full["idem_key"] = state.idem_keys.setdefault(ah, uuid.uuid4())  # (4) UUIDv4, stable per logical call so retries replay
            assert full["idem_key"].version == 4
        first_ok, repaired = True, False
        try:
            spec.model.model_validate(full, strict=True)
            tele.inc("harness.first_pass_valid")
        except ValidationError as e:
            first_ok = False
            tele.inc("harness.first_pass_invalid")
            tele.inc("harness.repair_attempt")                  # (2) deterministic regex auto-repair
            full2 = self._build(spec, ctx, fixed)
            if spec.needs_idem:
                full2["idem_key"] = full["idem_key"]
            try:
                if not changed:
                    raise e
                spec.model.model_validate(full2, strict=True)
                full, repaired = full2, True
                tele.inc("harness.repair_ok")
            except ValidationError as e2:
                err = e2.errors()[0]
                return done(ToolResult(False, tool, first_pass_valid=False,
                                       error={"code": -32602, "name": "INVALID_PARAMS", "msg": f"{'.'.join(map(str, err['loc']))}: {err['msg']}"[:200]}))
        # (3) RBAC tier
        if spec.tier not in ROLE_ALLOWED_TIERS[ctx.role]:
            return done(ToolResult(False, tool, repaired=repaired, first_pass_valid=first_ok,
                                   error={"code": -32001, "name": "TIER_DENIED", "msg": f"role {ctx.role.value} may not call {spec.tier.value}"}))
        payload = spec.model.model_validate(full, strict=True).model_dump(mode="json")
        try:
            raw = await self.host.call(spec.server, tool, payload, timeout_ms)
            if isinstance(raw, dict) and "ok" in raw and ("data" in raw or "error" in raw):   # state-store envelope
                res = ToolResult(bool(raw["ok"]), tool, data=raw.get("data"), error=raw.get("error"))
            else:
                res = ToolResult(True, tool, data=raw)
        except MCPCallError as e:
            res = ToolResult(False, tool, error={"code": e.code, "name": e.name, "msg": e.msg})
        res.repaired, res.first_pass_valid = repaired, first_ok
        return done(res)
