"""Unit tests for the topic-ownership guard (#138, design §4.1 M2).

`send`/`ask`/`recv --topic <someone else's topic>` used to succeed silently: the message
went to Telegram wearing the TARGET's icon (so the owner read it as that session speaking) while
the agent behind it — which reads its inbox, not Telegram — never saw anything, and `ask`
and `recv` additionally ate that agent's unread backlog and read cursor.

The guard refuses with exit 4 and names `notify`, which is the path that actually reaches
an agent. It is a tripwire against a known mistake, not a security boundary: a caller whose
pane can't be resolved is deliberately left alone.
"""

import json
import subprocess
import types

import pytest

from bridge import cli


def _tmux_hangs(_pane):
    raise subprocess.TimeoutExpired(cmd="tmux", timeout=10)

CALLER_PANE = "%A"
A, B = 111, 222


def _registry(**topics):
    registry = {
        "111": {"name": "Alpha", "created": "2026-08-08T10:00:00+0000",
                "pane": CALLER_PANE, "icon": "🦊"},
        "222": {"name": "Beta", "created": "2026-08-08T10:05:00+0000",
                "pane": "%B", "icon": "🐙"},
    }
    registry.update(topics)
    return registry


def _bind(monkeypatch, tmp_path, registry=None, pane=CALLER_PANE, pane_alive=None):
    """Run as the live session that owns topic A."""
    if pane is None:
        monkeypatch.delenv("TMUX_PANE", raising=False)
    else:
        monkeypatch.setenv("TMUX_PANE", pane)
    registry = _registry() if registry is None else registry
    monkeypatch.setattr(cli, "read_registry", lambda: registry)
    monkeypatch.setattr(cli, "state_path", lambda *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(cli, "pane_alive", pane_alive or (lambda _pane: True), raising=False)
    sent = []
    monkeypatch.setattr(cli, "send_text", lambda cfg, topic, text: sent.append((topic, text)))
    monkeypatch.setattr(cli, "send_typing", lambda cfg, topic: None)
    return sent


def _inbox(tmp_path, topic_id, texts, cursor=0):
    topic_dir = tmp_path / "topics" / str(topic_id)
    topic_dir.mkdir(parents=True)
    topic_dir.joinpath("inbox.jsonl").write_text(
        "".join(json.dumps({"ts": "2026-08-08T11:00:00+0000", "from": "the owner",
                            "kind": "text", "text": text}, ensure_ascii=False) + "\n"
                for text in texts), encoding="utf-8")
    topic_dir.joinpath("cursor").write_text(str(cursor), encoding="utf-8")
    return topic_dir


def _send_args(topic, text="hello", force=False):
    return types.SimpleNamespace(text=text, topic=str(topic), force=force, json=False)


def _recv_args(topic, peek=False, wait=None):
    return types.SimpleNamespace(topic=str(topic), wait=wait, peek=peek, json=False)


def _ask_args(topic, text="question", timeout=1):
    return types.SimpleNamespace(text=text, topic=str(topic), timeout=timeout, json=False)


# --- T1: send ---------------------------------------------------------------------

def test_send_to_another_sessions_topic_is_refused_and_names_notify(
    tmp_path, monkeypatch, capsys
):
    sent = _bind(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as refused:
        cli.cmd_send({}, _send_args(B))

    assert refused.value.code == cli.OWNERSHIP_EXIT
    assert sent == []                                    # nothing reached Telegram
    out = capsys.readouterr().out
    assert "another live session's topic" in out
    # The refusal has to carry the working alternative, or the agent just retries.
    assert f"tg-bridge notify --topic {B}" in out


def test_ownership_refusal_fires_before_the_unread_guard(tmp_path, monkeypatch, capsys):
    """Ordering is the whole point: the #135 unread refusal coaches `recv --topic <target>`.

    If the unread guard ran first, a cross-poster would be shown the target's private
    messages and then told to run the exact command that steals the target's cursor.
    """
    topic_dir = _inbox(tmp_path, B, ["private message for Beta"])
    sent = _bind(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as refused:
        cli.cmd_send({}, _send_args(B))

    assert refused.value.code == cli.OWNERSHIP_EXIT      # not UNREAD_EXIT
    out = capsys.readouterr().out
    assert "private message for Beta" not in out         # no preview of B's records
    assert f"recv --topic {B}" not in out                # no coaching into cursor theft
    assert topic_dir.joinpath("cursor").read_text() == "0"
    assert sent == []


def test_refusal_states_only_what_the_command_would_actually_have_done(
    tmp_path, monkeypatch, capsys
):
    """A teaching moment aimed at an agent that just erred must not overstate the damage:
    `send` posts without consuming, `recv` consumes without posting, only `ask` does both."""
    _bind(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        cli.cmd_send({}, _send_args(B))
    send_out = capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.cmd_recv({}, _recv_args(B))
    recv_out = capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.cmd_ask({}, _ask_args(B))
    ask_out = capsys.readouterr().out

    assert "post" in send_out and "consume" not in send_out and "drain" not in send_out
    assert "consume" in recv_out and "post" not in recv_out
    assert "post" in ask_out and "drain" in ask_out


@pytest.mark.parametrize(("pane_alive", "why"), [
    (lambda _pane: False,
     "pane died before the lifecycle sweep stamped its topic ended"),
    (_tmux_hangs,
     "tmux hung: the probe degrades to unknown caller instead of taking the CLI down"),
])
def test_a_caller_whose_pane_does_not_answer_is_unknown_not_blocked(
    tmp_path, monkeypatch, pane_alive, why
):
    """`TMUX_PANE` being set does not prove the process is still in that pane — a detached
    script keeps the value it inherited, and a dead pane stays 'live' in the registry until
    the daemon's next lifecycle poll. Such a caller must not be treated as that session; and
    since the guard is only a tripwire, failing to identify it must not block its work."""
    sent = _bind(monkeypatch, tmp_path, pane_alive=pane_alive)

    cli.cmd_send({}, _send_args(B, text=why))

    assert sent == [(B, why)]


def test_send_to_own_topic_still_reaches_telegram(tmp_path, monkeypatch):
    sent = _bind(monkeypatch, tmp_path)

    cli.cmd_send({}, _send_args(A, text="progress update"))

    assert sent == [(A, "progress update")]


# --- T2: ask and recv -------------------------------------------------------------

def test_ask_at_another_sessions_topic_refuses_before_any_drain(
    tmp_path, monkeypatch, capsys
):
    """`ask --topic B` was the worst hole: it drained B's backlog before sending anything."""
    topic_dir = _inbox(tmp_path, B, ["first for Beta", "second for Beta"])
    sent = _bind(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as refused:
        cli.cmd_ask({}, _ask_args(B))

    assert refused.value.code == cli.OWNERSHIP_EXIT
    assert sent == []
    out = capsys.readouterr().out
    assert "first for Beta" not in out and "second for Beta" not in out
    # B's cursor and pending records survive untouched — that agent must still get them.
    assert topic_dir.joinpath("cursor").read_text() == "0"
    assert len(cli.unread_before_send(B)) == 2


def test_recv_at_another_sessions_topic_refuses_on_the_committing_path(
    tmp_path, monkeypatch, capsys
):
    topic_dir = _inbox(tmp_path, B, ["for Beta only"])
    _bind(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as refused:
        cli.cmd_recv({}, _recv_args(B))

    assert refused.value.code == cli.OWNERSHIP_EXIT
    assert "for Beta only" not in capsys.readouterr().out
    assert topic_dir.joinpath("cursor").read_text() == "0"


def test_peek_at_another_sessions_topic_is_allowed(tmp_path, monkeypatch, capsys):
    """`--peek` moves no cursor, so it stays the sanctioned way to look at another topic."""
    topic_dir = _inbox(tmp_path, B, ["for Beta only"])
    _bind(monkeypatch, tmp_path)

    cli.cmd_recv({}, _recv_args(B, peek=True))

    assert "for Beta only" in capsys.readouterr().out
    assert topic_dir.joinpath("cursor").read_text() == "0"


def test_recv_at_an_ended_topic_is_allowed(tmp_path, monkeypatch, capsys):
    """Ended topics keep no agent waiting; their inboxes stay readable for forensics."""
    topic_dir = _inbox(tmp_path, B, ["what Beta never read"])
    registry = _registry(**{"222": {"name": "Beta", "created": "2026-08-08T10:05:00+0000",
                                   "pane": "%B", "icon": "🐙",
                                   "ended": "2026-08-08T12:00:00+0000"}})
    _bind(monkeypatch, tmp_path, registry=registry)

    cli.cmd_recv({}, _recv_args(B))

    assert "what Beta never read" in capsys.readouterr().out
    assert topic_dir.joinpath("cursor").read_text() == "1"


# --- T3: exemptions ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("pane", "registry", "target", "why"),
    [
        (None, None, B, "outside tmux: automation, cron and headless callers keep working"),
        ("%unregistered", None, B, "an unregistered pane cannot be attributed to a topic"),
        (CALLER_PANE,
         {"111": {"name": "Alpha", "pane": CALLER_PANE},
          "333": {"name": "Alpha again", "pane": CALLER_PANE},
          "222": {"name": "Beta", "pane": "%B"}},
         B, "ambiguous binding: no single owner to compare against"),
        (CALLER_PANE,
         {"111": {"name": "Alpha", "pane": CALLER_PANE},
          "222": {"name": "Events", "feed": True, "icon": "📡"}},
         B, "feed topics are outbound-only, nobody is waiting on them"),
        (CALLER_PANE, None, A, "the caller's own topic"),
    ],
)
def test_guard_exempts_unresolvable_callers_and_non_dialog_targets(
    tmp_path, monkeypatch, pane, registry, target, why
):
    sent = _bind(monkeypatch, tmp_path, registry=registry, pane=pane)

    cli.cmd_send({}, _send_args(target, text=why))

    assert sent == [(target, why)]
