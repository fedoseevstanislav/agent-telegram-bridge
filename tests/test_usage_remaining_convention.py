"""One meter convention for both accounts (#795).

Claude's cache reports % USED, Codex reports % LEFT. Reading them side by side flipped
routing calls, so `/usage` now prints REMAINING for both, in the same segment shape:
`<window> N% left (resets <time>)`. These tests pin the convention (C2) and the shape (C3)
on the real line builders, and the "used"-free reply on the handler.
"""

import json
import re

from bridge import codex_ctx, daemon


# Every segment of every meter line: a window label, a remaining %, an optional reset.
SEGMENT = re.compile(r"^[^:]+ \d+% left( \(resets [^)]+\))?$")


def _split_segments(line):
    """`<prefix>: seg · seg · seg` -> the segments, each still carrying its window label."""
    head, _, body = line.partition(": ")
    assert head and body, line
    return body.split(" · ")


def _claude_cache(tmp_path, monkeypatch, fable_pct=13):
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({
        "five_hour": {"utilization": 9.0, "resets_at": "2026-09-01T21:59:59+00:00"},
        "seven_day": {"utilization": 9.0, "resets_at": "2026-09-05T11:59:59+00:00"},
        "limits": [
            {"kind": "weekly_all", "group": "weekly", "percent": 9,
             "resets_at": "2026-09-05T11:59:59+00:00", "scope": None},
            {"kind": "weekly_scoped", "group": "weekly", "percent": fable_pct,
             "resets_at": "2026-09-05T11:59:59+00:00",
             "scope": {"model": {"display_name": "Fable"}}},
        ],
    }))
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(cache))
    monkeypatch.setattr(daemon, "local_hhmm", lambda ts: "HH:MM")
    monkeypatch.setattr(daemon, "local_datetime", lambda ts: "DATETIME")
    monkeypatch.setattr(daemon, "TZ_OFFSET", 3)


def _codex_snapshot(monkeypatch):
    monkeypatch.setattr(codex_ctx, "usage_latest", lambda: {
        "primary_pct": 30.0, "primary_window_min": 300, "primary_reset": 111,
        "secondary_pct": 11.0, "secondary_window_min": 10080, "secondary_reset": 222,
        "plan_type": "pro",
    })


def test_claude_line_reports_remaining_not_used(tmp_path, monkeypatch):
    # C2: the cache's 9% / 9% / 13% USED must render as 91% / 91% / 87% LEFT.
    _claude_cache(tmp_path, monkeypatch)
    line = daemon.account_usage_line()
    assert line == (
        "👤 Claude usage (account): 5h 91% left (resets HH:MM UTC+3) "
        "· week 91% left (resets DATETIME) · Fable week 87% left (resets DATETIME)")
    assert "used" not in line


def test_both_lines_share_the_same_segment_shape(tmp_path, monkeypatch):
    # C3: <window> N% left (resets <time>) — for the Claude 5h/week/Fable-week segments and
    # for every window the Codex snapshot reports.
    _claude_cache(tmp_path, monkeypatch)
    _codex_snapshot(monkeypatch)
    claude_segments = _split_segments(daemon.account_usage_line())
    codex_segments = _split_segments(daemon.codex_usage_line(None))
    assert [s.split(" ")[0] for s in claude_segments] == ["5h", "week", "Fable"]
    assert [s.split(" ")[0] for s in codex_segments] == ["5h", "weekly"]
    for seg in claude_segments + codex_segments:
        assert SEGMENT.match(seg), seg


def test_bare_usage_reply_never_says_used(tmp_path, monkeypatch):
    # C1 + C2 end to end on the real builders: one reply, both lines, no "used" anywhere.
    _claude_cache(tmp_path, monkeypatch)
    _codex_snapshot(monkeypatch)
    monkeypatch.setattr(daemon, "read_registry", lambda: {"5": {"pane": "%1", "name": "x"}})
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    monkeypatch.setattr(daemon, "pane_cwd", lambda pane: "/cwd")
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))

    daemon.handle_command({}, 5, "/usage")

    assert len(replies) == 1
    claude_line, codex_line = replies[0].split("\n")
    assert claude_line.startswith("👤 Claude usage (account): ")
    assert codex_line.startswith("🤖 Codex usage (pro): ")
    assert "used" not in replies[0]
    assert replies[0].count("% left") == 5


def test_help_text_advertises_left_and_both(tmp_path, monkeypatch):
    usage_help = [l for l in daemon.HELP_TEXT.splitlines() if l.startswith("/usage")]
    assert len(usage_help) == 1
    assert "LEFT" in usage_help[0] and "BOTH" in usage_help[0]
    assert "% used" not in daemon.HELP_TEXT


# ---- unusable source numbers must not fake a meter or sink the whole reply --------

def _cache(tmp_path, monkeypatch, five=9.0, week=9.0):
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({
        "five_hour": {"utilization": five}, "seven_day": {"utilization": week}, "limits": [],
    }))
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(cache))
    return cache


def test_null_utilization_drops_its_segment_instead_of_crashing(tmp_path, monkeypatch):
    # A `null` percent used to reach round() and raise; since the reply carries BOTH accounts
    # that would have taken the Codex line down with it (round 1 review finding).
    _cache(tmp_path, monkeypatch, five=None, week=9.0)
    line = daemon.account_usage_line()
    assert line == "👤 Claude usage (account): week 91% left"


def test_no_usable_claude_meter_returns_none_so_the_codex_line_survives(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, five=None, week=None)
    assert daemon.account_usage_line() is None

    _codex_snapshot(monkeypatch)
    monkeypatch.setattr(daemon, "read_registry", lambda: {})
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    daemon.handle_command({}, 5, "/usage")
    claude_line, codex_line = replies[0].split("\n")
    assert claude_line.startswith("👤 Claude: no data yet")
    assert codex_line.startswith("🤖 Codex usage (pro): ")


def test_out_of_range_percentages_are_clamped(tmp_path, monkeypatch):
    # A source value outside 0..100 must never print "-1% left" or "101% left".
    _cache(tmp_path, monkeypatch, five=-1, week=101)
    assert daemon.account_usage_line() == "👤 Claude usage (account): 5h 100% left · week 0% left"


def test_nan_percentages_are_dropped_from_both_lines(tmp_path, monkeypatch):
    nan = float("nan")
    _cache(tmp_path, monkeypatch, five=nan, week=9.0)
    assert "nan" not in daemon.account_usage_line()

    monkeypatch.setattr(codex_ctx, "usage_latest", lambda: {
        "primary_pct": nan, "primary_window_min": 300, "primary_reset": None,
        "secondary_pct": 11.0, "secondary_window_min": 10080, "secondary_reset": None,
        "plan_type": "pro",
    })
    assert daemon.codex_usage_line(None) == "🤖 Codex usage (pro): weekly 89% left"


def test_pct_left_rejects_non_numbers_and_booleans():
    assert daemon._pct_left(0) == 100 and daemon._pct_left(100) == 0
    assert daemon._pct_left(9.4) == 91          # rounded like the cache's own numbers
    for bad in (None, "9", True, False, float("inf"), float("-inf"), float("nan")):
        assert daemon._pct_left(bad) is None, bad


def test_malformed_fable_percent_drops_only_that_segment(tmp_path, monkeypatch):
    # Round-2 review finding: _scoped_weekly_pct() rounded before the validator ran, so a
    # Fable entry carrying "9" (or NaN) crashed the whole two-account reply.
    for bad in ("9", float("nan"), float("inf")):
        cache = tmp_path / "usage.json"
        cache.write_text(json.dumps({
            "five_hour": {"utilization": 9.0}, "seven_day": {"utilization": 9.0},
            "limits": [{"kind": "weekly_scoped", "group": "weekly", "percent": bad,
                        "scope": {"model": {"display_name": "Fable"}}}],
        }))
        monkeypatch.setattr(daemon, "USAGE_CACHE", str(cache))
        assert daemon.account_usage_line() == (
            "👤 Claude usage (account): 5h 91% left · week 91% left"), bad
