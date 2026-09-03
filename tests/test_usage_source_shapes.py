"""#284 — a malformed usage source yields that source's no-data line, never a raise.

Both builders read sources we do not parse ourselves: `/tmp/claude-usage-cache.json`, written
by a shell script from a network response, and Codex's own rollout JSON. Neither is hostile
input — a bad shape means a broken writer — but `handle_command` swallows the raise, so the
owner simply got NO answer to `/usage`. Since #795 the reply carries both accounts, so one
malformed cache also cost the other account's line.

The tests enumerate the shapes rather than sampling them: the bug was never one field, it was
"a shape we did not expect", and it appeared at eight sites at once.
"""

import json
import math

import pytest

from bridge import common, daemon


# Every "not what was promised" shape, once, reused everywhere a mapping is expected.
NON_DICTS = [None, "string", ["list"], 42, 0, True, False, 3.5, ()]
NON_LISTS = [None, "string", {"a": 1}, 42, True, 3.5]
BAD_NUMBERS = [None, "9", True, False, float("nan"), float("inf"), float("-inf"), [], {}]
BAD_RESETS = [None, 42, 3.5, True, [], {}, "", "   ", "not-a-date", "2026-13-45T99:99:99"]


def _write(tmp_path, monkeypatch, payload, raw=False):
    path = tmp_path / "usage.json"
    path.write_text(payload if raw else json.dumps(payload))
    monkeypatch.setattr(daemon, "USAGE_CACHE", str(path))
    return path


GOOD = {
    "five_hour": {"utilization": 30.0, "resets_at": "2026-09-02T12:09:59+00:00"},
    "seven_day": {"utilization": 22.0, "resets_at": "2026-09-07T23:59:59+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 30,
         "resets_at": "2026-09-02T12:09:59+00:00", "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 24,
         "resets_at": "2026-09-07T23:59:59+00:00",
         "scope": {"model": {"display_name": "Fable"}}},
    ],
}


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize("value", NON_DICTS)
def test_as_dict_admits_only_dicts(value):
    assert daemon._as_dict(value) == {}


@pytest.mark.parametrize("value", NON_LISTS)
def test_as_list_admits_only_lists(value):
    # deliberately not "any iterable": iterating a string yields characters, which then fail
    # as mappings one at a time — the original crash, one level down
    assert daemon._as_list(value) == []


@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_finite_number_rejects_bools_nan_and_infinity(value):
    assert daemon._finite_number(value) is None


@pytest.mark.parametrize("value", [0, 1, -1, 3.5, 1e9])
def test_finite_number_keeps_real_numbers(value):
    assert daemon._finite_number(value) == value


@pytest.mark.parametrize("value", BAD_NUMBERS + [1e300 * 1e300, -1e18, 1e18])
def test_usable_epoch_never_returns_something_that_cannot_be_formatted(value):
    got = daemon._usable_epoch(value)
    if got is not None:
        daemon.local_hhmm(got)        # must not raise
        daemon.local_datetime(got)


# --------------------------------------------------------------------------- C2 reset_epoch


@pytest.mark.parametrize("block", NON_DICTS)
def test_reset_epoch_is_total_over_non_dict_blocks(block):
    assert daemon.reset_epoch(block) is None


@pytest.mark.parametrize("raw", BAD_RESETS)
def test_reset_epoch_is_total_over_bad_reset_values(raw):
    # an int `resets_at` reached `.replace` and raised AttributeError through every caller,
    # because only ValueError was caught
    assert daemon.reset_epoch({"resets_at": raw}) is None


def test_reset_epoch_still_parses_a_good_value():
    assert daemon.reset_epoch({"resets_at": "2026-09-02T12:09:59+00:00"}) == pytest.approx(
        1788350999.0, abs=1)
    assert daemon.reset_epoch({"resets_at": "2026-09-02T12:09:59Z"}) == pytest.approx(
        1788350999.0, abs=1)


# --------------------------------------------------------------------------- C1 claude side


@pytest.mark.parametrize("payload", NON_DICTS)
def test_a_cache_that_is_not_a_dict_is_no_data_not_a_crash(tmp_path, monkeypatch, payload):
    _write(tmp_path, monkeypatch, payload)
    assert daemon.account_usage_line() is None


@pytest.mark.parametrize("raw", ["", "not json", "[", "\x00"])
def test_an_unparseable_cache_is_no_data(tmp_path, monkeypatch, raw):
    _write(tmp_path, monkeypatch, raw, raw=True)
    assert daemon.account_usage_line() is None


@pytest.mark.parametrize("window", NON_DICTS)
@pytest.mark.parametrize("key", ["five_hour", "seven_day"])
def test_a_non_dict_window_drops_only_its_own_segment(tmp_path, monkeypatch, key, window):
    cache = json.loads(json.dumps(GOOD))
    cache[key] = window
    _write(tmp_path, monkeypatch, cache)

    line = daemon.account_usage_line()

    assert line is not None, "one bad window must not cost the whole line"
    assert ("5h" in line) is (key != "five_hour")
    assert ("week 78% left" in line) is (key != "seven_day")
    assert "Fable" in line, "the scoped segment is independent of both windows"


@pytest.mark.parametrize("limits", NON_LISTS)
def test_non_list_limits_drop_only_the_scoped_segment(tmp_path, monkeypatch, limits):
    cache = json.loads(json.dumps(GOOD))
    cache["limits"] = limits
    _write(tmp_path, monkeypatch, cache)

    line = daemon.account_usage_line()

    assert line is not None and "Fable" not in line
    assert "5h 70% left" in line and "week 78% left" in line


@pytest.mark.parametrize("entry", NON_DICTS)
def test_a_limits_entry_that_is_not_a_dict_is_skipped(tmp_path, monkeypatch, entry):
    cache = json.loads(json.dumps(GOOD))
    cache["limits"] = [entry] + cache["limits"]
    _write(tmp_path, monkeypatch, cache)

    line = daemon.account_usage_line()

    assert line is not None and "Fable week 76% left" in line, \
        "a junk neighbour must not hide a good entry"


@pytest.mark.parametrize("scope", NON_DICTS)
def test_a_non_dict_scope_is_skipped(tmp_path, monkeypatch, scope):
    cache = json.loads(json.dumps(GOOD))
    cache["limits"][1]["scope"] = scope
    _write(tmp_path, monkeypatch, cache)

    line = daemon.account_usage_line()
    assert line is not None and "Fable" not in line


@pytest.mark.parametrize("model", NON_DICTS)
def test_a_non_dict_model_is_skipped(tmp_path, monkeypatch, model):
    cache = json.loads(json.dumps(GOOD))
    cache["limits"][1]["scope"] = {"model": model}
    _write(tmp_path, monkeypatch, cache)

    line = daemon.account_usage_line()
    assert line is not None and "Fable" not in line


@pytest.mark.parametrize("display", [None, 42, True, [], {}, 3.5])
def test_a_non_string_display_name_is_skipped(tmp_path, monkeypatch, display):
    cache = json.loads(json.dumps(GOOD))
    cache["limits"][1]["scope"] = {"model": {"display_name": display}}
    _write(tmp_path, monkeypatch, cache)

    line = daemon.account_usage_line()
    assert line is not None and "Fable" not in line


@pytest.mark.parametrize("raw", BAD_RESETS)
def test_a_bad_reset_drops_only_its_clause(tmp_path, monkeypatch, raw):
    cache = json.loads(json.dumps(GOOD))
    cache["five_hour"]["resets_at"] = raw
    _write(tmp_path, monkeypatch, cache)

    line = daemon.account_usage_line()

    assert line is not None and "5h 70% left" in line, "the meter survives a bad reset"
    assert line.count("resets") == 2, "only the 5h reset clause is dropped"


# --------------------------------------------------------------------------- C1 codex side


def _codex(monkeypatch, payload):
    monkeypatch.setattr(daemon.codex_ctx, "usage_latest", lambda: payload)
    monkeypatch.setattr(daemon.codex_ctx, "usage_for_cwd", lambda cwd: payload)


GOOD_CODEX = {"plan_type": "pro", "primary_pct": 30, "primary_window_min": 300,
              "primary_reset": 1788610199.0, "secondary_pct": 22,
              "secondary_window_min": 10080, "secondary_reset": 1789000000.0}


@pytest.mark.parametrize("payload", ["string", ["list"], 42, 3.5, True])
def test_a_truthy_non_dict_rollout_is_no_data_not_a_crash(monkeypatch, payload):
    _codex(monkeypatch, payload)
    assert daemon.codex_usage_line() is None


@pytest.mark.parametrize("payload", [None, {}, 0, "", [], False])
def test_a_falsy_rollout_is_still_no_data(monkeypatch, payload):
    _codex(monkeypatch, payload)
    assert daemon.codex_usage_line() is None


@pytest.mark.parametrize("reset", BAD_NUMBERS)
def test_a_non_finite_codex_reset_drops_only_its_clause(monkeypatch, reset):
    payload = dict(GOOD_CODEX, primary_reset=reset)
    _codex(monkeypatch, payload)

    line = daemon.codex_usage_line()

    assert line is not None and "5h 70% left" in line
    assert line.count("resets") == 1, "only the primary reset clause is dropped"


@pytest.mark.parametrize("win", BAD_NUMBERS)
def test_a_non_finite_window_length_falls_back_to_the_unknown_label(monkeypatch, win):
    _codex(monkeypatch, dict(GOOD_CODEX, primary_window_min=win))

    line = daemon.codex_usage_line()

    assert line is not None and "? 70% left" in line


# --------------------------------------------------------------------------- C3 plan_type


@pytest.mark.parametrize("plan,want", [
    ("pro", "pro"), ("  pro  ", "pro"), ("team plan", "team plan"),
    (None, "?"), ("", "?"), ("   ", "?"), (42, "?"), (True, "?"), ([], "?"), ({}, "?"),
    ("a\nb", "a b"), ("a\tb\r\nc", "a b c"),
])
def test_the_plan_label_is_bounded_and_single_line(plan, want):
    assert daemon._plan_label(plan) == want


def test_a_very_long_plan_type_is_capped():
    label = daemon._plan_label("x" * 4000)
    assert len(label) <= daemon.PLAN_LABEL_MAX
    assert label.endswith("…")


def test_a_hostile_plan_type_keeps_the_reply_to_one_message(monkeypatch):
    """C3: the bare /usage reply is two lines in ONE message. A 4,000-char plan_type made it
    4,104 characters, which split_for_telegram turned into three."""
    _codex(monkeypatch, dict(GOOD_CODEX, plan_type="x" * 4000))

    codex_line = daemon.codex_usage_line()
    reply = "\n".join(["👤 Claude usage (account): 5h 70% left", codex_line])

    assert len(common.split_for_telegram(reply)) == 1
    assert "\n" not in codex_line


# --------------------------------------------------------------------------- C4 independence


def test_a_malformed_claude_cache_does_not_cost_the_codex_line(tmp_path, monkeypatch):
    # The point of #284: since #795 both accounts share one reply, so a raise on one side
    # silently removed the other side's line too.
    _write(tmp_path, monkeypatch, ["nonsense"])
    _codex(monkeypatch, GOOD_CODEX)

    assert daemon.account_usage_line() is None          # no data...
    assert daemon.codex_usage_line() is not None        # ...but the other side is unaffected


def test_a_malformed_codex_rollout_does_not_cost_the_claude_line(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, GOOD)
    _codex(monkeypatch, "nonsense")

    assert daemon.codex_usage_line() is None
    assert daemon.account_usage_line() is not None


# --------------------------------------------------------------------------- C5 no drift


def test_a_well_formed_cache_renders_exactly_as_before(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, GOOD)

    assert daemon.account_usage_line() == (
        "👤 Claude usage (account): "
        "5h 70% left (resets 15:09 UTC+3) · "
        "week 78% left (resets Tue 08 Sep 02:59) · "
        "Fable week 76% left (resets Tue 08 Sep 02:59)"
    )


def test_a_well_formed_codex_rollout_renders_exactly_as_before(monkeypatch):
    _codex(monkeypatch, GOOD_CODEX)

    assert daemon.codex_usage_line() == (
        "🤖 Codex usage (pro): "
        "5h 70% left (resets 15:09 UTC+3) · "
        "weekly 78% left (resets Thu 10 Sep 03:26)"
    )


def test_no_builder_raises_for_any_enumerated_shape(tmp_path, monkeypatch):
    """The property, over the whole cross-product — the guarantee C1 actually asks for."""
    for payload in NON_DICTS + [GOOD]:
        _write(tmp_path, monkeypatch, payload)
        daemon.account_usage_line()
    for key in ("five_hour", "seven_day", "limits"):
        for shape in NON_DICTS + NON_LISTS:
            cache = json.loads(json.dumps(GOOD))
            cache[key] = shape
            _write(tmp_path, monkeypatch, cache)
            daemon.account_usage_line()
    for shape in NON_DICTS + [GOOD_CODEX]:
        _codex(monkeypatch, shape)
        daemon.codex_usage_line()
    for bad in BAD_NUMBERS:
        _codex(monkeypatch, dict(GOOD_CODEX, primary_reset=bad, secondary_window_min=bad,
                                 primary_pct=bad))
        daemon.codex_usage_line()
