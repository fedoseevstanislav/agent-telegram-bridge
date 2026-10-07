"""#373: one owner-visible message, with an explicit compact/full choice."""

import contextlib
import json

import pytest

from bridge import common, daemon


CFG = {"bot_token": "test-token", "chat_id": -100, "owner_id": 42}
ENTRY = {"name": "api-refactor", "engine": "claude", "session_id": "SID",
         "cwd": "/tmp", "pane": "%9", "ended": "2026-10-07T10:00:00Z",
         "parked": "2026-10-07T10:00:00Z"}
PROMPT = "Claude Code\n────────────────────\n❯ \n────────────────────\n  Fable · 35%"
PICKER = "\n".join(daemon.RESUME_MODAL_ROWS)


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(daemon.time, "time", lambda: now[0])
    monkeypatch.setattr(daemon.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def _picker_pane(monkeypatch, clock, screen):
    keys = []
    monkeypatch.setattr(daemon, "peek_pane", lambda *a, **k: screen(clock[0]))
    monkeypatch.setattr(daemon, "_pane_lock", lambda pane: contextlib.nullcontext())
    monkeypatch.setattr(daemon, "_tmux", lambda cmd, **kw: (
        keys.append(cmd[-1]) or type("R", (), {"returncode": 0})()))
    monkeypatch.setattr(daemon, "log", lambda *a: None)
    return keys


def test_idle_prompt_ends_picker_wait_within_five_seconds(monkeypatch, clock):
    keys = _picker_pane(monkeypatch, clock, lambda t: "loading" if t < 6 else PROMPT)
    assert daemon.answer_resume_picker("%9", "compact") == "absent"
    assert 6 < clock[0] <= 11
    assert keys == []


@pytest.mark.parametrize("choice,key", [("compact", "1"), ("full", "2")])
def test_picker_during_idle_grace_is_answered_as_before(monkeypatch, clock, choice, key):
    keys = _picker_pane(monkeypatch, clock, lambda t: PROMPT if t < 2 else PICKER)
    assert daemon.answer_resume_picker("%9", choice) == "answered"
    assert keys == [key, "Enter"]


@pytest.mark.parametrize("screen", ["loading", "", "❯ 1. Yes, switch model", "❯ busy\nesc to interrupt"])
def test_no_idle_prompt_or_picker_keeps_the_full_wait(monkeypatch, clock, screen):
    keys = _picker_pane(monkeypatch, clock, lambda t: screen)
    assert daemon.answer_resume_picker("%9", "full") == "absent"
    assert clock[0] == daemon.RESUME_MODAL_WAIT
    assert keys == []


def test_a_transient_idle_prompt_does_not_end_picker_wait(monkeypatch, clock):
    keys = _picker_pane(monkeypatch, clock, lambda t: PROMPT if t in (0, 4) else PICKER if t >= 6 else "loading")
    assert daemon.answer_resume_picker("%9", "compact") == "answered"
    assert clock[0] == 6 and keys == ["1", "Enter"]


@pytest.fixture
def telegram(monkeypatch):
    """Use the real reply/send helpers; only the Telegram transport is replaced."""
    events = []
    registry = {"7005": dict(ENTRY)}
    monkeypatch.setattr(daemon, "pending_reopens", {})
    monkeypatch.setattr(daemon, "_auto_reviving", set())
    monkeypatch.setattr(daemon, "log", lambda *a: None)
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "update_registry", lambda fn: fn(registry))
    monkeypatch.setattr(daemon, "session_context_tokens", lambda *a: 352_556)

    def api(token, method, params, **kw):
        assert token == CFG["bot_token"]
        assert method in ("sendMessage", "editMessageText", "answerCallbackQuery")
        events.append((method, dict(params)))
        return {"message_id": 100 + len(events)}

    monkeypatch.setattr(common, "api", api)
    monkeypatch.setattr(daemon, "api", api)
    return events, registry, api


def _question(telegram):
    assert daemon.offer_reopen_choice(CFG, 7005, ENTRY)
    return daemon.pending_reopens["7005"]


def _callback(message_id, choice="compact", **changes):
    query = {"id": "tap-1", "from": {"id": 42}, "data": "reopen:" + choice,
             "message": {"chat": {"id": -100}, "message_thread_id": 7005,
                         "message_id": message_id}}
    query.update(changes)
    return query


def _message(text):
    return {"chat": {"id": -100}, "from": {"id": 42}, "message_id": 501,
            "message_thread_id": 7005, "text": text}


def _inline_workers(monkeypatch):
    monkeypatch.setattr(daemon.threading, "Thread", lambda target=None, **kw: type(
        "T", (), {"start": lambda self: target()})())


def _resume(monkeypatch, telegram, clock, picker="absent", compact="injected"):
    """Real worker, revive_one, and briefing, with pane I/O and model commands mocked.

    Events distinguish launching, compact settling, restoring the #353 pair, and briefing
    from the owner-visible edits; a notice emitted before any of them cannot pass ordering.
    """
    events, registry, _api = telegram
    _inline_workers(monkeypatch)
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(daemon, "_revive_tmux_name", lambda *a: "revive-test")
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "test-boot")
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session", lambda *a: ("model", "max"))
    monkeypatch.setattr(daemon, "compaction_plan", lambda *a: {"model": "model", "effort": "max"})
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda *a, **k: picker)
    monkeypatch.setattr(daemon, "_compact_after_absent_picker", lambda *a: (compact, "hook words"))
    monkeypatch.setattr(daemon, "_safe_peek", lambda *a: "")
    monkeypatch.setattr(daemon, "pane_alive", lambda *a: True)
    monkeypatch.setattr(daemon, "pane_is_idle", lambda *a: True)
    monkeypatch.setattr(daemon, "has_live_recv", lambda *a: False)

    def launch(*a, **kw):
        events.append(("launch", {}))
        clock[0] += 10
        return "%77", None

    def settled(*a):
        events.append(("settled", {}))
        clock[0] += 8
        return True

    def restore(*a):
        events.append(("restore", {}))
        clock[0] += 3
        return "ok"

    def type_line(pane, text, **kw):
        assert pane == "%77" and kw["still_ok"]()
        events.append(("briefing", {"text": text}))
        clock[0] += 4
        return "sent"

    monkeypatch.setattr(daemon, "launch_pane", launch)
    monkeypatch.setattr(daemon, "_compaction_settled", settled)
    monkeypatch.setattr(daemon, "restore_after_compaction", restore)
    monkeypatch.setattr(daemon, "type_line", type_line)


@pytest.mark.parametrize("tokens", [352_556, None])
def test_question_is_three_lines_with_two_buttons_and_persisted_id(monkeypatch, telegram, tokens):
    events, _reg, _api = telegram
    monkeypatch.setattr(daemon, "session_context_tokens", lambda *a: tokens)
    pending = _question(telegram)
    method, params = events[0]
    assert method == "sendMessage" and params["message_thread_id"] == 7005
    text = params["text"]
    assert len(text.splitlines()) <= 3 and "api-refactor" in text
    assert "352,556 tokens" in text if tokens else "size unknown" in text
    assert "<code>c</code>" in text and "<code>f</code>" in text
    buttons = json.loads(params["reply_markup"])["inline_keyboard"]
    assert buttons == [[{"text": "Compact", "callback_data": "reopen:compact"},
                        {"text": "Full", "callback_data": "reopen:full"}]]
    assert pending["message_id"] == 101 and pending["state"] == "delivered"
    assert daemon._load_pending_reopens()["7005"] == pending


@pytest.mark.parametrize("choice", ["compact", "full"])
def test_current_button_uses_the_typed_answer_path_and_removes_buttons(monkeypatch, telegram, clock, choice):
    events, _reg, _api = telegram
    pending = _question(telegram)
    calls = []
    real_check = daemon.check_pending_reopen

    def check(cfg, tid, word):
        calls.append(word)
        return real_check(cfg, tid, word)

    monkeypatch.setattr(daemon, "check_pending_reopen", check)
    monkeypatch.setattr(daemon, "_revive_with_choice", lambda *a: calls.append(a[-1]) or True)
    for name in ("handle_message", "handle_command", "append_jsonl", "halt_carry_forward"):
        monkeypatch.setattr(daemon, name, lambda *a, **k: pytest.fail("callback entered message routing"))
    daemon.handle_callback_query(CFG, _callback(pending["message_id"], choice))
    assert calls == [choice, choice]
    assert pending["state"] == "reviving"
    assert [e[0] for e in events] == ["sendMessage", "editMessageText", "answerCallbackQuery"]
    assert json.loads(events[1][1]["reply_markup"]) == {"inline_keyboard": []}


@pytest.mark.parametrize("invalid", [
    "foreign_sender", "missing_sender", "malformed_sender", "bot", "owner_unset", "owner_bool",
    "foreign_chat", "missing_chat", "malformed_chat", "unknown_data", "malformed_data",
    "old_id", "no_id", "bool_id", "foreign_thread", "no_pending", "prepared", "reviving", "fresh",
])
def test_untrusted_or_stale_callbacks_only_answer_the_query(monkeypatch, telegram, invalid):
    events, _reg, _api = telegram
    pending = _question(telegram)
    query, cfg = _callback(pending["message_id"]), dict(CFG)
    if invalid == "foreign_sender":
        query["from"]["id"] = 7
    elif invalid == "missing_sender":
        query.pop("from")
    elif invalid == "malformed_sender":
        query["from"] = "owner"
    elif invalid == "bot":
        query["from"]["is_bot"] = True
    elif invalid == "owner_unset":
        cfg.pop("owner_id")
    elif invalid == "owner_bool":
        cfg["owner_id"] = True
        query["from"]["id"] = 1
    elif invalid == "foreign_chat":
        query["message"]["chat"]["id"] = -200
    elif invalid == "missing_chat":
        query["message"].pop("chat")
    elif invalid == "malformed_chat":
        query["message"]["chat"] = "chat"
    elif invalid == "unknown_data":
        query["data"] = "full"
    elif invalid == "malformed_data":
        query["data"] = ["reopen:full"]
    elif invalid == "old_id":
        query["message"]["message_id"] += 1
    elif invalid == "no_id":
        pending.pop("message_id")
    elif invalid == "bool_id":
        query["message"]["message_id"] = True
        pending["message_id"] = 1
    elif invalid == "foreign_thread":
        query["message"]["message_thread_id"] = 7006
    elif invalid == "no_pending":
        daemon.pending_reopens.clear()
    elif invalid == "fresh":
        pending["kind"] = "fresh"
    else:
        pending["state"] = invalid
    before = json.dumps(daemon.pending_reopens, sort_keys=True)
    for name in ("check_pending_reopen", "handle_message", "handle_command", "append_jsonl",
                 "halt_carry_forward", "_revive_with_choice"):
        monkeypatch.setattr(daemon, name, lambda *a, **k: pytest.fail("unauthorized action"))
    events.clear()
    daemon.handle_callback_query(cfg, query)
    assert events == [("answerCallbackQuery", {"callback_query_id": "tap-1"})]
    assert json.dumps(daemon.pending_reopens, sort_keys=True) == before


def test_callback_is_answered_even_if_the_answer_handler_raises(monkeypatch, telegram):
    events, _reg, _api = telegram
    pending = _question(telegram)

    def fail(*a):
        raise RuntimeError("answer failed")

    monkeypatch.setattr(daemon, "check_pending_reopen", fail)
    with pytest.raises(RuntimeError, match="answer failed"):
        daemon.handle_callback_query(CFG, _callback(pending["message_id"]))
    assert events[-1] == ("answerCallbackQuery", {"callback_query_id": "tap-1"})


@pytest.mark.parametrize("choice", ["compact", "full"])
@pytest.mark.parametrize("via", ["typed", "button"])
@pytest.mark.parametrize("picker", ["absent", "answered"])
def test_success_edits_only_the_question_after_briefing(monkeypatch, telegram, clock, choice, via, picker):
    events, registry, _api = telegram
    _resume(monkeypatch, telegram, clock, picker=picker)
    pending = _question(telegram)
    clock[0] = 100  # the elapsed time starts at the answer, not the earlier question
    if via == "button":
        daemon.handle_callback_query(CFG, _callback(pending["message_id"], choice))
    else:
        daemon.handle_message(CFG, _message(choice))
    sent = [p for m, p in events if m == "sendMessage"]
    edits = [p for m, p in events if m == "editMessageText"]
    assert len(sent) == 1 and len(edits) == 2
    assert [p["message_id"] for p in edits] == [101, 101]
    assert "Reopening" in edits[0]["text"]
    assert "is back" in edits[1]["text"]
    assert ("compacted" if choice == "compact" else "in full") in edits[1]["text"]
    elapsed = 25 if choice == "compact" and picker == "absent" else 22 if choice == "compact" else 14
    assert f"{elapsed} s" in edits[1]["text"]
    order = [m for m, _p in events]
    assert order.index("briefing") < len(order) - 1
    assert order.index("briefing") < max(i for i, m in enumerate(order) if m == "editMessageText")
    if choice == "compact" and picker == "absent":
        assert order.index("settled") < order.index("restore") < order.index("briefing")
    assert daemon.pending_reopens == {} and daemon._auto_reviving == set()
    assert "reopen" not in registry["7005"]


def test_delayed_compaction_defers_final_edit_through_briefing_retry(monkeypatch, telegram, clock):
    events, _registry, _api = telegram
    _resume(monkeypatch, telegram, clock)
    retries = []
    ready = [False]
    monkeypatch.setattr(daemon, "_compaction_settled", lambda *a: ready[0])
    monkeypatch.setattr(daemon.threading, "Timer", lambda delay, fn, args=(), kwargs=None: type(
        "T", (), {"start": lambda self: retries.append((fn, args, kwargs))})())
    _question(telegram)
    daemon.check_pending_reopen(CFG, 7005, "compact")
    assert not any("is back" in p.get("text", "") for m, p in events)
    assert len(retries) == 1
    ready[0] = True
    fn, args, kwargs = retries[0]
    fn(*args, **kwargs)
    order = [m for m, _p in events]
    assert order.index("restore") < order.index("briefing") < len(order) - 1
    assert events[-1][0] == "editMessageText" and "is back" in events[-1][1]["text"]


@pytest.mark.parametrize("listener_starts", ["retry", "delivery", "typing"])
def test_listener_starting_during_briefing_still_finishes_status(monkeypatch, telegram, clock, listener_starts):
    events, _registry, _api = telegram
    _resume(monkeypatch, telegram, clock)
    retries, listening = [], [False]
    monkeypatch.setattr(daemon, "has_live_recv", lambda *a: listening[0])
    _question(telegram)
    if listener_starts == "retry":
        ready = [False]
        monkeypatch.setattr(daemon, "_compaction_settled", lambda *a: ready[0])
        monkeypatch.setattr(daemon.threading, "Timer", lambda delay, fn, args=(), kwargs=None: type(
            "T", (), {"start": lambda self: retries.append((fn, args, kwargs))})())
    elif listener_starts == "delivery":
        def idle(pane):
            listening[0] = True
            return True
        monkeypatch.setattr(daemon, "pane_is_idle", idle)
    else:
        def type_line(pane, text, **kw):
            listening[0] = True
            assert not kw["still_ok"]()
            return "abandoned"
        monkeypatch.setattr(daemon, "type_line", type_line)
    daemon.check_pending_reopen(CFG, 7005, "compact")
    if listener_starts == "retry":
        assert len(retries) == 1
        ready[0] = listening[0] = True
        fn, args, kwargs = retries[0]
        fn(*args, **kwargs)
    edits = [p for m, p in events if m == "editMessageText"]
    assert len(edits) == 2 and "is back" in edits[1]["text"]
    assert "compacted" in edits[1]["text"]
    assert [m for m, p in events].index("restore") < len(events) - 1
    assert not any(m == "briefing" for m, p in events), "started a second listener"


def test_listener_on_a_rebound_pane_does_not_finish_the_old_status(monkeypatch, telegram, clock):
    events, registry, _api = telegram
    _resume(monkeypatch, telegram, clock)
    _question(telegram)

    def idle(pane):
        registry["7005"]["pane"] = "%999"
        monkeypatch.setattr(daemon, "has_live_recv", lambda *a: True)
        return True

    monkeypatch.setattr(daemon, "pane_is_idle", idle)
    daemon.check_pending_reopen(CFG, 7005, "compact")
    assert registry["7005"]["pane"] == "%999"
    assert not any("is back" in p.get("text", "") for m, p in events)
    assert not any(m == "briefing" for m, p in events)


@pytest.mark.parametrize("stage", ["progress", "final"])
def test_failed_edit_sends_the_same_text_and_tracks_the_fallback(monkeypatch, telegram, clock, stage):
    events, _registry, real_api = telegram
    _resume(monkeypatch, telegram, clock)
    _question(telegram)
    rejected = []

    def api(token, method, params, **kw):
        if method == "editMessageText" and not rejected and (
                ("is back" in params["text"]) == (stage == "final")):
            rejected.append(dict(params))
            raise RuntimeError("cannot edit")
        return real_api(token, method, params, **kw)

    monkeypatch.setattr(daemon, "api", api)
    daemon.check_pending_reopen(CFG, 7005, "full")
    sends = [p for m, p in events if m == "sendMessage"]
    assert len(sends) == 2 and sends[1]["text"] == rejected[0]["text"]
    if stage == "progress":
        edits = [p for m, p in events if m == "editMessageText"]
        assert len(edits) == 1 and edits[0]["message_id"] == 102


@pytest.mark.parametrize("word", list(daemon.REOPEN_COMPACT_WORDS) + list(daemon.REOPEN_FULL_WORDS)
                         + [" C! ", "FULL."])
def test_lone_answer_skips_question_and_inbox_with_one_status(monkeypatch, telegram, clock, word):
    events, registry, _api = telegram
    _resume(monkeypatch, telegram, clock)
    monkeypatch.setattr(daemon, "append_jsonl", lambda *a, **k: pytest.fail("answer reached inbox"))
    monkeypatch.setattr(daemon, "offer_reopen_choice", lambda *a: pytest.fail("asked after explicit choice"))
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: pytest.fail("answer nudged the pane"))
    daemon.handle_message(CFG, _message(word))
    sends = [p for m, p in events if m == "sendMessage"]
    edits = [p for m, p in events if m == "editMessageText"]
    assert len(sends) == len(edits) == 1
    assert "Reopening" in sends[0]["text"] and "nothing starts" not in sends[0]["text"]
    assert "is back" in edits[0]["text"] and edits[0]["message_id"] == 101
    assert "reopen" not in registry["7005"]


@pytest.mark.parametrize("word", ["full details", "!fully", "/full", "/compact", "full ."])
def test_other_text_and_slash_forms_keep_the_existing_routes(monkeypatch, telegram, word):
    events, _reg, _api = telegram
    routed = []
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda *a: True)
    monkeypatch.setattr(daemon, "has_unread_owner_message", lambda *a: True)
    monkeypatch.setattr(daemon, "append_jsonl", lambda path, rec: routed.append(("inbox", rec["text"])))
    monkeypatch.setattr(daemon, "handle_command", lambda cfg, tid, text: routed.append(("command", text)))
    monkeypatch.setattr(daemon, "interrupt_session", lambda cfg, tid, text: routed.append(("interrupt", text)))
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    monkeypatch.setattr(daemon, "_revive_with_choice", lambda *a: pytest.fail("unasked revive"))
    daemon.handle_message(CFG, _message(word))
    if word.startswith("/"):
        assert routed == [("command", word)] and events == []
    elif word.startswith("!"):
        assert routed == [("interrupt", word[1:])] and events == []
    else:
        assert routed == [("inbox", word)]
        assert len(events) == 1 and "nothing starts" in events[0][1]["text"]


def test_codex_answer_word_is_ordinary_inbox_content(monkeypatch, telegram):
    _events, registry, _api = telegram
    registry["7005"]["engine"] = "codex"
    monkeypatch.setattr(daemon, "has_unread_owner_message", lambda *a: True)
    inbox, revives = [], []
    monkeypatch.setattr(daemon, "append_jsonl", lambda path, rec: inbox.append(rec["text"]))
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    monkeypatch.setattr(daemon, "revive_one", lambda *a, **kw: revives.append(kw) or ("resumed", None))
    _inline_workers(monkeypatch)
    daemon.handle_message(CFG, _message("full"))
    assert inbox == ["full"] and len(revives) == 1
    assert "resume_choice" not in revives[0] and daemon.pending_reopens == {}


def test_small_claude_session_keeps_the_no_question_auto_revive(monkeypatch, telegram):
    _events, _registry, _api = telegram
    inbox, revives = [], []
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda *a: False)
    monkeypatch.setattr(daemon, "has_unread_owner_message", lambda *a: True)
    monkeypatch.setattr(daemon, "append_jsonl", lambda path, rec: inbox.append(rec["text"]))
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    monkeypatch.setattr(daemon, "revive_one", lambda *a, **kw: revives.append(kw) or ("resumed", None))
    _inline_workers(monkeypatch)
    daemon.handle_message(CFG, _message("continue the task"))
    assert inbox == ["continue the task"] and len(revives) == 1
    assert "resume_choice" not in revives[0] and daemon.pending_reopens == {}


def test_lone_word_does_not_bypass_carry_forward_kill_switch(monkeypatch, telegram):
    calls = []
    monkeypatch.setattr(daemon, "carry_forward_active", lambda *a: True)
    monkeypatch.setattr(daemon, "halt_carry_forward", lambda *a: calls.append("halt") or True)
    monkeypatch.setattr(daemon, "check_pending_reopen", lambda *a: pytest.fail("bypassed halt"))
    daemon.handle_message(CFG, _message("full"))
    assert calls == ["halt"] and daemon.pending_reopens == {}


def test_question_resend_replaces_id_and_old_tap_is_rejected(monkeypatch, telegram):
    events, _registry, _api = telegram
    pending = _question(telegram)
    original_id = pending["message_id"]
    assert daemon.check_pending_reopen(CFG, 7005, "continue the task") is False
    assert pending["message_id"] != original_id
    assert daemon._load_pending_reopens()["7005"]["message_id"] == pending["message_id"]
    monkeypatch.setattr(daemon, "check_pending_reopen", lambda *a: pytest.fail("old tap accepted"))
    events.clear()
    daemon.handle_callback_query(CFG, _callback(original_id))
    assert events == [("answerCallbackQuery", {"callback_query_id": "tap-1"})]


def test_startup_resends_prepared_question_with_buttons_and_id(monkeypatch, telegram):
    events, _registry, _api = telegram
    daemon.pending_reopens["7005"] = {"entry": dict(ENTRY), "tokens": 352_556, "state": "prepared"}
    daemon.resend_undelivered_reopen_questions(CFG)
    assert json.loads(events[0][1]["reply_markup"]) == daemon.REOPEN_KEYBOARD
    assert daemon._load_pending_reopens()["7005"]["message_id"] == 101


def test_main_requests_and_dispatches_callbacks_without_message_routing(monkeypatch, telegram):
    _events, _registry, _api = telegram
    queried, callbacks = [], []
    monkeypatch.setattr(daemon, "load_config", lambda: CFG)
    monkeypatch.setattr(daemon, "load_offset", lambda: 0)
    monkeypatch.setattr(daemon, "restore_on_boot", lambda *a: None)
    monkeypatch.setattr(daemon, "save_offset", lambda offset: None)
    monkeypatch.setattr(daemon, "resend_undelivered_reopen_questions", lambda *a: None)
    monkeypatch.setattr(daemon.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    query = _callback(101)

    def poll(token, method, params, **kw):
        assert method == "getUpdates"
        queried.append(json.loads(params["allowed_updates"]))
        if len(queried) > 1:
            raise KeyboardInterrupt
        return [{"update_id": 1, "callback_query": query}]

    monkeypatch.setattr(daemon, "api", poll)
    monkeypatch.setattr(daemon, "handle_callback_query", lambda cfg, q: callbacks.append(q))
    monkeypatch.setattr(daemon, "handle_message", lambda *a: pytest.fail("callback entered message routing"))
    with pytest.raises(KeyboardInterrupt):
        daemon.main()
    assert queried[0] == ["message", "callback_query"] and callbacks == [query]


@pytest.mark.parametrize("outcome,warning", [
    ("refused", "⚠️ /compact was refused by a PreCompact hook. The hook said: hook words. Resuming in full."),
    ("failed", "⚠️ Claude's picker did not appear and the bridge could not inject /compact. "
               "The session is resuming in full; the choice was not applied."),
    ("picker_failed", "⚠️ You chose `compact`, but Claude's resume picker could not be answered. "
                      "The session is resuming with its FULL context. Nothing was lost — but the choice was not applied."),
    ("unanswered", daemon.UNANSWERED_PICKER_NOTICE),
])
def test_compact_warnings_are_separate_messages_with_unchanged_words(monkeypatch, telegram, clock, outcome, warning):
    events, _registry, _api = telegram
    _resume(monkeypatch, telegram, clock, picker="failed" if outcome == "picker_failed" else "absent",
            compact="failed" if outcome == "unanswered" else outcome)
    if outcome == "unanswered":
        monkeypatch.setattr(daemon, "_safe_peek", lambda *a: PICKER)
        # Do not claim a completed reopening while the picker is still up.
        monkeypatch.setattr(daemon, "deliver_briefing", lambda *a, **kw: None)
    _question(telegram)
    daemon.check_pending_reopen(CFG, 7005, "compact")
    sends = [p["text"] for m, p in events if m == "sendMessage"]
    assert sends[1:] == [daemon.md_to_telegram_html("⚙️ " + warning)]
    if outcome != "unanswered":
        assert "in full" in events[-1][1]["text"]


@pytest.mark.parametrize("failure", ["failed", "crashed", "could_not_start"])
def test_worker_failures_keep_warning_words_and_rearm_question(monkeypatch, telegram, clock, failure):
    events, _registry, _api = telegram
    _resume(monkeypatch, telegram, clock)
    pending = _question(telegram)
    if failure == "could_not_start":
        def thread(**kw):
            raise RuntimeError("cannot create thread")
        monkeypatch.setattr(daemon.threading, "Thread", thread)
        warning = "I could not start the revive just now — the question is still open, answer again in a moment."
    elif failure == "crashed":
        def revive(*a, **kw):
            raise RuntimeError("revive crashed")
        monkeypatch.setattr(daemon, "revive_one", revive)
        warning = ("⚠️ The revive crashed — the session is still down. The question is open again: "
                   "reply `compact`/`c` or `full`/`f` to retry.")
    else:
        monkeypatch.setattr(daemon, "revive_one", lambda *a, **kw: ("failed", None))
        warning = ("⚠️ The revive failed — the session is still down. The question is open again: "
                   "reply `compact`/`c` or `full`/`f` to retry.")
    daemon.check_pending_reopen(CFG, 7005, "compact")
    sends = [p["text"] for m, p in events if m == "sendMessage"]
    assert sends[1:] == [daemon.md_to_telegram_html("⚙️ " + warning)]
    restored = daemon.pending_reopens["7005"]
    assert restored["state"] == "delivered" and restored["message_id"] == pending["message_id"]
    assert json.loads([p for m, p in events if m == "editMessageText"][-1]["reply_markup"]) == daemon.REOPEN_KEYBOARD
    assert not any("is back" in p.get("text", "") for m, p in events)


def test_changed_topic_keeps_the_original_warning_and_does_not_revive(monkeypatch, telegram):
    events, registry, _api = telegram
    _question(telegram)
    registry["7005"]["session_id"] = "NEW"
    monkeypatch.setattr(daemon, "_revive_with_choice", lambda *a: pytest.fail("stale topic revived"))
    assert daemon.check_pending_reopen(CFG, 7005, "full")
    warning = ("That topic changed since I asked — it is no longer the dead session I offered to reopen, "
               "so I have done nothing. Reopen it again if you still want it back.")
    assert events[-1] == ("sendMessage", {"chat_id": -100, "message_thread_id": 7005,
                                       "text": "⚙️ " + warning, "parse_mode": "HTML"})
    assert daemon.pending_reopens == {}


def test_question_persistence_failure_keeps_the_original_warning(monkeypatch, telegram):
    events, _reg, _api = telegram
    monkeypatch.setattr(daemon, "_save_pending_reopens", lambda: False)
    _question(telegram)
    warning = ("(I could not write this question to disk. It still works — but if the bridge restarts "
               "before you answer, close and reopen the topic to get it back.)")
    assert [p["text"] for m, p in events if m == "sendMessage"][1:] == ["⚙️ " + warning]
    assert daemon.pending_reopens["7005"]["state"] == "delivered"


def test_live_listener_final_edit_still_waits_for_model_restore(monkeypatch, telegram, clock):
    events, registry, _api = telegram
    _resume(monkeypatch, telegram, clock)
    _question(telegram)
    monkeypatch.setattr(daemon, "has_live_recv", lambda *a: True)
    daemon.check_pending_reopen(CFG, 7005, "compact")
    order = [m for m, _p in events]
    assert "restore" in order and "briefing" not in order
    assert events[-1][0] == "editMessageText" and "is back" in events[-1][1]["text"]
    assert registry["7005"]["pane"] == "%77"


def test_failed_model_restore_does_not_post_a_successful_back_line(monkeypatch, telegram, clock):
    events, _registry, _api = telegram
    _resume(monkeypatch, telegram, clock)
    _question(telegram)
    monkeypatch.setattr(daemon, "restore_after_compaction", lambda *a: "failed")
    daemon.check_pending_reopen(CFG, 7005, "compact")
    assert any(m == "briefing" for m, p in events)
    assert not any("is back" in p.get("text", "") for m, p in events)


def test_voice_transcribed_as_full_is_queued_and_asks_without_resuming(monkeypatch, telegram):
    events, _registry, _api = telegram
    inbox = []
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda *a: True)
    monkeypatch.setattr(daemon, "has_unread_owner_message", lambda *a: True)
    monkeypatch.setattr(daemon, "download_file", lambda *a: None)
    monkeypatch.setattr(daemon, "transcribe", lambda *a: "Full.")
    monkeypatch.setattr(daemon, "openai_api_key", lambda: "test-key")
    monkeypatch.setattr(daemon, "append_jsonl", lambda path, rec: inbox.append(rec))
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    monkeypatch.setattr(daemon, "_revive_with_choice", lambda *a: pytest.fail("voice authorized full resume"))
    msg = _message("")
    msg["voice"] = {"file_id": "voice-file"}
    daemon.handle_message(CFG, msg)
    assert len(inbox) == 1 and inbox[0]["text"] == "Full." and inbox[0]["kind"] == "voice"
    assert len(events) == 1 and "nothing starts" in events[0][1]["text"]
    assert daemon.pending_reopens["7005"]["state"] == "delivered"


@pytest.mark.parametrize("via", ["typed", "button", "lone"])
@pytest.mark.parametrize("error", [RuntimeError, common.PossiblyDelivered])
def test_progress_edit_and_send_outage_still_starts_the_authorized_revive(monkeypatch, telegram, clock, via, error):
    events, registry, real_api = telegram
    _resume(monkeypatch, telegram, clock)
    pending = _question(telegram) if via != "lone" else None
    rejected = []

    def api(token, method, params, **kw):
        if method in ("sendMessage", "editMessageText") and "Reopening" in params["text"]:
            rejected.append(method)
            raise error("status delivery failed")
        return real_api(token, method, params, **kw)

    monkeypatch.setattr(daemon, "api", api)
    monkeypatch.setattr(common, "api", api)
    if via == "button":
        daemon.handle_callback_query(CFG, _callback(pending["message_id"], "full"))
    else:
        daemon.handle_message(CFG, _message("full"))
    assert rejected == (["sendMessage"] if via == "lone" else ["editMessageText", "sendMessage"])
    assert registry["7005"]["pane"] == "%77" and "ended" not in registry["7005"]
    assert any(m == "briefing" for m, p in events)
    assert daemon.pending_reopens == {} and daemon._auto_reviving == set()
    assert any("is back" in p.get("text", "") for m, p in events)


@pytest.mark.parametrize("listening", [False, True])
def test_final_edit_and_send_outage_does_not_report_a_session_crash(monkeypatch, telegram, clock, listening):
    events, registry, real_api = telegram
    _resume(monkeypatch, telegram, clock)
    _question(telegram)
    monkeypatch.setattr(daemon, "has_live_recv", lambda *a: listening)
    rejected = []

    def api(token, method, params, **kw):
        if method in ("sendMessage", "editMessageText") and "is back" in params["text"]:
            rejected.append(method)
            raise RuntimeError("final status delivery failed")
        return real_api(token, method, params, **kw)

    monkeypatch.setattr(daemon, "api", api)
    monkeypatch.setattr(common, "api", api)
    assert daemon.check_pending_reopen(CFG, 7005, "compact")
    assert rejected == ["editMessageText", "sendMessage"]
    assert registry["7005"]["pane"] == "%77" and "ended" not in registry["7005"]
    assert daemon.pending_reopens == {} and daemon._auto_reviving == set()
    assert len([p for m, p in events if m == "sendMessage"]) == 1
