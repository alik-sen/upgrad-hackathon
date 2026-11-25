"""3-stage hybrid retrieval: BM25 || Bi-Encoder(HNSW) -> RRF -> Cross-Encoder (+ deterministic abstention)."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor

import faiss
import numpy as np

from ..config import settings
from ..util import canon_ids
from ..models import Chunk, HybridRetrieveIn, HybridRetrieveOut
from .batching import MicroBatcher
from .bm25 import BM25Index, tokenize
from .chunker import chunk_notes, load_vault
from .fusion import rrf
from .models_onnx import BiEncoder, CrossEncoder


class HybridRetriever:
    def __init__(self, vault_dir=None, load_models: bool = True):
        self.notes = load_vault(vault_dir or settings.vault_dir)
        self.chunks = chunk_notes(self.notes, settings.chunk_tokens, settings.chunk_overlap)
        self.texts = [f"{c.title}. {c.text}" for c in self.chunks]
        self.hashes = [c.chunk_hash for c in self.chunks]
        self.tenants = np.array([c.tenant for c in self.chunks])
        self.bm25 = BM25Index(self.texts)
        self.ex = ThreadPoolExecutor(max_workers=settings.rag_pool, thread_name_prefix="rag")
        self.bi = self.ce = None
        self.index = None
        self.dense_ok = self.rerank_ok = False
        self.rerank_timeout_s = 0.135 * settings.latency_scale  # 3x the 45 ms budget
        if load_models:
            self._load_models()
        self.qbatch = MicroBatcher(lambda qs: list(self.bi.embed_queries(qs)), self.ex, settings.microbatch_size, settings.microbatch_wait_ms) if self.bi else None
        self.rbatch = MicroBatcher(lambda items: self._rerank_batch(items), self.ex, settings.microbatch_size, settings.microbatch_wait_ms) if self.ce else None

    # ---- setup ----
    def _load_models(self):
        self.bi = BiEncoder()
        self.ce = CrossEncoder()
        key = hashlib.sha1(("".join(self.hashes) + self.bi.name).encode()).hexdigest()[:16]
        f = settings.cache_dir / f"emb_{key}.npy"
        if f.exists():
            emb = np.load(f)
        else:
            emb = self.bi.embed_docs(self.texts)
            np.save(f, emb)
        self.emb = emb
        d = emb.shape[1]
        self.index = faiss.IndexHNSWFlat(d, settings.hnsw_m, faiss.METRIC_INNER_PRODUCT)
        self.index.hnsw.efConstruction = settings.hnsw_ef_construction
        self.index.add(emb)
        self.index.hnsw.efSearch = settings.hnsw_ef_search
        self.dense_ok = self.rerank_ok = True
        self.ce.score([("warmup query", "warmup document text")])  # warm ORT
        self.bi.embed_queries(["warmup"])

    def _rerank_batch(self, items):  # items: list[(query, [docs])]
        pairs = [(q, d) for q, docs in items for d in docs]
        scores = self.ce.score(pairs)
        out, i = [], 0
        for _, docs in items:
            out.append(scores[i:i + len(docs)]); i += len(docs)
        return out

    def _allowed(self, tenant_id: str) -> np.ndarray:
        return (self.tenants == "all") | (self.tenants == tenant_id)

    # ---- public ----
    async def embed_query(self, q: str) -> np.ndarray:
        return await self.qbatch.submit(q)

    async def retrieve(self, req: HybridRetrieveIn) -> HybridRetrieveOut:
        T: dict[str, float] = {}
        allowed = self._allowed(req.tenant_id)
        n = len(self.chunks)
        degraded = False

        # Stage 1: BM25 (inline: ~1 ms)
        t0 = time.perf_counter()
        sc = self.bm25.scores(req.query, req.k1, req.b)
        sc = np.where(allowed, sc, -1.0)
        order = np.argsort(-sc)[:req.bm25_k]
        bm_rank = [int(i) for i in order if sc[i] > 0]
        T["bm25"] = (time.perf_counter() - t0) * 1000

        # Stage 2: dense
        t0 = time.perf_counter()
        dense_rank, dsc = [], {}
        try:
            qv = np.asarray(req.query_vec, dtype=np.float32) if req.query_vec else await self.embed_query(req.query)
            qv = qv / (np.linalg.norm(qv) or 1.0)
            k = min(n, req.dense_k + int((~allowed).sum()))
            D, I = self.index.search(qv[None, :], k)
            for s, i in zip(D[0], I[0]):
                if i >= 0 and allowed[i]:
                    dense_rank.append(int(i)); dsc[int(i)] = float(s)
            dense_rank = dense_rank[:req.dense_k]
        except Exception:  # FMEA #4: BM25-only
            degraded = True
        T["biencoder"] = (time.perf_counter() - t0) * 1000

        # Fusion
        t0 = time.perf_counter()
        fused = rrf([bm_rank, dense_rank], k=req.rrf_k, hashes=self.hashes, pool=settings.rrf_pool)
        T["rrf"] = (time.perf_counter() - t0) * 1000

        # Stage 3: cross-encoder
        t0 = time.perf_counter()
        idxs = [i for i, _ in fused][:req.pool]
        # entity pinning: notes of SKU/SUP IDs named in the query always enter the rerank pool (dense retrievers blur identifiers; the entity gate needs them)
        ids = {m.lower() for m in re.findall(r"\b(?:SKU-\d{3}|SUP-\d{2})\b", canon_ids(req.query))}
        pinned = [i for i, c in enumerate(self.chunks) if c.note_id in ids and allowed[i]]
        idxs += [i for i in pinned if i not in idxs]
        top_k = req.top_k + min(len(ids), 2)
        rr: dict[int, float] = {}
        if idxs:
            try:
                if not self.rerank_ok:
                    raise RuntimeError("reranker disabled")
                scores = await asyncio.wait_for(self.rbatch.submit((req.query, [self.texts[i] for i in idxs])), self.rerank_timeout_s)
                rr = dict(zip(idxs, scores))
            except Exception:  # FMEA #3: RRF order + stricter lexical gate
                degraded = True
                qt = set(tokenize(req.query))
                for i in idxs:
                    ct = set(tokenize(self.texts[i]))
                    ov = len(qt & ct) / max(1, len(qt))
                    rr[i] = 0.6 + 0.4 * ov if ov >= 0.5 else 0.0
        T["rerank"] = (time.perf_counter() - t0) * 1000

        bm_s = {i: float(sc[i]) for i in bm_rank}
        ranked = sorted(idxs, key=lambda i: -rr.get(i, 0.0))
        top_score = rr.get(ranked[0], 0.0) if ranked else 0.0
        abstain = top_score < req.threshold
        keep = [] if abstain else [i for i in ranked if rr[i] >= req.threshold][:top_k]
        rrf_s = dict(fused)
        chunks = [Chunk(chunk_id=self.chunks[i].chunk_id, note_id=self.chunks[i].note_id, title=self.chunks[i].title, tenant=self.chunks[i].tenant, text=self.chunks[i].text,
                        bm25=bm_s.get(i), dense=dsc.get(i), rrf=rrf_s.get(i, 0.0), rerank=rr.get(i), rank=r + 1) for r, i in enumerate(keep)]
        cands = None
        if req.debug:
            cands = {"bm25": [{"chunk_id": self.chunks[i].chunk_id, "score": bm_s[i]} for i in bm_rank[:25]],
                     "dense": [{"chunk_id": self.chunks[i].chunk_id, "score": dsc[i]} for i in dense_rank[:25]],
                     "rrf": [{"chunk_id": self.chunks[i].chunk_id, "score": s} for i, s in fused[:25]],
                     "rerank": [{"chunk_id": self.chunks[i].chunk_id, "score": rr.get(i, 0.0)} for i in ranked[:25]]}
        return HybridRetrieveOut(chunks=chunks, top_score=float(top_score), abstain=abstain, degraded=degraded, timings_ms={k: round(v, 3) for k, v in T.items()}, candidates=cands)

    async def stage_candidates(self, query: str, tenant_id: str = "eval", k: int = 50) -> dict:
        """Pre-rerank candidate lists for IR evaluation (Recall@50 etc.)."""
        allowed = self._allowed(tenant_id)
        sc = np.where(allowed, self.bm25.scores(query), -1.0)
        bm = [int(i) for i in np.argsort(-sc)[:k] if sc[i] > 0]
        qv = await self.embed_query(query)
        D, I = self.index.search((qv / np.linalg.norm(qv))[None, :], min(len(self.chunks), k + int((~allowed).sum())))
        de = [int(i) for i in I[0] if i >= 0 and allowed[i]][:k]
        fused = rrf([bm, de], k=settings.rrf_k, hashes=self.hashes, pool=k)
        return {"bm25": bm, "dense": de, "fused": [i for i, _ in fused]}

    def get_chunk(self, chunk_id: str) -> str | None:
        for c in self.chunks:
            if c.chunk_id == chunk_id:
                return c.text
        return None
