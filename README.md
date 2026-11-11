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
| `./bridge.sh batch file.xlsx …` | run the batch script directly (see *Batch file processing*) |

The batch UI is served **by the bridge itself** on the same port as `/docs`
(no second process): `http://127.0.0.1:8000/login` and `http://127.0.0.1:8000/home`.

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

### Per-key rate limit (2000 calls/min) with key cooldown

Every key gets its **own sliding-window budget**: `RATE_LIMIT_PER_KEY_PER_MINUTE`
calls per `RATE_LIMIT_WINDOW_SECONDS` (default **2000 calls/minute per key**).

- A key that exhausts its budget goes into **cooldown** and is *skipped* by the
  router until its oldest call leaves the window - traffic moves to the other keys.
- Only when **every** key is cooling down (or in flight) does the request wait -
  asynchronously, bounded by `RATE_LIMIT_MAX_WAIT_SECONDS` (default 65s) - and
  then fail with `429 KEY_POOL_COOLING_DOWN` (all keys rate limited) or
  `503 KEY_POOL_BUSY` (all keys in flight) instead of hanging.
- Upstream 429s from Composio still fail over to the next key as before.

```env
RATE_LIMIT_PER_KEY_PER_MINUTE=2000   # 0 = unlimited
RATE_LIMIT_WINDOW_SECONDS=60
RATE_LIMIT_MAX_WAIT_SECONDS=65
```

`GET /health` reports `rate_limit_per_key_per_minute`, `rate_limit_window_seconds`
and - per key - `window_used`, `window_limit` and `cooldown_seconds` (time until
the key has a free slot again).


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

Each bulk item may carry an **`id`** (e.g. the spreadsheet row) - it is echoed
back on the matching result slot, which is how the batch script correlates
results to rows.

## Batch file processing (`maps-batch`)

`batch_processor.py` reads an Excel/CSV file, runs every row through
`POST /v1/maps/search/bulk` and writes the results back into the **same row**
(new `maps_*` columns - input columns are never touched).

```bash
./bridge.sh batch input.xlsx                          # = uv run maps-batch input.xlsx
uv run maps-batch input.xlsx -o out.xlsx
uv run maps-batch input.xlsx --start-row 11 --end-row 60   # only rows 11..60 (1-based, inclusive)
uv run maps-batch input.xlsx --query-column "Search Term" --lat-column Latitude --lng-column Longitude
uv run maps-batch input.xlsx --query-column City,Item --shape full   # append columns into one query
```

- **Auto-detected input columns** (override with flags): `q`/`query`/`search term`, `ll`
  or `lat`+`lng`(+`zoom`), `start`, `user_id`. Rows with an empty query or an invalid
  location are marked per-row (`maps_status=invalid_input`) and never break the batch.
- **Multi-column query**: pass `--query-column` more than once or comma-separate
  (`--query-column City,Item`) - the cell values of the selected columns are appended
  with `QUERY_JOINER` (default a space) into one query, e.g. `City=Paris` +
  `Item=coffee` → `Paris coffee`. Empty cells are skipped.
- **Row range**: `--start-row` / `--end-row` (1-based, inclusive; row 1 = first row
  under the header). Rows outside the range are left untouched (`maps_status=not_processed`).
- **Response mode**: `--shape full` (default, or `PROCESSOR_SHAPE=full`) writes back
  **every field the endpoint returns** as its own `maps_<field>` column; `--shape slim`
  requests the compact place payload instead.
- Retries 429/5xx/transport errors with exponential backoff (`PROCESSOR_*` env vars).

Output columns written per row:

| Column | Meaning |
|---|---|
| `maps_status` | `ok` · `no_results` · `failed` · `invalid_input` · `not_processed` |
| `maps_count` | number of places found |
| `maps_title`, `maps_address`, `maps_type`, `maps_types` | top result |
| `maps_rating`, `maps_reviews`, `maps_price` | rating / review count / price level |
| `maps_phone`, `maps_website`, `maps_hours`, `maps_open_state` | contact + hours |
| `maps_place_id`, `maps_data_id`, `maps_latitude`, `maps_longitude` | identifiers + GPS |
| `maps_thumbnail`, `maps_position`, `maps_description` | extras |
| `maps_all_places_json` | **all** results of the row as JSON |
| `maps_latency_ms`, `maps_log_id` | per-item telemetry |
| `maps_error` | error message when the search failed |
| `maps_<field>` (dynamic) | **every other field** the endpoint returned for the top result (`maps_country`, `maps_operating_hours`, `maps_provider_id`, `maps_order_online`, `maps_reviews_link`, …) - added as columns on demand |
| `maps_search_information_json` | search metadata (`shape=full`) |

Cell values are clipped to Excel's 32 767-char limit; `maps_all_places_json`
automatically falls back to slimmed places when the full payload would not fit.

Other sheets in the workbook are copied through unchanged. Exit codes: `0` all rows
ok, `1` some rows failed, `2` fatal (bridge down, bad file, …).

## Web UI (`/login` + `/home`)

The UI is plain **HTML + CSS + JS served by the FastAPI bridge itself** - same
process, same port as `/docs`:

```bash
./bridge.sh restart                      # the bridge serves the UI
# open http://127.0.0.1:8000/login        # username/password -> JWT
#      http://127.0.0.1:8000/home          # the batch app (redirects to /login)
```

**Authentication (JWT)**

- First start creates a bootstrap admin: `UI_USERNAME` (default `admin`) +
  `UI_PASSWORD`. If `UI_PASSWORD` is empty, a random one is generated and
  printed **once** in the logs.
- Extra users: `UI_USERS=user1:pass1,user2:pass2`. Passwords are stored as
  PBKDF2-SHA256 hashes in the sqlite DB; login returns an HS256 JWT
  (`UI_JWT_SECRET`, generated+persisted when unset;
  `UI_JWT_EXPIRE_MINUTES`, default 720).
- More than 5 failed logins per username within 5 minutes -> HTTP 429.

**Per-user run history (sqlite)**

- Every started run is persisted in `UI_DB_PATH` (default `./data/ui.db`):
  file, sheet, mode, row range, status (`queued/running/done/stopped/error`),
  row counters, timestamps and the processing summary.
- The **History** view lists only *your* runs; other users' jobs return 404.

**Runs survive a closed page**

- Processing executes in a **server-side background thread** - closing the
  browser tab does not stop it. Reopen `/home` (auto-re-attach) or pick the run
  from **History** to watch live progress, stop it, or download the result.
- If the *server* restarts mid-run, the job is marked `error`
  ("Processing was interrupted by a server restart") so history never shows a
  phantom running state.

Flow: **upload file → sheet + column mapping (auto-detected, overridable) → pick the
row range + response mode → Start**. The UI then shows a live progress bar, per-row
preview table and counters (with a **Stop** button), and finally offers the processed
file as a download.

- **Query column(s) = checkboxes**: every column of the sheet is listed with a checkbox -
  tick as many as you like; their cell values are appended (`QUERY_JOINER`, default a
  space) into a single query per row (e.g. *City* + *Item* → `Paris coffee`).
  All other fields (location, lat/lng, …) use single-column selects.
- **Response mode radio**: `full` (default - every endpoint field becomes a `maps_*`
  column) or `slim` (compact payload, smaller file).
- The file is uploaded to a **server temp dir** (`TEMP_DIR`, default
  `<system tmp>/composio_bridge/jobs/…`) and erased after `TEMP_FILE_TTL_MINUTES`
  (default 60) by a background sweeper that runs every `TEMP_CLEANUP_INTERVAL_SECONDS`
  (history rows outlive their files).
- The sidebar shows live bridge status (keys, rate limit, max batch size).
- Bridge URL is configurable in the sidebar (`BRIDGE_URL` in `.env` by default).

```env
BRIDGE_URL=http://127.0.0.1:8000
PROCESSOR_SHAPE=full           # full = all endpoint fields as columns | slim
QUERY_JOINER=                  # separator for multi-column queries (default " ")
TEMP_DIR=                      # empty = <system tmp>/composio_bridge
TEMP_FILE_TTL_MINUTES=60
TEMP_CLEANUP_INTERVAL_SECONDS=300
UI_USERNAME=admin
UI_PASSWORD=                   # empty = random, logged once on first start
UI_USERS=                      # extra logins: user1:pass1,user2:pass2
UI_JWT_SECRET=                 # empty = generated + stored in the DB
UI_JWT_EXPIRE_MINUTES=720
UI_DB_PATH=./data/ui.db
UI_MAX_UPLOAD_MB=200
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
