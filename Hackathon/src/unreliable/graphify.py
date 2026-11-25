"""Graphify: structural dependency graph over Python modules, Pydantic schemas and Obsidian policy notes (Gate 17).

CLI:  python -m unreliable.graphify build ./src ./knowledge_vault --output graph.json
Runtime: GraphStore.expand(anchor, depth<=2, budget_tokens) -> minimal connected subgraph text (~320 tokens vs ~1,400 raw).
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import deque
from pathlib import Path

from .rag.bm25 import tokenize
from .rag.chunker import load_vault
from .util import est_tokens

ID_RE = re.compile(r"\b(?:SKU-\d{3}|SUP-\d{2})\b")


def build_graph(src_dir: Path, vault_dir: Path) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for n in load_vault(vault_dir):
        facts = [l[2:].strip() for l in n.body.splitlines() if l.startswith("- ")]
        nodes[f"note:{n.id}"] = {"id": f"note:{n.id}", "type": "note", "title": n.title, "domain": n.domain, "risk_tier": n.risk_tier,
                                 "tenant": n.tenant, "summary": n.summary, "facts": facts}
        for l in n.links:
            edges.append({"src": f"note:{n.id}", "dst": f"note:{l}", "rel": "wikilink"})
    mods: dict[str, str] = {}
    for p in sorted(Path(src_dir).rglob("*.py")):
        rel = str(p.relative_to(src_dir)).replace("/", ".")[:-3]
        mid = f"mod:{rel}"
        mods[rel] = mid
        nodes[mid] = {"id": mid, "type": "module", "title": rel, "summary": f"python module {rel}", "facts": []}
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
                if any(b in ("BaseModel", "Strict") or b.endswith("In") or b.endswith("Out") for b in bases) or node.name.endswith(("In", "Out")):
                    sid = f"schema:{node.name}"
                    nodes[sid] = {"id": sid, "type": "schema", "title": node.name, "summary": f"pydantic schema {node.name} defined in {rel}", "facts": []}
                    edges.append({"src": mid, "dst": sid, "rel": "defines"})
            elif isinstance(node, ast.ImportFrom) and node.level:
                target = (node.module or "").split(".")[0]
                if target:
                    edges.append({"src": mid, "dst": f"mod:{target}", "rel": "imports"})
    ids = set(nodes)
    edges = [e for e in edges if e["dst"] in ids]
    return {"nodes": list(nodes.values()), "edges": edges}


class GraphStore:
    def __init__(self, path: Path | str):
        g = json.loads(Path(path).read_text())
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.adj: dict[str, list[str]] = {}
        for e in g["edges"]:
            self.adj.setdefault(e["src"], []).append(e["dst"])  # outward wikilinks only: back-links would pull in unrelated siblings
        self.title_tokens = {nid: set(tokenize(n["title"])) for nid, n in self.nodes.items() if n["type"] == "note"}

    def resolve_anchor(self, text: str, hint_note: str | None = None, tenant: str = "all") -> str | None:
        for m in ID_RE.findall(text.upper()):
            nid = f"note:{m.lower()}"
            if nid in self.nodes:
                return nid
        if hint_note and f"note:{hint_note}" in self.nodes:
            return f"note:{hint_note}"
        q = set(tokenize(text))
        best, bs = None, 0.0
        for nid, tt in self.title_tokens.items():
            if not tt or self.nodes[nid].get("tenant", "all") not in ("all", tenant):
                continue
            s = len(q & tt) / len(tt)
            if s > bs:
                best, bs = nid, s
        return best if bs >= 0.5 else None

    @staticmethod
    def _toks(text: str) -> set[str]:
        t = {w for w in tokenize(text) if not (w.isdigit() and len(w) <= 3)}  # drop numeric fragments of IDs (sku-020 -> 20)
        t.update(f"tier_{m.lower()}" for m in re.findall(r"\bTier ([A-D])\b", text))
        return t

    def expand(self, anchor: str, depth: int = 2, budget_tokens: int = 320, query: str = "", tenant: str = "all") -> dict:
        if anchor not in self.nodes:
            return {"nodes": [], "edges": [], "text": "", "tokens": 0}
        depth = min(depth, 2)
        ctx = self._toks(query)
        seen = {anchor: 0}
        lines, kept = [], [anchor]
        a = self.nodes[anchor]
        first = " ".join(a["facts"][:6])
        lines.append(f"[{a['title']}] {first}")
        used = est_tokens(lines[0])
        frontier = [anchor]
        for hop in range(1, depth + 1):
            cand = []
            for f in frontier:
                for nb in self.adj.get(f, []):
                    if nb not in seen and nb in self.nodes and self.nodes[nb]["type"] == "note" and self.nodes[nb].get("tenant", "all") in ("all", tenant):
                        seen[nb] = hop; cand.append(nb)
            sc = {n: len(self._toks(" ".join(self.nodes[n]["facts"])) & ctx) for n in cand}
            cand = sorted([n for n in cand if sc[n] > 0], key=lambda n: -sc[n])[:4]
            frontier = []
            for nid in cand:
                n = self.nodes[nid]
                scored = sorted(n["facts"], key=lambda f: -len(self._toks(f) & ctx))
                picked = [f for f in scored[:1] if len(self._toks(f) & ctx) > 0] or n["facts"][:1]  # the single most relevant fact per hop node
                block = f"[{n['title']}] " + " ".join(picked)
                t = est_tokens(block)
                if used + t > budget_tokens:
                    continue
                lines.append(block); used += t; kept.append(nid); frontier.append(nid)
                if hop == 1:
                    ctx |= self._toks(" ".join(picked))
        es = [{"src": x, "dst": y} for x in kept for y in self.adj.get(x, []) if y in kept]
        return {"nodes": [{"id": k, "hop": seen[k], "title": self.nodes[k]["title"]} for k in kept], "edges": es, "text": "\n".join(lines), "tokens": used}


def _main():
    ap = argparse.ArgumentParser(prog="graphify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("src"); b.add_argument("vault"); b.add_argument("--output", default="graph.json")
    a = ap.parse_args()
    g = build_graph(Path(a.src), Path(a.vault))
    Path(a.output).write_text(json.dumps(g, indent=1))
    print(f"graphify: {len(g['nodes'])} nodes, {len(g['edges'])} edges -> {a.output}")


if __name__ == "__main__":
    _main()
