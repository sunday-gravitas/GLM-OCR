#!/usr/bin/env bash
# Runs as postStartCommand on every codespace (re)start:
#   0. sync the test-ocr branch + fetch Drive credentials (idempotent, fast)
#   1. bootstrap still running  → do nothing (it will launch the pipeline)
#   2. environment ready        → (re)launch the pipeline (resumes from log)
#   3. environment not ready    → (re)launch the bootstrap
# Double-launches are harmless: the Python pipeline takes an exclusive flock.

cd "$(dirname "$0")/.."
log() { echo "[watchdog $(date -u +%H:%M:%S)] $*"; }

# 0a. keep the pipeline code current (this codespace is a pipeline appliance)
if git fetch origin test-ocr --quiet 2>/dev/null; then
  if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/test-ocr)" ]; then
    log "updating to latest origin/test-ocr ($(git rev-parse --short origin/test-ocr))"
    git reset --hard origin/test-ocr --quiet
  fi
fi

# 0b. credentials (no-op when the DRIVE_CREDS_JSON secret is set or
#     .drive-creds.json already exists)
if ! bash .devcontainer/fetch-drive-creds.sh; then
  log "WARNING: no Drive credentials available yet"
fi

if pgrep -f "pipeline-bootstrap.sh" >/dev/null 2>&1; then
  log "bootstrap already running — nothing to do"
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

if [ "$READY" = "1" ]; then
  if pgrep -f "run_ocr_pipeline.py" >/dev/null 2>&1; then
    log "pipeline already running"
  else
    log "environment ready — launching pipeline"
    nohup python3 pipeline/run_ocr_pipeline.py >> /tmp/pipeline.log 2>&1 &
  fi
else
  log "environment not ready — launching bootstrap"
  nohup bash .devcontainer/pipeline-bootstrap.sh >> /tmp/pipeline-bootstrap.log 2>&1 &
fi
