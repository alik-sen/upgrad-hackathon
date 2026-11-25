"""FastAPI service: POST /api/v1/agent/query (+ /stream), HITL queue, admin/ops endpoints used by the Streamlit workbench."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from .config import settings
from .models import QueryRequest, QueryResponse, Role
from .orchestrator import Orchestrator
from .telemetry import tele


class Decision(BaseModel):
    tenant_id: str
    decision: str


class Toggle(BaseModel):
    enabled: bool


class Incident(BaseModel):
    mode: str


class Lab(BaseModel):
    tenant_id: str = "lab"
    query: str
    k1: float = 1.25
    b: float = 0.75
    rrf_k: int = 60
    threshold: float = 0.42
    pool: int | None = None
    top_k: int = 4


def get_role(x_role: str | None) -> Role:
    try:
        return Role(x_role or "reader")
    except ValueError:
        raise HTTPException(400, "invalid X-Role") from None


def create_app(orchestrator: Orchestrator | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.orch = orchestrator or Orchestrator()
        await app.state.orch.start()
        yield
        await app.state.orch.stop()

    app = FastAPI(title="The Unreliable Assistant — Enterprise", version="1.0", lifespan=lifespan)

    def admin(x_admin_key: str | None = Header(None)):
        if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.reviewer_api_key):
            raise HTTPException(401, "admin key required")

    async def _query(req: QueryRequest, request: Request, x_tenant_id, x_role, traceparent) -> QueryResponse:
        if x_tenant_id and x_tenant_id != req.tenant_id:
            raise HTTPException(403, "tenant mismatch between header and body")
        r = await request.app.state.orch.handle(req, get_role(x_role), traceparent)
        if r.status == "OVERLOADED":
            raise HTTPException(503, "overloaded", headers={"Retry-After": "1"})
        return r

    @app.post("/api/v1/agent/query", response_model=QueryResponse, response_model_exclude_none=True)
    async def query(req: QueryRequest, request: Request, x_tenant_id: str | None = Header(None), x_role: str | None = Header(None), traceparent: str | None = Header(None)):
        return await _query(req, request, x_tenant_id, x_role, traceparent)

    @app.post("/api/v1/agent/query/stream")
    async def query_stream(req: QueryRequest, request: Request, x_tenant_id: str | None = Header(None), x_role: str | None = Header(None), traceparent: str | None = Header(None)):
        t0 = time.perf_counter()
        r = await _query(req, request, x_tenant_id, x_role, traceparent)   # verified before first token (no retractions)

        async def gen():
            words = r.answer.split(" ")
            first = True
            for i in range(0, len(words), 3):
                if first:
                    tele.record("ttft", (time.perf_counter() - t0) * 1000)
                    first = False
                yield f"event: token\ndata: {json.dumps(' '.join(words[i:i+3]) + ' ')}\n\n"
            yield f"event: done\ndata: {r.model_dump_json(exclude_none=True)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/health")
    async def health(request: Request):
        return {"status": "ok", "mcp": [{"server": s["server"], "up": s["up"]} for s in request.app.state.orch.host.status()]}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        return tele.prometheus()

    # ---- HITL ----
    @app.get("/api/v1/hitl")
    async def hitl_list(request: Request, tenant_id: str, status: str | None = None, x_role: str | None = Header(None)):
        if get_role(x_role) == Role.reader:
            raise HTTPException(403, "operator/approver role required")
        r = await request.app.state.orch.host.call("state-store", "list_hitl", {"tenant_id": tenant_id, "status": status})
        return r.get("data", [])


    @app.post("/api/v1/hitl/{approval_id}/decision")
    async def hitl_decide(approval_id: str, body: Decision, request: Request, x_role: str | None = Header(None), x_user_id: str = Header("reviewer"),
                          x_reviewer_key: str = Header("")):
        from .models import ToolCtx
        ctx = ToolCtx(tenant_id=body.tenant_id, user_id=x_user_id, role=get_role(x_role), session_id="hitl")
        if body.decision not in ("approve", "reject"):
            raise HTTPException(400, "decision must be approve|reject")
        r = await request.app.state.orch.host.call("state-store", "decide_hitl", {"ctx": ctx.model_dump(mode="json"), "approval_id": approval_id, "decision": body.decision, "reviewer_key": x_reviewer_key})
        if not r.get("ok"):
            raise HTTPException(403 if r["error"]["name"] == "TIER_DENIED" else 409, r["error"])
        return r["data"]

    # ---- admin / ops ----
    @app.get("/api/v1/admin/status", dependencies=[Depends(admin)])
    async def status(request: Request):
        o = request.app.state.orch
        return {"mcp": o.host.status(), "plugins": o.plugins.status(), "breaker": o.breaker.snapshot(), "breaker_trips": o.breaker.trips, "incident": o.incident,
                "latency": tele.all_stats(), "counters": dict(tele.counters), "cache": {"enabled": o.cache.enabled, "threshold": o.cache.threshold},
                "overrides": o.rag_overrides, "mcp_overhead_p95": sorted(o.host.overhead)[int(len(o.host.overhead) * .95)] if o.host.overhead else 0.0,
                "mcp_serde_p95": sorted(o.host.serde)[int(len(o.host.serde) * .95)] if o.host.serde else 0.0}


    @app.post("/api/v1/admin/plugins/{name}", dependencies=[Depends(admin)])
    async def plugin_toggle(name: str, body: Toggle, request: Request):
        o = request.app.state.orch
        if name not in o.plugins.plugins:
            raise HTTPException(404, "unknown plugin")
        o.plugins.set_enabled(name, body.enabled)
        return o.plugins.status()

    @app.post("/api/v1/admin/breaker/reset", dependencies=[Depends(admin)])
    async def breaker_reset(request: Request):
        request.app.state.orch.breaker.reset()
        return {"ok": True}


    @app.post("/api/v1/admin/incident", dependencies=[Depends(admin)])
    async def incident(body: Incident, request: Request):
        request.app.state.orch.set_incident(body.mode)
        return {"incident": body.mode}

    @app.post("/api/v1/admin/inspect", dependencies=[Depends(admin)])
    async def inspect(body: Toggle, request: Request):
        request.app.state.orch.host.inspect = body.enabled
        return {"inspect": body.enabled}

    @app.get("/api/v1/admin/payloads", dependencies=[Depends(admin)])
    async def payloads(request: Request):
        return list(request.app.state.orch.host.payloads)

    @app.get("/api/v1/admin/traces", dependencies=[Depends(admin)])
    async def traces(request: Request):
        return request.app.state.orch.recent_traces

    @app.get("/api/v1/admin/audit/{tenant_id}", dependencies=[Depends(admin)])
    async def audit(tenant_id: str, request: Request):
        o = request.app.state.orch
        await o._flush()
        v = await o.host.call("state-store", "verify_chain", {"tenant_id": tenant_id})
        return {"chain": v.get("data")}

    @app.post("/api/v1/admin/cache/flush", dependencies=[Depends(admin)])
    async def cache_flush(request: Request, tenant_id: str):
        await request.app.state.orch.cache.purge_tenant(tenant_id)
        return {"ok": True}

    @app.post("/api/v1/admin/cache/enable", dependencies=[Depends(admin)])
    async def cache_enable(body: Toggle, request: Request):
        request.app.state.orch.cache.enabled = body.enabled
        return {"enabled": body.enabled}

    @app.post("/api/v1/admin/telemetry/reset", dependencies=[Depends(admin)])
    async def tele_reset():
        tele.reset()
        return {"ok": True}


    @app.post("/api/v1/admin/rag_lab", dependencies=[Depends(admin)])
    async def rag_lab(body: Lab, request: Request):
        args = {**body.model_dump(exclude_none=True), "debug": True}
        return await request.app.state.orch.host.call("hybrid-rag", "hybrid_retrieve", args, timeout_ms=5000)

    @app.get("/api/v1/admin/session/{tenant_id}/{session_id}", dependencies=[Depends(admin)])
    async def session_view(tenant_id: str, session_id: str, request: Request):
        o = request.app.state.orch
        from .models import ToolCtx
        ctx = ToolCtx(tenant_id=tenant_id, user_id="admin", role=Role.reader, session_id=session_id)
        facts = await o.host.call("state-store", "get_facts", {"ctx": ctx.model_dump(mode="json"), "limit": 20})
        w = o.memory.get(tenant_id, session_id)
        return {"window": w.turns, "facts": facts.get("data", []), "session_risk": o.session_risk.get((tenant_id, session_id), 0.0),
                "cache_counters": {k: v for k, v in tele.counters.items() if k.startswith("cache.")}}

    @app.post("/api/v1/admin/chaos/sku_loop", dependencies=[Depends(admin)])
    async def chaos_sku_loop(request: Request, tenant_id: str = "chaos", sku: str = "SKU-777"):
        return await request.app.state.orch.chaos_sku_loop(tenant_id, sku)

    return app


app = create_app()
