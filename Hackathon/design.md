# design.md — The Unreliable Assistant → Enterprise Grade  (Gate 1, LOCKED v1.0)

> HARD GATE: application code under `src/`, `mcp_servers/` and `app.py` was written only after this file.
> Changes to contracts/budgets require editing this file first. Spec gaps and conflicts: `docs/GAPS_AND_ASSUMPTIONS.md` (IDs G-xx / C-xx referenced below).
> Companion artifact (Gate 2): `docs/workflow_orchestration.png` (source: `docs/workflow_orchestration.dot`).

## 1.1 System Charter & Hard Boundaries

**Purpose.** A multi-tenant enterprise assistant answering questions over policies/SOPs/product catalogue and preparing (never autonomously finalising) operational transactions on SKUs and manufacturing lots.

**In scope**
- Grounded Q&A with citations over the Obsidian knowledge vault via 3-stage hybrid RAG.
- Read-only inventory lookups (SKU, LOT) and deterministic compute helpers.
- Transaction *preparation* (form validation, status-change requests) routed through a Human-in-the-Loop gate.
- Full observability, tenant isolation, tamper-evident audit.

**Non-goals / hard boundaries**
- **Zero autonomous clinical, financial or legal execution.** Any `TIER_3_MUTATE` action (`update_lot_status`, `release_batch`, `submit_form`) creates a `PENDING_APPROVAL` record and halts; only an authorised human approval (`approve` API with reviewer key) executes it.
- No web access, no code execution, no cross-tenant data access, no persistence of raw PII.
- The assistant never answers from parametric memory: no retrieved evidence above threshold ⇒ deterministic abstention.
- Not a general chatbot: out-of-scope requests are refused (`REJECT_OOS`).

**Personas**
| Persona | Role header | Allowed tiers | Typical use |
|---|---|---|---|
| Floor Associate | `reader` | T1 | "What is the storage temperature of SKU-017?" |
| Quality/Supply Operator | `operator` | T1, T2, request T3 | "Put LOT-00044 on hold" (→ pending approval) |
| QA Approver | `approver` | T1–T3 approve | Reviews HITL queue |
| Platform/AI Engineer | n/a | n/a | Streamlit workbench, KPI scorecard |

## 1.2 MCP Tool & Data Contracts

Transport: JSON-RPC 2.0 over MCP. `hybrid-rag` & `graphify-vault`: stdio (persistent session per API worker). `state-store`: Streamable HTTP `:8081/mcp`. All servers publish an HMAC-SHA256 signed manifest (`manifest_sig`); the host refuses to attach on mismatch (G-13).

**Common types (Pydantic v2, `extra="forbid"`, strict)**
```python
SKU  = constr(pattern=r"^SKU-\d{3}$")          # SKU-099
LOT  = constr(pattern=r"^LOT-\d{5}$")          # LOT-00044
TenantId = constr(pattern=r"^[a-z0-9_\-]{1,64}$")
IdempotencyKey = UUID4                          # mandatory on every T2/T3 call
class ToolCtx(BaseModel): tenant_id: TenantId; user_id: constr(max_length=64); role: Role; trace_id: str; session_id: str
```

**`mcp-hybrid-rag`** — `tool hybrid_retrieve`, `resource rag://chunks/{chunk_id}`
```python
class HybridRetrieveIn(BaseModel):
    tenant_id: TenantId; query: constr(min_length=1, max_length=512)
    top_k: int = Field(4, ge=1, le=10)            # rerank_k
    bm25_k: int = Field(50, ge=5, le=100); dense_k: int = Field(50, ge=5, le=100)
    k1: float = Field(1.25, ge=0.5, le=3.0); b: float = Field(0.75, ge=0, le=1)
    rrf_k: int = Field(60, ge=1, le=200); pool: int = Field(40, ge=4, le=100)
    threshold: float = Field(0.42, ge=0, le=1)
class Chunk(BaseModel): chunk_id: str; note_id: str; text: str; bm25: float|None; dense: float|None; rrf: float; rerank: float|None; rank: int
class HybridRetrieveOut(BaseModel): chunks: list[Chunk]; top_score: float; abstain: bool; timings_ms: dict[str,float]; candidates: dict   # candidates only if debug
```
**`mcp-state-store`** (every query/command filters by `tenant_id`)
| Tool | Tier | Input | Output |
|---|---|---|---|
| `lookup_sku` | T1 | `{ctx, sku: SKU}` | `{sku,name,storage_temp_c,shelf_life_days,hazard_class,supplier_id,stock}` |
| `lookup_lot` | T1 | `{ctx, lot: LOT}` | `{lot,sku,status,qty,expiry,hold_reason?}` |
| `compute_shelf_life` | T2 | `{ctx, lot, as_of: date}` | `{days_remaining, expired}` |
| `validate_form` | T2 | `{ctx, form_type, fields}` | `{valid, errors[]}` |
| `submit_form` / `update_lot_status` / `release_batch` | T3 | `{ctx, idem_key: UUID4, ...}` | `{status:"PENDING_APPROVAL", approval_id}` (never executes inline) |
| `decide_hitl` | approver | `{ctx, approval_id, decision, reviewer}` | `{status:"APPROVED"/"REJECTED", executed: bool}` |
| `put_fact` / `get_facts` | T1 | `{ctx, session_id, fact}` | `session_facts` rows (PII-redacted) |
| `append_audit` | system | `{ctx, event}` | `{seq, hash}` (hash chain) |

**`mcp-graphify-vault`** — `expand_subgraph{anchor:str, depth:int(le=2), budget_tokens:int(le=400)} → {nodes[], edges[], text, tokens}`, `graph_stats`.

**Error codes** (JSON-RPC `error.code`): `-32602` invalid params (Pydantic), `-32001 TIER_DENIED`, `-32002 NOT_FOUND`, `-32003 BREAKER_OPEN`, `-32004 IDEMPOTENT_REPLAY` (returns original result), `-32005 TOOL_TIMEOUT`, `-32006 TENANT_VIOLATION`, `-32007 HITL_REQUIRED`, `-32008 MANIFEST_INVALID`.

**Public API** `POST /api/v1/agent/query` body `{tenant_id, user_id, query, session_id?}` (+ headers `X-Tenant-ID`, `X-Role`). Response: `{request_id, trace_id, tenant_id, user_id, route, status, answer, citations[], abstained, faithfulness, cache_hit, steps, hitl{approval_id,status}?, latency_ms, timings}`. `status ∈ {OK, ABSTAINED, BLOCKED_PII?, BLOCKED_INJECTION, HALTED_BREAKER, PENDING_APPROVAL, REJECTED_OOS, VETOED}`. Header/body tenant mismatch ⇒ HTTP 403. Streaming variant `/api/v1/agent/query/stream` (SSE) for TTFT.

## 1.3 Distributed Topology & Data Flow

```
Client ─► NGINX/Envoy ─► FastAPI worker(s) (stateless, async) ─────────────────────────────────────────────┐
                           │ 1 Pre-plugins: PII Redactor(10) → Injection Shield(20)      [hook.pre]       │
                           │ 2 Semantic cache  Redis: sc:{sha256(tenant)}:{sha256(tenant:user:norm_q)}    │
                           │ 3 Triage (TF-IDF+LR, T=0)  (intent,domain,risk_tier,complexity) [triage]     │
                           │ 4 route ─┬ DIRECT_ANSWER / REJECT_OOS → templates                            │
                           │          ├ HYBRID_RAG → Retrieval subagent ─┐                                │
                           │          └ TRANSACTION_EXEC → Execution subagent                             │
                           │ 5 Bounded ReAct loop (≤5 it, 450 ms) ► ToolExecutionHarness ► CircuitBreaker │
                           │ 6 Verification subagent (claim↔chunk NLI proxy, RAGAS gate) [hook.post.ragas]│
                           │ 7 Tier-3 HITL gate → PENDING_APPROVAL ; 8 Audit (hash chain) + OTel export   │
                           ▼ MCP JSON-RPC (bulkheaded pools)                                              │
   mcp-hybrid-rag (stdio)      mcp-graphify-vault (stdio)        mcp-state-store (streamable-http :8081) │
   BM25 ┐                      Obsidian vault + graph.json        SQLite/PostgreSQL: inventory, session_facts,
   Dense(HNSW) ┘→RRF→Cross-Enc  depth≤2 subgraph                  hitl, audit_chain (tenant_id on every row)
   (ONNX, dynamic micro-batch)                                    Redis: semantic cache + hot ReAct window
                           └──────────────► OTel SDK → Collector → Prometheus + Tempo + Grafana ◄─────────┘
```
**Route mapping (C-04)**
| Gate 2 branch | Gate 6 route | Subagent | MCP servers | Guardrail |
|---|---|---|---|---|
| Direct Response | `DIRECT_ANSWER`, `REJECT_OOS` | none | none | static templates only |
| Retrieval Subagent | `HYBRID_RAG` | Retrieval | hybrid-rag, graphify-vault | read-only, T1 only |
| Execution Subagent | `TRANSACTION_EXEC` | Execution | state-store | no RAG; T3 ⇒ HITL freeze |
| Audit Subagent | (post-step on every route) | Verification | state-store (audit only) | independent context; may VETO |

**Bulkheads:** `rag_pool=64`, `db_pool=128`, `llm_pool=256` (semaphores + executors). Health checks and triage never share a pool with rerank.

## 1.4 End-to-End Latency Budget (200–600 ms)

Per-request **p95 budget on a cache-miss RAG path** (parallel BM25∥dense counted once). Target: **p95 ≤ 450 ms, p99 ≤ 590 ms**.

| # | Hop | Span | p95 budget (ms) |
|---|---|---|---|
| 1 | API ingress + auth + JSON parse | `http.ingress` | 6 |
| 2 | Pre-hook (PII + injection) | `hook.pre` | **15** |
| 3 | Cache lookup (exact + semantic) | `cache.lookup` | 5 |
| 4 | Triage | `triage.classify` | **25** |
| 5 | MCP client overhead (3 calls × ≤4 ms serialization) | `mcp.call` | 12 |
| 6 | BM25 ∥ Dense | `mcp.rag.bm25` / `.biencoder` | 12 ∥ 25 → **25** |
| 7 | RRF | `mcp.rag.rrf` | 3 |
| 8 | Cross-encoder rerank | `mcp.rag.rerank` | **45** |
| 9 | Graphify expansion | `mcp.graphify` | 10 |
| 10 | Extra ReAct step (state lookup) | `agent.react.step` | 30 |
| 11 | Generation (extractive: ≤20; LLM-backend TTFT ≤180) | `llm.generate` | 120 |
| 12 | Verification + RAGAS gate | `hook.post.ragas` | 30 |
| 13 | Audit write (async, off critical path) + cache store | `audit.append` | 5 |
| 14 | Queueing / scheduling headroom | – | 119 |
| | **Sum** | | **450** |

Fast paths: cache hit ≤ 30 ms; **abstention short-circuit ≤ 90 ms** (rows 1-8 only, no LLM). Under 50 K VUs the budget holds only with horizontal scale (N workers × 1 rag-MCP each, micro-batch 32 / 8 ms wait, Redis Cluster); dev-box proof is scaled (G-18). Incident modes (Gate 10/11) shrink `top_k` 50→25 and lower the cache threshold to 0.92 to recover ≥ 60 ms.

## 1.5 KPI & SLA Gate Matrix (36 KPIs)

Status vocabulary: PASS / FAIL / PARTIAL (met at reduced scale or via proxy metric) / NOT_MEASURED. Source of truth: `reports/kpi_report.json` produced by `evals/run_kpis.py`.

| ID | KPI | Threshold | Measurement |
|---|---|---|---|
| 01 | Pre-hook latency p95 | ≤ 15 ms | span `hook.pre` |
| 02 | PII redaction recall (email/phone/SSN/obfuscated) | ≥ 99.8 % | `data/pii_set.jsonl` |
| 03 | Prompt-injection block rate (direct+indirect) | ≥ 99.5 % | red-team set |
| 04 | Triage routing accuracy | ≥ 97.0 % | held-out labelled set |
| 05 | Triage latency p95 | ≤ 25 ms | span `triage` |
| 06 | False-positive refusal on benign | ≤ 1.0 % | benign set |
| 07 | BM25 latency p95 | ≤ 12 ms | span `rag.bm25` |
| 08 | Dense latency p95 | ≤ 25 ms | span `rag.dense` |
| 09 | Rerank latency p95 | ≤ 45 ms | span `rag.rerank` |
| 10 | Hybrid Recall@50 | ≥ 0.92 | golden set (gold notes) |
| 11 | Context precision@4 | ≥ 0.88 | golden set |
| 12 | MRR@4 | ≥ 0.85 | golden set |
| 13 | nDCG@4 | ≥ 0.86 | golden set |
| 14 | Tool-schema first-pass validation | ≥ 96.0 % | `data/tool_calls.jsonl` |
| 15 | Auto-repair success on malformed args | ≥ 90.0 % | malformed subset |
| 16 | Infinite-loop escape (breaker) | 100 % | chaos suite |
| 17 | Mean ReAct steps / completed task | ≤ 2.2 | traces |
| 18 | Context drift (goal cosine) | ≥ 0.85 | drift audit |
| 19 | Tier-3 HITL enforcement | 100 % | audit-log integrity |
| 20 | RAGAS faithfulness | ≥ 0.92 | proxy (G-08) |
| 21 | RAGAS answer relevancy | ≥ 0.90 | proxy |
| 22 | RAGAS context entity recall | ≥ 0.85 | proxy |
| 23 | Unanswerable abstention accuracy | ≥ 98.0 % | negative set |
| 24 | Unsupported-claim rate | ≤ 0.5 % | claim verifier |
| 25 | TTFT p95 | ≤ 180 ms | SSE endpoint |
| 26 | Prompt-token reduction (Graphify+Obsidian) | ≥ 45 % | token counter |
| 27 | Semantic cache hit ratio (steady state) | ≥ 35 % | cache metric |
| 28 | Cross-tenant cache isolation | 100 % (0 leaks) | concurrency probe |
| 29 | Cost / 1,000 queries | ≤ $0.45 | token cost calc |
| 30 | MCP JSON-RPC serialization p95 | ≤ 4 ms | client timer |
| 31–33 | e2e latency p50 / p95 / p99 @ 50K VUs | ≤ 240 / 450 / 590 ms | k6 + JMeter |
| 34 | HTTP 5xx rate | ≤ 0.05 % | k6 |
| 35 | Throughput | ≥ 8,500 RPS | k6 |
| 36 | RSS growth over 1-h soak | ≤ 3.0 % | psutil / node exporter |

**RAGAS proxy definitions (G-08).** *Faithfulness* = supported claims / total claims, a claim (answer sentence) is supported iff every number/ID it contains and ≥ 0.6 of its content-word stems occur in the cited chunks. *Answer relevancy* = fraction of the reference key-facts of the question covered by the answer (answerable) / 1 if abstained (unanswerable). *Context precision@k* = relevant-rank-weighted precision of retrieved chunks vs gold notes. *Context recall* = fraction of gold notes retrieved. *Context entity recall* = fraction of gold entities (IDs, numbers, named terms) present in retrieved contexts.

## 1.6 Failure Mode & Effects Analysis (FMEA)

| # | Component failure | Detection | Effect | Deterministic fallback | Sev |
|---|---|---|---|---|---|
| 1 | Redis down / slow | op timeout 20 ms | no cache, higher load | bypass cache (fail-open for reads), in-process LRU per worker; never serve cross-tenant | M |
| 2 | Redis key collision / leak | probe KPI-28, key format assertion | data leak | keys are `sc:{sha256(tenant)}:…`; lookup asserts prefix; on violation purge tenant + alert | H |
| 3 | Reranker timeout (>45 ms ×1.5) | span timeout | no precision | fall back to RRF order with *stricter* abstention (needs RRF score ≥ P90 and lexical overlap ≥ 0.5) else abstain | M |
| 4 | Dense encoder failure | exception | recall loss | BM25-only + rerank; flag `degraded=true` | M |
| 5 | MCP hybrid-rag crash/hang (>150 ms) | JSON-RPC timeout, EOF | no retrieval | bulkhead trips, restart worker, serve from cache else abstain with remediation message | H |
| 6 | MCP state-store down | HTTP error | no lookups/audit | reads → abstain "inventory unavailable"; T3 → refuse (never queue silently); audit buffered to local WAL file and replayed in order | H |
| 7 | Graphify server down | error | larger prompts | skip expansion, use reranked chunks only | L |
| 8 | LLM 429 / timeout (backend=anthropic) | HTTP 429/5xx | no generation | retry once with jitter ≤40 ms, then switch to extractive generator | M |
| 9 | Runaway tool loop | breaker: ≥2 identical failures, or it>5 / 450 ms | resource burn | `HALTED_BREAKER` + remediation prompt | H |
| 10 | Invalid/ambiguous tool args | Pydantic error | tool failure | regex auto-repair (SKU99→SKU-099); if unrepairable → ask user, no retry | M |
| 11 | Prompt injection in retrieved chunk | indirect-injection scanner on chunks | goal hijack | chunk quarantined (dropped from context, logged), answer from remaining chunks else abstain | H |
| 12 | Tier-3 request without reviewer / approval DB failure | `HITL_REQUIRED` | unauthorised mutation | fail-closed: refuse and record; execution only via `decide_hitl` | H |

## 1.7 Architecture Decision Records

**ADR-1 Hybrid retrieval weights.** Decision: BM25 (k1=1.25, b=0.75) and dense retrievers each return 50; fuse by **RRF k=60, equal weights**, dedup by `chunk_hash`, pool=40 → cross-encoder → top 4. *Why:* RRF is score-scale-free (BM25 scores unbounded, cosine in [0,1]) so no weight tuning/leakage; BM25 rescues SKU/lot identifiers that dense models blur, dense rescues paraphrase. *Rejected:* weighted-sum (needs per-corpus calibration), dense-only (keyword blindness cited in audit §1.1). *Index:* HNSW (efSearch=64) at this scale; IVF-PQ becomes preferable above ~1 M chunks (C-03). Workbench sliders (k1, b, RRF k, threshold) let engineers verify sensitivity.

**ADR-2 ONNX quantization.** Decision: all neural inference (bi-encoder, cross-encoder) via ONNX Runtime; FP16 on GPU, INT8 on CPU. *Why:* removes torch from the hot path, deterministic batching, 2-3× CPU speed-up with <1 pt nDCG loss in published BGE int8 results; also keeps worker RSS small for the soak KPI. *Trade-off:* dev profile uses smaller models (G-17); the `spec` profile is available to measure the real cost.

**ADR-3 Redis key isolation.** Decision: key = `sc:{sha256(tenant)}:{sha256(tenant:user:norm_query)}` with jittered TTL 300 s ± 45 s; semantic scan only within the `(tenant,user)` index set; a separate `shared` partition only for responses flagged non-personal. *Why:* global keys caused the baseline cross-user leak; hashing prevents tenant enumeration, prefix enables per-tenant purge, jitter prevents stampede. Every read re-checks the stored `tenant_id` field (defence in depth).

**ADR-4 Bounded ReAct loops.** Decision: explicit state machine `INIT→TRIAGE→REASONING→TOOL_CALL→OBSERVATION→VERIFY→DONE/HALTED` with max 5 iterations, 120 ms/step, 450 ms total, plus an idempotent breaker keyed by `(tool, sha256(canonical_args))`. *Why:* the baseline looped infinitely on `SKU99`; determinism + hard ceilings make loop-escape provable (KPI-16 = 100 %), and deterministic planning removes an LLM round-trip from each step (mean steps ≤ 2.2). *Rejected:* LLM-decides-when-to-stop (unbounded), recursion (stack/state ambiguity).

**ADR-5 Abstention over generation.** Decision: top rerank score < 0.42 ⇒ no LLM call, fixed refusal text. *Why:* removes hallucination risk on unanswerable queries and saves ~80 % tokens; threshold calibrated on dev split (C-10).

**ADR-6 Graphify pruning.** Decision: anchor-node subgraph with depth ≤ 2 instead of raw chunk dump. *Why:* policies are interlinked by wikilinks; two hops capture product→supplier→SOP chains (multi-hop questions) at ~320 tokens vs ~1,400.
