"""Unit tests for forum open/closed tracking (#161).

The morning digest listed topics the owner had closed in Telegram. The registry's `ended` flag was
already excluded and is a different concept; what was missing is the FORUM state, which
Telegram gives bots no way to read — there is no getForumTopic, and sendChatAction is not
enforced against closed topics (measurement returned OK for open and long-dead topics alike).
It can only be learned from an event: the forum_topic_closed /
forum_topic_reopened service messages, or a send Telegram rejects with TOPIC_CLOSED."""

import json

from bridge import common, daemon, digest


def _registry(tmp_path, monkeypatch, entries):
    """Point the REAL update_registry at a temp file — locking, temp write and atomic
    replace included — rather than stubbing it, so these tests exercise the actual
    persistence path (Codex review of PR #162)."""
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(json.dumps(entries))
    monkeypatch.setattr(common, "state_path", lambda *p, _r=root: str(_r.joinpath(*p)))
    monkeypatch.setattr(daemon, "state_path", lambda *p, _r=root: str(_r.joinpath(*p)))
    return root


def _read(root):
    return json.loads((root / "registry.json").read_text())


# ---- recording the state -----------------------------------------------------

def test_closing_and_reopening_a_topic(tmp_path, monkeypatch):
    root = _registry(tmp_path, monkeypatch, {"55": {"name": "bridge", "pane": "%0"}})

    daemon.set_topic_closed(55, True)
    assert _read(root)["55"]["closed"] is True

    daemon.set_topic_closed(55, False)
    assert "closed" not in _read(root)["55"]        # cleared, not left as False


def test_general_and_unknown_topics_are_ignored(tmp_path, monkeypatch):
    root = _registry(tmp_path, monkeypatch, {"55": {"name": "bridge"}})

    daemon.set_topic_closed(0, True)                # General is not a closable topic
    daemon.set_topic_closed(None, True)
    daemon.set_topic_closed(9999, True)             # not registered
    assert _read(root) == {"55": {"name": "bridge"}}


def test_service_messages_drive_the_state(tmp_path, monkeypatch):
    root = _registry(tmp_path, monkeypatch, {"55": {"name": "bridge"}})
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    # #206: the sender is now load-bearing. This test used to send these events with no
    # `from` at all, which is exactly the unauthenticated shape that reached session revival;
    # tests/test_service_event_auth.py owns that case now, as a negative.
    cfg = {"chat_id": 1, "owner_id": 7, "bot_token": "1:x"}
    owner = {"from": {"id": 7}}

    daemon.handle_message(cfg, {"chat": {"id": 1}, "message_thread_id": 55,
                                "forum_topic_closed": {}, **owner})
    assert _read(root)["55"]["closed"] is True

    daemon.handle_message(cfg, {"chat": {"id": 1}, "message_thread_id": 55,
                                "forum_topic_reopened": {}, **owner})
    assert "closed" not in _read(root)["55"]


def test_a_foreign_chat_cannot_touch_the_state(tmp_path, monkeypatch):
    # handle_message returns on a chat_id mismatch before anything else.
    root = _registry(tmp_path, monkeypatch, {"55": {"name": "bridge"}})
    daemon.handle_message({"chat_id": 1}, {"chat": {"id": 999}, "message_thread_id": 55,
                                           "forum_topic_closed": {}})
    assert "closed" not in _read(root)["55"]


# ---- learning it from a rejected send ----------------------------------------

def test_reply_marks_a_topic_closed_when_telegram_rejects_it(tmp_path, monkeypatch):
    # The only route to a topic closed BEFORE tracking began.
    root = _registry(tmp_path, monkeypatch, {"55": {"name": "bridge"}})
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)

    def reject(token, chat_id, text, thread_id=None):
        raise RuntimeError("sendMessage: Bad Request: TOPIC_CLOSED")
    monkeypatch.setattr(daemon, "send_message", reject)

    assert daemon.reply({"bot_token": "t", "chat_id": 1}, 55, "context 80%") is False
    assert _read(root)["55"]["closed"] is True


def test_reply_propagates_every_other_failure(tmp_path, monkeypatch):
    root = _registry(tmp_path, monkeypatch, {"55": {"name": "bridge"}})

    def boom(token, chat_id, text, thread_id=None):
        raise RuntimeError("sendMessage: Bad Request: message is too long")
    monkeypatch.setattr(daemon, "send_message", boom)

    try:
        daemon.reply({"bot_token": "t", "chat_id": 1}, 55, "x")
    except RuntimeError as e:
        assert "too long" in str(e)
    else:
        raise AssertionError("a non-closed-topic failure must propagate")
    assert "closed" not in _read(root)["55"]        # and must not be misfiled as closed


def test_reply_reports_delivery(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"55": {"name": "bridge"}})
    monkeypatch.setattr(daemon, "send_message", lambda *a, **k: None)
    assert daemon.reply({"bot_token": "t", "chat_id": 1}, 55, "hello") is True


def test_a_deleted_topic_is_marked_too(tmp_path, monkeypatch):
    root = _registry(tmp_path, monkeypatch, {"55": {"name": "bridge"}})
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)

    def deleted(token, chat_id, text, thread_id=None):
        raise RuntimeError("sendMessage: Bad Request: TOPIC_DELETED")
    monkeypatch.setattr(daemon, "send_message", deleted)

    assert daemon.reply({"bot_token": "t", "chat_id": 1}, 55, "x") is False
    assert _read(root)["55"]["closed"] is True


def test_topic_gone_error_detection():
    assert daemon._is_topic_gone_error(RuntimeError("Bad Request: TOPIC_CLOSED"))
    assert daemon._is_topic_gone_error(RuntimeError("the topic is closed"))
    # A DELETED topic is equally undeliverable and equally not part of the morning list.
    # The first version excluded it, which left a deleted topic in the digest for ever with
    # no event that could ever clear it — and a test enshrining that.
    assert daemon._is_topic_gone_error(RuntimeError("Bad Request: TOPIC_DELETED"))
    assert not daemon._is_topic_gone_error(RuntimeError("chat not found"))
    assert not daemon._is_topic_gone_error(RuntimeError("message is too long"))


# ---- the digest ---------------------------------------------------------------

def test_digest_lists_open_topics_only(monkeypatch):
    monkeypatch.setattr(digest, "fleet_panes", lambda: [
        ("%0", "s0", "t0", "claude"),
        ("%1", "s1", "t1", "claude"),
    ])
    monkeypatch.setattr(digest, "registry_by_pane", lambda: {
        "%0": ("55", {"name": "open one", "icon": "•"}),
        "%1": ("44", {"name": "closed one", "icon": "•", "closed": True}),
    })
    monkeypatch.setattr(digest, "unread_count", lambda tid: 0)
    monkeypatch.setattr(digest, "read_ctx_raw", lambda pane: {"pct": 40})

    lines = digest.session_lines({}, {})
    assert any("open one" in line for line in lines)
    assert not any("closed one" in line for line in lines)


def test_digest_still_shows_a_topic_with_no_state_recorded(monkeypatch):
    # Absent an event a topic counts as OPEN — the Bot API cannot tell us otherwise, so the
    # default must never hide a live session.
    monkeypatch.setattr(digest, "fleet_panes", lambda: [("%0", "s0", "t0", "claude")])
    monkeypatch.setattr(digest, "registry_by_pane", lambda: {
        "%0": ("55", {"name": "unknown state", "icon": "•"})})
    monkeypatch.setattr(digest, "unread_count", lambda tid: 0)
    monkeypatch.setattr(digest, "read_ctx_raw", lambda pane: {"pct": 40})

    assert any("unknown state" in line for line in digest.session_lines({}, {}))


# ---- callers that treat a notice as delivered before acting ------------------
#
# Codex review of PR #162: reply() swallowing the rejection made it return normally, and
# several callers read that as proof the owner was told. The worst was auto carry-forward —
# it posted the start notice, got a silent failure, and compacted and resumed the session
# anyway, leaving the owner with neither the notice nor the kill-switch it carries.

def test_auto_carry_forward_does_not_start_when_its_notice_cannot_be_delivered(monkeypatch):
    called = []
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: False)      # topic closed
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    monkeypatch.setattr(daemon, "handle_carry_forward",
                        lambda *a, **k: called.append("started"))

    fired = {}
    result = daemon._process_autocf({}, 55, {"name": "x"}, "%0", 99, "claude", set(), fired)

    assert called == []                    # the session was NOT compacted behind their back
    assert result is False
    assert fired.get(55) is False          # re-armed, so reopening the topic retries cleanly


def test_auto_carry_forward_starts_normally_when_the_notice_lands(monkeypatch):
    called = []
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    monkeypatch.setattr(daemon, "handle_carry_forward",
                        lambda *a, **k: bool(called.append("started")) or True)

    assert daemon._process_autocf({}, 55, {"name": "x"}, "%0", 99, "claude", set(), {}) is True
    assert called == ["started"]


def test_manual_carry_forward_releases_the_flow_when_its_notice_fails(monkeypatch):
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: False)
    started = []
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda *a, **k: started.append(k) or _NoThread())
    monkeypatch.setattr(daemon, "_pending_cf", {})

    daemon.handle_carry_forward({}, 55, "/carryforward", {"name": "x"}, "%0")

    assert started == []                             # no worker
    assert not daemon.carry_forward_active(55)       # and no armed flow left behind


class _NoThread:
    def start(self):
        raise AssertionError("the carry-forward worker must not start")


def test_blocked_pane_report_releases_its_cooldown_when_undelivered(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "load_config", lambda: {"bot_token": "t", "chat_id": 1})
    monkeypatch.setattr(daemon, "peek_pane", lambda pane, lines=12: "tail")
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_blocked_reported", {})

    assert daemon.report_blocked_pane(55, "%0", "a message") is False
    # Nothing was said, so the 30-minute slot must be free for the next attempt.
    assert 55 not in daemon._blocked_reported and "55" not in daemon._blocked_reported


# ---- digest: both engines, and the not-connected row ------------------------

def test_digest_filter_covers_codex_panes(monkeypatch):
    monkeypatch.setattr(digest, "fleet_panes", lambda: [("%1", "s1", "t1", "codex")])
    monkeypatch.setattr(digest, "registry_by_pane", lambda: {
        "%1": ("44", {"name": "closed codex", "icon": "•", "closed": True})})
    monkeypatch.setattr(digest, "unread_count", lambda tid: 0)
    monkeypatch.setattr(digest, "ctx_pct_for_pane", lambda pid, cwd: 55)

    assert digest.session_lines({}, {}) == ["(no sessions running)"]


def test_digest_still_reports_panes_with_no_registry_entry(monkeypatch):
    # The "not connected" row is emitted before the closed check and must be unaffected.
    monkeypatch.setattr(digest, "fleet_panes", lambda: [("%9", "stray", "t", "claude")])
    monkeypatch.setattr(digest, "registry_by_pane", lambda: {})

    assert any("not connected" in line for line in digest.session_lines({}, {}))


# ---- state must not outlive an undelivered notice ----------------------------
#
# Codex round 2 of PR #162: three more places advanced state on the strength of a notice
# that never arrived. The /kill one is the sharp one — it armed a destructive confirmation.

def test_kill_confirmation_is_not_armed_when_its_prompt_is_undeliverable(monkeypatch):
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"55": {"pane": "%0", "name": "victim"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "pending_kills", {})

    daemon.handle_command({}, 55, "/kill")

    # Otherwise a "yes" after the topic reopens kills a pane they were never asked about.
    assert daemon.pending_kills == {}


def test_kill_confirmation_is_armed_when_the_prompt_lands(monkeypatch):
    monkeypatch.setattr(daemon, "read_registry",
                        lambda: {"55": {"pane": "%0", "name": "victim"}})
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "pending_kills", {})

    daemon.handle_command({}, 55, "/kill")

    assert daemon.pending_kills[55]["pane"] == "%0"


def test_auto_carry_forward_rearms_when_the_second_notice_fails(monkeypatch):
    # The auto notice lands, then handle_carry_forward's own start notice does not and it
    # aborts. Persisting armed=True for a run that never started would strand the retry.
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "reply", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "carry_forward_active", lambda tid: False)
    monkeypatch.setattr(daemon, "handle_carry_forward", lambda *a, **k: False)

    fired = {}
    assert daemon._process_autocf({}, 55, {"name": "x"}, "%0", 99, "claude", set(), fired) is False
    assert fired.get(55) is False


def test_context_warning_threshold_is_banked_only_when_delivered(monkeypatch):
    # Not a loop test: the rung must move on a delivered warning and stay put otherwise, or
    # a close/reopen between polls loses that warning for good.
    warned = {}
    for delivered, expected in ((False, {}), (True, {55: 40})):
        warned.clear()
        monkeypatch.setattr(daemon, "reply", lambda *a, _d=delivered, **k: _d)
        monkeypatch.setattr(daemon, "log", lambda *a, **k: None)
        if daemon.reply({}, 55, "⚠️ Context window 45% used (x)"):
            warned[55] = 40
        assert warned == expected
