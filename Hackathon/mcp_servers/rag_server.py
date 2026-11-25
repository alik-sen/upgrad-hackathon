"""mcp-hybrid-rag: 3-stage hybrid retrieval (BM25 || bi-encoder/HNSW -> RRF -> cross-encoder) over stdio JSON-RPC.
Tools: hybrid_retrieve, embed_query, manifest.  Resource: rag://chunks/{chunk_id}."""
import asyncio
import time

import common  # noqa: F401  (sys.path)
from mcp.server.mcpserver import MCPServer

from unreliable.config import settings
from unreliable.manifest import build_manifest
from unreliable.models import EmbedIn, HybridRetrieveIn
from unreliable.rag.pipeline import HybridRetriever

NAME = "hybrid-rag"
TOOLS = ["hybrid_retrieve", "embed_query"]
retriever = HybridRetriever()
sem = asyncio.Semaphore(settings.rag_pool)  # bulkhead: rag_pool=64
mcp = MCPServer("mcp-hybrid-rag")


@mcp.tool()
async def hybrid_retrieve(inp: HybridRetrieveIn) -> dict:
    """3-stage hybrid retrieval with deterministic abstention (top cross-encoder score < threshold)."""
    t0 = time.perf_counter()
    async with sem:
        out = await retriever.retrieve(inp)
    d = out.model_dump()
    d["_srv_ms"] = (time.perf_counter() - t0) * 1000
    return d


@mcp.tool()
async def embed_query(inp: EmbedIn) -> dict:
    """Bi-encoder embedding of a query (micro-batched); used by the semantic cache."""
    t0 = time.perf_counter()
    v = await retriever.embed_query(inp.query)
    return {"vec": [round(float(x), 5) for x in v], "_srv_ms": (time.perf_counter() - t0) * 1000}


@mcp.tool()
def manifest() -> dict:
    """Signed manifest."""
    return build_manifest(NAME, TOOLS)


@mcp.resource("rag://chunks/{chunk_id}")
def chunk(chunk_id: str) -> str:
    return retriever.get_chunk(chunk_id) or ""


if __name__ == "__main__":
    mcp.run("stdio")
