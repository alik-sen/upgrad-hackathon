# Running on a GPU

**Status: implemented, NOT tested on a GPU.** The dev box that produced `reports/` has no GPU. The code path below was written against the
fastembed / onnxruntime APIs and exercised only for CPU fallback. Treat the first GPU run as a verification step, not a given.

## What changes with a GPU
| Area | CPU (default `MODEL_PROFILE=dev`) | GPU (`MODEL_PROFILE=gpu`, `DEVICE=auto|cuda`) |
|---|---|---|
| Bi-encoder | `bge-small-en-v1.5` | `bge-large-en-v1.5` (blueprint) on CUDA |
| Cross-encoder | MiniLM-L6, **INT8** dynamic-quantised, own ORT session | `bge-reranker-v2-m3` (blueprint), full precision on CUDA via fastembed |
| Rerank pool | 8 | 40 (blueprint) |
| Rerank threshold | 0.30 (calibrated on dev split for MiniLM) | 0.42 (blueprint) — **re-calibrate** (see below) |
| Runtime timeout scale (`LATENCY_SCALE`) | 4 | 1 (spec budgets: step 120 ms, loop 450 ms, MCP 150 ms) |
| Admission (`RAG_ADMIT`) | 1 | raise to 4–8 (GPU batches concurrent requests) |

## Setup
```bash
pip uninstall -y onnxruntime && pip install onnxruntime-gpu      # match your CUDA / cuDNN (see onnxruntime docs); fastembed uses it automatically
export MODEL_PROFILE=gpu DEVICE=auto RAG_ADMIT=4 ORT_THREADS=4
python -c "import onnxruntime as o; print(o.get_available_providers())"     # must list CUDAExecutionProvider
scripts/run_stack.sh restart
```
`DEVICE=auto` picks CUDA when `CUDAExecutionProvider` exists, else CPU. `DEVICE=cuda` without onnxruntime-gpu logs a note
(`settings.device_note`) and runs on CPU. `DEVICE=cpu` forces CPU.

## Must-do checks on the first GPU run
1. `python evals/run_kpis.py` — look at KPI-08/09 (`mcp.rag.biencoder`, `mcp.rag.rerank`). Expected to improve by an order of magnitude; **unmeasured**.
2. **Re-calibrate the rerank threshold.** The reranker changed, so score scales changed. Run `evals/` with `RERANK_THRESHOLD` sweeps on the golden **dev** split only
   (pattern used for the CPU profile: lowest score of an answerable gold chunk vs highest score of a non-entity negative) and set the value in `config.py`.
3. FP16: fastembed loads the ONNX model as shipped (FP32 for `bge-reranker-v2-m3`). The blueprint asks for FP16; convert the ONNX file
   (`onnxconverter-common` float16) and point `PROFILES["gpu"]["rerank_model"]` at it if you need the extra speed. Not done here.
4. Use several uvicorn workers only if each fits in GPU memory (each worker spawns its own MCP RAG server with its own models).
5. The INT8 CPU path is skipped automatically when CUDA is active (`CrossEncoder.custom` is False).

## Not affected by a GPU
KPI-18 (drift definition), KPI-21 (generator quality), KPI-35 (cluster-scale throughput) — see `docs/KNOWN_ISSUES.md`.
