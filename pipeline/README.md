# GLM-OCR THSC Physics Pipeline

OCR the physics "books" (PDFs) in the THSC Google Drive folder with the
GLM-OCR engine — **one book at a time** — and upload the OCRed results back
to Drive. Source PDFs are **never stored in this repository**: each book is
downloaded to a temp dir (`/tmp/ocr-work`), processed, uploaded, and deleted.

## What runs where

```
THSC/level11/physics (Google Drive)          ← source PDFs (68 books)
        │  download one at a time
        ▼
   Codespace (this repo, branch test-ocr)
        │  GLM-OCR engine
        │    • ZHIPU_API_KEY set → cloud (MaaS) mode
        │    • otherwise         → self-hosted Ollama (glm-ocr, CPU)
        ▼
THSC/level11/physics/ocr-book (Google Drive) ← <book>.md + <book>.json
THSC/level11/physics/ocr-book/ocr_log.json   ← live monitoring log
```

The pipeline starts automatically when the Codespace is created
(`.devcontainer/devcontainer.json` → `pipeline-bootstrap.sh`) and resumes
after any restart (`pipeline-watchdog.sh`). Books already marked `done` in
the log are skipped, so the run is fully resumable.

## Files

| File | Purpose |
|------|---------|
| `pipeline/run_ocr_pipeline.py` | Main orchestrator (download → OCR → upload → log) |
| `pipeline/drive_client.py` | Minimal Google Drive v3 client (refresh-token OAuth) |
| `.devcontainer/pipeline-bootstrap.sh` | One-time setup + auto-start of the pipeline |
| `.devcontainer/pipeline-watchdog.sh` | Resume/restart logic for codespace restarts |

## Required Codespace secret

| Secret | Value |
|--------|-------|
| `DRIVE_CREDS_JSON` | Google OAuth credentials JSON with `client_id`, `client_secret`, `refresh_token` (scope `drive.file`) for the THSC folder |

Optional secrets / env:

| Variable | Effect |
|----------|--------|
| `ZHIPU_API_KEY` | Switch the engine to GLM-OCR cloud (MaaS) mode — much faster, no local model |
| `THSC_FOLDER_ID` | Override the THSC root folder id (default `1uxF1H3BUqzrbgjEj37W-SNy9780xkq1q`) |

## Monitoring

* **`ocr_log.json`** in the `ocr-book` Drive folder — updated after every
  book: status per book (`pending` / `processing` / `done` / `failed`),
  page counts, durations, output file ids, and a summary block.
* Inside the codespace: `tail -f /tmp/pipeline.log`, or open the forwarded
  port `8787` → `/status` (the pipeline also self-pings this port every
  10 min so the codespace is not auto-stopped while working).

## Manual use (inside the codespace)

```bash
# run everything
python3 pipeline/run_ocr_pipeline.py

# first two books only
python3 pipeline/run_ocr_pipeline.py --limit 2

# verify wiring without touching Drive
python3 pipeline/run_ocr_pipeline.py --dry-run --limit 2
```
