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
import io
import sqlite3
import subprocess
import sys
import threading
import time
import tokenize
from concurrent.futures import Future
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


# --- blind-review round 2: two ways this could still kill a HEALTHY download --------------------


def test_the_stall_default_is_never_stricter_than_the_fetchers_own_read_timeout() -> None:
    """Finding 1. The fetcher passes `timeout=300` to `urlopen` (`fetch_tds.py:407,423`), which is
    the contract for how long a healthy transfer may go quiet. A stall deadline below that kills a
    bursty source urllib is perfectly happy with — the wall-clock cliff turned into a shorter
    inactivity cliff. The first version of this fix shipped 120s and did exactly that.
    """
    assert harvest.DEFAULT_STALL_TIMEOUT >= harvest.ENGINE_READ_TIMEOUT_SECONDS, (
        f"a {harvest.DEFAULT_STALL_TIMEOUT:.0f}s stall deadline pre-empts the fetcher's own "
        f"{harvest.ENGINE_READ_TIMEOUT_SECONDS:.0f}s per-read timeout"
    )


def test_a_bursty_but_healthy_transfer_survives_a_gap_the_fetcher_would_tolerate() -> None:
    """Finding 1, as behaviour rather than as an inequality between two constants.

    Everything is scaled from the REAL constants by the same factor, so the test asks one question:
    *is our kill threshold generous enough for a gap the downloader itself allows?* A pause is
    chosen that urllib would tolerate (below its read timeout) but that the old 120s default would
    not (above its scaled equivalent). Lower `DEFAULT_STALL_TIMEOUT` back under the read timeout and
    this goes red; the pause and the child's read timeout do not move with it.
    """
    read_timeout = 2.0  # stands in for the fetcher's 300s
    scale = read_timeout / harvest.ENGINE_READ_TIMEOUT_SECONDS
    pause = 0.9 * read_timeout  # urllib is happy: below its read timeout
    scaled_stall = harvest.DEFAULT_STALL_TIMEOUT * scale
    assert pause > 120.0 * scale, "the pause must be one the REJECTED 120s default would have killed"

    probe = Counter(1000, moves=1)  # one movement, then a long quiet gap: the bursty shape
    run = harvest.run_watched(
        sleeper(pause + 0.4),
        env=None,
        timeout=0,  # the ceiling is not what is under test here
        stall_timeout=scaled_stall,
        probe=probe,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict == "", f"killed a healthy transfer during a {pause:.1f}s gap urllib allows: {run.detail}"
    assert run.progress_observed is True


def test_losing_the_probe_after_movement_does_not_kill_a_healthy_download() -> None:
    """Finding 2, the reviewer's reproduction: probe returns 0, 1, then None forever.

    `None` means "cannot read", never "no bytes moved". Access denial, a descendant exiting between
    enumeration and counter read, or a PARTIAL subtree read all produce it — and the partial case is
    the nasty one, because the readable trampoline flatlines while the unreadable descendant is the
    process actually doing the work.
    """
    readings = [0, 1]

    def lost_probe(pid: int) -> int | None:  # pylint: disable=unused-argument
        return readings.pop(0) if readings else None

    run = harvest.run_watched(
        sleeper(1.0),
        env=None,
        timeout=30,
        stall_timeout=0.25,  # would fire almost immediately if a lost probe counted as a flatline
        probe=lost_probe,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict != "stalled", f"a lost probe was reported as a hung transfer: {run.detail}"
    assert (run.verdict, run.returncode) == ("", 0)


def test_a_lost_probe_still_ends_the_run_but_as_a_CEILING_not_a_stall() -> None:
    """Disarming the stall deadline must not mean waiting forever — just labelling it honestly."""
    readings = [0, 1]

    def lost_probe(pid: int) -> int | None:  # pylint: disable=unused-argument
        return readings.pop(0) if readings else None

    run = harvest.run_watched(
        sleeper(30),
        env=None,
        timeout=0.4,
        stall_timeout=0.05,
        probe=lost_probe,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict == "ceiling", f"expected an honest ceiling verdict, got {run.verdict!r}"
    assert "progress was seen and then the signal was lost" in run.detail
    assert "--download-timeout" in run.detail


def test_the_wall_clock_restarts_from_the_MOMENT_the_signal_was_lost() -> None:
    """A transfer that progressed for a while and then went blind gets a fresh window, not a kill.

    Measured from process start, a download that progressed for 9 minutes and lost its probe would
    be killed one minute later; measured from the loss, it gets the full ceiling to prove itself.
    """
    moving_for = 0.5
    started = time.perf_counter()

    def probe_that_dies(pid: int) -> int | None:  # pylint: disable=unused-argument
        elapsed = time.perf_counter() - started
        return int(elapsed * 1000) if elapsed < moving_for else None

    run = harvest.run_watched(
        sleeper(30),
        env=None,
        timeout=0.4,
        stall_timeout=60,
        probe=probe_that_dies,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict == "ceiling"
    assert run.elapsed >= moving_for + 0.4, (
        f"the ceiling was measured from process start, not from the loss of the signal "
        f"(killed after {run.elapsed:.2f}s, expected at least {moving_for + 0.4:.2f}s)"
    )


def test_losing_the_signal_is_announced_once(caplog: pytest.LogCaptureFixture) -> None:
    readings = [0, 1]

    def lost_probe(pid: int) -> int | None:  # pylint: disable=unused-argument
        return readings.pop(0) if readings else None

    with caplog.at_level("WARNING", logger="harvest_estate_assets"):
        harvest.run_watched(
            sleeper(0.8),
            env=None,
            timeout=30,
            stall_timeout=0.25,
            probe=lost_probe,
            poll_interval=0.05,
            heartbeat=1000.0,
            label="asset",
        )
    lost = [r.getMessage() for r in caplog.records if "lost the download-progress signal" in r.getMessage()]
    assert len(lost) == 1, f"expected exactly one announcement, got {len(lost)}"


def test_a_partial_subtree_reading_is_reported_as_UNAVAILABLE_not_as_a_smaller_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sum of the processes we CAN read is not a smaller answer; it is a different question.

    With the descendant unreadable, the readable trampoline's constant counters look exactly like a
    stalled download.
    """
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", "import time;time.sleep(5)"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert harvest.transferred_bytes(child.pid) is not None, "the readable case must still answer"
        monkeypatch.setattr(harvest, "process_tree", lambda pid: [pid, 999_999])
        assert harvest.transferred_bytes(child.pid) is None, "a partial reading was passed off as a total"
    finally:
        harvest.terminate_tree(child)
        child.wait(timeout=30)


def test_an_unreadable_process_reports_no_signal_at_all() -> None:
    assert harvest.transferred_bytes(999_999) is None


def test_the_cli_warns_when_the_stall_deadline_undercuts_the_fetcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The flag is the operator's to set, but a value below 300s has a consequence worth naming."""
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
    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "download", lambda *a, **k: (False, "nope"))
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
            "--download-stall-timeout",
            "30",
        ],
    )
    with caplog.at_level("WARNING", logger="harvest_estate_assets"):
        assert harvest.main() == harvest.EXIT_NOTHING_ASSESSED
    warned = [r.getMessage() for r in caplog.records if "BELOW the fetcher's own" in r.getMessage()]
    assert warned, "an operator undercut the fetcher's read timeout and was told nothing"
    assert "300s per-read timeout" in warned[0]


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
    assert "usable download-progress signal" in run.detail, f"it does not say WHY it could not tell: {run.detail}"
    assert "blind the whole time" in run.detail, f"it does not distinguish never-seen from lost: {run.detail}"
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


@pytest.mark.parametrize("verdict", ["stalled", "ceiling"])
def test_a_watchdog_verdict_cannot_carry_the_childs_reflected_pat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict: str
) -> None:
    """⚠️ The PATH #482 ADDED, and the one the credential tests do not reach.

    `download()` redacts on the `returncode != 0` branch, and that branch is what
    `tests/test_tableau_env.py` exercises. A watchdog verdict returns EARLIER, from `run.detail`,
    and drops the child's stderr entirely -- which is safe today and is one plausible "let's include
    what the child said" edit away from not being. The killed child is the likeliest one to have
    been mid-sign-in, so its stderr is exactly where a reflected PAT would sit.
    """
    secret = "SENTINEL_PAT_qrstuvwxyz012345"
    env = {
        "TABLEAU_SERVER_URL": "https://example.invalid",
        "TABLEAU_PAT_NAME": "probe-name",
        "TABLEAU_PAT_SECRET": secret,
    }
    reflected = f'401: {{"credentials": {{"personalAccessTokenSecret": "{secret}"}}}}'
    monkeypatch.setattr(
        harvest,
        "run_watched",
        lambda cmd, env_, **kwargs: harvest.WatchedRun(
            None, reflected, reflected, 900.0, verdict, f"{verdict}: no progress for 420s", False
        ),
    )
    ok, detail = harvest.download("workbook", "wb-1", tmp_path / "wb.twbx", env, tmp_path)

    assert ok is False
    assert secret not in detail, "a killed child's reflected PAT reached the operator-facing detail"
    assert "probe-name" not in detail, "the PAT name reached the operator-facing detail"
    # Non-vacuity: the verdict text itself must survive, or this passes on an empty detail.
    assert detail.startswith(f"{verdict}: no progress"), "the watchdog's own diagnostic was lost"


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
    assert (
        "2 parsed by both + 1 ours only + 1 his only + 1 both parsers + 0 invalid/indeterminate + 3 never downloaded"
        in closure
    )


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


# --- issue #483: an `ok` that is not exactly `true`/`false` must not enter no bucket, or a false one


def rows_with_ok(ours_ok: object, theirs_ok: object) -> list[dict]:
    """One asset that DID download, with the given (possibly malformed) `ok` on each side."""
    return [
        {
            "name": "Weird Verdict",
            "kind": "workbook",
            "luid": "wb-weird",
            "ours": {"ok": ours_ok},
            "theirs": {"ok": theirs_ok},
        }
    ]


@pytest.mark.parametrize(
    "ours_ok",
    [None, "true", "yes", 1, 0, [], ["partial"], {}],
    ids=["none", "string-true", "string-yes", "int-1", "int-0", "empty-list", "list", "dict"],
)
def test_a_malformed_ok_is_invalid_not_a_silent_success_or_failure(tmp_path: Path, ours_ok: object) -> None:
    """A truthy non-bool `ok` used to slip into `both_ok`; a falsy one used to slip past `is False`.

    Neither is a real verdict. Both must land in the invalid/indeterminate bucket and nowhere else.
    """
    rows = rows_with_ok(ours_ok, True)
    assert harvest.indeterminate_parser_outcomes(rows) == rows
    text = harvest.summarise(rows, tmp_path)
    assert "invalid/indeterminate outcome 1." in text
    assert "both parsed 0" in text.splitlines()[2]
    assert "Weird Verdict" in text.split("## Invalid/indeterminate parser outcome", 1)[1]


def test_ok_key_missing_from_an_existing_ours_dict_is_invalid(tmp_path: Path) -> None:
    """`{"ours": {}}` (no `ok` key at all) is a downloaded, parsed row with an incomplete verdict --
    distinct from a row with no `ours` key at all, which `never_downloaded()` already owns.
    """
    rows = [{"name": "Weird Verdict", "kind": "workbook", "luid": "wb-weird", "ours": {}, "theirs": {"ok": True}}]
    assert harvest.indeterminate_parser_outcomes(rows) == rows
    assert harvest.never_downloaded(rows) == [], "this row DID download and reach both parsers"
    text = harvest.summarise(rows, tmp_path)
    assert "invalid/indeterminate outcome 1." in text


def test_the_reproduction_both_sides_none_used_to_enter_no_bucket(tmp_path: Path) -> None:
    """The issue's own repro: total 1, every bucket 0, and the closure never caught it."""
    rows = rows_with_ok(None, None)
    text = harvest.summarise(rows, tmp_path)
    closure = next(line for line in text.splitlines() if line.startswith("Disjoint buckets"))
    assert closure.rstrip(".").endswith("= 1"), closure
    assert (
        "0 parsed by both + 0 ours only + 0 his only + 0 both parsers + 1 invalid/indeterminate + 0 never downloaded"
        in closure
    )


def test_a_valid_boolean_ok_is_never_misclassified_as_invalid(tmp_path: Path) -> None:
    """`True`/`False` themselves must not trip the new check -- only non-booleans should."""
    assert harvest.indeterminate_parser_outcomes(rows_with_ok(True, True)) == []
    assert harvest.indeterminate_parser_outcomes(rows_with_ok(False, False)) == []
    assert harvest.indeterminate_parser_outcomes(rows_with_ok(True, False)) == []


def test_an_invalid_outcome_is_not_a_clean_exit(tmp_path: Path) -> None:
    """The whole point: it must not exit 0 as a complete sweep."""
    rows = sweep_rows_all_ok_plus_one_invalid()
    assert harvest.sweep_exit_code(rows) == harvest.EXIT_PARTIAL


def sweep_rows_all_ok_plus_one_invalid() -> list[dict]:
    return [
        {"name": "Fine A", "kind": "workbook", "ours": {"ok": True}, "theirs": {"ok": True}},
        {"name": "Fine B", "kind": "workbook", "ours": {"ok": True}, "theirs": {"ok": True}},
        {"name": "Weird", "kind": "workbook", "ours": {"ok": "yes"}, "theirs": {"ok": True}},
    ]


def test_main_exits_partial_not_ok_when_an_asset_has_a_malformed_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: a real parser bug that returns a non-boolean `ok` must not read as a clean run."""
    db = tmp_path / "estate.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        INSERT INTO workbook VALUES ('wb-ia', 'IA Redemptions', 'p');
        """
    )
    con.commit()
    con.close()
    out = tmp_path / "_sweep"
    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "download", landing_download)
    monkeypatch.setattr(harvest, "parse_asset", lambda path, scripts: ({"ok": "yes"}, {"ok": True}))
    monkeypatch.setattr(
        sys, "argv", ["harvest_estate_assets.py", "--out", str(out), "--db", str(db), "--allow-unignored-out"]
    )
    assert harvest.main() == harvest.EXIT_PARTIAL, "a malformed ok must not read as a complete sweep"
    assert "invalid/indeterminate outcome 1." in (out / "parse-sweep.md").read_text(encoding="utf-8")


# --- blind-review follow-up: the PER-ASSET parse line must use the same strict verdict -----------


def _finished_future(result: tuple[dict, dict]) -> Future:
    future: Future = Future()
    future.set_result(result)
    return future


def test_record_parse_never_prints_ours_equals_ok_for_a_malformed_truthy_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A truthy-but-non-boolean `ok` (`"true"`, `1`, a list, a dict) used to print `ours=ok` here,
    even though the SAME row was correctly routed to the invalid/indeterminate bucket by
    `summarise()`. The two must never disagree about one row (blind-review follow-up to #483).
    """
    results: list[dict] = []
    with caplog.at_level("INFO", logger="harvest_estate_assets"):
        harvest.record_parse(
            (1, {"name": "Weird"}, _finished_future(({"ok": "true"}, {"ok": True}))), results, 1, time.perf_counter()
        )
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "ours=ok" not in messages, messages
    assert "ours=INVALID" in messages, messages
    assert "his=ok" in messages, messages


@pytest.mark.parametrize(
    "malformed",
    [None, "true", "yes", 1, 0, [], ["partial"], {}],
    ids=["none", "string-true", "string-yes", "int-1", "int-0", "empty-list", "list", "dict"],
)
def test_record_parse_labels_every_malformed_ok_shape_as_invalid(
    caplog: pytest.LogCaptureFixture, malformed: object
) -> None:
    """Every shape the summary-level test covers must ALSO be caught at the per-asset log line."""
    results: list[dict] = []
    with caplog.at_level("INFO", logger="harvest_estate_assets"):
        harvest.record_parse(
            (1, {"name": "Weird"}, _finished_future(({"ok": malformed}, {"ok": True}))), results, 1, time.perf_counter()
        )
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "ours=ok" not in messages and "ours=FAIL" not in messages, messages
    assert "ours=INVALID" in messages, messages


def test_record_parse_preserves_exact_true_false_wording(caplog: pytest.LogCaptureFixture) -> None:
    """A real boolean verdict must still read exactly `ok`/`FAIL`, never `INVALID`."""
    results: list[dict] = []
    with caplog.at_level("INFO", logger="harvest_estate_assets"):
        harvest.record_parse(
            (1, {"name": "Clean"}, _finished_future(({"ok": True}, {"ok": False}))), results, 1, time.perf_counter()
        )
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "ours=ok" in messages, messages
    assert "his=FAIL" in messages, messages
    assert "INVALID" not in messages, messages


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


def landing_download(kind, luid, target, env, scripts, **kwargs):  # pylint: disable=unused-argument
    """A `download()` stand-in whose asset actually LANDS, so `main()` reaches the parsers."""
    Path(target).write_text("<workbook/>", encoding="utf-8")
    return True, ""


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
    assert harvest.main() == harvest.EXIT_NOTHING_ASSESSED, "a harvest that assessed nothing must not exit 0"
    assert seen, "no download was attempted at all"
    assert seen[0]["timeout"] == 1234.0, f"the CLI ceiling never reached download(): {seen[0]}"
    assert seen[0]["stall_timeout"] == 77.0, f"the CLI stall timeout never reached download(): {seen[0]}"
    assert seen[0]["timeout"] != harvest.DEFAULT_DOWNLOAD_TIMEOUT, "the test cannot tell the flag from the default"


# --- leg 4: a failed datasource does not fail alone ----------------------------------------------


def estate_with_a_binding(path: Path) -> Path:
    """`IA IFC Sessions` binds to `DS_Sessions_by_Product`; `Standalone` binds to nothing."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        INSERT INTO workbook VALUES ('wb-ifc', 'IA IFC Sessions', 'p');
        INSERT INTO workbook VALUES ('wb-solo', 'Standalone', 'p');
        INSERT INTO datasource VALUES ('ds-sessions', 'DS_Sessions_by_Product', 'p');
        INSERT INTO dependency VALUES ('wb-ifc', 'ds-sessions', 'DS_Sessions_by_Product');
        """
    )
    con.commit()
    con.close()
    return path


def binding_rows() -> list[dict]:
    """The datasource failed; both workbooks landed. Only one of them is affected."""
    return [
        {"name": "DS_Sessions_by_Product", "kind": "datasource", "luid": "ds-sessions", "download_error": "500"},
        {"name": "IA IFC Sessions", "kind": "workbook", "luid": "wb-ifc", "ours": {"ok": True}, "theirs": {"ok": True}},
        {"name": "Standalone", "kind": "workbook", "luid": "wb-solo", "ours": {"ok": True}, "theirs": {"ok": True}},
    ]


def edges_from(db: Path) -> list[tuple[str, str, str, str, str]]:
    con = sqlite3.connect(db)
    try:
        return harvest.dependency_edges(con)
    finally:
        con.close()


def test_a_failed_datasource_names_the_workbooks_it_orphans(tmp_path: Path) -> None:
    """Their agent worked this out BY HAND; the harvester already resolves the edge."""
    edges = edges_from(estate_with_a_binding(tmp_path / "estate.db"))
    assert harvest.orphaned_dependents(binding_rows(), edges) == [
        ("DS_Sessions_by_Product", [("wb-ifc", "IA IFC Sessions")])
    ]


def test_a_workbook_bound_to_NOTHING_that_failed_is_not_flagged(tmp_path: Path) -> None:
    """Flagging every workbook would make the warning worthless."""
    edges = edges_from(estate_with_a_binding(tmp_path / "estate.db"))
    orphans = harvest.orphaned_dependents(binding_rows(), edges)
    assert "Standalone" not in [name for _, workbooks in orphans for _, name in workbooks]


def test_a_workbook_that_ITSELF_failed_is_not_listed_as_an_orphan(tmp_path: Path) -> None:
    """It is already in the never-landed list and is not about to be converted."""
    rows = binding_rows()
    rows[1] = {"name": "IA IFC Sessions", "kind": "workbook", "luid": "wb-ifc", "download_error": "timeout"}
    edges = edges_from(estate_with_a_binding(tmp_path / "estate.db"))
    assert harvest.orphaned_dependents(rows, edges) == []


def test_nothing_is_orphaned_when_the_datasource_landed(tmp_path: Path) -> None:
    rows = binding_rows()
    rows[0] = {
        "name": "DS_Sessions_by_Product",
        "kind": "datasource",
        "luid": "ds-sessions",
        "ours": {"ok": True},
        "theirs": {"ok": True},
    }
    edges = edges_from(estate_with_a_binding(tmp_path / "estate.db"))
    assert harvest.orphaned_dependents(rows, edges) == []


def test_an_edge_resolved_only_by_NAME_still_finds_the_dependent(tmp_path: Path) -> None:
    """A survey that could not resolve the LUID must not silently drop the binding."""
    db = estate_with_a_binding(tmp_path / "estate.db")
    con = sqlite3.connect(db)
    con.execute("DELETE FROM dependency")
    con.execute("INSERT INTO dependency VALUES ('wb-ifc', '', ' ds_sessions_by_product ')")
    con.commit()
    con.close()
    assert harvest.orphaned_dependents(binding_rows(), edges_from(db)) == [
        ("DS_Sessions_by_Product", [("wb-ifc", "IA IFC Sessions")])
    ]


def test_the_orphans_reach_the_report_and_the_operator(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    orphans = harvest.orphaned_dependents(binding_rows(), edges_from(estate_with_a_binding(tmp_path / "estate.db")))
    text = harvest.summarise(binding_rows(), tmp_path, orphans)
    with caplog.at_level("WARNING", logger="harvest_estate_assets"):
        harvest.report_failed_downloads(binding_rows(), orphans)
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "## Do not convert yet" in text
    assert "IA IFC Sessions" in text.split("## Do not convert yet", 1)[1]
    assert "wb-ifc" in text.split("## Do not convert yet", 1)[1], "the orphan's LUID must reach the report too"
    assert "DO NOT CONVERT YET" in messages
    assert "IA IFC Sessions" in messages
    assert "wb-ifc" in messages, "the orphan's LUID must reach the operator's log line too"


def test_the_section_is_absent_when_nothing_is_orphaned(tmp_path: Path) -> None:
    assert "## Do not convert yet" not in harvest.summarise(binding_rows(), tmp_path, [])


# --- issue #483: two same-named workbooks in different projects must remain two orphans ----------


def estate_with_two_same_named_workbooks(path: Path) -> Path:
    """Two DIFFERENT workbooks, same display name, in different projects, both bound to one datasource."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        INSERT INTO project VALUES ('proj-a', 'Region A');
        INSERT INTO project VALUES ('proj-b', 'Region B');
        INSERT INTO workbook VALUES ('wb-a', 'Revenue Dashboard', 'proj-a');
        INSERT INTO workbook VALUES ('wb-b', 'Revenue Dashboard', 'proj-b');
        INSERT INTO datasource VALUES ('ds-sessions', 'DS_Sessions_by_Product', 'proj-a');
        INSERT INTO dependency VALUES ('wb-a', 'ds-sessions', 'DS_Sessions_by_Product');
        INSERT INTO dependency VALUES ('wb-b', 'ds-sessions', 'DS_Sessions_by_Product');
        """
    )
    con.commit()
    con.close()
    return path


def duplicate_named_binding_rows() -> list[dict]:
    """The datasource failed; BOTH same-named workbooks landed."""
    return [
        {"name": "DS_Sessions_by_Product", "kind": "datasource", "luid": "ds-sessions", "download_error": "500"},
        {
            "name": "Revenue Dashboard",
            "kind": "workbook",
            "luid": "wb-a",
            "ours": {"ok": True},
            "theirs": {"ok": True},
        },
        {
            "name": "Revenue Dashboard",
            "kind": "workbook",
            "luid": "wb-b",
            "ours": {"ok": True},
            "theirs": {"ok": True},
        },
    ]


def test_two_same_named_workbooks_in_different_projects_are_two_orphans(tmp_path: Path) -> None:
    """Before the fix, `by_datasource` collected workbook NAMES into a `set`, so one 'Revenue
    Dashboard' silently absorbed the other -- a real second workbook vanished from the report.
    LUID must be kept in the return value, not just used internally and then thrown away, or the
    two entries print identically and read as one duplicated line (blind-review follow-up).
    """
    edges = edges_from(estate_with_two_same_named_workbooks(tmp_path / "estate.db"))
    orphans = harvest.orphaned_dependents(duplicate_named_binding_rows(), edges)
    assert orphans == [("DS_Sessions_by_Product", [("wb-a", "Revenue Dashboard"), ("wb-b", "Revenue Dashboard")])]
    assert len(orphans[0][1]) == 2, "two distinct workbook identities collapsed into one orphan"
    assert len({identity for identity, _ in orphans[0][1]}) == 2, "both entries must carry a DISTINCT identity"


def test_only_one_of_two_same_named_workbooks_landing_still_reports_just_that_one(tmp_path: Path) -> None:
    """The LUID-keyed fix must not accidentally start double-counting a single landed workbook."""
    rows = duplicate_named_binding_rows()
    rows[2] = {"name": "Revenue Dashboard", "kind": "workbook", "luid": "wb-b", "download_error": "timeout"}
    edges = edges_from(estate_with_two_same_named_workbooks(tmp_path / "estate.db"))
    orphans = harvest.orphaned_dependents(rows, edges)
    assert orphans == [("DS_Sessions_by_Product", [("wb-a", "Revenue Dashboard")])]


def test_same_named_orphans_are_distinguishable_in_the_markdown_and_log(tmp_path: Path) -> None:
    """The report/log MUST show the LUID beside the name, or the two lines read as one duplicate."""
    edges = edges_from(estate_with_two_same_named_workbooks(tmp_path / "estate.db"))
    orphans = harvest.orphaned_dependents(duplicate_named_binding_rows(), edges)
    text = harvest.summarise(duplicate_named_binding_rows(), tmp_path, orphans)
    section = text.split("## Do not convert yet", 1)[1]
    assert "wb-a" in section and "wb-b" in section, "both distinct identities must reach the report"


def test_missing_luid_falls_back_to_project_plus_name_identity() -> None:
    """When an edge cannot resolve a workbook LUID, the strongest identity left is the
    project-qualified name -- never a bare display name, which is the exact collision #483 exists
    to close. Built by hand: a real site survey never leaves `dependency_edges`' workbook LUID
    blank (no fallback join on that side), so this exercises the defensive branch directly.
    """
    results = [
        {"name": "DS_Sessions_by_Product", "kind": "datasource", "luid": "ds-sessions", "download_error": "500"},
        {"name": "Revenue Dashboard", "kind": "workbook", "luid": "", "ours": {"ok": True}, "theirs": {"ok": True}},
    ]
    edges = [("", "Revenue Dashboard", "Region A", "ds-sessions", "DS_Sessions_by_Product")]
    orphans = harvest.orphaned_dependents(results, edges)
    assert orphans == [("DS_Sessions_by_Product", [("Region A::Revenue Dashboard", "Revenue Dashboard")])]


def test_a_truly_unidentifiable_workbook_is_reported_not_dropped() -> None:
    """No LUID and no project left either -- the row must still be reported, never silently
    dropped, but it must also never be reported under a bare display name (issue #483)."""
    results = [
        {"name": "DS_Sessions_by_Product", "kind": "datasource", "luid": "ds-sessions", "download_error": "500"},
        {"name": "Mystery Workbook", "kind": "workbook", "luid": "", "ours": {"ok": True}, "theirs": {"ok": True}},
    ]
    edges = [("", "Mystery Workbook", "", "ds-sessions", "DS_Sessions_by_Product")]
    orphans = harvest.orphaned_dependents(results, edges)
    assert len(orphans) == 1, "an unidentifiable workbook must not be dropped"
    identity, name = orphans[0][1][0]
    assert identity.startswith("UNIDENTIFIED-"), identity
    assert name == "Mystery Workbook"


def test_an_estate_db_without_a_dependency_table_still_harvests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An older database cannot answer the orphan question; that is not a reason to fail the run.

    ⚠️ This test used to make the download FAIL and then assert `main() == 0`, which proved nothing
    about harvesting — it pinned the fail-open exit code instead (blind review of #482). The asset
    now lands, so a green run means the sweep completed on a dependency-table-less database, which
    is the claim in the title.
    """
    db = tmp_path / "estate.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        INSERT INTO workbook VALUES ('wb-ia', 'IA Redemptions', 'p');
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "download", landing_download)
    monkeypatch.setattr(harvest, "parse_asset", lambda path, scripts: ({"ok": True}, {"ok": True}))
    out = tmp_path / "_sweep"
    monkeypatch.setattr(
        sys,
        "argv",
        ["harvest_estate_assets.py", "--out", str(out), "--db", str(db), "--allow-unignored-out"],
    )
    assert harvest.main() == harvest.EXIT_OK
    assert "never downloaded 0, invalid/indeterminate outcome 0." in (out / "parse-sweep.md").read_text(
        encoding="utf-8"
    )


def test_main_end_to_end_names_the_orphaned_workbook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The whole leg, through the CLI: datasource fails, workbooks land, the report says so."""
    db = estate_with_a_binding(tmp_path / "estate.db")
    out = tmp_path / "_sweep"

    def selective_download(kind, luid, target, env, scripts, **kwargs):  # pylint: disable=unused-argument
        if kind == "datasource":
            return False, "download failed (500)"
        Path(target).write_text("<workbook/>", encoding="utf-8")
        return True, ""

    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "download", selective_download)
    monkeypatch.setattr(harvest, "parse_asset", lambda path, scripts: ({"ok": True}, {"ok": True}))
    monkeypatch.setattr(
        sys, "argv", ["harvest_estate_assets.py", "--out", str(out), "--db", str(db), "--allow-unignored-out"]
    )
    assert harvest.main() == harvest.EXIT_PARTIAL, "two workbooks landed and one datasource did not: that is PARTIAL"

    text = (out / "parse-sweep.md").read_text(encoding="utf-8")
    blocked = text.split("## Do not convert yet", 1)[1]
    assert "IA IFC Sessions" in blocked
    assert "Standalone" not in blocked, "a workbook bound to nothing that failed was flagged"


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
    assert harvest.main() == harvest.EXIT_NOTHING_ASSESSED

    text = (out / "parse-sweep.md").read_text(encoding="utf-8")
    assert (
        "**1 asset(s)** — ours failed 0, his failed 0, both parsed 0, never downloaded 1, invalid/indeterminate outcome 0."
        in text
    )
    assert "IA Redemptions by Campaign Report" in text.split("## Downloads that never landed", 1)[1]


# --- blind review round 3, finding 1: a completely unassessed harvest must not exit 0 ------------
#
# Reproduced before fixing, on a one-workbook estate with `download()` returning
# `(False, "network dead")`:
#
#   **1 asset(s)** — ours failed 0, his failed 0, both parsed 0, never downloaded 1.
#   exit_code: 0
#
# `return 0 if results else 1` counted ROWS, and a failed download appends a row. Three tests in
# this very file asserted `main() == 0` on estates where NOTHING was assessed, i.e. the suite was
# pinning the fail-open behaviour. They are corrected above, not merely re-pinned: the one whose
# subject is "an older database still harvests" now lands its asset, so it tests its own title.


def assessed(name: str) -> dict:
    """A row that reached BOTH parsers — the only shape that counts as assessed."""
    return {"name": name, "kind": "workbook", "luid": name, "ours": {"ok": True}, "theirs": {"ok": True}}


def unassessed(name: str) -> dict:
    """A row whose download failed, so it reached neither parser."""
    return {"name": name, "kind": "workbook", "luid": name, "download_error": "timeout after 600s"}


def test_a_harvest_that_assessed_NOTHING_does_not_exit_zero() -> None:
    """The defect, at the unit that decides it. Rows exist; assessments do not."""
    assert harvest.sweep_exit_code([unassessed("IA Redemptions by Campaign Report")]) == harvest.EXIT_NOTHING_ASSESSED


def test_an_empty_sweep_is_nothing_assessed_rather_than_a_clean_run() -> None:
    assert harvest.sweep_exit_code([]) == harvest.EXIT_NOTHING_ASSESSED


def test_the_customers_41_ok_6_failed_shape_is_distinguishable_from_a_total_failure() -> None:
    """The judgement call this contract turns on: partial and total must not share a code.

    SES ran 47 assets and got 41/6. Exit 0 said "clean"; a single non-zero for both shapes would say
    "something went wrong" and lose the difference between six retryable assets and a dead site.
    """
    partial = [assessed(f"ok-{i}") for i in range(41)] + [unassessed(f"bad-{i}") for i in range(6)]
    total = [unassessed(f"bad-{i}") for i in range(47)]
    clean = [assessed(f"ok-{i}") for i in range(47)]
    assert harvest.sweep_exit_code(partial) == harvest.EXIT_PARTIAL
    assert harvest.sweep_exit_code(total) == harvest.EXIT_NOTHING_ASSESSED
    assert harvest.sweep_exit_code(clean) == harvest.EXIT_OK
    assert len({harvest.sweep_exit_code(r) for r in (partial, total, clean)}) == 3, (
        "two of the three outcomes share an exit code, so automation cannot tell them apart"
    )


def test_a_PARSE_failure_is_the_report_this_script_exists_for_and_stays_exit_zero() -> None:
    """Both parsers refusing an asset is a finding, not a run failure — the asset WAS assessed."""
    rows = [{"name": "hard", "kind": "workbook", "luid": "h", "ours": {"ok": False}, "theirs": {"ok": False}}]
    assert harvest.sweep_exit_code(rows) == harvest.EXIT_OK


def test_a_totally_failed_harvest_exits_nonzero_through_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End to end, through the CLI: the reviewer's controlled experiment, now green the right way."""
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
    monkeypatch.setattr(harvest, "download", lambda *a, **k: (False, "network dead"))
    monkeypatch.setattr(
        sys, "argv", ["harvest_estate_assets.py", "--out", str(out), "--db", str(db), "--allow-unignored-out"]
    )
    assert harvest.main() == harvest.EXIT_NOTHING_ASSESSED
    # The report still has to be written: a non-zero exit must not cost the operator the evidence.
    assert "never downloaded 1, invalid/indeterminate outcome 0." in (out / "parse-sweep.md").read_text(
        encoding="utf-8"
    )


def test_the_verdict_is_SAID_not_just_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An exit code nobody prints is one an operator watching a console never sees."""
    db = estate_with_a_binding(tmp_path / "estate.db")

    def only_workbooks_land(kind, luid, target, env, scripts, **kwargs):  # pylint: disable=unused-argument
        if kind == "datasource":
            return False, "download failed (500)"
        Path(target).write_text("<workbook/>", encoding="utf-8")
        return True, ""

    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "download", only_workbooks_land)
    monkeypatch.setattr(harvest, "parse_asset", lambda path, scripts: ({"ok": True}, {"ok": True}))
    monkeypatch.setattr(
        sys,
        "argv",
        ["harvest_estate_assets.py", "--out", str(tmp_path / "_sweep"), "--db", str(db), "--allow-unignored-out"],
    )
    with caplog.at_level("WARNING", logger="harvest_estate_assets"):
        assert harvest.main() == harvest.EXIT_PARTIAL
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "PARTIAL HARVEST" in said, f"the run ended partial and never said so: {said}"
    assert "2 of 3 asset(s) assessed" in said


def test_the_exit_contract_is_documented_where_an_operator_reads_it() -> None:
    """`--help` is the contract's only reachable surface for someone not reading the source."""
    help_text = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"), "--help"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    ).stdout
    assert "Exit codes" in help_text
    assert "NOTHING COULD BE ASSESSED" in help_text


# --- blind review round 3, finding 2: the blind ceiling must outlast the child's own timer -------


def test_the_blind_ceiling_is_not_below_the_childs_own_bounded_retry_budget() -> None:
    """AGENTS.md: do not kill a tool that IS the bounded timer.

    The engine's `_http_download` makes up to `max_attempts` attempts, each of which may burn a full
    per-read timeout, before it raises its own classified error. The historical 600s ceiling sat
    below that product, so on any run where the progress probe is unreadable we killed the fetcher
    mid-budget and recorded `timeout after 600s` in place of the real HTTP failure.
    """
    assert harvest.DEFAULT_DOWNLOAD_TIMEOUT > harvest.ENGINE_DOWNLOAD_BUDGET_SECONDS, (
        f"a {harvest.DEFAULT_DOWNLOAD_TIMEOUT:.0f}s ceiling pre-empts the fetcher's own "
        f"{harvest.ENGINE_DOWNLOAD_BUDGET_SECONDS:.0f}s retry budget"
    )
    assert harvest.ENGINE_DOWNLOAD_BUDGET_SECONDS >= (
        harvest.ENGINE_DOWNLOAD_ATTEMPTS * harvest.ENGINE_READ_TIMEOUT_SECONDS
    ), "the budget does not even cover one full read timeout per attempt"


def test_the_ceiling_constants_match_the_INSTALLED_engine() -> None:
    """The arithmetic is only sound while the engine's own numbers are what we think they are.

    Read off the installed canonical engine rather than restated: `max_attempts` and the default
    `timeout` of `_http_download`, plus the `Retry-After` clamp. An engine bump that changes either
    reddens this instead of silently invalidating the derivation.
    """
    import ast  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    import re  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    try:
        fetch_tds = harvest.engine_scripts_dir() / "fetch_tds.py"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        pytest.skip(f"canonical engine not installed, so its constants cannot be read: {exc}")
    if not fetch_tds.is_file():
        pytest.skip(f"{fetch_tds} is missing")
    tree = ast.parse(fetch_tds.read_text(encoding="utf-8"))
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_http_download"),
        None,
    )
    assert fn is not None, "the engine no longer has `_http_download`; re-derive the ceiling"
    defaults = {
        arg.arg: value.value
        for arg, value in zip(fn.args.args[-len(fn.args.defaults) :], fn.args.defaults, strict=True)
        if isinstance(value, ast.Constant)
    }
    defaults.update(
        {
            arg.arg: value.value
            for arg, value in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True)
            if isinstance(value, ast.Constant)
        }
    )
    assert defaults.get("timeout") == harvest.ENGINE_READ_TIMEOUT_SECONDS, (
        f"the engine's per-read timeout is {defaults.get('timeout')}, not "
        f"{harvest.ENGINE_READ_TIMEOUT_SECONDS}; re-derive DEFAULT_DOWNLOAD_TIMEOUT"
    )
    assert defaults.get("max_attempts") == harvest.ENGINE_DOWNLOAD_ATTEMPTS, (
        f"the engine now makes {defaults.get('max_attempts')} attempts, not "
        f"{harvest.ENGINE_DOWNLOAD_ATTEMPTS}; re-derive DEFAULT_DOWNLOAD_TIMEOUT"
    )
    clamp = re.compile(rf"min\(\s*wait\s*,\s*{harvest.ENGINE_BACKOFF_CAP_SECONDS:g}(?:\.0)?\s*\)")
    assert clamp.search(fetch_tds.read_text(encoding="utf-8")), (
        "the engine's Retry-After clamp is no longer "
        f"{harvest.ENGINE_BACKOFF_CAP_SECONDS:g}s; re-derive the backoff term"
    )


def test_the_cli_warns_when_the_ceiling_undercuts_the_fetchers_retry_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Deriving the DEFAULT does not stop an operator re-creating the defect with a flag."""
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
    monkeypatch.setattr(harvest, "engine_scripts_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(harvest, "resolve_env", lambda path: {"TABLEAU_SERVER_URL": "https://example.invalid"})
    monkeypatch.setattr(harvest, "require", lambda env: None)
    monkeypatch.setattr(harvest, "download", landing_download)
    monkeypatch.setattr(harvest, "parse_asset", lambda path, scripts: ({"ok": True}, {"ok": True}))
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
            "600",  # the historical default, now knowably too small
        ],
    )
    with caplog.at_level("WARNING", logger="harvest_estate_assets"):
        assert harvest.main() == harvest.EXIT_OK
    warned = [r.getMessage() for r in caplog.records if "bounded retry budget" in r.getMessage()]
    assert warned, "an operator set a ceiling below the fetcher's own budget and was told nothing"
    assert "1380s bounded retry budget" in warned[0], warned[0]


def test_a_blind_child_reaches_its_OWN_verdict_before_our_ceiling_fires() -> None:
    """Finding 2 as behaviour, scaled from the real constants by one shared factor.

    The child stands in for a fetcher that spends its ENTIRE retry budget and then reports; the
    probe is unreadable throughout, which is the only situation where the ceiling is armed at all.
    The control repeats it with the historical 600s ceiling scaled identically — that one IS killed,
    which is what makes the first assertion evidence rather than a tautology.
    """
    scale = 1 / 200.0  # 1380s budget -> 6.9s, 1530s ceiling -> 7.65s
    child_budget = harvest.ENGINE_DOWNLOAD_BUDGET_SECONDS * scale
    blind = Counter(None)  # never readable: the unsupported-platform / denied-handle shape

    run = harvest.run_watched(
        sleeper(child_budget),
        env=None,
        timeout=harvest.DEFAULT_DOWNLOAD_TIMEOUT * scale,
        stall_timeout=harvest.DEFAULT_STALL_TIMEOUT * scale,
        probe=blind,
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert run.verdict == "", (
        f"killed at the ceiling after {run.elapsed:.2f}s, before the child's own "
        f"{child_budget:.2f}s (scaled) retry budget could produce a verdict: {run.detail}"
    )
    assert run.returncode == 0, "the child never reported for itself"

    killed = harvest.run_watched(
        sleeper(child_budget),
        env=None,
        timeout=600.0 * scale,  # the historical ceiling, scaled the same way
        stall_timeout=harvest.DEFAULT_STALL_TIMEOUT * scale,
        probe=Counter(None),
        poll_interval=0.05,
        heartbeat=1000.0,
    )
    assert killed.verdict == "ceiling", (
        "the control did not reproduce the defect, so the first assertion proves nothing about the ceiling"
    )


# --- the classification this module carries in the credential gate --------------------------------
#
# ⚠️ CI-red on #482, and the obvious diagnosis was the wrong one. `tests/test_diagnostic_redaction.py`
# records this module in `NON_HTTP_CREDENTIAL_SCRIPTS` — "holds a Tableau credential, makes NO
# credentialed HTTP request of its own" — and asserts the detector must NOT see it. That detector
# scans RAW SOURCE for markers, so a comment added by #482 that spelled `urlopen` immediately
# followed by an open paren reclassified this module as an HTTP client and failed two security gates.
# Registering it in `GATE_WAIVERS` would have turned both green while recording something false.
#
# So the two halves are pinned separately, and each says which one an edit broke: the STRUCTURAL
# claim (no HTTP client in code) is what the classification actually rests on; the PROSE trap is what
# breaks CI. `tests/test_diagnostic_redaction.py` remains the authority — this is a local mirror that
# fails first, with an actionable message, in the file whose change caused it.
_HTTP_CLIENT_MARKERS = (
    "urlopen(",
    "http.client",
    "requests.",
    "import tableauserverclient",
    "from tableauserverclient",
    "tableau_http",
    "._request(",
)


def executable_code(source: str) -> str:
    """`source` with every comment and string literal removed — what the module DOES, not what it says."""
    return "".join(
        "" if token.type in (tokenize.STRING, tokenize.COMMENT) else token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
    )


def test_this_module_still_makes_no_http_call_of_its_own() -> None:
    """The structural fact behind the classification: the engine child makes the request, we do not."""
    code = executable_code((REPO_ROOT / "scripts" / "harvest_estate_assets.py").read_text(encoding="utf-8"))
    found = sorted(marker for marker in _HTTP_CLIENT_MARKERS if marker in code)
    assert not found, (
        f"{found} now appear in this module's CODE, so it is a credentialed HTTP client and its "
        "`NON_HTTP_CREDENTIAL_SCRIPTS` entry in tests/test_diagnostic_redaction.py is false. Bring it "
        "under the taint gate (MODULES) — do NOT waive it."
    )
    # Positive control: an assertion over a marker list that stopped matching anything is not coverage.
    control = executable_code((REPO_ROOT / "scripts" / "tableau_http.py").read_text(encoding="utf-8"))
    assert any(marker in control for marker in _HTTP_CLIENT_MARKERS), (
        "the marker scan no longer recognises a module that IS an HTTP client, so it has gone inert"
    )


def test_the_credential_gate_is_not_tripped_by_this_module_s_PROSE() -> None:
    """#482's actual CI failure: a comment, not a call, put this module in front of a security gate."""
    source = (REPO_ROOT / "scripts" / "harvest_estate_assets.py").read_text(encoding="utf-8")
    prose_only = sorted(m for m in _HTTP_CLIENT_MARKERS if m in source and m not in executable_code(source))
    assert not prose_only, (
        f"{prose_only} appear only in comments or string literals here. That is still enough for the "
        "detector in tests/test_diagnostic_redaction.py to reclassify this module as a credentialed "
        "HTTP client and fail two security gates. Spell it apart, e.g. `urlopen` + `.read()`."
    )
