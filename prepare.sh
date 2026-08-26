#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CATALOG_SIZE="${FLAG_CATALOG_SIZE:-50}"

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

"$PYTHON_BIN" - <<'PYWAIT'
import socket
import time

deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 8013), timeout=1):
            break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit("flagd did not accept connections on 127.0.0.1:8013")
PYWAIT

echo "Prepared $CATALOG_SIZE distinct flags in Flipt and flagd."
echo "Next: uv pip install --python .venv/bin/python -r requirements-dev.txt"
echo "Then: ./run_experiment.sh"
