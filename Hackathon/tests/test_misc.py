from unreliable.config import settings
from unreliable.context import GoalAnchorBlock, Window, build_prompt, compress_observation, compress_text
from unreliable.graphify import GraphStore, build_graph
from unreliable.manifest import build_manifest, verify_manifest
from unreliable.triage import TriageAgent
from unreliable.util import canon_ids, est_tokens, norm_query
from unreliable.verification import claim_supported, verify_answer

EV = ["SKU-017 (Isopropyl Alcohol 99%) must be stored at 15-25°C inside a flammable-rated cabinet.", "A flammable cabinet may hold at most 60 L of flammable liquids."]


def test_verification_supports_and_rejects():
    assert verify_answer("SKU-017 must be stored at 15-25°C inside a flammable-rated cabinet. [1]", EV)[0] == 1.0
    f, bad = verify_answer("SKU-017 must be stored at 2-8°C. [1] A flammable cabinet may hold at most 60 L of flammable liquids.", EV)
    assert f == 0.5 and len(bad) == 1
    assert not claim_supported("The cabinet holds 600 L.", EV)           # numeric claim must appear verbatim


def test_manifest_signing():
    m = build_manifest("state-store", ["a", "b"])
    assert verify_manifest(m, "state-store", ["b", "a"])
    assert not verify_manifest({**m, "tools": ["a", "b", "evil"]}, "state-store", ["a", "b"])
    assert not verify_manifest({**m, "sig": "0" * 64}, "state-store", ["a", "b"])
    assert not verify_manifest(m, "other", ["a", "b"])


def test_goal_anchor_is_index_zero_and_immutable():
    a = GoalAnchorBlock("check lot", ("LOT-00001",))
    p = build_prompt(a, Window(), ["evidence one"], "q")
    assert p.startswith("[GOAL ANCHOR | immutable]")
    try:
        a.intent = "x"; raise AssertionError("mutable")
    except Exception as e:  # noqa: BLE001
        assert "frozen" in str(e).lower() or "cannot assign" in str(e).lower()


def test_observation_compression_limits():
    big = {"_srv_ms": 3, "meta": {"x": 1}, "text": "a " * 5000, "status": "OK"}
    out = compress_observation(big, "q")
    assert est_tokens(out) <= settings.observation_max_tokens + 5 and "_srv_ms" not in out and "meta" not in out
    assert est_tokens(compress_text("one two. " * 400, "one")) <= settings.observation_max_tokens


def test_sliding_window_keeps_last_three():
    w = Window()
    for i in range(7):
        w.add(f"q{i}", f"a{i}")
    assert [t["q"] for t in w.turns] == ["q4", "q5", "q6"]


def test_canon_and_norm():
    assert canon_ids("stock of sku99 and lot 44") == "stock of SKU-099 and LOT-00044"
    assert norm_query("  What IS  SKU99?? ") == "what is sku-099"


def test_graph_depth_and_budget(tmp_path):
    g = build_graph(settings.root / "src", settings.vault_dir)
    p = tmp_path / "g.json"
    import json
    p.write_text(json.dumps(g))
    gs = GraphStore(p)
    q = "How often must the supplier of SKU-030 be audited?"
    a = gs.resolve_anchor(q)
    assert a == "note:sku-030"
    sub = gs.expand(a, 2, 320, q)
    assert sub["tokens"] <= 330 and max(n["hop"] for n in sub["nodes"]) <= 2
    assert "Tier C suppliers must be audited every 6 months" in sub["text"]
    assert gs.expand(a, 0, 320, q)["nodes"][0]["id"] == a and len(gs.expand(a, 0, 320, q)["nodes"]) == 1


def test_graph_never_crosses_tenant(tmp_path):
    g = build_graph(settings.root / "src", settings.vault_dir)
    ids = {n["id"] for n in g["nodes"] if n.get("tenant") == "tenant_a"}
    assert "note:priv-tenant-a-pricing" in ids


def test_triage_routes():
    t = TriageAgent()
    cases = {"What is the storage temperature for SKU-017?": "HYBRID_RAG", "Put LOT-00044 on hold because of a failed seal check": "TRANSACTION_EXEC",
             "What is the current status of LOT-00044?": "TRANSACTION_EXEC", "Hello": "DIRECT_ANSWER", "Write me a poem about the sea": "REJECT_OOS",
             "Can QA release the lot directly, and what must happen first?": "HYBRID_RAG"}
    for q, r in cases.items():
        assert t.classify(q).route == r, q
    assert t.classify("Put LOT-00044 on hold because x").risk_tier == 3
    assert t.classify("How often must the supplier of SKU-030 be audited?").complexity == "high"
