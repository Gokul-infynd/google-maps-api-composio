#!/usr/bin/env python3
"""
FastAPI-rendered UI for the Composio Google Maps batch processor
===============================================================

Serves two HTML pages (HTML + CSS + JS, no server-side template engine) from
the SAME process/port as the REST bridge:

    http://127.0.0.1:8000/login    login page (username + password -> JWT)
    http://127.0.0.1:8000/home    the batch app page

Features
--------
* Authentication: username/password (PBKDF2-SHA256) -> signed HS256 JWT
  (stdlib hmac/base64, no extra dependency). The token is stored client-side
  and sent as `Authorization: Bearer <token>` on every /ui/api/* call.
* Per-user run history: every started run is persisted in SQLite
  (`UI_DB_PATH`, default ./data/ui.db) and shown in the History view.
* Background jobs: a run is executed in a server-side worker thread, so it
  keeps processing even if the browser tab is closed - reconnect (History or
  auto-attach on page load) to watch it live.
* Temp hygiene: uploaded/processed files live under TEMP_DIR and are erased
  after TEMP_FILE_TTL_MINUTES by a background sweeper (history rows survive).

Config (env, all optional)
--------------------------
    UI_USERNAME            bootstrap admin username   (default: admin)
    UI_PASSWORD            bootstrap admin password   (default: random, logged once)
    UI_USERS               extra users "user:pass,user2:pass2"
    UI_JWT_SECRET          token signing secret       (default: generated, persisted in DB)
    UI_JWT_EXPIRE_MINUTES  token lifetime             (default: 720 = 12h)
    UI_DB_PATH             sqlite file                (default: ./data/ui.db)
    UI_MAX_UPLOAD_MB       upload size cap            (default: 200)
    TEMP_DIR / TEMP_FILE_TTL_MINUTES / TEMP_CLEANUP_INTERVAL_SECONDS
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

import batch_processor as bp

log = logging.getLogger("ui")

router = APIRouter()

UI_DIR = Path(__file__).resolve().parent / "ui"

# --------------------------------------------------------------------------
# Configuration - resolved at startup() so tests can swap env per-instance
# --------------------------------------------------------------------------

DB_PATH: Path = Path("data/ui.db")
TEMP_DIR: Path = Path("/tmp/composio_bridge")
JOBS_DIR: Path = TEMP_DIR / "jobs"
TTL_SECONDS: float = 3600.0
SWEEP_INTERVAL: float = 300.0
JWT_SECRET: str = ""
JWT_EXPIRE_SECONDS: int = 720 * 60
MAX_UPLOAD_BYTES: int = 200 * 1024 * 1024

_PBKDF2_ITERATIONS = 310_000
_login_fails: dict[str, list[float]] = {}
_sweeper_started = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# SQLite (users / jobs / settings) - one short-lived connection per call
# --------------------------------------------------------------------------


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id            TEXT PRIMARY KEY,
                username      TEXT NOT NULL,
                original_name TEXT NOT NULL,
                sheet         TEXT,
                status        TEXT NOT NULL,
                config_json   TEXT,
                summary_json  TEXT,
                error         TEXT,
                input_path    TEXT NOT NULL,
                output_path   TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                started_at    TEXT,
                finished_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(username, created_at DESC);
            """
        )


def _setting_get(key: str) -> Optional[str]:
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _setting_set(key: str, value: str) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# --------------------------------------------------------------------------
# Passwords (PBKDF2-SHA256, stdlib) and JWT (HS256, stdlib hmac)
# --------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt),
                             _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt),
                                 int(iterations))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:  # noqa: BLE001 - malformed hash never authenticates
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(username: str, expires_in: Optional[int] = None) -> str:
    """Sign a compact HS256 JWT (header.payload.signature)."""
    now = int(time.time())
    exp = now + (expires_in if expires_in is not None else JWT_EXPIRE_SECONDS)
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"},
                                separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"sub": username, "iat": now, "exp": exp},
                                 separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64url(hmac.new(JWT_SECRET.encode("utf-8"), signing_input,
                                 hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def verify_token(token: str) -> Optional[str]:
    """Return the username for a valid, unexpired token; None otherwise."""
    try:
        header, payload, signature = token.split(".")
        signing_input = f"{header}.{payload}".encode("ascii")
        expected = _b64url(hmac.new(JWT_SECRET.encode("utf-8"), signing_input,
                                    hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            return None
        claims = json.loads(_b64url_decode(payload))
        if int(claims.get("exp", 0)) < time.time():
            return None
        sub = claims.get("sub")
        return sub if isinstance(sub, str) and sub else None
    except Exception:  # noqa: BLE001 - any malformed token is rejected
        return None


def _unauthorized(message: str = "Not authenticated - please log in") -> HTTPException:
    return HTTPException(status_code=401, detail=message,
                         headers={"WWW-Authenticate": "Bearer"})


def current_user(authorization: Optional[str] = Header(None)) -> str:
    """FastAPI dependency: require a valid Bearer JWT, return the username."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()
    username = verify_token(authorization[7:].strip())
    if not username:
        raise _unauthorized("Invalid or expired token - please log in again")
    return username


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def _read_page(name: str) -> str:
    path = UI_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        log.exception("cannot read UI page %s", path)
        raise HTTPException(500, f"UI page '{name}' is missing on the server") from None


@router.get("/", include_in_schema=False)
async def ui_root() -> RedirectResponse:
    return RedirectResponse("/home")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page() -> HTMLResponse:
    return HTMLResponse(_read_page("login.html"))


@router.get("/home", response_class=HTMLResponse, include_in_schema=False)
async def app_page() -> HTMLResponse:
    return HTMLResponse(_read_page("app.html"))


@router.get("/ui", include_in_schema=False)
async def ui_legacy() -> RedirectResponse:
    """Old page URL - kept as a redirect so existing bookmarks keep working."""
    return RedirectResponse("/home")


# --------------------------------------------------------------------------
# Auth endpoints
# --------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _throttle_login(username: str) -> None:
    """Simple limiter: >5 failed attempts per username within 5 minutes -> 429."""
    now = time.time()
    fails = [t for t in _login_fails.get(username, []) if now - t < 300]
    _login_fails[username] = fails
    if len(fails) >= 5:
        raise HTTPException(429, "Too many failed attempts - try again in a few minutes")


@router.post("/auth/login", summary="Exchange username/password for a JWT")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    username = body.username.strip()
    _throttle_login(username)
    with _db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE username=?",
                           (username,)).fetchone()
    if not row or not check_password(body.password, row["password_hash"]):
        _login_fails.setdefault(username, []).append(time.time())
        raise HTTPException(401, "Invalid username or password")
    _login_fails.pop(username, None)
    token = make_token(username)
    return {"ok": True, "token": token, "username": username,
            "expires_in": JWT_EXPIRE_SECONDS}


@router.get("/ui/api/me", summary="Current user")
def who_am_i(user: str = Depends(current_user)) -> dict[str, Any]:
    return {"username": user}


# --------------------------------------------------------------------------
# Workbook upload (raw body - no multipart dependency)
# --------------------------------------------------------------------------


def _safe_stem(filename: str) -> tuple[str, str]:
    """-> (sanitized basename, suffix) with a whitelisted suffix."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name).strip("._") or "upload"
    suffix = Path(name).suffix.lower()
    if suffix in bp.EXCEL_SUFFIXES | bp.CSV_SUFFIXES:
        return name, suffix
    stem = Path(name).stem or "upload"
    return f"{stem}.xlsx", ".xlsx"


def _sheet_info(sheets: dict[str, Any]) -> dict[str, Any]:
    previews: dict[str, list[dict[str, Any]]] = {}
    for name, df in sheets.items():
        cols = [str(c) for c in df.columns]
        values = json.loads(df.head(20).to_json(orient="values"))
        previews[name] = [dict(zip(cols, row)) for row in values]
    return {
        "sheets": list(sheets),
        "sheet": bp.pick_sheet(sheets),
        "sheet_columns": {n: [str(c) for c in df.columns] for n, df in sheets.items()},
        "sheet_rows": {n: len(df) for n, df in sheets.items()},
        "sheet_detected": {n: bp.detect_columns(df) for n, df in sheets.items()},
        "previews": previews,
    }


@router.post("/ui/api/uploads", summary="Upload a spreadsheet (raw file body)")
async def upload_file(request: Request, filename: str = Query(...),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty file upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")

    original_name, suffix = _safe_stem(filename)
    upload_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / upload_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{suffix}"
    input_path.write_bytes(data)

    try:
        sheets = bp.read_workbook(input_path)
    except Exception as exc:  # noqa: BLE001 - bad/corrupt file is a client error
        raise HTTPException(400, f"Could not read '{original_name}': {exc}") from exc

    info = _sheet_info(sheets)
    info.update({
        "upload_id": upload_id,
        "original_name": original_name,
        "status": "draft",
    })
    with _db() as conn:
        conn.execute(
            "INSERT INTO jobs(id, username, original_name, sheet, status, input_path, "
            "output_path, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (upload_id, user, original_name, info["sheet"], "draft",
             str(input_path), str(job_dir / f"processed{suffix}"), _now_iso()))
    return {"ok": True, **info}


# --------------------------------------------------------------------------
# Jobs: start / poll / stop / download
# --------------------------------------------------------------------------

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}  # job_id -> {reporter, thread, username, started}


class JobStart(BaseModel):
    upload_id: str
    sheet: Optional[str] = None
    query_columns: list[str] = Field(min_length=1, max_length=50)
    ll: Optional[str] = None
    lat: Optional[str] = None
    lng: Optional[str] = None
    zoom: Optional[str] = None
    start: Optional[str] = None
    start_row: Optional[int] = Field(default=None, ge=1)
    end_row: Optional[int] = Field(default=None, ge=1)
    shape: str = "full"
    batch_size: Optional[int] = Field(default=None, ge=1, le=100)
    concurrency: Optional[int] = Field(default=None, ge=1, le=16)
    bridge_url: Optional[str] = None


def _get_owned_job(job_id: str, user: str) -> sqlite3.Row:
    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["username"] != user:
        # Do not leak the existence of other users' jobs.
        raise HTTPException(404, "Job not found")
    return row


def _worker(job_id: str, reporter: bp.ProgressReporter, kwargs: dict[str, Any]) -> None:
    """Run process_file in the background; persist the outcome to SQLite."""
    try:
        with _db() as conn:
            conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        summary = bp.process_file(**kwargs, progress_cb=reporter.publish,
                                  stop_cb=reporter.stop_requested)
        stopped = bool(summary.get("stopped"))
        reporter.finish(summary, stopped=stopped)
        status = "stopped" if stopped else "done"
        with _db() as conn:
            conn.execute("UPDATE jobs SET status=?, summary_json=?, finished_at=?, "
                         "error=NULL WHERE id=?",
                         (status, json.dumps(summary, default=str), _now_iso(), job_id))
        log.info("job %s finished: %s (%s/%s rows)", job_id, status,
                 summary.get("rows_succeeded"), summary.get("rows_processed"))
    except Exception as exc:  # noqa: BLE001 - any failure must be visible in the UI
        log.exception("job %s failed", job_id)
        message = f"{type(exc).__name__}: {exc}"
        reporter.fail(message)
        try:
            with _db() as conn:
                conn.execute("UPDATE jobs SET status='error', error=?, finished_at=? "
                             "WHERE id=?", (message, _now_iso(), job_id))
        except Exception:  # noqa: BLE001 - best effort
            log.exception("could not persist failure of job %s", job_id)
    finally:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["finished"] = True


def _prune_jobs(max_finished: int = 60) -> None:
    """Keep the in-memory registry small: drop finished entries beyond N."""
    with _jobs_lock:
        finished = [jid for jid, e in _jobs.items() if e.get("finished")]
        for jid in finished[:max(0, len(finished) - max_finished)]:
            _jobs.pop(jid, None)


@router.post("/ui/api/jobs", summary="Start processing (background job)")
def start_job(body: JobStart, user: str = Depends(current_user)) -> dict[str, Any]:
    row = _get_owned_job(body.upload_id, user)
    if row["status"] != "draft":
        raise HTTPException(409, "This upload has already been started")
    if body.shape not in ("slim", "full"):
        raise HTTPException(422, "shape must be 'slim' or 'full'")
    if body.bridge_url and not body.bridge_url.startswith(("http://", "https://")):
        raise HTTPException(422, "bridge_url must be an http(s) URL")

    input_path = Path(row["input_path"])
    output_path = Path(row["output_path"])
    if not input_path.exists():
        raise HTTPException(410, "The uploaded file has expired - upload it again")

    # Row count for the progress total (clamped exactly like the processor).
    try:
        sheets = bp.read_workbook(input_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not re-read the input file: {exc}") from exc
    sheet = body.sheet or row["sheet"] or bp.pick_sheet(sheets)
    if sheet not in sheets:
        raise HTTPException(422, f"Sheet '{sheet}' not found (available: {list(sheets)})")
    lo, hi = bp._range_bounds(len(sheets[sheet]), body.start_row, body.end_row)
    total = max(0, hi - lo)

    overrides: dict[str, Any] = {
        "query": list(body.query_columns),
        "ll": body.ll, "lat": body.lat, "lng": body.lng,
        "zoom": body.zoom, "start": body.start,
    }
    config = {
        "sheet": sheet, "query_columns": list(body.query_columns),
        "ll": body.ll, "lat": body.lat, "lng": body.lng, "zoom": body.zoom,
        "start": body.start, "start_row": lo + 1, "end_row": hi,
        "shape": body.shape, "batch_size": body.batch_size,
        "concurrency": body.concurrency, "bridge_url": body.bridge_url,
        "rows_in_range": total,
    }
    with _db() as conn:
        conn.execute("UPDATE jobs SET status='queued', sheet=?, config_json=?, "
                     "started_at=? WHERE id=?",
                     (sheet, json.dumps(config), _now_iso(), body.upload_id))

    reporter = bp.ProgressReporter(total=total)
    kwargs: dict[str, Any] = {
        "input_path": input_path, "output_path": output_path, "sheet": sheet,
        "column_overrides": overrides, "start_row": lo + 1, "end_row": hi,
        "shape": body.shape,
    }
    if body.bridge_url:
        kwargs["bridge_url"] = body.bridge_url.rstrip("/")
    if body.batch_size:
        kwargs["batch_size"] = int(body.batch_size)
    if body.concurrency:
        kwargs["concurrency"] = int(body.concurrency)

    _prune_jobs()
    thread = threading.Thread(target=_worker, args=(body.upload_id, reporter, kwargs),
                              daemon=True, name=f"job-{body.upload_id}")
    with _jobs_lock:
        _jobs[body.upload_id] = {"reporter": reporter, "thread": thread,
                                 "username": user, "started": time.time(),
                                 "finished": False}
    thread.start()
    log.info("job %s started by %s: sheet=%s rows=%d shape=%s",
             body.upload_id, user, sheet, total, body.shape)
    return {"ok": True, "job_id": body.upload_id, "status": "queued",
            "rows_in_range": total}


def _load_output(row: sqlite3.Row) -> Optional[dict[str, Any]]:
    """Read the processed sheet back for the result view (cap 500 rows)."""
    output_path = Path(row["output_path"])
    if not output_path.exists():
        return None
    try:
        sheets = bp.read_workbook(output_path)
        sheet = row["sheet"] if row["sheet"] in sheets else next(iter(sheets))
        df = sheets[sheet]
        cols = [str(c) for c in df.columns]
        values = json.loads(df.head(500).to_json(orient="values"))
        rows = [dict(zip(cols, r)) for r in values]
        return {"sheet": sheet, "columns": cols, "rows": rows,
                "total_rows": len(df), "truncated": len(df) > 500}
    except Exception as exc:  # noqa: BLE001 - file may be corrupt/expired
        log.warning("cannot read output of job %s: %s", row["id"], exc)
        return {"error": f"Output preview unavailable: {exc}"}


@router.get("/ui/api/jobs/{job_id}", summary="Job status / progress / result")
def get_job(job_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    row = _get_owned_job(job_id, user)
    config = json.loads(row["config_json"] or "{}")
    entry = _jobs.get(job_id)

    progress: Optional[dict[str, Any]] = None
    if entry:
        progress = asdict(entry["reporter"].snapshot())
        if progress.get("summary") is not None:
            # Normalise to plain JSON types (progress may hold exotic values).
            progress["summary"] = json.loads(json.dumps(progress["summary"], default=str))

    response: dict[str, Any] = {
        "id": row["id"],
        "status": row["status"],
        "original_name": row["original_name"],
        "sheet": row["sheet"],
        "config": config,
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "progress": progress,
        "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
        "output": None,
        "download_url": None,
    }
    if row["status"] in ("done", "stopped"):
        output = _load_output(row)
        response["output"] = output or {"error": "Output file not found (expired?)"}
        response["download_url"] = f"/ui/api/jobs/{job_id}/download"
    return response


@router.post("/ui/api/jobs/{job_id}/stop", summary="Request a graceful stop")
def stop_job(job_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    row = _get_owned_job(job_id, user)
    entry = _jobs.get(job_id)
    if row["status"] in ("queued", "running") and entry:
        entry["reporter"].request_stop()
        return {"ok": True, "message": "Stop requested - finishing the current batch"}
    raise HTTPException(409, f"Job is not running (status: {row['status']})")


@router.get("/ui/api/jobs/{job_id}/download", summary="Download the processed file")
def download_job(job_id: str, user: str = Depends(current_user)) -> FileResponse:
    row = _get_owned_job(job_id, user)
    path = Path(row["output_path"])
    if not path.exists():
        raise HTTPException(410, "The processed file has expired and was deleted")
    suffix = path.suffix.lower()
    media = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
             if suffix in bp.EXCEL_SUFFIXES else "text/csv")
    stem = Path(row["original_name"]).stem
    return FileResponse(path, media_type=media,
                        filename=f"{stem}_processed{suffix}")


@router.post("/ui/api/jobs/{job_id}/duplicate",
             summary="Clone a previous upload into a fresh draft (re-run)")
def duplicate_job(job_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    """Copy the input file into a new draft so a range can be re-processed."""
    row = _get_owned_job(job_id, user)
    source = Path(row["input_path"])
    if not source.exists():
        raise HTTPException(410, "The original uploaded file has expired - upload it again")

    suffix = source.suffix.lower()
    new_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / new_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{suffix}"
    input_path.write_bytes(source.read_bytes())

    sheets = bp.read_workbook(input_path)
    info = _sheet_info(sheets)
    info.update({"upload_id": new_id, "original_name": row["original_name"],
                 "status": "draft"})
    with _db() as conn:
        conn.execute(
            "INSERT INTO jobs(id, username, original_name, sheet, status, input_path, "
            "output_path, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (new_id, user, row["original_name"], info["sheet"], "draft",
             str(input_path), str(job_dir / f"processed{suffix}"), _now_iso()))
    return {"ok": True, **info}


# --------------------------------------------------------------------------
# History + config
# --------------------------------------------------------------------------


@router.get("/ui/api/history", summary="Run history for the current user")
def history(user: str = Depends(current_user), limit: int = Query(100, ge=1, le=500)
            ) -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, original_name, sheet, status, config_json, summary_json, error, "
            "created_at, started_at, finished_at FROM jobs "
            "WHERE username=? AND status != 'draft' "
            "ORDER BY created_at DESC LIMIT ?", (user, limit)).fetchall()
    items = []
    for row in rows:
        summary = json.loads(row["summary_json"]) if row["summary_json"] else None
        config = json.loads(row["config_json"] or "{}")
        items.append({
            "id": row["id"],
            "original_name": row["original_name"],
            "sheet": row["sheet"],
            "status": row["status"],
            "error": row["error"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "shape": config.get("shape"),
            "range": [config.get("start_row"), config.get("end_row")],
            "rows_processed": (summary or {}).get("rows_processed"),
            "rows_succeeded": (summary or {}).get("rows_succeeded"),
            "rows_failed": (summary or {}).get("rows_failed"),
        })
    return {"ok": True, "items": items}


@router.get("/ui/api/config", summary="Defaults for the UI page")
def ui_config() -> dict[str, Any]:
    return {
        "bridge_url": bp.BRIDGE_URL,
        "batch_size": bp.PROCESSOR_BATCH_SIZE,
        "concurrency": bp.PROCESSOR_CONCURRENCY,
        "shape": bp.PROCESSOR_SHAPE,
        "query_joiner": bp.QUERY_JOINER,
        "max_batch_size": bp.PROCESSOR_BATCH_SIZE,
        "temp_dir": str(TEMP_DIR),
        "ttl_minutes": TTL_SECONDS / 60,
        "cleanup_interval": SWEEP_INTERVAL,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
    }


# --------------------------------------------------------------------------
# Background sweeper + startup/shutdown
# --------------------------------------------------------------------------


def _sweep_once() -> int:
    """Delete temp files older than the TTL (never touching running jobs)."""
    if not TEMP_DIR.exists():
        return 0
    with _jobs_lock:
        active = {jid for jid, e in _jobs.items() if not e.get("finished")}
    removed = 0
    now = time.time()
    for path in sorted(TEMP_DIR.rglob("*"),
                       key=lambda p: p.stat().st_mtime if p.exists() else 0,
                       reverse=True):
        try:
            if path.is_dir() and path.parent == JOBS_DIR and path.name in active:
                continue  # a job is running on this directory right now
            if path.is_file() and now - path.stat().st_mtime > TTL_SECONDS:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    # Drafts whose files are gone can no longer be started -> drop them.
    with _db() as conn:
        stale = conn.execute("SELECT id, input_path FROM jobs WHERE status='draft'").fetchall()
        for row in stale:
            if not Path(row["input_path"]).exists():
                conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
    if removed:
        log.info("temp cleanup: removed %d file(s) older than %.0f min from %s",
                 removed, TTL_SECONDS / 60, TEMP_DIR)
    return removed


def _sweep_loop() -> None:
    while True:
        try:
            _sweep_once()
        except Exception:  # noqa: BLE001 - never let the sweeper die
            log.exception("temp cleanup sweep failed")
        time.sleep(max(30.0, SWEEP_INTERVAL))


def _seed_users() -> None:
    """Create the bootstrap admin (+ optional UI_USERS) when the DB is empty."""
    with _db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if count == 0:
        username = os.getenv("UI_USERNAME", "admin").strip() or "admin"
        password = os.getenv("UI_PASSWORD", "").strip()
        if not password:
            password = secrets.token_urlsafe(12)
            log.warning("UI_PASSWORD not set - bootstrap login: %s / %s",
                        username, password)
        with _db() as conn:
            conn.execute("INSERT INTO users(username, password_hash, created_at) "
                         "VALUES(?,?,?)", (username, hash_password(password), _now_iso()))
        log.info("created UI user '%s'", username)

    extra = os.getenv("UI_USERS", "").strip()
    if extra:
        for pair in extra.split(","):
            if ":" not in pair:
                continue
            name, secret = pair.split(":", 1)
            name, secret = name.strip(), secret.strip()
            if not name or not secret:
                continue
            with _db() as conn:
                conn.execute(
                    "INSERT INTO users(username, password_hash, created_at) "
                    "VALUES(?,?,?) ON CONFLICT(username) DO NOTHING",
                    (name, hash_password(secret), _now_iso()))


def startup() -> None:
    """Idempotent init: config, schema, users, secret, stale jobs, sweeper."""
    global DB_PATH, TEMP_DIR, JOBS_DIR, TTL_SECONDS, SWEEP_INTERVAL
    global JWT_SECRET, JWT_EXPIRE_SECONDS, MAX_UPLOAD_BYTES, _sweeper_started

    DB_PATH = Path(os.getenv("UI_DB_PATH") or (Path.cwd() / "data" / "ui.db"))
    TEMP_DIR = Path(os.getenv("TEMP_DIR") or (Path(os.getenv("TMPDIR", "/tmp"))
                                              / "composio_bridge"))
    JOBS_DIR = TEMP_DIR / "jobs"
    TTL_SECONDS = float(os.getenv("TEMP_FILE_TTL_MINUTES", "60")) * 60
    SWEEP_INTERVAL = float(os.getenv("TEMP_CLEANUP_INTERVAL_SECONDS", "300"))
    JWT_EXPIRE_SECONDS = int(os.getenv("UI_JWT_EXPIRE_MINUTES", "720")) * 60
    MAX_UPLOAD_BYTES = int(os.getenv("UI_MAX_UPLOAD_MB", "200")) * 1024 * 1024
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

    _init_schema()

    secret = os.getenv("UI_JWT_SECRET", "").strip()
    if not secret:
        secret = _setting_get("jwt_secret") or ""
        if not secret:
            secret = secrets.token_urlsafe(32)
            _setting_set("jwt_secret", secret)
    JWT_SECRET = secret

    _seed_users()

    # Jobs left 'running' by a previous process were killed with it.
    with _db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='error', error='Processing was interrupted by a "
            "server restart', finished_at=? WHERE status IN ('queued','running')",
            (_now_iso(),))
        if cur.rowcount:
            log.warning("marked %d interrupted job(s) as error", cur.rowcount)

    _login_fails.clear()
    try:
        _sweep_once()
    except Exception:  # noqa: BLE001
        log.exception("initial temp sweep failed")
    if not _sweeper_started:
        _sweeper_started = True
        threading.Thread(target=_sweep_loop, daemon=True, name="temp-cleanup").start()
    log.info("UI ready -> /home (login: /login) | db=%s | temp=%s (ttl %.0f min)",
             DB_PATH, TEMP_DIR, TTL_SECONDS / 60)


def shutdown() -> None:
    """Mark in-flight jobs so history never shows a phantom 'running' state."""
    with _jobs_lock:
        running = [jid for jid, e in _jobs.items() if not e.get("finished")]
    if running:
        with _db() as conn:
            conn.execute(
                "UPDATE jobs SET status='error', error='Processing was interrupted by "
                "a server restart', finished_at=? WHERE id IN (%s) "
                "AND status IN ('queued','running')"
                % ",".join("?" * len(running)),
                (_now_iso(), *running))
