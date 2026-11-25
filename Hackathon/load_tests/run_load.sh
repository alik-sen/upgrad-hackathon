#!/usr/bin/env bash
# Scaled load run: k6 (+ optional JMeter) against the local stack, writing reports/k6_summary.json, reports/soak.json, reports/jmeter_summary.json.
# usage: PEAK_VUS=200 SCALE_T=0.25 QUERY_MIX=spec|diverse WORKERS=1 JMETER=1 bash load_tests/run_load.sh
# The 50,000-VU spec run needs a distributed k6/JMeter cluster (G-18); this script is the same test at dev-box scale.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
K6="${K6:-$HOME/tools/k6-v0.55.0-linux-amd64/k6}"; JM="${JM:-$HOME/tools/apache-jmeter-5.6.3/bin/jmeter}"
export PEAK_VUS="${PEAK_VUS:-200}" SCALE_T="${SCALE_T:-0.25}" QUERY_MIX="${QUERY_MIX:-spec}" THINK_S="${THINK_S:-1}"
[ -z "${NO_STACK:-}" ] && WORKERS="${WORKERS:-1}" scripts/run_stack.sh restart
python_bin="$ROOT/.venv/bin/python"
TOTAL=$(python3 -c "print(int((120+180+300+120)*float('$SCALE_T')))")
"$python_bin" load_tests/soak.py "$TOTAL" reports/soak.json > /tmp/soak.out 2>&1 &
SOAK=$!
"$K6" run load_tests/k6_50k_load.js 2>&1 | tail -25
wait $SOAK; cat /tmp/soak.out
if [ -n "${JMETER:-}" ]; then
  rm -f reports/jmeter.jtl
  JVM_ARGS="-Xms512m -Xmx1g" "$JM" -n -t load_tests/jmeter_50k_plan.jmx -Jthreads="${JM_THREADS:-100}" -Jramp="${JM_RAMP:-20}" -Jduration="${JM_DURATION:-60}" -l reports/jmeter.jtl 2>&1 | tail -8
  "$python_bin" load_tests/parse_jtl.py reports/jmeter.jtl reports/jmeter_summary.json
fi
