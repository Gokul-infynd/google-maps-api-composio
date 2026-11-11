"""
Offline tests for the batch processor: column detection, row ranges, per-row
write-back, file round-trips and bridge-call retries. The bridge HTTP call is
always stubbed - no network access.

Run:  uv run pytest
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import batch_processor as bp


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def place(title: str) -> dict:
    return {"title": title, "address": f"Addr of {title}", "rating": 4.5, "reviews": 10,
            "gps_coordinates": {"latitude": 1.5, "longitude": 2.5},
            "place_id": "p-1", "phone": "123", "website": "https://x.test"}


def fake_post(payload: dict, **_kw) -> dict:
    """Stub of POST /v1/maps/search/bulk: echoes one place per request."""
    results = []
    for req in payload["requests"]:
        results.append({"index": len(results), "id": req.get("id"), "ok": True,
                        "data": [place(f"T-{req['q']}")], "latency_ms": 7, "log_id": "log-1"})
    return {"ok": True, "total": len(results), "succeeded": len(results), "failed": 0,
            "results": results}


def empty_post(payload: dict, **_kw) -> dict:
    return {"ok": True, "total": len(payload["requests"]), "succeeded": len(payload["requests"]),
            "failed": 0,
            "results": [{"id": req.get("id"), "ok": True, "data": [], "latency_ms": 1,
                         "log_id": "l"} for req in payload["requests"]]}


# --------------------------------------------------------------------------
# Column detection
# --------------------------------------------------------------------------


def test_detect_columns_finds_common_aliases():
    df = pd.DataFrame(columns=["Search Term", "Latitude", "Longitude", "Page", "Row ID"])
    found = bp.detect_columns(df)
    assert found["query"] == "Search Term"
    assert found["lat"] == "Latitude"
    assert found["lng"] == "Longitude"
    assert found["start"] == "Page"
    assert found["id"] == "Row ID"


def test_detect_columns_plain_names_and_normalisation():
    df = pd.DataFrame(columns=["Q", "ll", "s.no", "Zoom Level"])
    found = bp.detect_columns(df)
    assert found["query"] == "Q"
    assert found["ll"] == "ll"
    assert found["id"] == "s.no"
    assert found["zoom"] == "Zoom Level"


def test_merge_mapping_overrides_including_explicit_none():
    detected = {"query": "q", "ll": None, "lat": "lat", "lng": None,
                "zoom": None, "start": None, "user_id": None, "id": None}
    mapping = bp.merge_mapping(detected, {"query": "term", "lat": "(none)", "lng": "longitude"})
    assert mapping.query == "term"
    assert mapping.lat is None          # explicitly cleared
    assert mapping.lng == "longitude"
    assert mapping.start is None


def test_merge_mapping_splits_comma_separated_query_and_keeps_lists():
    columns = ["City", "Item", "City, State"]
    detected = bp.detect_columns(pd.DataFrame(columns=columns))
    # comma-separated string -> list of columns
    assert bp.merge_mapping(detected, {"query": "City,Item"}, columns=columns).query == ["City", "Item"]
    # list stays a list
    assert bp.merge_mapping(detected, {"query": ["City", "Item"]}, columns=columns).query == ["City", "Item"]
    # a single column whose NAME contains a comma is not split
    assert bp.merge_mapping(detected, {"query": "City, State"}, columns=columns).query == "City, State"
    # empty selection disables the field
    assert bp.merge_mapping(detected, {"query": []}, columns=columns).query is None


def test_build_arguments_appends_multiple_query_columns():
    m = bp.ColumnMapping(query=["City", "Item"])
    args, err = bp.build_arguments({"City": "Paris", "Item": "coffee"}, m)
    assert err is None and args["q"] == "Paris coffee"
    # empty cells are skipped, not fatal
    args, err = bp.build_arguments({"City": None, "Item": "coffee"}, m)
    assert err is None and args["q"] == "coffee"
    # all selected columns empty -> invalid row
    assert bp.build_arguments({"City": "", "Item": ""}, m)[0] is None
    # single column still works
    assert bp.build_arguments({"City": "Lisbon", "Item": "coffee"},
                              bp.ColumnMapping(query="City"))[0]["q"] == "Lisbon"


# --------------------------------------------------------------------------
# Row -> arguments
# --------------------------------------------------------------------------


def test_build_arguments_ll_passthrough_and_zoom_default():
    m = bp.ColumnMapping(query="q", ll="loc")
    args, err = bp.build_arguments({"q": "  pizza ", "loc": "@40.5,-74.1"}, m)
    assert err is None
    assert args == {"q": "pizza", "ll": f"@40.5,-74.1,{bp.DEFAULT_ZOOM}z"}

    args, err = bp.build_arguments({"q": "x", "loc": "40.5,-74.1,12z"}, m)
    assert err is None and args["ll"] == "@40.5,-74.1,12z"


def test_build_arguments_composes_lat_lng_columns():
    m = bp.ColumnMapping(query="q", lat="la", lng="lo", zoom="z")
    args, err = bp.build_arguments({"q": "sushi", "la": "35.67", "lo": "139.65", "z": "13"}, m)
    assert err is None and args["ll"] == "@35.67,139.65,13z"


def test_build_arguments_rejects_bad_location_per_row():
    m = bp.ColumnMapping(query="q", ll="loc")
    args, err = bp.build_arguments({"q": "x", "loc": "new york"}, m)
    assert args is None and "invalid location" in err


def test_build_arguments_empty_or_missing_query():
    m = bp.ColumnMapping(query="q")
    assert bp.build_arguments({"q": ""}, m)[0] is None
    assert bp.build_arguments({"q": None}, m)[0] is None
    no_col = bp.ColumnMapping(query=None)
    assert bp.build_arguments({"q": "x"}, no_col)[0] is None


def test_build_arguments_forwards_start_and_skips_out_of_range():
    m = bp.ColumnMapping(query="q", start="pg")
    assert bp.build_arguments({"q": "x", "pg": "15"}, m)[0]["start"] == 15
    assert "start" not in bp.build_arguments({"q": "x", "pg": "0"}, m)[0]
    assert "start" not in bp.build_arguments({"q": "x", "pg": "abc"}, m)[0]


# --------------------------------------------------------------------------
# Dataframe processing
# --------------------------------------------------------------------------


def test_process_dataframe_writes_results_into_same_row():
    df = pd.DataFrame({"q": ["pizza", "", "sushi"], "note": ["a", "b", "c"]})
    out, summary = bp.process_dataframe(df, bp.ColumnMapping(query="q"), post_bulk_fn=fake_post)

    assert out.loc[0, "maps_status"] == "ok"
    assert out.loc[0, "maps_title"] == "T-pizza"
    assert out.loc[0, "maps_count"] == 1
    assert out.loc[0, "maps_latitude"] == 1.5
    assert json.loads(out.loc[0, "maps_all_places_json"])[0]["title"] == "T-pizza"
    assert out.loc[0, "note"] == "a"                     # input column untouched

    assert out.loc[1, "maps_status"] == "invalid_input"  # empty query never hits the bridge
    assert out.loc[2, "maps_title"] == "T-sushi"

    assert summary["rows_succeeded"] == 2
    assert summary["rows_failed"] == 1
    assert summary["rows_processed"] == 3


def test_process_dataframe_row_range_only_sends_selected_rows():
    df = pd.DataFrame({"q": [f"q{i}" for i in range(10)]})
    sent: list[str] = []

    def capture(payload, **kw):
        sent.extend(r["q"] for r in payload["requests"])
        return empty_post(payload, **kw)

    out, summary = bp.process_dataframe(df, bp.ColumnMapping(query="q"),
                                        post_bulk_fn=capture, start_row=3, end_row=5)
    assert sent == ["q2", "q3", "q4"]
    assert summary["range"] == [3, 5]
    assert summary["rows_in_range"] == 3
    assert summary["rows_out_of_range"] == 7
    assert list(out.loc[:1, "maps_status"]) == ["not_processed"] * 2
    assert list(out.loc[5:, "maps_status"]) == ["not_processed"] * 5
    assert list(out.loc[2:4, "maps_status"]) == ["no_results"] * 3


def test_process_dataframe_full_file_by_default():
    df = pd.DataFrame({"q": ["a", "b"]})
    _out, summary = bp.process_dataframe(df, bp.ColumnMapping(query="q"), post_bulk_fn=empty_post)
    assert summary["rows_in_range"] == 2 and summary["rows_out_of_range"] == 0
    assert summary["range"] == [1, 2]


def test_process_dataframe_empty_range_raises():
    df = pd.DataFrame({"q": ["a", "b"]})
    with pytest.raises(ValueError, match="Row range is empty"):
        bp.process_dataframe(df, bp.ColumnMapping(query="q"), start_row=9, end_row=20,
                             post_bulk_fn=fake_post)


def test_stop_callback_halts_after_current_batch():
    df = pd.DataFrame({"q": [f"q{i}" for i in range(6)]})
    calls = {"n": 0}

    def counting_post(payload, **kw):
        calls["n"] += 1
        return empty_post(payload, **kw)

    out, summary = bp.process_dataframe(df, bp.ColumnMapping(query="q"), batch_size=2,
                                        post_bulk_fn=counting_post,
                                        stop_cb=lambda: calls["n"] >= 1)
    assert calls["n"] == 1
    assert summary["stopped"] is True
    assert summary["rows_processed"] == 2
    assert summary["rows_skipped"] == 4
    assert list(out["maps_status"]) == ["no_results"] * 2 + ["not_processed"] * 4


def test_failed_bridge_item_marks_row_failed():
    df = pd.DataFrame({"q": ["a"]})

    def failing_post(payload, **_kw):
        return {"ok": True, "results": [
            {"id": payload["requests"][0]["id"], "ok": False,
             "error": {"error": {"code": "TOOL_EXECUTION_FAILED", "message": "boom"}}}]}

    out, summary = bp.process_dataframe(df, bp.ColumnMapping(query="q"), post_bulk_fn=failing_post)
    assert out.loc[0, "maps_status"] == "failed"
    assert out.loc[0, "maps_error"] == "TOOL_EXECUTION_FAILED: boom"
    assert summary["rows_failed"] == 1


def test_progress_callback_reports_counts_and_finishes():
    df = pd.DataFrame({"q": ["a", "b"]})
    seen: list[bp.ProgressInfo] = []
    bp.process_dataframe(df, bp.ColumnMapping(query="q"), post_bulk_fn=fake_post,
                         progress_cb=seen.append)
    assert seen[0].message == "starting" and seen[0].done == 0
    final = seen[-1]
    assert final.finished is True and final.done == 2 and final.succeeded == 2
    assert final.summary["rows_total"] == 2
    assert final.preview and final.preview[0]["query"] == "a"


# --------------------------------------------------------------------------
# Response mode (slim / full) and full field write-back
# --------------------------------------------------------------------------


def full_shape_post(payload: dict, **_kw) -> dict:
    """Stub of the bulk endpoint in shape=full: raw payload with extra fields."""
    results = []
    for i, req in enumerate(payload["requests"]):
        results.append({
            "index": i, "id": req.get("id"), "ok": True, "latency_ms": 3, "log_id": "L",
            "data": {"results": {
                "local_results": [{"title": f"T-{req['q']}", "address": "Addr",
                                   "country": "US", "provider_id": "p-9",
                                   "operating_hours": {"Monday": "9-5"},
                                   "order_online": True}],
                "search_information": {"query_displayed": req["q"]},
            }},
        })
    return {"ok": True, "total": len(results), "succeeded": len(results), "failed": 0,
            "results": results}


def test_full_shape_writes_every_endpoint_field_as_its_own_column():
    df = pd.DataFrame({"q": ["x"]})
    out, summary = bp.process_dataframe(df, bp.ColumnMapping(query="q"),
                                        post_bulk_fn=full_shape_post, shape="full")
    # curated columns still filled from the top result
    assert out.loc[0, "maps_title"] == "T-x"
    assert out.loc[0, "maps_address"] == "Addr"
    # every OTHER field the endpoint returned becomes its own column
    assert out.loc[0, "maps_country"] == "US"
    assert out.loc[0, "maps_provider_id"] == "p-9"
    assert json.loads(out.loc[0, "maps_operating_hours"])["Monday"] == "9-5"
    assert out.loc[0, "maps_order_online"] in (True, "True")
    assert json.loads(out.loc[0, "maps_search_information_json"])["query_displayed"] == "x"
    # and they are reported in the summary
    assert "maps_country" in summary["extra_columns"]
    assert summary["shape"] == "full"


def test_preview_rows_carry_every_output_field():
    """Live preview rows must contain ALL fields that go into the file."""
    df = pd.DataFrame({"q": ["x"]})
    seen: list[bp.ProgressInfo] = []
    bp.process_dataframe(df, bp.ColumnMapping(query="q"), post_bulk_fn=full_shape_post,
                         progress_cb=seen.append)
    preview = seen[-1].preview
    assert preview, "expected preview rows"
    row = preview[0]
    assert row["row"] == 1 and row["query"] == "x"
    assert row["maps_title"] == "T-x"          # curated field
    assert row["maps_country"] == "US"         # dynamic field
    assert row["maps_status"] == "ok" and row["maps_error"] is None


def test_shape_is_validated_and_passed_to_the_bridge_call():
    df = pd.DataFrame({"q": ["a"]})
    seen: dict[str, Any] = {}

    def capture(payload, shape=None, **_kw):
        seen["shape"] = shape
        return empty_post(payload)

    bp.process_dataframe(df, bp.ColumnMapping(query="q"), post_bulk_fn=capture, shape="slim")
    assert seen["shape"] == "slim"
    with pytest.raises(ValueError, match="Invalid shape"):
        bp.process_dataframe(df, bp.ColumnMapping(query="q"), shape="tiny",
                             post_bulk_fn=capture)


def test_post_bulk_puts_shape_in_the_url(monkeypatch):
    seen: list[str] = []

    def fake(url, json=None, headers=None, timeout=None):
        seen.append(url)
        return FakeResp(200, {"ok": True, "results": []})

    monkeypatch.setattr(bp.httpx, "post", fake)
    bp.post_bulk({"requests": []}, bearer=None, shape="full")
    bp.post_bulk({"requests": []}, bearer=None, shape="slim")
    assert seen[0].endswith("shape=full") and seen[1].endswith("shape=slim")


def test_places_json_falls_back_when_too_big_for_an_excel_cell():
    huge = [{"title": "x" * 500, "description": "y" * 500} for _ in range(60)]
    dump = bp._places_json(huge)
    assert len(dump) <= bp.CELL_LIMIT
    # normal sized payloads stay complete and valid JSON
    ok = bp._places_json([{"title": "a"}, {"title": "b"}])
    assert json.loads(ok)[1]["title"] == "b"


# --------------------------------------------------------------------------
# File round-trips
# --------------------------------------------------------------------------


def test_process_file_excel_preserves_other_sheets(tmp_path: Path):
    src = tmp_path / "input.xlsx"
    with pd.ExcelWriter(src, engine="openpyxl") as w:
        pd.DataFrame({"Search Term": ["pizza", "sushi"],
                      "ll": ["@40.7,-74.0,13z", ""]}).to_excel(w, sheet_name="Data", index=False)
        pd.DataFrame({"meta": ["keep me"]}).to_excel(w, sheet_name="Meta", index=False)

    summary = bp.process_file(src, tmp_path / "out.xlsx", post_bulk_fn=fake_post)
    assert summary["sheet"] == "Data"
    assert summary["columns"]["query"] == "Search Term"

    sheets = bp.read_workbook(tmp_path / "out.xlsx")
    assert list(sheets["Meta"]["meta"]) == ["keep me"]        # untouched sheet
    assert "maps_title" in sheets["Data"].columns
    assert sheets["Data"].loc[0, "maps_title"] == "T-pizza"
    assert list(sheets["Data"]["Search Term"]) == ["pizza", "sushi"]  # input preserved
    assert summary["output"] == str(tmp_path / "out.xlsx")


def test_process_file_row_range_and_output_suffix(tmp_path: Path):
    src = tmp_path / "in.xlsx"
    pd.DataFrame({"q": [f"q{i}" for i in range(5)]}).to_excel(src, index=False, engine="openpyxl")

    summary = bp.process_file(src, start_row=2, end_row=3, post_bulk_fn=empty_post)
    out_path = Path(summary["output"])
    assert out_path.name == "in_processed.xlsx"
    out = pd.read_excel(out_path, dtype=str)
    assert list(out["maps_status"]) == ["not_processed", "no_results", "no_results",
                                        "not_processed", "not_processed"]


def test_process_file_csv(tmp_path: Path):
    src = tmp_path / "in.csv"
    src.write_text("q\npizza\nsushi\n", encoding="utf-8")
    summary = bp.process_file(src, post_bulk_fn=fake_post)
    text = Path(summary["output"]).read_text(encoding="utf-8")
    header, *lines = text.strip().splitlines()
    assert "maps_title" in header and "q" in header
    assert lines[0].split(",")[0] == "pizza"


def test_process_file_requires_query_column(tmp_path: Path):
    src = tmp_path / "bad.xlsx"
    pd.DataFrame({"a": ["x"]}).to_excel(src, index=False, engine="openpyxl")
    with pytest.raises(ValueError, match="No query column"):
        bp.process_file(src, post_bulk_fn=fake_post)


# --------------------------------------------------------------------------
# Bridge call retries
# --------------------------------------------------------------------------


class FakeResp:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self.text = json.dumps(body or {})
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


def test_post_bulk_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(bp, "PROCESSOR_BACKOFF_BASE", 0.01)
    calls = {"n": 0}

    def fake_httpx_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(429, {"error": {"code": "KEY_POOL_COOLING_DOWN"}},
                            headers={"Retry-After": "0.01"})
        return FakeResp(200, {"ok": True, "results": []})

    monkeypatch.setattr(bp.httpx, "post", fake_httpx_post)
    data = bp.post_bulk({"requests": []}, bearer=None)
    assert data["ok"] is True and calls["n"] == 2


def test_post_bulk_retries_transport_errors(monkeypatch):
    monkeypatch.setattr(bp, "PROCESSOR_BACKOFF_BASE", 0.01)
    calls = {"n": 0}

    def flaky_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise bp.httpx.ConnectError("nope")
        return FakeResp(200, {"ok": True, "results": []})

    monkeypatch.setattr(bp.httpx, "post", flaky_post)
    assert bp.post_bulk({"requests": []}, bearer=None)["ok"] is True
    assert calls["n"] == 2


def test_post_bulk_does_not_retry_client_errors(monkeypatch):
    calls = {"n": 0}

    def bad_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResp(422, {"error": {"code": "VALIDATION_ERROR"}})

    monkeypatch.setattr(bp.httpx, "post", bad_post)
    with pytest.raises(bp.BulkRequestError) as exc:
        bp.post_bulk({"requests": []}, bearer=None)
    assert exc.value.status_code == 422
    assert calls["n"] == 1


def test_post_bulk_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(bp, "PROCESSOR_BACKOFF_BASE", 0.01)

    def always_503(url, json=None, headers=None, timeout=None):
        return FakeResp(503, {"error": {"code": "NOPE"}})

    monkeypatch.setattr(bp.httpx, "post", always_503)
    with pytest.raises(bp.BulkRequestError, match="attempt"):
        bp.post_bulk({"requests": []}, bearer=None, max_retries=2)
