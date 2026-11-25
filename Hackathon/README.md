# The Unreliable Assistant → Enterprise Grade

An MCP-enabled, plugin-extensible, multi-tenant agentic RAG assistant built to the *Hackathon Mid-Check* blueprint
(18 gates, 36 KPIs). Spec documents: `Unreliable_Assistant_MidCheck_Enterprise_Blueprint_v2.docx` (authoritative),
`SLM_Comprehensive_Guide.docx`, `Post_Hardening_Engineering_Audit_Report.docx` (treated as *targets*, not evidence).

**Read first:** [`CLAUDE.md`](CLAUDE.md) (project rules) · [`design.md`](design.md) (Gate 1 contract) ·
[`docs/GAPS_AND_ASSUMPTIONS.md`](docs/GAPS_AND_ASSUMPTIONS.md) (what the specs left out or contradicted, and the decision taken) ·
[`reports/KPI_SCORECARD.md`](reports/KPI_SCORECARD.md) (measured results — PASS/PARTIAL/FAIL/NOT_MEASURED, never "claimed").

## Quick start
```bash
source .venv/bin/activate && pip install -r requirements.txt     # Python 3.14 here (spec: 3.11 — see GAPS G-04)
python scripts/build_corpus.py        # Obsidian vault, 250-Q golden set, red-team/PII/triage/benign/chaos sets (deterministic)
python scripts/build_graph.py         # graphify build ./src ./knowledge_vault --output graph.json
python scripts/render_workflow.py     # docs/workflow_orchestration.png (Gate 2, 300 DPI)
scripts/run_stack.sh start            # state-store MCP :8081 + FastAPI :8000 (spawns hybrid-rag + graphify MCP over stdio)
scripts/q.sh "How often must the supplier of SKU-030 be audited?"
streamlit run app.py                  # 6-tab workbench
pytest -q                             # 84 tests (82 unit + integration) (integration tests auto-skip if the stack is down)
python evals/run_kpis.py              # all measurable KPIs -> reports/kpi_report.json + KPI_SCORECARD.md
PEAK_VUS=200 SCALE_T=0.25 JMETER=1 bash load_tests/run_load.sh && python evals/merge_load.py   # scaled k6 + JMeter
```

## What was built (gate → code)
| Gate | Deliverable |
|---|---|
| 0 Stack, MCP, plugins | `mcp_config.json`; `mcp_servers/{rag,state,graphify}_server.py` (MCP SDK, JSON-RPC 2.0; stdio + streamable-http); HMAC-signed manifests (`manifest.py`); pluggy plugins (`plugins/`) |
| 1 `design.md` | seven mandatory sections, 12-row FMEA, ADR-1…6 — written before any code |
| 2 Workflow image | `scripts/render_workflow.py` → `docs/workflow_orchestration.png` (Graphviz, 300 DPI), rendered in Streamlit tab 1 |
| 3/8 3-stage RAG + RAGAS | `rag/` BM25(k1 1.25,b .75)∥HNSW bi-encoder → RRF(k60, dedup) → INT8 ONNX cross-encoder (σ>0.42); `evals/` local RAGAS proxies over the 250-Q stratified golden set |
| 4 Harness / loop | `harness.py` (strict Pydantic → regex repair → RBAC tier → UUIDv4 idempotency); `loop.py` state machine, ≤5 it / step / total budgets |
| 5 Context | `context.py`: immutable `GoalAnchorBlock` at prompt index 0, ≤180-token observation compression, 3-turn window + `session_facts` |
| 6/7 Triage + subagents | `triage.py` (TF-IDF+LR + deterministic guards, ~3 ms), `subagents.py` (Retrieval read-only / Execution no-RAG / Verification veto) |
| 9 Patterns | idempotent circuit breaker, bulkheads (rag 64 / db 128 / llm 256), abstention short-circuit, async HITL gate |
| 10/11 Observability | W3C traceparent, OTel SDK spans (`hook.pre … hook.post.ragas`), `/metrics`, incident runbooks (`/admin/incident`) |
| 12 KPIs | `evals/run_kpis.py` → 36-row scorecard |
| 13/14 Governance / red team | per-tenant SHA-256 audit chain, tenant_id in every query & cache key, PII redaction before persistence, 100-prompt red-team suite |
| 15/16 Load | `load_tests/k6_50k_load.js`, `jmeter_50k_plan.jmx` (spec parameters; run scaled locally) |
| 17 Tokens | `knowledge_vault/` atomic notes + `graphify.py` depth-≤2 subgraph |
| 18 Workbench | `app.py` — six tabs |
| SLM guide | `training/qlora_finetune.py` (corrected; needs GPU, not executed) |

## More docs
* [`docs/GPU.md`](docs/GPU.md) — running on a GPU (implemented, untested on GPU hardware).
* [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) — why KPI-18/21/35 (and 09/23) are not green and what to do next.
* Optional LLM answers: `LLM_BACKEND=anthropic` + your own Anthropic credentials (see KNOWN_ISSUES, KPI-21); default is offline/extractive.

## Honest limits (read before trusting any number)
* Corpus, golden set, red-team set and PII set are **synthetic and self-authored** (the specs ship none). Scores on them are optimistic versus real data, and some guards (triage rules, injection rules) were tuned after inspecting failures on these sets.
* Default generator is **extractive** (offline, deterministic): faithfulness/hallucination KPIs validate the grounding + verification pipeline, not a free-form LLM. An optional `LLM_BACKEND=anthropic` path exists but is not what the scorecard measures.
* RAGAS numbers are **local deterministic proxies** (no LLM judge) — reported `PARTIAL` at best.
* The dev box is 4 vCPU / no GPU: cross-encoder latency (≈250 ms for 8 pairs) cannot meet the 45 ms target; runtime timeouts are scaled ×4 (`LATENCY_SCALE`), KPI thresholds are not.
* The **50,000-VU / 10-injector / 8,500-RPS / 1-hour-soak** targets were **not** verified; k6 and JMeter run at reduced scale and are marked `PARTIAL`/`FAIL`/`NOT_MEASURED` accordingly. The audit report's claim that all 36 KPIs passed at 50K VUs is unsubstantiated here.
