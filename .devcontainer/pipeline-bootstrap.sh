#!/usr/bin/env bash
# GLM-OCR pipeline bootstrap — runs inside the Codespace (as postCreateCommand,
# in the background). Idempotent: safe to re-run after an interrupted install.
#
# What it does:
#   0. Fetches Drive credentials (short-lived public handoff, burned after read)
#   1. Installs the GLM-OCR SDK from this repository
#        - ZHIPU_API_KEY set      → light install (cloud/MaaS mode, no GPU)
#        - otherwise              → self-hosted install (CPU torch + layout model)
#   2. Self-hosted only: installs Ollama, pulls the glm-ocr model and creates
#      a 16k-context variant (the SDK does not raise Ollama's default num_ctx,
#      which would truncate long OCR outputs).
#   3. Publishes the /status monitoring port (best effort) and launches the
#      pipeline in the background.
#
# Progress is reported to Drive as bootstrap_status.json (via status_marker.py)
# so provisioning can be watched without access to the codespace.
# Logs: /tmp/pipeline-bootstrap.log, /tmp/ollama.log, /tmp/pipeline.log

set -euo pipefail
cd "$(dirname "$0")/.."

log() { echo "[bootstrap $(date -u +%H:%M:%S)] $*"; }
mark() { python3 pipeline/status_marker.py --status "$1" ${2:+--note "$2"} \
           >/dev/null 2>&1 || true; }
trap 'mark "bootstrap-failed" "last stage: $STAGE"' ERR

log "starting bootstrap in $(pwd)"

# --------------------------------------------------------------------------
# 0. Google Drive credentials (short-lived public handoff, burned after read;
#    skipped when the DRIVE_CREDS_JSON Codespaces secret is set instead)
# --------------------------------------------------------------------------
STAGE="credentials"
if ! bash .devcontainer/fetch-drive-creds.sh; then
  log "WARNING: no Drive credentials yet — pipeline will report FATAL until they exist"
fi
mark "credentials-ready"

# --------------------------------------------------------------------------
# 1. Python environment + GLM-OCR SDK (from this repo)
# --------------------------------------------------------------------------
export PIP_DISABLE_PIP_VERSION_CHECK=1
STAGE="python-deps"

if python3 -c "import glmocr" >/dev/null 2>&1; then
  log "glmocr already installed — skipping"
else
  if [ -n "${ZHIPU_API_KEY:-}" ]; then
    log "ZHIPU_API_KEY detected → cloud (MaaS) mode, light install"
    pip install --quiet -e .
  else
    log "no ZHIPU_API_KEY → self-hosted Ollama mode, CPU install"
    # CPU-only torch wheels first (avoids the multi-GB CUDA wheels)
    pip install --quiet --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision \
      || pip install --quiet torch torchvision
    pip install --quiet -e ".[selfhosted]"
  fi
fi
python3 -c "import glmocr" 2>/dev/null || { log "FATAL: glmocr import failed"; exit 1; }
mark "python-deps-done"

# --------------------------------------------------------------------------
# 2. Ollama + GLM-OCR model (self-hosted mode only)
# --------------------------------------------------------------------------
if [ -z "${ZHIPU_API_KEY:-}" ]; then
  STAGE="ollama-install"
  if ! command -v ollama >/dev/null 2>&1; then
    log "installing Ollama…"
    curl -fsSL https://ollama.ai/install.sh | sh || {
      log "FATAL: Ollama install failed"; exit 1; }
  fi

  if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
    log "starting ollama serve…"
    OLLAMA_KEEP_ALIVE=-1 nohup ollama serve > /tmp/ollama.log 2>&1 &
  fi

  STAGE="ollama-wait"
  for i in $(seq 1 90); do
    if curl -sf http://127.0.0.1:11434/api/version >/dev/null; then break; fi
    sleep 2
  done
  curl -sf http://127.0.0.1:11434/api/version >/dev/null || {
    log "FATAL: ollama serve did not come up (see /tmp/ollama.log)"; exit 1; }
  log "ollama is up: $(curl -sf http://127.0.0.1:11434/api/version)"
  mark "ollama-up"

  STAGE="model-pull"
  if ! ollama list 2>/dev/null | grep -q "^glm-ocr "; then
    log "pulling glm-ocr model (~2 GB, first run only)…"
    mark "model-pull-started"
    # retry loop: ollama resumes partial pulls, so re-attempts are cheap
    for attempt in 1 2 3; do
      if timeout 2400 ollama pull glm-ocr:latest; then break; fi
      log "pull attempt $attempt failed/timed out — retrying…"
      [ "$attempt" = "3" ] && { log "FATAL: model pull failed 3×"; exit 1; }
    done
  fi
  mark "model-pulled"

  # 16k-context variant — the SDK cannot raise num_ctx itself, and the model
  # default would silently truncate long region OCR outputs.
  STAGE="model-variant"
  MODEL="glm-ocr:latest"
  if ! ollama list 2>/dev/null | grep -q "^glm-ocr-16k "; then
    printf 'FROM glm-ocr:latest\nPARAMETER num_ctx 16384\n' \
      > /tmp/Modelfile.glmocr
    if ollama create glm-ocr-16k -f /tmp/Modelfile.glmocr >/dev/null 2>&1; then
      MODEL="glm-ocr-16k"
      log "created glm-ocr-16k (num_ctx=16384)"
    else
      log "WARNING: could not create 16k variant, using glm-ocr:latest"
    fi
  else
    MODEL="glm-ocr-16k"
  fi
  echo "$MODEL" > /tmp/ollama_model.txt
  log "effective ollama model: $MODEL"
  mark "engine-ready" "model: $MODEL"
fi

# --------------------------------------------------------------------------
# 3. Launch the pipeline (flock-protected against double starts)
# --------------------------------------------------------------------------
# Best effort: make the status port public so the pipeline's self-ping
# keep-alive counts as activity (prevents the 30-min idle auto-stop).
# Uses the codespace's own authenticated gh CLI; failure is harmless.
if [ -n "${CODESPACE_NAME:-}" ] && command -v gh >/dev/null 2>&1; then
  gh codespace ports visibility 8787:public -c "$CODESPACE_NAME" >/dev/null 2>&1 \
    && log "status port 8787 made public (keep-alive self-ping effective)" \
    || log "could not publish port 8787 (keep-alive may rely on user interaction)"
fi

STAGE="pipeline-launch"
if pgrep -f "run_ocr_pipeline.py" >/dev/null 2>&1; then
  log "pipeline already running — not starting another"
else
  log "launching pipeline…"
  mkdir -p /tmp/ocr-work
  setsid --fork bash -c "exec python3 pipeline/run_ocr_pipeline.py >> /tmp/pipeline.log 2>&1" < /dev/null
  log "pipeline launched (log: /tmp/pipeline.log)"
fi
mark "pipeline-launched"

log "bootstrap complete"
