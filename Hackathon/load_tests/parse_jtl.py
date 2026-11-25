"""Summarise a JMeter .jtl (CSV) into reports/jmeter_summary.json: percentiles, error rate, RPS, assertion failures."""
import csv, json, sys
from pathlib import Path

jtl = Path(sys.argv[1]); out = Path(sys.argv[2])
rows = list(csv.DictReader(jtl.open()))
el = sorted(int(r["elapsed"]) for r in rows)
ts = [int(r["timeStamp"]) for r in rows]
pct = lambda p: el[min(len(el) - 1, int(len(el) * p / 100))]
dur = (max(ts) - min(ts)) / 1000 or 1
bad = [r for r in rows if r["success"] != "true"]
res = {"samples": len(rows), "p50": pct(50), "p95": pct(95), "p99": pct(99), "max": el[-1], "error_rate": len(bad) / len(rows), "rps": len(rows) / dur,
       "threads_max": max(int(r["allThreads"]) for r in rows), "failure_messages": sorted({r["failureMessage"][:80] for r in bad if r.get("failureMessage")})[:5]}
out.write_text(json.dumps(res, indent=1)); print(res)
