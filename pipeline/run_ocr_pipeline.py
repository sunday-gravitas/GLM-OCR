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
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))


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


_FATAL_DRIVE: Optional["DriveClient"] = None
_FATAL_FOLDER: str = os.environ.get(
    "OCR_BOOK_FOLDER_ID", "1iDlabXG8aF9zxSyB7EzlOboL2N-buNqj")


def fatal_report(reason: str) -> None:
    """Last-gasp: push the crash reason + log tail to Drive (best effort)."""
    global _FATAL_DRIVE
    try:
        if _FATAL_DRIVE is None:
            _FATAL_DRIVE = DriveClient(load_drive_creds())
        _FATAL_DRIVE.upload_json(_FATAL_FOLDER, "pipeline_fatal.json", {
            "component": "run_ocr_pipeline",
            "codespace": os.environ.get("CODESPACE_NAME", "?"),
            "fatal": reason,
            "updated_at": utcnow(),
            "pipeline_log_tail": _tail("/tmp/pipeline.log", 60),
        })
    except Exception:  # noqa: BLE001
        pass


def log_line(msg: str) -> None:
    print(f"[{utcnow()}] {msg}", flush=True)


# ------------------------------------------------------------------ credentials
def load_drive_creds() -> Dict[str, Any]:
    """Load Google OAuth credentials from env var or file (codespace secret)."""
    raw = os.environ.get("DRIVE_CREDS_JSON", "").strip()
    if not raw:
        for candidate in (
            os.environ.get("DRIVE_CREDS_FILE"),
            str(Path(__file__).resolve().parent.parent / ".drive-creds.json"),
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
    cfg: Dict[str, Any] = {}
    try:
        import yaml  # shipped with the SDK install
        cfg = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    except ImportError:
        # minimal fallback when PyYAML is unavailable — the essential
        # fields the pipeline overrides (everything else uses SDK defaults)
        cfg = {"pipeline": {"page_loader": {}, "layout": {}, "result_formatter": {}}}

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
    cfg.setdefault("logging", {})["level"] = os.environ.get(
        "GLMOCR_LOG_LEVEL", "INFO")

    # JSON is a subset of YAML 1.2 — safe_load parses it fine, and this
    # avoids a hard dependency on PyYAML for the pipeline process
    path = Path("/tmp/glmocr_engine.yaml")
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
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


def env_dump() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for mod in ("torch", "torchvision", "transformers", "tokenizers",
                "safetensors", "accelerate", "cv2", "numpy", "PIL", "pymupdf"):
        try:
            m = __import__(mod)
            out[mod] = str(getattr(m, "__version__", "?"))
        except Exception as exc:  # noqa: BLE001
            out[mod] = f"IMPORT FAIL: {exc}"[:120]
    return out


def engine_selftest(parser: Any) -> Dict[str, Any]:
    """Run the engine on a synthetic image; zero output means the layout
    detector is broken (corrupt cache / bad build), not just a weird PDF."""
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (1000, 700), "white")
        draw = ImageDraw.Draw(img)
        draw.text((80, 80), "HELLO WORLD OCR SELF TEST 12345", fill="black")
        draw.text((80, 160), "The quick brown fox jumps over the lazy dog.", fill="black")
        draw.rectangle((80, 300, 920, 520), outline="black", width=3)
        draw.text((120, 390), "Column A    Column B    Column C", fill="black")
        path = Path("/tmp/ocr_selftest.png")
        img.save(path)
        result = parser.parse(str(path))
        md = result.markdown_result or ""
        structured = result.json_result
        regions = 0
        if isinstance(structured, list):
            regions = sum(len(pg) for pg in structured if isinstance(pg, list))
        return {"markdown_chars": len(md), "regions": regions,
                "sample": md.strip()[:120]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}


def layout_probe(image_path: str) -> Dict[str, Any]:
    """Run the PP-DocLayout detector directly on an image (bypasses OCR)."""
    try:
        from glmocr.config import load_config
        from glmocr.layout.layout_detector import PPDocLayoutDetector

        cfg = load_config("/tmp/glmocr_engine.yaml")
        detector = PPDocLayoutDetector(cfg.pipeline.layout)
        detector.start()
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        results, _vis = detector.process([img], save_visualization=False)
        page = results[0] if results else {}
        boxes = page.get("boxes", [])
        scores = page.get("scores", [])
        labels = page.get("labels", [])
        score_vals = []
        for sc in scores:
            try:
                score_vals.append(round(float(sc), 4))
            except (TypeError, ValueError):
                score_vals.append(str(sc))
        return {
            "num_boxes": len(boxes),
            "scores": score_vals[:20],
            "labels": [int(l) for l in labels[:20]]
            if len(labels) else [],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}


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
    regions = 0
    if isinstance(structured, list):
        pages = len(structured)
        regions = sum(len(pg) for pg in structured if isinstance(pg, list))
    elif isinstance(structured, dict) and isinstance(
        structured.get("layout_details"), list
    ):
        pages = len(structured["layout_details"])
    if pages and regions == 0 and not markdown.strip():
        raise RuntimeError(
            f"empty OCR output: {pages} pages rendered but 0 regions "
            f"detected (layout detector problem)")
    out_dir.mkdir(parents=True, exist_ok=True)
    return {"markdown": markdown, "json": structured, "pages": pages,
            "regions": regions}


# ------------------------------------------------------- status & keep-alive
def _tail(path: str, lines: int = 30) -> str:
    try:
        text = Path(path).read_text(errors="replace").splitlines()
        return "\n".join(text[-lines:])
    except OSError:
        return "(not available)"


def start_runtime_reporter(drive: DriveClient, folder_id: str) -> None:
    """Push a status snapshot (+ main-thread stack dump + log tail) to Drive
    every 5 minutes, so the run can be monitored without codespace access."""
    main_tid = threading.get_ident()

    def snapshot() -> Dict[str, Any]:
        data = STATE.snapshot()
        stack = []
        for tid, frame in sys._current_frames().items():
            if tid == main_tid:
                stack = traceback.format_stack(frame)
                break
        data["main_thread_stack"] = "".join(stack[-8:]).strip() or "(empty)"
        data["pipeline_log_tail"] = _tail("/tmp/pipeline.log")
        return data

    def report() -> None:
        wait = 300
        while True:
            time.sleep(wait)
            try:
                drive.upload_json(folder_id, "pipeline_status.json", snapshot())
                wait = 300
            except Exception as exc:  # noqa: BLE001
                log_line(f"WARNING: runtime report failed: {exc}")
                wait = 60  # retry sooner while things are broken

    threading.Thread(target=report, daemon=True).start()
    try:
        drive.upload_json(folder_id, "pipeline_status.json", snapshot())
        log_line("runtime reporter armed (pipeline_status.json every 5 min)")
    except DriveError as exc:
        log_line(f"WARNING: initial runtime report failed: {exc}")


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
        try:
            tmp = Path("/tmp/pipeline_state.json.tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace("/tmp/pipeline_state.json")
        except OSError:
            pass

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.data)


STATE = PipelineState()
STATE.update()  # persist the initial state file for the standalone status server


# ------------------------------------------------------------------- log book
class BookLog:
    """The ocr_log.json file on Drive — source of truth for monitoring."""

    def __init__(self, drive: DriveClient, folder_id: str, engine: Dict[str, Any],
                 folders: Dict[str, str]):
        self.drive = drive
        self.folder_id = folder_id
        self.enabled = True
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
            # books marked done with empty output (broken engine era) are
            # reset so they get reprocessed
            if entry.get("status") == "done" and (
                not entry.get("markdown_chars")
                or not entry.get("output_md")
            ):
                entry["status"] = "pending"
                entry["note"] = "reset: empty output from broken engine"
            self.book_index[entry["drive_file_id"]] = entry

    def push(self, note: Optional[str] = None) -> None:
        if not self.enabled:
            return
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
    log_line("status served by standalone process (port 8787)")

    creds = load_drive_creds()
    log_line("drive credentials loaded")
    drive = DriveClient(creds)
    global _FATAL_DRIVE
    _FATAL_DRIVE = drive
    # arm the runtime reporter EARLY (before any Drive call can hang), so a
    # stack dump + step log always reaches Drive
    start_runtime_reporter(drive, os.environ.get(
        "OCR_BOOK_FOLDER_ID", "1iDlabXG8aF9zxSyB7EzlOboL2N-buNqj"))

    # folder resolution ------------------------------------------------------
    log_line("resolving folders: THSC → level11 → physics → ocr-book …")
    thsc_meta = {"id": THSC_FOLDER_ID, "name": "THSC (root id)"}
    log_line(f"listing THSC root ({THSC_FOLDER_ID}) …")
    level_meta = drive.find_folder(THSC_FOLDER_ID, LEVEL_NAME, create=False)
    log_line(f"found level folder: {level_meta['name']} ({level_meta['id']})")
    subject_meta = drive.find_folder(level_meta["id"], SUBJECT_NAME, create=False)
    log_line(f"found subject folder: {subject_meta['name']} ({subject_meta['id']})")
    ocrbook_meta = drive.find_folder(subject_meta["id"], OCR_BOOK_DIR_NAME, create=True)
    log_line(f"output folder: {ocrbook_meta['name']} ({ocrbook_meta['id']})")
    log_line(
        f"folders: THSC/{LEVEL_NAME}/{SUBJECT_NAME}/{OCR_BOOK_DIR_NAME} → "
        f"{ocrbook_meta['id']}"
    )
    books = pdf_books(drive.list_children(subject_meta["id"]))
    log_line(f"found {len(books)} PDF books in {SUBJECT_NAME}")

    folders = {
        "thsc_root": thsc_meta["id"],
        "level": f"{level_meta['name']} ({level_meta['id']})",
        "subject": f"{subject_meta['name']} ({subject_meta['id']})",
        "output": f"{OCR_BOOK_DIR_NAME} ({ocrbook_meta['id']})",
    }

    # engine ------------------------------------------------------------
    # initial log push happens BEFORE engine init — the layout model may
    # take minutes to download/load on the first run
    engine_info: Dict[str, Any] = {"mode": "initializing"}
    engine: Any = None
    booklog = BookLog(drive, ocrbook_meta["id"], engine_info, folders)
    if args.dry_run:
        booklog.folder_id = "DRY-RUN"  # never touch Drive in dry-run mode
        booklog.enabled = False

    STATE.update(engine=engine_info, books_total=len(books), status="running")

    # load previous state -------------------------------------------------
    if not args.dry_run:
        booklog.load()
    for meta in books:
        entry = booklog.entry(meta)
        # reset stale 'processing' flags left by a killed previous run
        if entry["status"] == "processing":
            entry["status"] = "pending"
            entry["note"] = "recovered from interrupted run"
    booklog.push(f"run started; {len(books)} books discovered; engine loading")

    if args.dry_run:
        booklog.payload["engine"] = {"mode": "dry-run", "model": "fake"}
    else:
        try:
            engine, engine_info = build_engine()
        except Exception as exc:  # noqa: BLE001
            engine_info = {"mode": "failed", "error": str(exc)[:500]}
            log_line(f"FATAL: engine init failed: {exc}")
            engine = None
        if engine is not None:
            engine_info["env"] = env_dump()
            st = engine_selftest(engine)
            engine_info["selftest"] = st
            if not st.get("markdown_chars"):
                probe = layout_probe("/tmp/ocr_selftest.png")
                engine_info["layout_probe"] = probe
                log_line(f"FATAL: engine self-test EMPTY — env={engine_info['env']} probe={probe}")
                Path("/tmp/.engine_broken").write_text(utcnow(), "utf-8")
                if not args.dry_run:
                    booklog.payload["engine"] = engine_info
                    booklog.push("engine self-test FAILED — exiting for repair")
                    fatal_report("engine self-test empty (layout detector broken)")
                return 1
            log_line(f"engine self-test OK: {st.get('sample')!r}")
        booklog.payload["engine"] = engine_info
        booklog.push(f"engine ready: {engine_info.get('mode')}")
        STATE.update(engine=engine_info)

    # main loop — ONE BOOK AT A TIME ---------------------------------------
    attempted = 0
    consecutive_drive_failures = 0
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
                regions = 3
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
                regions=regions,
                markdown_chars=len(md),
                **uploads,
            )
            done = booklog.summary()["done"]
            consecutive_drive_failures = 0
            log_line(f"✓ done: {name} — pages={pages} regions={regions} "
                     f"in {duration}s ({done}/{len(books)} complete)")
            STATE.update(books_done=done)

        except Exception as exc:  # noqa: BLE001
            duration = round(time.time() - started, 1)
            try:
                booklog.mark(
                    meta, "failed",
                    completed_at=utcnow(),
                    duration_seconds=duration,
                    error=str(exc)[:800],
                )
            except Exception:  # noqa: BLE001
                pass
            failed = booklog.summary()["failed"]
            log_line(f"✗ FAILED: {name} — {exc}")
            STATE.update(books_failed=failed)
            if isinstance(exc, DriveError):
                consecutive_drive_failures += 1
                if consecutive_drive_failures >= 4:
                    log_line("Drive appears to be down — exiting for restart")
                    if not args.dry_run:
                        fatal_report(
                            f"circuit breaker: {consecutive_drive_failures} "
                            f"consecutive Drive failures — restarting")
                    return 1
            else:
                consecutive_drive_failures = 0
        finally:
            # 4. never keep PDFs or outputs around (and never in the repo)
            shutil.rmtree(book_dir, ignore_errors=True)

    summary = booklog.summary()
    if not args.dry_run:
        booklog.push(
            f"run finished — {summary['done']} done, {summary['failed']} failed, "
            f"{summary['pending']} pending"
        )
    STATE.update(status="finished", current_book=None, detail=summary)
    log_line(f"=== run finished: {summary} ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        import traceback as _tb
        fatal_report("FATAL: " + "".join(
            _tb.format_exception(type(exc), exc, exc.__traceback__))[-1500:])
        raise
