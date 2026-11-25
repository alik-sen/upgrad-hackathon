#!/usr/bin/env bash
# usage: scripts/q.sh "query" [user] [role] [tenant]
curl -s -X POST localhost:${PORT:-8000}/api/v1/agent/query -H 'content-type: application/json' -H "X-Tenant-ID: ${4:-t1}" -H "X-Role: ${3:-reader}" \
 -d "$(python3 -c 'import json,sys; print(json.dumps({"tenant_id":sys.argv[4],"user_id":sys.argv[2],"query":sys.argv[1],"debug":True}))' "$1" "${2:-u1}" "${3:-reader}" "${4:-t1}")" | python3 -c "
import sys,json; d=json.load(sys.stdin); print(d.get('route'),d.get('status'),'|',(d.get('answer') or str(d))[:400],'|',d.get('latency_ms'),'ms steps',d.get('steps'),'cache',d.get('cache_hit'))
t=(d.get('trace') or [{}])[0]; print('   spans:',{s['name']:s['ms'] for s in t.get('spans',[])}, t.get('abstain_reason'))"
