"""
Offline tests for the per-key rate limiter: sliding window, cooldown rotation,
bounded waiting and the HTTP surface it produces.

Run:  uv run pytest
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import composio_bridge as cb

SEARCH = {"q": "pizza", "ll": "@40.7455096,-74.0083012,14z"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    with TestClient(cb.app) as c:
        yield c


@pytest.fixture(autouse=True)
def restore_state():
    keys = cb.KEY_POOL.keys()
    yield
    cb.KEY_POOL.reload(keys)


def stub_upstream(monkeypatch):
    async def fake(slug, payload, key, key_index, started):
        cb.KEY_POOL.record(key_index, "success")
        return {"ok": True, "data": {"results": {"local_results": []}},
                "log_id": "log", "latency_ms": 1, "key_index": key_index + 1}

    monkeypatch.setattr(cb, "_execute_once", fake)


# --------------------------------------------------------------------------
# Window / cooldown mechanics
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_key_enters_cooldown_after_hitting_limit():
    pool = cb.KeyPool(["k1"], rate_limit=3, window_seconds=60)
    for _ in range(3):
        async with pool.use(max_wait=0):
            pass

    with pytest.raises(cb.HTTPException) as exc:
        async with pool.use(max_wait=0):
            pass
    assert exc.value.status_code == 429
    assert exc.value.detail["error"]["code"] == "KEY_POOL_COOLING_DOWN"
    assert exc.value.detail["error"]["retry_after_seconds"] > 0

    snap = pool.snapshot()[0]
    assert snap["window_used"] == 3
    assert snap["window_limit"] == 3
    assert snap["cooldown_seconds"] > 0


@pytest.mark.anyio
async def test_cooldown_expires_when_window_slides():
    pool = cb.KeyPool(["k1"], rate_limit=1, window_seconds=0.3)
    async with pool.use(max_wait=0):
        pass

    started = time.monotonic()
    async with pool.use(max_wait=2):   # must wait for the old call to leave the window
        pass
    assert time.monotonic() - started >= 0.25
    # the key served the waiting call again - its fresh slot is now cooling down
    assert 0 < pool.snapshot()[0]["cooldown_seconds"] <= 0.3


@pytest.mark.anyio
async def test_traffic_moves_to_the_next_key_while_one_cools_down():
    pool = cb.KeyPool(["k1", "k2"], rate_limit=1, window_seconds=60)

    async with pool.use(max_wait=0) as (_i, key):
        assert key == "k1"
    async with pool.use(max_wait=0) as (_i, key):
        assert key == "k2"          # k1 is cooling down -> k2 serves the request

    with pytest.raises(cb.HTTPException) as exc:   # both keys cooling down now
        async with pool.use(max_wait=0):
            pass
    assert exc.value.status_code == 429


@pytest.mark.anyio
async def test_rate_limit_zero_disables_the_limiter():
    pool = cb.KeyPool(["k1"], rate_limit=0, window_seconds=60)
    for _ in range(10):
        async with pool.use(max_wait=0):
            pass
    snap = pool.snapshot()[0]
    assert snap["window_limit"] is None
    assert snap["cooldown_seconds"] == 0


@pytest.mark.anyio
async def test_reload_resets_rate_limit_windows():
    pool = cb.KeyPool(["k1"], rate_limit=1, window_seconds=60)
    async with pool.use(max_wait=0):
        pass
    with pytest.raises(cb.HTTPException):
        async with pool.use(max_wait=0):
            pass

    pool.reload(["k1"])
    async with pool.use(max_wait=0):   # fresh window after reload
        pass


@pytest.mark.anyio
async def test_fully_busy_pool_reports_503_not_cooldown():
    pool = cb.KeyPool(["k1"], rate_limit=0)
    held = pool.use(max_wait=0)
    await held.__aenter__()            # keep the only key in flight
    try:
        with pytest.raises(cb.HTTPException) as exc:
            async with pool.use(max_wait=0.01):
                pass
        assert exc.value.status_code == 503
        assert exc.value.detail["error"]["code"] == "KEY_POOL_BUSY"
    finally:
        await held.__aexit__(None, None, None)


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_health_exposes_rate_limit_config_and_per_key_windows(client):
    cb.KEY_POOL.reload(["k1", "k2"])
    d = client.get("/health").json()
    assert d["rate_limit_per_key_per_minute"] == cb.RATE_LIMIT_PER_KEY_PER_MINUTE
    assert d["rate_limit_window_seconds"] == cb.RATE_LIMIT_WINDOW_SECONDS
    for key in d["keys"]:
        assert key["window_used"] == 0
        assert key["window_limit"] == cb.RATE_LIMIT_PER_KEY_PER_MINUTE
        assert key["cooldown_seconds"] == 0


def test_endpoint_429_when_every_key_is_cooling_down(client, monkeypatch):
    cb.KEY_POOL.reload(["k1"])
    monkeypatch.setattr(cb.KEY_POOL, "rate_limit", 1)
    monkeypatch.setattr(cb.KEY_POOL, "window_seconds", 60.0)
    monkeypatch.setattr(cb, "RATE_LIMIT_MAX_WAIT_SECONDS", 0.05)
    stub_upstream(monkeypatch)

    assert client.post("/v1/maps/search", json=SEARCH).status_code == 200

    r = client.post("/v1/maps/search", json=SEARCH)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "KEY_POOL_COOLING_DOWN"
    assert err["retry_after_seconds"] > 0


@pytest.mark.anyio
async def test_execute_tool_records_one_window_slot_per_attempt(monkeypatch):
    cb.KEY_POOL.reload(["k1", "k2"])
    monkeypatch.setattr(cb.KEY_POOL, "rate_limit", 1)
    monkeypatch.setattr(cb.KEY_POOL, "window_seconds", 60.0)
    monkeypatch.setattr(cb, "RATE_LIMIT_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(cb, "_client", object())   # bypass the "not ready" guard

    async def fake(slug, payload, key, key_index, started):
        return {"ok": True, "data": None, "log_id": "l", "latency_ms": 1, "key_index": key_index + 1}

    monkeypatch.setattr(cb, "_execute_once", fake)

    await cb.execute_tool("SLUG", {"q": "x"})      # key 1
    await cb.execute_tool("SLUG", {"q": "x"})      # key 2 (key 1 cooling down)
    assert sum(s["window_used"] for s in cb.KEY_POOL.snapshot()) == 2

    with pytest.raises(cb.HTTPException) as exc:   # both keys cooling down
        await cb.execute_tool("SLUG", {"q": "x"})
    assert exc.value.status_code == 429
    assert exc.value.detail["error"]["code"] == "KEY_POOL_COOLING_DOWN"
