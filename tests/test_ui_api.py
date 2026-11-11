"""UI API: JWT auth, login, uploads, background jobs, per-user history."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import batch_processor as bp
import ui_api


# --------------------------------------------------------------------------
# Unit: passwords + JWT (stdlib HS256)
# --------------------------------------------------------------------------


def test_password_hash_roundtrip():
    stored = ui_api.hash_password("hunter2!")
    assert stored.startswith("pbkdf2_sha256$")
    assert ui_api.check_password("hunter2!", stored)
    assert not ui_api.check_password("wrong", stored)
    assert not ui_api.check_password("hunter2!", "garbage")


def test_jwt_roundtrip_and_rejection(monkeypatch):
    monkeypatch.setattr(ui_api, "JWT_SECRET", "unit-test-secret")
    token = ui_api.make_token("alice")
    assert ui_api.verify_token(token) == "alice"

    # expired tokens are rejected
    expired = ui_api.make_token("alice", expires_in=-5)
    assert ui_api.verify_token(expired) is None

    # tampered signature / payload are rejected
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload}.{signature[:-2]}xy"
    assert ui_api.verify_token(forged) is None
    assert ui_api.verify_token("not-a-token") is None
    assert ui_api.verify_token("") is None

    # a different secret cannot verify it
    monkeypatch.setattr(ui_api, "JWT_SECRET", "other-secret")
    assert ui_api.verify_token(token) is None


# --------------------------------------------------------------------------
# App flow: login -> upload -> job -> history -> download
# --------------------------------------------------------------------------


@pytest.fixture()
def client():
    import composio_bridge

    with TestClient(composio_bridge.app) as c:
        yield c


def _login(client: TestClient, username: str = "admin", password: str = "test-password") -> str:
    res = client.post("/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_pages_are_served(client):
    res = client.get("/login")
    assert res.status_code == 200
    assert "password" in res.text.lower()

    res = client.get("/home")
    assert res.status_code == 200
    assert "Google Maps Batch Search" in res.text

    # the old /ui URL still works - it redirects to /home
    res = client.get("/ui", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/home"

    res = client.get("/", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/home"


def test_login_validation_and_auth_gate(client):
    # wrong password -> 401
    res = client.post("/auth/login", json={"username": "admin", "password": "nope"})
    assert res.status_code == 401

    # unknown user -> 401 (same shape, no user enumeration)
    res = client.post("/auth/login", json={"username": "ghost", "password": "nope"})
    assert res.status_code == 401

    # correct credentials -> token works on protected endpoints
    token = _login(client)
    assert client.get("/ui/api/me", headers=_auth(token)).json()["username"] == "admin"

    # no token / garbage token -> 401
    assert client.get("/ui/api/history").status_code == 401
    assert client.get("/ui/api/history",
                      headers=_auth("garbage")).status_code == 401
    assert client.post("/ui/api/uploads?filename=x.xlsx").status_code == 401


def test_config_endpoint(client):
    token = _login(client)
    cfg = client.get("/ui/api/config", headers=_auth(token)).json()
    assert cfg["shape"] in ("full", "slim")
    assert cfg["ttl_minutes"] > 0
    assert cfg["max_upload_mb"] >= 1


def _sample_xlsx() -> bytes:
    df = pd.DataFrame({"City": ["Paris", "Lisbon"], "Item": ["coffee", "pizza"]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _fake_process_file(**kwargs):
    """Stand-in for bp.process_file: writes an output workbook, no network."""
    out = Path(kwargs["output_path"])
    sheet = kwargs.get("sheet") or "Sheet1"
    df = pd.read_excel(kwargs["input_path"], sheet_name=sheet)
    df["maps_status"] = "ok"
    df["maps_title"] = [f"T-{c}" for c in df.get("City", df.index)]
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet, index=False)
    summary = {
        "rows_total": len(df), "rows_in_range": len(df), "rows_processed": len(df),
        "rows_succeeded": len(df), "rows_failed": 0, "rows_skipped": 0,
        "rows_out_of_range": 0, "range": [1, len(df)], "batches": 1,
        "shape": kwargs.get("shape", "full"), "extra_columns": ["maps_title"],
        "stopped": False, "elapsed_seconds": 0.01, "output": str(out),
    }
    cb = kwargs.get("progress_cb")
    if cb:
        cb(bp.ProgressInfo(total=len(df), done=len(df), succeeded=len(df), failed=0,
                           message="done", elapsed=0.01, finished=True, summary=summary))
    return summary


def _upload(client, token: str, payload: bytes, filename: str = "sample.xlsx") -> dict:
    res = client.post(f"/ui/api/uploads?filename={filename}", content=payload,
                      headers={**_auth(token), "Content-Type": "application/octet-stream"})
    assert res.status_code == 200, res.text
    return res.json()


def test_upload_rejects_unreadable_file(client):
    token = _login(client)
    res = client.post("/ui/api/uploads?filename=broken.xlsx", content=b"not a spreadsheet",
                      headers={**_auth(token), "Content-Type": "application/octet-stream"})
    assert res.status_code == 400


def test_full_job_flow_with_history_and_download(client, monkeypatch):
    token = _login(client)

    # 1. upload -> workbook info
    info = _upload(client, token, _sample_xlsx())
    assert info["sheets"] == ["Sheet1"]
    assert info["sheet_columns"]["Sheet1"] == ["City", "Item"]
    assert info["sheet_rows"]["Sheet1"] == 2
    assert info["sheet_detected"]["Sheet1"]["query"] is None  # no auto match
    assert info["previews"]["Sheet1"][0]["City"] == "Paris"

    # 2. start a background job (process_file stubbed - no network)
    monkeypatch.setattr(ui_api.bp, "process_file", _fake_process_file)
    res = client.post("/ui/api/jobs", headers=_auth(token), json={
        "upload_id": info["upload_id"], "sheet": "Sheet1",
        "query_columns": ["City", "Item"], "shape": "full",
    })
    assert res.status_code == 200, res.text
    job_id = res.json()["job_id"]
    assert res.json()["rows_in_range"] == 2

    # 3. poll until the worker finishes (runs even though no page is open)
    job = None
    for _ in range(200):
        job = client.get(f"/ui/api/jobs/{job_id}", headers=_auth(token)).json()
        if job["status"] in ("done", "stopped", "error"):
            break
        time.sleep(0.05)
    assert job is not None and job["status"] == "done", job
    assert job["summary"]["rows_succeeded"] == 2
    assert job["output"]["columns"] == ["City", "Item", "maps_status", "maps_title"]
    assert job["output"]["rows"][0]["maps_title"] == "T-Paris"
    assert job["download_url"]

    # 4. history for this user contains the run
    items = client.get("/ui/api/history", headers=_auth(token)).json()["items"]
    match = [i for i in items if i["id"] == job_id]
    assert match, items
    assert match[0]["status"] == "done"
    assert match[0]["rows_succeeded"] == 2
    assert match[0]["shape"] == "full"

    # 5. download returns the workbook bytes
    res = client.get(f"/ui/api/jobs/{job_id}/download", headers=_auth(token))
    assert res.status_code == 200
    assert res.content[:2] == b"PK"  # xlsx = zip container

    # 6. starting the same upload twice is rejected
    res = client.post("/ui/api/jobs", headers=_auth(token), json={
        "upload_id": info["upload_id"], "sheet": "Sheet1",
        "query_columns": ["City"], "shape": "full",
    })
    assert res.status_code == 409

    # 7. duplicate (re-run with another range) creates a fresh draft
    res = client.post(f"/ui/api/jobs/{job_id}/duplicate", headers=_auth(token))
    assert res.status_code == 200
    assert res.json()["status"] == "draft"
    assert res.json()["upload_id"] != job_id


def test_jobs_are_private_per_user(client, monkeypatch):
    admin_token = _login(client)
    info = _upload(client, admin_token, _sample_xlsx())
    monkeypatch.setattr(ui_api.bp, "process_file", _fake_process_file)
    job_id = client.post("/ui/api/jobs", headers=_auth(admin_token), json={
        "upload_id": info["upload_id"], "sheet": "Sheet1",
        "query_columns": ["City"], "shape": "slim",
    }).json()["job_id"]

    # a second user (seeded via UI_USERS) sees nothing of the first user's run
    bob = _login(client, "bob", "bob-pass")
    items = client.get("/ui/api/history", headers=_auth(bob)).json()["items"]
    assert all(i["id"] != job_id for i in items)
    assert client.get(f"/ui/api/jobs/{job_id}", headers=_auth(bob)).status_code == 404
    assert client.get(f"/ui/api/jobs/{job_id}/download",
                      headers=_auth(bob)).status_code == 404
    assert client.post(f"/ui/api/jobs/{job_id}/stop",
                       headers=_auth(bob)).status_code == 404
