"""Central configuration. Every number here traces to design.md / the blueprint; env-overridable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("UA_ROOT", Path(__file__).resolve().parents[2]))

PROFILES = {
    # GPU profile: the blueprint models, full precision on CUDA (see docs/GPU.md). Pool 40 / timeout scale 1 / threshold 0.42 apply automatically (any non-dev profile).
    "gpu": {"embed_model": "BAAI/bge-large-en-v1.5", "rerank_model": "BAAI/bge-reranker-v2-m3", "dim": 1024},
    # CPU-friendly stand-ins (G-17)
    "dev": {"embed_model": "BAAI/bge-small-en-v1.5", "rerank_model": "Xenova/ms-marco-MiniLM-L-6-v2", "dim": 384},
    # models named in the blueprint (int8 ONNX for the reranker on CPU)
    "spec": {"embed_model": "BAAI/bge-large-en-v1.5", "rerank_model": "BAAI/bge-reranker-v2-m3-int8", "dim": 1024},
}


def _cuda_available() -> bool:
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:  # noqa: BLE001
        return False


def _device() -> str:
    """DEVICE=auto|cpu|cuda. 'cuda' without onnxruntime-gpu falls back to cpu (logged by `unreliable.config.settings.device_note`)."""
    want = os.environ.get("DEVICE", "auto").lower()
    if want == "cpu":
        return "cpu"
    return "cuda" if _cuda_available() else "cpu"


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    root: Path = ROOT
    vault_dir: Path = ROOT / "knowledge_vault"
    data_dir: Path = ROOT / "data"
    reports_dir: Path = ROOT / "reports"
    cache_dir: Path = ROOT / ".cache"
    db_path: Path = Path(os.environ.get("STATE_DB", ROOT / ".cache" / "state.db"))
    graph_path: Path = ROOT / "graph.json"

    profile: str = os.environ.get("MODEL_PROFILE", "dev")
    device: str = _device()
    device_note: str = ("DEVICE=cuda requested but onnxruntime-gpu/CUDAExecutionProvider not available -> running on CPU"
                        if os.environ.get("DEVICE", "auto").lower() == "cuda" and not _cuda_available() else "")
    ort_threads: int = _i("ORT_THREADS", 2)
    # Dev CPU box is ~10-20x slower than the GPU/ONNX target (G-17): runtime *timeouts* scale by this factor, KPI thresholds never do.
    latency_scale: float = _f("LATENCY_SCALE", 4.0 if os.environ.get("MODEL_PROFILE", "dev") == "dev" else 1.0)

    # --- RAG (Gate 3/8) ---
    chunk_tokens: int = 512
    chunk_overlap: int = 64
    bm25_k1: float = 1.25
    bm25_b: float = 0.75
    bm25_top_k: int = _i("BM25_TOP_K", 50)
    dense_top_k: int = _i("DENSE_TOP_K", 50)
    hnsw_m: int = 32
    hnsw_ef_construction: int = 200
    hnsw_ef_search: int = 64
    rrf_k: int = 60
    rrf_pool: int = _i("RRF_POOL", 40)  # fusion pool (spec 40)
    rerank_pool: int = _i("RERANK_POOL", 8 if os.environ.get("MODEL_PROFILE", "dev") == "dev" else 40)  # candidates sent to the cross-encoder
    rerank_k: int = 4
    rerank_threshold: float = _f("RERANK_THRESHOLD", 0.30 if os.environ.get("MODEL_PROFILE", "dev") == "dev" else 0.42)  # dev: calibrated on golden DEV split (GAPS C-10)
    rerank_max_len: int = 512
    rerank_max_len_dev: int = _i("RERANK_MAX_LEN_DEV", 160)  # dev-profile CPU cap (G-17)
    rerank_batch: int = 16
    microbatch_size: int = 32
    microbatch_wait_ms: float = 8.0

    # --- loop (Gate 4) ---
    max_iterations: int = 5
    per_step_timeout_ms: int = _i("PER_STEP_TIMEOUT_MS", int(120 * _f("LATENCY_SCALE", 4.0 if os.environ.get("MODEL_PROFILE", "dev") == "dev" else 1.0)))
    total_loop_budget_ms: int = _i("TOTAL_LOOP_BUDGET_MS", int(450 * _f("LATENCY_SCALE", 4.0 if os.environ.get("MODEL_PROFILE", "dev") == "dev" else 1.0)))
    breaker_max_identical: int = 2

    # --- context (Gate 5) ---
    observation_max_tokens: int = 180
    window_turns: int = 3

    # --- admission control: concurrent CPU-bound retrievals admitted per API worker; waiters beyond max_queue_wait are shed with 503 ---
    rag_admit: int = _i("RAG_ADMIT", 1)
    max_queue_wait_s: float = _f("MAX_QUEUE_WAIT_S", 2.0)
    # --- bulkheads (Gate 9) ---
    rag_pool: int = 64
    db_pool: int = 128
    llm_pool: int = 256
    mcp_timeout_ms: int = _i("MCP_TIMEOUT_MS", int(150 * _f("LATENCY_SCALE", 4.0 if os.environ.get("MODEL_PROFILE", "dev") == "dev" else 1.0)))

    # --- cache (Gate 10/11) ---
    redis_url: str = os.environ.get("REDIS_URL", "")
    cache_ttl_s: int = 300
    cache_ttl_jitter_s: int = 45
    semantic_threshold: float = _f("SEMANTIC_THRESHOLD", 0.95)
    cache_max_entries_per_partition: int = 64

    # --- generation / cost (G-05, G-09) ---
    llm_backend: str = os.environ.get("LLM_BACKEND", "extractive")
    llm_model: str = os.environ.get("LLM_MODEL", "claude-haiku-5-5")
    price_in_per_mtok: float = _f("PRICE_PER_MTOK_IN", 0.80)
    price_out_per_mtok: float = _f("PRICE_PER_MTOK_OUT", 4.00)
    min_faithfulness: float = 0.92

    # --- governance ---
    manifest_key: str = os.environ.get("MANIFEST_SIGNING_KEY", "dev-only-manifest-key")
    reviewer_api_key: str = os.environ.get("REVIEWER_API_KEY", "dev-reviewer-key")
    state_url: str = os.environ.get("STATE_URL", "http://127.0.0.1:8081/mcp")
    mcp_mode: str = os.environ.get("MCP_MODE", "stdio")  # stdio|inproc (inproc only for unit tests)

    @property
    def use_cuda(self) -> bool:
        return self.device == "cuda"

    @property
    def models(self) -> dict:
        return PROFILES[self.profile]


settings = Settings()
settings.cache_dir.mkdir(parents=True, exist_ok=True)
settings.reports_dir.mkdir(parents=True, exist_ok=True)
