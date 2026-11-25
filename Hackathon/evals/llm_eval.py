"""Golden-set RAGAS-proxy run against a stack started with LLM_BACKEND=anthropic -> reports/kpi21_llm.json"""
import asyncio, json
import online
from common import Api, ROOT
async def main():
    api = Api(); await api.admin("POST", "telemetry/reset")
    res = await online.golden_run(api, "test")
    r = online.ragas_proxies(res)
    st = await api.status()
    r["llm_judge_calls"] = st["counters"].get("llm.judge_calls", 0)
    r["status_counts"] = {k: v for k, v in st["counters"].items() if k.startswith("status.")}
    r["e2e_p50_ms"] = st["latency"].get("e2e", {}).get("p50"); r["e2e_p95_ms"] = st["latency"].get("e2e", {}).get("p95")
    (ROOT / "reports" / "kpi21_llm.json").write_text(json.dumps(r, indent=1)); print(json.dumps(r, indent=1))
asyncio.run(main())
