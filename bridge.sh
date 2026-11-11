#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Composio REST Bridge - control script (PM2 + uv)
#
#   ./bridge.sh setup      setup env (venv + uv sync) and PM2 boot config
#   ./bridge.sh start      start (install deps first if the venv is missing)
#   ./bridge.sh stop       stop
#   ./bridge.sh restart    restart (e.g. after editing code / .env)
#   ./bridge.sh reload     0-downtime restart
#   ./bridge.sh status     PM2 status
#   ./bridge.sh logs       tail logs (Ctrl+C to exit)
#   ./bridge.sh delete     remove from PM2 entirely
#   ./bridge.sh health     curl /health
#   ./bridge.sh test       run the pytest suite
#   ./bridge.sh local      foreground run (not via PM2), for debugging
#   ./bridge.sh ui         start the Streamlit UI (uv sync --extra ui + PM2)
#   ./bridge.sh ui-stop    stop the Streamlit UI
#   ./bridge.sh ui-logs    tail the UI logs
#   ./bridge.sh batch      run the batch script: ./bridge.sh batch file.xlsx [args...]
# ---------------------------------------------------------------------------
set -euo pipefail

APP_NAME="composio-rest-bridge"
UI_APP_NAME="composio-maps-ui"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV_PY="$ROOT/.venv/bin/python"
ENV_FILE="$ROOT/.env"
PORT="${PORT:-$(grep -E '^PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' "' || true)}"
PORT="${PORT:-8000}"
UI_PORT="${UI_PORT:-$(grep -E '^UI_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' "' || true)}"
UI_PORT="${UI_PORT:-8501}"

c_green='\033[0;32m'; c_red='\033[0;31m'; c_yel='\033[0;33m'; c_off='\033[0m'
info() { printf "${c_green}==>${c_off} %s\n" "$*"; }
warn() { printf "${c_yel}!!${c_off} %s\n" "$*"; }
die()  { printf "${c_red}xx${c_off} %s\n" "$*" >&2; exit 1; }

need_pm2() {
  command -v pm2 >/dev/null 2>&1 || \
    die "pm2 not found. Install it with: npm install -g pm2"
}

need_env() {
  [ -f "$ENV_FILE" ] || die ".env missing. Copy .env.example -> .env and add COMPOSIO_API_KEY=key1,key2"
  if ! grep -qE '^[[:space:]]*COMPOSIO_API_KEY=[^[:space:]]+' "$ENV_FILE"; then
    die "COMPOSIO_API_KEY is not set in .env"
  fi
}

# Ensure the uv-managed venv exists.
# NOTE: always pass --extra ui - a plain `uv sync` prunes every package not
# selected by that sync, which UNINSTALLS streamlit (21 packages) and deletes
# its static files out from under a running UI -> HTTP 500 on the UI port.
ensure_venv() {
  command -v uv >/dev/null 2>&1 || die "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  if [ ! -x "$VENV_PY" ]; then
    info "No .venv found - running 'uv sync --extra ui'..."
    uv sync --extra ui
  else
    info "Checking environment (uv sync --extra ui...)"
    uv sync --extra ui --quiet
  fi
  [ -x "$VENV_PY" ] || die "uv sync did not create .venv/bin/python"
}

# Ensure the venv exists INCLUDING the Streamlit UI extra.
ensure_venv_ui() {
  command -v uv >/dev/null 2>&1 || die "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  info "Syncing environment with the 'ui' extra (streamlit)..."
  uv sync --extra ui --quiet
  [ -x "$VENV_PY" ] || die "uv sync did not create .venv/bin/python"
}

key_count() {
  grep -E '^[[:space:]]*COMPOSIO_API_KEY=' "$ENV_FILE" | tail -1 \
    | cut -d= -f2- | tr ',' '\n' | grep -c '[^[:space:]]' || true
}

wait_healthy() {
  command -v curl >/dev/null 2>&1 || return 0
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      info "Healthy on http://127.0.0.1:${PORT} (docs: http://127.0.0.1:${PORT}/docs)"
      return 0
    fi
    sleep 1
  done
  warn "Not responding on port ${PORT} yet - check: ./bridge.sh logs"
}

wait_ui() {
  command -v curl >/dev/null 2>&1 || return 0
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 -o /dev/null "http://127.0.0.1:${UI_PORT}/" 2>/dev/null; then
      info "UI up on http://127.0.0.1:${UI_PORT}"
      return 0
    fi
    sleep 1
  done
  warn "UI not responding on port ${UI_PORT} yet - check: ./bridge.sh ui-logs"
}

case "${1:-}" in
  setup)
    ensure_venv
    need_env
    need_pm2
    info "Loaded $(key_count) API key(s) from .env"
    mkdir -p logs
    pm2 start ecosystem.config.js --only "$APP_NAME" >/dev/null
    pm2 save >/dev/null && info "PM2 process list saved"
    wait_healthy
    ;;

  start)
    need_pm2; need_env; ensure_venv
    mkdir -p logs
    if pm2 describe "$APP_NAME" >/dev/null 2>&1; then
      info "Already registered in PM2 - starting it"
      pm2 start "$APP_NAME"
    else
      pm2 start ecosystem.config.js --only "$APP_NAME"
    fi
    git_commit() { git rev-parse --short HEAD 2>/dev/null || echo "n/a"; }
    info "Keys: $(key_count) | commit: $(git_commit)"
    wait_healthy
    ;;

  stop)
    need_pm2
    pm2 stop "$APP_NAME" && info "Stopped"
    pm2 stop "$UI_APP_NAME" >/dev/null 2>&1 && info "Stopped $UI_APP_NAME" || true
    ;;

  restart)
    need_pm2; need_env
    ensure_venv
    pm2 restart "$APP_NAME" --update-env && info "Restarted"
    if pm2 describe "$UI_APP_NAME" >/dev/null 2>&1; then
      pm2 restart "$UI_APP_NAME" --update-env && info "Restarted $UI_APP_NAME"
    fi
    wait_healthy
    ;;

  reload)
    need_pm2
    pm2 reload "$APP_NAME" --update-env && info "Reloaded (zero downtime)"
    ;;

  status|list)
    need_pm2
    pm2 status "$APP_NAME"
    ;;

  logs)
    need_pm2
    pm2 logs "$APP_NAME" --lines 100
    ;;

  delete)
    need_pm2
    pm2 delete "$APP_NAME" && info "Removed from PM2"
    ;;

  health)
    curl -fsS -m 10 "http://127.0.0.1:${PORT}/health" | "${PYTHON:-python3}" -m json.tool \
      || die "health check failed on port ${PORT}"
    ;;

  test)
    ensure_venv
    uv run --extra dev pytest
    ;;

  ui)
    need_pm2; ensure_venv_ui
    mkdir -p logs
    if pm2 describe "$UI_APP_NAME" >/dev/null 2>&1; then
      info "Restarting $UI_APP_NAME (--update-env)"
      pm2 restart "$UI_APP_NAME" --update-env
    else
      pm2 start ecosystem.config.js --only "$UI_APP_NAME"
    fi
    info "UI port: ${UI_PORT} (UI_PORT in .env)"
    wait_ui
    ;;

  ui-stop)
    need_pm2
    pm2 stop "$UI_APP_NAME" && info "Stopped $UI_APP_NAME"
    ;;

  ui-logs)
    need_pm2
    pm2 logs "$UI_APP_NAME" --lines 100
    ;;

  batch)
    ensure_venv
    [ $# -ge 2 ] || die "usage: ./bridge.sh batch <input.xlsx> [maps-batch args...]"
    shift  # drop the 'batch' word, pass the rest to maps-batch
    exec "$VENV_PY" -m batch_processor "$@"
    ;;

  local)
    need_env; ensure_venv
    info "Foreground mode (Ctrl+C to quit)"
    exec "$VENV_PY" -m composio_bridge
    ;;

  ui-local)
    ensure_venv_ui
    info "Streamlit foreground mode on http://127.0.0.1:${UI_PORT} (Ctrl+C to quit)"
    exec "$VENV_PY" -m streamlit run streamlit_app.py \
      --server.port "$UI_PORT" --server.headless true --browser.gatherUsageStats false
    ;;

  *)
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
