#!/usr/bin/env python3
"""GLM-OCR book pipeline — THSC level11/physics → OCR → Google Drive.

What it does
------------
1. Connects to the THSC Google Drive folder and navigates to
   ``THSC/level11/physics`` (all lookups case-insensitive).
2. Ensures the output folder ``THSC/level11/physics/ocr-book`` exists.
3. Keeps a monitoring log ``ocr_log.json`` inside that folder. The log is
   re-uploaded to Drive on every state change, so progress can be watched
   live from Google Drive.
4. Lists every PDF "book" in the physics folder and processes them ONE AT A
   TIME:
       download to a temp dir (never committed to the repo)
       → OCR with the GLM-OCR engine (this repository)
       → upload ``<book>.md`` + ``<book>.json`` to the ocr-book folder
       → update the log → delete the local temp files.
5. Books already marked ``done`` in the log are skipped, so the pipeline can
   be stopped and resumed at any time.

OCR engine modes (auto-selected)
--------------------------------
* If ``ZHIPU_API_KEY`` is set  → GLM-OCR cloud (MaaS) mode.
* Otherwise                    → self-hosted mode against a local Ollama
  server (see ``.devcontainer/pipeline-bootstrap.sh``, which installs
  Ollama and pulls the ``glm-ocr`` model).

Environment variables
---------------------
DRIVE_CREDS_JSON    (required) JSON string with client_id / client_secret /
                    refresh_token (Google OAuth desktop-app credentials).
THSC_FOLDER_ID      (optional) Root THSC folder id.
                    Default: 1uxF1H3BUqzrbgjEj37W-SNy9780xkq1q
ZHIPU_API_KEY       (optional) Switches the engine to cloud/MaaS mode.
OLLAMA_MODEL_FILE   (optional) File containing the effective Ollama model
                    name (written by the bootstrap). Default: glm-ocr:latest
OCR_STATUS_PORT     (optional) Local port for the /status monitor. 8787.
OCR_HEARTBEAT_SEC   (optional) Self-ping interval to keep the Codespace
                    alive while a job runs. 600.

Usage
-----
    python3 pipeline/run_ocr_pipeline.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # glmocr dependency
import yaml  # PyYAML ships with glmocr

from drive_client import DriveClient, DriveError

# --------------------------------------------------------------------- config
THSC_FOLDER_ID = os.environ.get("THSC_FOLDER_ID", "1uxF1H3BUqzrbgjEj37W-SNy9780xkq1q")
LEVEL_NAME = os.environ.get("THSC_LEVEL_NAME", "level11")
SUBJECT_NAME = os.environ.get("THSC_SUBJECT_NAME", "physics")
OCR_BOOK_DIR_NAME = os.environ.get("OCR_BOOK_DIR_NAME", "ocr-book")
LOG_FILE_NAME = os.environ.get("OCR_LOG_NAME", "ocr_log.json")

WORK_ROOT = Path(os.environ.get("OCR_WORK_ROOT", "/tmp/ocr-work"))
STATUS_PORT = int(os.environ.get("OCR_STATUS_PORT", "8787"))
HEARTBEAT_SEC = int(os.environ.get("OCR_HEARTBEAT_SEC", "600"))
LOCK_FILE = "/tmp/ocr-pipeline.instance.lock"

MaaS_MAX_FILE_BYTES = 10 * 1024 * 1024  # SDK limit for cloud mode

PIPELINE_ID = "glm-ocr-thsc-level11-physics"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_line(msg: str) -> None:
    print(f"[{utcnow()}] {msg}", flush=True)


# ------------------------------------------------------------------ credentials
def load_drive_creds() -> Dict[str, Any]:
    """Load Google OAuth credentials from env var or file (codespace secret)."""
    raw = os.environ.get("DRIVE_CREDS_JSON", "").strip()
    if not raw:
        for candidate in (
            os.environ.get("DRIVE_CREDS_FILE"),
            "/tmp/drive_creds.json",
            str(Path.home() / ".drive_creds.json"),
            "/run/secrets/drive_creds.json",
        ):
            if candidate and Path(candidate).is_file():
                raw = Path(candidate).read_text(encoding="utf-8").strip()
                break
    if not raw:
        raise SystemExit(
            "FATAL: DRIVE_CREDS_JSON is not set. Add it as a Codespaces secret "
            "(repo Settings → Secrets) or export it before running."
        )
    creds = json.loads(raw)
    for key in ("client_id", "client_secret", "refresh_token"):
        if not creds.get(key):
            raise SystemExit(f"FATAL: Drive credentials missing '{key}'.")
    return creds


# ------------------------------------------------------------------ OCR engine
def effective_ollama_model() -> str:
    src = os.environ.get("OLLAMA_MODEL_FILE", "/tmp/ollama_model.txt")
    try:
        val = Path(src).read_text(encoding="utf-8").strip()
        if val:
            return val
    except OSError:
        pass
    return os.environ.get("OLLAMA_MODEL", "glm-ocr:latest")


def write_engine_config(model_name: str, port: int = 11434) -> Path:
    """Write a YAML config for the SDK (self-hosted Ollama mode).

    Starts from the SDK's own shipped ``config.yaml`` (so every project
    default — task prompts, label mappings, formatter switches — is kept)
    and only overrides what the pipeline needs.
    """
    import glmocr

    base_path = Path(glmocr.__file__).parent / "config.yaml"
    cfg = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}

    pipeline_cfg = cfg.setdefault("pipeline", {})
    pipeline_cfg["maas"] = {"enabled": False}
    pipeline_cfg["ocr_api"] = {
        "api_host": "127.0.0.1",
        "api_port": port,
        "api_path": "/api/generate",
        "api_mode": "ollama_generate",
        "model": model_name,
        "verify_ssl": False,
        "connect_timeout": 300,
        "request_timeout": 3600,
        "retry_max_attempts": 3,
    }
    pipeline_cfg["max_workers"] = 2
    pipeline_cfg.setdefault("page_loader", {})["pdf_dpi"] = 200
    layout = pipeline_cfg.setdefault("layout", {})
    layout["model_dir"] = "PaddlePaddle/PP-DocLayoutV3_safetensors"
    layout["device"] = "cpu"
    layout["batch_size"] = 1
    cfg.setdefault("logging", {})["level"] = "INFO"

    path = Path("/tmp/glmocr_engine.yaml")
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def build_engine() -> Any:
    """Build the GLM-OCR parser. Cloud mode if ZHIPU_API_KEY, else Ollama."""
    from glmocr import GlmOcr  # installed from this repository

    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if api_key:
        parser = GlmOcr(api_key=api_key, mode="maas", timeout=1800)
        return parser, {"mode": "maas-cloud", "model": "glm-ocr"}

    model_name = effective_ollama_model()
    cfg_path = write_engine_config(model_name)
    parser = GlmOcr(
        config_path=str(cfg_path), mode="selfhosted", layout_device="cpu"
    )
    return parser, {
        "mode": "selfhosted-ollama",
        "model": model_name,
        "config": str(cfg_path),
    }


def ocr_book(parser: Any, pdf_path: Path, out_dir: Path) -> Dict[str, Any]:
    """OCR one PDF with the GLM-OCR engine; return {markdown, json, pages}."""
    result = parser.parse(str(pdf_path))
    markdown = result.markdown_result or ""
    structured = result.json_result
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except json.JSONDecodeError:
            structured = {"raw": structured}
    pages = None
    if isinstance(structured, list):
        pages = len(structured)
    elif isinstance(structured, dict) and isinstance(
        structured.get("layout_details"), list
    ):
        pages = len(structured["layout_details"])
    out_dir.mkdir(parents=True, exist_ok=True)
    return {"markdown": markdown, "json": structured, "pages": pages}


# ------------------------------------------------------- status & keep-alive
class PipelineState:
    """Thread-safe in-memory view of the run, served over HTTP for monitoring."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = {
            "pipeline": PIPELINE_ID,
            "status": "starting",
            "current_book": None,
            "started_at": utcnow(),
            "last_heartbeat": None,
            "heartbeat_url": None,
            "books_done": 0,
            "books_failed": 0,
            "books_total": 0,
            "engine": None,
            "detail": None,
        }

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            self.data.update(kwargs)
            self.data["updated_at"] = utcnow()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.data)


STATE = PipelineState()


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path in ("/", "/status", "/healthz"):
            body = json.dumps(STATE.snapshot(), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:  # silence access log
        pass


def start_status_server() -> Optional[threading.Thread]:
    try:
        server = ThreadingHTTPServer(("0.0.0.0", STATUS_PORT), StatusHandler)
    except OSError as exc:
        log_line(f"WARNING: could not bind status port {STATUS_PORT}: {exc}")
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log_line(f"status server listening on :{STATUS_PORT} (/status)")
    return thread


def start_heartbeat() -> Optional[threading.Thread]:
    """Self-ping the Codespace's forwarded port so the idle timer resets.

    Requests that reach a codespace through its forwarded public port count
    as activity, which prevents the 30-minute auto-shutdown while the OCR
    pipeline is grinding through books in the background.
    """
    name = os.environ.get("CODESPACE_NAME", "").strip()
    domain = os.environ.get(
        "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev"
    ).strip()
    if not name:
        return None  # not running inside a Codespace
    url = f"https://{name}-{STATUS_PORT}.{domain}/status"
    STATE.update(heartbeat_url=url)

    def beat() -> None:
        while True:
            try:
                resp = requests.get(url, timeout=30)
                STATE.update(last_heartbeat=f"{utcnow()} ({resp.status_code})")
            except Exception as exc:  # noqa: BLE001
                STATE.update(last_heartbeat=f"{utcnow()} (error: {exc})")
            time.sleep(HEARTBEAT_SEC)

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    log_line(f"keep-alive heartbeat every {HEARTBEAT_SEC}s → {url}")
    return thread


# ------------------------------------------------------------------- log book
class BookLog:
    """The ocr_log.json file on Drive — source of truth for monitoring."""

    def __init__(self, drive: DriveClient, folder_id: str, engine: Dict[str, Any],
                 folders: Dict[str, str]):
        self.drive = drive
        self.folder_id = folder_id
        self.payload: Dict[str, Any] = {
            "pipeline": PIPELINE_ID,
            "description": (
                "GLM-OCR of THSC level11/physics books → ocr-book/ "
                "(one book at a time; source PDFs never stored in the repo)"
            ),
            "engine": engine,
            "folders": folders,
            "started_at": utcnow(),
            "last_updated": utcnow(),
            "books": [],
        }
        self.book_index: Dict[str, Dict[str, Any]] = {}
        self.local_copy = WORK_ROOT / LOG_FILE_NAME

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        existing = self.drive.find_file(self.folder_id, LOG_FILE_NAME)
        if existing:
            try:
                data = self.drive.download_json(existing["id"])
                books = data.get("books", [])
                # keep engine/folders fresh, but preserve historical books
                data["engine"] = self.payload["engine"]
                data["folders"] = self.payload["folders"]
                data["resumed_at"] = utcnow()
                data["books"] = books
                self.payload = data
                log_line(f"resuming existing log (file id {existing['id']})")
            except Exception as exc:  # noqa: BLE001
                log_line(f"WARNING: could not load existing log: {exc}")
        for entry in self.payload["books"]:
            self.book_index[entry["drive_file_id"]] = entry

    def push(self, note: Optional[str] = None) -> None:
        self.payload["last_updated"] = utcnow()
        self.payload["summary"] = self.summary()
        if note:
            self.payload["last_note"] = note
        try:
            self.drive.upload_json(self.folder_id, LOG_FILE_NAME, self.payload)
            self.local_copy.parent.mkdir(parents=True, exist_ok=True)
            self.local_copy.write_text(
                json.dumps(self.payload, ensure_ascii=False, indent=2), "utf-8"
            )
        except DriveError as exc:
            log_line(f"WARNING: failed to sync log to Drive: {exc}")

    # -- book entries -----------------------------------------------------
    def entry(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        entry = self.book_index.get(meta["id"])
        if entry is None:
            entry = {
                "name": meta["name"],
                "drive_file_id": meta["id"],
                "size_bytes": int(meta.get("size", 0) or 0),
                "status": "pending",
                "attempts": 0,
            }
            self.payload["books"].append(entry)
            self.book_index[meta["id"]] = entry
        entry["name"] = meta["name"]  # keep renamed files in sync
        return entry

    def mark(self, meta: Dict[str, Any], status: str, **extra: Any) -> None:
        entry = self.entry(meta)
        entry["status"] = status
        entry.update(extra)
        self.push()

    def summary(self) -> Dict[str, Any]:
        books = self.payload["books"]
        done = sum(1 for b in books if b["status"] == "done")
        failed = sum(1 for b in books if b["status"] == "failed")
        processing = sum(1 for b in books if b["status"] == "processing")
        pending = sum(1 for b in books if b["status"] == "pending")
        return {
            "total": len(books),
            "done": done,
            "failed": failed,
            "processing": processing,
            "pending": pending,
        }


# ---------------------------------------------------------------------- main
def pdf_books(children: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    books = [
        c for c in children
        if c.get("mimeType") == "application/pdf"
        or c.get("name", "").lower().endswith(".pdf")
    ]
    return sorted(books, key=lambda c: c["name"].lower())


def main() -> int:
    parser_cli = argparse.ArgumentParser(description=__doc__)
    parser_cli.add_argument("--dry-run", action="store_true",
                            help="resolve folders and list books; fake OCR; "
                                 "write log locally only (no Drive uploads)")
    parser_cli.add_argument("--limit", type=int, default=0,
                            help="process at most N books then stop (0 = all)")
    args = parser_cli.parse_args()

    # single-instance lock -------------------------------------------------
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log_line("another pipeline instance is already running — exiting.")
        return 0

    log_line("=== GLM-OCR THSC physics pipeline starting ===")
    start_status_server()
    start_heartbeat()

    creds = load_drive_creds()
    drive = DriveClient(creds)

    # folder resolution ------------------------------------------------------
    thsc_meta = {"id": THSC_FOLDER_ID, "name": "THSC (root id)"}
    level_meta = drive.find_folder(THSC_FOLDER_ID, LEVEL_NAME, create=False)
    subject_meta = drive.find_folder(level_meta["id"], SUBJECT_NAME, create=False)
    ocrbook_meta = drive.find_folder(subject_meta["id"], OCR_BOOK_DIR_NAME, create=True)
    log_line(
        f"folders: THSC/{LEVEL_NAME}/{SUBJECT_NAME}/{OCR_BOOK_DIR_NAME} → "
        f"{ocrbook_meta['id']}"
    )

    books = pdf_books(drive.list_children(subject_meta["id"]))
    log_line(f"found {len(books)} PDF books in {SUBJECT_NAME}")

    # engine -------------------------------------------------------------
    engine_info: Dict[str, Any]
    engine: Any
    if args.dry_run:
        engine_info = {"mode": "dry-run", "model": "fake"}
        engine = None
    else:
        try:
            engine, engine_info = build_engine()
        except Exception as exc:  # noqa: BLE001
            engine_info = {"mode": "failed", "error": str(exc)[:500]}
            log_line(f"FATAL: engine init failed: {exc}")
            engine = None

    folders = {
        "thsc_root": thsc_meta["id"],
        "level": f"{level_meta['name']} ({level_meta['id']})",
        "subject": f"{subject_meta['name']} ({subject_meta['id']})",
        "output": f"{OCR_BOOK_DIR_NAME} ({ocrbook_meta['id']})",
    }
    booklog = BookLog(drive, ocrbook_meta["id"], engine_info, folders)
    if args.dry_run:
        booklog.folder_id = "DRY-RUN"  # never touch Drive in dry-run mode

    STATE.update(engine=engine_info, books_total=len(books), status="running")

    # load previous state --------------------------------------------------
    if not args.dry_run:
        booklog.load()
    for meta in books:
        entry = booklog.entry(meta)
        # reset stale 'processing' flags left by a killed previous run
        if entry["status"] == "processing":
            entry["status"] = "pending"
            entry["note"] = "recovered from interrupted run"
    booklog.push(f"run started; {len(books)} books discovered")

    # main loop — ONE BOOK AT A TIME ---------------------------------------
    attempted = 0
    for meta in books:
        entry = booklog.book_index[meta["id"]]
        if entry["status"] == "done":
            continue
        if args.limit and attempted >= args.limit:
            log_line(f"limit of {args.limit} reached — stopping")
            break

        attempted += 1  # count every book we touch, not only successes
        name = meta["name"]
        stem = Path(name).stem
        started = time.time()
        STATE.update(current_book=name, status="ocr")
        booklog.mark(meta, "processing", started_at=utcnow(),
                     attempts=entry.get("attempts", 0) + 1,
                     pages=None, error=None)
        log_line(f"--- processing: {name} "
                 f"({int(meta.get('size', 0) or 0) / 1e6:.2f} MB)")

        book_dir = WORK_ROOT / "book"
        shutil.rmtree(book_dir, ignore_errors=True)
        book_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = book_dir / name

        try:
            # 1. fetch from Drive (temp only — nothing is stored in the repo)
            drive.download_file(meta["id"], pdf_path)

            # 2. OCR with the engine
            if args.dry_run:
                time.sleep(2)
                md = f"# {stem}\n\n(dry-run fake OCR output)"
                structured = {"dry_run": True, "pages": 3}
                pages = 3
                out_dir = book_dir / "out"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{stem}.md").write_text(md, "utf-8")
                (out_dir / f"{stem}.json").write_text(
                    json.dumps(structured, indent=2), "utf-8")
            else:
                if engine is None:
                    raise RuntimeError("engine unavailable (init failed earlier)")
                if (engine_info.get("mode") == "maas-cloud"
                        and pdf_path.stat().st_size >= MaaS_MAX_FILE_BYTES):
                    raise RuntimeError(
                        "PDF exceeds the 10 MB cloud (MaaS) API limit — "
                        "use the self-hosted Ollama mode for large files"
                    )
                out_dir = book_dir / "out"
                res = ocr_book(engine, pdf_path, out_dir)
                md, structured, pages = (
                    res["markdown"], res["json"], res["pages"])
                (out_dir / f"{stem}.md").write_text(md, "utf-8")
                (out_dir / f"{stem}.json").write_text(
                    json.dumps(structured, ensure_ascii=False, indent=2),
                    "utf-8")

            # 3. upload the OCRed book to Drive
            md_file = out_dir / f"{stem}.md"
            json_file = out_dir / f"{stem}.json"
            uploads: Dict[str, Any] = {}
            if not args.dry_run:
                up_md = drive.upload_file(md_file, ocrbook_meta["id"])
                up_json = drive.upload_file(json_file, ocrbook_meta["id"])
                uploads = {
                    "output_md": {
                        "name": up_md.get("name", md_file.name),
                        "drive_file_id": up_md.get("id"),
                    },
                    "output_json": {
                        "name": up_json.get("name", json_file.name),
                        "drive_file_id": up_json.get("id"),
                    },
                }

            duration = round(time.time() - started, 1)
            booklog.mark(
                meta, "done",
                completed_at=utcnow(),
                duration_seconds=duration,
                pages=pages,
                markdown_chars=len(md),
                **uploads,
            )
            done = booklog.summary()["done"]
            log_line(f"✓ done: {name} — pages={pages} in {duration}s "
                     f"({done}/{len(books)} complete)")
            STATE.update(books_done=done)

        except Exception as exc:  # noqa: BLE001
            duration = round(time.time() - started, 1)
            booklog.mark(
                meta, "failed",
                completed_at=utcnow(),
                duration_seconds=duration,
                error=str(exc)[:800],
            )
            failed = booklog.summary()["failed"]
            log_line(f"✗ FAILED: {name} — {exc}")
            STATE.update(books_failed=failed)
        finally:
            # 4. never keep PDFs or outputs around (and never in the repo)
            shutil.rmtree(book_dir, ignore_errors=True)

    summary = booklog.summary()
    booklog.push(
        f"run finished — {summary['done']} done, {summary['failed']} failed, "
        f"{summary['pending']} pending"
    )
    STATE.update(status="finished", current_book=None, detail=summary)
    log_line(f"=== run finished: {summary} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
