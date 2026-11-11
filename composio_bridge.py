#!/usr/bin/env python3
"""
Composio -> REST bridge
=======================

Turns Composio MCP tools into a plain, language-agnostic REST API so any
external code (curl, Node, Go, PHP, n8n, ...) can call them with simple JSON.

Primary tool exposed
--------------------
    Composio Search -> Composio Google Maps Search
    slug: COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH

    Input  : {"q": "pizza", "ll": "@40.7455096,-74.0083012,14z", "start": 1}
             q     (required) free-text place query, e.g. "pizza", "hotels"
             ll    (optional) "@latitude,longitude,zoom" - locate the search;
                   REQUIRED when paginating with "start"
             start (optional) pagination offset (1, 15, 30, ...)

Other Composio tools can be reached through the generic passthrough route
(/v1/tools/{slug}), so the same server can front any tool in your account.

Run
---
    pip install fastapi "uvicorn[standard]" httpx python-dotenv
    # .env
    COMPOSIO_API_KEY=ak_...
    uvicorn composio_bridge:app --host 0.0.0.0 --port 8000

    # or simply
    python composio_bridge.py

Docs: http://127.0.0.1:8000/docs   (Swagger UI)
      http://127.0.0.1:8000/redoc  (ReDoc)

Quick test
----------
    curl -X POST http://127.0.0.1:8000/v1/maps/search \
         -H 'Content-Type: application/json' \
         -d '{"q":"pizza","ll":"@40.7455096,-74.0083012,14z"}'
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError as ValidationErrorPlaceholder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()  # loads .env sitting next to this file / cwd

# --- API keys: "COMPOSIO_API_KEY=ak_a,ak_b,ak_c" -> round-robin 1,2,3,1,2,3... ---


def _parse_keys(raw: str) -> list[str]:
    """Split on commas/newlines/semicolons, trim, drop blanks, de-duplicate (order kept)."""
    seen: set[str] = set()
    keys: list[str] = []
    for piece in raw.replace(";", ",").replace("\n", ",").split(","):
        k = piece.strip().strip('"').strip("'")
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


API_KEYS: list[str] = _parse_keys(os.getenv("COMPOSIO_API_KEY") or "")
COMPOSIO_BASE_URL: str = os.getenv("COMPOSIO_BASE_URL", "https://backend.composio.dev").rstrip("/")
COMPOSIO_USER_ID: str = os.getenv("COMPOSIO_USER_ID", "default")
REQUEST_TIMEOUT: float = float(os.getenv("COMPOSIO_TIMEOUT", "90"))
MAX_BATCH_SIZE: int = int(os.getenv("MAX_BATCH_SIZE", "50"))
MAX_BATCH_CONCURRENCY: int = int(os.getenv("MAX_BATCH_CONCURRENCY", "8"))
# Optional: set a bearer token to protect this bridge when exposed publicly.
BRIDGE_API_TOKEN: str = (os.getenv("BRIDGE_API_TOKEN") or "").strip()
# Optional: friendly aliases, e.g. COMPOSIO_ALIASES="maps=COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH"
_raw_aliases = os.getenv("COMPOSIO_ALIASES", "maps=COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH")
ALIASES: dict[str, str] = {}
for _pair in _raw_aliases.split(","):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        ALIASES[_k.strip().lower()] = _v.strip()

GOOGLE_MAPS_SLUG = "COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH"
DEFAULT_ZOOM = int(os.getenv("GOOGLE_MAPS_DEFAULT_ZOOM", "13"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("composio-bridge")

# --------------------------------------------------------------------------
# HTTP client lifecycle
# --------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def mask_key(key: str) -> str:
    """ak_abcd...wxyz - safe to show in logs / /health."""
    if len(key) <= 10:
        return (key[:4] + "***") if key else ""
    return f"{key[:8]}...{key[-4:]}"


class KeyPool:
    """
    Round-robin rotation over N API keys.

    Request 1 -> keys[0], request 2 -> keys[1], ..., request N+1 -> keys[0] ...

    "Busy" keys (currently mid-request) are skipped so concurrent bulk calls
    spread evenly instead of all landing on the same key. If every key is busy
    it falls back to the pure round-robin pick rather than waiting.
    """

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)
        self._cursor = 0
        self._busy: dict[int, int] = {i: 0 for i in range(len(keys))}
        self._lock = threading.Lock()
        self._stats: dict[int, dict[str, int]] = {
            i: {"requests": 0, "successes": 0, "failures": 0, "rate_limited": 0, "auth_failures": 0}
            for i in range(len(keys))
        }
        self._last_used: dict[int, float] = {}

    # -- introspection ------------------------------------------------------
    def __len__(self) -> int:
        return len(self._keys)

    @property
    def size(self) -> int:
        return len(self._keys)

    def keys(self) -> list[str]:
        return list(self._keys)

    def reload(self, keys: list[str]) -> int:
        """Swap the key list at runtime (e.g. after editing .env). Resets rotation/stats."""
        with self._lock:
            self._keys = list(keys)
            self._cursor = 0
            self._busy = {i: 0 for i in range(len(self._keys))}
            self._stats = {i: {"requests": 0, "successes": 0, "failures": 0,
                               "rate_limited": 0, "auth_failures": 0} for i in range(len(self._keys))}
            self._last_used = {}
        return len(self._keys)

    def snapshot(self, reveal: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for i, key in enumerate(self._keys):
                out.append({
                    "index": i + 1,
                    "key": key if reveal else mask_key(key),
                    "in_flight": self._busy[i],
                    "last_used_epoch": self._last_used.get(i),
                    **self._stats[i],
                })
            return out

    @property
    def total_requests(self) -> int:
        with self._lock:
            return sum(s["requests"] for s in self._stats.values())

    # -- rotation -----------------------------------------------------------
    def _acquire(self) -> int:
        """Pick the next key index (round-robin, skipping busy keys) and mark it busy."""
        with self._lock:
            n = len(self._keys)
            if n == 0:
                raise RuntimeError("No API keys configured")
            start = self._cursor % n
            chosen = start
            for step in range(n):
                idx = (start + step) % n
                if self._busy[idx] == 0:
                    chosen = idx
                    break
            self._cursor = (chosen + 1) % n  # next request starts searching from the following key
            self._busy[chosen] += 1
            self._stats[chosen]["requests"] += 1
            self._last_used[chosen] = time.time()
            return chosen

    def _release(self, idx: int) -> None:
        with self._lock:
            self._busy[idx] = max(0, self._busy[idx] - 1)

    def record(self, idx: int, outcome: str) -> None:
        """outcome: success | failure | rate_limited | auth_failure"""
        bucket = {"rate_limited": "rate_limited", "auth_failure": "auth_failures"}.get(outcome, outcome)
        with self._lock:
            if idx in self._stats and bucket in self._stats[idx]:
                self._stats[idx][bucket] += 1

    @asynccontextmanager
    async def use(self):
        """Yield (index, key) for one upstream call, then release the busy slot."""
        idx = self._acquire()
        try:
            yield idx, self._keys[idx]
        finally:
            self._release(idx)


KEY_POOL = KeyPool(API_KEYS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    if not API_KEYS:
        log.warning("COMPOSIO_API_KEY is not set - every tool call will fail with 503.")
    _client = httpx.AsyncClient(
        base_url=COMPOSIO_BASE_URL,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        headers={"Content-Type": "application/json"},
    )
    log.info("Composio bridge ready -> %s | %d API key(s) loaded: %s", COMPOSIO_BASE_URL,
             KEY_POOL.size, ", ".join(mask_key(k) for k in API_KEYS) or "NONE")
    try:
        yield
    finally:
        await _client.aclose()
        _client = None


app = FastAPI(
    title="Composio REST Bridge",
    version="1.1.0",
    description=(
        "Plain REST wrapper around Composio tools. "
        "Focus: **COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH** (Composio Search -> Google Maps Search). "
        "Batch (Bulk) endpoints accept up to " + str(MAX_BATCH_SIZE) + " items per call."
    ),
    lifespan=lifespan,
)

# --------------------------------------------------------------------------
# Error model
#
# Composio returns HTTP 200 with {"successful": false, "error": "..."} for
# bad tool arguments, and real 4xx/5xx for auth / missing-tool problems.
# We normalise both into meaningful HTTP statuses so callers never have to
# inspect a 200 response body to know something failed.
# --------------------------------------------------------------------------


def error_body(status_code: int, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"status": status_code, "code": code, "message": message, **extra}}


def raise_upstream(error: dict[str, Any], key_index: Optional[int] = None) -> None:
    """Map a Composio API-level error object ({'error': {...}}) to an HTTPException."""
    err = error.get("error") or {}
    tag = {"key_index": key_index + 1} if key_index is not None else {}
    upstream_status = int(err.get("status") or 502)
    slug = err.get("slug") or "ComposioError"
    message = err.get("message") or "Composio request failed"

    if upstream_status in (401, 403):
        http_status, code = 502, "COMPOSIO_AUTH_FAILED"
        message = f"Composio authentication failed (check COMPOSIO_API_KEY): {message}"
    elif upstream_status == 404:
        http_status, code = 404, "TOOL_NOT_FOUND"
    elif upstream_status == 429:
        http_status, code = 429, "COMPOSIO_RATE_LIMITED"
    elif 400 <= upstream_status < 500:
        http_status, code = 422, "INVALID_ARGUMENTS"
    else:
        http_status, code = 502, "COMPOSIO_UPSTREAM_ERROR"

    raise HTTPException(
        status_code=http_status,
        detail=error_body(http_status, code, message, upstream_slug=slug,
                          suggested_fix=err.get("suggested_fix") or "",
                          request_id=err.get("request_id"), **tag),
    )


class KeyRetryable(Exception):
    """Internal: this key was unusable (401/403/429) - retry on the next key."""

    def __init__(self, http_exc: HTTPException) -> None:
        self.http_exc = http_exc


RETRY_ON_KEY_FAILURE: bool = os.getenv("COMPOSIO_RETRY_ON_KEY_FAILURE", "true").lower() in ("1", "true", "yes")


async def _execute_once(slug: str, payload: dict[str, Any], key: str, key_index: int, started: float) -> dict[str, Any]:
    """Single upstream call using one specific key. Raises HTTPException or KeyRetryable."""
    assert _client is not None
    try:
        resp = await _client.post(f"/api/v3/tools/execute/{slug}", json=payload, headers={"x-api-key": key})
    except httpx.TimeoutException:
        KEY_POOL.record(key_index, "failure")
        raise HTTPException(504, detail=error_body(504, "UPSTREAM_TIMEOUT",
                                                   f"Composio did not respond within {REQUEST_TIMEOUT}s.",
                                                   key_index=key_index + 1))
    except httpx.HTTPError as exc:
        KEY_POOL.record(key_index, "failure")
        raise HTTPException(502, detail=error_body(502, "UPSTREAM_UNREACHABLE", str(exc), key_index=key_index + 1))

    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        body = resp.json()
    except ValueError:
        KEY_POOL.record(key_index, "failure")
        raise HTTPException(502, detail=error_body(502, "BAD_UPSTREAM_RESPONSE",
                                                   resp.text[:500] or "Non-JSON response", key_index=key_index + 1))

    # API-level error (bad key, unknown tool, rate limit ...)
    if isinstance(body, dict) and "error" in body and not body.get("successful", False):
        upstream_status = int((body.get("error") or {}).get("status") or 0)
        if upstream_status in (401, 403):
            KEY_POOL.record(key_index, "auth_failure")
        elif upstream_status == 429:
            KEY_POOL.record(key_index, "rate_limited")
        else:
            KEY_POOL.record(key_index, "failure")
        if upstream_status in (401, 403, 429) and RETRY_ON_KEY_FAILURE and KEY_POOL.size > 1:
            try:
                raise_upstream(body, key_index)
            except HTTPException as exc:
                raise KeyRetryable(exc)
        raise_upstream(body, key_index)

    # Tool-level failure delivered as HTTP 200 + successful:false
    if not body.get("successful", False):
        KEY_POOL.record(key_index, "failure")
        msg = body.get("error") or "Composio tool reported failure"
        raise HTTPException(422, detail=error_body(422, "TOOL_EXECUTION_FAILED", msg,
                                                   log_id=body.get("log_id"), key_index=key_index + 1))

    KEY_POOL.record(key_index, "success")
    return {"ok": True, "data": body.get("data"), "log_id": body.get("log_id"),
            "latency_ms": latency_ms, "key_index": key_index + 1}


async def execute_tool(slug: str, arguments: dict[str, Any], user_id: Optional[str] = None) -> dict[str, Any]:
    """
    Call POST /api/v3/tools/execute/{slug} using the next key from the round-robin pool.

    If a key is rejected (401/403) or rate limited (429), the call is retried on
    the next key, up to (number of keys) attempts.

    Returns: {"ok": True, "data": ..., "log_id": ..., "latency_ms": ..., "key_index": n}
    Raises HTTPException on any failure.
    """
    if _client is None:
        raise HTTPException(503, detail=error_body(503, "NOT_READY", "Server still starting up."))
    if KEY_POOL.size == 0:
        raise HTTPException(503, detail=error_body(503, "NO_API_KEYS",
                                                   "No Composio API keys configured. Set COMPOSIO_API_KEY=key1,key2,... in .env"))

    payload: dict[str, Any] = {"arguments": arguments}
    uid = user_id or COMPOSIO_USER_ID
    if uid:
        payload["user_id"] = uid

    attempts = max(1, KEY_POOL.size)
    last_exc: Optional[HTTPException] = None
    tried: list[int] = []

    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            async with KEY_POOL.use() as (key_index, key):
                tried.append(key_index + 1)
                return await _execute_once(slug, payload, key, key_index, started)
        except KeyRetryable as retry:
            if not RETRY_ON_KEY_FAILURE:
                raise retry.http_exc
            last_exc = retry.http_exc
            log.warning("Key #%d unusable (attempt %d/%d) - rotating to next key",
                        tried[-1], attempt + 1, attempts)
            continue

    assert last_exc is not None
    if isinstance(last_exc.detail, dict):
        last_exc.detail["error"]["keys_tried"] = tried
    raise last_exc


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

LatLngPattern = r"^@-?\d+(\.\d+)?,-?\d+(\.\d+)?(,\d+(\.\d+)?z)?$"


class MapsSearchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"q": "pizza", "ll": "@40.7455096,-74.0083012,14z", "start": 1}
        }
    )

    q: str = Field(..., min_length=1, max_length=500, description="The query you want to search.", examples=["pizza", "hotels"])
    ll: Optional[str] = Field(
        None,
        pattern=LatLngPattern,
        description="GPS coordinates for the search, formatted as '@latitude,longitude,zoom_level'. Required when paginating.",
        examples=["@40.7455096,-74.0083012,14z"],
    )
    start: Optional[int] = Field(None, ge=1, le=1000, description="Used for pagination (1, 15, 30, ...).")
    user_id: Optional[str] = Field(None, max_length=128, description="Composio end-user id (default: server default).")

    @field_validator("q")
    @classmethod
    def _strip_q(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("'q' must not be blank")
        return v

    def to_arguments(self) -> dict[str, Any]:
        # Only forward fields that were actually supplied.
        return self.model_dump(mode="json", exclude_none=True, exclude={"user_id"})


class BulkMapsSearchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "continue_on_error": True,
                "concurrency": 4,
                "requests": [
                    {"id": "nyc-pizza", "q": "pizza", "ll": "@40.7455096,-74.0083012,14z"},
                    {"id": "tokyo-sushi", "q": "sushi", "ll": "@35.6762,139.6503,13z"},
                ],
            }
        }
    )

    requests: list[MapsSearchRequest] = Field(..., min_length=1, description="1..MAX_BATCH_SIZE search requests.")
    continue_on_error: bool = Field(True, description="Keep going when one item fails (its slot gets an error).")
    concurrency: int = Field(4, ge=1, le=16, description="Max parallel Composio calls.")

    @field_validator("requests")
    @classmethod
    def _size_limit(cls, v: list[MapsSearchRequest]) -> list[MapsSearchRequest]:
        if len(v) > MAX_BATCH_SIZE:
            raise ValueError(f"Too many requests: {len(v)} > MAX_BATCH_SIZE={MAX_BATCH_SIZE}")
        return v


class BulkItem(BaseModel):
    index: int
    id: Optional[str] = None
    ok: bool
    data: Any = None
    error: Any = None
    latency_ms: Optional[int] = None
    log_id: Optional[str] = None


class BulkResponse(BaseModel):
    ok: bool
    total: int
    succeeded: int
    failed: int
    results: list[BulkItem]


class GenericExecuteRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"example": {"arguments": {"q": "coffee"}, "user_id": "default"}}
    )
    arguments: dict[str, Any] = Field(default_factory=dict, description="Raw tool arguments, e.g. {\"q\": \"coffee\"}.")
    user_id: Optional[str] = None


class GenericBulkItem(BaseModel):
    slug: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None
    id: Optional[str] = None


class GenericBulkRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "continue_on_error": True,
                "items": [
                    {"slug": "COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH", "arguments": {"q": "pizza"}},
                    {"slug": "COMPOSIO_SEARCH_SEARCH", "arguments": {"query": "fastapi"}},
                ],
            }
        }
    )
    items: list[GenericBulkItem] = Field(..., min_length=1)
    continue_on_error: bool = True
    concurrency: int = Field(4, ge=1, le=16)

    @field_validator("items")
    @classmethod
    def _size_limit(cls, v: list[GenericBulkItem]) -> list[GenericBulkItem]:
        if len(v) > MAX_BATCH_SIZE:
            raise ValueError(f"Too many items: {len(v)} > MAX_BATCH_SIZE={MAX_BATCH_SIZE}")
        return v


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def check_bearer(authorization: Optional[str]) -> None:
    """If BRIDGE_API_TOKEN is configured, require 'Authorization: Bearer <token>'."""
    if not BRIDGE_API_TOKEN:
        return
    expected = f"Bearer {BRIDGE_API_TOKEN}"
    if (authorization or "") != expected:
        raise HTTPException(401, detail=error_body(401, "UNAUTHORIZED", "Missing or invalid bearer token."),
                            headers={"WWW-Authenticate": "Bearer"})


def resolve_slug(slug: str) -> str:
    return ALIASES.get(slug.lower(), slug.upper())


def compose_ll(lat: float, lng: float, zoom: int = DEFAULT_ZOOM) -> str:
    return f"@{lat},{lng},{zoom}z"


def normalise_ll(ll: Optional[str], lat: Optional[float], lng: Optional[float], zoom: int) -> Optional[str]:
    """Accept either ?ll=@lat,lng,zoom  OR  ?lat=..&lng=..&zoom=.."""
    if lat is not None and lng is not None:
        return compose_ll(lat, lng, zoom)
    if ll:
        if not ll.startswith("@"):
            ll = "@" + ll
        # Add zoom if the caller only gave "@lat,lng"
        parts = ll[1:].split(",")
        if len(parts) == 2:
            ll = f"{ll},{zoom}z"
        return ll
    return None


def slim_place(place: dict[str, Any]) -> dict[str, Any]:
    """Reduce a very verbose SerpApi local result (~2.5 KB each) to useful fields."""
    gps = place.get("gps_coordinates") or {}
    keep = {
        "title": place.get("title"),
        "address": place.get("address"),
        "type": place.get("type"),
        "types": place.get("types"),
        "rating": place.get("rating"),
        "reviews": place.get("reviews"),
        "price": place.get("price"),
        "phone": place.get("phone"),
        "website": place.get("website"),
        "hours": place.get("hours"),
        "open_state": place.get("open_state"),
        "description": place.get("description"),
        "place_id": place.get("place_id"),
        "data_id": place.get("data_id"),
        "gps_coordinates": gps or None,
        "latitude": gps.get("latitude"),
        "longitude": gps.get("longitude"),
        "thumbnail": place.get("thumbnail"),
        "position": place.get("position"),
    }
    return {k: v for k, v in keep.items() if v is not None}


def extract_places(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, dict):
        return []
    places = results.get("local_results") or []
    return places if isinstance(places, list) else []


# --------------------------------------------------------------------------
# Routes: health & discovery
# --------------------------------------------------------------------------


@app.get("/health", tags=["Meta"], summary="Liveness + config check",
         description="Always public. Shows how many API keys are loaded and per-key usage counters.")
async def health(reveal_keys: bool = Query(False, description="Show full keys instead of masked ones.")) -> dict[str, Any]:
    return {
        "ok": True,
        "service": "composio-rest-bridge",
        "version": app.version,
        "status": "healthy" if KEY_POOL.size else "degraded",
        # ---- key pool ----
        "api_keys_loaded": KEY_POOL.size,
        "keys_configured": KEY_POOL.size > 0,
        "rotation_strategy": "round-robin",
        "next_key_index": (KEY_POOL._cursor % KEY_POOL.size) + 1 if KEY_POOL.size else None,
        "total_requests_routed": KEY_POOL.total_requests,
        "retry_on_key_failure": RETRY_ON_KEY_FAILURE,
        "keys": KEY_POOL.snapshot(reveal=reveal_keys),
        # ---- config ----
        "composio_base_url": COMPOSIO_BASE_URL,
        "default_user_id": COMPOSIO_USER_ID,
        "bridge_token_required": bool(BRIDGE_API_TOKEN),
        "max_batch_size": MAX_BATCH_SIZE,
    }


@app.get("/v1/keys", tags=["Meta"], summary="Key pool status",
         description="Per-key counters (masked). Use /health?reveal_keys=true if you really need full keys.")
async def key_status(reveal: bool = Query(False),
                     authorization: Optional[str] = Header(None)) -> Any:
    check_bearer(authorization)
    if reveal and not BRIDGE_API_TOKEN:
        raise HTTPException(403, detail=error_body(
            403, "REVEAL_REQUIRES_TOKEN",
            "Set BRIDGE_API_TOKEN in .env to allow revealing full keys."))
    return {
        "ok": True,
        "rotation_strategy": "round-robin",
        "keys_loaded": KEY_POOL.size,
        "next_key_index": (KEY_POOL._cursor % KEY_POOL.size) + 1 if KEY_POOL.size else None,
        "keys": KEY_POOL.snapshot(reveal=reveal),
    }


@app.post("/v1/keys/reload", tags=["Meta"], summary="Reload keys from .env without restarting",
          description="Re-reads COMPOSIO_API_KEY from the environment / .env file and resets rotation.")
async def reload_keys(authorization: Optional[str] = Header(None)) -> Any:
    check_bearer(authorization)
    load_dotenv(override=True)
    global API_KEYS
    API_KEYS = _parse_keys(os.getenv("COMPOSIO_API_KEY") or "")
    count = KEY_POOL.reload(API_KEYS)
    log.info("Key pool reloaded: %d key(s)", count)
    return {"ok": True, "api_keys_loaded": count, "keys": KEY_POOL.snapshot()}


@app.get("/v1/tools", tags=["Meta"], summary="List / discover Composio tools",
         description="Lists tools for a toolkit (default: composio_search). Mirrors Composio GET /api/v3/tools.")
async def list_tools(
    toolkit_slug: str = Query("composio_search"),
    search: Optional[str] = Query(None, description="Filter results by substring of slug/name/description."),
    limit: int = Query(50, ge=1, le=200),
    authorization: Optional[str] = Header(None),
) -> Any:
    check_bearer(authorization)
    if _client is None:
        raise HTTPException(503, "Server starting")
    try:
        resp = await _client.get("/api/v3/tools", params={"toolkit_slug": toolkit_slug, "limit": limit})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=error_body(502, "UPSTREAM_UNREACHABLE", str(exc)))
    items = resp.json().get("items", [])
    if search:
        s = search.lower()
        items = [t for t in items
                 if s in (t.get("slug") or "").lower()
                 or s in (t.get("name") or "").lower()
                 or s in (t.get("description") or "").lower()]
    return {
        "ok": True,
        "toolkit": toolkit_slug,
        "count": len(items),
        "tools": [
            {"slug": t.get("slug"), "name": t.get("name"), "description": (t.get("description") or "")[:240],
             "no_auth": t.get("no_auth")}
            for t in items
        ],
    }


@app.get("/v1/tools/{slug:path}/schema", tags=["Meta"], summary="Fetch a tool's input/output JSON schema")
async def tool_schema(slug: str, authorization: Optional[str] = Header(None)) -> Any:
    check_bearer(authorization)
    if _client is None:
        raise HTTPException(503, "Server starting")
    real = resolve_slug(slug)
    resp = await _client.get(f"/api/v3/tools/{real}")
    if resp.status_code == 404:
        raise HTTPException(404, detail=error_body(404, "TOOL_NOT_FOUND", f"Tool {real} not found"))
    try:
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=error_body(502, "UPSTREAM_ERROR", str(exc)))
    t = resp.json()
    return {
        "ok": True,
        "slug": t.get("slug"),
        "name": t.get("name"),
        "description": t.get("description"),
        "version": t.get("version"),
        "input_parameters": t.get("input_parameters"),
        "output_parameters": t.get("output_parameters"),
    }


# --------------------------------------------------------------------------
# Routes: Google Maps Search (single) - dedicated, typed endpoints
# --------------------------------------------------------------------------


def _maps_response(result: dict[str, Any], shape: str, q: str, ll: Optional[str]) -> dict[str, Any]:
    data = result["data"] or {}
    places = extract_places(data)
    body: dict[str, Any] = {
        "ok": True,
        "tool": GOOGLE_MAPS_SLUG,
        "query": q,
        "ll": ll,
        "count": len(places),
        "log_id": result.get("log_id"),
        "latency_ms": result.get("latency_ms"),
        "key_index": result.get("key_index"),
    }
    if shape == "full":
        body["data"] = data
    else:
        body["places"] = [slim_place(p) for p in places if isinstance(p, dict)]
        results = data.get("results") if isinstance(data, dict) else {}
        if isinstance(results, dict) and "search_information" in results:
            body["search_information"] = results["search_information"]
    return body


@app.post("/v1/maps/search", tags=["Google Maps Search"], summary="Single search (JSON body)",
         description="Composio Google Maps Search. Returns a slimmed 'places' list by default, 'data' when shape=full.")
async def maps_search(
    req: MapsSearchRequest = Body(...),
    shape: Literal["slim", "full"] = Query("slim", description="slim = compact places list, full = raw Composio data"),
    authorization: Optional[str] = Header(None),
) -> Any:
    check_bearer(authorization)
    result = await execute_tool(GOOGLE_MAPS_SLUG, req.to_arguments(), req.user_id)
    return _maps_response(result, shape, req.q, req.ll)


@app.get("/v1/maps/search", tags=["Google Maps Search"], summary="Single search (query string)",
         description="Same as POST /v1/maps/search but callable from a browser or simple GET clients.")
async def maps_search_get(
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    ll: Optional[str] = Query(None, pattern=LatLngPattern, description="'@lat,lng,zoom'"),
    lat: Optional[float] = Query(None, ge=-90, le=90, description="Latitude (alternative to ll)"),
    lng: Optional[float] = Query(None, ge=-180, le=180, description="Longitude (alternative to ll)"),
    zoom: int = Query(DEFAULT_ZOOM, ge=3, le=21, description="Zoom level when using lat/lng"),
    start: Optional[int] = Query(None, ge=1, le=1000, description="Pagination offset"),
    user_id: Optional[str] = Query(None),
    shape: Literal["slim", "full"] = Query("slim"),
    authorization: Optional[str] = Header(None),
) -> Any:
    check_bearer(authorization)
    resolved_ll = normalise_ll(ll, lat, lng, zoom)
    args: dict[str, Any] = {"q": q.strip()}
    if resolved_ll:
        args["ll"] = resolved_ll
    if start is not None:
        args["start"] = start
    if not args["q"]:
        raise HTTPException(422, detail=error_body(422, "INVALID_ARGUMENTS", "'q' must not be blank"))
    result = await execute_tool(GOOGLE_MAPS_SLUG, args, user_id)
    return _maps_response(result, shape, args["q"], resolved_ll)


# --------------------------------------------------------------------------
# Routes: Google Maps Search (bulk)
# --------------------------------------------------------------------------


@app.post("/v1/maps/search/bulk", tags=["Google Maps Search"], summary="Bulk search (1..N queries in one call)",
         description=(
             "Runs up to MAX_BATCH_SIZE searches concurrently against Composio. "
             "Each result slot always contains either 'data' or 'error'. "
             "HTTP 200 for a mixed batch; 422 if every item failed and continue_on_error=false."
         ),
         response_model=BulkResponse)
async def maps_search_bulk(
    req: BulkMapsSearchRequest = Body(...),
    shape: Literal["slim", "full"] = Query("slim"),
    authorization: Optional[str] = Header(None),
) -> Any:
    check_bearer(authorization)
    sem = asyncio.Semaphore(min(req.concurrency, MAX_BATCH_CONCURRENCY))

    async def one(idx: int, item: MapsSearchRequest) -> BulkItem:
        async with sem:
            try:
                res = await execute_tool(GOOGLE_MAPS_SLUG, item.to_arguments(), item.user_id)
                out = _maps_response(res, shape, item.q, item.ll)
                return BulkItem(index=idx, id=getattr(item, "id", None), ok=True,
                                data=out.get("data") if shape == "full" else out.get("places"),
                                latency_ms=res.get("latency_ms"), log_id=res.get("log_id"))
            except HTTPException as exc:
                if not req.continue_on_error:
                    raise
                return BulkItem(index=idx, ok=False, error=exc.detail)
            except Exception as exc:  # noqa: BLE001 - never let one item kill the batch
                if not req.continue_on_error:
                    raise
                return BulkItem(index=idx, ok=False,
                                error=error_body(500, "ITEM_FAILED", f"{type(exc).__name__}: {exc}"))

    results = await asyncio.gather(*(one(i, r) for i, r in enumerate(req.requests)),
                                   return_exceptions=False)
    succeeded = sum(1 for r in results if r.ok)
    return BulkResponse(ok=succeeded == len(results), total=len(results),
                        succeeded=succeeded, failed=len(results) - succeeded, results=results)


# --------------------------------------------------------------------------
# Routes: generic passthrough (any Composio tool), single + bulk
# --------------------------------------------------------------------------


@app.post("/v1/tools/bulk/execute", tags=["Any Composio tool"], summary="Execute many Composio tools in one call",
          description="Mix different tools in one request (e.g. maps search + web search + news).")
async def execute_any_bulk(
    req: GenericBulkRequest = Body(...),
    authorization: Optional[str] = Header(None),
) -> Any:
    check_bearer(authorization)
    return await _run_generic_bulk(req)


# NOTE: this catch-all MUST stay declared *after* /v1/tools/bulk/execute,
# otherwise 'bulk' is swallowed by the {slug} path parameter.
@app.post("/v1/tools/{slug:path}/execute", tags=["Any Composio tool"], summary="Execute any Composio tool")
async def execute_any(
    slug: str,
    req: GenericExecuteRequest = Body(...),
    raw: bool = Query(True, description="true = return Composio 'data' verbatim; false = wrapped"),
    authorization: Optional[str] = Header(None),
) -> Any:
    check_bearer(authorization)
    real = resolve_slug(slug)
    result = await execute_tool(real, req.arguments, req.user_id)
    if raw:
        return JSONResponse(result["data"], headers={"X-Composio-Log-Id": result.get("log_id") or "",
                                                     "X-Composio-Latency-Ms": str(result.get("latency_ms") or 0),
                                                     "X-Composio-Key-Index": str(result.get("key_index") or 0)})
    return result


async def _run_generic_bulk(req: GenericBulkRequest) -> Any:
    sem = asyncio.Semaphore(min(req.concurrency, MAX_BATCH_CONCURRENCY))

    async def one(idx: int, item: GenericBulkItem) -> dict[str, Any]:
        async with sem:
            try:
                res = await execute_tool(resolve_slug(item.slug), item.arguments, item.user_id)
                return {"index": idx, "id": item.id, "slug": item.slug, "ok": True, "data": res["data"],
                        "latency_ms": res.get("latency_ms"), "log_id": res.get("log_id")}
            except HTTPException as exc:
                if not req.continue_on_error:
                    raise
                return {"index": idx, "id": item.id, "slug": item.slug, "ok": False, "error": exc.detail}

    results = await asyncio.gather(*(one(i, it) for i, it in enumerate(req.items)))
    succeeded = sum(1 for r in results if r["ok"])
    return {"ok": succeeded == len(results), "total": len(results), "succeeded": succeeded,
            "failed": len(results) - succeeded, "results": results}


# --------------------------------------------------------------------------
# Convenience aliases matching the Composio slug spelling
# --------------------------------------------------------------------------


@app.post("/v1/COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH", tags=["Google Maps Search"],
          summary="Alias -> POST /v1/maps/search")
async def slug_alias(req: MapsSearchRequest = Body(...), authorization: Optional[str] = Header(None)) -> Any:
    check_bearer(authorization)
    result = await execute_tool(GOOGLE_MAPS_SLUG, req.to_arguments(), req.user_id)
    return _maps_response(result, "full", req.q, req.ll)


# --------------------------------------------------------------------------
# Global error shape: every error is {"ok": false, "error": {...}}
# --------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if not (isinstance(detail, dict) and "error" in detail):
        detail = error_body(exc.status_code, "HTTP_ERROR", str(detail))
    return JSONResponse(detail, status_code=exc.status_code, headers=getattr(exc, "headers", None))


def _safe_issues(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pydantic puts the raw ValueError object in ctx for custom validators - make it JSON-safe."""
    cleaned = []
    for e in errors:
        item = {k: v for k, v in e.items() if k != "ctx"}
        if "ctx" in e:
            item["ctx"] = {k: (str(v) if isinstance(v, BaseException) else v) for k, v in e["ctx"].items()}
        cleaned.append(item)
    return cleaned


@app.exception_handler(ValidationErrorPlaceholder)
async def validation_error_handler(request: Request, exc: ValidationErrorPlaceholder) -> JSONResponse:
    return JSONResponse(
        error_body(422, "VALIDATION_ERROR", "Request body / query parameters are invalid.",
                   issues=_safe_issues(exc.errors())),
        status_code=422,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(error_body(500, "INTERNAL", f"{type(exc).__name__}: {exc}"), status_code=500)


def main() -> None:
    """Console entry point: `uv run composio-bridge` (also `python composio_bridge.py`)."""
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload_flag = os.getenv("RELOAD", "").lower() in ("1", "true", "yes")
    if not KEY_POOL.size:
        log.warning("No API keys loaded - set COMPOSIO_API_KEY=key1,key2 in .env")
    # Import string is required for --reload to work.
    uvicorn.run("composio_bridge:app" if reload_flag else app, host=host, port=port, reload=reload_flag)


if __name__ == "__main__":
    main()
