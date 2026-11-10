"""Tests for scripts/check_migration_progress.py.

Measured event: of two parallel migrations past 100 minutes, one had written 27 model and 148 report
files and the other ZERO report files in 105 minutes while accumulating scratch. The tests weight
that discrimination and the two ways it was first got wrong (recency and liveness rescuing a stall).
"""

import importlib.util
import json
import logging
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

ORDERS = "M.SemanticModel/definition/tables/Orders.tmdl"
REPORT_AT = "2026-08-10T08:00:00Z"


def _touch(path: Path, minutes_ago: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if minutes_ago:
        when = time.time() - minutes_ago * 60
        os.utime(path, (when, when))
    return path


def _generated(bundle: Path, **extra) -> dict:
    report = bundle / "report.json"
    if not report.is_file():
        report.write_text(json.dumps({"generated_at": REPORT_AT}), encoding="utf-8")
    return {
        "version": 1,
        "run_id": "run-1",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "report_generated_at": REPORT_AT,
        "report_sha256": cmp_mod.sha256_file(report),
        **extra,
    }


def _write_manifest(bundle: Path, files: dict[str, str], **extra) -> None:
    payload = {"generated_artifacts": _generated(bundle, files=files, **extra)}
    (bundle / "input_manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _declaration(target: str, baseline_sha256: str | None, expected_sha256: str | None) -> dict:
    return {
        "version": 1,
        "run_id": "run-1",
        "kind": "changed",
        "target": target,
        "baseline_sha256": baseline_sha256,
        "expected_sha256": expected_sha256,
        "script_identity": "_build/fix_orders_navigation.py",
        "script_sha256": "script-hash",
    }


def _write_declaration(bundle: Path, target: str, baseline: str | None, expected: str | None) -> None:
    path = bundle / "_build" / "generated-edit-declarations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "declarations": [_declaration(target, baseline, expected)]}), encoding="utf-8"
    )


def _drifted(bundle: Path) -> tuple[Path, str]:
    """A pristine Orders.tmdl with a baseline, then edited in place. Returns (file, baseline hash)."""
    generated = _touch(bundle / ORDERS)
    baseline = cmp_mod.sha256_file(generated)
    _write_manifest(bundle, {ORDERS: baseline})
    generated.write_text("changed in place", encoding="utf-8")
    return generated, baseline


def _scan(bundle: Path, window: int = 30, baseline_minutes: float | None = None, liveness=None):
    since = datetime.now() - timedelta(minutes=window)
    baseline = datetime.now() - timedelta(minutes=baseline_minutes) if baseline_minutes else None
    kwargs = {"liveness": liveness} if liveness else {}
    return cmp_mod.verdict(cmp_mod.scan(bundle, since, baseline), window, **kwargs)


DELIVERABLE = "pbip/M.SemanticModel/definition/tables/T.tmdl"

# (name, [(relative path, minutes ago)], window, baseline minutes, liveness, state, detail needle)
VERDICT_CASES = [
    ("deliverable-written", [(DELIVERABLE, 0)], 30, None, None, "PROGRESSING", None),
    ("setup-before-baseline", [(DELIVERABLE, 10)], 15, 5, None, "SILENT", "has this run started"),
    ("baseline-shortens-window", [("_work/probe.py", 0)], 15, 5, None, "THINKING", "5m observed after baseline"),
    (
        "scratch-only-full-window",
        [(f"_work/p{i}.py", i) for i in range(5)],
        30,
        None,
        None,
        "STALLED",
        "ZERO deliverables",
    ),
    ("recency-must-not-rescue", [("_work/now.py", 0), ("_work/old.py", 25)], 30, None, None, "STALLED", None),
    (
        "liveness-must-not-rescue",
        [(f"_work/p{i}.py", i) for i in range(5)],
        30,
        None,
        "active",
        "STALLED",
        "ZERO deliverables",
    ),
    ("short-window-refuses", [("_work/probe.py", 0)], 5, None, None, "THINKING", "too short to judge"),
    ("nothing-at-all", [], 30, None, None, "SILENT", None),
    ("stale-activity", [("_work/old.py", 120)], 30, None, None, "SILENT", "credential"),
    ("liveness-keeps-quiet-alive", [], 30, None, "active", "THINKING", "runtime liveness signal is active"),
    ("burst-writer", [(DELIVERABLE, 20)], 15, None, None, "THINKING", "write in bursts"),
    ("ancient-deliverables", [(DELIVERABLE, 7 * 24 * 60)], 15, None, None, "SILENT", "last deliverable"),
]


def test_progress_verdict_ladder(tmp_path):
    """Only deliverables in the window are PROGRESSING; recency and liveness never rescue a stall."""
    wrong = []
    for name, files, window, baseline, liveness, expected, needle in VERDICT_CASES:
        bundle = tmp_path / name
        bundle.mkdir()
        for relative, ago in files:
            _touch(bundle / relative, minutes_ago=ago)
        state, detail = _scan(bundle, window, baseline, liveness)
        if state != expected or (needle and needle not in detail):
            wrong.append((name, state, detail))
    assert not wrong


def test_files_land_in_the_right_bucket():
    """Scratch matching is component-normalized: `_probe`/`_scratch` sandboxes are scratch, `template` is not."""
    cases = {
        "pbip/Model.SemanticModel/definition/tables/T.tmdl": "deliverable",
        "pbip/R.Report/definition/pages/p/visuals/v/visual.json": "deliverable",
        "out/Thing.pbip": "deliverable",
        "template/Model.SemanticModel/definition/tables/T.tmdl": "deliverable",
        "_work/probe.py": "scratch",
        "scratch/oracle.json": "scratch",
        "_build/q1.dax": "scratch",
        "mig/_probe/Probe.pbip": "scratch",
        "_scratch/orderprobe/run0/Probe.pbip": "scratch",
        "report.json": "other",
    }
    assert {p: cmp_mod.classify(Path(p)) for p in cases} == cases


def test_handoff_requires_every_model_to_have_a_fresh_cache(tmp_path):
    """No cache, a cache OLDER than the TMDL, or one cold copy among two all block the handoff."""
    tables = Path("definition") / "tables" / "T.tmdl"
    cache = Path(".pbi") / "cache.abf"
    _touch(tmp_path / "none" / "M.SemanticModel" / tables)
    state, notes = cmp_mod.handoff_ready(tmp_path / "none")
    assert state == "NOT_READY" and "NO cache.abf" in notes[0]
    stale = tmp_path / "stale" / "M.SemanticModel"
    _touch(stale / cache, minutes_ago=30)
    _touch(stale / tables, minutes_ago=5)
    state, notes = cmp_mod.handoff_ready(tmp_path / "stale")
    assert state == "NOT_READY" and "STALE" in notes[0]
    warm = tmp_path / "two" / "tier2" / "M.SemanticModel"
    _touch(warm / tables, minutes_ago=10)
    _touch(warm / cache, minutes_ago=1)
    assert cmp_mod.handoff_ready(tmp_path / "two" / "tier2")[0] == "READY"
    _touch(tmp_path / "two" / "out" / "M.SemanticModel" / tables, minutes_ago=10)
    assert cmp_mod.handoff_ready(tmp_path / "two")[0] == "NOT_READY"
    (tmp_path / "empty" / "reports").mkdir(parents=True)
    assert cmp_mod.handoff_ready(tmp_path / "empty")[0] == "NO_MODEL"


def test_tamper_requires_structured_hash_evidence_for_every_drift(tmp_path):
    """Undeclared, comment-only and wrong-hash edits are DRIFT; legacy and append-only declarations clear it."""
    undeclared = tmp_path / "undeclared"
    _drifted(undeclared)
    state, notes = cmp_mod.tamper_check(undeclared)
    assert state == "DRIFT" and "UNDECLARED" in notes[0] and "Orders.tmdl" in notes[0]

    comment = tmp_path / "comment"
    _drifted(comment)
    _touch(comment / "_build" / "fix_orders_navigation.py").write_text(f'"""Mentions {ORDERS}."""\n', encoding="utf-8")
    assert cmp_mod.tamper_check(comment)[0] == "DRIFT"

    wrong_hash = tmp_path / "wrong-hash"
    _, baseline = _drifted(wrong_hash)
    _write_declaration(wrong_hash, ORDERS, baseline, "not-the-current-hash")
    assert cmp_mod.tamper_check(wrong_hash)[0] == "DRIFT"

    legacy = tmp_path / "legacy"
    generated, baseline = _drifted(legacy)
    _write_declaration(legacy, ORDERS, baseline, cmp_mod.sha256_file(generated))
    state, notes = cmp_mod.tamper_check(legacy)
    assert state == "DECLARED_DRIFT" and any("fix_orders_navigation.py" in n for n in notes)

    appended = tmp_path / "appended"
    generated, baseline = _drifted(appended)
    record = appended / "_build" / "generated-edit-declarations" / "one.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(_declaration(ORDERS, baseline, cmp_mod.sha256_file(generated))), encoding="utf-8")
    state, notes = cmp_mod.tamper_check(appended)
    assert state == "DECLARED_DRIFT" and any("fix_orders_navigation.py" in n for n in notes)


def test_tamper_ignores_refresh_cache_and_desktop_sidecars(tmp_path):
    """The false-positive guard: a normal refresh/autosave must not train people to bypass the check."""
    generated = _touch(tmp_path / ORDERS)
    _write_manifest(tmp_path, {ORDERS: cmp_mod.sha256_file(generated)})
    for sidecar in (
        "M.SemanticModel/.pbi/cache.abf",
        "M.SemanticModel/.pbi/localSettings.json",
        "R.Report/.pbi/localSettings.json",
    ):
        _touch(tmp_path / sidecar)
    state, notes = cmp_mod.tamper_check(tmp_path)
    assert state == "CLEAN" and "pristine" in notes[0]


def test_baseline_absence_shapes_are_distinct(tmp_path):
    """Missing, corrupt, foreign-report and unknown-version baselines are NO_BASELINE; a slice-only
    bundle (#230, no `generated_artifacts` key) is expected absence; a backfill still detects drift."""
    cases = {
        "missing": (None, "no input_manifest.json"),
        "corrupt": ("{not valid json", "not valid JSON"),
        "foreign": ({"generated_artifacts": {"version": 1, "run_id": "x", "recorded_at": "1999-01-01T00:00:00",
                     "report_generated_at": REPORT_AT, "report_sha256": "not-this-report", "files": {}}}, "baseline"),
        "version": ({"generated_artifacts": {"version": 999, "recorded_at": "1999-01-01T00:00:00", "files": {}}}, "baseline"),
    }  # fmt: skip
    for name, (manifest, needle) in cases.items():
        bundle = tmp_path / name
        generated = _touch(bundle / ORDERS)
        if isinstance(manifest, dict):
            manifest["generated_artifacts"]["files"] = {ORDERS: cmp_mod.sha256_file(generated)}
        (bundle / "report.json").write_text(json.dumps({"generated_at": REPORT_AT}), encoding="utf-8")
        if manifest is not None:
            text = manifest if isinstance(manifest, str) else json.dumps(manifest)
            (bundle / "input_manifest.json").write_text(text, encoding="utf-8")
        state, notes = cmp_mod.tamper_check(bundle)
        assert (state, needle in notes[0]) == ("NO_BASELINE", True), name

    slice_only = tmp_path / "slice"
    _touch(slice_only / ORDERS)
    (slice_only / "report.json").write_text(json.dumps({"generated_at": REPORT_AT}), encoding="utf-8")
    (slice_only / "input_manifest.json").write_text(
        json.dumps({"assets": [], "source_kind": "folder"}), encoding="utf-8"
    )
    state, notes = cmp_mod.tamper_check(slice_only)
    assert state == "NO_BASELINE_BY_DESIGN" and "EXPECTED ABSENCE" in notes[0] and "not tampering" in notes[0]

    for drift in (False, True):
        bundle = tmp_path / f"backfill-{drift}"
        generated = _touch(bundle / ORDERS)
        _write_manifest(bundle, {ORDERS: cmp_mod.sha256_file(generated)}, coverage="slice_only_backfill")
        if drift:
            generated.write_text("changed after the backfill", encoding="utf-8")
        state, notes = cmp_mod.tamper_check(bundle)
        assert state == ("DRIFT" if drift else "CLEAN")
        assert any("PARTIAL COVERAGE" in n for n in notes)
        assert not drift or any("UNDECLARED" in n for n in notes)


def _corrupt_both_declaration_locations(bundle: Path) -> None:
    """Invalid UTF-8 in the legacy ledger AND in the append-only directory."""
    legacy = bundle / "_build" / "generated-edit-declarations.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b'{"version": 1, "\xff\xfe": 1}')
    appended = bundle / "_build" / "generated-edit-declarations"
    appended.mkdir(parents=True, exist_ok=True)
    (appended / "record.json").write_bytes(b'{"version": 1, "\xff\xfe": 1}')


def test_a_pristine_bundle_never_consults_the_declaration_ledger(tmp_path, monkeypatch):
    """PR #399 round 3: loading the ledger before computing drift turned CLEAN into a traceback exit 1."""
    generated = _touch(tmp_path / ORDERS)
    _write_manifest(tmp_path, {ORDERS: cmp_mod.sha256_file(generated)})
    _corrupt_both_declaration_locations(tmp_path)
    state, notes = cmp_mod.tamper_check(tmp_path)
    assert state == "CLEAN" and "pristine" in notes[0]
    assert cmp_mod.run_tamper_mode(tmp_path, as_json=False) == 0

    def explode(_bundle):
        raise AssertionError("the declaration ledger was read for a bundle with no drift")

    monkeypatch.setattr(cmp_mod, "load_generated_edit_declarations", explode)
    assert cmp_mod.adjudicate_generated_drift(tmp_path, cmp_mod.load_generated_artifact_baseline(tmp_path)) == []


def test_drift_with_an_unreadable_ledger_is_its_own_state(tmp_path):
    """ "Cannot read the exonerating evidence" is not "there is none": exit 4, never DRIFT's 1."""
    _drifted(tmp_path)
    _corrupt_both_declaration_locations(tmp_path)
    state, notes = cmp_mod.tamper_check(tmp_path)
    assert state == "UNREADABLE_DECLARATIONS" and "NOT the same as undeclared drift" in notes[0]
    assert cmp_mod.run_tamper_mode(tmp_path, as_json=False) == 4
    with pytest.raises(cmp_mod.UnreadableDeclarations):
        cmp_mod.adjudicate_generated_drift(tmp_path, cmp_mod.load_generated_artifact_baseline(tmp_path))


def _declare(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DECLARE_SCRIPT), *args], capture_output=True, text=True, check=False)


def test_declare_wrapper_records_a_composed_path_fix_script(tmp_path):
    generated = _touch(tmp_path / ORDERS, minutes_ago=1)
    _write_manifest(tmp_path, {ORDERS: cmp_mod.sha256_file(generated)})
    fix = tmp_path / "_build" / "fix_post_engine.py"
    fix.parent.mkdir(parents=True, exist_ok=True)
    fix.write_text(
        "from pathlib import Path\n"
        "target = Path.cwd() / 'M.SemanticModel' / 'definition' / 'tables' / 'Orders.tmdl'\n"
        "target.write_text('fixed by composed path', encoding='utf-8')\n",
        encoding="utf-8",
    )
    proc = _declare("--bundle", str(tmp_path), "--target", ORDERS, "--script", str(fix), "--purpose", "fix Orders")
    assert proc.returncode == 0, proc.stderr
    state, notes = cmp_mod.tamper_check(tmp_path)
    assert state == "DECLARED_DRIFT" and any("fix_post_engine.py" in n for n in notes)


def test_batch_declaration_runs_stamper_once_and_tamper_requires_every_edit(tmp_path):
    """Batch declares the real stamper's three outputs; an extra mutation must fail the gate."""
    model = tmp_path / "pbip" / "M.SemanticModel"
    (model / "definition").mkdir(parents=True)
    (model / "definition" / "model.tmdl").write_bytes(b"model M\n\tculture: en-US\n")
    (model / "definition.pbism").write_bytes(b'{"version":"4.2.0","settings":{}}')
    baseline = {p.relative_to(tmp_path).as_posix(): cmp_mod.sha256_file(p) for p in model.rglob("*") if p.is_file()}
    _write_manifest(tmp_path, baseline)
    build = tmp_path / "_build"
    build.mkdir()
    md = build / "ai-instructions.md"
    md.write_bytes(b"# Model\nLatest means max date; avoid today.")
    stamper = REPO / ".github" / "skills" / "powerbi-ai-readiness" / "scripts" / "set_ai_instructions.py"
    replay = build / "stamp.py"
    replay.write_text(
        "import runpy\nfrom pathlib import Path\n"
        "counter = Path('_build/count')\n"
        "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')\n"
        f"runpy.run_path({str(stamper)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    targets = [
        model / "definition" / "cultures" / "en-US.tmdl",
        model / "definition" / "model.tmdl",
        model / "definition.pbism",
    ]
    command = ["--bundle", str(tmp_path), "--targets", *map(str, targets), "--script", str(replay)]
    command += ["--", "--model", str(model), "--md", str(md)]
    for count in (1, 2):
        proc = _declare(*command)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (build / "count").read_text() == str(count)
        assert cmp_mod.tamper_check(tmp_path)[0] == "DECLARED_DRIFT"
    targets[-1].write_bytes(b'{"settings":{"qnaEnabled":true},"undeclared":1}')
    proc = _declare(*command)  # the idempotent stamp cannot conceal the unrelated mutation
    assert proc.returncode == 1 and "UNDECLARED" in proc.stdout + proc.stderr
    never = tmp_path / "never.py"
    never.write_text("raise AssertionError('must not launch')", encoding="utf-8")
    for bad in ([tmp_path / "a", tmp_path / "a"], [tmp_path.parent / "outside"]):
        proc = _declare("--bundle", str(tmp_path), "--targets", *map(str, bad), "--script", str(never))
        assert proc.returncode == 2 and "targets must be distinct files inside the bundle" in proc.stderr


@pytest.mark.timing
def test_declare_wrapper_concurrent_writers_keep_both_declarations(tmp_path):
    """Two wrapper processes released by one barrier must not erase each other's declaration."""
    first = Path("M.SemanticModel") / "definition" / "tables" / "Orders.tmdl"
    second = Path("M.SemanticModel") / "definition" / "tables" / "Customers.tmdl"
    files = {t.as_posix(): cmp_mod.sha256_file(_touch(tmp_path / t)) for t in (first, second)}
    _write_manifest(tmp_path, files)
    go = tmp_path / "_build" / "go.signal"
    processes = []
    for target in (first, second):
        script = tmp_path / "_build" / f"fix_{target.stem.lower()}.py"
        ready = script.with_suffix(".ready")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "import sys, time\nfrom pathlib import Path\nroot = Path.cwd()\n"
            f"ready = root / {ready.relative_to(tmp_path).as_posix()!r}\n"
            f"go = root / {go.relative_to(tmp_path).as_posix()!r}\n"
            "ready.write_text('ready', encoding='utf-8')\n"
            "deadline = time.monotonic() + 10\n"
            "while not go.exists():\n"
            "    if time.monotonic() > deadline:\n"
            "        sys.exit(97)\n"
            "    time.sleep(0.01)\n"
            f"(root / {target.as_posix()!r}).write_text('fixed {target.stem}', encoding='utf-8')\n",
            encoding="utf-8",
        )
        argv = [sys.executable, str(DECLARE_SCRIPT), "--bundle", str(tmp_path), "--target", target.as_posix()]
        argv += ["--script", str(script), "--purpose", f"fix {target.stem}"]
        processes.append(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    deadline = time.monotonic() + 10
    while len(list((tmp_path / "_build").glob("*.ready"))) < 2:
        assert time.monotonic() < deadline, "both declaration writers must reach the barrier"
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")
    completed = [process.communicate(timeout=15) + (process.returncode,) for process in processes]
    assert all(code == 0 for _out, _err, code in completed), completed
    declarations = cmp_mod.load_generated_edit_declarations(tmp_path)
    assert {d["target"] for d in declarations} == set(files)
    state, notes = cmp_mod.tamper_check(tmp_path)
    assert state == "DECLARED_DRIFT"
    assert sum(1 for note in notes if note.startswith("DECLARED changed")) == 2


def _baseline_arg(minutes_ago: int = 60) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def test_cli_exit_codes_and_refusals(tmp_path, capsys, caplog):
    """Usable exit codes, machine-readable JSON, and every refusal is a 2 rather than a crash."""
    _touch(tmp_path / "_work" / "p.py")
    progress = ["--bundle", str(tmp_path), "--since-minutes", "30", "--baseline", _baseline_arg(), "--json"]
    assert cmp_mod.main(progress) == 1
    _touch(tmp_path / "pbip" / "M.SemanticModel" / "definition" / "t.tmdl")
    capsys.readouterr()
    assert cmp_mod.main(progress) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "PROGRESSING" and payload["buckets"]["deliverable"]["count"] == 1
    with caplog.at_level(logging.ERROR):
        assert cmp_mod.main(["--bundle", str(tmp_path), "--json"]) == 2
        assert "--baseline" in caplog.text
        assert cmp_mod.main(["--bundle", str(tmp_path), "--handoff", "--tamper"]) == 2
        assert "choose only one mode" in caplog.text
        assert cmp_mod.main(["--bundle", "no-such-dir-anywhere"]) == 2
        assert "no such bundle" in caplog.text.lower()
    slice_only = tmp_path / "slice"
    _touch(slice_only / ORDERS)
    (slice_only / "report.json").write_text(json.dumps({"generated_at": REPORT_AT}), encoding="utf-8")
    (slice_only / "input_manifest.json").write_text(json.dumps({"assets": []}), encoding="utf-8")
    capsys.readouterr()
    assert cmp_mod.main(["--bundle", str(slice_only), "--tamper", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["state"] == "NO_BASELINE_BY_DESIGN"


def test_STALLED_output_says_ASK_rather_than_kill(tmp_path):
    """End to end through `__main__`: the verdict must route to a question, not a kill."""
    _touch(tmp_path / "_work" / "p.py")
    out = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--bundle",
            str(tmp_path),
            "--since-minutes",
            "30",
            "--baseline",
            _baseline_arg(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 1
    assert "ASK IT WHAT IT IS BLOCKED ON" in out.stdout + out.stderr and "Do NOT kill it" in out.stdout + out.stderr


# --- the CANONICAL location: both modes must look at the phase-2 packages, not only the bundle ---
# Blind-review round-4 B1 (#460): --tamper reported CLEAN at exit 0 for an edited package, and
# progress reported SILENT with deliverable_count_seen=0, because only the bundle was inspected.


def _bundle_baseline(bundle: Path) -> None:
    files = {
        p.relative_to(bundle).as_posix(): cmp_mod.sha256_file(p)
        for p in sorted(bundle.rglob("*"))
        if cmp_mod._is_generated_artifact(p, bundle)  # noqa: SLF001  # pylint: disable=protected-access
    }
    _write_manifest(bundle, files)


def _run_layout(tmp_path: Path, *, edited: bool, minutes_ago: float = 0) -> tuple[Path, Path]:
    """`(bundle, package)` in the canonical `_runs/<NNN>-<slug>/{bundle,packages}` shape, bundle pristine."""
    bundle = tmp_path / "run" / "bundle"
    package = tmp_path / "run" / "packages" / "batch" / "Book"
    model = package / "fabric" / "Book.SemanticModel" / "definition"
    _touch(bundle / "pbip" / "Book" / "Book.Report" / "definition" / "pages" / "pages.json", minutes_ago)
    _touch(model / "tables" / "Imported0.tmdl", minutes_ago)
    contents = {
        p.relative_to(package).as_posix(): cmp_mod.sha256_file(p) for p in sorted(package.rglob("*")) if p.is_file()
    }
    if edited:
        (model / "tables" / "Imported0.tmdl").write_text("edited by the agent\n", encoding="utf-8")
    manifest = {"unit": "Book", "contents": {"files": contents}}
    (package / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _bundle_baseline(bundle)
    return bundle, package


def test_tamper_judges_the_canonical_package_too(tmp_path, capsys):
    """Edited 1, unedited 0, no digest 5 (unassessable), no packages a NAMED clean state."""
    bundle, _ = _run_layout(tmp_path / "edited", edited=True)
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "PACKAGE_DRIFT" and any("PACKAGE EDITED: Book" in n for n in notes)
    assert cmp_mod.main(["--bundle", str(bundle), "--tamper", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "PACKAGE_DRIFT"

    bundle, package = _run_layout(tmp_path / "clean", edited=False)
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "CLEAN" and any("phase-2 package(s) still match" in n for n in notes)
    assert cmp_mod.run_tamper_mode(bundle, as_json=False) == 0
    (package / "package-manifest.json").write_text(json.dumps({"unit": "Book"}), encoding="utf-8")
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "PACKAGE_UNASSESSABLE" and any("PACKAGE UNASSESSABLE" in n for n in notes)
    assert cmp_mod.run_tamper_mode(bundle, as_json=False) == 5

    lone = tmp_path / "lone" / "run" / "bundle"
    _touch(lone / "pbip" / "Book" / "Book.Report" / "definition" / "pages" / "pages.json")
    _bundle_baseline(lone)
    state, notes = cmp_mod.tamper_check(lone)
    assert state == "CLEAN" and any("no phase-2 packages found" in n for n in notes)


def test_a_bundle_level_finding_is_not_masked_by_a_package_finding(tmp_path):
    """The bundle verdict headlines (it is the baseline everything is measured from); package notes still ship."""
    bundle, _package = _run_layout(tmp_path, edited=True)
    (bundle / "input_manifest.json").unlink()
    state, notes = cmp_mod.tamper_check(bundle)
    assert state == "NO_BASELINE"
    assert any("PACKAGE EDITED: Book" in note for note in notes), "the package finding was dropped, not deferred"


def test_progress_counts_work_done_in_the_CANONICAL_package(tmp_path):
    """An old bundle and a fresh package is the shape of a unit handed to an agent after phase 2."""
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
    """A package carries its own manifest; `--packages` overrides the search rather than adding to it."""
    bundle, package = _run_layout(tmp_path, edited=False)
    assert cmp_mod.discover_package_roots(bundle) == [package]
    (tmp_path / "run" / "packages" / "batch" / "NotAPackage").mkdir(parents=True, exist_ok=True)
    assert cmp_mod.discover_package_roots(bundle) == [package]
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "Other").mkdir(parents=True, exist_ok=True)
    (elsewhere / "Other" / "package-manifest.json").write_text("{}", encoding="utf-8")
    assert cmp_mod.discover_package_roots(bundle, elsewhere) == [elsewhere / "Other"]
