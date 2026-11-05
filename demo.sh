#!/usr/bin/env bash
# Fresh sandbox + one worker run. Usage: ./demo.sh [none|download_503_once|session_expire_on_route_once|route_commit_then_503_once]
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-none}"
PY=.venv/bin/python
pkill -f "uvicorn app:app --host 127.0.0.1 --port 8000" 2>/dev/null || true
sleep 1
$PY reset_sandbox.py
LINDA_FAULT_MODE="$MODE" $PY -m uvicorn app:app --host 127.0.0.1 --port 8000 >runs-server.log 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
$PY -W ignore -m linda_agent --fresh "${@:2}"
$PY - <<'PY'
import json
for r in json.load(open("runs/results.json"))["faxes"]:
    f = r["selected_fields"] or {}
    print(f'{r["fax_id"]}  {r["action"]:<18} {r["verification"] or "":<14} {f.get("doc_type") or "-":<20} {(r["duplicate_of"] or "")}')
PY
$PY -m linda_agent.report runs/results.json
