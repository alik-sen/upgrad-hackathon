// Grafana k6 load test — blueprint Gate 15/16 (spec parameters preserved; PEAK_VUS / DURATIONS overridable for the dev box).
//   spec run : k6 run load_tests/k6_50k_load.js                      (50,000 VUs, 12 min: needs a distributed k6 cluster)
//   dev run  : PEAK_VUS=200 SCALE_T=0.1 k6 run load_tests/k6_50k_load.js
//   diverse  : QUERY_MIX=diverse  -> random golden-set questions (exercises the cache-MISS RAG path, not just exact cache hits)
// Fixes vs the blueprint snippet: guarded JSON parse in checks (a non-JSON 5xx body would throw and mask the real error);
// summary export to reports/k6_summary.json for the KPI scorecard; p(50) threshold 240 ms (KPI-31) instead of 250.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { SharedArray } from 'k6/data';

const PEAK = parseInt(__ENV.PEAK_VUS || '50000');
const T = parseFloat(__ENV.SCALE_T || '1');           // time scale for stage durations
const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const MIX = __ENV.QUERY_MIX || 'spec';
const SLEEP = parseFloat(__ENV.THINK_S || '1');

const questions = new SharedArray('q', () => open('../data/golden_set.jsonl').split('\n').filter(Boolean).map(JSON.parse).filter(g => g.answerable).map(g => g.question));

const s = (secs) => `${Math.max(1, Math.round(secs * T))}s`;
export const options = {
  stages: [
    { duration: s(120), target: Math.round(PEAK * 0.2) },  // ramp to 10K VUs (20% of peak)
    { duration: s(180), target: PEAK },                    // ramp to 50K concurrent VUs
    { duration: s(300), target: PEAK },                    // sustain peak
    { duration: s(120), target: 0 },                       // graceful ramp-down
  ],
  thresholds: {
    'http_req_duration': ['p(50)<240', 'p(95)<450', 'p(99)<600'],   // hard 200-600 ms SLA
    'http_req_failed': ['rate<0.0005'],                              // < 0.05 % error rate
    'checks': ['rate>0.999'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function jsonField(r, f) { try { return r.json(f); } catch (e) { return undefined; } }

export default function () {
  const vuId = __VU;
  const tenant = `tenant_${vuId % 50}`;
  const query = MIX === 'diverse' ? questions[Math.floor(Math.random() * questions.length)]
                                  : 'Verify batch LOT-88392 safety protocol and return triage status';
  const payload = JSON.stringify({ tenant_id: tenant, user_id: `user_${vuId}`, query: query });
  const params = { headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': tenant } };
  const res = http.post(`${BASE}/api/v1/agent/query`, payload, params);
  check(res, {
    'status is 200': (r) => r.status === 200,
    'latency within 200-600ms SLA': (r) => r.timings.duration <= 600,
    'no cross-tenant leak': (r) => jsonField(r, 'tenant_id') === tenant,
  });
  sleep(SLEEP);
}

export function handleSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const out = {
    vus_max: data.metrics.vus_max.values.max,
    p50: d.med, p95: d['p(95)'], p99: d['p(99)'], avg: d.avg, max: d.max,
    fail_rate: data.metrics.http_req_failed.values.rate,
    rps: data.metrics.http_reqs.values.rate,
    requests: data.metrics.http_reqs.values.count,
    checks_rate: data.metrics.checks ? data.metrics.checks.values.rate : null,
    mix: MIX, think_s: SLEEP, time_scale: T,
    note: `k6 ${MIX} mix, peak ${PEAK} VUs, think ${SLEEP}s, time scale ${T}`,
  };
  const path = __ENV.SUMMARY_OUT || 'reports/k6_summary.json';
  return { [path]: JSON.stringify(out, null, 1), stdout: `\nVUs max ${out.vus_max}  p50 ${out.p50.toFixed(1)}  p95 ${out.p95.toFixed(1)}  p99 ${out.p99.toFixed(1)} ms  fail ${(out.fail_rate * 100).toFixed(3)}%  rps ${out.rps.toFixed(1)}\n` };
}
