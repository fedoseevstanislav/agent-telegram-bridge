r"""The codex spawn bootstrap must tell codex how to operate on the bridge (#107):
send multi-line messages via a file on stdin so newlines are real (never a literal \n in a
quoted argument), and read only with a foreground recv (never a background recv --wait,
which silently drains messages before the agent acts)."""

from bridge import daemon

BOOTSTRAPS = (daemon.CODEX_BOOTSTRAP, daemon.CODEX_BOOTSTRAP_NO_TASK)


def test_bootstrap_gives_a_valid_copyable_send_command():
    # the exact command must appear verbatim and be shell-valid (single-line stdin
    # redirect from a file) — NOT a multi-line heredoc whose delimiter could be indented.
    for b in BOOTSTRAPS:
        assert "tg-bridge send --topic N - < /tmp/reply.txt" in b
        assert "<<" not in b          # no heredoc (its column-0 EOF is too fragile to embed)


def test_bootstrap_warns_against_literal_backslash_n():
    # the warning must literally show a backslash-n (two chars), not a real newline —
    # this is the whole point: the shell doesn't interpret \n in a quoted arg.
    for b in BOOTSTRAPS:
        assert "\\n" in b
        assert "literal \\n" in b


def test_bootstrap_forbids_background_recv():
    for b in BOOTSTRAPS:
        assert "recv --wait" in b and "background" in b
    assert "NEVER run recv --wait in the background" in daemon.CODEX_BOOTSTRAP
    assert "NEVER run recv --wait in the" in daemon.CODEX_BOOTSTRAP_NO_TASK


def test_bootstrap_still_nudge_driven_foreground_recv():
    # the existing nudge-driven contract must remain: read on the [tg-bridge] nudge
    for b in BOOTSTRAPS:
        assert "[tg-bridge]" in b
        assert "recv --topic N" in b
