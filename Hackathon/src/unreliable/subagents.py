"""Triage-routed specialised subagents with isolated tool allow-lists (Gate 6/7).

Retrieval   : hybrid-rag + graphify-vault, read-only (cannot call state-mutating tools).
Execution   : state-store only; no RAG / web; Tier-3 => HITL freeze.
Verification: independent context; claim<->chunk check, may VETO an ungrounded draft.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import settings
from .context import GoalAnchorBlock, SessionMemory, build_prompt, compress_text
from .generator import ABSTAIN_TEXT, Answer, Evidence, ExtractiveGenerator
from .harness import EXECUTION_ALLOWED, RETRIEVAL_ALLOWED, ToolExecutionHarness
from .loop import Action, LoopResult, Obs, ReActLoop
from .plugins.base import RequestState
from .util import LOT_LOOSE, SKU_LOOSE, canon_ids, est_tokens, extract_entities
from .verification import verify_answer

import os
UNSUPPORTED_TERMS_GATE = os.environ.get("UNSUPPORTED_TERMS_GATE", "0") == "1"   # off by default: costs ~10 pts of recall (blocks generic words like "kept"/"company") for one near-miss negative
RETRYABLE = {"INTERNAL"}   # timeouts are NOT retried: cancelled work keeps running server-side, a retry doubles load (metastable overload)


class RetrievalPlanner:
    """Step 1 hybrid_retrieve; step 2 (only if needed: multi-hop / reasoning / summary) expand_subgraph. Retries only retryable errors, once."""
    def __init__(self, query: str, tenant: str, qvec, complexity: str, summary: bool, overrides: dict | None = None):
        self.q, self.t, self.qvec, self.cx, self.summary = query, tenant, qvec, complexity, summary
        self.ov = overrides or {}

    def next_action(self, obs: list[Obs]) -> Action | None:
        if not obs:
            a = {"query": self.q, **self.ov}
            if self.qvec is not None:
                a["query_vec"] = [float(x) for x in self.qvec]
            return Action("hybrid_retrieve", a)
        last = obs[-1]
        if not last.res.ok:
            n_fail = sum(1 for o in obs if not o.res.ok)
            if last.res.error and last.res.error.get("name") in RETRYABLE and n_fail < 2:
                return last.action
            return None
        if last.action.tool == "hybrid_retrieve":
            d = last.res.data
            if d.get("abstain") or not d.get("chunks"):
                return None
            if self.cx != "low" or self.summary:
                return Action("expand_subgraph", {"query": self.q, "tenant_id": self.t, "hint_note": d["chunks"][0]["note_id"], "depth": 2, "budget_tokens": 320})
        return None


@dataclass
class RetrievalOutcome:
    answer: Answer
    evidence: list[Evidence]
    abstain: bool
    reason: str
    loop: LoopResult
    prompt: str = ""
    chunks: list[dict] = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    graph_tokens: int = 0


class RetrievalSubagent:
    def __init__(self, harness: ToolExecutionHarness, plugins, generator: ExtractiveGenerator, memory: SessionMemory):
        self.h, self.plugins, self.gen, self.mem = harness, plugins, generator, memory
        self.loop = ReActLoop(harness)
        from .rag.bm25 import tokenize
        from .rag.chunker import load_vault
        self.vocab = {t for n in load_vault(settings.vault_dir) for t in tokenize(n.title + " " + n.body)}

    async def run(self, state: RequestState, query: str, qvec, complexity: str, anchor: GoalAnchorBlock, overrides: dict | None = None) -> RetrievalOutcome:
        q = canon_ids(query)
        summary = bool(re.search(r"\bsummari[sz]e\b|\boverview\b|\bexplain what\b", q, re.I))
        planner = RetrievalPlanner(q, state.ctx.tenant_id, qvec, complexity, summary, overrides)
        lr = await self.loop.run(state, planner, RETRIEVAL_ALLOWED, q)
        chunks, graph_text, timings, abstain = [], "", {}, True
        for o in lr.obs:
            if not o.res.ok:
                continue
            if o.action.tool == "hybrid_retrieve":
                d = o.res.data
                chunks, abstain, timings = d.get("chunks", []), bool(d.get("abstain")), d.get("timings_ms", {})
                for k, v in timings.items():
                    from .telemetry import tele
                    tele.record(f"mcp.rag.{k}", v)
                    tr = tele.current()
                    if tr is not None:
                        tr["spans"].append({"name": f"mcp.rag.{k}", "ms": round(v, 3), "offset_ms": 0.0})
            elif o.action.tool == "expand_subgraph":
                graph_text = o.res.data.get("text", "")
        if not any(o.action.tool == "hybrid_retrieve" and o.res.ok for o in lr.obs):   # retrieval itself failed: not an evidence-based abstention
            return RetrievalOutcome(Answer(ABSTAIN_TEXT), [], True, "RETRIEVAL_FAILED:" + lr.status, lr)
        # indirect-injection scan: quarantine poisoned chunks (FMEA #11)
        chunks = self.plugins.call("chunks_retrieved", state, chunks) if chunks else chunks
        if graph_text:
            from .plugins.injection import score as inj_score
            if inj_score(graph_text)[0] >= 0.6:
                state.quarantined_chunks.append({"chunk_id": "graph", "hits": ["injection"]}); graph_text = ""
        if abstain or not chunks:
            return RetrievalOutcome(Answer(ABSTAIN_TEXT), [], True, "LOW_CONFIDENCE" if abstain else "QUARANTINED", lr, timings=timings)
        evidence = [Evidence(c["chunk_id"], c["note_id"], c.get("title", ""), compress_text(c["text"], q), float(c.get("rerank") or 0.0)) for c in chunks]
        if graph_text:
            evidence.append(Evidence("graph", "graph", "graph", graph_text, 0.0))
        # entity-grounding gate: catalogue IDs named in the question must be present in the evidence (cross-encoders cannot verify identifiers)
        joined = " ".join(e.text for e in evidence)
        missing = [i for i in re.findall(r"\b(?:SKU-\d{3}|SUP-\d{2})\b", q) if i not in joined]
        missing += [m for m in re.findall(r"\b(?:Tier [A-Z]|Class [IVX]+)\b", q) if m.lower() not in joined.lower()]   # enumerated qualifiers (Tier D, Class IV)
        if missing:
            return RetrievalOutcome(Answer(ABSTAIN_TEXT), evidence, True, f"ENTITY_NOT_FOUND:{','.join(missing)}", lr, timings=timings)
        # near-miss gate: >=2 query concepts unknown to the whole vault AND weak lexical support in the evidence => the question is about something we don't cover
        from .rag.bm25 import tokenize
        content = {t for t in tokenize(q) if not any(c.isdigit() for c in t)}
        oov = {t for t in content if t not in self.vocab}
        cover = len(content & set(tokenize(joined))) / max(1, len(content))
        if UNSUPPORTED_TERMS_GATE and len(oov) >= 2 and len(oov) / max(1, len(content)) >= 0.35 and cover < 0.5:
            return RetrievalOutcome(Answer(ABSTAIN_TEXT), evidence, True, f"UNSUPPORTED_TERMS:{','.join(sorted(oov))}", lr, timings=timings)
        prompt = build_prompt(anchor, self.mem.get(state.ctx.tenant_id, state.ctx.session_id), [f"{e.title}: {e.text}" if e.id != 'graph' else e.text for e in evidence], query)
        ans = self.gen.generate(q, evidence, summary=summary)
        return RetrievalOutcome(ans, evidence, False, "OK", lr, prompt=prompt, chunks=chunks, timings=timings, graph_tokens=est_tokens(graph_text))


# ----------------------------------------------------------------------------- Execution
T3_STATUS = re.compile(r"\b(hold|quarantine|scrap|released?)\b", re.I)
STATUS_MAP = {"hold": "HOLD", "quarantine": "QUARANTINE", "scrap": "SCRAP", "release": "RELEASED", "released": "RELEASED"}


class ExecutionPlanner:
    def __init__(self, query: str):
        self.raw = query
        self.q = canon_ids(query)
        ents = extract_entities(self.q)
        self.lot = ents["lot"][0] if ents["lot"] else None
        self.sku = ents["sku"][0] if ents["sku"] else None
        m = re.search(r"\blot[\s_\-]*([A-Za-z0-9\-]+)", query, re.I)
        self.raw_lot = (m.group(1) if m else None)
        m2 = re.search(r"\bsku[\s_\-]*([A-Za-z0-9\-]+)", query, re.I)
        self.raw_sku = (m2.group(1) if m2 else None)
        low = query.lower()
        if re.search(r"\b(?:submit|file)\b.{0,20}\bform\b", low):
            self.kind = "form"
        elif re.search(r"\brelease\b.{0,15}\b(?:batch|lot)\b|\bneed\b.{0,15}\breleased\b|\breleased now\b", low):
            self.kind = "release"
        elif re.search(r"\b(?:put|place|set|mark|move|update|change|quarantine|scrap)\b", low) and self.lot and not re.match(r"^(?:what|how|where|is|are|show|check|which|who|when)\b", low):
            self.kind = "status"
        elif re.search(r"expir|how many days", low):
            self.kind = "expiry"
        elif self.sku or (self.raw_sku and not self.lot and not self.raw_lot):
            self.kind = "stock"
        else:
            self.kind = "lot_status"
        m = T3_STATUS.search(query)
        self.new_status = STATUS_MAP[m.group(1).lower()] if m else "HOLD"
        m = re.search(r"(?:because|due to|reason[:\s]|,)\s*(.{3,120})$", query, re.I)
        self.reason = (m.group(1).strip() if m else "requested via assistant")[:200]
        self.form_type = "release" if "release" in low else ("transfer" if "transfer" in low else "deviation")

    def _lot_arg(self):
        return self.lot or (self.raw_lot if self.raw_lot else None)

    def next_action(self, obs: list[Obs]) -> Action | None:
        n = len(obs)
        if n and not obs[-1].res.ok:
            err = (obs[-1].res.error or {}).get("name")
            if err in RETRYABLE and sum(1 for o in obs if not o.res.ok) < 2:
                return obs[-1].action
            return None
        k = self.kind
        if k == "stock":
            return Action("lookup_sku", {"sku": self.sku or self.raw_sku}) if n == 0 and (self.sku or self.raw_sku) else None
        lot = self._lot_arg()
        if lot is None:
            return None
        if n == 0:
            if k == "form":
                return Action("submit_form", {"form_type": self.form_type, "fields": {"lot": lot, "description": self.reason}})
            return Action("lookup_lot", {"lot": lot})
        if n == 1:
            if k == "expiry":
                return Action("compute_shelf_life", {"lot": lot, "as_of": date.today().isoformat()})
            if k == "status":
                return Action("update_lot_status", {"lot": lot, "new_status": self.new_status, "reason": self.reason if len(self.reason) >= 3 else "requested via assistant"})
            if k == "release":
                return Action("release_batch", {"lot": lot})
        return None


class ExecutionSubagent:
    def __init__(self, harness: ToolExecutionHarness):
        self.h = harness
        self.loop = ReActLoop(harness)

    async def run(self, state: RequestState, query: str):
        planner = ExecutionPlanner(query)
        lr = await self.loop.run(state, planner, EXECUTION_ALLOWED, query)
        return planner, lr

    @staticmethod
    def narrate(planner: ExecutionPlanner, lr: LoopResult, state: RequestState) -> tuple[str, list[str]]:
        """Deterministic templated answer from tool data (no free-form generation => no hallucination surface)."""
        facts: list[str] = []
        for o in lr.obs:
            if not o.res.ok:
                continue
            d = o.res.data
            if o.action.tool == "lookup_lot":
                facts.append(f"{d['lot']} (SKU {d['sku']}) has status {d['status']}, quantity {d['qty']}, expiry {d['expiry']}.")
            elif o.action.tool == "lookup_sku":
                facts.append(f"{d['sku']} ({d['name']}) has {d['stock']} units in stock; reorder point is {d['reorder']} units.")
            elif o.action.tool == "compute_shelf_life":
                facts.append(f"{d['lot']} expires on {d['expiry']}: {d['days_remaining']} days remaining" + (" (EXPIRED)." if d["expired"] else "."))
        if state.hitl:
            facts.append(f"Your {planner.kind} request is recorded as PENDING_APPROVAL (id {state.hitl['approval_id']}). A QA approver must approve it; nothing has been changed yet.")
        if lr.status == "HALTED_BREAKER":
            return ("I stopped after repeated identical failures on " + state.meta.get("breaker_tool", "a tool") + ". "
                    "Please check the identifier (format SKU-001 / LOT-00044) or contact QA support; I will not keep retrying."), facts
        last_fail = next((o for o in reversed(lr.obs) if not o.res.ok), None)
        if last_fail and not facts:
            e = last_fail.res.error or {}
            if e.get("name") == "TIER_DENIED":
                return f"You are not permitted to perform that action with your current role ({state.ctx.role.value}).", facts
            if e.get("name") == "NOT_FOUND":
                return f"I could not find that record: {e.get('msg')}. Please check the identifier.", facts
            if e.get("name") == "INVALID_PARAMS":
                return "That identifier is not valid. Expected formats: SKU-001 or LOT-00044. Please correct it and try again.", facts
            return "The inventory system is currently unavailable; please try again shortly. No changes were made.", facts
        if last_fail and state.hitl is None and last_fail.res.error and last_fail.res.error.get("name") in ("TIER_DENIED", "NOT_FOUND"):
            facts.append(f"Note: {last_fail.res.error.get('msg')}")
        if not facts:
            return "Please tell me which SKU-nnn or LOT-nnnnn you mean.", facts
        return " ".join(facts), facts


class VerificationSubagent:
    """Independent grounding check (own context: only answer + evidence). Veto => deterministic abstention."""
    @staticmethod
    def check(answer: str, evidence_texts: list[str]) -> tuple[float, list[str]]:
        return verify_answer(answer, evidence_texts)
