#!/usr/bin/env bash
# Repeated open-loop comparison of published Python OpenFeature provider paths.
#
# Defaults:
#   - 50 generated distinct flags in the catalog
#   - 3 and 50 distinct evaluations per request
#   - 5 offered requests/second (conservative across every provider path)
#   - 5,000 stable virtual user IDs
#   - direct and threaded_batch strategies
#   - three interleaved repetitions
#
# threaded_parallel is available but intentionally excluded from the default
# matrix because it adds provider concurrency as well as event-loop offload.
set -euo pipefail

ROOT="${APP_DIR:-$(cd "$(dirname "$0")" && pwd)}"
if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
PORT="${PORT:-8000}"
CATALOG_SIZE="${FLAG_CATALOG_SIZE:-50}"
FLAG_COUNTS_RAW="${FLAG_COUNTS:-3 50}"
RATES_RAW="${RATES:-5}"
BACKENDS_RAW="${BACKENDS:-flipt flagd_rpc flagd_inprocess}"
STRATEGIES_RAW="${STRATEGIES:-direct threaded_batch}"
REPS="${REPS:-3}"
DURATION="${DURATION:-15}"
WARMUP="${WARMUP:-3}"
VIRTUAL_USERS="${VIRTUAL_USERS:-5000}"
MAX_CONNECTIONS="${MAX_CONNECTIONS:-2000}"
POOL="${FLAG_POOL_SIZE:-16}"
DEADLINE="${FLAG_DEADLINE_MS:-500}"
SETTLE_SECONDS="${SETTLE_SECONDS:-2}"
RUN_MICRO="${RUN_MICRO:-1}"
MICRO_FLAG_COUNTS="${MICRO_FLAG_COUNTS:-1,3,10,25,50}"
MICRO_ROUNDS="${MICRO_ROUNDS:-6}"
RUN_STRESS="${RUN_STRESS:-0}"
STRESS_CONCURRENCY="${STRESS_CONCURRENCY:-20}"
STRESS_DURATION="${STRESS_DURATION:-10}"
STRESS_REPS="${STRESS_REPS:-3}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="${OUT:-$ROOT/results/$RUN_ID.jsonl}"
REPORT_OUT="${REPORT_OUT:-$ROOT/results/$RUN_ID.md}"
VALIDATION_OUT="${VALIDATION_OUT:-$ROOT/results/$RUN_ID.validation.json}"
METADATA_OUT="${METADATA_OUT:-$ROOT/results/$RUN_ID.metadata.json}"
MICRO_OUT="${MICRO_OUT:-$ROOT/results/$RUN_ID.micro.json}"

read -r -a FLAG_COUNT_LIST <<< "${FLAG_COUNTS_RAW//,/ }"
read -r -a RATE_LIST <<< "${RATES_RAW//,/ }"
read -r -a BACKEND_LIST <<< "${BACKENDS_RAW//,/ }"
read -r -a STRATEGY_LIST <<< "${STRATEGIES_RAW//,/ }"
CONFIGS=()
for backend in "${BACKEND_LIST[@]}"; do
  for strategy in "${STRATEGY_LIST[@]}"; do
    CONFIGS+=("$backend+$strategy")
  done
done
CONFIGS_RAW="${CONFIGS[*]}"

for backend in "${BACKEND_LIST[@]}"; do
  case "$backend" in
    flipt|flagd_rpc|flagd_inprocess) ;;
    *) echo "Invalid backend: $backend" >&2; exit 1 ;;
  esac
done
for strategy in "${STRATEGY_LIST[@]}"; do
  case "$strategy" in
    direct|threaded_batch|threaded_parallel) ;;
    *) echo "Invalid strategy: $strategy" >&2; exit 1 ;;
  esac
done
if (( ${#CONFIGS[@]} == 0 )); then
  echo "At least one backend/strategy configuration is required" >&2
  exit 1
fi
if (( ${#FLAG_COUNT_LIST[@]} == 0 )); then
  echo "At least one flag count is required" >&2
  exit 1
fi
if [[ ! "$CATALOG_SIZE" =~ ^[0-9]+$ ]] || (( CATALOG_SIZE < 1 )); then
  echo "FLAG_CATALOG_SIZE must be a positive integer" >&2
  exit 1
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "PORT must be an integer from 1 through 65535" >&2
  exit 1
fi

"$PYTHON_BIN" - "$RATES_RAW" "$DURATION" "$WARMUP" "$REPS" \
  "$VIRTUAL_USERS" "$MAX_CONNECTIONS" "$POOL" "$DEADLINE" \
  "$RUN_MICRO" "$RUN_STRESS" "$STRESS_DURATION" "$STRESS_REPS" \
  "$SETTLE_SECONDS" "$MICRO_ROUNDS" "$STRESS_CONCURRENCY" <<'PYVALIDATE'
import math
import sys

rates = [float(item) for item in sys.argv[1].replace(",", " ").split()]
duration = float(sys.argv[2])
warmup = float(sys.argv[3])
reps = int(sys.argv[4])
users = int(sys.argv[5])
max_connections = int(sys.argv[6])
pool = int(sys.argv[7])
deadline = int(sys.argv[8])
run_micro = sys.argv[9]
run_stress = sys.argv[10]
stress_duration = float(sys.argv[11])
stress_reps = int(sys.argv[12])
settle_seconds = float(sys.argv[13])
micro_rounds = int(sys.argv[14])
stress_concurrency = int(sys.argv[15])

if not rates or any(not math.isfinite(rate) or rate <= 0 for rate in rates):
    raise SystemExit("RATES must contain finite positive numbers")
if not math.isfinite(duration) or not math.isfinite(warmup):
    raise SystemExit("DURATION and WARMUP must be finite")
if duration <= 0 or warmup < 0:
    raise SystemExit("DURATION must be positive and WARMUP cannot be negative")
if reps < 1 or users < 1 or max_connections < 1 or pool < 1 or deadline < 1:
    raise SystemExit("REPS, users, connections, pool, and deadline must be positive")
if run_micro not in {"0", "1"} or run_stress not in {"0", "1"}:
    raise SystemExit("RUN_MICRO and RUN_STRESS must be 0 or 1")
if not math.isfinite(stress_duration) or stress_duration <= 0 or stress_reps < 1:
    raise SystemExit("STRESS_DURATION and STRESS_REPS must be finite and positive")
if not math.isfinite(settle_seconds) or settle_seconds < 0:
    raise SystemExit("SETTLE_SECONDS must be finite and non-negative")
if micro_rounds < 1 or stress_concurrency < 1:
    raise SystemExit("MICRO_ROUNDS and STRESS_CONCURRENCY must be positive")
PYVALIDATE

export FLAG_CATALOG_SIZE="$CATALOG_SIZE"
export FLAG_COUNTS="$FLAG_COUNTS_RAW"
export RATES="$RATES_RAW"
export BACKENDS="$BACKENDS_RAW"
export STRATEGIES="$STRATEGIES_RAW"
export REPS DURATION WARMUP VIRTUAL_USERS MAX_CONNECTIONS
export FLAG_POOL_SIZE="$POOL"
export FLAG_DEADLINE_MS="$DEADLINE"
export FLAGD_RPC_CACHE="${FLAGD_RPC_CACHE:-disabled}"
export FLIPT_ENVIRONMENT="${FLIPT_ENVIRONMENT:-default}"
export FLIPT_NAMESPACE="${FLIPT_NAMESPACE:-default}"
export RUN_MICRO MICRO_FLAG_COUNTS MICRO_ROUNDS RUN_STRESS
export STRESS_CONCURRENCY STRESS_DURATION STRESS_REPS

APP_PID=""
TEMP_DIR=""
cleanup_app() {
  if [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
  fi
  APP_PID=""
}
cleanup_all() {
  cleanup_app
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf -- "$TEMP_DIR"
  fi
}
trap cleanup_all EXIT

cd "$ROOT"
"$PYTHON_BIN" tools/generate_flags.py --count "$CATALOG_SIZE" --check > /dev/null

for count in "${FLAG_COUNT_LIST[@]}"; do
  if [[ ! "$count" =~ ^[0-9]+$ ]]; then
    echo "Invalid flag count '$count': must be a single integer" >&2
    exit 1
  fi
  if (( count < 1 || count > CATALOG_SIZE )); then
    echo "Invalid flag count $count for catalog size $CATALOG_SIZE" >&2
    exit 1
  fi
done

"$PYTHON_BIN" - "$RUN_MICRO" "$OUT" "$REPORT_OUT" "$VALIDATION_OUT" \
  "$METADATA_OUT" "$MICRO_OUT" <<'PYOUTPUTS'
import sys
from pathlib import Path

run_micro = sys.argv[1] == "1"
paths = [Path(value).expanduser().resolve() for value in sys.argv[2:6]]
if run_micro:
    paths.append(Path(sys.argv[6]).expanduser().resolve())
if len(paths) != len(set(paths)):
    raise SystemExit("result output paths must be distinct")
existing = [str(path) for path in paths if path.exists()]
if existing:
    raise SystemExit("refusing to overwrite existing result files: " + ", ".join(existing))
for path in paths:
    path.parent.mkdir(parents=True, exist_ok=True)
PYOUTPUTS

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/openfeature-benchmark.XXXXXX")"
"$PYTHON_BIN" -m bench.capture_metadata --output "$METADATA_OUT" > /dev/null
: > "$OUT"

start_app() {
  local backend="$1"
  local strategy="$2"
  local flag_count="$3"
  local log_file="$TEMP_DIR/app.log"

  cleanup_app
  env \
    FLAG_BACKEND="$backend" \
    FLAG_STRATEGY="$strategy" \
    FLAGS_PER_REQUEST="$flag_count" \
    FLAG_MANIFEST_PATH="$ROOT/flags/manifest.json" \
    FLAGD_FLAG_FILE="${FLAGD_FLAG_FILE:-$ROOT/flagd/flags.json}" \
    FLIPT_URL="${FLIPT_URL:-http://localhost:8080}" \
    FLIPT_ENVIRONMENT="${FLIPT_ENVIRONMENT:-default}" \
    FLIPT_NAMESPACE="${FLIPT_NAMESPACE:-default}" \
    FLAGD_HOST="${FLAGD_HOST:-localhost}" \
    FLAGD_PORT="${FLAGD_PORT:-8013}" \
    FLAGD_RPC_CACHE="${FLAGD_RPC_CACHE:-disabled}" \
    FLAG_POOL_SIZE="$POOL" \
    FLAG_DEADLINE_MS="$DEADLINE" \
    "$PYTHON_BIN" -m uvicorn app.main:app \
      --host 127.0.0.1 --port "$PORT" --log-level warning \
      > "$log_file" 2>&1 &
  APP_PID=$!

  for _ in $(seq 1 80); do
    if ! kill -0 "$APP_PID" 2>/dev/null; then
      break
    fi
    if "$PYTHON_BIN" - "$PORT" <<'PYHEALTH' > /dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(
    f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=2
) as response:
    if response.status != 200:
        raise SystemExit(1)
PYHEALTH
    then
      if "$PYTHON_BIN" -m bench.preflight \
        --url "http://127.0.0.1:$PORT" \
        --manifest "$ROOT/flags/manifest.json" \
        --samples 5 \
        --expected-label "$backend+$strategy" \
        --expected-count "$flag_count" \
        --expected-pid "$APP_PID" > "$TEMP_DIR/preflight.json"; then
        if kill -0 "$APP_PID" 2>/dev/null; then
          return 0
        fi
        break
      fi
      echo "Semantic preflight failed for $backend/$strategy with $flag_count flags" >&2
      cat "$TEMP_DIR/preflight.json" >&2 || true
      return 1
    fi
    sleep 0.25
  done

  echo "App failed to start for $backend/$strategy with $flag_count flags" >&2
  tail -50 "$log_file" >&2 || true
  return 1
}

run_measurement() {
  local label="$1"
  local measurement_duration="$2"
  shift 2

  set +e
  "$PYTHON_BIN" -m bench.bench \
    --url "http://127.0.0.1:$PORT" \
    --manifest "$ROOT/flags/manifest.json" \
    --label "$label" \
    --users "$VIRTUAL_USERS" \
    --duration "$measurement_duration" \
    --warmup "$WARMUP" \
    --max-connections "$MAX_CONNECTIONS" \
    --json-out "$OUT" \
    "$@" > "$TEMP_DIR/measurement.json"
  local status=$?
  set -e

  if [[ $status -ne 0 && $status -ne 2 ]]; then
    cat "$TEMP_DIR/measurement.json" >&2 || true
    return "$status"
  fi
  if [[ $status -eq 2 ]]; then
    echo "  recorded failed measurement for $label" >&2
  fi
}

if [[ "$RUN_MICRO" == "1" ]]; then
  "$PYTHON_BIN" -m bench.microbench \
    --flipt-url "${FLIPT_URL:-http://localhost:8080}" \
    --flipt-environment "${FLIPT_ENVIRONMENT:-default}" \
    --flipt-namespace "${FLIPT_NAMESPACE:-default}" \
    --flagd-host "${FLAGD_HOST:-localhost}" \
    --flagd-port "${FLAGD_PORT:-8013}" \
    --deadline-ms "$DEADLINE" \
    --manifest "$ROOT/flags/manifest.json" \
    --flag-file "$ROOT/flagd/flags.json" \
    --flag-counts "$MICRO_FLAG_COUNTS" \
    --rounds "$MICRO_ROUNDS" \
    --json-out "$MICRO_OUT"
fi

for rep in $(seq 1 "$REPS"); do
  config_offset=$(( ((rep - 1) * 2) % ${#CONFIGS[@]} ))
  for rate in "${RATE_LIST[@]}"; do
    for flag_count in "${FLAG_COUNT_LIST[@]}"; do
      for config_index in "${!CONFIGS[@]}"; do
        rotated_index=$(( (config_index + config_offset) % ${#CONFIGS[@]} ))
        label="${CONFIGS[$rotated_index]}"
        backend="${label%%+*}"
        strategy="${label#*+}"
        echo "open-loop rep=$rep rate=$rate flags=$flag_count config=$label"
        start_app "$backend" "$strategy" "$flag_count"
        run_measurement "$label" "$DURATION" --rate "$rate"
        sleep "$SETTLE_SECONDS"
      done
    done
  done
done

if [[ "$RUN_STRESS" == "1" ]]; then
  for rep in $(seq 1 "$STRESS_REPS"); do
    config_offset=$(( ((rep - 1) * 2 + 1) % ${#CONFIGS[@]} ))
    for flag_count in "${FLAG_COUNT_LIST[@]}"; do
      for config_index in "${!CONFIGS[@]}"; do
        rotated_index=$(( (config_index + config_offset) % ${#CONFIGS[@]} ))
        label="${CONFIGS[$rotated_index]}"
        backend="${label%%+*}"
        strategy="${label#*+}"
        echo "closed-loop rep=$rep concurrency=$STRESS_CONCURRENCY flags=$flag_count config=$label"
        start_app "$backend" "$strategy" "$flag_count"
        run_measurement "$label" "$STRESS_DURATION" \
          --concurrency "$STRESS_CONCURRENCY"
        sleep "$SETTLE_SECONDS"
      done
    done
  done
fi

cleanup_app
"$PYTHON_BIN" -m bench.report "$OUT" > "$REPORT_OUT"

validation_args=(
  "$OUT"
  --expected-configs "$CONFIGS_RAW"
  --expected-rates "$RATES_RAW"
  --expected-flag-counts "$FLAG_COUNTS_RAW"
  --expected-open-reps "$REPS"
  --write "$VALIDATION_OUT"
)
if [[ "$RUN_STRESS" == "1" ]]; then
  validation_args+=(--expected-stress-reps "$STRESS_REPS")
fi

set +e
"$PYTHON_BIN" -m bench.validate_results "${validation_args[@]}"
validation_status=$?
set -e

echo "Results: $OUT"
echo "Report: $REPORT_OUT"
echo "Metadata: $METADATA_OUT"
echo "Validation: $VALIDATION_OUT"
if [[ "$RUN_MICRO" == "1" ]]; then
  echo "Microbenchmark: $MICRO_OUT"
fi
exit "$validation_status"
