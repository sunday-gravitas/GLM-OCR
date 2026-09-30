#!/usr/bin/env bash
# Runs as postStartCommand on every codespace (re)start:
#   - bootstrap still running  → do nothing (it will launch the pipeline)
#   - environment ready        → (re)launch the pipeline (resumes from log)
#   - environment not ready    → (re)launch the bootstrap
# Double-launches are harmless: the Python pipeline takes an exclusive flock.

cd "$(dirname "$0")/.."
log() { echo "[watchdog $(date -u +%H:%M:%S)] $*"; }

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
