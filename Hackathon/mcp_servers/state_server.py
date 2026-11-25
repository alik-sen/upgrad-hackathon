"""mcp-state-store: tenant-isolated inventory + session facts + HITL + hash-chained audit. Streamable HTTP on :8081/mcp.

Defence in depth: tiers are re-checked server-side; Tier-3 tools ONLY create PENDING_APPROVAL records (never execute inline);
`decide_hitl` needs role=approver AND the reviewer key AND reviewer != requester (four-eyes).
"""
import asyncio
import hmac
import sys
import time

import common  # noqa: F401
from mcp.server.mcpserver import MCPServer

from unreliable.config import settings
from unreliable.manifest import build_manifest
from unreliable.models import (AppendAuditIn, ComputeShelfLifeIn, DecideHitlIn, GetFactsIn, ListHitlIn, LookupLotIn, LookupSkuIn, PutFactIn, ReleaseBatchIn,
                               Role, SubmitFormIn, UpdateLotStatusIn, ValidateFormIn, VerifyChainIn)
from unreliable.state import StateError, StateStore

NAME = "state-store"
TOOLS = ["lookup_sku", "lookup_lot", "compute_shelf_life", "validate_form", "update_lot_status", "release_batch", "submit_form",
         "put_fact", "get_facts", "append_audit_batch", "list_hitl", "decide_hitl", "verify_chain"]
store = StateStore()
sem = asyncio.Semaphore(settings.db_pool)  # bulkhead db_pool=128
mcp = MCPServer("mcp-state-store")


async def run(fn, *a, need_t2=False, need_t3=False, ctx=None):
    t0 = time.perf_counter()
    try:
        if ctx is not None:
            if need_t2 and ctx.role == Role.reader:
                raise StateError(-32001, "TIER_DENIED", "role reader may only use TIER_1_READ tools")
            if need_t3 and ctx.role == Role.reader:
                raise StateError(-32001, "TIER_DENIED", "role reader may not request TIER_3 mutations")
        async with sem:
            data = await asyncio.to_thread(fn, *a)
        return {"ok": True, "data": data, "_srv_ms": (time.perf_counter() - t0) * 1000}
    except StateError as e:
        return {"ok": False, "error": {"code": e.code, "name": e.name, "msg": e.msg}, "_srv_ms": (time.perf_counter() - t0) * 1000}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": {"code": -32603, "name": "INTERNAL", "msg": type(e).__name__}, "_srv_ms": (time.perf_counter() - t0) * 1000}


@mcp.tool()
async def lookup_sku(inp: LookupSkuIn) -> dict:
    return await run(store.lookup_sku, inp.ctx.tenant_id, inp.sku)


@mcp.tool()
async def lookup_lot(inp: LookupLotIn) -> dict:
    return await run(store.lookup_lot, inp.ctx.tenant_id, inp.lot)


@mcp.tool()
async def compute_shelf_life(inp: ComputeShelfLifeIn) -> dict:
    return await run(store.compute_shelf_life, inp.ctx.tenant_id, inp.lot, inp.as_of, need_t2=True, ctx=inp.ctx)


@mcp.tool()
async def validate_form(inp: ValidateFormIn) -> dict:
    return await run(store.validate_form, inp.form_type, inp.fields, need_t2=True, ctx=inp.ctx)


@mcp.tool()
async def update_lot_status(inp: UpdateLotStatusIn) -> dict:
    """TIER_3: creates a PENDING_APPROVAL record; never mutates."""
    return await run(store.create_hitl, inp.ctx.tenant_id, inp.ctx.user_id, "update_lot_status",
                     {"lot": inp.lot, "new_status": inp.new_status, "reason": inp.reason}, str(inp.idem_key), need_t3=True, ctx=inp.ctx)


@mcp.tool()
async def release_batch(inp: ReleaseBatchIn) -> dict:
    """TIER_3: creates a PENDING_APPROVAL record; never mutates."""
    return await run(store.create_hitl, inp.ctx.tenant_id, inp.ctx.user_id, "release_batch", {"lot": inp.lot}, str(inp.idem_key), need_t3=True, ctx=inp.ctx)


@mcp.tool()
async def submit_form(inp: SubmitFormIn) -> dict:
    """TIER_3: creates a PENDING_APPROVAL record; never mutates."""
    return await run(store.create_hitl, inp.ctx.tenant_id, inp.ctx.user_id, "submit_form",
                     {"form_type": inp.form_type, "fields": inp.fields}, str(inp.idem_key), need_t3=True, ctx=inp.ctx)


@mcp.tool()
async def put_fact(inp: PutFactIn) -> dict:
    return await run(store.put_fact, inp.ctx.tenant_id, inp.ctx.session_id, inp.fact)


@mcp.tool()
async def get_facts(inp: GetFactsIn) -> dict:
    return await run(store.get_facts, inp.ctx.tenant_id, inp.ctx.session_id, inp.limit)


@mcp.tool()
async def append_audit_batch(inp: AppendAuditIn) -> dict:
    return await run(store.append_audit_batch, inp.tenant_id, inp.events)


@mcp.tool()
async def list_hitl(inp: ListHitlIn) -> dict:
    return await run(store.list_hitl, inp.tenant_id, inp.status)


@mcp.tool()
async def decide_hitl(inp: DecideHitlIn) -> dict:
    if inp.ctx.role != Role.approver or not hmac.compare_digest(inp.reviewer_key, settings.reviewer_api_key):
        return {"ok": False, "error": {"code": -32001, "name": "TIER_DENIED", "msg": "authorised approver key and role required"}, "_srv_ms": 0.0}
    return await run(store.decide_hitl, inp.ctx.tenant_id, inp.approval_id, inp.decision, inp.ctx.user_id)


@mcp.tool()
async def verify_chain(inp: VerifyChainIn) -> dict:
    return await run(store.verify_chain, inp.tenant_id)


@mcp.tool()
def manifest() -> dict:
    return build_manifest(NAME, TOOLS)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    mcp.run("streamable-http", host="127.0.0.1", port=port)
