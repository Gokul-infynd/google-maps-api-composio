# Composio REST Bridge

Single-file FastAPI service that turns Composio tools into plain REST.

Primary tool: **Composio Search → Composio Google Maps Search**
slug `COMPOSIO_SEARCH_GOOGLE_MAPS_SEARCH` (not the Google Maps toolkit — no Google API key needed).

## Setup (uv + PM2)

Requires [uv](https://docs.astral.sh/uv/) and [PM2](https://pm2.keymetrics.io/) (`npm i -g pm2`).

```bash
git clone git@github.com:Gokul-infynd/google-maps-api-composio.git
cd google-maps-api-composio
cp .env.example .env          # set COMPOSIO_API_KEY=key1,key2,...

./bridge.sh start             # uv sync (if needed) + pm2 start + health check
```

### `bridge.sh` commands

| Command | Does |
|---|---|
| `./bridge.sh start` | create `.venv` via `uv sync` if missing, start under PM2, wait for health |
| `./bridge.sh stop` | stop the PM2 app |
| `./bridge.sh restart` | `uv sync` then restart (use after code or `.env` changes) |
| `./bridge.sh reload` | zero-downtime reload |
| `./bridge.sh status` / `logs` / `delete` | PM2 status / tail logs / remove |
| `./bridge.sh health` | pretty-print `/health` |
| `./bridge.sh test` | run the offline pytest suite |
| `./bridge.sh local` | run in the foreground (debugging, no PM2) |

Logs go to `logs/out.log` and `logs/error.log`.

### Raw uv commands

```bash
uv sync                  # create/refresh .venv from uv.lock
uv sync --extra dev      # + pytest
uv run composio-bridge   # console entry point (= python -m composio_bridge)
uv run pytest            # tests
```

> **One PM2 instance only.** The round-robin key cursor and per-key stats live
> in-process, so running `instances: N` would give each worker its own rotation.

## Multiple API keys (round-robin)

Add as many keys as you like, comma separated:

```env
COMPOSIO_API_KEY=ak_key1,ak_key2,ak_key3
COMPOSIO_RETRY_ON_KEY_FAILURE=true
```

Rotation is **round-robin**: request 1 → key 1, request 2 → key 2, request 3 → key 3, request 4 → key 1 …

- Duplicates and blank entries are ignored automatically.
- Concurrent requests (e.g. a bulk call) skip keys that are already in flight, so load spreads evenly instead of piling onto one key.
- `/health` shows `api_keys_loaded` plus per-key counters (masked keys).
- `POST /v1/keys/reload` re-reads `.env` without restarting the server.

### Failover

By default, if a key returns **401/403/429**, the same call is retried on the next key
(up to one attempt per key), so a dead or throttled key doesn't fail user requests.
If *every* key fails, you get a `COMPOSIO_AUTH_FAILED` / `COMPOSIO_RATE_LIMITED` error
whose `keys_tried` lists the attempted key numbers.

Set `COMPOSIO_RETRY_ON_KEY_FAILURE=false` to get strict rotation with no failover.


Swagger UI: http://127.0.0.1:8000/docs

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/health` | liveness + key-pool status (public) |
| GET  | `/v1/keys` | per-key counters |
| POST | `/v1/keys/reload` | reload keys from `.env` without restart |
| POST | `/v1/maps/search` | single search (JSON body) |
| GET  | `/v1/maps/search` | single search (query string) |
| POST | `/v1/maps/search/bulk` | **bulk**: 1..50 searches per call |
| POST | `/v1/tools/bulk/execute` | **bulk**: many tools (mixed slugs) per call |
| POST | `/v1/tools/{slug}/execute` | any Composio tool (alias `maps` supported) |
| GET  | `/v1/tools?search=maps` | discover tools in `composio_search` |
| GET  | `/v1/tools/{slug}/schema` | input/output JSON schema |

### Single
```bash
curl -X POST http://127.0.0.1:8000/v1/maps/search -H 'Content-Type: application/json' \
  -d '{"q":"pizza","ll":"@40.7455096,-74.0083012,14z"}'
```
Params: `q` (required), `ll` (`@lat,lng,zoom`), `start` (pagination), `user_id`.
Add `?shape=full` for raw Composio data.

### Bulk
```bash
curl -X POST 'http://127.0.0.1:8000/v1/maps/search/bulk?shape=slim' \
  -H 'Content-Type: application/json' -d '{
  "continue_on_error": true, "concurrency": 4,
  "requests": [
    {"q":"coffee","ll":"@12.9716,77.5946,13z"},
    {"q":"sushi","ll":"@51.5074,-0.1278,13z"}
  ]}'
```

## Responses & errors

Success: `{"ok":true,"places":[...],"count":20,"latency_ms":834,"log_id":"..."}`

Every error uses one shape:
```json
{"ok": false, "error": {"status": 422, "code": "TOOL_EXECUTION_FAILED", "message": "..."}}
```

| HTTP | code | meaning |
|---|---|---|
| 422 | `VALIDATION_ERROR` | bad request body |
| 422 | `TOOL_EXECUTION_FAILED` | Composio rejected the tool args |
| 404 | `TOOL_NOT_FOUND` | unknown tool slug |
| 429 | `COMPOSIO_RATE_LIMITED` | rate limited upstream |
| 502 | `COMPOSIO_AUTH_FAILED` | bad COMPOSIO_API_KEY |
| 504 | `UPSTREAM_TIMEOUT` | Composio timed out |

> Note: Composio returns **HTTP 200 with `successful:false`** for bad tool args.
> The bridge converts those into real 4xx statuses so your client code can rely on status codes.

## Protection

Set `BRIDGE_API_TOKEN` in `.env` to require `Authorization: Bearer <token>` on all routes except `/health`.
