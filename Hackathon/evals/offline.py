"""In-process KPI measurements that need no running stack: PII (KPI-02), triage (04/05), IR metrics (10-13), harness (14/15), chaos loops (16)."""
from __future__ import annotations

import asyncio
import math
import re
import time

from common import ROOT, load_jsonl, norm  # noqa: F401

from unreliable.config import settings
from unreliable.util import percentile


# ------------------------------------------------------------------ KPI-02 PII
def pii_eval() -> dict:
    from unreliable.plugins.pii import fold, redact
    rows = load_jsonl("pii_set.jsonl")
    pos = [r for r in rows if r["pii"]]
    leaks, per_kind = [], {}
    for r in pos:
        out, found = redact(r["text"])
        f = fold(out).lower()
        pii = fold(r["pii"]).lower()
        if r["kind"] in ("email", "email_obf", "email_homoglyph"):
            core = re.split(r"[@\[\(\s]", pii)[0]
            leaked = core in f or not found
        else:
            digits = re.sub(r"\D", "", pii)
            if digits:
                leaked = digits in re.sub(r"\D", "", f) or not found
            else:  # spelled digits: any run of >=5 digit words remaining
                leaked = bool(re.search(r"(?:(?:zero|one|two|three|four|five|six|seven|eight|nine)\W+){5,}", f)) or not found
        k = per_kind.setdefault(r["kind"], [0, 0])
        k[1] += 1
        if leaked:
            k[0] += 1
            leaks.append(r["text"])
    clean = [r for r in rows if not r["pii"]]
    fp = sum(1 for r in clean if redact(r["text"])[1])
    recall = 1 - sum(v[0] for v in per_kind.values()) / len(pos)
    return {"recall": recall, "n": len(pos), "per_kind_leaks": {k: f"{v[0]}/{v[1]}" for k, v in per_kind.items()}, "false_redactions": f"{fp}/{len(clean)}", "leak_examples": leaks[:5]}


# ------------------------------------------------------------------ KPI-04/05 triage
def triage_eval() -> dict:
    from unreliable.triage import TriageAgent
    ta = TriageAgent()
    rows = [r for r in load_jsonl("triage_labeled.jsonl") if r["split"] == "test"]
    ok, lat, conf = 0, [], []
    for r in rows:
        t = ta.classify(r["text"])
        lat.append(t.ms)
        ok += t.route == r["route"]
        if t.route != r["route"]:
            conf.append((r["text"][:70], r["route"], t.route))
    return {"accuracy": ok / len(rows), "n": len(rows), "p95_ms": percentile(lat, 95), "p50_ms": percentile(lat, 50), "errors": conf[:8]}


# ------------------------------------------------------------------ KPI-10..13 IR
async def ir_eval(retriever) -> dict:
    from unreliable.models import HybridRetrieveIn
    gold = [g for g in load_jsonl("golden_set.jsonl") if g["answerable"] and g["split"] == "test"]
    rec50, prec4, mrr, ndcg, rec10 = [], [], [], [], []
    for g in gold:
        gn = set(g["gold_notes"])
        cand = await retriever.stage_candidates(g["question"], "eval", 50)
        fused_notes = [retriever.chunks[i].note_id for i in cand["fused"]]
        rec50.append(len(gn & set(fused_notes)) / len(gn))
        rec10.append(len(gn & set(fused_notes[:10])) / len(gn))
        out = await retriever.retrieve(HybridRetrieveIn(tenant_id="eval", query=g["question"], threshold=0.0))
        notes = [c.note_id for c in out.chunks][:4]
        rel = [1 if n in gn else 0 for n in notes]
        # RAGAS-style context precision@4: rank-weighted precision (average precision over relevant positions)
        hits, ap = 0, 0.0
        for i, r in enumerate(rel, 1):
            if r:
                hits += 1; ap += hits / i
        prec4.append(ap / hits if hits else 0.0)
        mrr.append(next((1 / (i + 1) for i, r in enumerate(rel) if r), 0.0))
        dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rel))
        idcg = sum(1 / math.log2(i + 2) for i in range(min(len(gn), 4)))
        ndcg.append(dcg / idcg if idcg else 0.0)
    m = lambda x: sum(x) / len(x)  # noqa: E731
    return {"recall@50": m(rec50), "recall@10": m(rec10), "precision@4": m(prec4), "mrr@4": m(mrr), "ndcg@4": m(ndcg), "n": len(gold)}


# ------------------------------------------------------------------ KPI-14/15 harness
async def harness_eval() -> dict:
    from unreliable.harness import EXECUTION_ALLOWED, ToolExecutionHarness
    from unreliable.mcp_host import MCPHost
    from unreliable.models import Role, ToolCtx
    from unreliable.plugins.base import PluginRegistry, RequestState
    seen: list[dict] = []

    async def fake(tool, args):
        seen.append(args)
        return {"ok": True, "data": {"sku": args.get("sku"), "lot": args.get("lot")}}
    host = MCPHost(fake={"state-store": fake})
    pr = PluginRegistry()
    h = ToolExecutionHarness(host, pr)
    calls = load_jsonl("tool_calls.jsonl")
    first_ok = 0
    typo = [c for c in calls if c["expect_repair"]]
    garbage = [c for c in calls if not c["expect_repair"] and not re.fullmatch(r"(SKU-\d{3}|LOT-\d{5})", next(iter(c["args"].values())))]
    repaired_ok = 0
    garbage_rejected = 0
    for c in calls:
        st = RequestState(ctx=ToolCtx(tenant_id="eval", user_id="u", role=Role.operator, session_id="s"))
        n0 = len(seen)
        r = await h.call(st, c["tool"], c["args"], EXECUTION_ALLOWED)
        first_ok += r.first_pass_valid
        if c["expect_repair"]:
            k = "sku" if c["tool"] == "lookup_sku" else "lot"
            repaired_ok += bool(r.ok and r.repaired and len(seen) > n0 and seen[-1].get(k) == c["expect_repair"])
        elif c in garbage:
            garbage_rejected += (not r.ok) and len(seen) == n0
    return {"first_pass_rate": first_ok / len(calls), "n": len(calls), "repair_success": repaired_ok / len(typo), "typo_calls": len(typo),
            "garbage_rejected": f"{garbage_rejected}/{len(garbage)}"}


# ------------------------------------------------------------------ KPI-16 chaos loops
async def chaos_eval(runs_per_scenario: int = 8) -> dict:
    from unreliable.harness import EXECUTION_ALLOWED, ToolExecutionHarness
    from unreliable.loop import Action, ReActLoop
    from unreliable.mcp_host import MCPCallError, MCPHost
    from unreliable.models import Role, ToolCtx
    from unreliable.plugins.base import PluginRegistry, RequestState
    from unreliable.plugins.breaker import CircuitBreaker

    class Stub:
        def __init__(self, seq): self.seq, self.i = seq, 0
        def next_action(self, obs):
            a = self.seq[self.i % len(self.seq)]; self.i += 1
            return a if self.i <= 50 else None   # a runaway agent would never stop on its own

    async def f_notfound(tool, args):
        return {"ok": False, "error": {"code": -32002, "name": "NOT_FOUND", "msg": "x"}}

    async def f_err(tool, args):
        return {"ok": False, "error": {"code": -32603, "name": "INTERNAL", "msg": "x"}}

    async def f_timeout(tool, args):
        await asyncio.sleep(30)

    async def f_mcp_timeout(tool, args):
        raise MCPCallError(-32005, "TOOL_TIMEOUT", "down")

    scen = {
        "C1 stubborn unknown SKU": (f_notfound, lambda: Stub([Action("lookup_sku", {"sku": "SKU-777"})])),
        "C2 unrepairable arg": (f_notfound, lambda: Stub([Action("lookup_lot", {"lot": "banana"})])),
        "C3 always-error tool": (f_err, lambda: Stub([Action("lookup_sku", {"sku": "SKU-001"})])),
        "C4 hung tool (timeouts)": (f_timeout, lambda: Stub([Action("lookup_lot", {"lot": "LOT-00001"})])),
        "C5 flapping args (unique hashes)": (f_notfound, lambda: Stub([Action("lookup_sku", {"sku": f"SKU-{700 + i:03d}"}) for i in range(60)])),
        "C6 oscillating plan A/B": (f_err, lambda: Stub([Action("lookup_sku", {"sku": "SKU-001"}), Action("lookup_lot", {"lot": "LOT-00001"})])),
        "C7 server down (MCP error)": (f_mcp_timeout, lambda: Stub([Action("lookup_sku", {"sku": "SKU-002"})])),
    }
    res, escapes, total = {}, 0, 0
    budget_ms = settings.total_loop_budget_ms
    for name, (fake, mk) in scen.items():
        outs = []
        for _ in range(runs_per_scenario):
            host = MCPHost(fake={"state-store": fake})
            pr = PluginRegistry()
            br = CircuitBreaker(2)
            pr.register("circuit_breaker", br)
            loop = ReActLoop(ToolExecutionHarness(host, pr))
            st = RequestState(ctx=ToolCtx(tenant_id="eval", user_id="u", role=Role.operator, session_id="s"))
            lr = await loop.run(st, mk(), EXECUTION_ALLOWED, "q")
            ok = lr.status.startswith("HALTED") and lr.steps <= settings.max_iterations and lr.elapsed_ms <= budget_ms + 400
            outs.append((lr.status, lr.steps, round(lr.elapsed_ms), ok))
            escapes += ok; total += 1
        res[name] = {"status": outs[0][0], "steps": outs[0][1], "elapsed_ms": outs[0][2], "all_escaped": all(o[3] for o in outs)}
    return {"escape_rate": escapes / total, "runs": total, "scenarios": res}
