"""Tests for the t2p command-line help experience."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
T2P_SCRIPT = REPO_ROOT / "scripts" / "t2p.py"
SCRIPTS = REPO_ROOT / "scripts"


def test_no_arguments_print_help_with_usage_error_exit() -> None:
    """Bare invocation prints human help to stderr and preserves exit 2."""
    result = subprocess.run(
        [sys.executable, "-B", str(T2P_SCRIPT)],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"usage: python -B scripts/t2p.py" in result.stderr
    assert b"python -B scripts/t2p.py desktop --help" in result.stderr
    assert b"python -B scripts/t2p.py check C:\\private\\package" in result.stderr
    assert b'"schema_version"' not in result.stderr
    _assert_description_wraps(result.stderr)


@pytest.mark.parametrize("installed", [False, True])
def test_main_no_arguments_skips_runtime_and_binding(installed: bool) -> None:
    """Both entry modes print help without invoking runtime or install checks."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(SCRIPTS), env.get("PYTHONPATH", "")) if part)
    probe = (
        "import t2p\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise AssertionError('runtime invoked')\n"
        "t2p._delegate = forbidden\n"
        "t2p._install_binding = forbidden\n"
        f"raise SystemExit(t2p.main([], installed={installed!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        timeout=30,
        check=False,
        env=env,
    )

    expected_prog = "t2p" if installed else "python -B scripts/t2p.py"
    assert result.returncode == 2
    assert result.stdout == b""
    assert f"usage: {expected_prog}".encode() in result.stderr


def test_json_without_command_keeps_syntax_error_envelope() -> None:
    """The explicit JSON syntax-error path remains compatible."""
    result = subprocess.run(
        [sys.executable, "-B", str(T2P_SCRIPT), "--json"],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == b""
    envelope = json.loads(result.stdout)
    assert envelope["limitations"] == ["invalid_arguments_use_help"]
    assert envelope["exit_code"] == 2


@pytest.mark.parametrize(
    ("command", "example"),
    [
        ("refresh", "refresh C:\\path\\to\\model.pbip --canaries Orders Customers"),
        ("check", "check C:\\private\\package"),
        ("desktop", "desktop observe --pid 1234"),
        ("desktop", "desktop observe C:\\path\\to\\model.pbip"),
        ("desktop", "desktop collect C:\\path\\to\\model.pbip --out C:\\private\\dialog-details.txt"),
    ],
)
def test_actionable_subcommand_help_includes_examples(command: str, example: str) -> None:
    """Local operation help shows copyable command forms."""
    result = subprocess.run(
        [sys.executable, "-B", str(T2P_SCRIPT), command, "--help"],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert b"Examples:" in result.stdout
    assert example.encode() in result.stdout
    _assert_description_wraps(result.stdout)


def _assert_description_wraps(output: bytes) -> None:
    """Command descriptions wrap to the normal help width before positional arguments."""
    normalized = output.replace(b"\r\n", b"\n")
    help_sections = normalized.split(b"\n\n", maxsplit=1)
    description = help_sections[1].split(b"\npositional arguments:", maxsplit=1)[0]
    assert all(len(line) <= 80 for line in description.splitlines()), description.decode()
