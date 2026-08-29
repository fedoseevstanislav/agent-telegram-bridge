"""Unit tests for the Fable-scoped weekly breakout in account_usage_line() (#113).

The Fable weekly usage is not a top-level cache field — it lives in limits[] as a
`weekly_scoped` entry whose scope.model.display_name == "Fable"."""

import json

from bridge import daemon


def _write_cache(path, fable_pct=None):
    cache = {
        "five_hour": {"utilization": 15.0, "resets_at": "2026-07-22T13:59:59+00:00"},
        "seven_day": {"utilization": 3.0, "resets_at": "2026-07-28T11:59:59+00:00"},
        "limits": [
            {"kind": "session", "group": "session", "percent": 15,
             "resets_at": "2026-07-22T13:59:59+00:00", "scope": None},
            {"kind": "weekly_all", "group": "weekly", "percent": 3,
             "resets_at": "2026-07-28T11:59:59+00:00", "scope": None},
        ],
    }
    if fable_pct is not None:
        cache["limits"].append({
            "kind": "weekly_scoped", "group": "weekly", "percent": fable_pct,
            "resets_at": "2026-07-28T11:59:59+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
        })
    path.write_text(json.dumps(cache))


def test_fable_weekly_shown_when_present(tmp_path, monkeypatch):
    cache = tmp_path / "usage.json"
    _write_cache(cache, fable_pct=42)
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(cache))
    line = daemon.account_usage_line()
    assert line is not None
    assert "Fable week 42% used" in line
    # aggregate segments are still present and unchanged
    assert "5h 15% used" in line
    assert "week 3% used" in line


def test_no_fable_segment_when_absent(tmp_path, monkeypatch):
    cache = tmp_path / "usage.json"
    _write_cache(cache, fable_pct=None)
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(cache))
    line = daemon.account_usage_line()
    assert line is not None
    assert "Fable" not in line
    assert "week 3% used" in line


def test_scoped_weekly_pct_helper_case_insensitive_and_absent():
    u = {"limits": [
        {"group": "session", "percent": 15, "scope": None},
        {"group": "weekly", "percent": 3, "scope": None},
        {"group": "weekly", "percent": 7, "scope": {"model": {"display_name": "Fable"}}},
    ]}
    res = daemon._scoped_weekly_pct(u, "fable")   # match is case-insensitive
    assert res is not None and res[0] == 7
    assert daemon._scoped_weekly_pct(u, "Opus") is None


def test_scoped_weekly_pct_ignores_non_weekly_scope():
    # a model-scoped entry that is NOT weekly must not be picked up
    u = {"limits": [
        {"group": "session", "percent": 99, "scope": {"model": {"display_name": "Fable"}}},
    ]}
    assert daemon._scoped_weekly_pct(u, "Fable") is None


def test_present_fable_entry_with_null_percent_is_omitted(tmp_path, monkeypatch):
    # A Fable weekly entry that EXISTS but has percent=None must NOT render a false "0%"
    cache = tmp_path / "usage.json"
    data = {
        "five_hour": {"utilization": 15.0, "resets_at": "2026-07-22T13:59:59+00:00"},
        "seven_day": {"utilization": 3.0, "resets_at": "2026-07-28T11:59:59+00:00"},
        "limits": [
            {"kind": "weekly_scoped", "group": "weekly", "percent": None,
             "resets_at": "2026-07-28T11:59:59+00:00",
             "scope": {"model": {"display_name": "Fable"}}},
        ],
    }
    cache.write_text(json.dumps(data))
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(cache))
    assert "Fable" not in daemon.account_usage_line()
    assert daemon._scoped_weekly_pct(data, "Fable") is None


def test_scoped_weekly_pct_real_zero_is_shown():
    # percent == 0 is a real value (0% used), distinct from None — must be returned, not dropped
    u = {"limits": [
        {"group": "weekly", "percent": 0, "scope": {"model": {"display_name": "Fable"}}},
    ]}
    res = daemon._scoped_weekly_pct(u, "Fable")
    assert res is not None and res[0] == 0
