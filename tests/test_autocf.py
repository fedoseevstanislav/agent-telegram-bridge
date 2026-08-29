"""Unit tests for the auto carry-forward decision logic (#92): _autocf_decide — the
pure state machine that decides when the daemon auto-fires a carry-forward at a context
threshold. The wiring into warning_loop (which drives the real pane) isn't unit-tested."""

from bridge import daemon

T, R = 60, 50   # threshold / rearm used explicitly so tests don't depend on env config


def dec(pct, armed, active, engine="claude"):
    return daemon._autocf_decide(pct, engine, armed, active, threshold=T, rearm=R)


def test_disabled_threshold_never_fires():
    assert daemon._autocf_decide(95, "claude", False, False, threshold=0) == (False, False)
    assert daemon._autocf_decide(95, "claude", True, False, threshold=0) == (False, True)


def test_non_claude_engine_never_fires():
    # carry-forward is Claude-only (Codex has no /compact-driven flow)
    assert dec(90, False, False, engine="codex") == (False, False)
    assert dec(90, True, False, engine="codex") == (False, True)


def test_unknown_engine_never_fires():
    # engine_of_pane returns None for a freshly-registered / not-yet-in-fleet pane;
    # None must NEVER fire (guards the #93-review bug where a fresh Codex pane fired)
    assert dec(90, False, False, engine=None) == (False, False)
    assert dec(75, False, False, engine=None) == (False, False)


def test_rearm_is_engine_independent():
    # a context drop re-arms regardless of engine detection (rearm runs before the
    # engine gate), so a transient unknown-engine can't strand the armed flag
    assert dec(10, True, False, engine=None) == (False, False)
    assert dec(10, True, False, engine="codex") == (False, False)


def test_below_threshold_does_not_fire():
    assert dec(40, False, False) == (False, False)   # below rearm
    assert dec(55, False, False) == (False, False)   # between rearm and threshold


def test_crossing_threshold_fires_once():
    assert dec(60, False, False) == (True, True)     # crosses -> fire + arm
    assert dec(62, True, False) == (False, True)     # already armed -> no repeat
    assert dec(90, True, False) == (False, True)     # stays armed, still no repeat


def test_active_carryforward_blocks_fire():
    # a carry-forward already running: don't fire, stay unarmed so we retry next cycle
    assert dec(70, False, True) == (False, False)


def test_rearm_after_compact():
    assert dec(10, True, False) == (False, False)    # dropped below rearm -> re-arm
    assert dec(60, False, False) == (True, True)     # then climbing back fires again


def test_hysteresis_no_flap_at_boundary():
    # hovering at 60/59/60 while armed must NOT re-fire and must stay armed
    assert dec(60, True, False) == (False, True)
    assert dec(59, True, False) == (False, True)     # 59 > rearm(50) -> stays armed
    assert dec(60, True, False) == (False, True)
    assert dec(49, True, False) == (False, False)    # only a real drop below rearm re-arms


def test_full_lifecycle_sequence():
    armed, fires = False, []
    # climb, fire at 60, keep climbing (no refire), compact-drop, climb again (refire once)
    for pct in [30, 45, 55, 60, 63, 70, 8, 40, 58, 61]:
        fire, armed = dec(pct, armed, False)
        if fire:
            fires.append(pct)
    assert fires == [60, 61]


def test_module_defaults_are_60_50():
    # sanity: default threshold/rearm come from the module constants
    assert daemon.AUTOCF_PCT == 60
    assert daemon.AUTOCF_REARM_PCT == 50
    assert daemon._autocf_decide(60, "claude", False, False) == (True, True)
