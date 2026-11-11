#!/usr/bin/env python3
"""
Streamlit UI for the Composio Google Maps batch processor
=========================================================

Flow:
    1. user uploads an Excel/CSV file (stored in the server's temp dir)
    2. UI auto-detects the sheet + input columns (both overridable) and lets
       the user pick a row range to process
    3. "Start processing" invokes batch_processor.process_file() in a worker
       thread - it calls POST /v1/maps/search/bulk and writes results back
       into each row
    4. live progress: progress bar, counters, per-row preview, Stop button
    5. the processed file is offered as a download; the copy on the server is
       erased automatically after TEMP_FILE_TTL_MINUTES (env-configurable)

Run:
    uv run --extra ui streamlit run streamlit_app.py      # or: ./bridge.sh ui
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import streamlit as st

import batch_processor as bp

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("maps-ui")

# --------------------------------------------------------------------------
# Configuration (env-driven)
# --------------------------------------------------------------------------

BRIDGE_URL: str = os.getenv("BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
TEMP_DIR: Path = Path(os.getenv("TEMP_DIR") or (Path(tempfile.gettempdir()) / "composio_bridge"))
TEMP_FILE_TTL_MINUTES: float = float(os.getenv("TEMP_FILE_TTL_MINUTES", "60"))
TEMP_CLEANUP_INTERVAL_SECONDS: float = float(os.getenv("TEMP_CLEANUP_INTERVAL_SECONDS", "300"))
JOBS_DIR: Path = TEMP_DIR / "jobs"

st.set_page_config(page_title="Google Maps Batch Search", page_icon="🗺️", layout="wide")


# --------------------------------------------------------------------------
# Temp file hygiene: erase uploaded/processed files after TTL
# --------------------------------------------------------------------------


def cleanup_temp_files(root: Path, ttl_seconds: float) -> int:
    """Delete files older than ttl_seconds (by mtime) and empty job dirs."""
    if not root.exists():
        return 0
    removed = 0
    now = time.time()
    for path in sorted(root.rglob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        try:
            if path.is_file() and now - path.stat().st_mtime > ttl_seconds:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:  # file vanished / in use - try again next sweep
            continue
    if removed:
        log.info("temp cleanup: removed %d file(s) older than %.0f min from %s",
                 removed, ttl_seconds / 60, root)
    return removed


def _cleanup_loop(root: Path, ttl_seconds: float, interval: float) -> None:
    while True:
        try:
            cleanup_temp_files(root, ttl_seconds)
        except Exception:  # never let the sweeper die
            log.exception("temp cleanup sweep failed")
        time.sleep(max(30.0, interval))


@st.cache_resource
def _runtime() -> dict[str, Any]:
    """Process-wide singleton: temp dir + cleanup sweeper (started once)."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    ttl = TEMP_FILE_TTL_MINUTES * 60
    cleanup_temp_files(TEMP_DIR, ttl)  # sweep immediately on boot
    threading.Thread(target=_cleanup_loop, args=(TEMP_DIR, ttl, TEMP_CLEANUP_INTERVAL_SECONDS),
                     daemon=True, name="temp-cleanup").start()
    return {"root": str(TEMP_DIR), "ttl_minutes": TEMP_FILE_TTL_MINUTES,
            "cleanup_interval": TEMP_CLEANUP_INTERVAL_SECONDS, "started_at": time.time()}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def _bridge_status(bridge_url: str) -> dict[str, Any]:
    return bp.check_bridge(bridge_url)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "upload.xlsx"


def _init_job(uploaded: Any) -> dict[str, Any]:
    """Persist the upload into the temp dir and pre-compute sheet info."""
    job_id = uuid.uuid4().hex[:12]
    suffix = Path(_safe_name(uploaded.name)).suffix.lower()
    if suffix not in bp.EXCEL_SUFFIXES | bp.CSV_SUFFIXES:
        suffix = ".xlsx"
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{suffix}"
    input_path.write_bytes(uploaded.getvalue())

    sheets = bp.read_workbook(input_path)
    info = {
        "id": job_id,
        "job_dir": job_dir,
        "input_path": input_path,
        "output_path": job_dir / f"processed{suffix}",
        "original_name": Path(uploaded.name).name,
        "sheets": list(sheets),
        "sheet": bp.pick_sheet(sheets),
        "sheet_columns": {name: [str(c) for c in df.columns] for name, df in sheets.items()},
        "sheet_rows": {name: len(df) for name, df in sheets.items()},
        "sheet_detected": {name: bp.detect_columns(df) for name, df in sheets.items()},
        "previews": {name: df.head(20) for name, df in sheets.items()},
        "status": "configured",
        "summary": None,
        "error": None,
        "reporter": None,
        "created_at": time.time(),
    }
    return info


def _reset_sheet_widgets(job: dict[str, Any]) -> None:
    """Drop mapping/range widget state when the selected sheet changes."""
    jid = job["id"]
    for key in list(st.session_state):
        if (key.startswith(f"{jid}_map_") or key.startswith(f"{jid}_qcol_")
                or key in (f"{jid}_start_row", f"{jid}_end_row")):
            del st.session_state[key]


def _worker(job: dict[str, Any], reporter: bp.ProgressReporter, kwargs: dict[str, Any]) -> None:
    try:
        summary = bp.process_file(**kwargs, progress_cb=reporter.publish,
                                  stop_cb=reporter.stop_requested)
        reporter.finish(summary, stopped=bool(summary.get("stopped")))
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        log.exception("job %s failed", job.get("id"))
        reporter.fail(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------


def render() -> None:
    runtime = _runtime()
    st.title("🗺️ Google Maps Batch Search")
    st.caption("Upload a spreadsheet → pick the rows to process → each row is searched via "
               "`POST /v1/maps/search/bulk` and the results are written back into the same row.")

    # ---- bridge status (sidebar) ----------------------------------------
    with st.sidebar:
        st.header("Bridge")
        bridge_url = st.text_input("Bridge URL", value=BRIDGE_URL, key="bridge_url")
        try:
            health = _bridge_status(bridge_url)
            st.success(f"connected · {health.get('api_keys_loaded', 0)} key(s)")
            rl = health.get("rate_limit_per_key_per_minute")
            st.caption("rate limit: " + (f"{rl} calls/min per key" if rl else "unlimited")
                       + f" · window {health.get('rate_limit_window_seconds', '?')}s"
                       + f" · max batch {health.get('max_batch_size', '?')}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Bridge unreachable: {exc}")
            health = None

        st.header("Temp files")
        st.caption(f"Stored in `{runtime['root']}` and erased after "
                   f"**{runtime['ttl_minutes']:g} min** "
                   f"(`TEMP_FILE_TTL_MINUTES`, sweep every "
                   f"{runtime['cleanup_interval']:g}s).")

    # ---- upload ----------------------------------------------------------
    uploaded = st.file_uploader("Upload Excel/CSV file",
                                type=["xlsx", "xls", "xlsm", "csv", "tsv"])
    if uploaded is None:
        st.info("Upload a file to begin. Expected: a header row + one search query per row "
                "(column auto-detected from names like *q*, *query*, *search term*, ...).")
        return

    file_key = hashlib.md5(f"{uploaded.name}:{uploaded.size}".encode()).hexdigest()
    job: Optional[dict[str, Any]] = st.session_state.get("job")
    running = bool(job and job.get("status") == "running")

    if job is None or job.get("file_key") != file_key:
        if running:
            st.warning("A job is still running - wait for it to finish (or stop it) before "
                       "uploading another file.")
            return
        job = _init_job(uploaded)
        job["file_key"] = file_key
        st.session_state["job"] = job
        st.session_state.pop("result", None)

    jid = job["id"]
    nrows = job["sheet_rows"][job["sheet"]]

    # ---- configuration: sheet, columns, row range ------------------------
    st.subheader("1 · Configure")
    col_cfg_a, col_cfg_b = st.columns([1, 2])

    with col_cfg_a:
        sheet = st.selectbox("Sheet", job["sheets"],
                             index=job["sheets"].index(job["sheet"]), key=f"{jid}_sheet")
        if sheet != job["sheet"]:
            _reset_sheet_widgets(job)
            job["sheet"] = sheet
            job["summary"] = None
            nrows = job["sheet_rows"][sheet]
            st.rerun()

        st.markdown(f"**{nrows}** data row(s) · {len(job['sheet_columns'][sheet])} column(s)")

        detected = job["sheet_detected"][sheet]
        columns = job["sheet_columns"][sheet]
        options = ["(none)"] + columns

        def _sel(label: str, field_name: str, help_text: str) -> Optional[str]:
            current = detected.get(field_name)
            default_idx = options.index(current) if current in options else 0
            value = st.selectbox(label, options, index=default_idx,
                                 key=f"{jid}_map_{field_name}", help=help_text)
            return None if value == "(none)" else value

        st.caption("Input columns (auto-detected - override as needed)")

        # Query: tick one or more columns - their cell values are appended
        # (joined with QUERY_JOINER, default a space) into a single query/row.
        st.markdown("Query column(s) *")
        st.caption("Tick the boxes of the columns that build the query - selected "
                   "values are appended together (e.g. *City* + *Item* → \"Paris coffee\").")
        q_default = detected.get("query")
        q_defaults = q_default if isinstance(q_default, list) else ([q_default] if q_default else [])
        query_cols: list[str] = []
        grid = st.columns(2)
        for i, col_name in enumerate(columns):
            with grid[i % 2]:
                if st.checkbox(str(col_name), value=col_name in q_defaults,
                               key=f"{jid}_qcol_{i}"):
                    query_cols.append(col_name)
        if not query_cols:
            st.warning("Tick at least one query column to enable processing.")

        ll_col = _sel("Location column ('@lat,lng,zoom')", "ll", "Optional - locates the search")
        lat_col = _sel("Latitude column", "lat", "Optional - alternative to the ll column")
        lng_col = _sel("Longitude column", "lng", "Optional - needs the latitude column")
        zoom_col = _sel("Zoom column", "zoom", "Optional - default from GOOGLE_MAPS_DEFAULT_ZOOM")
        start_col = _sel("Pagination start column", "start", "Optional - 1, 15, 30, ...")

    with col_cfg_b:
        st.markdown("**Row range to process**")
        range_a, range_b = st.columns(2)
        with range_a:
            start_row = st.number_input("From row", min_value=1, max_value=max(nrows, 1),
                                        value=st.session_state.get(f"{jid}_start_row", 1),
                                        step=1, key=f"{jid}_start_row",
                                        help="1 = first data row (below the header)")
        with range_b:
            end_row = st.number_input("To row", min_value=1, max_value=max(nrows, 1),
                                      value=st.session_state.get(f"{jid}_end_row", max(nrows, 1)),
                                      step=1, key=f"{jid}_end_row", help="inclusive")
        start_row, end_row = int(start_row), int(end_row)
        if start_row > end_row:
            st.error("`From row` must be ≤ `To row`")
            return
        selected = end_row - start_row + 1
        st.caption(f"Processing **{selected} of {nrows}** rows "
                   f"(rows outside the range are left untouched)")

        st.markdown("**Response mode**")
        mode = st.radio("Mode", ["full", "slim"], index=0, horizontal=True,
                        key=f"{jid}_shape",
                        help="full = every field returned by the endpoint is written back "
                             "as its own column (recommended); slim = compact place fields "
                             "only (smaller payload).")

        with st.expander("Advanced"):
            batch_size = st.number_input("Rows per bulk call", 1, 100,
                                         value=min(bp.PROCESSOR_BATCH_SIZE, 100), step=1,
                                         help="POST /v1/maps/search/bulk items per request")
            concurrency = st.number_input("Parallel searches per call", 1, 16,
                                          value=min(bp.PROCESSOR_CONCURRENCY, 16), step=1)

        st.markdown("**Sheet preview** (first 20 rows)")
        st.dataframe(job["previews"][job["sheet"]], use_container_width=True, height=260)

    # ---- start -----------------------------------------------------------
    st.subheader("2 · Process")
    if job["status"] == "configured" and st.button(
            "▶ Start processing", type="primary", key=f"{jid}_start",
            disabled=not query_cols,
            help="Tick at least one query column first." if not query_cols else None):
        overrides = {"query": query_cols or None, "ll": ll_col, "lat": lat_col, "lng": lng_col,
                     "zoom": zoom_col, "start": start_col}
        reporter = bp.ProgressReporter(total=selected)
        job["reporter"] = reporter
        job["status"] = "running"
        job["summary"] = None
        job["error"] = None
        kwargs = dict(input_path=job["input_path"], output_path=job["output_path"],
                      sheet=job["sheet"], column_overrides=overrides,
                      start_row=start_row, end_row=end_row, shape=mode,
                      bridge_url=bridge_url,
                      batch_size=int(batch_size), concurrency=int(concurrency))
        thread = threading.Thread(target=_worker, args=(job, reporter, kwargs),
                                  daemon=True, name=f"job-{job['id']}")
        job["thread"] = thread
        thread.start()
        st.rerun()

    # ---- live progress ---------------------------------------------------
    if job["status"] == "running":
        reporter: bp.ProgressReporter = job["reporter"]
        if st.button("⏹ Stop", key=f"{jid}_stop"):
            reporter.request_stop()
            st.info("Stop requested - finishing the current batch…")

        bar = st.progress(0.0, text="starting…")
        status_box = st.empty()
        preview_box = st.empty()

        while True:
            info = reporter.snapshot()
            pct = (info.done / info.total) if info.total else 0.0
            bar.progress(min(1.0, pct),
                         text=f"{info.done}/{info.total} rows · ok={info.succeeded} "
                              f"failed={info.failed} · {info.message}")
            status_box.caption(f"elapsed {info.elapsed:.1f}s")
            if info.preview:
                preview_box.dataframe(pd.DataFrame(info.preview), use_container_width=True,
                                      height=240)
            if info.finished:
                break
            time.sleep(0.4)

        if info.error:
            job["status"] = "error"
            job["error"] = info.error
            st.rerun()
        job["status"] = "stopped" if info.stopped else "done"
        job["summary"] = info.summary or {}

    # ---- error state -----------------------------------------------------
    if job["status"] == "error":
        st.subheader("3 · Result")
        st.error(f"Processing failed: {job.get('error')}")
        if st.button("↻ Try again", key=f"{jid}_retry"):
            job["status"] = "configured"
            job["error"] = None
            st.rerun()
        return

    if job["status"] not in ("done", "stopped"):
        return  # still configured / running (progress renders above)

    # ---- results ---------------------------------------------------------
    summary = job.get("summary") or {}
    st.subheader("3 · Result")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Processed", f"{summary.get('rows_processed', 0)}/{summary.get('rows_in_range', 0)}")
    m2.metric("Succeeded", summary.get("rows_succeeded", 0))
    m3.metric("Failed", summary.get("rows_failed", 0))
    m4.metric("Out of range", summary.get("rows_out_of_range", 0))
    m5.metric("Time", f"{summary.get('elapsed_seconds', 0)}s")
    if job["status"] == "stopped":
        st.warning("Stopped early - rows after the stop point are marked `not_processed`.")

    output_path = Path(summary.get("output") or job["output_path"])
    if not output_path.exists():
        st.error("Output file was not produced.")
        return

    st.download_button("⬇️ Download processed file",
                       data=output_path.read_bytes(),
                       file_name=f"{Path(job['original_name']).stem}_processed{output_path.suffix}",
                       mime=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                             if output_path.suffix.lower() in bp.EXCEL_SUFFIXES else "text/csv"),
                       type="primary", key=f"{jid}_download")

    expires = datetime.fromtimestamp(output_path.stat().st_mtime + TEMP_FILE_TTL_MINUTES * 60)
    st.caption(f"Server copy: `{output_path}` · auto-deleted after "
               f"**{TEMP_FILE_TTL_MINUTES:g} min** (≈ {expires.strftime('%H:%M:%S')}) - "
               "set `TEMP_FILE_TTL_MINUTES` in `.env` to change.")

    # Full output table: every column stored in the file (input columns +
    # all maps_* fields, including the dynamic ones of shape=full).
    st.markdown("**Output - all fields as stored in the file**")
    try:
        out_sheets = bp.read_workbook(output_path)
        if job["sheet"] in out_sheets:
            out_df = out_sheets[job["sheet"]]
        else:
            out_df = next(iter(out_sheets.values()))
        st.dataframe(out_df, use_container_width=True, height=420)
        st.caption(f"Sheet `{job['sheet']}` · **{out_df.shape[0]} row(s) × "
                   f"{out_df.shape[1]} column(s)** - identical to the downloaded file.")
    except Exception as exc:  # noqa: BLE001 - preview is best effort
        st.caption(f"Output preview unavailable: {exc}")

    with st.expander("Processing summary"):
        st.json(summary)

    if st.button("↻ Process another range of this file", key=f"{jid}_again"):
        job["status"] = "configured"
        job["summary"] = None
        st.rerun()


render()
