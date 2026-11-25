"""mcp-graphify-vault: Obsidian + code dependency graph; returns the minimal depth<=2 subgraph for a query anchor."""
import time

import common  # noqa: F401
from mcp.server.mcpserver import MCPServer

from unreliable.config import settings
from unreliable.graphify import GraphStore, build_graph
from unreliable.manifest import build_manifest
from unreliable.models import ExpandIn

NAME = "graphify-vault"
TOOLS = ["expand_subgraph", "graph_stats"]
import json
if not settings.graph_path.exists():
    settings.graph_path.write_text(json.dumps(build_graph(settings.root / "src", settings.vault_dir)))
graph = GraphStore(settings.graph_path)
mcp = MCPServer("mcp-graphify-vault")


@mcp.tool()
def expand_subgraph(inp: ExpandIn) -> dict:
    """Resolve the anchor node from the query and traverse at most `depth` (<=2) outward wikilink edges."""
    t0 = time.perf_counter()
    anchor = graph.resolve_anchor(inp.query, inp.hint_note, inp.tenant_id)
    if not anchor:
        return {"anchor": None, "nodes": [], "edges": [], "text": "", "tokens": 0, "_srv_ms": (time.perf_counter() - t0) * 1000}
    r = graph.expand(anchor, inp.depth, inp.budget_tokens, inp.query, inp.tenant_id)
    r["anchor"] = anchor
    r["_srv_ms"] = (time.perf_counter() - t0) * 1000
    return r


@mcp.tool()
def graph_stats() -> dict:
    types: dict[str, int] = {}
    for n in graph.nodes.values():
        types[n["type"]] = types.get(n["type"], 0) + 1
    return {"nodes": len(graph.nodes), "types": types, "edges": sum(len(v) for v in graph.adj.values())}


@mcp.tool()
def manifest() -> dict:
    return build_manifest(NAME, TOOLS)


if __name__ == "__main__":
    mcp.run("stdio")
