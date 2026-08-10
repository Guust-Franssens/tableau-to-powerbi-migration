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
DECLARE_SCRIPT = REPO / "scripts" / "declare_generated_edit.py"
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


def _write_manifest(bundle: Path, files: dict[str, str]) -> None:
    report = bundle / "report.json"
    if not report.is_file():
        report.write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")
    (bundle / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "run-1",
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "report_generated_at": "2026-08-10T08:00:00Z",
                    "report_sha256": cmp_mod.sha256_file(report),
                    "files": files,
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_declaration(
    bundle: Path,
    target: str,
    baseline_sha256: str | None,
    expected_sha256: str | None,
    kind: str = "changed",
) -> None:
    path = bundle / "_build" / "generated-edit-declarations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "declarations": [
                    {
                        "version": 1,
                        "run_id": "run-1",
                        "kind": kind,
                        "target": target,
                        "baseline_sha256": baseline_sha256,
                        "expected_sha256": expected_sha256,
                        "script_identity": "_build/fix_orders_navigation.py",
                        "script_sha256": "script-hash",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _scan_now(bundle: Path, window_minutes: int = 30):
    since = datetime.now() - timedelta(minutes=window_minutes)
    return cmp_mod.verdict(cmp_mod.scan(bundle, since), window_minutes)


# --- the discrimination the tool exists for ------------------------------------------------------


def test_writing_deliverables_is_PROGRESSING(tmp_path):
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "tables" / "T.tmdl")
    assert _scan_now(tmp_path)[0] == "PROGRESSING"


def test_baseline_excludes_dispatcher_setup_from_PROGRESSING(tmp_path):
    """Files written before delegation are setup artifacts, not subagent progress."""
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "tables" / "T.tmdl", minutes_ago=10)
    baseline = datetime.now() - timedelta(minutes=5)
    since = datetime.now() - timedelta(minutes=15)

    state, detail = cmp_mod.verdict(cmp_mod.scan(tmp_path, since, baseline), window_minutes=15)

    assert state == "SILENT"
    assert "has this run started" in detail


def test_baseline_shortens_the_observed_window_in_the_verdict(tmp_path):
    """A 15m configured window with a 5m baseline has only observed 5m of agent work."""
    _touch(tmp_path / "_work" / "probe.py")
    baseline = datetime.now() - timedelta(minutes=5)
    since = datetime.now() - timedelta(minutes=15)

    state, detail = cmp_mod.verdict(cmp_mod.scan(tmp_path, since, baseline), window_minutes=15)

    assert state == "THINKING"
    assert "5m observed after baseline" in detail


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


def test_LIVENESS_MUST_NOT_RESCUE_scratch_only_across_a_full_window(tmp_path):
    """A rising tool-call count proves activity, not deliverable progress."""
    for i in range(5):
        _touch(tmp_path / "_work" / f"probe{i}.py", minutes_ago=i)
    since = datetime.now() - timedelta(minutes=30)

    state, detail = cmp_mod.verdict(cmp_mod.scan(tmp_path, since), window_minutes=30, liveness="active")

    assert state == "STALLED"
    assert "ZERO deliverables" in detail


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


def test_runtime_liveness_keeps_read_heavy_phase_from_SILENT(tmp_path):
    """A rising tool-call count is external evidence that a quiet bundle is still being worked."""
    (tmp_path / "bundle").mkdir()
    scanned = cmp_mod.scan(tmp_path, datetime.now() - timedelta(minutes=30))

    state, detail = cmp_mod.verdict(scanned, window_minutes=30, liveness="active")

    assert state == "THINKING"
    assert "runtime liveness signal is active" in detail


def test_prior_deliverables_outside_fixed_window_report_THINKING_not_SILENT(tmp_path):
    """Burst writers can be healthy even when the polling window lands between output bursts."""
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "tables" / "T.tmdl", minutes_ago=20)
    since = datetime.now() - timedelta(minutes=15)

    state, detail = cmp_mod.verdict(cmp_mod.scan(tmp_path, since), window_minutes=15)

    assert state == "THINKING"
    assert "write in bursts" in detail


def test_ancient_deliverables_do_not_create_unbounded_THINKING(tmp_path):
    """Historical output is useful context, not an indefinite all-clear."""
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "tables" / "T.tmdl", minutes_ago=7 * 24 * 60)
    since = datetime.now() - timedelta(minutes=15)

    state, detail = cmp_mod.verdict(cmp_mod.scan(tmp_path, since), window_minutes=15)

    assert state == "SILENT"
    assert "last deliverable" in detail


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


def test_underscored_scratch_directory_is_scratch_not_deliverable():
    """`_scratch` is the same intent as `scratch`, even when it contains a PBIP-shaped sandbox."""
    assert cmp_mod.classify(Path("_scratch/orderprobe/run0/Probe.pbip")) == "scratch"


def test_temp_component_does_not_match_template():
    """Scratch matching stays component-normalized, not substring-based."""
    assert cmp_mod.classify(Path("template/Model.SemanticModel/definition/tables/T.tmdl")) == "deliverable"


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


# --- generated artifact drift ---------------------------------------------------------------------


def test_tamper_detects_an_undeclared_generated_artifact_edit(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    _write_manifest(tmp_path, {"M.SemanticModel/definition/tables/Orders.tmdl": cmp_mod.sha256_file(generated)})
    generated.write_text("changed in place", encoding="utf-8")

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "DRIFT"
    assert "UNDECLARED" in notes[0]
    assert "Orders.tmdl" in notes[0]


def test_tamper_allows_a_generated_edit_declared_by_a_fix_script(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    baseline_hash = cmp_mod.sha256_file(generated)
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_manifest(tmp_path, {target: baseline_hash})
    generated.write_text("changed by replayable fix", encoding="utf-8")
    _write_declaration(tmp_path, target, baseline_hash, cmp_mod.sha256_file(generated))

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "DECLARED_DRIFT"
    assert any("fix_orders_navigation.py" in note for note in notes)


def test_tamper_rejects_a_source_comment_without_structured_hash_evidence(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_manifest(tmp_path, {target: cmp_mod.sha256_file(generated)})
    generated.write_text("changed in place", encoding="utf-8")
    fix = tmp_path / "_build" / "fix_orders_navigation.py"
    fix.parent.mkdir(parents=True, exist_ok=True)
    fix.write_text(f'"""Mentions {target}, but declares no output hash."""\n', encoding="utf-8")

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "DRIFT"
    assert "UNDECLARED" in notes[0]


def test_tamper_rejects_a_structured_declaration_with_the_wrong_output_hash(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    baseline_hash = cmp_mod.sha256_file(generated)
    _write_manifest(tmp_path, {target: baseline_hash})
    generated.write_text("changed in place", encoding="utf-8")
    _write_declaration(tmp_path, target, baseline_hash, "not-the-current-hash")

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "DRIFT"
    assert "UNDECLARED" in notes[0]


def test_declare_wrapper_records_a_composed_path_fix_script(tmp_path):
    target = Path("M.SemanticModel") / "definition" / "tables" / "Orders.tmdl"
    generated = _touch(tmp_path / target, minutes_ago=1)
    _write_manifest(tmp_path, {target.as_posix(): cmp_mod.sha256_file(generated)})
    fix = tmp_path / "_build" / "fix_post_engine.py"
    fix.parent.mkdir(parents=True, exist_ok=True)
    fix.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "root = Path.cwd()",
                "target = root / 'M.SemanticModel' / 'definition' / 'tables' / 'Orders.tmdl'",
                "target.write_text('fixed by composed path', encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(DECLARE_SCRIPT),
            "--bundle",
            str(tmp_path),
            "--target",
            target.as_posix(),
            "--script",
            str(fix),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    state, notes = cmp_mod.tamper_check(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert state == "DECLARED_DRIFT"
    assert any("fix_post_engine.py" in note for note in notes)


def test_tamper_rejects_a_manifest_without_current_run_identity(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    (tmp_path / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 999,
                    "recorded_at": "1999-01-01T00:00:00",
                    "files": {"M.SemanticModel/definition/tables/Orders.tmdl": cmp_mod.sha256_file(generated)},
                }
            }
        ),
        encoding="utf-8",
    )

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "NO_BASELINE"
    assert "baseline" in notes[0]


def test_tamper_rejects_a_manifest_bound_to_a_different_report(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    (tmp_path / "report.json").write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")
    (tmp_path / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "foreign-run",
                    "recorded_at": "1999-01-01T00:00:00",
                    "report_generated_at": "2026-08-10T08:00:00Z",
                    "report_sha256": "definitely-not-the-current-report",
                    "files": {"M.SemanticModel/definition/tables/Orders.tmdl": cmp_mod.sha256_file(generated)},
                }
            }
        ),
        encoding="utf-8",
    )

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "NO_BASELINE"
    assert "baseline" in notes[0]


def test_tamper_ignores_refresh_cache_and_desktop_sidecars(tmp_path):
    """The false-positive guard: a normal refresh/autosave must not train people to bypass the check."""
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    _write_manifest(tmp_path, {"M.SemanticModel/definition/tables/Orders.tmdl": cmp_mod.sha256_file(generated)})
    _touch(tmp_path / "M.SemanticModel" / ".pbi" / "cache.abf")
    _touch(tmp_path / "M.SemanticModel" / ".pbi" / "localSettings.json")
    _touch(tmp_path / "R.Report" / ".pbi" / "localSettings.json")

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "CLEAN"
    assert "pristine" in notes[0]


# --- the CLI contract -----------------------------------------------------------------------------


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def _baseline_arg(minutes_ago: int = 60) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def test_exit_codes_let_a_caller_GATE_on_the_result(tmp_path):
    """The orchestrator runs this before assigning the report phase, so the code must be usable."""
    _touch(tmp_path / "_work" / "p.py")
    baseline = _baseline_arg()
    assert _run("--bundle", str(tmp_path), "--since-minutes", "30", "--baseline", baseline, "--json").returncode == 1
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "t.tmdl")
    assert _run("--bundle", str(tmp_path), "--since-minutes", "30", "--baseline", baseline, "--json").returncode == 0


def test_json_output_is_machine_readable(tmp_path):
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "t.tmdl")
    payload = json.loads(_run("--bundle", str(tmp_path), "--baseline", _baseline_arg(), "--json").stdout)
    assert payload["state"] == "PROGRESSING"
    assert payload["buckets"]["deliverable"]["count"] == 1


def test_progress_mode_fails_closed_without_a_baseline(tmp_path):
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "t.tmdl")
    proc = _run("--bundle", str(tmp_path), "--json")
    assert proc.returncode == 2
    assert "--baseline" in proc.stderr + proc.stdout


def test_modes_are_mutually_exclusive(tmp_path):
    _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    proc = _run("--bundle", str(tmp_path), "--handoff", "--tamper")
    assert proc.returncode == 2
    assert "choose only one mode" in proc.stderr + proc.stdout


def test_a_missing_bundle_is_reported_not_crashed():
    proc = _run("--bundle", "no-such-dir-anywhere")
    assert proc.returncode == 2
    assert "no such bundle" in proc.stderr.lower() + proc.stdout.lower()


def test_STALLED_output_says_ASK_rather_than_kill(tmp_path):
    """The verdict must route to a question. Killing a slow-but-productive run is the worse error."""
    _touch(tmp_path / "_work" / "p.py")
    out = _run("--bundle", str(tmp_path), "--since-minutes", "30", "--baseline", _baseline_arg())
    combined = out.stdout + out.stderr
    assert "ASK IT WHAT IT IS BLOCKED ON" in combined
    assert "Do NOT kill it" in combined
