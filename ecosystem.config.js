// PM2 process definition for the Composio REST bridge.
// Start with:  ./bridge.sh start      (or)  pm2 start ecosystem.config.js
// Logs:        ./bridge.sh logs   |   pm2 logs composio-rest-bridge
//
// Uses the project's uv-managed .venv so PM2 never depends on a global 'uv'.
// If .venv is missing, run 'uv sync' first (bridge.sh does this automatically).

const path = require("path");
const fs = require("fs");

const ROOT = __dirname;
const VENV_PYTHON = path.join(ROOT, ".venv", "bin", "python");

// Read a key from .env (fallback: default). Used for the UI port only -
// streamlit_app.py itself loads .env via python-dotenv for everything else.
function envFile(key, fallback) {
  try {
    const raw = fs.readFileSync(path.join(ROOT, ".env"), "utf8");
    const m = raw.match(new RegExp(`^${key}=(.*)$`, "m"));
    if (!m) return fallback;
    return m[1].trim().replace(/^["']|["']$/g, "") || fallback;
  } catch (e) {
    return fallback;
  }
}

const UI_PORT = process.env.UI_PORT || envFile("UI_PORT", "8501");
const UI_HOST = process.env.UI_HOST || envFile("UI_HOST", "0.0.0.0");

module.exports = {
  apps: [
    {
      name: "composio-rest-bridge",
      // Runs composio_bridge.main() -> uvicorn with HOST/PORT from .env
      script: VENV_PYTHON,
      args: ["-m", "composio_bridge"],
      cwd: ROOT,
      interpreter: "none",

      // Only set non-*.env vars here. HOST / PORT / COMPOSIO_API_KEY come from
      // .env (loaded by the app via load_dotenv). Do NOT hardcode PORT here:
      // PM2 env vars take precedence over .env, so a literal PORT would
      // silently override whatever you set in .env.
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
      env_production: {},

      // Single worker: the round-robin key pool lives in-process.
      // Running multiple instances would give each its own cursor/stats.
      instances: 1,
      exec_mode: "fork",

      autorestart: true,
      restart_delay: 3000,
      max_restarts: 10,
      min_uptime: "10s",
      max_memory_restart: "512M",
      kill_timeout: 8000,
      listen_timeout: 10000,

      out_file: path.join(ROOT, "logs", "out.log"),
      error_file: path.join(ROOT, "logs", "error.log"),
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      time: true,
    },
    {
      // Streamlit batch-processing UI (http://<host>:<UI_PORT>).
      // Requires the 'ui' extra:  uv sync --extra ui   (bridge.sh ui does this)
      // Start ONLY this app:      pm2 start ecosystem.config.js --only composio-maps-ui
      name: "composio-maps-ui",
      script: VENV_PYTHON,
      args: [
        "-m", "streamlit", "run", "streamlit_app.py",
        "--server.port", String(UI_PORT),
        "--server.address", String(UI_HOST),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
      ],
      cwd: ROOT,
      interpreter: "none",

      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
      env_production: {},

      instances: 1,
      exec_mode: "fork",

      autorestart: true,
      restart_delay: 3000,
      max_restarts: 10,
      min_uptime: "10s",
      max_memory_restart: "1G",
      kill_timeout: 8000,
      listen_timeout: 30000,

      out_file: path.join(ROOT, "logs", "out.log"),
      error_file: path.join(ROOT, "logs", "error.log"),
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      time: true,
    },
  ],
};
