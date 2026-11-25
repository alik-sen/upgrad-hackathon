"""Gate 2: render docs/workflow_orchestration.png (300 DPI) with Graphviz.

DOT is built with the `graphviz` python library. Rendering uses the `dot` binary when present, otherwise
Graphviz compiled to WASM (@hpcc-js/wasm-graphviz via node; installed in ~/tools/gv because the dev box has no sudo).
SVG -> PNG rasterisation at 300 DPI via CairoSVG.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import graphviz

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def build() -> graphviz.Digraph:
    g = graphviz.Digraph("workflow", format="svg")
    g.attr(rankdir="TB", fontname="Helvetica", fontsize="20", labelloc="t", compound="true", nodesep="0.35", ranksep="0.55",
           label="The Unreliable Assistant — Enterprise Workflow Orchestration (Gate 2)")
    g.attr("node", fontname="Helvetica", fontsize="12", shape="box", style="rounded,filled", fillcolor="white")
    g.attr("edge", fontname="Helvetica", fontsize="10")

    g.node("client", "Client / k6 / JMeter\nPOST /api/v1/agent/query", shape="oval", fillcolor="#e8eefc")

    with g.subgraph(name="cluster_ingress") as c:
        c.attr(label="1. Ingress & Security Layer  (hook.pre ≤ 15 ms)", style="filled", color="#c9d6f2", fillcolor="#f3f6fd")
        c.node("ingress", "Ingress\n(auth, tenant check)")
        c.node("pii", "Pre-Exec Plugin 1\nPII Redactor (prio 10, fail-closed)", fillcolor="#fde9d9")
        c.node("inj", "Pre-Exec Plugin 2\nInjection Classifier (prio 20, fail-closed)", fillcolor="#fde9d9")
        c.node("cache", "Redis Semantic Cache Check\nkey = SHA256(tenant:user:norm_query)", shape="cylinder", fillcolor="#fff2cc")
        c.edge("ingress", "pii"); c.edge("pii", "inj"); c.edge("inj", "cache")

    with g.subgraph(name="cluster_triage") as c:
        c.attr(label="2. Triage & Subagent Routing", style="filled", color="#cfe8cf", fillcolor="#f2faf2")
        c.node("triage", "Triage Agent\ntemperature=0.0 · <30 ms (KPI ≤25 ms)\n(intent, domain, risk_tier, complexity)", fillcolor="#d9ead3")
        c.node("direct", "Direct Response\nDIRECT_ANSWER / REJECT_OOS")
        c.node("retr", "Retrieval Subagent\nHYBRID_RAG (read-only)")
        c.node("exec", "Execution Subagent\nTRANSACTION_EXEC (no RAG/web)")
        c.node("audit", "Audit / Verification Subagent\nclaim↔chunk NLI, veto", fillcolor="#ead1dc")
        for t in ("direct", "retr", "exec"):
            c.edge("triage", t)

    with g.subgraph(name="cluster_mcp") as c:
        c.attr(label="3. MCP (JSON-RPC 2.0) & 3-Stage RAG Pipeline", style="filled", color="#d9c9ee", fillcolor="#f7f2fc")
        c.node("rag", "mcp-hybrid-rag  (stdio)", fillcolor="#e4d7f5")
        c.node("bm25", "Stage 1  BM25Okapi + Porter\nk1=1.25 b=0.75 top50 <12 ms")
        c.node("dense", "Stage 2  Bi-Encoder + FAISS HNSW\nefSearch=64 top50 <25 ms")
        c.node("rrf", "RRF  k=60, dedup chunk_hash\npool=40 <3 ms")
        c.node("ce", "Stage 3  Cross-Encoder (ONNX)\nrerank_k=4, sigmoid>0.42 <45 ms")
        c.node("graph", "mcp-graphify-vault (stdio)\ndepth≤2 subgraph ~320 tok", fillcolor="#e4d7f5")
        c.node("state", "mcp-state-store (streamable-http :8081)\nPostgres/SQLite: session_facts, HITL, audit", shape="cylinder", fillcolor="#e4d7f5")
        c.edge("rag", "bm25"); c.edge("rag", "dense"); c.edge("bm25", "rrf"); c.edge("dense", "rrf"); c.edge("rrf", "ce")

    with g.subgraph(name="cluster_ctrl") as c:
        c.attr(label="4. Control Plane & Egress", style="filled", color="#f2d2a9", fillcolor="#fff8ee")
        c.node("react", "Bounded ReAct Loop  max_steps=5 · 120 ms/step · 450 ms total\nGoalAnchorBlock @0 · observations ≤180 tok · window=3", fillcolor="#fce5cd")
        c.node("harness", "ToolExecutionHarness\nPydantic v2 → regex repair → RBAC tier → UUIDv4 idem")
        c.node("breaker", "Circuit Breaker\n≥2 identical (tool,args_hash) failures → HALTED_BREAKER", fillcolor="#f4cccc")
        c.node("hitl", "Tier 3 HITL Gate\nPENDING_APPROVAL → human approve/reject", shape="octagon", fillcolor="#f4cccc")
        c.node("abstain", "Deterministic Abstention\ntop score < 0.42 → no LLM · <90 ms", fillcolor="#fff2cc")
        c.node("gen", "Grounded Generation\n(extractive | LLM quote-only)")
        c.node("post", "Post-Exec Plugins\nRAGAS auditor (faith ≥ 0.92) → OTel export", fillcolor="#fde9d9")
        c.edge("react", "harness"); c.edge("harness", "breaker"); c.edge("breaker", "hitl", label="T3 call")

    g.node("resp", "Response\n(answer, citations, faithfulness, trace_id)", shape="oval", fillcolor="#e8eefc")
    g.node("obs", "OTel Collector → Prometheus + Tempo + Grafana", shape="component", fillcolor="#eeeeee")

    g.edge("client", "ingress")
    g.edge("cache", "resp", label="HIT", style="dashed", color="#bf9000")
    g.edge("cache", "triage", label="MISS")
    g.edge("retr", "react"); g.edge("exec", "react")
    g.edge("react", "rag", label="hybrid_retrieve (JSON-RPC)", lhead="cluster_mcp")
    g.edge("react", "graph", style="dotted"); g.edge("harness", "state", label="tools / facts / audit")
    g.edge("ce", "abstain", label="score<0.42"); g.edge("ce", "gen", label="score≥0.42")
    g.edge("abstain", "post"); g.edge("gen", "audit"); g.edge("audit", "post", label="veto → abstain")
    g.edge("direct", "post"); g.edge("hitl", "post", label="PENDING_APPROVAL")
    g.edge("post", "resp"); g.edge("post", "obs", style="dashed")
    g.edge("inj", "resp", label="BLOCKED", style="dashed", color="#cc0000")
    return g


def render() -> Path:
    g = build()
    DOCS.mkdir(exist_ok=True)
    dot_path = DOCS / "workflow_orchestration.dot"
    svg_path = DOCS / "workflow_orchestration.svg"
    dot_path.write_text(g.source)
    if shutil.which("dot"):
        subprocess.run(["dot", "-Tsvg", str(dot_path), "-o", str(svg_path)], check=True)
    else:
        node_dir = Path.home() / "tools" / "gv"
        subprocess.run(["node", str(node_dir / "render.mjs"), str(dot_path), str(svg_path)], check=True, cwd=node_dir)
    import cairosvg
    png = DOCS / "workflow_orchestration.png"
    cairosvg.svg2png(url=str(svg_path), write_to=str(png), dpi=300, background_color="white")
    from PIL import Image
    im = Image.open(png)
    im.save(png, dpi=(300, 300))
    return png


if __name__ == "__main__":
    out = render()
    print("wrote", out, file=sys.stderr)
