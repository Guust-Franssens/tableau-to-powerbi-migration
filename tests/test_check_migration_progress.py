"""Tests for scripts/check_migration_progress.py.

The tool exists because of one measured event: four migrations ran in parallel and two passed 100
minutes on their first turn. Elapsed time could not tell them apart - one had written 27 model and
148 report files (30 stubbed calcs is genuinely slow work), the other had written **zero** report
files in 105 minutes while accumulating scratch. So the tests are weighted toward that
discrimination, and toward the two ways it was got wrong on the way here.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_migration_progress.py"
spec = importlib.util.spec_from_file_location("check_migration_progress", SCRIPT)
cmp_mod = importlib.util.module_from_spec(spec)
sys.modules["check_migration_progress"] = cmp_mod
spec.loader.exec_module(cmp_mod)


def _touch(path: Path, minutes_ago: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if minutes_ago:
        when = time.time() - minutes_ago * 60
        os.utime(path, (when, when))
    return path


def _scan_now(bundle: Path, window_minutes: int = 30):
    since = datetime.now() - timedelta(minutes=window_minutes)
    return cmp_mod.verdict(cmp_mod.scan(bundle, since), window_minutes)


# --- the discrimination the tool exists for ------------------------------------------------------


def test_writing_deliverables_is_PROGRESSING(tmp_path):
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "tables" / "T.tmdl")
    assert _scan_now(tmp_path)[0] == "PROGRESSING"


def test_scratch_only_across_a_full_window_is_STALLED(tmp_path):
    """The 105-minute run: busy, and producing nothing the user asked for."""
    for i in range(5):
        _touch(tmp_path / "_work" / f"probe{i}.py", minutes_ago=i)
    state, detail = _scan_now(tmp_path, window_minutes=30)
    assert state == "STALLED"
    assert "ZERO deliverables" in detail


def test_RECENCY_MUST_NOT_RESCUE_a_window_with_no_deliverables(tmp_path):
    """The bug this tool shipped with, caught by running it against the live run it was written for.

    The first version asked "was the last write < 180s ago?" and answered THINKING if so. An agent
    touching a scratch file every 30 seconds for 105 minutes therefore read THINKING forever - the
    precise case the tool exists to catch. Recency may only separate STALLED from SILENT; the WINDOW
    decides whether an absence of output is meaningful.
    """
    _touch(tmp_path / "_work" / "just_now.py", minutes_ago=0)
    _touch(tmp_path / "_work" / "older.py", minutes_ago=25)
    assert _scan_now(tmp_path, window_minutes=30)[0] == "STALLED"


def test_a_short_window_refuses_to_judge(tmp_path):
    """A Desktop load is ~90s and a refresh ~93s, so a short window cannot tell loading from stuck."""
    _touch(tmp_path / "_work" / "probe.py")
    state, detail = _scan_now(tmp_path, window_minutes=5)
    assert state == "THINKING"
    assert "too short to judge" in detail


def test_nothing_at_all_is_SILENT_not_STALLED(tmp_path):
    """SILENT must stay distinct: a correct early STOP produces almost no artifacts and is RIGHT."""
    (tmp_path / "empty").mkdir()
    assert _scan_now(tmp_path)[0] == "SILENT"


def test_stale_activity_outside_the_window_is_SILENT(tmp_path):
    _touch(tmp_path / "_work" / "old.py", minutes_ago=120)
    state, detail = _scan_now(tmp_path, window_minutes=30)
    assert state == "SILENT"
    assert "credential" in detail, "must remind the reader that a blocked run looks like a dead one"


# --- bucketing ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("pbip/Model.SemanticModel/definition/tables/T.tmdl", "deliverable"),
        ("pbip/R.Report/definition/pages/p/visuals/v/visual.json", "deliverable"),
        ("out/Thing.pbip", "deliverable"),
        ("_work/probe.py", "scratch"),
        ("scratch/oracle.json", "scratch"),
        ("_build/q1.dax", "scratch"),
        ("_probe/Probe.pbip", "scratch"),
        ("report.json", "other"),
    ],
)
def test_files_land_in_the_right_bucket(relative, expected):
    assert cmp_mod.classify(Path(relative)) == expected


def test_a_probe_sandbox_inside_a_bundle_is_scratch_not_deliverable():
    """`_probe/Probe.pbip` is a reachability sandbox. Counting it as output would mask a stall."""
    assert cmp_mod.classify(Path("mig/_probe/Probe.pbip")) == "scratch"


# --- the handoff gate -----------------------------------------------------------------------------


def test_a_model_with_no_cache_is_NOT_READY(tmp_path):
    """The report builder would open an empty model and probably trigger its own refresh."""
    _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "T.tmdl")
    state, notes = cmp_mod.handoff_ready(tmp_path)
    assert state == "NOT_READY"
    assert "NO cache.abf" in notes[0]


def test_a_cache_OLDER_than_the_tmdl_is_NOT_READY(tmp_path):
    """The dangerous one: something loads, so nothing looks wrong."""
    model = tmp_path / "M.SemanticModel"
    _touch(model / ".pbi" / "cache.abf", minutes_ago=30)
    _touch(model / "definition" / "tables" / "T.tmdl", minutes_ago=5)
    state, notes = cmp_mod.handoff_ready(tmp_path)
    assert state == "NOT_READY"
    assert "STALE" in notes[0]


def test_a_cache_that_postdates_the_tmdl_is_READY(tmp_path):
    model = tmp_path / "M.SemanticModel"
    _touch(model / "definition" / "tables" / "T.tmdl", minutes_ago=10)
    _touch(model / ".pbi" / "cache.abf", minutes_ago=1)
    assert cmp_mod.handoff_ready(tmp_path)[0] == "READY"


def test_every_model_in_the_bundle_must_pass(tmp_path):
    """One bundle held TWO copies of a model and only one was warm - the other opens empty."""
    warm = tmp_path / "tier2" / "M.SemanticModel"
    _touch(warm / "definition" / "tables" / "T.tmdl", minutes_ago=10)
    _touch(warm / ".pbi" / "cache.abf", minutes_ago=1)
    cold = tmp_path / "out" / "M.SemanticModel"
    _touch(cold / "definition" / "tables" / "T.tmdl", minutes_ago=10)
    assert cmp_mod.handoff_ready(tmp_path)[0] == "NOT_READY"


def test_no_model_at_all_is_its_own_state(tmp_path):
    (tmp_path / "reports").mkdir()
    assert cmp_mod.handoff_ready(tmp_path)[0] == "NO_MODEL"


# --- the CLI contract -----------------------------------------------------------------------------


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def test_exit_codes_let_a_caller_GATE_on_the_result(tmp_path):
    """The orchestrator runs this before assigning the report phase, so the code must be usable."""
    _touch(tmp_path / "_work" / "p.py")
    assert _run("--bundle", str(tmp_path), "--since-minutes", "30", "--json").returncode == 1
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "t.tmdl")
    assert _run("--bundle", str(tmp_path), "--since-minutes", "30", "--json").returncode == 0


def test_json_output_is_machine_readable(tmp_path):
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "t.tmdl")
    payload = json.loads(_run("--bundle", str(tmp_path), "--json").stdout)
    assert payload["state"] == "PROGRESSING"
    assert payload["buckets"]["deliverable"]["count"] == 1


def test_a_missing_bundle_is_reported_not_crashed():
    proc = _run("--bundle", "no-such-dir-anywhere")
    assert proc.returncode == 2
    assert "no such bundle" in proc.stderr.lower() + proc.stdout.lower()


def test_STALLED_output_says_ASK_rather_than_kill(tmp_path):
    """The verdict must route to a question. Killing a slow-but-productive run is the worse error."""
    _touch(tmp_path / "_work" / "p.py")
    out = _run("--bundle", str(tmp_path), "--since-minutes", "30")
    combined = out.stdout + out.stderr
    assert "ASK IT WHAT IT IS BLOCKED ON" in combined
    assert "Do NOT kill it" in combined
