"""Request orchestration: ingress plugins -> semantic cache -> triage -> subagent loop -> verification -> HITL/abstention -> egress (design.md §1.3)."""
from __future__ import annotations

import asyncio
import re
import time
import uuid

import numpy as np

from .cache import SemanticCache
from .config import settings
from .context import GoalAnchorBlock, SessionMemory
from .generator import ABSTAIN_TEXT, AnthropicGenerator, ExtractiveGenerator, llm_verify_claims
from .harness import ToolExecutionHarness
from .mcp_host import MCPHost
from .models import Citation, QueryRequest, QueryResponse, Role, ToolCtx
from .plugins.base import PluginRegistry, RequestState
from .plugins.breaker import CircuitBreaker
from .plugins.hitl import HITLGate
from .plugins.injection import InjectionShield
from .plugins.otel import OTelExporter
from .plugins.pii import PIIRedactor
from .plugins.ragas_auditor import RagasAuditor
from .subagents import ExecutionSubagent, RetrievalSubagent
from .telemetry import tele
from .triage import TriageAgent
from .util import est_tokens, extract_entities, norm_query, sha256

DIRECT = [
    (re.compile(r"\b(?:thanks|thank you|ok thanks)\b", re.I), "You're welcome! Ask me anything about SOPs, policies, products or lot status."),
    (re.compile(r"\b(?:bye|goodbye)\b", re.I), "Goodbye!"),
    (re.compile(r"\b(?:what can you do|how can you help|capabilit|who are you|what do you do|help)\b", re.I),
     "I answer questions about SOPs, policies and products (with citations), look up SKU/LOT status, and prepare lot-status changes that a QA approver must approve."),
    (re.compile(r".*"), "Hello! I'm the enterprise operations assistant. Ask about an SOP, policy, SKU or LOT."),
]
OOS_TEXT = "That's outside what I can help with. I only handle enterprise SOPs, policies, products and lot operations."
BLOCK_TEXT = "Your request was blocked by a security policy and was not processed."


class Orchestrator:
    def __init__(self, host: MCPHost | None = None, cache: SemanticCache | None = None):
        self.host = host or MCPHost()
        self.plugins = PluginRegistry()
        self.breaker = CircuitBreaker(self.plugins_cfg("circuit_breaker").get("max_identical_calls", 2))
        self.shield = InjectionShield()
        for name, pl in [("pii_redactor", PIIRedactor()), ("injection_shield", self.shield), ("circuit_breaker", self.breaker), ("hitl_gate", HITLGate()),
                         ("ragas_auditor", RagasAuditor()), ("otel_exporter", OTelExporter())]:
            self.plugins.register(name, pl)
        self.harness = ToolExecutionHarness(self.host, self.plugins)
        self.cache = cache or SemanticCache()
        self.triage = TriageAgent()
        self.gen = ExtractiveGenerator()
        self.llm = AnthropicGenerator() if settings.llm_backend == "anthropic" else None
        self.memory = SessionMemory()
        self.retrieval = RetrievalSubagent(self.harness, self.plugins, self.gen, self.memory)
        self.execution = ExecutionSubagent(self.harness)
        self.session_risk: dict[tuple[str, str], float] = {}
        self.audit_q: list[tuple[str, dict]] = []
        self.rag_overrides: dict = {}
        self.incident = "normal"
        self.recent_traces: list[dict] = []
        self.rag_gate = asyncio.Semaphore(settings.rag_admit)
        self._flusher: asyncio.Task | None = None

    def plugins_cfg(self, name):
        import json
        return {p["name"]: p for p in json.loads((settings.root / "mcp_config.json").read_text())["plugins"]}.get(name, {})

    async def start(self):
        await self.host.start()
        self._flusher = asyncio.create_task(self._flush_loop())

    async def stop(self):
        if self._flusher:
            self._flusher.cancel()
        await self._flush()
        await self.host.stop()

    # ---------------- incident runbooks (Gate 10/11) ----------------
    def set_incident(self, mode: str):
        self.incident = mode
        if mode == "latency":       # latency breach: aggressive semantic cache + smaller candidate sets
            self.cache.threshold = 0.92
            self.rag_overrides = {"bm25_k": 25, "dense_k": 25}
        elif mode == "hallucination":  # raise cross-encoder cutoff, strict extractive quote mode
            self.cache.threshold = settings.semantic_threshold
            self.rag_overrides = {"threshold": 0.55}
        else:
            self.cache.threshold = settings.semantic_threshold
            self.rag_overrides = {}

    # ---------------- audit ----------------
    async def _flush_loop(self):
        while True:
            await asyncio.sleep(0.25)
            await self._flush()

    async def _flush(self):
        if not self.audit_q:
            return
        q, self.audit_q = self.audit_q, []
        by: dict[str, list[dict]] = {}
        for t, ev in q:
            by.setdefault(t, []).append(ev)
        for t, evs in by.items():
            try:
                with tele.span("audit.append"):
                    await self.host.call("state-store", "append_audit_batch", {"tenant_id": t, "events": evs}, timeout_ms=2000)
            except Exception:  # noqa: BLE001  FMEA #6: buffer locally, replay in order
                self.audit_q = [(t, e) for e in evs] + self.audit_q
                tele.inc("audit.buffered")
                break

    # ---------------- workbench helpers ----------------
    async def chaos_sku_loop(self, tenant_id: str, sku: str = "SKU-777", naive_retries: int = 50) -> dict:
        """Replays the prototype's failure mode (an agent retrying a failing lookup forever) through the live harness + circuit breaker."""
        from .harness import EXECUTION_ALLOWED
        from .loop import Action
        from .models import ToolCtx
        st = RequestState(ctx=ToolCtx(tenant_id=tenant_id, user_id="chaos", role=Role.operator, session_id="chaos"))

        class Naive:
            def __init__(s): s.n = 0
            def next_action(s, obs):
                s.n += 1
                return Action("lookup_sku", {"sku": sku}) if s.n <= naive_retries else None
        lr = await self.execution.loop.run(st, Naive(), EXECUTION_ALLOWED, "chaos")
        return {"status": lr.status, "steps": lr.steps, "naive_would_retry": naive_retries, "elapsed_ms": round(lr.elapsed_ms, 1), "transitions": lr.transitions,
                "observations": [{"step": o.step, "tool": o.action.tool, "ok": o.res.ok, "error": (o.res.error or {}).get("name")} for o in lr.obs],
                "remediation": "Check the identifier (SKU-001 / LOT-00044) or contact QA; automatic retries are disabled for this call."}

    # ---------------- helpers ----------------
    async def embed(self, q: str):
        r = await self.host.call("hybrid-rag", "embed_query", {"query": q[:512]})
        return np.asarray(r["vec"], dtype=np.float32)

    def _decay_risk(self, key) -> float:
        return self.session_risk.get(key, 0.0)

    async def handle(self, req: QueryRequest, role: Role = Role.reader, traceparent: str | None = None) -> QueryResponse:
        t0 = time.perf_counter()
        tr = tele.start_trace(traceparent)
        tr["t0"] = t0
        sid = req.session_id or f"{req.user_id}-default"
        ctx = ToolCtx(tenant_id=req.tenant_id, user_id=req.user_id, role=role, trace_id=tr["trace_id"], session_id=sid)
        st = RequestState(ctx=ctx, raw_len=len(req.query), query=req.query)
        rid = str(uuid.uuid4())
        resp = QueryResponse(request_id=rid, trace_id=tr["trace_id"], tenant_id=req.tenant_id, user_id=req.user_id, route="", status="OK", answer="")
        skey = (req.tenant_id, sid)

        # 1. pre-execution plugins (PII -> injection)
        with tele.span("hook.pre"):
            risk = self._decay_risk(skey)
            self.plugins.call("pre_execute", st, risk)
            self.session_risk[skey] = min(2.0, 0.6 * risk + (st.risk_score if st.risk_score < 0.9 else 0.0))
            if len(self.session_risk) > 100_000:
                self.session_risk.clear()
        if st.blocked:
            resp.route, resp.status, resp.answer, resp.abstained = "BLOCKED", st.status, BLOCK_TEXT, True
            return self._finish(resp, st, t0, req)

        # 2. semantic cache (tenant+user isolated)
        qvec = None
        with tele.span("cache.lookup"):
            async def _emb(q):
                nonlocal qvec
                qvec = await self.embed(q)
                return qvec
            try:
                hit, kind = await self.cache.lookup(req.tenant_id, req.user_id, st.query, _emb)
            except Exception:  # noqa: BLE001
                hit, kind = None, None
        if hit:
            for k in ("route", "status", "answer", "citations", "abstained", "faithfulness", "steps"):
                setattr(resp, k, hit[k] if k != "citations" else [Citation(**c) for c in hit[k]])
            resp.cache_hit = True
            tele.inc("requests.cache_hit")
            return self._finish(resp, st, t0, req, audit=True)

        # 3. triage
        with tele.span("triage.classify"):
            tri = self.triage.classify(st.query)
        st.route = tri.route
        resp.route = tri.route
        ents = extract_entities(st.query)
        anchor = GoalAnchorBlock(intent=norm_query(st.query)[:200], entities=tuple(ents["sku"] + ents["lot"]))

        try:
            if tri.route == "REJECT_OOS":
                st.answer, resp.status, resp.abstained = OOS_TEXT, "REJECTED_OOS", True
            elif tri.route == "DIRECT_ANSWER":
                st.answer = next(t for rx, t in DIRECT if rx.search(st.query))
            elif tri.route == "HYBRID_RAG":
                try:   # admission control: step deadlines then measure service time, not queueing; excess load is shed (HTTP 503)
                    await asyncio.wait_for(self.rag_gate.acquire(), settings.max_queue_wait_s)
                except asyncio.TimeoutError:
                    tele.inc("requests.shed")
                    resp.status, resp.abstained, st.answer = "OVERLOADED", True, "The service is busy. Please retry shortly."
                else:
                    try:
                        await self._rag(st, resp, tri, anchor, qvec)
                    finally:
                        self.rag_gate.release()
            else:
                await self._exec(st, resp)
        except Exception as e:  # noqa: BLE001  never leak internals; fail safe
            tele.inc("requests.internal_error")
            st.answer, resp.status, resp.abstained = "Something went wrong while processing your request. No changes were made.", "ERROR", True
            st.meta["error"] = type(e).__name__

        # 4. post-execution (RAGAS auditor, may veto)
        with tele.span("hook.post.ragas"):
            self.plugins.call("post_execute", st)
        if resp.status == "OVERLOADED":
            resp.answer = st.answer
            return self._finish(resp, st, t0, req, audit=False)
        if st.status == "VETOED":
            resp.status, st.answer, resp.abstained = "VETOED", ABSTAIN_TEXT, True
        resp.answer = st.answer
        resp.faithfulness = round(st.faithfulness, 4)
        resp.steps = st.steps
        if st.hitl:
            resp.hitl = {"approval_id": st.hitl.get("approval_id"), "status": st.hitl.get("status")}
        if st.quarantined_chunks:
            resp.timings["quarantined_chunks"] = float(len(st.quarantined_chunks))

        # 5. cache store (RAG answers only; shareable only if non-personal & public notes)
        if tri.route == "HYBRID_RAG" and resp.status in ("OK", "ABSTAINED") and not st.pii_found and not st.quarantined_chunks:
            shareable = all(not c.note_id.startswith(("priv-", "poison-")) for c in resp.citations)
            asyncio.create_task(self.cache.store(req.tenant_id, req.user_id, st.query, self._cacheable(resp), qvec, shareable))
        if resp.status == "OK" and not resp.abstained:
            self.memory.get(req.tenant_id, sid).add(st.query, st.answer)
        return self._finish(resp, st, t0, req, audit=True, debug=req.debug)

    @staticmethod
    def _cacheable(r: QueryResponse) -> dict:
        return {"route": r.route, "status": r.status, "answer": r.answer, "citations": [c.model_dump() for c in r.citations], "abstained": r.abstained,
                "faithfulness": r.faithfulness, "steps": r.steps}

    async def _rag(self, st, resp, tri, anchor, qvec):
        out = await self.retrieval.run(st, st.query, qvec, tri.complexity, anchor, self.rag_overrides)
        st.evidence = [{"text": c["text"]} for c in out.chunks] + ([{"text": e.text} for e in out.evidence if e.id == "graph"])
        with tele.span("llm.generate"):
            ans = out.answer
            if self.llm is not None and not out.abstain:   # LLM synthesis; output still passes the claim verifier (post_execute) and falls back to extractive on error
                ans = await self.llm.generate(out.prompt, st.query, out.evidence, bool(re.search(r"\bsummari[sz]e\b|\boverview\b", st.query, re.I)))
                st.meta["llm_backend"] = ans.backend
                if ans.backend == "anthropic":
                    await self._llm_ground(st, ans.text, out)
        if out.abstain and out.reason.startswith("RETRIEVAL_FAILED"):
            st.answer, resp.abstained, st.status = "The knowledge service is temporarily unavailable. Please retry shortly; no answer was guessed.", True, "UNAVAILABLE"
            resp.status = "HALTED_BREAKER" if st.halted == "HALTED_BREAKER" else "UNAVAILABLE"
            st.meta["abstain_reason"] = out.reason
        elif out.abstain:
            st.answer, resp.status, resp.abstained, st.status = ABSTAIN_TEXT, "ABSTAINED", True, "ABSTAINED"
            resp.timings["abstain_reason"] = 0.0
            st.meta["abstain_reason"] = out.reason
        else:
            st.answer = ans.text
            resp.citations = [Citation(chunk_id=e.id, note_id=e.note_id, title=e.title, score=e.score) for e in ans.cited if e.id != "graph"]
            tele.inc("tokens.in", est_tokens(out.prompt)); tele.inc("tokens.out", ans.output_tokens)
            resp.tokens = {"prompt": est_tokens(out.prompt), "output": ans.output_tokens, "graph": out.graph_tokens}
        st.meta["loop"] = out.loop.transitions
        st.meta["obs"] = [{"step": o.step, "tool": o.action.tool, "ok": o.res.ok, "ms": round(o.res.ms, 1), "obs": o.compressed[:300]} for o in out.loop.obs]
        st.meta["evidence"] = [{"id": c["chunk_id"], "note_id": c["note_id"], "text": c["text"], "rerank": c.get("rerank")} for c in out.chunks]
        st.meta["graph_notes"] = [n["id"].split(":", 1)[1] for o in out.loop.obs if o.action.tool == "expand_subgraph" and o.res.ok for n in o.res.data.get("nodes", [])]
        st.meta["graph_text"] = next((e.text for e in out.evidence if e.id == "graph"), "")
        st.meta["prompt"] = out.prompt

    async def _llm_ground(self, st, text: str, out):
        """Claim-level grounding of an LLM draft: lexical check first, independent LLM entailment judge for the claims it cannot confirm. Fails closed."""
        from .util import split_sentences
        from .verification import claim_supported
        ev = [c["text"] for c in out.chunks] + [e.text for e in out.evidence if e.id == "graph"]
        claims = [c for c in (re.sub(r"\s*\[\d+\]\s*", " ", s).strip() for s in split_sentences(text)) if len(c) > 3]
        weak = [c for c in claims if not claim_supported(c, ev, question=st.query)]
        bad = weak
        if weak:
            judge_ev = [f"{c.get('title', '')}: {c['text']}" for c in out.chunks] + [e.text for e in out.evidence if e.id == "graph"]   # titles let "SOP-CC-01 states ..." be checked
            verdict = await llm_verify_claims(self.llm, st.query, weak, judge_ev)
            bad = weak if verdict is None else [c for c, ok in zip(weak, verdict) if not ok]
            tele.inc("llm.judge_calls")
        st.faithfulness = 1.0 - len(bad) / max(1, len(claims))
        st.unsupported = bad
        st.meta["verified_by_llm"] = True

    async def _exec(self, st, resp):
        planner, lr = await self.execution.run(st, st.query)
        text, facts = self.execution.narrate(planner, lr, st)
        st.answer = text
        st.evidence = [{"text": f} for f in facts]
        st.meta["loop"] = lr.transitions
        st.meta["obs"] = [{"step": o.step, "tool": o.action.tool, "ok": o.res.ok, "ms": round(o.res.ms, 1), "obs": o.compressed[:300]} for o in lr.obs]
        if lr.status == "PENDING_APPROVAL":
            resp.status = "PENDING_APPROVAL"
        elif lr.status == "HALTED_BREAKER":
            resp.status, resp.abstained = "HALTED_BREAKER", True
        elif st.blocked:
            resp.status = st.status
        elif lr.status.startswith("HALTED"):
            resp.status = lr.status
        for o in lr.obs:
            if o.res.ok and o.action.tool in ("lookup_lot", "lookup_sku"):
                asyncio.create_task(self._fact(st, o))

    async def _fact(self, st, o):
        try:
            d = o.res.data
            fact = f"{d.get('lot') or d.get('sku')} status/info verified: {d.get('status', d.get('name'))}"
            await self.host.call("state-store", "put_fact", {"ctx": st.ctx.model_dump(mode="json"), "fact": fact})
        except Exception:  # noqa: BLE001
            pass

    def _finish(self, resp: QueryResponse, st: RequestState, t0: float, req: QueryRequest, audit: bool = True, debug: bool = False) -> QueryResponse:
        total = (time.perf_counter() - t0) * 1000
        resp.latency_ms = round(total, 2)
        tr = tele.current() or {"spans": []}
        agg: dict[str, float] = {}
        for sp in tr["spans"]:
            agg[sp["name"]] = agg.get(sp["name"], 0.0) + sp["ms"]
        resp.timings = {**resp.timings, **{k: round(v, 2) for k, v in agg.items()}}
        tele.record("e2e", total)
        tele.record("e2e.hit" if resp.cache_hit else "e2e.miss", total)
        tele.inc("requests.total")
        tele.inc(f"status.{resp.status}")
        if debug or req.debug:
            resp.trace = [{"spans": tr["spans"], "loop": st.meta.get("loop"), "obs": st.meta.get("obs"), "traceparent": tr.get("traceparent"),
                           "triage_route": st.route, "quarantined": st.quarantined_chunks, "injection_hits": st.meta.get("injection_hits"),
                           "pii_found": st.pii_found, "abstain_reason": st.meta.get("abstain_reason"),
                           "evidence": st.meta.get("evidence"), "graph_notes": st.meta.get("graph_notes"), "graph_text": st.meta.get("graph_text"), "prompt": st.meta.get("prompt")}]
            self.recent_traces.append({"trace_id": resp.trace_id, "spans": tr["spans"], "route": resp.route, "status": resp.status, "latency_ms": resp.latency_ms})
            self.recent_traces = self.recent_traces[-50:]
        if audit:
            self.audit_q.append((req.tenant_id, {"ts": time.time(), "trace_id": resp.trace_id, "request_id": resp.request_id, "user": req.user_id, "route": resp.route,
                                                 "status": resp.status, "qhash": sha256(st.query), "qlen": len(st.query), "pii": st.pii_found, "steps": resp.steps,
                                                 "cache_hit": resp.cache_hit, "latency_ms": resp.latency_ms, "hitl": (resp.hitl or {}).get("approval_id"),
                                                 "quarantined": [c["chunk_id"] for c in st.quarantined_chunks], "block": st.block_reason}))
        return resp
