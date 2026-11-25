import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from unreliable.mcp_host import MCPHost  # noqa: E402
from unreliable.models import Role, ToolCtx  # noqa: E402
from unreliable.plugins.base import PluginRegistry, RequestState  # noqa: E402
from unreliable.plugins.breaker import CircuitBreaker  # noqa: E402
from unreliable.plugins.hitl import HITLGate  # noqa: E402
from unreliable.state import StateStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.db")


def make_state(role=Role.operator, tenant="t1", user="u1"):
    return RequestState(ctx=ToolCtx(tenant_id=tenant, user_id=user, role=role, session_id="s1"))


@pytest.fixture
def mkstate():
    return make_state


@pytest.fixture
def plugins():
    pr = PluginRegistry()
    pr.register("circuit_breaker", CircuitBreaker(2))
    pr.register("hitl_gate", HITLGate())
    return pr


def fake_host(store: StateStore):
    """MCPHost whose state-store 'server' is a real StateStore (no sockets) — exercises the same envelope contract."""
    calls = []

    async def state(tool, args):
        calls.append((tool, args))
        from unreliable.state import StateError
        try:
            ctx = args.get("ctx", {})
            tenant = ctx.get("tenant_id")
            if tool == "lookup_sku":
                return {"ok": True, "data": store.lookup_sku(tenant, args["sku"])}
            if tool == "lookup_lot":
                return {"ok": True, "data": store.lookup_lot(tenant, args["lot"])}
            if tool in ("update_lot_status", "release_batch", "submit_form"):
                p = {k: v for k, v in args.items() if k not in ("ctx", "idem_key")}
                return {"ok": True, "data": store.create_hitl(tenant, ctx["user_id"], tool, p, args["idem_key"])}
        except StateError as e:
            return {"ok": False, "error": {"code": e.code, "name": e.name, "msg": e.msg}}
        return {"ok": False, "error": {"code": -32601, "name": "NO_SUCH_TOOL", "msg": tool}}
    h = MCPHost(fake={"state-store": state})
    h.calls = calls
    return h
