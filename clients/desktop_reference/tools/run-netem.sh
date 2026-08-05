#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCENARIOS="clean,delay,random-loss-1,random-loss-3,random-loss-5,burst-loss,jitter,reorder,udp-blocked"
PROFILES="wss-opus/1,udp-opus-gcm/1"
REPEATS="5"
SEED="20260805"
OUTPUT="$ROOT/artifacts/netem"

usage() {
  cat <<'EOF'
Usage: sudo clients/desktop_reference/tools/run-netem.sh [options]
  --scenarios CSV  clean,delay,random-loss-1,random-loss-3,random-loss-5,burst-loss,jitter,reorder,udp-blocked
  --profiles CSV   wss-opus/1,udp-opus-gcm/1
  --repeats N      attempts per scenario/profile (1..50)
  --seed N         requested tc seed base (used only when supported)
  --output DIR     JSONL/JSON output directory
EOF
}

while (($#)); do
  case "$1" in
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    --profiles) PROFILES="$2"; shift 2 ;;
    --repeats) REPEATS="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || { echo "Linux is required" >&2; exit 2; }
[[ "$EUID" -eq 0 ]] || { echo "root is required for network namespaces and tc" >&2; exit 2; }
for command in ip tc uv; do
  command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 2; }
done
[[ -e /proc/net/psched ]] || { echo "kernel traffic control support is unavailable" >&2; exit 2; }

NS="rva-netem-$$"
cleanup() {
  ip netns exec "$NS" tc qdisc del dev lo root >/dev/null 2>&1 || true
  ip netns delete "$NS" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

ip netns add "$NS"
ip -n "$NS" link set lo up
# Probe sch_netem in the isolated namespace before starting the Product cluster.
ip netns exec "$NS" tc qdisc add dev lo root netem delay 1ms
ip netns exec "$NS" tc qdisc del dev lo root
mkdir -p "$OUTPUT"

ip netns exec "$NS" env \
  PATH="$PATH" \
  RVA_NETEM_NAMESPACE=1 \
  RVA_NETEM_SCENARIOS="$SCENARIOS" \
  RVA_NETEM_PROFILES="$PROFILES" \
  RVA_NETEM_REPEATS="$REPEATS" \
  RVA_NETEM_SEED="$SEED" \
  RVA_NETEM_OUTPUT="$OUTPUT" \
  uv run --directory "$ROOT/clients/desktop_reference" pytest \
    tests/e2e/netem_deterministic_host.py -m e2e_host

ip netns exec "$NS" uv run --directory "$ROOT/clients/desktop_reference" python - "$OUTPUT/aggregate.json" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    "netem capability:",
    f"tc_seed_control={report['tc_seed_control']}",
    f"paired_randomness={str(report['paired_randomness']).lower()}",
    f"comparison_limit={report['comparison_limit']}",
)
PY
