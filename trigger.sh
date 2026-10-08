#!/usr/bin/env bash
# Manual operator interface for the deployed ats-watcher Lambda.
#
#   ./trigger.sh backlog A          one-off sweep of Tier A's open backlog
#   ./trigger.sh backlog A,B 150    tiers A and B, at most 150 jobs each
#   ./trigger.sh preview A          count what a sweep would send, mail nothing
#   ./trigger.sh run                force one diff cycle now (mails new jobs)
#   ./trigger.sh status             function config + schedule + state summary
#   ./trigger.sh logs [minutes]     recent INFO/WARN/ERROR lines (default 60)
#
# Env overrides: AWS_REGION, FUNCTION_NAME, STATE_BUCKET, STATE_KEY
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
FUNCTION="${FUNCTION_NAME:-ats-watcher}"
BUCKET="${STATE_BUCKET:-ats-watcher-state-<ACCOUNT_ID>}"
KEY="${STATE_KEY:-ats-watcher/state.json}"
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

# A sweep fans out over hundreds of boards; don't let the client hang up first.
invoke() {
  aws lambda invoke --function-name "$FUNCTION" --region "$REGION" \
    --cli-read-timeout 320 --cli-connect-timeout 30 \
    --payload "$(printf '%s' "$1" | base64)" "$OUT" \
    --query 'StatusCode' --output text >/dev/null
  python3 -m json.tool < "$OUT"
}

cmd="${1:-help}"
case "$cmd" in
  backlog|preview)
    tiers="${2:-A}"
    limit="${3:-}"
    dry=false; [ "$cmd" = preview ] && dry=true
    payload=$(python3 -c '
import json,sys
tiers,limit,dry = sys.argv[1], sys.argv[2], sys.argv[3]=="true"
p = {"mode":"backlog","tiers":[t.strip().upper() for t in tiers.split(",") if t.strip()]}
if limit: p["limit"]=int(limit)
if dry: p["dry_run"]=True
print(json.dumps(p))' "$tiers" "$limit" "$dry")
    echo "→ $cmd  tiers=$tiers  limit=${limit:-none}"
    echo "  payload: $payload"
    invoke "$payload"
    ;;
  run)
    echo "→ forcing one diff cycle"
    invoke '{}'
    ;;
  status)
    echo "── function ──"
    aws lambda get-function-configuration --function-name "$FUNCTION" --region "$REGION" \
      --query '{Runtime:Runtime,Memory:MemorySize,Timeout:Timeout,Modified:LastModified,State:State}' \
      --output table
    echo "── schedule ──"
    aws events describe-rule --name ats-watcher-hourly --region "$REGION" \
      --query '{Rule:Name,State:State,Schedule:ScheduleExpression}' --output table 2>/dev/null \
      || echo "  (no ats-watcher-hourly rule)"
    echo "── state ──"
    aws s3 cp "s3://$BUCKET/$KEY" - --quiet | python3 -c '
import json,sys
s=json.load(sys.stdin); c=s.get("companies",{})
print(f"  companies tracked : {len(c)}")
print(f"  job ids tracked   : {sum(len(v) for v in c.values())}")
print(f"  updated_at        : {s.get(\"updated_at\")}")
print(f"  last weekly report: {s.get(\"last_weekly_report\") or \"never\"}")
es={k:v for k,v in (s.get("empty_streak") or {}).items() if v}
fs={k:v for k,v in (s.get("fail_streak") or {}).items() if v}
print(f"  failing companies : {len(fs)} {sorted(fs)[:5]}")
print(f"  empty (unconfirmed): {len(es)} {sorted(es)[:5]}")'
    ;;
  logs)
    mins="${2:-60}"
    start=$(python3 -c "import time,sys;print(int((time.time()-int(sys.argv[1])*60)*1000))" "$mins")
    aws logs filter-log-events --log-group-name "/aws/lambda/$FUNCTION" \
      --region "$REGION" --start-time "$start" \
      --filter-pattern '?INFO ?WARNING ?ERROR' \
      --query 'events[].message' --output text | sed 's/\t/ /g' | tail -60
    ;;
  *)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
