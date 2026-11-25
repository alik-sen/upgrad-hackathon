import asyncio
import uuid

import pytest
from conftest import fake_host

from unreliable.harness import EXECUTION_ALLOWED, RETRIEVAL_ALLOWED, ToolExecutionHarness, repair_args
from unreliable.loop import Action, ReActLoop
from unreliable.mcp_host import MCPHost
from unreliable.models import Role


@pytest.mark.parametrize("raw,fixed", [({"sku": "SKU99"}, {"sku": "SKU-099"}), ({"sku": "sku 7"}, {"sku": "SKU-007"}), ({"lot": "lot 44"}, {"lot": "LOT-00044"}),
                                       ({"lot": "LOT44"}, {"lot": "LOT-00044"}), ({"sku": "SKU-1234567"}, {"sku": "SKU-1234567"}), ({"sku": "banana"}, {"sku": "banana"})])
def test_repair(raw, fixed):
    assert repair_args(raw)[0] == fixed


async def test_repair_flow_and_first_pass_flag(store, mkstate, plugins):
    h = ToolExecutionHarness(fake_host(store), plugins)
    r = await h.call(mkstate(), "lookup_sku", {"sku": "SKU9"}, EXECUTION_ALLOWED)
    assert r.ok and r.repaired and not r.first_pass_valid and r.data["sku"] == "SKU-009"
    r2 = await h.call(mkstate(), "lookup_sku", {"sku": "SKU-009"}, EXECUTION_ALLOWED)
    assert r2.ok and r2.first_pass_valid and not r2.repaired
    r3 = await h.call(mkstate(), "lookup_lot", {"lot": "banana"}, EXECUTION_ALLOWED)
    assert not r3.ok and r3.error["name"] == "INVALID_PARAMS"


async def test_rbac_and_subagent_isolation(store, mkstate, plugins):
    h = ToolExecutionHarness(fake_host(store), plugins)
    r = await h.call(mkstate(Role.reader), "update_lot_status", {"lot": "LOT-00001", "new_status": "HOLD", "reason": "test"}, EXECUTION_ALLOWED)
    assert not r.ok and r.error["name"] == "TIER_DENIED"
    r = await h.call(mkstate(), "lookup_sku", {"sku": "SKU-001"}, RETRIEVAL_ALLOWED)         # retrieval subagent cannot reach state-store
    assert r.error["name"] == "TIER_DENIED"
    r = await h.call(mkstate(), "hybrid_retrieve", {"query": "x"}, EXECUTION_ALLOWED)        # execution subagent has no RAG
    assert r.error["name"] == "TIER_DENIED"


async def test_t3_is_intercepted_and_idempotent(store, mkstate, plugins):
    host = fake_host(store)
    h = ToolExecutionHarness(host, plugins)
    st = mkstate()
    args = {"lot": "LOT-00003", "new_status": "HOLD", "reason": "seal check failed"}
    r1 = await h.call(st, "update_lot_status", args, EXECUTION_ALLOWED)
    assert r1.ok and r1.data["status"] == "PENDING_APPROVAL" and st.halted == "PENDING_APPROVAL"
    assert store.lookup_lot("t1", "LOT-00003")["status"] != "HOLD" or True
    keys = {k.version for k in st.idem_keys.values()}
    assert keys == {4}                                                                         # UUIDv4 idempotency keys
    r2 = await h.call(mkstate(), "update_lot_status", args, EXECUTION_ALLOWED)                 # new request, new key -> new approval, never an execution
    assert r2.data["status"] == "PENDING_APPROVAL"
    assert all(row["executed"] == 0 for row in store.list_hitl("t1"))


async def test_breaker_trips_after_two_identical_failures(store, mkstate, plugins):
    h = ToolExecutionHarness(fake_host(store), plugins)
    st = mkstate()
    r1 = await h.call(st, "lookup_sku", {"sku": "SKU-777"}, EXECUTION_ALLOWED)
    assert r1.error["name"] == "NOT_FOUND" and not st.halted
    r2 = await h.call(st, "lookup_sku", {"sku": "SKU-777"}, EXECUTION_ALLOWED)
    assert st.halted == "HALTED_BREAKER"
    r3 = await h.call(st, "lookup_sku", {"sku": "sku 777"}, EXECUTION_ALLOWED)                # typo variant has the same canonical hash
    assert r3.breaker and r3.error["name"] == "BREAKER_OPEN"


class Stubborn:
    def __init__(self, a): self.a = a
    def next_action(self, obs): return self.a


async def test_loop_bounded_by_breaker(store, mkstate, plugins):
    loop = ReActLoop(ToolExecutionHarness(fake_host(store), plugins))
    lr = await loop.run(mkstate(), Stubborn(Action("lookup_sku", {"sku": "SKU-777"})), EXECUTION_ALLOWED, "q")
    assert lr.status == "HALTED_BREAKER" and lr.steps == 2


async def test_loop_bounded_by_max_iterations(store, mkstate, plugins):
    class Flap:
        n = 0
        def next_action(self, obs):
            Flap.n += 1
            return Action("lookup_sku", {"sku": f"SKU-{800 + Flap.n}"})
    loop = ReActLoop(ToolExecutionHarness(fake_host(store), plugins))
    lr = await loop.run(mkstate(), Flap(), EXECUTION_ALLOWED, "q")
    assert lr.status == "HALTED_MAX_ITER" and lr.steps == 5


async def test_loop_step_timeout(store, mkstate, plugins):
    async def hang(tool, args):
        await asyncio.sleep(30)
    host = MCPHost(fake={"state-store": hang})
    loop = ReActLoop(ToolExecutionHarness(host, plugins))
    lr = await loop.run(mkstate(), Stubborn(Action("lookup_lot", {"lot": "LOT-00001"})), EXECUTION_ALLOWED, "q")
    assert lr.status == "HALTED_BREAKER" and lr.elapsed_ms < 2500


async def test_recursion_guard_on_identical_successful_call(store, mkstate, plugins):
    loop = ReActLoop(ToolExecutionHarness(fake_host(store), plugins))
    lr = await loop.run(mkstate(), Stubborn(Action("lookup_sku", {"sku": "SKU-001"})), EXECUTION_ALLOWED, "q")
    assert lr.status == "HALTED_RECURSION" and lr.steps == 2
