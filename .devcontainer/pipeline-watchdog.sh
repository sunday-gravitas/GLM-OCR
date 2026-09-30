#!/usr/bin/env bash
# Runs as postStartCommand on every codespace (re)start:
#   0. push a "watchdog-started" marker (with log tails) to Drive
#   1. sync the test-ocr branch
#   2. fetch Drive credentials (idempotent, fast)
#   3. bootstrap still running  → do nothing (it will launch the pipeline)
#      environment ready        → (re)launch the pipeline (resumes from log)
#      environment not ready    → (re)launch the bootstrap
# Double-launches are harmless: the Python pipeline takes an exclusive flock.
# Every decision is reported to Drive as bootstrap_status.json.

cd "$(dirname "$0")/.."
log() { echo "[watchdog $(date -u +%H:%M:%S)] $*"; }
mark() { local s="$1"; shift; python3 pipeline/status_marker.py --status "$s" "$@" \
           >/dev/null 2>&1 || true; }

mark "watchdog-started" --note "postStartCommand is alive"
log "watchdog started"

# 1. keep the pipeline code current (this codespace is a pipeline appliance)
if git fetch origin test-ocr --quiet 2>/dev/null; then
  if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/test-ocr)" ]; then
    log "updating to latest origin/test-ocr ($(git rev-parse --short origin/test-ocr))"
    git reset --hard origin/test-ocr --quiet
  fi
else
  log "WARNING: git fetch failed — running existing code"
fi
mark "code-synced" --note "HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo '?')"

# 2. credentials (no-op when the DRIVE_CREDS_JSON secret is set or
#    .drive-creds.json already exists)
if ! bash .devcontainer/fetch-drive-creds.sh; then
  log "WARNING: no Drive credentials available yet"
fi
CREDS_OK=0
[ -s .drive-creds.json ] && CREDS_OK=1
[ -n "${DRIVE_CREDS_JSON:-}" ] && CREDS_OK=1
mark "credentials-checked" --note "creds present: $CREDS_OK"

if pgrep -f "pipeline-bootstrap.sh" >/dev/null 2>&1; then
  log "bootstrap already running — nothing to do"
  mark "watchdog-done" --note "bootstrap already running"
  exit 0
fi

if [ -n "${ZHIPU_API_KEY:-}" ]; then
  READY=1
  python3 -c "import glmocr" >/dev/null 2>&1 || READY=0
else
  READY=1
  python3 -c "import glmocr" >/dev/null 2>&1 || READY=0
  command -v ollama >/dev/null 2>&1 || READY=0
  curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 || READY=0
  [ -f /tmp/ollama_model.txt ] || READY=0
fi
mark "ready-check" --note "READY=$READY"

if [ "$READY" = "1" ]; then
  # Best effort: publish the /status monitoring port so the pipeline's
  # self-ping keep-alive counts as codespace activity (see bootstrap).
  if [ -n "${CODESPACE_NAME:-}" ] && command -v gh >/dev/null 2>&1; then
    gh codespace ports visibility 8787:public -c "$CODESPACE_NAME" >/dev/null 2>&1 || true
  fi
  if pgrep -f "run_ocr_pipeline.py" >/dev/null 2>&1; then
    log "pipeline already running"
    mark "watchdog-done" --note "pipeline already running" --logs
  else
    log "environment ready — launching pipeline"
    mark "pipeline-launching" --note "environment ready"
    setsid nohup python3 pipeline/run_ocr_pipeline.py >> /tmp/pipeline.log 2>&1 < /dev/null &
    disown || true
  fi
else
  log "environment not ready — launching bootstrap"
  mark "bootstrap-launching" --note "environment not ready"
  setsid nohup bash .devcontainer/pipeline-bootstrap.sh >> /tmp/pipeline-bootstrap.log 2>&1 < /dev/null &
  disown || true
fi

# verify background processes survived the lifecycle runner cleanup
sleep 60
if pgrep -f "pipeline-bootstrap.sh" >/dev/null 2>&1 || pgrep -f "run_ocr_pipeline.py" >/dev/null 2>&1; then
  mark "watchdog-verified" --note "background processes alive 60s after launch" --logs
else
  mark "watchdog-failed" --note "background processes DIED within 60s" --logs
fi
