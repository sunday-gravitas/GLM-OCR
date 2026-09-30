#!/usr/bin/env bash
# pipeline-supervisor.sh — keeps the OCR pipeline running forever.
# Launched (setsid --fork) by the watchdog or the bootstrap. The Python
# pipeline holds its own flock, so overlapping instances exit instantly;
# this supervisor simply restarts it whenever it is not running (crash,
# circuit breaker on network outage, or codespace restart leftovers).

cd "$(dirname "$0")/.."
exec 9>/tmp/pipeline-supervisor.lock
flock -n 9 || exit 0   # single supervisor instance

log() { echo "[supervisor $(date -u +%H:%M:%S)] $*" >> /tmp/pipeline.log; }
log "supervisor up (pid $$)"

while true; do
  if pgrep -f "[r]un_ocr_pipeline.py" >/dev/null 2>&1; then
    sleep 30
    continue
  fi
  log "pipeline not running — starting"
  python3 pipeline/run_ocr_pipeline.py >> /tmp/pipeline.log 2>&1
  log "pipeline exited (code $?) — restart in 60s"
  sleep 60
done
