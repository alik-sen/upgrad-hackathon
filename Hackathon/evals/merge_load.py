"""Fold reports/k6_summary.json, jmeter_summary.json and soak.json into reports/kpi_report.json (KPI-31..36) and re-render KPI_SCORECARD.md."""
import json
from pathlib import Path

R = Path(__file__).resolve().parent.parent / "reports"
rep = json.loads((R / "kpi_report.json").read_text())
k6 = json.loads((R / "k6_summary.json").read_text()) if (R / "k6_summary.json").exists() else None
jm = json.loads((R / "jmeter_summary.json").read_text()) if (R / "jmeter_summary.json").exists() else None
div = json.loads((R / "k6_diverse_summary.json").read_text()) if (R / "k6_diverse_summary.json").exists() else None
soak = json.loads((R / "soak.json").read_text()) if (R / "soak.json").exists() else None


def st(val, op, thr, scaled):
    if val is None:
        return "NOT_MEASURED"
    ok = val >= thr if op == ">=" else val <= thr
    return "FAIL" if not ok else ("PARTIAL" if scaled else "PASS")


def set_row(i, val, op, thr, source, scaled, note=""):
    for k in rep["kpis"]:
        if k["id"] == f"KPI-{i:02d}":
            k.update(value=None if val is None else round(float(val), 4), status=st(val, op, thr, scaled), source=source, note=note)


if k6:
    sc = k6["vus_max"] < 50000
    dv = f"; CACHE-MISS path (diverse mix, {div['vus_max']:.0f} VUs): p50={div['p50']:.0f} p95={div['p95']:.0f} p99={div['p99']:.0f} ms at only {div['rps']:.1f} RPS (CPU-bound reranker)" if div else ""
    extra = dv + f"; JMeter {jm['threads_max']} threads p95={jm['p95']} ms err={jm['error_rate']:.4f}" if jm else ""
    for i, key, op, thr in ((31, "p50", "<=", 240), (32, "p95", "<=", 450), (33, "p99", "<=", 590)):
        set_row(i, k6[key], op, thr, f"k6 {k6['vus_max']:.0f} VUs ({k6['mix']} mix, think {k6['think_s']} s)", sc, "SCALED RUN — spec is 50,000 VUs" + extra)
    set_row(34, k6["fail_rate"], "<=", 0.0005, f"k6 {k6['vus_max']:.0f} VUs", sc, "scaled run")
    set_row(35, k6["rps"], ">=", 8500, f"k6 {k6['vus_max']:.0f} VUs", False, "offered load of a 4-vCPU box with think time; 8,500 RPS needs a cluster")
if soak:
    set_row(36, soak["rss_growth"], "<=", 0.03, f"{soak['duration_s']} s soak under k6 load (spec: 1 h)", True, f"RSS {soak['rss_baseline_mb']:.0f} -> {soak['rss_end_mb']:.0f} MB; a {soak['duration_s']} s run cannot show leaks that take an hour")
rep["summary"] = {s: sum(1 for k in rep["kpis"] if k["status"] == s) for s in ("PASS", "PARTIAL", "FAIL", "NOT_MEASURED")}
rep["details"]["load"] = {"k6_diverse": div, "k6": k6, "jmeter": jm, "soak": soak}
(R / "kpi_report.json").write_text(json.dumps(rep, indent=1, default=str))
md = ["# 36-KPI Scorecard (measured, not claimed)", "",
      f"Generated {rep['generated']} · profile `{rep['profile']}` · runtime-timeout scale ×{rep['latency_scale']} · {rep['summary']}", "",
      "Status: **PASS** measured & met · **PARTIAL** met only via proxy metric or at reduced scale · **FAIL** measured & missed · **NOT_MEASURED**.", "",
      "| KPI | Metric | Threshold | Measured | Status | Source / note |", "|---|---|---|---|---|---|"]
for k in rep["kpis"]:
    md.append(f"| {k['id']} | {k['name']} | {k['threshold']} | {k['value'] if k['value'] is not None else '—'} | **{k['status']}** | {k['source']}{' — ' + k['note'] if k['note'] else ''} |")
(R / "KPI_SCORECARD.md").write_text("\n".join(md) + "\n")
print(rep["summary"])
