#!/usr/bin/env bash
# Runs the full test matrix from Computer A, applying tc netem on A's interface.
# Usage:  sudo SERVER=192.168.1.20 IFACE=eth0 ./scripts/run_matrix.sh
# netem only shapes traffic LEAVING this interface (A -> B), so an added delay of
# 50ms shows up as ~+50ms RTT. To shape both directions, also apply it on B.
set -u
SERVER="${SERVER:?set SERVER=<ip of computer B>}"
IFACE="${IFACE:?set IFACE=<network interface, e.g. eth0 / wlp2s0>}"
RUNS="${RUNS:-3}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"

# name|netem arguments  (empty = no impairment)
CONDITIONS=(
  "baseline|"
  "delay-100ms|delay 100ms"
  "jitter-50ms+-30ms|delay 50ms 30ms"
  "loss-1pct|loss 1%"
  "loss-5pct|loss 5%"
  "bw-2mbit|rate 2mbit"
)

cleanup() { tc qdisc del dev "$IFACE" root 2>/dev/null || true; }
trap cleanup EXIT
cleanup

for entry in "${CONDITIONS[@]}"; do
  name="${entry%%|*}"; args="${entry#*|}"
  echo "=== condition: $name  (netem: ${args:-none}) ==="
  cleanup
  if [ -n "$args" ]; then tc qdisc add dev "$IFACE" root netem $args; fi
  sleep 1
  # run as the invoking user if called with sudo so the DB isn't owned by root
  $PY "$DIR/client/test_runner.py" --server "$SERVER" --condition "$name" --runs "$RUNS" \
      --db "$DIR/data/results.db"
done
echo "done. results in $DIR/data/results.db"
