from bridge import daemon


def test_missing_boot_id_with_dead_registered_panes_restores_before_saving(monkeypatch):
    events = []
    registry = {
        "13": {
            "name": "queue-refactor",
            "pane": "%2",
            "engine": "claude",
            "session_id": "sid-13",
        }
    }

    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-new")
    monkeypatch.setattr(daemon, "load_stored_boot_id", lambda: None)
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: False)
    monkeypatch.setattr(daemon, "api", lambda *args, **kwargs: events.append(("api", args, kwargs)))

    # `cause` is keyword-only with no default: a defaulted fake cannot tell "production
    # passed it" from "production omitted it".
    def fake_revive(_cfg, tid, info, brief=True, taken=None, *, cause, **kw):
        events.append(("revive", tid, info["session_id"], brief, cause))
        return "resumed", {"needs_brief": False}

    monkeypatch.setattr(daemon, "revive_one", fake_revive)
    monkeypatch.setattr(daemon, "save_boot_id", lambda boot: events.append(("save", boot)))

    daemon.restore_on_boot({"bot_token": "token", "chat_id": -100})

    # This branch is reached on "no stored boot_id AND every registered pane is dead", which
    # is a reboot OR a killed tmux server — _all_target_panes_dead's own docstring says so.
    # It must NOT tell the sessions a reboot happened (#167).
    expected = ("revive", "13", "sid-13", False, "recovery")
    assert expected in events
    assert events.index(expected) < events.index(("save", "boot-new"))

    # The summary the owner reads must not claim a reboot either. `api` is called positionally:
    # api(token, "sendMessage", {"chat_id": ..., "text": ...}).
    posted = [a[2]["text"] for _ev, a, _kw in (e for e in events if e[0] == "api")
              if len(a) > 2 and isinstance(a[2], dict) and "text" in a[2]]
    assert posted, "restore must post a summary"
    assert "Recovery restore" in posted[0]
    assert "Reboot restore" not in posted[0]


def test_missing_boot_id_with_live_registered_panes_only_baselines(monkeypatch):
    events = []
    registry = {
        "13": {
            "name": "queue-refactor",
            "pane": "%2",
            "engine": "claude",
            "session_id": "sid-13",
        }
    }

    monkeypatch.setattr(daemon, "current_boot_id", lambda: "boot-new")
    monkeypatch.setattr(daemon, "load_stored_boot_id", lambda: None)
    monkeypatch.setattr(daemon, "read_registry", lambda: registry)
    monkeypatch.setattr(daemon, "pane_alive", lambda pane: True)
    monkeypatch.setattr(daemon, "revive_one", lambda *args, **kwargs: events.append(("revive", args, kwargs)))
    monkeypatch.setattr(daemon, "save_boot_id", lambda boot: events.append(("save", boot)))

    daemon.restore_on_boot({"bot_token": "token", "chat_id": -100})

    assert events == [("save", "boot-new")]
