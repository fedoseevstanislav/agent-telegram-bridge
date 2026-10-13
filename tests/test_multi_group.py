"""#377: repeat Telegram thread 7 in two groups and exercise the real routing seams.

The transport and terminal are fake; registry, inbox/cursor/outbox, questions, warnings,
parking and revival use production paths under conftest's isolated HOME/state directory.
"""

import copy
import hashlib
import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from bridge import cli, common, daemon, digest, model_watchdog, restore_cli, watchdog


CFG = {"bot_token": "101:fake-work", "chat_id": -100, "owner_id": 42,
       "groups": [{"name": "personal", "chat_id": -200, "bot_token": "202:fake-personal"}]}


@pytest.fixture
def cfg():
    return copy.deepcopy(CFG)


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(common, "CONFIG_PATH", str(path))

    def write(cfg):
        path.write_text(json.dumps(cfg))
        path.chmod(0o600)
        return common.load_config()
    return write


@pytest.fixture
def transport(monkeypatch):
    calls = []
    cli._ICON_SET_CACHE.clear()

    def urlopen(req, **kw):
        token, method = req.full_url.split("/bot", 1)[1].split("/", 1)
        if req.get_header("Content-type", "").startswith("multipart/"):
            # The file assertions inspect the actual multipart bytes as well as the URL.
            params = {"multipart": req.data}
        else:
            params = {k: v[0] for k, v in parse_qs(req.data.decode()).items()}
        calls.append((token, method, params))
        result = ([] if method == "getForumTopicIconStickers" else
                  {"message_id": 100 + len(calls), "message_thread_id": 7})
        return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())

    monkeypatch.setattr(common.urllib.request, "urlopen", urlopen)
    yield calls
    cli._ICON_SET_CACHE.clear()


@pytest.fixture
def seats(monkeypatch, tmp_path):
    entries = {
        "7": {"name": "Work", "pane": "%1", "engine": "claude", "session_id": "work-sid",
              "created": "2026-10-01T00:00:00Z", "cwd": str(tmp_path), "icon": "🦊"},
        "personal:7": {"name": "Personal", "pane": "%2", "engine": "claude",
                       "session_id": "personal-sid", "created": "2026-10-02T00:00:00Z",
                       "cwd": str(tmp_path), "icon": "🦉"},
    }
    common.update_registry(lambda reg: reg.update(entries))
    for name in ("pending_reopens", "pending_kills", "_pending_cf", "_PARK_REFUSALS"):
        monkeypatch.setattr(daemon, name, {})
    monkeypatch.setattr(daemon, "_auto_reviving", set())
    monkeypatch.setattr(daemon, "pane_alive", lambda p: True)
    monkeypatch.setattr(cli, "pane_alive", lambda p: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda p: "claude")
    monkeypatch.setattr(daemon, "pane_is_idle", lambda p: True)
    monkeypatch.setattr(daemon, "session_context_tokens", lambda *a: 350_000)
    return entries


def message(text="hello", chat=-200, **extra):
    return {"chat": {"id": chat}, "from": {"id": 42, "first_name": "Owner"},
            "message_thread_id": 7, "message_id": 501, "text": text, **extra}


def inline_threads(monkeypatch):
    def thread(target, args=(), kwargs=None, **kw):
        return SimpleNamespace(start=lambda: target(*args, **(kwargs or {})))
    monkeypatch.setattr(daemon.threading, "Thread", thread)


def one_poll(monkeypatch, interval):
    count = 0

    def sleep(seconds):
        nonlocal count
        if seconds == interval:
            count += 1
            if count > 1:
                raise KeyboardInterrupt
    monkeypatch.setattr(daemon.time, "sleep", sleep)


def test_single_group_config_keeps_bare_keys_and_credentials(config_file):
    cfg = {k: v for k, v in CFG.items() if k != "groups"}
    assert config_file(cfg) == cfg
    assert common.topic_key(cfg, "work", 7) == 7
    assert common.normalize_topic_key("007") == 7
    assert common.topic_credentials(cfg, "7") == (cfg["bot_token"], -100, 7)
    assert daemon.polling_bots(cfg) == [(cfg["bot_token"], None)]


@pytest.mark.parametrize("groups,problem", [
    ({}, "list"), (None, "list"), (["bad"], "object"),
    ([{"chat_id": -200, "bot_token": "SECRET"}], "name"),
    ([{"name": "../escape", "chat_id": -200, "bot_token": "SECRET"}], "name"),
    ([{"name": "Upper", "chat_id": -200, "bot_token": "SECRET"}], "name"),
    ([{"name": "work", "chat_id": -200, "bot_token": "SECRET"}], "repeats a group name"),
    ([{"name": "personal", "chat_id": -100, "bot_token": "SECRET"}], "repeats a chat_id"),
    ([{"name": "personal", "chat_id": True, "bot_token": "SECRET"}], "chat_id"),
    ([{"name": "personal", "chat_id": "-200", "bot_token": "SECRET"}], "chat_id"),
    ([{"name": "personal", "chat_id": 200, "bot_token": "SECRET"}], "chat_id"),
    ([{"name": "personal", "chat_id": -200}], "exactly one"),
    ([{"name": "personal", "chat_id": -200, "bot_token": "SECRET", "bot_token_file": "x"}], "exactly one"),
    ([{"name": "personal", "chat_id": -200, "bot_token": 123}], "bot_token"),
    ([{"name": "personal", "chat_id": -200, "bot_token": "SECRET\nvalue"}], "control"),
    ([{"name": "personal", "chat_id": -200, "bot_token": ""}], "empty"),
    ([{"name": "personal", "chat_id": -200, "bot_token_file": None}], "path"),
    ([{"name": "personal", "chat_id": -200, "bot_token": "SECRET"},
      {"name": "personal", "chat_id": -300, "bot_token": "SECRET"}], "repeats a group name"),
    ([{"name": "personal", "chat_id": -200, "bot_token": "SECRET"},
      {"name": "other", "chat_id": -200, "bot_token": "SECRET"}], "repeats a chat_id"),
])
def test_invalid_groups_refuse_without_secrets(config_file, cfg, groups, problem, capsys):
    cfg["groups"] = groups
    with pytest.raises(SystemExit, match=problem) as exc:
        config_file(cfg)
    assert "SECRET" not in str(exc.value) + str(capsys.readouterr())


def test_custom_default_group_is_reserved_and_bare(config_file, cfg):
    cfg["default_group"] = "office"
    cfg = config_file(cfg)
    assert common.topic_key(cfg, "office", 7) == 7
    assert common.topic_key(cfg, "personal", 7) == "personal:7"
    cfg["groups"][0]["name"] = "office"
    with pytest.raises(SystemExit, match="repeats a group name"):
        config_file(cfg)
    cfg["default_group"] = "invalid/name"
    with pytest.raises(SystemExit, match="default_group"):
        config_file(cfg)


def test_token_file_has_descriptor_secret_checks(config_file, cfg, tmp_path, monkeypatch):
    token = tmp_path / "bot-token"
    token.write_text("202:file-secret\n")
    token.chmod(0o600)
    cfg["groups"][0].pop("bot_token")
    cfg["groups"][0]["bot_token_file"] = str(token)
    assert config_file(cfg)["groups"][0]["bot_token"] == "202:file-secret"
    token.chmod(0o640)
    with pytest.raises(SystemExit, match="permission bits") as exc:
        config_file(cfg)
    assert "file-secret" not in str(exc.value)
    token.chmod(0o600)
    real_open = common._open_secret_file

    def foreign(path):
        if path == str(token):
            return None, "is owned by another uid"
        return real_open(path)
    monkeypatch.setattr(common, "_open_secret_file", foreign)
    with pytest.raises(SystemExit, match="owned"):
        config_file(cfg)


def test_token_file_swap_does_not_replace_checked_bytes(config_file, cfg, tmp_path, monkeypatch):
    token = tmp_path / "bot-token"
    token.write_text("202:original")
    token.chmod(0o600)
    replacement = tmp_path / "replacement"
    replacement.write_text("303:attacker")
    replacement.chmod(0o666)
    cfg["groups"][0].pop("bot_token")
    cfg["groups"][0]["bot_token_file"] = str(token)
    real_open = common._open_secret_file

    def swap(path):
        result = real_open(path)
        if path == str(token):
            os.replace(replacement, token)
        return result
    monkeypatch.setattr(common, "_open_secret_file", swap)
    assert config_file(cfg)["groups"][0]["bot_token"] == "202:original"


def test_bot_api_errors_cannot_echo_tokens(cfg, monkeypatch):
    token = cfg["groups"][0]["bot_token"]

    def fail(req, **kw):
        raise ValueError("bad URL " + req.full_url)
    monkeypatch.setattr(common.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError) as exc:
        common.api(token, "getUpdates")
    assert token not in str(exc.value) and exc.value.__suppress_context__


def test_distinct_bots_share_only_their_own_poller_and_offsets(cfg):
    cfg["groups"].append({"name": "research", "chat_id": -300, "bot_token": cfg["bot_token"]})
    bots = daemon.polling_bots(cfg)
    assert len(bots) == 2
    default_key = hashlib.sha256(cfg["bot_token"].encode()).hexdigest()
    assert bots[0] == (cfg["bot_token"], default_key)
    fingerprint = hashlib.sha256(cfg["groups"][0]["bot_token"].encode()).hexdigest()
    assert bots[1] == (cfg["groups"][0]["bot_token"], fingerprint)
    daemon.save_offset(17)
    assert daemon.initial_bot_offset(cfg, *bots[0], legacy_offset=17) == 17
    daemon.save_offset(99, fingerprint)
    assert daemon.load_offset(default_key) == 17 and daemon.load_offset(fingerprint) == 99
    assert Path(common.state_path("offset")).read_text() == "17"
    assert not any(token in Path(common.STATE_DIR).as_posix() for token, _key in bots)


def test_main_starts_one_poller_per_token(cfg, monkeypatch, seats):
    cfg["groups"].append({"name": "research", "chat_id": -300, "bot_token": cfg["bot_token"]})
    started, foreground = [], []
    monkeypatch.setattr(daemon, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon, "restore_on_boot", lambda *a: None)
    monkeypatch.setattr(daemon, "resend_undelivered_reopen_questions", lambda *a: None)
    monkeypatch.setattr(daemon.threading, "Thread", lambda **kw: SimpleNamespace(
        start=lambda: started.append(kw)))

    def poll(*args, **kw):
        foreground.append((args, kw))
        raise KeyboardInterrupt
    monkeypatch.setattr(daemon, "poll_updates", poll)
    with pytest.raises(KeyboardInterrupt):
        daemon.main()
    extra = [s for s in started if s["target"] is poll]
    assert len(extra) == 1 and extra[0]["args"][1] == cfg["groups"][0]["bot_token"]
    assert foreground[0][0] == (cfg, *daemon.polling_bots(cfg)[0])


@pytest.mark.parametrize("bot_index", [0, 1])
def test_poller_resumes_its_persisted_offset(cfg, monkeypatch, bot_index):
    token, key = daemon.polling_bots(cfg)[bot_index]
    daemon.save_offset(50) if key is None else daemon.save_offset(50, key)
    offsets = []

    def poll(token_arg, method, params, **kw):
        assert token_arg == token and method == "getUpdates"
        offsets.append(params["offset"])
        if len(offsets) > 1:
            raise KeyboardInterrupt
        return [{"update_id": 72}]
    monkeypatch.setattr(daemon, "api", poll)
    monkeypatch.setattr(daemon, "route_update", lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        daemon.poll_updates(cfg, token, key)
    assert offsets == [50, 73]
    assert daemon.load_offset(key) == 73
    other_key = daemon.polling_bots(cfg)[1 - bot_index][1]
    assert daemon.load_offset(other_key) == 0
    assert daemon.load_offset() == (73 if bot_index == 0 else 0)


def test_default_bot_change_and_promotion_keep_per_token_offsets(cfg):
    # The legacy file is adopted before either poller, including a bot with no new updates.
    daemon.save_offset(71)
    original = daemon.polling_bots(cfg)
    assert daemon.initial_bot_offset(cfg, *original[0], legacy_offset=71) == 71
    assert daemon.initial_bot_offset(cfg, *original[1]) == 0
    daemon.save_offset(99, original[1][1])
    old_token = cfg["bot_token"]
    cfg["bot_token"] = "303:fake-new"
    cfg["groups"].append({"name": "previous", "chat_id": -300, "bot_token": old_token})
    bots = daemon.polling_bots(cfg)
    assert daemon.initial_bot_offset(cfg, *bots[0], legacy_offset=71) == 0
    assert daemon.initial_bot_offset(cfg, *bots[2]) == 71
    # Promoting the existing extra bot must use its 99, rather than the default's zero.
    cfg["bot_token"] = "202:fake-personal"
    assert daemon.initial_bot_offset(cfg, *daemon.polling_bots(cfg)[0]) == 99
    assert daemon.load_offset() == 99


@pytest.mark.parametrize("attachment", ["photo", "document", "voice"])
def test_download_error_redacts_bot_url_before_inbox_or_log(cfg, seats, monkeypatch, capsys, attachment):
    token = cfg["groups"][0]["bot_token"]
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    monkeypatch.setattr(common, "api", lambda *a, **kw: {"file_path": "payload"})

    def fail(url, **kw):
        raise ValueError("invalid URL " + url)
    monkeypatch.setattr(common.urllib.request, "urlopen", fail)
    msg = message("")
    msg[attachment] = ([{"file_id": "id"}] if attachment == "photo" else
                       {"file_id": "id", "file_name": "result.txt", "mime_type": "text/plain"})
    daemon.handle_message(cfg, msg)
    records, _ = cli.read_new("personal:7", 0)
    assert records and "[bot token redacted]" in records[0]["text"]
    assert token not in json.dumps(records) + capsys.readouterr().err
    assert not list(Path(common.STATE_DIR).rglob("*.part-*"))


def test_unknown_or_wrong_bot_chat_is_content_free(cfg, seats, monkeypatch, capsys):
    monkeypatch.setattr(daemon, "handle_message", lambda *a: pytest.fail("foreign ingress"))
    for chat in (-999, -200):
        daemon.route_update(cfg, cfg["bot_token"], {"update_id": 123,
                                                   "message": message("PRIVATE BODY", chat=chat)})
    log = capsys.readouterr().err
    assert "ignored update 123" in log and "PRIVATE BODY" not in log
    assert not Path(common.state_path("topics", "personal:7", "inbox.jsonl")).exists()


def test_shared_bot_maps_chat_to_its_group(cfg, seats, monkeypatch):
    cfg["groups"][0]["bot_token"] = cfg["bot_token"]
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    daemon.route_update(cfg, cfg["bot_token"], {"update_id": 1, "message": message()})
    assert cli.read_new("personal:7", 0)[0][0]["thread_id"] == "personal:7"
    assert cli.read_new(7, 0)[0] == []


@pytest.mark.parametrize("source", ["argument", "env", "file"])
def test_cli_key_resolution(source, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(topic="personal:7" if source == "argument" else None)
    if source == "env":
        monkeypatch.setenv("TG_BRIDGE_TOPIC", "personal:7")
    elif source == "file":
        Path(cli.CWD_BINDING_FILE).write_text("personal:7")
    assert cli.resolve_topic(args) == "personal:7"


def test_register_group_uses_its_bot_and_binds_key(cfg, seats, transport, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%3")
    args = cli.build_parser().parse_args(["register", "--name", "Research", "--group", "personal", "--bind"])
    cli.cmd_register(cfg, args)
    assert Path(cli.CWD_BINDING_FILE).read_text() == "personal:7"
    assert common.read_registry()["personal:7"]["pane"] == "%3"
    assert "topic_id=personal:7" in capsys.readouterr().out
    assert [c[:2] for c in transport] == [("202:fake-personal", "getForumTopicIconStickers"),
                                        ("202:fake-personal", "createForumTopic")]
    assert transport[-1][2]["chat_id"] == "-200"
    assert common.read_registry()["7"]["pane"] == "%1"


def test_icons_cached_per_bot_and_retheme_routes_each_group(cfg, seats, transport):
    assert cli.icon_emoji_ids(common.group_config(cfg)) == {}
    assert cli.icon_emoji_ids(common.group_config(cfg, "personal")) == {}
    assert len(transport) == 2
    # Use a bot-settable icon in the fake cache to drive the edit call itself.
    for token in (cfg["bot_token"], cfg["groups"][0]["bot_token"]):
        cli._ICON_SET_CACHE[hashlib.sha256(token.encode()).hexdigest()] = {"🔬": "123"}
    common.update_registry(lambda reg: reg["personal:7"].update(name="Research"))
    cli.cmd_retheme(cfg, SimpleNamespace(apply=True))
    assert transport[-1] == ("202:fake-personal", "editForumTopic",
                             {"chat_id": "-200", "message_thread_id": "7", "icon_custom_emoji_id": "123"})


def test_cli_send_recv_ask_typing_and_current_topic(cfg, seats, transport, monkeypatch, capsys):
    monkeypatch.setenv("TMUX_PANE", "%2")
    inbox = common.state_path("topics", "personal:7", "inbox.jsonl")
    common.append_jsonl(inbox, {"text": "incoming", "kind": "text", "ts": common.now_iso(), "from": "Owner"})
    cli.cmd_recv(cfg, cli.build_parser().parse_args(["recv", "--topic", "personal:7"]))
    assert "incoming" in capsys.readouterr().out and cli.load_cursor("personal:7") == 1
    cli.cmd_send(cfg, cli.build_parser().parse_args(["send", "outgoing", "--topic", "personal:7"]))
    def answer(*args):
        record = {"text": "answer", "kind": "text", "ts": common.now_iso(), "from": "Owner"}
        common.append_jsonl(inbox, record)
        return [record], 2, 0, 1
    monkeypatch.setattr(cli, "wait_for_messages", answer)
    cli.cmd_ask(cfg, cli.build_parser().parse_args(["ask", "question", "--topic", "personal:7"]))
    cli.cmd_typing(cfg, cli.build_parser().parse_args(["typing", "--topic", "personal:7", "--seconds", "0"]))
    assert cli.current_topic_metadata()["topic_id"] == "personal:7"
    assert all(token == "202:fake-personal" and p["chat_id"] == "-200"
               and p["message_thread_id"] == "7" for token, method, p in transport)
    journal = Path(common.state_path("topics", "personal:7", "outbox.jsonl")).read_text()
    assert "content_sha256" in journal and "outgoing" not in journal and "question" not in journal
    assert cli.load_cursor(7) == 0


@pytest.mark.parametrize("suffix,method", [(".txt", "sendDocument"), (".png", "sendPhoto")])
def test_files_use_group_token_chat_and_numeric_thread(cfg, seats, transport, tmp_path, suffix, method):
    path = tmp_path / ("result" + suffix)
    path.write_text("document payload")
    cli.send_files(cfg, "personal:7", [str(path)], caption="result")
    token, actual_method, params = transport[-1]
    assert (token, actual_method) == ("202:fake-personal", method)
    assert b'name="chat_id"\r\n\r\n-200' in params["multipart"]
    assert b'name="message_thread_id"\r\n\r\n7' in params["multipart"]
    assert Path(common.state_path("topics", "personal:7", "outbox.jsonl")).exists()


@pytest.mark.parametrize("command", ["send", "recv", "ask"])
def test_same_thread_in_other_group_is_foreign(cfg, seats, transport, monkeypatch, command):
    monkeypatch.setenv("TMUX_PANE", "%1")
    argv = [command] + (["body"] if command in ("send", "ask") else []) + ["--topic", "personal:7"]
    with pytest.raises(SystemExit) as exc:
        getattr(cli, "cmd_" + command)(cfg, cli.build_parser().parse_args(argv))
    assert exc.value.code == cli.OWNERSHIP_EXIT and transport == []


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_group_notify_wakes_target_and_echoes_in_sender_group(cfg, seats, transport, monkeypatch, reverse):
    sender_key, target_key, pane = (("personal:7", 7, "%2") if reverse else (7, "personal:7", "%1"))
    target_pane = "%1" if reverse else "%2"
    monkeypatch.setenv("TMUX_PANE", pane)
    typed = []
    monkeypatch.setattr(daemon, "type_line", lambda p, text, **kw: typed.append((p, text)) or "sent")
    result = cli.notify_topic(cfg, target_key, "ignored", "peer-event", "peer text")
    assert result["wake"] == "nudged" and result["telegram"] == "posted"
    assert typed[0][0] == target_pane and f"recv --topic {target_key}" in typed[0][1]
    records, _ = cli.read_new(target_key, 0)
    assert records[0]["sender_topic_id"] == sender_key and records[0]["kind"] == "peer"
    assert cli.read_new(sender_key, 0)[0] == []
    target = common.topic_credentials(cfg, target_key)
    sender = common.topic_credentials(cfg, sender_key)
    assert [(t, p["chat_id"]) for t, m, p in transport] == [(target[0], str(target[1])),
                                                           (sender[0], str(sender[1]))]
    assert cli.notify_topic(cfg, target_key, None, "peer-event", "peer text")["status"] == "duplicate"


@pytest.mark.parametrize("audio", [False, True])
def test_second_group_text_voice_inbox_and_wake(cfg, seats, transport, monkeypatch, audio):
    typed, downloads = [], []
    monkeypatch.setattr(daemon, "type_line", lambda p, text, **kw: typed.append((p, text)) or "sent")
    monkeypatch.setattr(daemon.threading, "Timer", lambda delay, target, args=(): SimpleNamespace(
        start=lambda: target(*args)))
    msg = message("" if audio else "inbound text", reply_to_message={"message_id": 7})
    if audio:
        msg["voice"] = {"file_id": "voice-file"}

        def download(token, file_id, path):
            downloads.append((token, path))
            Path(path).write_bytes(b"audio")
        monkeypatch.setattr(daemon, "download_file", download)
        monkeypatch.setattr(daemon, "openai_api_key", lambda: "fake")
        monkeypatch.setattr(daemon, "transcribe", lambda *a: "voice transcript")
    daemon.route_update(cfg, "202:fake-personal", {"update_id": 1, "message": msg})
    records, _ = cli.read_new("personal:7", 0)
    assert records[0]["text"] == ("voice transcript" if audio else "inbound text")
    assert records[0]["thread_id"] == "personal:7" and "reply_to_message_id" not in records[0]
    assert typed[0][0] == "%2" and "recv --topic personal:7" in typed[0][1]
    assert cli.read_new(7, 0)[0] == []
    if audio:
        assert downloads[0][0] == "202:fake-personal" and "personal:7" in downloads[0][1]


@pytest.mark.parametrize("attachment", ["photo", "document"])
def test_second_group_attachment_download_uses_group_credentials(cfg, seats, transport, monkeypatch, attachment):
    downloads = []

    def download(token, file_id, path):
        downloads.append((token, file_id, path))
        Path(path).write_bytes(b"file contents")
    monkeypatch.setattr(daemon, "download_file", download)
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    msg = message("caption")
    msg[attachment] = ([{"file_id": "photo-id"}] if attachment == "photo" else
                       {"file_id": "doc-id", "file_name": "result.txt", "mime_type": "text/plain"})
    daemon.handle_message(cfg, msg)
    assert downloads[0][0] == "202:fake-personal" and "/topics/personal:7/media/" in downloads[0][2]
    assert downloads[0][2] in cli.read_new("personal:7", 0)[0][0]["text"]
    assert cli.read_new(7, 0)[0] == []


@pytest.mark.parametrize("sender", [43, 101, None])
def test_second_group_non_owner_and_wrong_bot_service_events_are_denied(cfg, seats, transport, monkeypatch, sender):
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: pytest.fail("untrusted wake"))
    daemon.handle_message(cfg, message("secret", **{"from": {"id": sender}}))
    event = message("", forum_topic_closed={}, **{"from": {"id": sender, "is_bot": sender == 101}})
    daemon.handle_message(cfg, event)
    assert "closed" not in common.read_registry()["personal:7"] and transport == []
    assert cli.read_new("personal:7", 0)[0] == []


@pytest.mark.parametrize("sender", [42, 202])
def test_second_group_owner_and_own_bot_service_events_use_key(cfg, seats, transport, sender):
    daemon.handle_message(cfg, message("", forum_topic_closed={}, **{"from": {"id": sender}}))
    assert common.read_registry()["personal:7"]["closed"] is True
    assert "closed" not in common.read_registry()["7"]
    assert daemon.reopen_topic(cfg, "personal:7") is True
    assert transport[-1][:2] == ("202:fake-personal", "reopenForumTopic")
    assert transport[-1][2] == {"chat_id": "-200", "message_thread_id": "7"}


def prepare_resume(monkeypatch):
    inline_threads(monkeypatch)
    monkeypatch.setattr(daemon.time, "sleep", lambda *a: None)
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr(daemon, "_name_taken_by_other", lambda *a: False)
    monkeypatch.setattr(daemon, "current_boot_id", lambda: "test-boot")
    monkeypatch.setattr(daemon, "launch_pane", lambda *a, **kw: ("%77", None))
    monkeypatch.setattr(daemon, "last_model_and_effort_for_session", lambda *a: ("model", "max"))
    monkeypatch.setattr(daemon, "compaction_plan", lambda *a: None)
    monkeypatch.setattr(daemon, "answer_resume_picker", lambda *a, **kw: "answered")
    monkeypatch.setattr(daemon, "_compaction_settled", lambda *a: True)
    monkeypatch.setattr(daemon, "_safe_peek", lambda *a: "")
    monkeypatch.setattr(daemon, "has_live_recv", lambda *a: False)
    monkeypatch.setattr(daemon, "_pane_hosts_session", lambda *a: True)
    monkeypatch.setattr(daemon, "engine_of_pane", lambda *a: "claude")
    typed = []

    def type_line(pane, text, **kw):
        if "still_ok" in kw:
            assert kw["still_ok"]()
        typed.append((pane, text))
        return "sent"
    monkeypatch.setattr(daemon, "type_line", type_line)
    return typed


@pytest.mark.parametrize("via", ["typed", "button"])
@pytest.mark.parametrize("choice", ["compact", "full"])
def test_second_group_reopen_question_and_real_revive(cfg, seats, transport, monkeypatch, via, choice):
    common.update_registry(lambda reg: reg["personal:7"].update(ended=common.now_iso(), parked=True))
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda *a: True)
    typed = prepare_resume(monkeypatch)
    daemon.handle_message(cfg, message("", forum_topic_reopened={}))
    pending = daemon.pending_reopens["personal:7"]
    assert daemon._load_pending_reopens()["personal:7"]["message_id"] == pending["message_id"]
    assert "reply_markup" in transport[-1][2]
    if via == "typed":
        daemon.handle_message(cfg, message(choice))
    else:
        query = {"id": "tap", "from": {"id": 42}, "data": "reopen:" + choice,
                 "message": {"chat": {"id": -200}, "message_thread_id": 7,
                             "message_id": pending["message_id"]}}
        daemon.route_update(cfg, "202:fake-personal", {"update_id": 2, "callback_query": query})
    info = common.read_registry()["personal:7"]
    assert info["pane"] == "%77" and "ended" not in info and "parked" not in info
    assert daemon.pending_reopens == {} and cli.read_new("personal:7", 0)[0] == []
    assert typed and "personal:7" in typed[-1][1]
    assert all(t == "202:fake-personal" for t, _m, _p in transport)
    assert all(p.get("chat_id", "-200") == "-200" for _t, _m, p in transport)
    edits = [p for _t, m, p in transport if m == "editMessageText"]
    assert edits and "is back" in edits[-1]["text"]
    assert common.read_registry()["7"]["pane"] == "%1"


@pytest.mark.parametrize("bad", ["owner", "bot", "chat", "stale", "wrong_polling_bot"])
def test_second_group_callback_authorization_is_group_scoped(cfg, seats, transport, monkeypatch, bad):
    common.update_registry(lambda reg: reg["personal:7"].update(ended=common.now_iso()))
    assert daemon.offer_reopen_choice(cfg, "personal:7", common.read_registry()["personal:7"])
    pending = daemon.pending_reopens["personal:7"]
    query = {"id": "tap", "from": {"id": 42}, "data": "reopen:full",
             "message": {"chat": {"id": -200}, "message_thread_id": 7, "message_id": pending["message_id"]}}
    if bad == "owner":
        query["from"]["id"] = 43
    elif bad == "bot":
        query["from"]["is_bot"] = True
    elif bad == "chat":
        query["message"]["chat"]["id"] = -100
    elif bad == "stale":
        query["message"]["message_id"] += 1
    monkeypatch.setattr(daemon, "check_pending_reopen", lambda *a: pytest.fail("unauthorized choice"))
    token = cfg["bot_token"] if bad == "wrong_polling_bot" else "202:fake-personal"
    before = copy.deepcopy(daemon.pending_reopens)
    transport.clear()
    daemon.route_update(cfg, token, {"update_id": 1, "callback_query": query})
    assert daemon.pending_reopens == before
    assert all(t == "202:fake-personal" and m == "answerCallbackQuery" for t, m, p in transport)


def test_second_group_parks_then_owner_message_revives_same_session(cfg, seats, transport, monkeypatch):
    now = time.time()
    monkeypatch.setattr(daemon, "_session_last_activity", lambda info: now - 9 * 3600)
    monkeypatch.setattr(daemon, "_pane_occupant", lambda p: (4242, now - 10 * 3600))
    monkeypatch.setattr(daemon, "_pane_hosts_session", lambda *a: True)
    monkeypatch.setattr(daemon, "context_for", lambda *a: {"pct": 35})
    monkeypatch.setattr(daemon, "load_park_exempt", lambda: {"7"})
    tmux = []
    monkeypatch.setattr(daemon, "_tmux", lambda argv, **kw: tmux.append(argv) or SimpleNamespace(returncode=0))
    one_poll(monkeypatch, daemon.IDLE_PARK_POLL)
    with pytest.raises(KeyboardInterrupt):
        daemon.idle_park_loop(cfg)
    parked = common.read_registry()["personal:7"]
    assert parked["parked"] is True and parked["session_id"] == "personal-sid"
    assert tmux == [["tmux", "kill-pane", "-t", "%2"]]
    assert any("Parked" in p.get("text", "") for t, m, p in transport)
    assert all(t == "202:fake-personal" for t, m, p in transport)
    prepare_resume(monkeypatch)
    monkeypatch.setattr(daemon, "_reopen_needs_asking", lambda *a: False)
    monkeypatch.setattr(daemon, "schedule_nudge", lambda *a: None)
    daemon.handle_message(cfg, message("wake up"))
    assert "parked" not in common.read_registry()["personal:7"]
    assert common.read_registry()["personal:7"]["session_id"] == "personal-sid"
    assert cli.read_new("personal:7", 0)[0][0]["text"] == "wake up"


@pytest.mark.parametrize("carry_forward", [False, True])
def test_second_group_context_warning_autocf_and_compaction(cfg, seats, transport, monkeypatch, carry_forward):
    inline_threads(monkeypatch)
    monkeypatch.setattr(daemon, "CARRY_FORWARD", carry_forward)
    monkeypatch.setattr(daemon, "AUTOCF_PCT", 60)
    monkeypatch.setattr(daemon, "context_for", lambda pane, *a: {"pct": 95 if pane == "%2" else 5})
    monkeypatch.setattr(daemon, "_cf_wait_idle", lambda *a: "idle")
    monkeypatch.setattr(daemon, "_cf_wait_done", lambda *a: "done")
    monkeypatch.setattr(daemon, "_cf_compaction_plan", lambda *a: None)
    monkeypatch.setattr(daemon, "_cf_transcript_cursor", lambda *a: (None, None))
    monkeypatch.setattr(daemon, "_cf_capture_tail", lambda *a: "")
    monkeypatch.setattr(daemon, "_cf_clear_modal", lambda *a: None)
    monkeypatch.setattr(daemon, "_cf_await_started", lambda *a: ("compacting", None))
    monkeypatch.setattr(daemon, "_cf_await_compacted", lambda *a: ("done", None))
    paths = []

    def save_cf(_cfg, tid, name, cf_file, marker):
        Path(cf_file).write_text("carry-forward saved")
        paths.append(cf_file)
        return "owner/repo#1", "session"
    monkeypatch.setattr(daemon, "_cf_verify_or_create_issue", save_cf)
    typed = []
    monkeypatch.setattr(daemon, "type_line", lambda pane, text, **kw: typed.append((pane, text)) or "sent")
    one_poll(monkeypatch, daemon.CTX_POLL)
    with pytest.raises(KeyboardInterrupt):
        daemon.warning_loop(cfg)
    warned = json.loads(Path(common.state_path("warnings.json")).read_text())
    autocf = json.loads(Path(common.state_path("autocf.json")).read_text())
    assert warned["personal:7"] >= 90 and autocf["personal:7"] is True
    assert any(text == "/compact" for pane, text in typed)
    assert all(pane == "%2" for pane, text in typed)
    assert any("Compaction done" in p.get("text", "") for _t, _m, p in transport)
    assert all(t == "202:fake-personal" and p["chat_id"] == "-200" for t, m, p in transport)
    assert not daemon.carry_forward_active("personal:7")
    if carry_forward:
        assert paths and "/topics/personal:7/" in paths[0]


def test_second_group_commands_answer_in_arriving_group_and_general_stays_silent(cfg, seats, transport):
    daemon.handle_message(cfg, message("/help"))
    assert transport and all(t == "202:fake-personal" and p["chat_id"] == "-200" for t, m, p in transport)
    assert transport[0][2]["message_thread_id"] == "7"
    sent = len(transport)
    daemon.handle_message(cfg, message("hello", message_thread_id=0))
    hidden = message("", message_thread_id=0)
    hidden.pop("text", None)
    hidden["general_forum_topic_hidden"] = {}
    daemon.handle_message(cfg, hidden)
    assert len(transport) == sent  # #388: no reply in General, not even to service events
    assert not list(Path(common.state_path("topics")).glob("*/inbox.jsonl"))


def test_general_commands_still_answer(cfg, seats, transport):
    daemon.handle_message(cfg, message("/help", message_thread_id=0))
    assert transport and all(p["chat_id"] == "-200" for t, m, p in transport)
    assert "message_thread_id" not in transport[-1][2]


def test_second_group_lifecycle_closes_its_own_topic(cfg, seats, transport, monkeypatch):
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: pane == "%1")
    one_poll(monkeypatch, daemon.LIFECYCLE_POLL)
    with pytest.raises(KeyboardInterrupt):
        daemon.lifecycle_loop(cfg)
    assert common.read_registry()["personal:7"]["ended"]
    assert any(m == "closeForumTopic" for t, m, p in transport)
    assert all(t == "202:fake-personal" and p["chat_id"] == "-200" for t, m, p in transport)


def test_status_digest_and_watchdog_name_non_default_group(cfg, seats, transport, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="active", stderr=""))
    cli.cmd_status(cfg, SimpleNamespace())
    assert "personal:7" in capsys.readouterr().out
    monkeypatch.setattr(digest, "fleet_panes", lambda: [("%2", "seat", "title", "claude")])
    monkeypatch.setattr(digest, "read_ctx_raw", lambda *a: {"pct": 35})
    assert "personal:7" in digest.session_lines({}, {})[0]
    model_watchdog.send_topic(cfg, "personal:7", "model changed")
    assert transport[-1][0] == "202:fake-personal" and "personal:7" in transport[-1][2]["text"]
    watchdog.send_general(cfg, "daemon down")
    assert transport[-1][0] == cfg["bot_token"] and "personal" in transport[-1][2]["text"]


def test_second_group_model_watchdog_sweep_routes_changed_model(cfg, seats, transport, monkeypatch, tmp_path):
    monkeypatch.setattr(model_watchdog, "engine_of_pane", lambda pane: "claude")
    monkeypatch.setattr(model_watchdog, "pane_alive", lambda pane: True)
    monkeypatch.setattr(model_watchdog, "read_context", lambda pane: {
        "session_id": "personal-sid" if pane == "%2" else "work-sid"})
    monkeypatch.setattr(model_watchdog, "load_state", lambda: {
        "personal-sid": {"last_model": "old", "last_fallback_ts": None}})
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-07T01:00:00Z",
                                "message": {"model": "new"}}) + "\n")
    monkeypatch.setattr(model_watchdog, "transcript_path", lambda *a: str(path))
    model_watchdog.sweep(cfg)
    assert len(transport) == 1 and transport[0][0] == "202:fake-personal"
    assert transport[0][2]["chat_id"] == "-200" and "personal:7" in transport[0][2]["text"]


def test_latest_binding_compares_thread_ids_only_within_group():
    reg = {"99": {"pane": "%1", "created": "2026-10-01"},
           "personal:7": {"pane": "%1", "created": "2026-10-02"}}
    assert common.latest_topics_by_pane(reg) == {"%1": "personal:7"}
    reg["personal:8"] = {"pane": "%1", "created": "2026-09-01"}
    assert common.latest_topics_by_pane(reg) == {"%1": "personal:8"}
    assert ":" not in daemon._revive_tmux_name({}, "personal:7", set())


def test_manual_restore_accepts_group_key_for_codex(cfg, monkeypatch):
    monkeypatch.setattr(restore_cli, "load_config", lambda: cfg)
    specs = []
    monkeypatch.setattr(daemon, "revive_topics", lambda _cfg, rows: specs.extend(rows) or {"personal:7": "resumed"})
    monkeypatch.setattr(restore_cli.sys, "argv", ["restore", "--codex", "personal:7:session-uuid"])
    assert restore_cli.main() == 0 and specs[0]["tid"] == "personal:7"


def test_registry_key_of_a_removed_group_never_stops_a_daemon_sweep(tmp_path, monkeypatch, capsys):
    """#377 review F1: a `personal:7` entry left in the registry after its group is removed from
    config raised SystemExit from topic_credentials, which `except Exception` loops do not catch —
    the sweep thread died for every group. Now that one reply is dropped and logged."""
    single = {"bot_token": "101:fake-work", "chat_id": -100, "owner_id": 42}
    sent = []
    monkeypatch.setattr(daemon, "send_message", lambda *a, **k: sent.append(a) or [{"result": {}}])
    monkeypatch.setattr(daemon, "log", lambda line: sent.append(("log", line)))
    assert daemon.reply(single, "personal:7", "context warning") is False
    assert not [s for s in sent if s[0] != "log"], "nothing may be sent for an unknown group"
    assert any("unknown Telegram group" in s[1] for s in sent if s[0] == "log")
    assert daemon.reply(single, 7, "still delivered") is True
    with pytest.raises(common.TopicKeyError):
        common.topic_credentials(single, "personal:7")
    assert not issubclass(common.TopicKeyError, SystemExit)


def test_cli_turns_a_bad_topic_key_into_an_exit(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: {"bot_token": "101:fake-work", "chat_id": -100,
                                                    "owner_id": 42})
    monkeypatch.setattr(cli.sys, "argv", ["tg-bridge", "send", "x", "--topic", "personal:7"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert "unknown Telegram group" in str(exc.value.code)


def test_a_malformed_registry_key_does_not_stop_the_pane_prepass():
    """#377 review r2: latest_topics_by_pane ran before the per-topic loops of the context and
    model sweeps, so one malformed key aborted the whole poll for every topic."""
    reg = {"bad key!": {"pane": "%1", "created": "2026-10-01"},
           "7": {"pane": "%1", "created": "2026-10-02"},
           "personal:3": {"pane": "%2", "created": "2026-10-02"}}
    assert common.latest_topics_by_pane(reg) == {"%1": "7", "%2": "personal:3"}
    assert sorted(reg, key=common.topic_sort_key)[-1] == "bad key!"


def test_a_malformed_pending_key_does_not_strand_the_other_questions(monkeypatch):
    single = {"bot_token": "101:fake-work", "chat_id": -100, "owner_id": 42}
    entry = {"name": "x", "engine": "claude", "session_id": "s", "pane": "%1", "ended": "t"}
    monkeypatch.setattr(daemon, "pending_reopens", {
        "bad key!": {"entry": entry, "tokens": 1, "state": "prepared"},
        "7": {"entry": entry, "tokens": 1, "state": "prepared"}})
    monkeypatch.setattr(daemon, "read_registry", lambda: {"bad key!": entry, "7": entry})
    monkeypatch.setattr(daemon, "should_auto_revive", lambda e: True)
    asked = []
    monkeypatch.setattr(daemon, "_send_reopen_question", lambda cfg, key, pending: asked.append(key) or True)
    monkeypatch.setattr(daemon, "_save_pending_reopens", lambda: True)
    monkeypatch.setattr(daemon, "log", lambda line: None)
    daemon.resend_undelivered_reopen_questions(single)
    assert asked == [7]
    assert "bad key!" not in daemon.pending_reopens and daemon.pending_reopens["7"]["state"] == "delivered"


def test_spawn_check_with_an_unconfigured_group_logs_instead_of_raising(monkeypatch):
    single = {"bot_token": "101:fake-work", "chat_id": -100, "owner_id": 42}
    monkeypatch.setattr(daemon, "_tmux", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    lines, calls = [], []
    monkeypatch.setattr(daemon, "log", lines.append)
    monkeypatch.setattr(daemon, "api", lambda *a, **k: calls.append(a))
    daemon.verify_spawn(single, "seat", "claude", "personal:7")
    assert calls == [] and any("personal:7" in line for line in lines)


@pytest.mark.parametrize("argv", [
    ["tg-bridge", "notify", "--topic", "bad key!", "--idempotency-key", "k"],
    ["tg-bridge", "send", "x", "--topic", "personal:7"],
])
def test_every_cli_path_reports_a_bad_topic_key_as_an_exit(monkeypatch, argv):
    monkeypatch.setattr(cli, "load_config", lambda: {"bot_token": "101:fake-work", "chat_id": -100,
                                                    "owner_id": 42})
    monkeypatch.setattr(cli.sys, "argv", argv)
    monkeypatch.setattr(cli.sys, "stdin", __import__("io").StringIO("body"))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert isinstance(exc.value.code, str) and exc.value.code
