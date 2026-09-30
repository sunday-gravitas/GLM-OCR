#!/usr/bin/env bash
# pipeline-supervisor.sh — keeps the OCR pipeline + status server running.
# Launched (setsid --fork) by the watchdog or the bootstrap. The Python
# pipeline holds its own flock, so overlapping instances exit instantly;
# this supervisor simply restarts whatever is not running (crash, circuit
# breaker on network outage, or codespace restart leftovers).

cd "$(dirname "$0")/.."
exec 9>/tmp/pipeline-supervisor.lock
flock -n 9 || exit 0   # single supervisor instance

log() { echo "[supervisor $(date -u +%H:%M:%S)] $*" >> /tmp/pipeline.log; }
mark() { python3 pipeline/status_marker.py --status "supervisor" \
           --note "$1" --logs --state >/dev/null 2>&1 || true; }
log "supervisor up (pid $$)"
LAST_MARK=0

while true; do
  NOW=$(date +%s)
  if [ $((NOW - LAST_MARK)) -ge 120 ]; then
    MARK_NOTE="alive; pipeline=$(pgrep -f '[r]un_ocr_pipeline.py' >/dev/null 2>&1 && echo running || echo stopped)"
    mark "$MARK_NOTE"
    LAST_MARK=$NOW
  fi
  # standalone status server (separate process so the pipeline itself never
  # binds a port — binding coincided with losing outbound connectivity)
  if ! pgrep -f "[s]tatus_server.py" >/dev/null 2>&1; then
    log "status server not running — starting"
    setsid --fork bash -c \
      "exec python3 .devcontainer/status_server.py >> /tmp/status_server.log 2>&1" \
      < /dev/null >/dev/null 2>&1 || true
  fi

  if pgrep -f "[r]un_ocr_pipeline.py" >/dev/null 2>&1; then
    sleep 30
    continue
  fi

  # keep the code current: apply branch updates between pipeline runs
  if [ "${SUPERVISOR_REEXEC:-}" != "1" ]; then
    if git fetch origin test-ocr --quiet 2>/dev/null; then
      if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/test-ocr)" ]; then
        log "updating to origin/test-ocr ($(git rev-parse --short origin/test-ocr)) — re-exec"
        git reset --hard origin/test-ocr --quiet
        export SUPERVISOR_REEXEC=1
        exec bash "$0"
      fi
    fi
  fi

  # engine repair: when the pipeline flags a broken engine (layout
  # self-test empty — typically torch/torchvision ABI mismatch from
  # interrupted installs), reinstall the vision stack cleanly
  if [ -f /tmp/.engine_broken ]; then
    log "engine flagged broken — reinstalling vision stack"
    mark "engine-repair" --note "reinstalling torch/torchvision/opencv"
    pip install --quiet --force-reinstall --no-deps \
      --index-url https://download.pytorch.org/whl/cpu torch torchvision \
      >> /tmp/pipeline.log 2>&1 || \
      pip install --quiet --force-reinstall --no-deps torch torchvision \
      >> /tmp/pipeline.log 2>&1 || true
    pip install --quiet --force-reinstall opencv-python-headless \
      >> /tmp/pipeline.log 2>&1 || true
    rm -f /tmp/.engine_broken /tmp/.layout_cache_cleared
    log "vision stack reinstalled"
  fi

  log "pipeline not running — starting"
  GLMOCR_LOG_LEVEL=${GLMOCR_LOG_LEVEL:-DEBUG} \
    python3 pipeline/run_ocr_pipeline.py >> /tmp/pipeline.log 2>&1
  CODE=$?
  log "pipeline exited (code $CODE) — restart in 60s"
  mark "pipeline-exit code=$CODE"
  sleep 60
done
