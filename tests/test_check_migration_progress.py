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


# --- the declaration ledger is evidence ABOUT drift, so it is loaded only when there IS drift ------


def _corrupt_both_declaration_locations(bundle: Path) -> None:
    """Invalid UTF-8 in the legacy ledger AND in the append-only directory."""
    legacy = bundle / "_build" / "generated-edit-declarations.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b'{"version": 1, "\xff\xfe": 1}')
    appended = bundle / "_build" / "generated-edit-declarations"
    appended.mkdir(parents=True, exist_ok=True)
    (appended / "record.json").write_bytes(b'{"version": 1, "\xff\xfe": 1}')


def test_a_pristine_bundle_stays_clean_when_the_declaration_ledger_is_undecodable(tmp_path):
    """⚠️ Regression guard for the delegation seam (blind review round 3 of PR #399).

    Extracting `adjudicate_generated_drift` moved the ledger load AHEAD of the drift computation, so
    a bundle whose generated artifacts were entirely pristine began depending on data that could not
    change its verdict: `CLEAN` became an uncaught `UnicodeDecodeError` and a CLI traceback exiting
    1 - the same code as a real `DRIFT`. Nothing about a clean bundle may consult the ledger.
    """
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    _write_manifest(tmp_path, {"M.SemanticModel/definition/tables/Orders.tmdl": cmp_mod.sha256_file(generated)})
    _corrupt_both_declaration_locations(tmp_path)

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "CLEAN"
    assert "pristine" in notes[0]
    assert cmp_mod.run_tamper_mode(tmp_path, as_json=False) == 0


def test_drift_with_an_unreadable_ledger_is_not_reported_as_undeclared_drift(tmp_path):
    """ "Cannot read the exonerating evidence" and "there is none" are different findings."""
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_manifest(tmp_path, {target: cmp_mod.sha256_file(generated)})
    generated.write_text("changed in place", encoding="utf-8")
    _corrupt_both_declaration_locations(tmp_path)

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "UNREADABLE_DECLARATIONS"
    assert "NOT the same as undeclared drift" in notes[0]
    code = cmp_mod.run_tamper_mode(tmp_path, as_json=False)
    assert code == 4
    assert code != 1, "must be distinguishable from a positively detected DRIFT"


def test_adjudicate_raises_a_named_error_rather_than_a_bare_decode_error(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_manifest(tmp_path, {target: cmp_mod.sha256_file(generated)})
    generated.write_text("changed in place", encoding="utf-8")
    _corrupt_both_declaration_locations(tmp_path)

    with pytest.raises(cmp_mod.UnreadableDeclarations):
        cmp_mod.adjudicate_generated_drift(tmp_path, cmp_mod.load_generated_artifact_baseline(tmp_path))


def test_adjudicating_a_pristine_bundle_never_touches_the_ledger(tmp_path, monkeypatch):
    """The ordering stated behaviourally, so it cannot regress into "works because it parses"."""
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    _write_manifest(tmp_path, {"M.SemanticModel/definition/tables/Orders.tmdl": cmp_mod.sha256_file(generated)})

    def explode(_bundle):
        raise AssertionError("the declaration ledger was read for a bundle with no drift")

    monkeypatch.setattr(cmp_mod, "load_generated_edit_declarations", explode)
    assert cmp_mod.adjudicate_generated_drift(tmp_path, cmp_mod.load_generated_artifact_baseline(tmp_path)) == []


def test_tamper_reads_append_only_declaration_files(tmp_path):
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    baseline_hash = cmp_mod.sha256_file(generated)
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_manifest(tmp_path, {target: baseline_hash})
    generated.write_text("changed by append-only declaration", encoding="utf-8")
    declaration_dir = tmp_path / "_build" / "generated-edit-declarations"
    declaration_dir.mkdir(parents=True)
    (declaration_dir / "one.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "run-1",
                "kind": "changed",
                "target": target,
                "baseline_sha256": baseline_hash,
                "expected_sha256": cmp_mod.sha256_file(generated),
                "script_identity": "_build/fix_orders_navigation.py",
                "script_sha256": "script-hash",
            }
        ),
        encoding="utf-8",
    )

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


@pytest.mark.timing
def test_declare_wrapper_concurrent_writers_keep_both_declarations(tmp_path):
    """Two wrapper processes released by one barrier must not erase each other's declaration."""
    first = Path("M.SemanticModel") / "definition" / "tables" / "Orders.tmdl"
    second = Path("M.SemanticModel") / "definition" / "tables" / "Customers.tmdl"
    first_file = _touch(tmp_path / first)
    second_file = _touch(tmp_path / second)
    _write_manifest(
        tmp_path,
        {
            first.as_posix(): cmp_mod.sha256_file(first_file),
            second.as_posix(): cmp_mod.sha256_file(second_file),
        },
    )
    go = tmp_path / "_build" / "go.signal"
    processes = []
    for target in (first, second):
        script = tmp_path / "_build" / f"fix_{target.stem.lower()}.py"
        ready = script.with_suffix(".ready")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "\n".join(
                [
                    "import sys, time",
                    "from pathlib import Path",
                    "root = Path.cwd()",
                    f"ready = root / {str(ready.relative_to(tmp_path)).replace(chr(92), '/').__repr__()}",
                    f"go = root / {str(go.relative_to(tmp_path)).replace(chr(92), '/').__repr__()}",
                    "ready.write_text('ready', encoding='utf-8')",
                    "deadline = time.monotonic() + 10",
                    "while not go.exists():",
                    "    if time.monotonic() > deadline:",
                    "        sys.exit(97)",
                    "    time.sleep(0.01)",
                    f"(root / {target.as_posix().__repr__()}).write_text('fixed {target.stem}', encoding='utf-8')",
                ]
            ),
            encoding="utf-8",
        )
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(DECLARE_SCRIPT),
                    "--bundle",
                    str(tmp_path),
                    "--target",
                    target.as_posix(),
                    "--script",
                    str(script),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    deadline = time.monotonic() + 10
    while len(list((tmp_path / "_build").glob("*.ready"))) < 2:
        assert time.monotonic() < deadline, "both declaration writers must reach the barrier"
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")
    completed = [process.communicate(timeout=15) + (process.returncode,) for process in processes]

    assert all(returncode == 0 for _stdout, _stderr, returncode in completed), completed
    declarations = cmp_mod.load_generated_edit_declarations(tmp_path)
    assert {declaration["target"] for declaration in declarations} == {first.as_posix(), second.as_posix()}
    state, notes = cmp_mod.tamper_check(tmp_path)
    assert state == "DECLARED_DRIFT"
    assert sum(1 for note in notes if note.startswith("DECLARED changed")) == 2

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


# --- issue #230: a --slice-only bundle must not read as tampered -------------------------------


def test_slice_only_shaped_manifest_is_NO_BASELINE_BY_DESIGN_not_NO_BASELINE(tmp_path):
    """A `--slice-only` bundle (pre-backfill) has a valid manifest with no `generated_artifacts` key.

    This is the exact shape `migrate_estate.py` itself writes (sort_keys=True, no generated_artifacts
    key at all) - `run_estate.py`'s wrapper is what adds that key, and `--slice-only` used to skip it
    entirely (issue #230). That must read as EXPECTED ABSENCE, not as a suspicious missing baseline.
    """
    _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    (tmp_path / "report.json").write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")
    (tmp_path / "input_manifest.json").write_text(
        json.dumps({"assets": [], "root": str(tmp_path), "source_kind": "folder"}, sort_keys=True),
        encoding="utf-8",
    )

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "NO_BASELINE_BY_DESIGN"
    assert "EXPECTED ABSENCE" in notes[0]
    assert "not tampering" in notes[0]


def test_missing_input_manifest_stays_NO_BASELINE_not_by_design(tmp_path):
    """No input_manifest.json at all is more suspicious than 'never had this key' - keep it distinct."""
    _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    (tmp_path / "report.json").write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "NO_BASELINE"
    assert "no input_manifest.json" in notes[0]


def test_corrupt_input_manifest_stays_NO_BASELINE_not_by_design(tmp_path):
    """Unparsable JSON must not crash the check, and must not read as 'expected absence' either."""
    _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    (tmp_path / "report.json").write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")
    (tmp_path / "input_manifest.json").write_text("{not valid json", encoding="utf-8")

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "NO_BASELINE"
    assert "not valid JSON" in notes[0]


def test_tamper_still_detects_drift_on_a_slice_only_backfilled_baseline(tmp_path):
    """The regression that matters most: a real DRIFT must not be softened by the new partial state."""
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    baseline_hash = cmp_mod.sha256_file(generated)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")
    (tmp_path / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "slice-run-1",
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "report_generated_at": "2026-08-10T08:00:00Z",
                    "report_sha256": cmp_mod.sha256_file(report),
                    "coverage": "slice_only_backfill",
                    "files": {"M.SemanticModel/definition/tables/Orders.tmdl": baseline_hash},
                }
            }
        ),
        encoding="utf-8",
    )
    generated.write_text("changed after the slice-only backfill", encoding="utf-8")

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "DRIFT"
    assert any("UNDECLARED" in note for note in notes)
    assert any("PARTIAL COVERAGE" in note for note in notes)


def test_tamper_flags_partial_coverage_on_a_clean_slice_only_backfilled_baseline(tmp_path):
    """A pass drawn from a backfilled baseline must still say its coverage is partial."""
    generated = _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")
    (tmp_path / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "slice-run-2",
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "report_generated_at": "2026-08-10T08:00:00Z",
                    "report_sha256": cmp_mod.sha256_file(report),
                    "coverage": "slice_only_backfill",
                    "files": {"M.SemanticModel/definition/tables/Orders.tmdl": cmp_mod.sha256_file(generated)},
                }
            }
        ),
        encoding="utf-8",
    )

    state, notes = cmp_mod.tamper_check(tmp_path)

    assert state == "CLEAN"
    assert any("PARTIAL COVERAGE" in note for note in notes)


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


def test_tamper_cli_exit_code_for_a_slice_only_shaped_bundle_is_distinct_from_no_baseline(tmp_path):
    """The process-level contract for issue #230: 3, not 2 - a caller gating on exit code must see it."""
    _touch(tmp_path / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl")
    (tmp_path / "report.json").write_text(json.dumps({"generated_at": "2026-08-10T08:00:00Z"}), encoding="utf-8")
    (tmp_path / "input_manifest.json").write_text(json.dumps({"assets": []}), encoding="utf-8")

    proc = _run("--bundle", str(tmp_path), "--tamper", "--json")

    assert proc.returncode == 3
    assert json.loads(proc.stdout)["state"] == "NO_BASELINE_BY_DESIGN"


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


# --- the CANONICAL location: both modes must look at the phase-2 packages, not only the bundle ---
#
# Blind-review round-4 finding B1. #460 was settled by `promote_unit.py` - the package's `fabric/`
# is where an agent edits and where phase 3 ships FROM - but both modes of this gate still inspected
# only the bundle. Measured on a package whose model had just been edited: `--tamper` reported
# `CLEAN  "3 generated artifact(s) are pristine against their engine-run hashes"` at exit 0, and
# progress reported `SILENT  deliverable_count_seen=0`.


def _run_layout(tmp_path: Path, *, edited: bool, minutes_ago: float = 0) -> tuple[Path, Path]:
    """`(bundle, package)` in the canonical `_runs/<NNN>-<slug>/{bundle,packages}` shape.

    The package carries its own `package-manifest.json` digest, which is the authority on whether it
    has been edited - the same one `package_unit.package_edits` reads, and the same one
    `package_unit` itself refuses to repackage over. The BUNDLE is given a matching engine-run
    baseline so it is genuinely pristine: the whole point of the finding is a true "the bundle is
    clean" note standing in for a verdict about a tree nobody looked at.
    """
    bundle = tmp_path / "run" / "bundle"
    package = tmp_path / "run" / "packages" / "batch" / "Book"
    model = package / "fabric" / "Book.SemanticModel" / "definition"
    _touch(bundle / "pbip" / "Book" / "Book.Report" / "definition" / "pages" / "pages.json", minutes_ago)
    _touch(model / "tables" / "Imported0.tmdl", minutes_ago)
    contents = {
        path.relative_to(package).as_posix(): cmp_mod.sha256_file(path)
        for path in sorted(package.rglob("*"))
        if path.is_file()
    }
    if edited:
        (model / "tables" / "Imported0.tmdl").write_text("edited by the agent\n", encoding="utf-8")
    (package / "package-manifest.json").write_text(
        json.dumps({"unit": "Book", "contents": {"files": contents}}), encoding="utf-8"
    )
    _write_manifest(
        bundle,
        {
            path.relative_to(bundle).as_posix(): cmp_mod.sha256_file(path)
            for path in sorted(bundle.rglob("*"))
            if cmp_mod._is_generated_artifact(path, bundle)  # noqa: SLF001  # pylint: disable=protected-access
        },
    )
    return bundle, package


def test_an_edit_to_the_CANONICAL_package_is_no_longer_reported_as_clean(tmp_path):
    """Reproduced: `exit=0  state=CLEAN  "3 generated artifact(s) are pristine"`.

    The bundle really IS pristine - that note is true and always was. What made the verdict wrong is
    that it was the ONLY tree looked at, so a gate whose whole job is "did a generated artifact
    change without evidence" answered about the copy nobody was working in.
    """
    bundle, _package = _run_layout(tmp_path, edited=True)
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "PACKAGE_DRIFT"
    assert any("PACKAGE EDITED: Book" in note for note in notes)
    assert cmp_mod.run_tamper_mode(bundle, as_json=False) == 1


def test_an_UNEDITED_package_still_reports_clean(tmp_path):
    """The control: looking at a second tree must not make every migration fail the gate."""
    bundle, _package = _run_layout(tmp_path, edited=False)
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "CLEAN"
    assert any("phase-2 package(s) still match" in note for note in notes)
    assert cmp_mod.run_tamper_mode(bundle, as_json=False) == 0


def test_no_packages_at_all_is_a_NAMED_state_not_a_silent_pass(tmp_path):
    """ "No packages" and "packages all pristine" must not print the same thing.

    The same doctrine `_no_baseline_verdict` applies one layer up: a silent absence reads as a clean
    bill of health for a tree nobody looked at. A run that has not reached phase 2 is legitimate -
    and it has to SAY so.
    """
    bundle = tmp_path / "run" / "bundle"
    _touch(bundle / "pbip" / "Book" / "Book.Report" / "definition" / "pages" / "pages.json")
    _write_manifest(
        bundle,
        {
            path.relative_to(bundle).as_posix(): cmp_mod.sha256_file(path)
            for path in sorted(bundle.rglob("*"))
            if cmp_mod._is_generated_artifact(path, bundle)  # noqa: SLF001  # pylint: disable=protected-access
        },
    )
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "CLEAN"
    assert any("no phase-2 packages found" in note for note in notes)


def test_a_package_carrying_no_digest_cannot_be_assessed_rather_than_passing(tmp_path):
    """A package with no recorded digest is not "unedited" - it is unassessable, and says so.

    Its own exit code (5), because "I cannot tell" and "it was edited" route to different responses:
    one is a question about the package's provenance, the other is a finding about its content.
    """
    bundle, package = _run_layout(tmp_path, edited=False)
    (package / "package-manifest.json").write_text(json.dumps({"unit": "Book"}), encoding="utf-8")
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "PACKAGE_UNASSESSABLE"
    assert any("PACKAGE UNASSESSABLE" in note for note in notes)
    assert cmp_mod.run_tamper_mode(bundle, as_json=False) == 5


def test_a_bundle_level_finding_is_not_masked_by_a_package_finding(tmp_path):
    """Precedence: the bundle verdict is about the baseline everything else is measured from.

    Both trees are in trouble here on purpose - the package carries an edit AND the bundle has no
    usable baseline. Returning `PACKAGE_DRIFT` would tell a caller to go look at the package while
    the more fundamental problem (nothing can be adjudicated at all) went unreported. The package
    NOTES still ship either way, so nothing is lost by the ordering - only the headline changes.
    """
    bundle, _package = _run_layout(tmp_path, edited=True)
    (bundle / "input_manifest.json").unlink()
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "NO_BASELINE"
    assert any("PACKAGE EDITED: Book" in note for note in notes), "the package finding was dropped, not deferred"


def test_progress_counts_work_done_in_the_CANONICAL_package(tmp_path):
    """Reproduced: `state=SILENT  deliverable_count_seen=0` while the agent was working.

    The bundle here is deliberately OLD and the package fresh, which is exactly the shape of a unit
    that has been packaged and handed to an agent: nothing more is written to `pbip/` after phase 2.
    Scanning only the bundle therefore sees ZERO deliverables in the window, and an orchestrator
    reads that as a dead subagent.

    The assertion is on the deliverable COUNT rather than on the exact quiet state, because which
    quiet state a bundle-only scan lands in (`SILENT` vs `STALLED` vs `THINKING`) depends on what
    else happens to be in the tree - and the finding is about the count that every one of them is
    computed from.
    """
    bundle, package = _run_layout(tmp_path, edited=False, minutes_ago=180)
    for path in package.rglob("*"):
        if path.is_file():
            _touch(path, minutes_ago=1)

    since = datetime.now() - timedelta(minutes=30)
    bundle_only = cmp_mod.scan(bundle, since)
    with_packages = cmp_mod.scan(bundle, since, None, [package])

    assert bundle_only["buckets"]["deliverable"]["count"] == 0, "the fixture must reproduce the blind window"
    assert cmp_mod.verdict(bundle_only, 30)[0] != "PROGRESSING"
    assert with_packages["buckets"]["deliverable"]["count"] > 0
    assert cmp_mod.verdict(with_packages, 30)[0] == "PROGRESSING"
    assert with_packages["packages_scanned"] == [str(package)]


def test_package_discovery_finds_the_canonical_layout_and_nothing_else(tmp_path):
    """A package is identified by carrying its own manifest, never by its position in the tree."""
    bundle, package = _run_layout(tmp_path, edited=False)
    assert cmp_mod.discover_package_roots(bundle) == [package]

    # A sibling directory that merely looks like a batch, with no manifest, is not a package.
    (tmp_path / "run" / "packages" / "batch" / "NotAPackage").mkdir(parents=True, exist_ok=True)
    assert cmp_mod.discover_package_roots(bundle) == [package]

    # --packages OVERRIDES the search rather than adding to it, so a caller with an odd layout does
    # not silently also pick up the canonical one.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "Other").mkdir(parents=True, exist_ok=True)
    (elsewhere / "Other" / "package-manifest.json").write_text("{}", encoding="utf-8")
    assert cmp_mod.discover_package_roots(bundle, elsewhere) == [elsewhere / "Other"]


def test_the_tamper_cli_exit_map_has_no_duplicate_meaning(tmp_path):
    """Every tamper state maps to a code, and the two new ones do not collide with the five old."""
    bundle, package = _run_layout(tmp_path, edited=True)
    proc = _run("--bundle", str(bundle), "--tamper", "--json")
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["state"] == "PACKAGE_DRIFT"
    assert any("PACKAGE EDITED" in note for note in payload["notes"])
    assert package.is_dir()
