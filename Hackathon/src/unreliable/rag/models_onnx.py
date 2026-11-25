"""ONNX model wrappers (fastembed = ONNX Runtime). Bi-encoder + cross-encoder; both profile-selectable (dev|spec)."""
from __future__ import annotations

import numpy as np

from ..config import settings


def _cache_dir() -> str:
    d = settings.cache_dir / "models"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _ort_kwargs() -> dict:
    """CUDA execution provider when DEVICE resolves to cuda (needs onnxruntime-gpu); CPU thread count otherwise."""
    return {"cuda": True, "device_ids": [0]} if settings.use_cuda else {}


class BiEncoder:
    def __init__(self, model: str | None = None):
        from fastembed import TextEmbedding
        self.name = model or settings.models["embed_model"]
        self.m = TextEmbedding(self.name, threads=settings.ort_threads, cache_dir=_cache_dir(), **_ort_kwargs())

    def embed_docs(self, texts: list[str]) -> np.ndarray:
        v = np.asarray(list(self.m.embed(texts, batch_size=32)), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        v = np.asarray(list(self.m.query_embed(texts)), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


class CrossEncoder:
    """Cross-encoder reranker.

    dev profile : MiniLM-L6 dynamically quantised to INT8 (ADR-2), own ORT session, dynamic padding, length-sorted batches, max_len capped.
    spec profile: fastembed's bge-reranker-v2-m3 (int8 ONNX).
    """

    def __init__(self, model: str | None = None):
        self.name = model or settings.models["rerank_model"]
        self.custom = settings.profile == "dev" and "MiniLM" in self.name and not settings.use_cuda   # INT8 CPU path; GPU uses fastembed + CUDA
        if self.custom:
            self._init_custom()
        else:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            self.m = TextCrossEncoder(self.name, threads=settings.ort_threads, cache_dir=_cache_dir(), **_ort_kwargs())

    def _init_custom(self):
        import onnxruntime as ort
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        base = TextCrossEncoder(self.name, threads=settings.ort_threads, cache_dir=_cache_dir())  # downloads + gives tokenizer/model path
        src = base.model.model_path if hasattr(base.model, "model_path") else None
        import glob
        src = src or glob.glob(f"{_cache_dir()}/models--*MiniLM-L-6*/snapshots/*/onnx/model.onnx")[0]
        dst = settings.cache_dir / "minilm_l6_int8.onnx"
        if not dst.exists():
            from onnxruntime.quantization import QuantType, quantize_dynamic
            quantize_dynamic(str(src), str(dst), weight_type=QuantType.QInt8)
        so = ort.SessionOptions()
        so.intra_op_num_threads = settings.ort_threads
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")  # avoid spin-wait contention with the bi-encoder session
        self.sess = ort.InferenceSession(str(dst), so, providers=["CPUExecutionProvider"])
        self.tok = base.model.tokenizer
        self.tok.enable_truncation(max_length=settings.rerank_max_len_dev)
        self.tok.no_padding()
        self.input_names = {i.name for i in self.sess.get_inputs()}
        self.m = None

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Returns sigmoid(logit) per (query, doc) pair."""
        if not self.custom:
            out = [0.0] * len(pairs)
            by_q: dict[str, list[int]] = {}
            for i, (q, _) in enumerate(pairs):
                by_q.setdefault(q, []).append(i)
            for q, idxs in by_q.items():
                for i, l in zip(idxs, self.m.rerank(q, [pairs[i][1] for i in idxs], batch_size=32)):
                    out[i] = float(1.0 / (1.0 + np.exp(-l)))
            return out
        if not pairs:
            return []
        enc = self.tok.encode_batch(pairs)
        order = sorted(range(len(pairs)), key=lambda i: len(enc[i].ids))
        out = [0.0] * len(pairs)
        for s in range(0, len(order), settings.rerank_batch):
            idx = order[s:s + settings.rerank_batch]
            L = max(len(enc[i].ids) for i in idx)
            ids = np.zeros((len(idx), L), np.int64); am = np.zeros((len(idx), L), np.int64); tt = np.zeros((len(idx), L), np.int64)
            for r, i in enumerate(idx):
                n = len(enc[i].ids)
                ids[r, :n] = enc[i].ids; am[r, :n] = 1; tt[r, :n] = enc[i].type_ids
            feed = {"input_ids": ids, "attention_mask": am}
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = tt
            logits = self.sess.run(None, feed)[0].reshape(len(idx), -1)[:, 0]
            for r, i in enumerate(idx):
                out[i] = float(1.0 / (1.0 + np.exp(-logits[r])))
        return out
