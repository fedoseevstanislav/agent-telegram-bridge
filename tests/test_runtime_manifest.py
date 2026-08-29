import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_manifest_is_current_and_stdlib_only():
    result = subprocess.run(
        [sys.executable, "scripts/generate_runtime_manifest.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    manifest = json.loads((ROOT / "security/runtime-manifest.json").read_text())
    assert manifest["python"]["version"] == "3.12.*"
    assert manifest["python"]["packages"] == []
    assert manifest["files"]
    assert all(value.startswith("sha256:") for value in manifest["files"].values())


def test_required_host_capabilities_are_explicit():
    manifest = json.loads((ROOT / "security/runtime-manifest.json").read_text())
    required = {
        item["name"] for item in manifest["externalCommands"] if item["required"]
    }
    assert required == {"bash", "gh", "pgrep", "python3", "systemctl", "tmux"}
