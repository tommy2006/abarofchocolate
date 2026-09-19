#!/usr/bin/env bash
# Trustworthy Process Monitor - one-command launcher for macOS / Linux.
#   ./run.sh                 install (first time) + start the UI on http://127.0.0.1:8000
#   ./run.sh --demo          also generate sample data and run the pipeline on it first
#   ./run.sh --pull-models   pull the configured Ollama model if Ollama is installed
#   ./run.sh --port 8080     another port
#   ./run.sh --no-open       do not open the browser
#   ./run.sh --doctor        only run the environment check
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DEMO=0; PULL=0; OPEN=1; DOCTOR=0; PORT=8000; HOST=127.0.0.1
while [ $# -gt 0 ]; do
  case "$1" in
    --demo) DEMO=1 ;;
    --pull-models) PULL=1 ;;
    --no-open) OPEN=0 ;;
    --doctor) DOCTOR=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
  shift
done

banner() { echo; echo "================================================================"; echo " $1"; echo "================================================================"; }
banner "Trustworthy Process Monitor (TPM) - launcher"

# 1) Python 3.10+
find_python() {
  for c in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then echo "$c"; return 0; fi
    fi
  done
  return 1
}
VENV_PY="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  PY="$(find_python || true)"
  if [ -z "$PY" ]; then
    echo "Python 3.10+ was not found. Install it (https://www.python.org/downloads/ or your package manager) and run again." >&2
    exit 1
  fi
  echo "Creating virtual environment with $PY"
  "$PY" -m venv "$ROOT/.venv"
fi

# 2) dependencies
STAMP="$ROOT/.venv/.requirements.sha"
if command -v shasum >/dev/null 2>&1; then REQ_HASH="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"; else REQ_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"; fi
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$REQ_HASH" ]; then
  echo "Installing dependencies (a few minutes the first time)..."
  "$VENV_PY" -m pip install --upgrade pip --quiet --disable-pip-version-check
  "$VENV_PY" -m pip install -r requirements.txt --quiet --disable-pip-version-check --progress-bar on
  echo "$REQ_HASH" > "$STAMP"
  echo "Dependencies installed."
else
  echo "Dependencies up to date."
fi

# 3) .env
if [ ! -f "$ROOT/.env" ]; then cp "$ROOT/.env.example" "$ROOT/.env"; echo "Created .env from .env.example (defaults: no-egress profile, no API key needed)."; fi

# 4) Ollama (optional)
MODEL="$(grep -E '^\s*model:\s*' config/settings.yaml | head -n1 | sed -E 's/^\s*model:\s*([^ #]+).*/\1/')"
MODEL="${MODEL:-gemma4:e4b-it-qat}"
OLLAMA_OK=0
if command -v ollama >/dev/null 2>&1; then OLLAMA_OK=1; fi
if [ "$OLLAMA_OK" = "0" ] && command -v curl >/dev/null 2>&1; then
  if curl -s -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then OLLAMA_OK=1; fi
fi
if [ "$OLLAMA_OK" = "1" ]; then
  if curl -s -m 3 http://localhost:11434/api/tags 2>/dev/null | grep -q "$MODEL"; then
    echo "Ollama: local model $MODEL is available."
  else
    echo "Ollama is installed but the local model is not pulled. To enable model-written explanations run:"
    echo "    ollama pull $MODEL"
    if [ "$PULL" = "1" ]; then echo "Pulling $MODEL (about 6 GB)..."; ollama pull "$MODEL" || true; fi
  fi
else
  echo "Ollama not found: the app runs fully in template mode (no model-written text). Optional: install https://ollama.com/download; models can then be chosen and downloaded in the app (top bar > Local model)."
fi

# 5) doctor / demo / serve
if [ "$DOCTOR" = "1" ]; then exec "$VENV_PY" -m tpm doctor; fi
if [ "$DEMO" = "1" ]; then
  banner "Running the demo pipeline on samples/demo_process.csv"
  "$VENV_PY" -m tpm demo || true
fi
URL="http://$HOST:$PORT"
banner "Starting the UI at $URL   (workspace: $ROOT/workspace)   Ctrl+C stops it"
ARGS=(-m tpm serve --host "$HOST" --port "$PORT")
if [ "$OPEN" = "1" ]; then ARGS+=(--open); fi
exec "$VENV_PY" "${ARGS[@]}"
