#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CATALOG_SIZE="${FLAG_CATALOG_SIZE:-50}"
FLAGD_READY_HOST="${FLAGD_HOST:-localhost}"
FLAGD_READY_PORT="${FLAGD_PORT:-8013}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
elif command -v python3 > /dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "Python 3 is required; create .venv with the README's uv commands" >&2
  exit 1
fi

cd "$ROOT"
if ! command -v docker > /dev/null 2>&1; then
  echo "docker is required to start Flipt and flagd" >&2
  exit 1
fi
if ! docker compose version > /dev/null 2>&1; then
  echo "the Docker Compose plugin is required" >&2
  exit 1
fi

"$PYTHON_BIN" tools/generate_flags.py --count "$CATALOG_SIZE"

docker compose up -d flipt flagd
docker compose run --rm seed

if ! "$PYTHON_BIN" - "$FLAGD_READY_HOST" "$FLAGD_READY_PORT" <<'PYWAIT'
import socket
import sys
import time

host = sys.argv[1]
port = int(sys.argv[2])
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=1):
            break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit(f"flagd did not accept connections on {host}:{port}")
PYWAIT
then
  docker compose ps --all >&2 || true
  docker compose logs --no-color --tail=200 flagd >&2 || true
  exit 1
fi

echo "Prepared $CATALOG_SIZE distinct flags in Flipt and flagd."
echo "Next: uv pip install --python .venv/bin/python -r app/requirements.lock -r requirements-dev.txt"
echo "Then: ./run_experiment.sh"
