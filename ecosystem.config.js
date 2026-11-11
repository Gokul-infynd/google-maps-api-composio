// PM2 process definition for the Composio REST bridge.
// Start with:  ./bridge.sh start      (or)  pm2 start ecosystem.config.js
// Logs:        ./bridge.sh logs   |   pm2 logs composio-rest-bridge
//
// Uses the project's uv-managed .venv so PM2 never depends on a global 'uv'.
// If .venv is missing, run 'uv sync' first (bridge.sh does this automatically).

const path = require("path");

const ROOT = __dirname;
const VENV_PYTHON = path.join(ROOT, ".venv", "bin", "python");

module.exports = {
  apps: [
    {
      name: "composio-rest-bridge",
      // Runs composio_bridge.main() -> uvicorn with HOST/PORT from .env
      script: VENV_PYTHON,
      args: ["-m", "composio_bridge"],
      cwd: ROOT,
      interpreter: "none",

      // Load .env so HOST / PORT / COMPOSIO_API_KEY etc. apply.
      // dotenv is loaded by the app itself too, but this makes PORT visible to PM2.
      env: {
        NODE_ENV: "production",
        PYTHONUNBUFFERED: "1",
        HOST: "0.0.0.0",
        PORT: "8000",
      },
      env_production: {
        HOST: "0.0.0.0",
        PORT: "8000",
      },

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
  ],
};
