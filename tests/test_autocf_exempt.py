import json

from bridge import daemon


def _point_state(monkeypatch, tmp_path, name="autocf_exempt.json"):
    """Redirect daemon.state_path('autocf_exempt.json') at a temp file; return its path."""
    p = tmp_path / name

    def fake_state_path(*parts):
        return str(p) if parts == (name,) else "/nonexistent/" + "/".join(parts)

    monkeypatch.setattr(daemon, "state_path", fake_state_path)
    return p


# ---- loader ----

def test_exempt_returns_topic_ids_as_strings(monkeypatch, tmp_path):
    p = _point_state(monkeypatch, tmp_path)
    p.write_text(json.dumps(["5935", 5927]))  # mixed str/int -> all coerced to str
    assert daemon.load_autocf_exempt() == {"5935", "5927"}


def test_exempt_missing_file_is_empty(monkeypatch, tmp_path):
    _point_state(monkeypatch, tmp_path)  # file never created
    assert daemon.load_autocf_exempt() == set()


def test_exempt_malformed_json_is_empty(monkeypatch, tmp_path):
    p = _point_state(monkeypatch, tmp_path)
    p.write_text("{not json")
    assert daemon.load_autocf_exempt() == set()


def test_exempt_non_list_is_empty(monkeypatch, tmp_path):
    p = _point_state(monkeypatch, tmp_path)
    p.write_text(json.dumps({"5935": True}))  # a dict, not a list -> ignored
    assert daemon.load_autocf_exempt() == set()


def test_exempt_empty_list(monkeypatch, tmp_path):
    p = _point_state(monkeypatch, tmp_path)
    p.write_text("[]")
    assert daemon.load_autocf_exempt() == set()


# ---- control flow (the guard actually affects auto-CF) ----

def _patch_autocf_side_effects(monkeypatch):
    """Stub the side-effecting deps of _process_autocf; return a call recorder."""
    rec = {"cf": [], "reply": 0}
    monkeypatch.setattr(  # returns True since #161: it reports whether a worker started
        daemon, "handle_carry_forward",
        lambda cfg, tid, cmd, info, pane: bool(rec["cf"].append(tid)) or True)
    monkeypatch.setattr(daemon, "reply",
                        lambda *a, **k: bool(rec.__setitem__("reply", rec["reply"] + 1)) or True)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    return rec


def test_process_autocf_exempt_never_fires_and_clears_armed(monkeypatch):
    rec = _patch_autocf_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    autocf_fired = {"5935": True}  # pre-existing armed flag
    # 95% context on a claude session would normally fire — but 5935 is exempt.
    fired = daemon._process_autocf({}, "5935", {}, "%1", 95, "claude", {"5935"}, autocf_fired)
    assert fired is False
    assert rec["cf"] == [] and rec["reply"] == 0
    assert "5935" not in autocf_fired  # stale armed flag cleared for clean un-exempt


def test_process_autocf_non_exempt_fires_at_threshold(monkeypatch):
    rec = _patch_autocf_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    autocf_fired = {}
    fired = daemon._process_autocf({}, "606", {}, "%2", 95, "claude", set(), autocf_fired)
    assert fired is True
    assert rec["cf"] == [606]  # handle_carry_forward called with int(tid)
    assert autocf_fired["606"] is True  # now armed


def test_process_autocf_non_exempt_skips_while_cf_active(monkeypatch):
    rec = _patch_autocf_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: True)  # already running
    autocf_fired = {}
    fired = daemon._process_autocf({}, "606", {}, "%2", 95, "claude", set(), autocf_fired)
    assert fired is False
    assert rec["cf"] == []  # handle_carry_forward not called while a CF is already active


def test_process_autocf_non_exempt_does_not_fire_below_rearm(monkeypatch):
    rec = _patch_autocf_side_effects(monkeypatch)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    autocf_fired = {"606": True}  # armed
    # context dropped below rearm -> re-arm (armed_next False), no fire
    fired = daemon._process_autocf({}, "606", {}, "%2", 10, "claude", set(), autocf_fired)
    assert fired is False
    assert rec["cf"] == []
    assert autocf_fired["606"] is False  # re-armed for the next climb
