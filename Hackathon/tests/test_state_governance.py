import pytest

from unreliable.state import GENESIS, StateError


def test_tenant_isolation(store):
    store.ensure_tenant("a"); store.ensure_tenant("b")
    store.create_hitl("a", "u", "update_lot_status", {"lot": "LOT-00001", "new_status": "HOLD", "reason": "x"}, "11111111-1111-4111-8111-111111111111")
    assert len(store.list_hitl("a")) == 1 and store.list_hitl("b") == []
    aid = store.list_hitl("a")[0]["approval_id"]
    with pytest.raises(StateError):
        store.decide_hitl("b", aid, "approve", "qa")           # other tenant cannot touch it


def test_t3_never_executes_without_approval_and_four_eyes(store):
    before = store.lookup_lot("t", "LOT-00001")["status"]
    r = store.create_hitl("t", "alice", "update_lot_status", {"lot": "LOT-00001", "new_status": "SCRAP", "reason": "bad"}, "22222222-2222-4222-8222-222222222222")
    assert r["status"] == "PENDING_APPROVAL" and r["executed"] is False
    assert store.lookup_lot("t", "LOT-00001")["status"] == before
    with pytest.raises(StateError):
        store.decide_hitl("t", r["approval_id"], "approve", "alice")   # requester cannot approve own request
    assert store.decide_hitl("t", r["approval_id"], "approve", "bob")["executed"] is True
    assert store.lookup_lot("t", "LOT-00001")["status"] == "SCRAP"
    with pytest.raises(StateError):
        store.decide_hitl("t", r["approval_id"], "approve", "carol")    # no double execution


def test_idempotent_replay(store):
    k = "33333333-3333-4333-8333-333333333333"
    a = store.create_hitl("t", "u", "release_batch", {"lot": "LOT-00002"}, k)
    b = store.create_hitl("t", "u", "release_batch", {"lot": "LOT-00002"}, k)
    assert a["approval_id"] == b["approval_id"] and b.get("replayed") and len(store.list_hitl("t")) == 1


def test_hash_chain_detects_tamper(store):
    for i in range(5):
        store.append_audit("t", {"i": i, "q": "h"})
    assert store.verify_chain("t")["ok"]
    c = store._conn()
    c.execute("UPDATE audit_chain SET payload='{\"i\":99}' WHERE tenant_id='t' AND seq=3")
    r = store.verify_chain("t")
    assert not r["ok"] and r["broken_at"] == 3


def test_chain_is_per_tenant_and_starts_at_genesis(store):
    store.append_audit("x", {"a": 1}); store.append_audit("y", {"a": 1})
    assert store._conn().execute("SELECT prev_hash FROM audit_chain WHERE tenant_id='x' AND seq=1").fetchone()[0] == GENESIS


def test_release_blocked_lot_cannot_be_released(store):
    store._conn().execute("UPDATE lots SET status='QUARANTINE' WHERE lot='LOT-00005' AND tenant_id='t'") if False else None
    store.ensure_tenant("t")
    store._conn().execute("UPDATE lots SET status='QUARANTINE' WHERE lot='LOT-00005' AND tenant_id='t'")
    r = store.create_hitl("t", "u", "release_batch", {"lot": "LOT-00005"}, "44444444-4444-4444-8444-444444444444")
    with pytest.raises(StateError):
        store.decide_hitl("t", r["approval_id"], "approve", "qa")       # SOP-LR-06: hold must be cleared first
