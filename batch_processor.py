#!/usr/bin/env python3
"""
Batch Google Maps search over a spreadsheet
============================================

Reads an Excel/CSV file, runs every row's query through the bridge's bulk
endpoint (``POST /v1/maps/search/bulk``) and writes the results back into the
*same row* as new ``maps_*`` columns.

Column mapping is auto-detected from common header names (q/query/search,
ll/lat/lng, start, ...) and can be overridden explicitly.

Only a *range* of rows can be processed: ``start_row`` / ``end_row`` are
1-based and inclusive (row 1 = first data row under the header). Rows outside
the range are left untouched (``maps_status = not_processed``).

Standalone CLI
--------------
    uv run maps-batch input.xlsx -o output.xlsx
    uv run maps-batch input.xlsx --start-row 11 --end-row 60 --query-column q

As a library (built-in /home page)
--------------------------------
    from batch_processor import process_file, ProgressReporter

    reporter = ProgressReporter(total=100)
    summary = process_file(src, dst, start_row=1, end_row=50,
                           progress_cb=reporter.publish, stop_cb=reporter.stop_requested)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("maps-batch")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BRIDGE_URL: str = os.getenv("BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
PROCESSOR_BATCH_SIZE: int = int(os.getenv("PROCESSOR_BATCH_SIZE", "50"))
PROCESSOR_CONCURRENCY: int = int(os.getenv("PROCESSOR_CONCURRENCY", "8"))
PROCESSOR_MAX_RETRIES: int = int(os.getenv("PROCESSOR_MAX_RETRIES", "4"))
PROCESSOR_TIMEOUT: float = float(os.getenv("PROCESSOR_TIMEOUT", "120"))
PROCESSOR_BACKOFF_BASE: float = float(os.getenv("PROCESSOR_BACKOFF_BASE", "1.0"))
# Response mode for every search: "full" = the complete payload from the
# endpoint (EVERY field becomes a column), "slim" = compact places list.
PROCESSOR_SHAPE: str = (os.getenv("PROCESSOR_SHAPE") or "full").lower()
if PROCESSOR_SHAPE not in ("slim", "full"):
    PROCESSOR_SHAPE = "full"
# Joiner used when several columns are ticked for the query field.
QUERY_JOINER: str = os.getenv("QUERY_JOINER", " ")
BEARER_TOKEN: str = (os.getenv("BRIDGE_API_TOKEN") or "").strip()
DEFAULT_ZOOM: int = int(os.getenv("GOOGLE_MAPS_DEFAULT_ZOOM", "13"))

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"}
CSV_SUFFIXES = {".csv", ".tsv"}

LL_PATTERN = re.compile(r"^@-?\d+(\.\d+)?,-?\d+(\.\d+)?(,\d+(\.\d+)?z)?$")

# Columns written back into the sheet (always prefixed to never clobber input).
OUTPUT_COLUMNS: list[str] = [
    "maps_status",
    "maps_count",
    "maps_title",
    "maps_address",
    "maps_type",
    "maps_types",
    "maps_rating",
    "maps_reviews",
    "maps_price",
    "maps_phone",
    "maps_website",
    "maps_hours",
    "maps_open_state",
    "maps_description",
    "maps_place_id",
    "maps_data_id",
    "maps_latitude",
    "maps_longitude",
    "maps_thumbnail",
    "maps_position",
    "maps_all_places_json",
    "maps_latency_ms",
    "maps_log_id",
    "maps_error",
]

STATUS_OK = "ok"
STATUS_NO_RESULTS = "no_results"
STATUS_FAILED = "failed"
STATUS_INVALID = "invalid_input"
STATUS_SKIPPED = "not_processed"

# Excel hard limit for a single cell (32767 chars) - keep a small margin.
CELL_LIMIT = 32000

# Fields the bridge keeps when shape=slim (mirrors composio_bridge.slim_place).
SLIM_PLACE_KEYS = frozenset({
    "title", "address", "type", "types", "rating", "reviews", "price", "phone",
    "website", "hours", "open_state", "description", "place_id", "data_id",
    "gps_coordinates", "latitude", "longitude", "thumbnail", "position",
})

# Place field -> curated output column (everything else becomes maps_<field>).
PLACE_FIELD_MAP: dict[str, str] = {
    "title": "maps_title",
    "address": "maps_address",
    "type": "maps_type",
    "types": "maps_types",
    "rating": "maps_rating",
    "reviews": "maps_reviews",
    "price": "maps_price",
    "phone": "maps_phone",
    "website": "maps_website",
    "hours": "maps_hours",
    "open_state": "maps_open_state",
    "description": "maps_description",
    "place_id": "maps_place_id",
    "data_id": "maps_data_id",
    "thumbnail": "maps_thumbnail",
    "position": "maps_position",
}


class BulkRequestError(RuntimeError):
    """The bridge could not be reached / rejected the batch (non-retryable)."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------
# Column mapping
# --------------------------------------------------------------------------


def _norm_col(col: Any) -> str:
    """'Search Term' -> 'search_term', 'S.NO' -> 's_no' (lowercase, alnum runs -> _)."""
    out: list[str] = []
    pending_us = False
    for ch in str(col).strip().lower():
        if ch.isalnum():
            if pending_us and out:
                out.append("_")
            out.append(ch)
            pending_us = False
        else:
            pending_us = True
    return "".join(out).strip("_")


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "query": ("q", "query", "queries", "search", "search_query", "search_term", "search_terms",
              "term", "keyword", "keywords", "place", "place_query", "what", "looking_for"),
    "ll": ("ll", "latlng", "lat_lng", "location", "geo", "coords", "coordinates", "gps"),
    "lat": ("lat", "latitude"),
    "lng": ("lng", "lon", "long", "longitude"),
    "zoom": ("zoom", "zoom_level"),
    "start": ("start", "page", "page_number", "offset", "pagination", "result_start"),
    "user_id": ("user_id", "userid", "composio_user_id", "composio_user"),
    "id": ("id", "row_id", "rowid", "uid", "sno", "s_no", "sr_no", "record_id"),
}


@dataclass
class ColumnMapping:
    """Which spreadsheet column feeds which bridge parameter (None = unused).

    `query` may be a list of columns - their cell values are then appended
    (joined with QUERY_JOINER) into one search query per row.
    """

    query: Optional[str | list[str]] = None
    ll: Optional[str] = None
    lat: Optional[str] = None
    lng: Optional[str] = None
    zoom: Optional[str] = None
    start: Optional[str] = None
    user_id: Optional[str] = None
    id: Optional[str] = None

    FIELDS = ("query", "ll", "lat", "lng", "zoom", "start", "user_id", "id")

    def to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.FIELDS}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ColumnMapping":
        return cls(**{f: data.get(f) for f in cls.FIELDS})


def detect_columns(df: pd.DataFrame) -> dict[str, Optional[str]]:
    """Auto-detect the input columns from their header names (best effort)."""
    found: dict[str, Optional[str]] = {f: None for f in ColumnMapping.FIELDS}
    for col in df.columns:
        norm = _norm_col(col)
        for field_name, aliases in FIELD_ALIASES.items():
            if found[field_name] is None and norm in aliases:
                found[field_name] = col
                break
    return found


def merge_mapping(detected: dict[str, Optional[str]],
                  overrides: Optional[dict[str, Any]] = None,
                  columns: Optional[list[Any]] = None) -> ColumnMapping:
    """Apply UI/CLI overrides on top of detection.

    - An override value of None / "" / "(none)" / [] explicitly disables that field;
      keys absent from `overrides` keep the detected value.
    - `query` accepts a single column, a list of columns, or a comma-separated
      string ("City,Item") which is split into a list (unless the whole string
      is itself an existing column name).
    """
    merged: dict[str, Any] = dict(detected)
    for key, value in (overrides or {}).items():
        if key not in ColumnMapping.FIELDS:
            continue
        if isinstance(value, (list, tuple)):
            value = [str(v) for v in value if str(v).strip()] or None
        elif (isinstance(value, str) and key == "query" and "," in value
                and not (columns and value in columns)):
            value = [p.strip() for p in value.split(",") if p.strip()]
        if value in (None, "", "(none)"):
            value = None
        merged[key] = value
    return ColumnMapping.from_dict(merged)


# --------------------------------------------------------------------------
# Row -> bridge arguments
# --------------------------------------------------------------------------


def _text(value: Any) -> Optional[str]:
    """Cell -> stripped str, or None for empty/NaN/NA."""
    if value is None or value is pd.NA:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return s or None


def _number(value: Any) -> Optional[float]:
    s = _text(value)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalise_ll(ll: Optional[str], lat: Optional[float], lng: Optional[float],
                 zoom: Optional[float]) -> Optional[str]:
    """Accept '@lat,lng,zoom', 'lat,lng' or separate lat/lng -> '@lat,lng,zoomz'."""
    if lat is not None and lng is not None:
        z = zoom if zoom is not None else DEFAULT_ZOOM
        return f"@{lat},{lng},{z:g}z"
    if not ll:
        return None
    value = ll.strip()
    if not value.startswith("@"):
        value = "@" + value
    parts = value[1:].split(",")
    if len(parts) == 2:  # caller gave '@lat,lng' -> append default zoom
        z = zoom if zoom is not None else DEFAULT_ZOOM
        value = f"{value},{z:g}z"
    return value


def compose_query(record: dict[str, Any], query: Optional[str | list[str]]) -> Optional[str]:
    """Build the search query for one row.

    A single column -> its value. Multiple columns -> the non-empty cell values
    appended together with QUERY_JOINER (default a space), e.g.
    City="Paris" + Item="coffee" -> "Paris coffee".
    """
    if not query:
        return None
    cols = query if isinstance(query, (list, tuple)) else [query]
    parts = [_text(record.get(col)) for col in cols]
    return QUERY_JOINER.join(p for p in parts if p) or None


def build_arguments(record: dict[str, Any], mapping: ColumnMapping,
                    default_zoom: int = DEFAULT_ZOOM) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Turn one spreadsheet row into bulk-request arguments.

    Returns (arguments, None) on success or (None, reason) when the row's input
    is invalid - such rows are marked per-row instead of failing the batch.
    """
    q = compose_query(record, mapping.query)
    if not q:
        return None, "no query value (query column(s) missing or empty)"
    if len(q) > 500:
        return None, f"query is {len(q)} chars (max 500)"

    args: dict[str, Any] = {"q": q}

    zoom = _number(record.get(mapping.zoom)) if mapping.zoom else None
    ll = _text(record.get(mapping.ll)) if mapping.ll else None
    lat = _number(record.get(mapping.lat)) if mapping.lat else None
    lng = _number(record.get(mapping.lng)) if mapping.lng else None
    if ll or (lat is not None and lng is not None):
        composed = normalise_ll(ll, lat, lng, zoom if zoom is not None else float(default_zoom))
        if composed and not LL_PATTERN.match(composed):
            # A bad ll here would 422 the whole bulk batch - reject just this row.
            return None, f"invalid location value: {ll or f'{lat},{lng}'} (expected '@lat,lng,zoom' or lat/lng columns)"
        if composed:
            args["ll"] = composed

    if mapping.start:
        start = _number(record.get(mapping.start))
        if start is not None and 1 <= start <= 1000:
            args["start"] = int(start)

    return args, None


# --------------------------------------------------------------------------
# Progress reporting (shared between worker thread and UI)
# --------------------------------------------------------------------------


@dataclass
class ProgressInfo:
    """Snapshot of a running job - passed to progress_cb after every batch."""

    total: int = 0
    done: int = 0
    succeeded: int = 0
    failed: int = 0
    message: str = ""
    elapsed: float = 0.0
    finished: bool = False
    stopped: bool = False
    error: Optional[str] = None
    summary: Optional[dict[str, Any]] = None
    preview: list[dict[str, Any]] = field(default_factory=list)


class ProgressReporter:
    """Thread-safe progress bridge between the worker thread and the UI."""

    def __init__(self, total: int = 0) -> None:
        self._lock = threading.Lock()
        self._info = ProgressInfo(total=total)
        self._stop = threading.Event()

    # -- worker side --------------------------------------------------------
    def publish(self, info: ProgressInfo) -> None:
        with self._lock:
            self._info = info

    def finish(self, summary: dict[str, Any], stopped: bool = False) -> None:
        with self._lock:
            self._info.finished = True
            self._info.stopped = stopped
            self._info.summary = summary
            self._info.message = "stopped by user" if stopped else "done"

    def fail(self, error: str) -> None:
        with self._lock:
            self._info.finished = True
            self._info.error = error
            self._info.message = f"error: {error}"

    def request_stop(self) -> None:
        self._stop.set()

    def stop_requested(self) -> bool:
        return self._stop.is_set()

    # -- reader side --------------------------------------------------------
    def snapshot(self) -> ProgressInfo:
        with self._lock:
            info = self._info
            return ProgressInfo(total=info.total, done=info.done, succeeded=info.succeeded,
                                failed=info.failed, message=info.message, elapsed=info.elapsed,
                                finished=info.finished, stopped=info.stopped, error=info.error,
                                summary=info.summary, preview=list(info.preview))


# --------------------------------------------------------------------------
# Bridge call
# --------------------------------------------------------------------------


def _backoff_wait(attempt: int, retry_after: Optional[str] = None) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return min(30.0, PROCESSOR_BACKOFF_BASE * (2 ** attempt))


def post_bulk(payload: dict[str, Any], *, bridge_url: str = BRIDGE_URL,
              timeout: float = PROCESSOR_TIMEOUT, max_retries: int = PROCESSOR_MAX_RETRIES,
              bearer: Optional[str] = BEARER_TOKEN,
              shape: str = PROCESSOR_SHAPE) -> dict[str, Any]:
    """POST /v1/maps/search/bulk with retry/backoff on 429, 5xx and transport errors.

    shape="full" asks the bridge for the complete endpoint payload (every field
    is then written back as a column); shape="slim" for the compact list.
    """
    if shape not in ("slim", "full"):
        shape = PROCESSOR_SHAPE
    url = f"{bridge_url.rstrip('/')}/v1/maps/search/bulk?shape={shape}"
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    last_error: Optional[str] = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                time.sleep(_backoff_wait(attempt))
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if attempt < max_retries:
                time.sleep(_backoff_wait(attempt, resp.headers.get("Retry-After")))
            continue

        if resp.status_code >= 400:
            raise BulkRequestError(
                f"bridge rejected the batch (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code)

        try:
            data = resp.json()
        except ValueError as exc:
            raise BulkRequestError(f"bridge returned a non-JSON body: {exc}") from None
        if not isinstance(data, dict):
            raise BulkRequestError("bridge returned an unexpected payload")
        return data

    raise BulkRequestError(f"bridge unreachable after {max_retries + 1} attempt(s): {last_error}")


def check_bridge(bridge_url: str = BRIDGE_URL, timeout: float = 5.0) -> dict[str, Any]:
    """GET /health - used by the UI to show bridge/key/rate-limit status."""
    resp = httpx.get(f"{bridge_url.rstrip('/')}/health", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------
# Result flattening (one row -> individual output fields)
# --------------------------------------------------------------------------


def _error_text(err: Any) -> str:
    if err is None:
        return "unknown error"
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        inner = err.get("error")
        if isinstance(inner, dict):
            code, msg = inner.get("code"), inner.get("message") or str(inner)
            return f"{code}: {msg}" if code else str(msg)
        return json.dumps(err, ensure_ascii=False)
    return str(err)


def _failed_output(message: str, status: str = STATUS_FAILED) -> dict[str, Any]:
    return {"maps_status": status, "maps_error": message}


def _clip(value: Any) -> Any:
    """Keep strings inside the Excel cell limit (32767 chars) so files stay openable."""
    if isinstance(value, str) and len(value) > CELL_LIMIT:
        return value[:CELL_LIMIT - 20] + "…[truncated]"
    return value


def _cell_json(value: Any) -> Any:
    """dict/list -> JSON string (clipped); str -> clipped; anything else untouched."""
    if isinstance(value, (dict, list, tuple)):
        return _clip(json.dumps(value, ensure_ascii=False, default=str))
    return _clip(value)


def _places_json(places: list[dict[str, Any]]) -> Optional[str]:
    """All places as JSON; falls back to slimmed places when too big for a cell."""
    if not places:
        return None
    dump = json.dumps(places, ensure_ascii=False, default=str)
    if len(dump) <= CELL_LIMIT:
        return dump
    slim = [{k: p[k] for k in SLIM_PLACE_KEYS if k in p} for p in places]
    dump = json.dumps(slim, ensure_ascii=False, default=str)
    return _clip(dump) if len(dump) > CELL_LIMIT else dump


def flatten_result(item: Optional[dict[str, Any]]) -> dict[str, Any]:
    """One bulk result slot -> individual `maps_*` fields for the row.

    Every field the endpoint returned for the top result becomes its own
    column: curated columns for the common ones (maps_title, maps_rating, ...)
    and dynamic `maps_<field>` columns for anything else (country,
    operating_hours, order_online, provider_id, ...). The complete list of
    places goes into maps_all_places_json.
    """
    if not item or not item.get("ok"):
        return _failed_output(_error_text(item.get("error") if item else None))

    data = item.get("data")
    search_info: Any = None
    if isinstance(data, dict):  # shape=full: raw payload {"results": {"local_results": [...]}}
        results = data.get("results") if isinstance(data.get("results"), dict) else {}
        places = results.get("local_results") or []
        search_info = results.get("search_information")
    elif isinstance(data, list):  # shape=slim: already a list of places
        places = data
    else:
        places = []
    places = [p for p in places if isinstance(p, dict)]
    first = places[0] if places else {}
    gps = first.get("gps_coordinates") or {}

    row: dict[str, Any] = {
        "maps_status": STATUS_OK if places else STATUS_NO_RESULTS,
        "maps_count": len(places),
        "maps_title": _clip(first.get("title")),
        "maps_address": _clip(first.get("address")),
        "maps_type": _clip(first.get("type")),
        "maps_types": _cell_json(first.get("types")) if first.get("types") else None,
        "maps_rating": first.get("rating"),
        "maps_reviews": first.get("reviews"),
        "maps_price": first.get("price"),
        "maps_phone": _clip(first.get("phone")),
        "maps_website": _clip(first.get("website")),
        "maps_hours": _cell_json(first.get("hours")) if first.get("hours") else None,
        "maps_open_state": _clip(first.get("open_state")),
        "maps_description": _clip(first.get("description")),
        "maps_place_id": _clip(first.get("place_id")),
        "maps_data_id": _clip(first.get("data_id")),
        "maps_latitude": gps.get("latitude", first.get("latitude")),
        "maps_longitude": gps.get("longitude", first.get("longitude")),
        "maps_thumbnail": _clip(first.get("thumbnail")),
        "maps_position": _cell_json(first.get("position")) if isinstance(first.get("position"), (dict, list)) else first.get("position"),
        "maps_all_places_json": _places_json(places),
        "maps_latency_ms": item.get("latency_ms"),
        "maps_log_id": item.get("log_id"),
        "maps_error": None,
    }
    if search_info:
        row["maps_search_information_json"] = _cell_json(search_info)

    # Dynamic columns: EVERY remaining field the endpoint returned for the top
    # result gets its own maps_<field> column (never overwriting curated ones).
    for key, value in first.items():
        col = PLACE_FIELD_MAP.get(key)
        if col is not None:
            continue  # already written through its curated column
        col = "maps_gps_coordinates" if key == "gps_coordinates" else f"maps_{key}"
        if col in OUTPUT_COLUMNS or col in row:
            continue  # never clobber a curated/status column
        row[col] = _cell_json(value)
    return row


def _preview_row(row_index: int, record: dict[str, Any], mapping: ColumnMapping,
                 flat: dict[str, Any]) -> dict[str, Any]:
    """Per-row view for the live progress table (1-based row number).

    Starts with `row`/`query` and then carries EVERY output field the endpoint
    returned for that row - i.e. exactly the fields that are stored in the file.
    """
    row: dict[str, Any] = {
        "row": row_index + 1,
        "query": compose_query(record, mapping.query),
    }
    row.update(flat)
    return row


def _range_bounds(total: int, start_row: Optional[int], end_row: Optional[int]) -> tuple[int, int]:
    """1-based inclusive row numbers -> 0-based half-open [lo, hi) bounds."""
    lo = max(1, int(start_row or 1)) - 1
    hi = min(total, int(end_row) if end_row is not None else total)
    return max(0, lo), max(0, hi)


# --------------------------------------------------------------------------
# Core processing
# --------------------------------------------------------------------------


def process_dataframe(df: pd.DataFrame, mapping: ColumnMapping, *,
                      bridge_url: str = BRIDGE_URL,
                      batch_size: int = PROCESSOR_BATCH_SIZE,
                      concurrency: int = PROCESSOR_CONCURRENCY,
                      timeout: float = PROCESSOR_TIMEOUT,
                      max_retries: int = PROCESSOR_MAX_RETRIES,
                      bearer: Optional[str] = BEARER_TOKEN,
                      default_zoom: int = DEFAULT_ZOOM,
                      shape: str = PROCESSOR_SHAPE,
                      start_row: Optional[int] = None,
                      end_row: Optional[int] = None,
                      progress_cb: Optional[Callable[[ProgressInfo], None]] = None,
                      stop_cb: Optional[Callable[[], bool]] = None,
                      post_bulk_fn: Optional[Callable[..., dict[str, Any]]] = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Search every (in-range) row via the bulk endpoint and write results back.

    Returns (output DataFrame, summary dict). Rows outside [start_row, end_row]
    (1-based, inclusive) are marked `not_processed` and never sent upstream.

    shape: "full" (default) = every field returned by the endpoint is written
    back (curated + dynamic columns); "slim" = the compact place fields.
    """
    if not mapping.query:
        raise ValueError("No query column configured")
    if shape not in ("slim", "full"):
        raise ValueError(f"Invalid shape '{shape}' - use 'slim' or 'full'")

    out = df.copy()
    for col in OUTPUT_COLUMNS:
        out[col] = None

    total_rows = len(out)
    lo, hi = _range_bounds(total_rows, start_row, end_row)
    if total_rows and lo >= hi:
        raise ValueError(f"Row range is empty: rows {start_row or 1}-{end_row or total_rows} of {total_rows}")

    targets = list(range(lo, hi))
    total = len(targets)
    records = out.to_dict(orient="records")
    results: dict[int, dict[str, Any]] = {}
    preview: deque[dict[str, Any]] = deque(maxlen=50)
    post = post_bulk_fn or post_bulk

    started = time.perf_counter()
    done = succeeded = failed = 0
    batches = 0
    stopped = False

    def publish(message: str) -> None:
        if progress_cb:
            progress_cb(ProgressInfo(
                total=total, done=done, succeeded=succeeded, failed=failed, message=message,
                elapsed=time.perf_counter() - started, preview=list(preview)))

    publish("starting")

    for chunk_start in range(0, total, max(1, int(batch_size))):
        if stop_cb and stop_cb():
            stopped = True
            break
        chunk = targets[chunk_start:chunk_start + max(1, int(batch_size))]

        requests: list[tuple[int, dict[str, Any]]] = []
        for gi in chunk:
            args, reason = build_arguments(records[gi], mapping, default_zoom)
            if reason:
                results[gi] = _failed_output(reason, status=STATUS_INVALID)
                failed += 1
                preview.append(_preview_row(gi, records[gi], mapping, results[gi]))
            else:
                requests.append((gi, {"id": str(gi), **args}))  # type: ignore[arg-type]

        if requests:
            payload = {
                "requests": [req for _, req in requests],
                "continue_on_error": True,
                "concurrency": max(1, min(int(concurrency), 16)),
            }
            data = post(payload, bridge_url=bridge_url, timeout=timeout,
                        max_retries=max_retries, bearer=bearer, shape=shape)
            batches += 1
            slots = list(data.get("results") or [])
            by_id = {str(s.get("id")): s for s in slots if isinstance(s, dict) and s.get("id") is not None}
            for pos, (gi, req) in enumerate(requests):
                item = by_id.get(str(req["id"]))
                if item is None and pos < len(slots):  # fall back to positional match
                    item = slots[pos]
                flat = flatten_result(item if isinstance(item, dict) else None)
                results[gi] = flat
                if flat["maps_status"] in (STATUS_OK, STATUS_NO_RESULTS):
                    succeeded += 1
                else:
                    failed += 1
                preview.append(_preview_row(gi, records[gi], mapping, flat))

        done = min(chunk_start + len(chunk), total)
        msg = f"rows {lo + done - len(chunk) + 1}-{lo + done} of {total_rows}"
        publish(msg)

    skipped = total - done
    elapsed = time.perf_counter() - started

    # Write every result back into its own row; untouched rows stay not_processed.
    # Dynamic columns (fields outside the curated set) are added for every
    # maps_* key any row produced - so ALL endpoint fields become columns.
    dynamic_cols: set[str] = set()
    for flat in results.values():
        for key in flat:
            if key.startswith("maps_") and key not in OUTPUT_COLUMNS:
                dynamic_cols.add(key)
    for col in sorted(dynamic_cols):
        out[col] = None
    all_cols = OUTPUT_COLUMNS + sorted(dynamic_cols)
    for gi in range(total_rows):
        flat = results.get(gi) or {"maps_status": STATUS_SKIPPED}
        for col in all_cols:
            out.at[out.index[gi], col] = flat.get(col)

    summary = {
        "rows_total": total_rows,
        "rows_in_range": total,
        "rows_processed": done,
        "rows_succeeded": succeeded,
        "rows_failed": failed,
        "rows_skipped": skipped,
        "rows_out_of_range": total_rows - total,
        "range": [lo + 1, hi],  # 1-based inclusive, as the user sees it
        "batches": batches,
        "shape": shape,
        "extra_columns": sorted(dynamic_cols),
        "stopped": stopped,
        "elapsed_seconds": round(elapsed, 2),
    }
    if progress_cb:
        progress_cb(ProgressInfo(total=total, done=done, succeeded=succeeded, failed=failed,
                                 elapsed=elapsed, finished=True, stopped=stopped,
                                 message="stopped" if stopped else "done",
                                 summary=summary, preview=list(preview)))
    return out, summary


# --------------------------------------------------------------------------
# File IO
# --------------------------------------------------------------------------


def read_workbook(path: str | Path) -> dict[str, pd.DataFrame]:
    """Read an Excel workbook (all sheets) or a CSV -> {sheet name: DataFrame}.

    Everything is read as text so phone numbers / leading zeros survive.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    suffix = p.suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        sheets = pd.read_excel(p, sheet_name=None, dtype=str)
        if not sheets:
            raise ValueError(f"No sheets found in {p.name}")
        return sheets
    if suffix in CSV_SUFFIXES:
        sep = "\t" if suffix == ".tsv" else ","
        return {p.stem: pd.read_csv(p, sep=sep, dtype=str)}
    raise ValueError(f"Unsupported file type '{suffix}' - use {sorted(EXCEL_SUFFIXES | CSV_SUFFIXES)}")


def pick_sheet(sheets: dict[str, pd.DataFrame]) -> str:
    """Prefer the first sheet whose headers yield a detected query column."""
    for name, df in sheets.items():
        if detect_columns(df).get("query"):
            return name
    return next(iter(sheets))


def write_workbook(path: str | Path, sheets: dict[str, pd.DataFrame]) -> None:
    """Write all sheets back (Excel for .xlsx/.xls, CSV for .csv/.tsv)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() in EXCEL_SUFFIXES:
        with pd.ExcelWriter(p, engine="openpyxl") as writer:
            for name, df in sheets.items():
                df.to_excel(writer, sheet_name=str(name)[:31], index=False)
    else:
        if len(sheets) != 1:
            raise ValueError("CSV output supports a single sheet only")
        next(iter(sheets.values())).to_csv(p, index=False)


def default_output_path(input_path: str | Path) -> Path:
    p = Path(input_path)
    return p.with_name(f"{p.stem}_processed{p.suffix or '.xlsx'}")


def process_file(input_path: str | Path, output_path: str | Path | None = None, *,
                 sheet: str | int | None = None,
                 column_overrides: Optional[dict[str, Any]] = None,
                 start_row: Optional[int] = None,
                 end_row: Optional[int] = None,
                 shape: str = PROCESSOR_SHAPE,
                 progress_cb: Optional[Callable[[ProgressInfo], None]] = None,
                 stop_cb: Optional[Callable[[], bool]] = None,
                 **process_kwargs: Any) -> dict[str, Any]:
    """Read `input_path`, process the row range of one sheet, write `output_path`.

    All other sheets are passed through unchanged. Returns the summary dict
    (with input/output/sheet/mapping details added).

    `shape` is "full" (every endpoint field written back as a column) or
    "slim" (compact place fields only).
    """
    input_path = Path(input_path)
    sheets = read_workbook(input_path)

    if sheet is None:
        sheet_name = pick_sheet(sheets)
    elif isinstance(sheet, int):
        sheet_name = list(sheets)[max(0, min(sheet, len(sheets) - 1))]
    else:
        if sheet not in sheets:
            raise ValueError(f"Sheet '{sheet}' not found (available: {list(sheets)})")
        sheet_name = sheet

    df = sheets[sheet_name]
    mapping = merge_mapping(detect_columns(df), column_overrides,
                            columns=[str(c) for c in df.columns])
    if not mapping.query:
        raise ValueError(f"No query column detected in sheet '{sheet_name}' - set it explicitly")

    processed, summary = process_dataframe(
        df, mapping, start_row=start_row, end_row=end_row, shape=shape,
        progress_cb=progress_cb, stop_cb=stop_cb, **process_kwargs)

    sheets[sheet_name] = processed
    out_path = Path(output_path) if output_path else default_output_path(input_path)
    if out_path.resolve() == input_path.resolve():
        out_path = default_output_path(input_path)
    write_workbook(out_path, sheets)

    summary.update({
        "input": str(input_path),
        "output": str(out_path),
        "sheet": sheet_name,
        "columns": mapping.to_dict(),
        "file_type": out_path.suffix.lower(),
    })
    return summary


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="maps-batch",
        description="Run Google Maps searches for every row of a spreadsheet via "
                    "POST /v1/maps/search/bulk and write the results back into the file.")
    parser.add_argument("input", help="input .xlsx/.xls/.csv file")
    parser.add_argument("-o", "--output", help="output file (default: <input>_processed.<ext>)")
    parser.add_argument("--sheet", default=None, help="sheet name or 0-based index (default: auto)")
    parser.add_argument("--start-row", type=int, default=None, help="first row to process (1-based, inclusive)")
    parser.add_argument("--end-row", type=int, default=None, help="last row to process (1-based, inclusive)")
    parser.add_argument("--query-column", action="append", default=None, metavar="COL",
                        help="column holding the search query; repeat the flag or comma-separate "
                             "several columns (e.g. --query-column City,Item) to append them "
                             "into one query")
    parser.add_argument("--ll-column", default=None, help="column holding '@lat,lng,zoom'")
    parser.add_argument("--lat-column", default=None)
    parser.add_argument("--lng-column", default=None)
    parser.add_argument("--zoom-column", default=None)
    parser.add_argument("--start-column", default=None, help="column holding the pagination offset")
    parser.add_argument("--shape", choices=("slim", "full"), default=PROCESSOR_SHAPE,
                        help="response mode: 'full' writes back every endpoint field as a "
                             "column (default), 'slim' the compact place fields")
    parser.add_argument("--bridge-url", default=BRIDGE_URL)
    parser.add_argument("--batch-size", type=int, default=PROCESSOR_BATCH_SIZE,
                        help="rows per bulk call")
    parser.add_argument("--concurrency", type=int, default=PROCESSOR_CONCURRENCY,
                        help="parallel searches per bulk call (bridge side)")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(levelname)s %(message)s")

    query_override: Any = None
    if args.query_column:
        query_override = args.query_column[0] if len(args.query_column) == 1 else args.query_column

    overrides = {k: v for k, v in {
        "query": query_override, "ll": args.ll_column, "lat": args.lat_column,
        "lng": args.lng_column, "zoom": args.zoom_column, "start": args.start_column,
    }.items() if v}

    def on_progress(info: ProgressInfo) -> None:
        if args.quiet:
            return
        pct = (100.0 * info.done / info.total) if info.total else 100.0
        sys.stderr.write(f"\r  {pct:5.1f}%  {info.done}/{info.total} rows  "
                         f"ok={info.succeeded} failed={info.failed}  {info.message}   ")
        sys.stderr.flush()

    sheet: Any = args.sheet
    if sheet is not None and isinstance(sheet, str) and sheet.isdigit():
        sheet = int(sheet)

    try:
        summary = process_file(
            args.input, args.output, sheet=sheet, column_overrides=overrides,
            start_row=args.start_row, end_row=args.end_row, shape=args.shape,
            bridge_url=args.bridge_url, batch_size=args.batch_size,
            concurrency=args.concurrency, progress_cb=on_progress)
    except (BulkRequestError, ValueError, FileNotFoundError, OSError) as exc:
        if not args.quiet:
            sys.stderr.write("\n")
        log.error("%s", exc)
        return 2

    if not args.quiet:
        sys.stderr.write("\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0 if summary["rows_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
