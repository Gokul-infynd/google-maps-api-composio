"""
Offline tests for the bridge plumbing: key rotation, failover, validation, routing.
No network access required - upstream calls are stubbed.

Run:  uv run pytest
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import composio_bridge as cb

REAL_FAKE = "ak_fake_valid_key_1234567890"
BAD_FAKE = "ak_fake_bad_key_0000000000"


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def restore_state():
    """Keep global module state clean between tests."""
    keys, retry = cb.KEY_POOL.keys(), cb.RETRY_ON_KEY_FAILURE
    yield
    cb.KEY_POOL.reload(keys)
    cb.RETRY_ON_KEY_FAILURE = retry


@pytest.fixture
def client():
    with TestClient(cb.app) as c:
        yield c


def stub_upstream(monkeypatch, seen: list[str] | None = None, bad_keys=(), rate_limited=(), unknown_slugs=()):
    """Replace the single upstream call with a fake that records which key was used."""
    seen = seen if seen is not None else []

    async def fake(slug, payload, key, key_index, started):
        if slug in unknown_slugs:
            raise cb.HTTPException(404, detail=cb.error_body(404, "TOOL_NOT_FOUND", f"Tool {slug} not found"))
        seen.append(key)
        if key in bad_keys:
            cb.KEY_POOL.record(key_index, "auth_failure")
            exc = cb.HTTPException(502, detail=cb.error_body(502, "COMPOSIO_AUTH_FAILED", "bad key",
                                                             key_index=key_index + 1))
            # Mirror production: _execute_once only signals KeyRetryable when failover is on.
            if cb.RETRY_ON_KEY_FAILURE:
                raise cb.KeyRetryable(exc)
            raise exc
        if key in rate_limited:
            cb.KEY_POOL.record(key_index, "rate_limited")
            raise cb.KeyRetryable(
                cb.HTTPException(429, detail=cb.error_body(429, "COMPOSIO_RATE_LIMITED", "slow down",
                                                           key_index=key_index + 1)))
        cb.KEY_POOL.record(key_index, "success")
        return {"ok": True, "data": {"results": {"local_results": [{"title": "Fake Place"}]}},
                "log_id": "log_fake", "latency_ms": 1, "key_index": key_index + 1}

    monkeypatch.setattr(cb, "_execute_once", fake)
    return seen


SEARCH = {"q": "pizza", "ll": "@40.7455096,-74.0083012,14z"}


# --------------------------------------------------------------------------
# Key parsing
# --------------------------------------------------------------------------


def test_parse_keys_split_trim_dedupe():
    assert cb._parse_keys(" ak_1 , ak_2,ak_1 ;; ak_3 ,, ") == ["ak_1", "ak_2", "ak_3"]


def test_parse_keys_empty():
    assert cb._parse_keys("") == []
    assert cb._parse_keys(" , , ") == []


def test_mask_key_hides_middle():
    assert cb.mask_key("ak_1234567890abcdefghij") == "ak_12345...ghij"
    assert "***" in cb.mask_key("short")


# --------------------------------------------------------------------------
# Round-robin rotation
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_round_robin_order_is_1_2_3_repeat():
    cb.KEY_POOL.reload(["k1", "k2", "k3"])
    order = []
    for _ in range(7):
        async with cb.KEY_POOL.use() as (i, _k):
            order.append(i + 1)
    assert order == [1, 2, 3, 1, 2, 3, 1]


@pytest.mark.anyio
async def test_concurrent_requests_spread_evenly():
    import asyncio
    from collections import Counter

    cb.KEY_POOL.reload(["k1", "k2", "k3"])
    seen = []

    async def one():
        async with cb.KEY_POOL.use() as (i, _k):
            seen.append(i + 1)
            await asyncio.sleep(0.01)

    await asyncio.gather(*(one() for _ in range(12)))
    assert Counter(seen) == {1: 4, 2: 4, 3: 4}


# --------------------------------------------------------------------------
# Failover
# --------------------------------------------------------------------------


def test_failover_uses_next_key_and_succeeds(client, monkeypatch):
    cb.KEY_POOL.reload([BAD_FAKE, REAL_FAKE])
    seen = stub_upstream(monkeypatch, bad_keys={BAD_FAKE})

    r = client.post("/v1/maps/search", json=SEARCH)
    assert r.status_code == 200
    assert r.json()["key_index"] == 2          # served by key 2 after key 1 failed
    assert seen[0] == BAD_FAKE and seen[1] == REAL_FAKE


def test_rate_limited_key_rotates(client, monkeypatch):
    cb.KEY_POOL.reload([REAL_FAKE, REAL_FAKE + "_b"])
    seen = stub_upstream(monkeypatch, rate_limited={REAL_FAKE})
    r = client.post("/v1/maps/search", json=SEARCH)
    assert r.status_code == 200
    assert r.json()["key_index"] == 2


def test_all_keys_bad_returns_502_with_keys_tried(client, monkeypatch):
    cb.KEY_POOL.reload(["k1", "k2"])
    stub_upstream(monkeypatch, bad_keys={"k1", "k2"})
    r = client.post("/v1/maps/search", json=SEARCH)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["code"] == "COMPOSIO_AUTH_FAILED"
    assert err["keys_tried"] == [1, 2]


def test_no_failover_mode_hits_each_key_in_turn(client, monkeypatch):
    cb.KEY_POOL.reload([REAL_FAKE, BAD_FAKE])
    cb.RETRY_ON_KEY_FAILURE = False
    seen = stub_upstream(monkeypatch, bad_keys={BAD_FAKE})


    codes = []
    for _ in range(4):
        r = client.post("/v1/maps/search", json=SEARCH)
        codes.append(r.status_code)
    assert codes == [200, 502, 200, 502]
    assert seen == [REAL_FAKE, BAD_FAKE, REAL_FAKE, BAD_FAKE]   # strict 1,2,1,2


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_health_reports_key_count(client):
    cb.KEY_POOL.reload(["k1", "k2", "k3"])
    d = client.get("/health").json()
    assert d["api_keys_loaded"] == 3
    assert d["keys_configured"] is True
    assert d["rotation_strategy"] == "round-robin"
    assert len(d["keys"]) == 3
    # keys must be masked by default (short keys use the '***' form)
    assert all(("..." in k["key"]) or ("***" in k["key"]) for k in d["keys"])
    assert "k1" not in [k["key"] for k in d["keys"]]


def test_health_no_keys_is_degraded(client):
    cb.KEY_POOL.reload([])
    d = client.get("/health").json()
    assert d["api_keys_loaded"] == 0
    assert d["status"] == "degraded"
    assert d["next_key_index"] is None


def test_missing_q_is_422_validation_error(client):
    r = client.post("/v1/maps/search", json={"ll": "@1,2,3z"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_bad_ll_format_is_422(client):
    r = client.post("/v1/maps/search", json={"q": "x", "ll": "new york"})
    assert r.status_code == 422


def test_bulk_limit_exceeded_returns_422(client):
    body = {"requests": [{"q": f"q{i}"} for i in range(cb.MAX_BATCH_SIZE + 1)]}
    r = client.post("/v1/maps/search/bulk", json=body)
    assert r.status_code == 422
    assert "Too many requests" in str(r.json()["error"]["issues"])


def test_bulk_partial_failure_isolated(client, monkeypatch):
    cb.KEY_POOL.reload([REAL_FAKE])
    stub_upstream(monkeypatch)

    real_exec = cb.execute_tool

    async def flaky(slug, args, user_id=None):
        if args.get("q") == "boom":
            raise cb.HTTPException(504, detail=cb.error_body(504, "UPSTREAM_TIMEOUT", "injected"))
        return await real_exec(slug, args, user_id)

    monkeypatch.setattr(cb, "execute_tool", flaky)

    r = client.post("/v1/maps/search/bulk", json={"continue_on_error": True, "requests": [
        {"q": "pizza"}, {"q": "boom"}, {"q": "hotels"}]})
    d = r.json()
    assert r.status_code == 200
    assert (d["succeeded"], d["failed"]) == (2, 1)
    assert d["results"][1]["error"]["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_bulk_stop_on_error(client, monkeypatch):
    cb.KEY_POOL.reload([REAL_FAKE])
    stub_upstream(monkeypatch)

    async def always_fail(slug, args, user_id=None):
        raise cb.HTTPException(504, detail=cb.error_body(504, "UPSTREAM_TIMEOUT", "injected"))

    monkeypatch.setattr(cb, "execute_tool", always_fail)
    r = client.post("/v1/maps/search/bulk", json={"continue_on_error": False,
                                                  "requests": [{"q": "a"}, {"q": "b"}]})
    assert r.status_code == 504


def test_get_search_accepts_lat_lng_shorthand(client, monkeypatch):
    cb.KEY_POOL.reload([REAL_FAKE])
    stub_upstream(monkeypatch)
    r = client.get("/v1/maps/search", params={"q": "hotels", "lat": 35.6762, "lng": 139.6503})
    assert r.status_code == 200
    assert r.json()["ll"] == "@35.6762,139.6503,13z"


def test_generic_bulk_route_not_shadowed_by_slug_route(client, monkeypatch):
    cb.KEY_POOL.reload([REAL_FAKE])
    stub_upstream(monkeypatch, unknown_slugs={"NOPE"})
    r = client.post("/v1/tools/bulk/execute", json={"items": [
        {"slug": "maps", "arguments": {"q": "a"}},
        {"slug": "NOPE", "arguments": {}}]})
    assert r.status_code == 200
    d = r.json()
    assert d["succeeded"] == 1   # unknown slug fails per-item, not with 404 TOOL "BULK"


def test_keys_reload_endpoint(client, monkeypatch):
    # load_dotenv(override=True) would restore .env, so stub it out for this test
    monkeypatch.setattr(cb, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("COMPOSIO_API_KEY", "a1,a2")
    d = client.post("/v1/keys/reload").json()
    assert d["api_keys_loaded"] == 2
    assert [k["index"] for k in d["keys"]] == [1, 2]


def test_bearer_token_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(cb, "BRIDGE_API_TOKEN", "s3cret")
    assert client.post("/v1/maps/search", json=SEARCH).status_code == 401
    r = client.post("/v1/maps/search", json=SEARCH, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code != 401   # passes auth (upstream may still fail -> not 401)
    assert client.get("/health").status_code == 200   # health stays public
