#!/usr/bin/env bash
# GLM-OCR pipeline bootstrap — runs inside the Codespace (as postCreateCommand,
# in the background). Idempotent: safe to re-run after an interrupted install.
#
# What it does:
#   1. Installs the GLM-OCR SDK from this repository
#        - ZHIPU_API_KEY set      → light install (cloud/MaaS mode, no GPU)
#        - otherwise              → self-hosted install (CPU torch + layout model)
#   2. Self-hosted only: installs Ollama, pulls the glm-ocr model and creates
#      a 16k-context variant (the SDK does not raise Ollama's default num_ctx,
#      which would truncate long OCR outputs).
#   3. Launches pipeline/run_ocr_pipeline.py in the background.
#
# Logs: /tmp/pipeline-bootstrap.log, /tmp/ollama.log, /tmp/pipeline.log

set -euo pipefail
cd "$(dirname "$0")/.."

log() { echo "[bootstrap $(date -u +%H:%M:%S)] $*"; }

log "starting bootstrap in $(pwd)"

# --------------------------------------------------------------------------
# 0. Google Drive credentials (short-lived public handoff, burned after read;
#    skipped when the DRIVE_CREDS_JSON Codespaces secret is set instead)
# --------------------------------------------------------------------------
if ! bash .devcontainer/fetch-drive-creds.sh; then
  log "WARNING: no Drive credentials yet — pipeline will report FATAL until they exist"
fi

# --------------------------------------------------------------------------
# 1. Python environment + GLM-OCR SDK (from this repo)
# --------------------------------------------------------------------------
export PIP_DISABLE_PIP_VERSION_CHECK=1

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
python3 -c "import glmocr; log 'glmocr import OK'"

# --------------------------------------------------------------------------
# 2. Ollama + GLM-OCR model (self-hosted mode only)
# --------------------------------------------------------------------------
if [ -z "${ZHIPU_API_KEY:-}" ]; then
  if ! command -v ollama >/dev/null 2>&1; then
    log "installing Ollama…"
    curl -fsSL https://ollama.ai/install.sh | sh || {
      log "FATAL: Ollama install failed"; exit 1; }
  fi

  if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
    log "starting ollama serve…"
    OLLAMA_KEEP_ALIVE=-1 nohup ollama serve > /tmp/ollama.log 2>&1 &
  fi

  for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:11434/api/version >/dev/null; then break; fi
    sleep 2
  done
  curl -sf http://127.0.0.1:11434/api/version >/dev/null || {
    log "FATAL: ollama serve did not come up (see /tmp/ollama.log)"; exit 1; }
  log "ollama is up: $(curl -sf http://127.0.0.1:11434/api/version)"

  if ! ollama list 2>/dev/null | grep -q "^glm-ocr "; then
    log "pulling glm-ocr model (~2 GB, first run only)…"
    ollama pull glm-ocr:latest
  fi

  # 16k-context variant — the SDK cannot raise num_ctx itself, and the model
  # default would silently truncate long region OCR outputs.
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
fi

# --------------------------------------------------------------------------
# 3. Launch the pipeline (flock-protected against double starts)
# --------------------------------------------------------------------------
if pgrep -f "run_ocr_pipeline.py" >/dev/null 2>&1; then
  log "pipeline already running — not starting another"
else
  log "launching pipeline…"
  mkdir -p /tmp/ocr-work
  nohup python3 pipeline/run_ocr_pipeline.py >> /tmp/pipeline.log 2>&1 &
  log "pipeline launched (log: /tmp/pipeline.log)"
fi

log "bootstrap complete"
