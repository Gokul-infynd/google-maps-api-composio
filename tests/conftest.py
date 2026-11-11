"""Global test env: keep the UI side effects away from real directories.

composio_bridge's lifespan now calls ui_api.startup(), which creates the
sqlite DB, seeds users and starts the temp sweeper. Redirect all of that
into a throwaway temp dir BEFORE any test module imports the app.
"""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="maps_ui_test_")

os.environ.setdefault("UI_DB_PATH", os.path.join(_tmp, "ui.db"))
os.environ.setdefault("TEMP_DIR", os.path.join(_tmp, "temp"))
os.environ.setdefault("UI_USERNAME", "admin")
os.environ.setdefault("UI_PASSWORD", "test-password")
os.environ.setdefault("UI_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("UI_USERS", "bob:bob-pass")
