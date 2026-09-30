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

## Credentials (pick one channel — see "Credential delivery" below)

Either a `DRIVE_CREDS_JSON` Codespaces secret, or the automated Drive
handoff (`.drive-creds.json` inside the codespace). Google OAuth credentials
JSON with `client_id`, `client_secret`, `refresh_token` (scope
`drive.file`) for the THSC folder.

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

## Credential delivery (Codespaces)

The pipeline needs Google OAuth credentials for the THSC folder. They are
**never committed to this repository**. Two supported channels, in priority
order:

1. **Codespaces secret (recommended, zero exposure)** — add a secret named
   `DRIVE_CREDS_JSON` (GitHub → Settings → Codespaces → Secrets, scope it to
   this repo) containing the credentials JSON (`client_id`, `client_secret`,
   `refresh_token`). The pipeline reads it from the environment first.
2. **Short-lived Drive handoff (automated initial provisioning)** — an
   operator runs `pipeline/make_handoff.py --creds <file>` locally, which
   uploads the credentials to Drive as a file readable only via its
   unguessable link and writes the URL into `pipeline/creds_url.txt`. When a
   codespace starts, `.devcontainer/fetch-drive-creds.sh` downloads the file
   into the gitignored `.drive-creds.json` and **immediately deletes the
   Drive file** (burn after reading), so the link only works for a few
   minutes. For future runs, either re-create a handoff or (better) add the
   proper Codespaces secret from option 1.

## Manual use (inside the codespace)

```bash
# run everything
python3 pipeline/run_ocr_pipeline.py

# first two books only
python3 pipeline/run_ocr_pipeline.py --limit 2

# verify wiring without touching Drive
python3 pipeline/run_ocr_pipeline.py --dry-run --limit 2
```
