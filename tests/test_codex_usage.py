"""Unit tests for global Codex usage (#94): codex_ctx.latest_rollout_any / usage_latest
(account-wide, no live pane needed) and the `/usage [claude|codex]` arg routing."""

import json
import os

from bridge import codex_ctx, daemon


# ---- codex_ctx: account-wide rollout lookup ----------------------------------

def _write_rollout(path, cwd, primary_pct=15, secondary_pct=11, plan="pro"):
    with open(path, "w") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n")
        f.write(json.dumps({
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": primary_pct, "window_minutes": 300, "resets_at": 1},
                "secondary": {"used_percent": secondary_pct, "window_minutes": 10080, "resets_at": 2},
                "plan_type": plan,
            }},
        }) + "\n")


def _sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions" / "2026" / "07" / "12"
    d.mkdir(parents=True)
    monkeypatch.setattr(codex_ctx, "SESSIONS_DIR", str(tmp_path / "sessions"))
    return d


def test_latest_rollout_any_picks_newest_across_cwds(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    old, new = str(d / "rollout-old.jsonl"), str(d / "rollout-new.jsonl")
    _write_rollout(old, "/a", primary_pct=10)
    _write_rollout(new, "/b", primary_pct=40)
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert codex_ctx.latest_rollout_any() == new


def test_usage_latest_reads_account_wide(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    _write_rollout(str(d / "rollout-x.jsonl"), "/any", primary_pct=15, secondary_pct=11, plan="pro")
    u = codex_ctx.usage_latest()
    assert (u["primary_pct"], u["secondary_pct"], u["plan_type"]) == (15, 11, "pro")


def test_usage_latest_none_when_no_sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_ctx, "SESSIONS_DIR", str(tmp_path / "nope"))
    assert codex_ctx.latest_rollout_any() is None
    assert codex_ctx.usage_latest() is None


def _write_rollout_no_ratelimits(path, cwd):
    with open(path, "w") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n")
        f.write(json.dumps({"type": "event_msg",
                            "payload": {"type": "token_count", "info": {"x": 1}}}) + "\n")


def test_usage_latest_skips_newer_rollout_without_ratelimits(tmp_path, monkeypatch):
    # a freshly-started session's rollout is newest but has no rate_limits yet; it must
    # NOT mask an older rollout that still carries account-wide usage (Codex #95 review)
    d = _sessions_dir(tmp_path, monkeypatch)
    old, new = str(d / "rollout-old.jsonl"), str(d / "rollout-new-fresh.jsonl")
    _write_rollout(old, "/a", primary_pct=22)          # older, HAS rate_limits
    _write_rollout_no_ratelimits(new, "/b")            # newer, NO rate_limits
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert codex_ctx.latest_rollout_any() == new       # newest is the empty one...
    assert codex_ctx.usage_latest()["primary_pct"] == 22   # ...but usage skips to the valid older one


def test_usage_for_cwd_still_filters_by_cwd(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    _write_rollout(str(d / "rollout-a.jsonl"), "/aaa", primary_pct=20)
    _write_rollout(str(d / "rollout-b.jsonl"), "/bbb", primary_pct=30)
    assert codex_ctx.usage_for_cwd("/aaa")["primary_pct"] == 20
    assert codex_ctx.usage_for_cwd("/bbb")["primary_pct"] == 30
    assert codex_ctx.usage_for_cwd("/nomatch") is None


def test_usage_from_rollout_none_path():
    assert codex_ctx._usage_from_rollout(None) is None


# ---- /usage arg routing in handle_command ------------------------------------

def _usage_env(monkeypatch, engine):
    monkeypatch.setattr(daemon, "read_registry", lambda: {"5": {"pane": "%1", "name": "x"}})
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: engine)
    monkeypatch.setattr(daemon, "pane_cwd", lambda pane: "/cwd")
    calls = {"codex": [], "claude": 0}
    monkeypatch.setattr(daemon, "codex_usage_line", lambda cwd=None: calls["codex"].append(cwd) or "CODEX")
    def _acct():
        calls["claude"] += 1
        return "CLAUDE"
    monkeypatch.setattr(daemon, "account_usage_line", _acct)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    return calls, replies


def test_usage_codex_arg_is_global_from_any_topic(monkeypatch):
    calls, replies = _usage_env(monkeypatch, engine="claude")   # a CLAUDE topic
    daemon.handle_command({}, 5, "/usage codex")
    assert replies == ["CODEX"]
    assert calls["codex"] == [None]        # account-wide (cwd=None), not the topic's cwd
    assert calls["claude"] == 0


def test_usage_claude_arg_forces_account(monkeypatch):
    calls, replies = _usage_env(monkeypatch, engine="codex")    # a CODEX topic
    daemon.handle_command({}, 5, "/usage claude")
    assert replies == ["CLAUDE"]
    assert calls["claude"] == 1 and calls["codex"] == []


def test_usage_codex_arg_in_codex_topic_scopes_to_cwd(monkeypatch):
    calls, replies = _usage_env(monkeypatch, engine="codex")
    daemon.handle_command({}, 5, "/usage codex")
    assert replies == ["CODEX"]
    assert calls["codex"] == ["/cwd"]      # /usage codex + codex topic -> that session's rollout
    assert calls["claude"] == 0


def test_usage_bare_shows_both_meters_in_one_message(monkeypatch):
    # #795 C1: one message, both accounts, from a CLAUDE topic and from a CODEX topic alike.
    for engine in ("claude", "codex"):
        calls, replies = _usage_env(monkeypatch, engine=engine)
        daemon.handle_command({}, 5, "/usage")
        assert replies == ["CLAUDE\nCODEX"]        # exactly ONE reply carrying both lines
        assert calls["claude"] == 1
        assert calls["codex"] == [None]            # bare is account-wide for both engines


def test_usage_bare_with_no_pane_still_shows_both(monkeypatch):
    calls, replies = _usage_env(monkeypatch, engine="claude")
    monkeypatch.setattr(daemon, "read_registry", lambda: {})   # topic with no bound pane
    daemon.handle_command({}, 5, "/usage")
    assert replies == ["CLAUDE\nCODEX"]


def test_usage_bare_names_the_missing_source_and_keeps_the_other(monkeypatch):
    # #795 C1: when one source has no data its line says so IN PLACE; the other still shows.
    calls, replies = _usage_env(monkeypatch, engine="codex")
    monkeypatch.setattr(daemon, "codex_usage_line", lambda cwd=None: None)
    daemon.handle_command({}, 5, "/usage")
    assert len(replies) == 1
    claude_line, codex_line = replies[0].split("\n")
    assert claude_line == "CLAUDE"
    assert codex_line.startswith("🤖 Codex: no data yet")

    calls, replies = _usage_env(monkeypatch, engine="claude")
    monkeypatch.setattr(daemon, "account_usage_line", lambda: None)
    daemon.handle_command({}, 5, "/usage")
    claude_line, codex_line = replies[0].split("\n")
    assert claude_line.startswith("👤 Claude: no data yet")
    assert codex_line == "CODEX"


def test_usage_invalid_arg_shows_hint_and_calls_nothing(monkeypatch):
    calls, replies = _usage_env(monkeypatch, engine="claude")
    daemon.handle_command({}, 5, "/usage bogus")
    assert len(replies) == 1 and replies[0].startswith("Usage:")
    assert calls == {"codex": [], "claude": 0}


def test_usage_extra_token_rejected(monkeypatch):
    # contract is `/usage [claude|codex]`; extra tokens must be rejected (Codex #95 review)
    calls, replies = _usage_env(monkeypatch, engine="claude")
    daemon.handle_command({}, 5, "/usage codex extra")
    assert len(replies) == 1 and replies[0].startswith("Usage:")
    assert calls == {"codex": [], "claude": 0}


def test_usage_codex_arg_is_case_insensitive(monkeypatch):
    calls, replies = _usage_env(monkeypatch, engine="claude")
    daemon.handle_command({}, 5, "/usage CODEX")
    assert replies == ["CODEX"] and calls["codex"] == [None]


# ---- window labelling by length, not slot (#103) -----------------------------

def test_codex_window_label_by_length():
    assert daemon._codex_window_label(300) == "5h"        # the classic 5h window
    assert daemon._codex_window_label(10080) == "weekly"  # 7 days
    assert daemon._codex_window_label(1440) == "1d"       # daily
    assert daemon._codex_window_label(600) == "10h"       # arbitrary hours
    assert daemon._codex_window_label(None) is None       # unknown length


def test_codex_usage_line_weekly_only_labels_weekly(monkeypatch):
    # #103: codex removed the 5h window, so the account exposes ONLY a weekly window and it
    # arrives in the `primary` slot (window_minutes=10080); `secondary` is null. The OLD code
    # hardcoded primary->"5h" + secondary->"weekly", producing "5h 84% left · weekly n/a".
    # Must now render the primary as WEEKLY (matching codex's own TUI) and drop the absent one.
    monkeypatch.setattr(codex_ctx, "usage_for_cwd", lambda cwd: {
        "primary_pct": 16.0, "primary_window_min": 10080, "primary_reset": None,
        "secondary_pct": None, "secondary_window_min": None, "secondary_reset": None,
        "plan_type": "pro",
    })
    line = daemon.codex_usage_line("/cwd")
    assert line == "🤖 Codex usage (pro): weekly 84% left"
    assert "5h" not in line and "n/a" not in line


def test_codex_usage_line_both_windows(monkeypatch):
    # Normal account with both windows: primary=5h (300), secondary=weekly (10080).
    monkeypatch.setattr(codex_ctx, "usage_latest", lambda: {
        "primary_pct": 30.0, "primary_window_min": 300, "primary_reset": None,
        "secondary_pct": 11.0, "secondary_window_min": 10080, "secondary_reset": None,
        "plan_type": "pro",
    })
    assert daemon.codex_usage_line(None) == "🤖 Codex usage (pro): 5h 70% left · weekly 89% left"


def test_codex_usage_line_reset_format_by_window(monkeypatch):
    # Reset format is chosen by window LENGTH: short (≈5h) -> HH:MM, longer -> full datetime.
    monkeypatch.setattr(daemon, "local_hhmm", lambda ts: "HH:MM")
    monkeypatch.setattr(daemon, "local_datetime", lambda ts: "DATETIME")
    monkeypatch.setattr(daemon, "TZ_OFFSET", 3)
    monkeypatch.setattr(codex_ctx, "usage_latest", lambda: {
        "primary_pct": 30.0, "primary_window_min": 300, "primary_reset": 111,
        "secondary_pct": 11.0, "secondary_window_min": 10080, "secondary_reset": 222,
        "plan_type": "pro",
    })
    assert daemon.codex_usage_line(None) == (
        "🤖 Codex usage (pro): 5h 70% left (resets HH:MM UTC+3) · weekly 89% left (resets DATETIME)")


def test_codex_usage_line_none_when_no_window_has_pct(monkeypatch):
    monkeypatch.setattr(codex_ctx, "usage_latest", lambda: {
        "primary_pct": None, "primary_window_min": None, "primary_reset": None,
        "secondary_pct": None, "secondary_window_min": None, "secondary_reset": None,
        "plan_type": "pro",
    })
    assert daemon.codex_usage_line(None) is None


def test_codex_window_label_weekly_band_edges():
    # The weekly band is 9000..11520 min; just outside it must fall back to day/hour labels,
    # not be misclassified as weekly (review-noted edge).
    assert daemon._codex_window_label(9000) == "weekly"
    assert daemon._codex_window_label(11520) == "weekly"
    assert daemon._codex_window_label(8999) == "6d"    # just below the band -> days
    assert daemon._codex_window_label(11521) == "8d"   # just above the band -> days
    assert daemon._codex_window_label(360) == "6h"     # top of the "short" range


def test_usage_handler_none_falls_back_to_hint(monkeypatch):
    # When codex_usage_line returns None (no rate-limit snapshot yet), the /usage handler
    # must reply with the fallback hint, not crash or send an empty message.
    monkeypatch.setattr(daemon, "read_registry", lambda: {"5": {"pane": "%1", "name": "x"}})
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "codex")
    monkeypatch.setattr(daemon, "pane_cwd", lambda pane: "/cwd")
    monkeypatch.setattr(daemon, "codex_usage_line", lambda cwd=None: None)
    replies = []
    monkeypatch.setattr(daemon, "reply", lambda cfg, tid, text: replies.append(text))
    daemon.handle_command({}, 5, "/usage codex")
    assert len(replies) == 1 and replies[0].startswith("🤖 Codex: no data yet")



# ---- exact rollout resolution: open fds, not cwd (#121, #123) -----------------

def _write_thread_rollout(path, cwd, meta_extra=None, used=None, window=400000):
    meta = {"cwd": cwd}
    meta.update(meta_extra or {})
    with open(path, "w") as f:
        f.write(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
        if used is not None:
            f.write(json.dumps({
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "model_context_window": window,
                    "last_token_usage": {"input_tokens": used},
                }},
            }) + "\n")


def _fake_proc(tmp_path, monkeypatch, procs):
    """procs: {pid: {"ppid": int, "fds": [target, ...]}} rendered as a fake /proc."""
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    for pid, spec in procs.items():
        entry = root / str(pid)
        (entry / "fd").mkdir(parents=True)
        (entry / "stat").write_text(f"{pid} (codex) S {spec['ppid']} 0 0 0 -1\n")
        for index, target in enumerate(spec.get("fds", [])):
            (entry / "fd" / str(index)).symlink_to(target)
    monkeypatch.setattr(codex_ctx, "PROC_DIR", str(root))
    return root


def test_process_tree_walks_descendants(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch, {
        100: {"ppid": 1}, 200: {"ppid": 100}, 300: {"ppid": 200}, 400: {"ppid": 1},
    })

    tree = codex_ctx.process_tree(100)

    assert sorted(tree) == [100, 200, 300]
    assert codex_ctx.process_tree(None) == []


def test_open_rollout_found_on_a_child_process(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    own = str(d / "rollout-own.jsonl")
    _write_thread_rollout(own, "/home/user", {"thread_source": "user"})
    # The tmux pane process is a wrapper; only its CHILD holds the rollout open.
    _fake_proc(tmp_path, monkeypatch, {10: {"ppid": 1}, 11: {"ppid": 10, "fds": [own]}})

    assert codex_ctx.open_rollout_for_pid_tree(10) == own


def test_open_rollout_excludes_the_parents_subagent_rollouts(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    own = str(d / "rollout-own.jsonl")
    sub_by_parent = str(d / "rollout-sub-a.jsonl")
    sub_by_source = str(d / "rollout-sub-b.jsonl")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("x")
    _write_thread_rollout(own, "/home/user", {"thread_source": "user"})
    _write_thread_rollout(sub_by_parent, "/home/user", {"parent_thread_id": "01900000"})
    _write_thread_rollout(sub_by_source, "/home/user", {"thread_source": "subagent"})
    _fake_proc(tmp_path, monkeypatch, {
        10: {"ppid": 1, "fds": [own, sub_by_parent, sub_by_source, str(unrelated)]},
    })

    opened, roots = codex_ctx.open_rollouts_for_pids([10])

    assert len(opened) == 3 and roots == [own]   # each marker alone must disqualify
    assert codex_ctx.open_rollout_for_pid_tree(10) == own


def test_open_rollout_refuses_to_pick_between_two_root_threads(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    first, second = str(d / "rollout-a.jsonl"), str(d / "rollout-b.jsonl")
    _write_thread_rollout(first, "/home/user", {"thread_source": "user"})
    _write_thread_rollout(second, "/home/user", {"thread_source": "user"})
    _fake_proc(tmp_path, monkeypatch, {10: {"ppid": 1, "fds": [first, second]}})

    assert codex_ctx.open_rollout_for_pid_tree(10) is None
    assert codex_ctx.open_rollout_for_pids([]) is None


def test_rollout_for_session_matches_thread_uuid(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    uuid = "01900000-0000-7000-8000-000000000001"
    wanted = str(d / f"rollout-2026-07-31T12-32-35-{uuid}.jsonl")
    _write_thread_rollout(wanted, "/home/user")
    _write_thread_rollout(str(d / "rollout-2026-07-31T13-00-00-other-uuid.jsonl"), "/home/user")

    assert codex_ctx.rollout_for_session(uuid) == wanted
    assert codex_ctx.rollout_for_session("missing-uuid") is None
    assert codex_ctx.rollout_for_session(None) is None


def test_rollout_for_pane_prefers_fd_and_falls_back_only_when_blind(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    own = str(d / "rollout-own.jsonl")
    sub = str(d / "rollout-sub.jsonl")
    _write_thread_rollout(own, "/home/user", {"thread_source": "user"}, used=180000)
    _write_thread_rollout(sub, "/home/user", {"parent_thread_id": "01900000"}, used=316000)
    os.utime(own, (1000, 1000))
    os.utime(sub, (2000, 2000))  # a busy sub-agent is the newest rollout for this cwd
    _fake_proc(tmp_path, monkeypatch, {
        10: {"ppid": 1, "fds": [own, sub]},   # pane resolves exactly
        20: {"ppid": 1, "fds": [sub]},        # only a sub-agent visible: unusable
        30: {"ppid": 1},                      # blind: no rollout fd at all
    })

    assert codex_ctx.rollout_for_pane(10, "/home/user") == own
    assert codex_ctx.ctx_pct_for_pane(10, "/home/user") == 45
    # Unusable fd evidence must NOT fall through to the cwd lookup: that answers with
    # the sub-agent's file (79%), i.e. a wrong number instead of no number.
    assert codex_ctx.rollout_for_pane(20, "/home/user") is None
    assert codex_ctx.ctx_pct_for_pane(20, "/home/user") is None
    # Blind (no fd evidence at all): the cwd may answer, but only where it is itself
    # unambiguous — one root thread. It must never hand back the sub-agent's file.
    assert codex_ctx.rollout_for_pane(30, "/home/user") == own
    assert codex_ctx.ctx_pct_for_cwd("/home/user") == 79  # what the old code reported

    second_root = str(d / "rollout-second-root.jsonl")
    _write_thread_rollout(second_root, "/home/user", {"thread_source": "user"}, used=8000)
    assert codex_ctx.rollout_for_pane(30, "/home/user") is None  # two panes, one cwd


def test_ctx_pct_for_pane_without_a_pid_uses_the_cwd_lookup(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    own = str(d / "rollout-own.jsonl")
    _write_thread_rollout(own, "/home/user", {"thread_source": "user"}, used=180000)
    _fake_proc(tmp_path, monkeypatch, {})

    assert codex_ctx.ctx_pct_for_pane(None, "/home/user") == 45
    assert codex_ctx.ctx_pct_for_pane(None, "/nowhere") is None


# ---- daemon call site: the pane's pid must reach the lookup (#123) ------------

def test_codex_ctx_dict_passes_the_panes_pid_and_cwd(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon, "pane_pid", lambda pane: 4242)
    monkeypatch.setattr(daemon, "pane_cwd", lambda pane: "/home/user")
    monkeypatch.setattr(daemon.codex_ctx, "ctx_pct_for_pane",
                        lambda pid, cwd: seen.append((pid, cwd)) or 46)

    assert daemon._codex_ctx_dict("%16") == {"pct": 46}
    assert seen == [(4242, "/home/user")]


def test_codex_ctx_dict_survives_a_pid_lookup_failure(monkeypatch):
    def boom(pane):
        raise RuntimeError("tmux hiccup")

    monkeypatch.setattr(daemon, "pane_pid", boom)
    monkeypatch.setattr(daemon, "pane_cwd", lambda pane: "/home/user")
    monkeypatch.setattr(daemon.codex_ctx, "ctx_pct_for_pane",
                        lambda pid, cwd: 16 if pid is None else None)

    # The cwd fallback still answers rather than the readout going dark.
    assert daemon._codex_ctx_dict("%16") == {"pct": 16}


def test_an_unclassifiable_open_rollout_makes_the_set_unusable(tmp_path, monkeypatch):
    d = _sessions_dir(tmp_path, monkeypatch)
    own = str(d / "rollout-own.jsonl")
    broken = str(d / "rollout-broken.jsonl")
    _write_thread_rollout(own, "/home/user", {"thread_source": "user"})
    with open(broken, "w") as f:
        f.write("not-json\n")  # could itself be the pane's thread — we cannot tell
    _fake_proc(tmp_path, monkeypatch, {10: {"ppid": 1, "fds": [own, broken]}})

    opened, roots = codex_ctx.open_rollouts_for_pids([10])

    assert len(opened) == 2 and roots == []
    assert codex_ctx.rollout_for_pane(10, "/home/user") is None


def test_pane_locators_degrade_independently(monkeypatch):
    def boom(pane):
        raise RuntimeError("tmux hiccup")

    monkeypatch.setattr(daemon, "pane_pid", boom)
    monkeypatch.setattr(daemon, "pane_cwd", lambda pane: "/home/user")
    assert daemon._pane_locators("%16") == (None, "/home/user")

    monkeypatch.setattr(daemon, "pane_pid", lambda pane: 4242)
    monkeypatch.setattr(daemon, "pane_cwd", boom)
    assert daemon._pane_locators("%16") == (4242, None)


def test_digest_row_degrades_when_a_pane_lookup_fails(monkeypatch):
    from bridge import digest

    def boom(pane):
        raise RuntimeError("tmux hiccup")

    monkeypatch.setattr(digest, "pane_pid", boom)
    monkeypatch.setattr(digest, "pane_cwd", lambda pane: "/home/user")
    monkeypatch.setattr(digest, "fleet_panes", lambda: [("%16", "s", "t", "codex")])
    monkeypatch.setattr(digest, "registry_by_pane",
                        lambda: {"%16": ("8265", {"name": "agents-cleanup"})})
    monkeypatch.setattr(digest, "unread_count", lambda tid: 0)

    lines = digest.session_lines({}, {})

    assert any("ctx n/a" in line for line in lines)
