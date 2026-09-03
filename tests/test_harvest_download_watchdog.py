"""Tests for the download watchdog and the sweep tally, both from issue #472.

The defect this file exists for: a customer's 47-asset harvest lost `IA Redemptions by Campaign
Report` to `timeout after 600s`, twice. There are TWO nested timeouts catching OPPOSITE failures —
the engine's per-socket-read `timeout=300` (`fetch_tds.py:407,423`) and our total wall clock — so a
download that is slow but perfectly healthy satisfies every socket read and is then killed by ours.

These tests drive `run_watched` with REAL child processes and a probe the test controls, because the
thing under test is a decision about time and liveness, and a mocked subprocess would not exercise
the kill path, the pipe drain, or the "never observed" fallback at all.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "harvest_estate_assets", REPO_ROOT / "scripts" / "harvest_estate_assets.py"
)
assert SPEC and SPEC.loader
harvest = importlib.util.module_from_spec(SPEC)
sys.modules["harvest_estate_assets"] = harvest
SPEC.loader.exec_module(harvest)


SLEEPER = "import time,sys;sys.stdout.write('started');sys.stdout.flush();time.sleep({seconds})"


def sleeper(seconds: float) -> list[str]:
    """A child that just sits there: the shape of a download that has stopped transferring."""
    return [sys.executable, "-c", SLEEPER.format(seconds=seconds)]


class Counter:
    """A probe the test drives.

    `moves` bytes-worth of movement happens on the first `moves` samples and then it FREEZES, which
    is the observe-then-flatline shape of a real hung transfer. `moves=0` never moves at all, which
    is the uv-trampoline shape. Movement has to happen DURING the run: a value bumped before the
    first sample is indistinguishable from a constant, because progress is a change between samples.
    """

    def __init__(self, value: int | None = 0, moves: int = 0) -> None:
        self.value = value
        self.moves = moves
        self.calls = 0

    def __call__(self, pid: int) -> int | None:  # pylint: disable=unused-argument
        self.calls += 1
        if self.moves > 0 and self.calls > 1 and self.value is not None:
            self.moves -= 1
            self.value += 1024
        return self.value

    def advance(self, by: int = 1024) -> None:
        assert self.value is not None
        self.value += by


# --- the fix: a download that is MOVING is not killed, one that STOPPED is -----------------------


def test_a_progressing_download_is_not_killed_by_the_wall_clock_ceiling() -> None:
    """The customer's defect, directly: healthy transfer, ceiling far below its duration."""
    probe = Counter(1000)
    stop = threading.Event()

    def keep_moving() -> None:
        while not stop.is_set():
            probe.advance()
            time.sleep(0.05)

    mover = threading.Thread(target=keep_moving, daemon=True)
    mover.start()
    try:
        run = harvest.run_watched(
            sleeper(1.5),
            env=None,
            timeout=0.3,  # the pre-#472 behaviour would have killed this at 0.3s
            stall_timeout=5.0,
            probe=probe,
            poll_interval=0.05,
            heartbeat=1000.0,
        )
    finally:
        stop.set()
        mover.join(2)

    assert run.verdict == "", f"killed a progressing download: {run.detail}"
    assert run.returncode == 0
    assert run.elapsed > 0.3, "the run did not even reach the ceiling it was supposed to survive"
    assert run.progress_observed is True


def test_a_stalled_download_is_killed_once_progress_stops() -> None:
    """Movement observed, then a flatline: that is a hung transfer and killing it is right."""
    probe = Counter(1000, moves=2)  # moves during the run, then flatlines
    run = harvest.run_watched(
        sleeper(30),
        env=None,
        timeout=0,  # no ceiling at all: only the stall detector can end this
        stall_timeout=0.3,
        probe=probe,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict == "stalled", f"a flatlined download survived: {run!r}"
    assert run.returncode is None
    assert run.elapsed < 20, "the stall detector waited for the child instead of killing it"


def test_progress_must_be_OBSERVED_before_a_flatline_counts_as_a_stall() -> None:
    """A probe that never moves is a probe we cannot trust — measured, not hypothetical.

    Under a uv venv, `Popen.pid` is a TRAMPOLINE whose I/O counters never move (measured:
    `Popen.pid = 35152` while the child's own `os.getpid()` was `20856`). If a never-moving probe
    were allowed to arm the stall deadline, every download on such a venv would be killed as hung.
    """
    probe = Counter(1000)  # never advanced
    run = harvest.run_watched(
        sleeper(30),
        env=None,
        timeout=0.4,
        stall_timeout=0.05,  # far tighter than the ceiling: it must NOT be the one that fires
        probe=probe,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict == "ceiling", f"an unmoved probe was trusted as a stall signal: {run!r}"
    assert run.progress_observed is False
    assert run.elapsed >= 0.4


def test_an_unavailable_probe_falls_back_to_the_ceiling_rather_than_killing() -> None:
    """`None` means "cannot tell", which is a different answer from "no bytes"."""
    run = harvest.run_watched(
        sleeper(30),
        env=None,
        timeout=0.4,
        stall_timeout=0.05,
        probe=lambda pid: None,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict == "ceiling"
    assert run.progress_observed is False


def test_a_short_healthy_run_returns_its_output_and_no_verdict() -> None:
    run = harvest.run_watched(
        [sys.executable, "-c", "print('landed')"],
        env=None,
        timeout=30,
        stall_timeout=30,
        probe=Counter(0),
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert (run.verdict, run.returncode, run.stdout.strip()) == ("", 0, "landed")


def test_the_child_output_is_drained_while_the_watchdog_polls() -> None:
    """A chatty child must not deadlock on a full pipe buffer — that would LOOK like a stall.

    64 KiB is the Windows pipe buffer; this writes well past it before exiting.
    """
    chatty = [sys.executable, "-c", "import sys;sys.stdout.write('x'*400000);sys.stdout.flush()"]
    run = harvest.run_watched(
        chatty, env=None, timeout=30, stall_timeout=30, probe=Counter(0), poll_interval=0.05, heartbeat=1000.0
    )
    assert run.verdict == ""
    assert len(run.stdout) == 400000, "the child's output was truncated or the drain deadlocked"


# --- the failure TEXT: an operator must be able to act on it ------------------------------------


def test_the_stall_message_says_what_was_observed_and_which_flag_to_turn() -> None:
    probe = Counter(1000, moves=2)  # moves during the run, then flatlines
    run = harvest.run_watched(
        sleeper(30), env=None, timeout=0, stall_timeout=0.3, probe=probe, poll_interval=0.05, heartbeat=1000.0
    )
    assert "stalled" in run.detail
    assert "--download-stall-timeout" in run.detail, f"the message names no flag: {run.detail}"
    assert "elapsed" in run.detail, f"the message reports no elapsed time: {run.detail}"


def test_the_ceiling_message_distinguishes_itself_from_a_stall() -> None:
    """`timeout after 600s` told the customer only what WE had configured."""
    run = harvest.run_watched(
        sleeper(30),
        env=None,
        timeout=0.4,
        stall_timeout=0.05,
        probe=lambda pid: None,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert "--download-timeout" in run.detail
    assert "elapsed" in run.detail
    assert "no download-progress signal" in run.detail, f"it does not say WHY it could not tell: {run.detail}"
    assert "stalled" not in run.detail, "a ceiling kill must not be reported as a stall"


def test_a_long_run_reports_elapsed_time_rather_than_looking_like_work(caplog: pytest.LogCaptureFixture) -> None:
    """AGENTS.md: never block silently; anything past ~60s reports elapsed time."""
    probe = Counter(1000, moves=2)  # moves during the run, then flatlines
    with caplog.at_level("INFO", logger="harvest_estate_assets"):
        harvest.run_watched(
            sleeper(30),
            env=None,
            timeout=0,
            stall_timeout=0.5,
            probe=probe,
            poll_interval=0.05,
            heartbeat=0.1,  # stands in for the 60s default
            label="[7/47] IA Redemptions by Campaign Report",
        )
    beats = [r.getMessage() for r in caplog.records if "still running" in r.getMessage()]
    assert beats, "a long-running download said nothing at all"
    assert "elapsed=" in beats[0]
    assert "IA Redemptions by Campaign Report" in beats[0], "the heartbeat does not say which asset"


def test_a_download_progressing_past_the_ceiling_is_announced_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """Not killing it is right, but silently ignoring our own configured ceiling is not."""
    probe = Counter(1000)
    stop = threading.Event()

    def keep_moving() -> None:
        while not stop.is_set():
            probe.advance()
            time.sleep(0.05)

    mover = threading.Thread(target=keep_moving, daemon=True)
    mover.start()
    try:
        with caplog.at_level("WARNING", logger="harvest_estate_assets"):
            harvest.run_watched(
                sleeper(1.2),
                env=None,
                timeout=0.3,
                stall_timeout=5.0,
                probe=probe,
                poll_interval=0.05,
                heartbeat=1000.0,
                label="asset",
            )
    finally:
        stop.set()
        mover.join(2)
    warnings = [r.getMessage() for r in caplog.records if "NOT killing" in r.getMessage()]
    assert warnings, "we sailed past our own --download-timeout without saying so"
    assert "--download-timeout" in warnings[0]
    assert len(warnings) == 1, "the announcement repeated on every poll"


# --- the timeouts are reachable from the CLI ----------------------------------------------------


def test_the_ceiling_is_no_longer_hard_coded() -> None:
    """The operator's actual complaint: 600 was unreachable, so the asset was unharvestable."""
    help_text = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"), "--help"],
        capture_output=True,
        # The module's own docstring is non-ASCII and it forces UTF-8 on its streams; decoding the
        # pipe with the Windows locale codec raises UnicodeDecodeError and hides the real answer.
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    ).stdout
    assert "--download-timeout" in help_text
    assert "--download-stall-timeout" in help_text


def test_download_passes_the_operator_s_timeouts_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run_watched(cmd, env, **kwargs):  # pylint: disable=unused-argument
        seen.update(kwargs)
        return harvest.WatchedRun(0, "", "", 1.0, "", "", True)

    monkeypatch.setattr(harvest, "run_watched", fake_run_watched)
    monkeypatch.setattr(harvest, "engine_child_env", lambda env: {})
    ok, detail = harvest.download(
        "workbook",
        "wb-1",
        tmp_path / "wb.twbx",
        {"TABLEAU_SERVER_URL": "https://example.invalid"},
        tmp_path,
        timeout=1234.0,
        stall_timeout=77.0,
        label="[3/9] Something",
    )
    assert (ok, detail) == (True, "")
    assert seen["timeout"] == 1234.0
    assert seen["stall_timeout"] == 77.0
    assert seen["label"] == "[3/9] Something"


def test_a_watchdog_verdict_reaches_the_caller_as_the_failure_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        harvest,
        "run_watched",
        lambda cmd, env, **kwargs: harvest.WatchedRun(None, "", "", 900.0, "stalled", "stalled: no progress", True),
    )
    monkeypatch.setattr(harvest, "engine_child_env", lambda env: {})
    ok, detail = harvest.download(
        "workbook", "wb-1", tmp_path / "wb.twbx", {"TABLEAU_SERVER_URL": "https://example.invalid"}, tmp_path
    )
    assert (ok, detail) == (False, "stalled: no progress")


# --- the real probe, on this machine ------------------------------------------------------------


def test_the_real_probe_sums_a_process_SUBTREE_not_just_the_pid_handed_to_popen() -> None:
    """Measured 2026-09-03: `.venv/Scripts/python.exe` is a uv TRAMPOLINE.

    `Popen.pid` was 35152 while the child's own `os.getpid()` reported 20856, and the trampoline's
    I/O counters never move. A probe that sampled only `Popen.pid` would report every download as
    stalled. This asserts the subtree walk reaches the descendant that does the work.
    """
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", "import os,sys,time;print(os.getpid(),flush=True);time.sleep(5)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        real_pid = int(child.stdout.readline().strip())
        tree = harvest.process_tree(child.pid)
        assert child.pid in tree
        assert real_pid in tree, (
            f"the subtree walk missed the real interpreter: popen={child.pid} real={real_pid} {tree}"
        )
    finally:
        harvest.terminate_tree(child)
        child.wait(timeout=30)


def test_the_real_probe_returns_a_number_that_moves_for_a_working_child() -> None:
    """A probe that always returns the same value is indistinguishable from a stall."""
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [
            sys.executable,
            "-c",
            "import time\nfor _ in range(200):\n open(__file__ if False else 'nul','rb').read(1)\n time.sleep(0.01)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        first = harvest.transferred_bytes(child.pid)
        assert first is not None, "no progress signal at all on this platform"
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline:
            if (harvest.transferred_bytes(child.pid) or 0) != first:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"the probe never moved for a child doing real I/O (stuck at {first})")
    finally:
        harvest.terminate_tree(child)
        child.wait(timeout=30)


def test_terminate_tree_kills_the_descendant_too() -> None:
    """Killing only `Popen.pid` would orphan the real interpreter, socket and session included."""
    parent = subprocess.Popen(  # pylint: disable=consider-using-with
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time\n"
            "kid = subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
            "print(kid.pid, flush=True)\n"
            "time.sleep(120)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    grandchild = int(parent.stdout.readline().strip())
    harvest.terminate_tree(parent)
    parent.wait(timeout=30)
    deadline = time.perf_counter() + 10
    while time.perf_counter() < deadline:
        if grandchild not in harvest.process_tree(grandchild)[1:] and not _alive(grandchild):
            break
        time.sleep(0.2)
    assert not _alive(grandchild), f"the descendant {grandchild} survived terminate_tree"


def _alive(pid: int) -> bool:
    """True while `pid` is a live process (Windows: an openable, non-exited handle)."""
    if sys.platform == "win32":
        lib = harvest.kernel32()
        handle = lib.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = harvest.ctypes.c_uint32()
        lib.GetExitCodeProcess.argtypes = [harvest.ctypes.c_void_p, harvest.ctypes.POINTER(harvest.ctypes.c_uint32)]
        ok = lib.GetExitCodeProcess(handle, harvest.ctypes.byref(code))
        lib.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        import os  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --- the tally has to close ----------------------------------------------------------------------


def sweep_rows() -> list[dict]:
    """The customer's shape: some parsed, some failed a parser, some never downloaded at all."""
    return [
        {"name": "Parsed A", "kind": "workbook", "ours": {"ok": True}, "theirs": {"ok": True}},
        {"name": "Parsed B", "kind": "workbook", "ours": {"ok": True}, "theirs": {"ok": True}},
        {
            "name": "Ours only",
            "kind": "workbook",
            "ours": {"ok": False, "error": "ValueError: bad"},
            "theirs": {"ok": True},
        },
        {"name": "His only", "kind": "workbook", "ours": {"ok": True}, "theirs": {"ok": False, "error": "KeyError: x"}},
        {
            "name": "Both",
            "kind": "workbook",
            "ours": {"ok": False, "error": "ValueError: bad"},
            "theirs": {"ok": False, "error": "KeyError: x"},
        },
        {
            "name": "IA Redemptions by Campaign Report",
            "kind": "workbook",
            "download_error": "timeout after 600s (elapsed 601.4s)",
        },
        {
            "name": "Distribution of Users by Portal Load Time",
            "kind": "workbook",
            "download_error": "timeout after 600s (elapsed 612.9s)",
        },
        {"name": "DS_Sessions_by_Product", "kind": "datasource", "download_error": "download failed (500)"},
    ]


def test_a_never_downloaded_asset_is_named_in_the_report(tmp_path: Path) -> None:
    """It used to land in NO bucket while still counting in the denominator."""
    text = harvest.summarise(sweep_rows(), tmp_path)
    assert "## Downloads that never landed" in text
    assert "IA Redemptions by Campaign Report" in text
    assert "DS_Sessions_by_Product" in text


def test_the_header_arithmetic_closes(tmp_path: Path) -> None:
    """`47 != 0 + 0 + 41` is how a partial harvest read as complete."""
    rows = sweep_rows()
    text = harvest.summarise(rows, tmp_path)
    assert f"**{len(rows)} asset(s)**" in text
    assert f"never downloaded {len(harvest.never_downloaded(rows))}" in text
    closure = next(line for line in text.splitlines() if line.startswith("Disjoint buckets"))
    assert closure.rstrip(".").endswith(f"= {len(rows)}"), closure
    assert "2 parsed by both + 1 ours only + 1 his only + 1 both parsers + 3 never downloaded" in closure


def test_the_parser_buckets_ignore_rows_that_never_reached_a_parser(tmp_path: Path) -> None:
    """A download failure is not a parser verdict and must not be reported as one."""
    text = harvest.summarise(sweep_rows(), tmp_path)
    header = text.splitlines()[2]
    assert "ours failed 2, his failed 2, both parsed 2, never downloaded 3" in header, header


def test_download_failures_are_grouped_by_shape_not_by_elapsed_seconds(tmp_path: Path) -> None:
    """Two timeouts differing only in their elapsed seconds are ONE finding, not two."""
    text = harvest.summarise(sweep_rows(), tmp_path)
    timeout_bullets = [line for line in text.splitlines() if line.startswith("- **") and "timeout after" in line]
    assert timeout_bullets == ["- **2x** `timeout after Ns (elapsed Ns)`"], timeout_bullets


def test_a_clean_sweep_still_says_none(tmp_path: Path) -> None:
    """The new section must not cry wolf when every asset landed."""
    text = harvest.summarise([r for r in sweep_rows() if "download_error" not in r], tmp_path)
    section = text.split("## Downloads that never landed", 1)[1]
    assert section.lstrip().startswith("_none_")
    assert "never downloaded 0" in text


def test_the_failed_downloads_are_reported_to_the_operator_at_the_end(caplog: pytest.LogCaptureFixture) -> None:
    """`download_error` was written to the record and surfaced NOWHERE a human looks."""
    with caplog.at_level("WARNING", logger="harvest_estate_assets"):
        missing = harvest.report_failed_downloads(sweep_rows())
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert len(missing) == 3
    assert "IA Redemptions by Campaign Report" in messages
    assert "timeout after 600s (elapsed 601.4s)" in messages
    assert "not successes" in messages


def test_nothing_is_reported_when_every_asset_landed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="harvest_estate_assets"):
        assert harvest.report_failed_downloads([r for r in sweep_rows() if "download_error" not in r]) == []
    assert not [r for r in caplog.records if "NEVER DOWNLOADED" in r.getMessage()]


# --- end to end through main() -------------------------------------------------------------------


def test_main_forwards_the_operator_s_timeouts_to_every_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Parsing a flag and then ignoring it looks exactly like having no flag at all.

    A mutation that replaced `args.download_timeout` with the module default at the call site
    survived every other test in this file, because they all call `download()` directly.
    """
    db = tmp_path / "estate.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        INSERT INTO workbook VALUES ('wb-ia', 'IA Redemptions', 'p');
        """
    )
    con.commit()
    con.close()

    seen: list[dict] = []
    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "download", lambda *a, **k: (seen.append(k), (False, "nope"))[1])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harvest_estate_assets.py",
            "--out",
            str(tmp_path / "_sweep"),
            "--db",
            str(db),
            "--allow-unignored-out",
            "--download-timeout",
            "1234",
            "--download-stall-timeout",
            "77",
        ],
    )
    assert harvest.main() == 0
    assert seen, "no download was attempted at all"
    assert seen[0]["timeout"] == 1234.0, f"the CLI ceiling never reached download(): {seen[0]}"
    assert seen[0]["stall_timeout"] == 77.0, f"the CLI stall timeout never reached download(): {seen[0]}"
    assert seen[0]["timeout"] != harvest.DEFAULT_DOWNLOAD_TIMEOUT, "the test cannot tell the flag from the default"


def test_a_failed_download_reaches_parse_sweep_md_through_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The committed artifact, not just the console, has to name the asset that never landed."""
    db = tmp_path / "estate.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        INSERT INTO workbook VALUES ('wb-ia', 'IA Redemptions by Campaign Report', 'p');
        """
    )
    con.commit()
    con.close()

    out = tmp_path / "_sweep"
    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(
        harvest,
        "download",
        lambda *a, **k: (False, "stalled: no progress for 120s (elapsed 240s). Raise --download-stall-timeout"),
    )
    monkeypatch.setattr(
        sys, "argv", ["harvest_estate_assets.py", "--out", str(out), "--db", str(db), "--allow-unignored-out"]
    )
    assert harvest.main() == 0

    text = (out / "parse-sweep.md").read_text(encoding="utf-8")
    assert "**1 asset(s)** — ours failed 0, his failed 0, both parsed 0, never downloaded 1." in text
    assert "IA Redemptions by Campaign Report" in text.split("## Downloads that never landed", 1)[1]
