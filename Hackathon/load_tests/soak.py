"""Sample RSS (MB) of the API process tree (uvicorn workers + MCP children) every 5 s; write reports/soak.json (growth between 20 % mark and end)."""
import json, sys, time
import psutil
from pathlib import Path

dur = float(sys.argv[1]); out = Path(sys.argv[2])
def tree_rss():
    tot = 0
    for p in psutil.process_iter(["cmdline", "memory_info"]):
        c = " ".join(p.info["cmdline"] or [])
        if any(k in c for k in ("unreliable.api", "rag_server.py", "graphify_server.py", "state_server.py", "multiprocessing.spawn")) and "soak.py" not in c:
            tot += p.info["memory_info"].rss
    return tot / 1e6
samples = []; t0 = time.time()
while time.time() - t0 < dur:
    samples.append((time.time() - t0, tree_rss())); time.sleep(5)
base = [m for t, m in samples if t >= dur * 0.2][0] if samples else 0
res = {"duration_s": round(dur), "rss_start_mb": samples[0][1] if samples else 0, "rss_baseline_mb": base, "rss_end_mb": samples[-1][1] if samples else 0,
       "rss_growth": (samples[-1][1] - base) / base if base else None, "samples": len(samples)}
out.write_text(json.dumps(res, indent=1)); print(res)
