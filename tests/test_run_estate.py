"""The estate coordinator turns an engine run into something safe to hand downstream.

Every test here corresponds to a measured gap in the deterministic tier's output contract, not to a
hypothetical. The engine is not at fault for any of them - it is a batch migrator and its choices are
defensible for that job. They are simply not safe for a CONSUMER, which is what this script is.
"""

from __future__ import annotations

import builtins
import contextlib
import functools
import hashlib
import io
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import NamedTuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_estate  # noqa: E402  # pylint: disable=wrong-import-position
from check_path_ceiling import DIR_CEILING, FILE_CEILING, utf16_len  # noqa: E402
from check_reference_readiness import engine_page_id  # noqa: E402
from host_paths import discloses_host_location  # noqa: E402

# These are imported ONLY to state ground truth for the composed CLI-evidence assertions below
# (the reservation directory name, the manifest key, the verify state) - every actual allocation in
# this file still goes exclusively through the public `work_dirs.py` CLI via subprocess, never
# through this import.
from work_dirs import (  # noqa: E402
    RUN_LOCATION_INTACT,
    RUN_PATH_KEY,
    _reservations_root,
    _run_number_dir_name,
)


def _report(workbooks=None, dod_status="pass", gates=None) -> dict:
    """A minimal report.json in the engine's real shape."""
    return {
        "tool": "tableau-fabric-skills",
        "generated_at": "2026-08-06T00:00:00Z",
        "source": {"kind": "folder", "root": "in"},
        "pending_gates": gates or [],
        "definition_of_done": {
            "applicable": True,
            "status": dod_status,
            "reports_bound": 1,
            "reports_failed": 0,
            "reports_warned": 0,
            "workbooks_total": 1,
        },
        "summary": {"workbook_calcs_stubbed": 0, "visuals_warned": 0},
        "workbooks": workbooks if workbooks is not None else [],
    }


def _workbook(name: str, model: str, requests: list[dict] | None = None) -> dict:
    return {
        "name": name,
        "bound_model": model,
        "model_translation_handoff": {"requests": requests or []},
        "viz_fidelity": [],
    }


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _root_for_length(length: int) -> Path:
    """A synthetic resolved root with an exact UTF-16 length on the current host."""
    anchor = Path.cwd().anchor
    return Path(anchor + "r" * (length - utf16_len(anchor))).resolve()


def _boundary_root(unit: str, ceiling: int) -> Path:
    probe_root = Path("/r").resolve()
    probe = run_estate.project_estate_path_ceiling(probe_root, [unit])
    file_length = next(path["length"] for path in probe["paths"] if path["kind"] == "file")
    target_length = utf16_len(str(probe_root)) + ceiling - file_length
    return _root_for_length(target_length)


# ---------------------------------------------------------------------------
# The reason this script exists at all
# ---------------------------------------------------------------------------


def test_a_failed_definition_of_done_is_not_a_pass() -> None:
    """The engine prints [FAIL] and then returns 0 anyway.

    `migrate_estate.py` ends with `# Soft-but-loud: exit stays 0` and an unconditional `return 0`.
    That is deliberate on its side - one bad workbook should not fail a batch - but a consumer that
    gates on the exit code silently accepts a failed migration. This is the single check that most
    justifies the coordinator being code rather than an instruction an agent must remember.
    """
    ok, detail = run_estate.check_definition_of_done(_report(dod_status="failed"))
    assert ok is False
    assert "failed" in detail


def test_projected_path_uses_utf16_and_accepts_the_measured_file_boundary() -> None:
    unit = "A" * 20
    root = _boundary_root(unit, FILE_CEILING)
    projection = run_estate.project_estate_path_ceiling(root, [unit])
    file_path = next(path for path in projection["paths"] if path["kind"] == "file")
    assert file_path["length"] == FILE_CEILING
    assert projection["status"] == "ok"


def test_projected_path_refuses_the_next_file_and_directory_boundaries() -> None:
    unit = "A" * 20
    root = _boundary_root(unit, FILE_CEILING + 1)
    projection = run_estate.project_estate_path_ceiling(root, [unit])
    assert projection["status"] == "over_ceiling"
    assert any(path["length"] == FILE_CEILING + 1 for path in projection["offenders"])
    assert any(path["length"] == DIR_CEILING + 1 for path in projection["offenders"])


def test_projected_path_counts_supplementary_characters_as_two_units() -> None:
    unit = "😀" * 20
    projection = run_estate.project_estate_path_ceiling(Path("/r"), [unit])
    file_path = next(path for path in projection["paths"] if path["kind"] == "file")
    assert file_path["length"] == utf16_len(file_path["path"])
    assert file_path["length"] > len(file_path["path"])


def test_projected_names_include_engine_collision_suffixes() -> None:
    projection = run_estate.project_estate_path_ceiling(Path("/short"), ["Sales", "sales"])

    assert projection["status"] == "cannot_establish"


def test_projected_names_are_supplied_by_the_selected_engine(tmp_path: Path) -> None:
    engine = _versioned_engine(tmp_path / "engine", "test")
    source = tmp_path / "source"
    source.mkdir()
    (source / "Sales.tds").write_text("<datasource />", encoding="utf-8")
    (source / "Sales.twb").write_text("<workbook />", encoding="utf-8")

    assert run_estate._engine_unit_names(engine, source) == ["Sales", "Sales_2"]


def test_selected_engine_controls_naming(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "Sales.twb").write_text("<workbook />", encoding="utf-8")
    canonical = _versioned_engine(tmp_path / "canonical", "test")
    override = _versioned_engine(tmp_path / "override", "test")
    helper = override / "skills" / "tableau-migration" / "scripts" / "migrate_estate.py"
    helper.write_text(
        helper.read_text(encoding="utf-8").replace("return candidate\n", 'return "override_" + candidate\n'),
        encoding="utf-8",
    )

    assert run_estate._engine_unit_names(canonical, source) == ["Sales"]
    assert run_estate._engine_unit_names(override, source) == ["override_Sales"]


def test_engine_helper_failure_cannot_assess(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    scripts = engine / "skills" / "tableau-migration" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "migrate_estate.py").write_text("raise RuntimeError('changed helper')\n", encoding="utf-8")
    source = tmp_path / "source.twb"
    source.write_text("<workbook />", encoding="utf-8")

    assert run_estate._engine_unit_names(engine, source) is None


def test_packaged_source_requires_full_utf8_decode(tmp_path: Path) -> None:
    source = tmp_path / "Broken.twbx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("workbook.twb", b"<workbook>\xff</workbook>")

    assert run_estate._readable_source(source) is False


def test_loose_source_requires_full_utf8_decode(tmp_path: Path) -> None:
    source = tmp_path / "Broken.twb"
    source.write_bytes(b"<workbook>\xff</workbook>")

    assert run_estate._readable_source(source) is False


def test_unreadable_source_cannot_pass_path_preflight(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Locked.twb"
    source.write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "_readable_source", lambda _path: False)

    ok, detail = run_estate.preflight_estate_path_ceiling(source, Path("/short"))

    assert ok is False
    assert "CANNOT ASSESS" in detail


def test_pbir_envelope_is_pinned_to_committed_artifacts() -> None:
    """The projection must remain above the largest committed page/visual directory identifiers."""
    paths = list((Path(__file__).resolve().parents[1] / "examples").rglob("visual.json"))
    paths += list((Path(__file__).resolve().parents[1] / "migrations").rglob("visual.json"))
    identifiers = []
    for path in paths:
        parts = path.parts
        page_index = parts.index("pages")
        visual_index = parts.index("visuals")
        identifiers.append((utf16_len(parts[page_index + 1]), utf16_len(parts[visual_index + 1])))
    assert len(identifiers) == 869
    assert max(page for page, _ in identifiers) == 20
    assert run_estate._PBIR_MAX_PAGE_ID_UTF16 == 24
    assert max(visual for _, visual in identifiers) == run_estate._PBIR_MAX_VISUAL_ID_UTF16
    assert utf16_len(engine_page_id("x" * 100)) == run_estate._PBIR_MAX_PAGE_ID_UTF16


def test_realistic_long_estate_is_refused_by_conservative_pbir_envelope() -> None:
    root = _root_for_length(90)
    unit = "u" * 37
    projection = run_estate.project_estate_path_ceiling(root, [unit])
    file_path = next(path for path in projection["paths"] if path["kind"] == "file")
    directory = next(path for path in projection["paths"] if path["kind"] == "directory")
    report_root = Path("pbip") / unit / f"{unit}.Report"
    visual_tail = Path(run_estate._PBIR_VISUAL_TAIL)
    assert utf16_len(str(root)) == 90
    assert file_path["length"] == utf16_len(str(root)) + 1 + utf16_len(str(report_root / visual_tail))
    assert directory["length"] == utf16_len(str(root)) + 1 + utf16_len(str(report_root / visual_tail.parent))
    assert projection["status"] == "over_ceiling"


def _refuse_before_engine(tmp_path: Path, monkeypatch, output: Path) -> tuple[int, list[Path], str]:
    """Run `main` to the preflight refusal and return (exit code, engine calls, printed detail)."""
    engine = _versioned_engine(tmp_path / "engine", "2.126.0")
    source = tmp_path / "src"
    source.mkdir()
    (source / "Sales.twb").write_text("<workbook />", encoding="utf-8")
    calls: list[Path] = []
    monkeypatch.setattr(run_estate, "run_engine", lambda *args: calls.append(args[0]) or (0, ""))
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_estate.main(
            [
                "--engine",
                str(engine),
                "--allow-noncanonical-engine",
                "--input",
                str(source),
                "--output",
                str(output),
            ]
        )
    return code, calls, buffer.getvalue()


def test_main_refuses_an_over_ceiling_projection_and_says_which_path_and_by_how_much(tmp_path, monkeypatch) -> None:
    """The refusal must be ATTRIBUTABLE, not merely non-zero.

    ⚠️ Round-1 review: this test previously shared one body (and one assertion set) with the
    unreadable-source case below, so `EXIT_PATH_CEILING` plus "engine not called, output absent" was
    accepted for a `CANNOT ASSESS` result that names no path at all. Those two arms refuse for
    different reasons and now assert their own reason. The exit code is deliberately checked LAST:
    it is the weakest signal here and it is shared by both arms.
    """
    output = _boundary_root("Sales", FILE_CEILING + 1)
    code, calls, printed = _refuse_before_engine(tmp_path, monkeypatch, output)

    match = re.search(
        r"PATH CEILING: projected (?P<kind>directory|file) is (?P<length>\d+) UTF-16 units "
        r"\(ceiling (?P<ceiling>\d+)\) for unit (?P<unit>'[^']+')",
        printed,
    )
    assert match, f"the refusal did not name the offending path.\nprinted:\n{printed}"
    kind, length, ceiling, unit = (
        match.group("kind"),
        int(match.group("length")),
        int(match.group("ceiling")),
        match.group("unit"),
    )
    # The root was constructed to sit exactly ONE unit over both ceilings, so only these two
    # (kind, length, ceiling) triples are legitimate. Membership rather than a single expectation,
    # because which of the two equally-over offenders wins is an incidental tie-break.
    assert (kind, length, ceiling) in {
        ("directory", DIR_CEILING + 1, DIR_CEILING),
        ("file", FILE_CEILING + 1, FILE_CEILING),
    }, f"named {kind} {length} vs ceiling {ceiling}; the boundary root puts both exactly one over"
    assert unit == "'Sales'", f"the refusal named unit {unit}, not the estate's only unit"
    assert "CANNOT ASSESS" not in printed, "an over-ceiling projection must not report itself as unassessable"
    assert "--runs-parent" in printed, "the refusal must name the supported escape command (issue #479)"
    assert not calls, "the engine must not run after a path-ceiling refusal"
    assert not output.exists(), "a refused run must not create its output root"
    assert code == run_estate.EXIT_PATH_CEILING


def test_main_refuses_an_unreadable_source_with_its_own_cannot_assess_reason(tmp_path, monkeypatch) -> None:
    """The other arm: nothing could be measured, so nothing may be reported as measured.

    It shares `EXIT_PATH_CEILING` with the case above, which is exactly why the diagnostic — not the
    exit code — carries the meaning.
    """
    monkeypatch.setattr(run_estate, "_readable_source", lambda _path: False)
    output = tmp_path / "short"
    code, calls, printed = _refuse_before_engine(tmp_path, monkeypatch, output)

    assert "CANNOT ASSESS downstream PBIP path length" in printed, (
        f"an unreadable source must refuse as unassessable.\nprinted:\n{printed}"
    )
    assert "no usable unit/workbook name" in printed, (
        f"the CANNOT ASSESS refusal must say WHY it could not measure.\nprinted:\n{printed}"
    )
    assert not re.search(r"PATH CEILING: projected (directory|file) is \d+", printed), (
        "an unmeasurable estate must not claim a projected length it never computed"
    )
    assert "--runs-parent" in printed
    assert not calls, "the engine must not run after a preflight refusal"
    assert not output.exists(), "a refused run must not create its output root"
    assert code == run_estate.EXIT_PATH_CEILING


def test_estate_path_preflight_accepts_short_root_and_refuses_long_root(tmp_path: Path) -> None:
    source = tmp_path / ("A" * 20 + ".twb")
    source.write_text("<workbook />", encoding="utf-8")
    engine = _versioned_engine(tmp_path / "engine", "test")
    assert run_estate.preflight_estate_path_ceiling(source, Path("/short"), engine)[0] is True
    ok, detail = run_estate.preflight_estate_path_ceiling(source, _boundary_root("A" * 20, FILE_CEILING + 1), engine)
    assert ok is False
    assert "--runs-parent" in detail, "the refusal must name the actual supported escape command (issue #479)"


def _work_dirs_allocate(unit: str, root_flag: str, root: Path) -> dict:
    """Invoke the PUBLIC `work_dirs.py` CLI (not the library function) and parse its JSON."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "work_dirs.py"
    result = subprocess.run(
        [sys.executable, str(script), unit, root_flag, str(root), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _work_dirs_verify(root_flag: str, root: Path) -> dict:
    """Invoke the PUBLIC `work_dirs.py --verify` CLI (not the library function) and parse its JSON."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "work_dirs.py"
    result = subprocess.run(
        [sys.executable, str(script), "--verify", root_flag, str(root), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _assert_cli_evidence(root_flag: str, external_root: Path, alloc: dict) -> None:
    """The CLI's own reported paths must correspond to a REAL allocation on disk, not merely a
    plausible-looking JSON payload. A helper that hardcoded a returned path without ever invoking
    `allocate_run` would satisfy the earlier "invoke the CLI, parse its JSON" requirement to the
    letter while proving nothing - this is what closes that gap. See
    `test_cli_evidence_assertions_reject_a_hardcoded_path_with_no_real_allocation` for the negative
    control proving these checks actually have teeth.
    """
    run_root = Path(alloc["root"])
    bundle = Path(alloc["bundle"])
    assert bundle == run_root / "bundle", f"reported bundle {bundle} is not root/bundle under {run_root}"
    assert run_root.is_dir(), f"reported run root {run_root} does not exist on disk"

    manifest_path = run_root / "run.json"
    assert manifest_path.is_file(), f"reported run root {run_root} has no run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get(RUN_PATH_KEY) == os.path.abspath(str(run_root)), (
        f"run.json's {RUN_PATH_KEY}={manifest.get(RUN_PATH_KEY)!r} does not match the CLI-reported root {run_root}"
    )

    runs_root_dir = external_root / "_runs"
    reservation_dir = _reservations_root(runs_root_dir) / _run_number_dir_name(alloc["run_number"])
    assert reservation_dir.is_dir(), f"expected reservation directory {reservation_dir} for run {alloc['run_number']}"

    verify = _work_dirs_verify(root_flag, external_root)
    matching = [r for r in verify["runs"] if r.get("run") == alloc["run_number"]]
    assert matching, f"--verify did not report run {alloc['run_number']} among {[r.get('run') for r in verify['runs']]}"
    state = matching[0]["location_check"]["state"]
    assert state == RUN_LOCATION_INTACT, f"--verify reported run {alloc['run_number']} as {state!r}, not intact"


def test_cli_evidence_assertions_reject_a_hardcoded_path_with_no_real_allocation(tmp_path: Path) -> None:
    """Mutation-sensitive negative control: an `alloc` dict shaped exactly like the CLI's real JSON
    output, but never produced by an actual `allocate_run` call (no run.json, no reservation, no
    directory on disk), must fail `_assert_cli_evidence` - proving a helper that merely returns a
    plausible path string cannot pass the composed test's evidence checks.
    """
    fake_root = tmp_path / "_runs" / "001-fake"
    fake_alloc = {"root": str(fake_root), "bundle": str(fake_root / "bundle"), "run_number": 1, "unit_key": "fake"}
    with pytest.raises(AssertionError):
        _assert_cli_evidence("--repo-root", tmp_path, fake_alloc)


def _short_external_root() -> Path:
    """A genuinely short, writable, unique root - the test equivalent of `C:\\t2p` on Windows, or a
    short unique directory directly under `/tmp` on POSIX.

    `tempfile.mkdtemp()` alone is NOT a short-root control: on Windows it resolves under `%TEMP%`,
    which lives deep under the user profile (`C:\\Users\\<name>\\AppData\\Local\\Temp\\...`) and
    measured to still reproduce `over_ceiling` there - it is not a short root at all, just a
    different long one. This allocates directly under the drive root on Windows, or directly under
    `/tmp` on POSIX, so the contrast with the deep/default root above is real.
    """
    unique = uuid.uuid4().hex[:8]
    if os.name == "nt":
        drive = os.environ.get("SystemDrive", "C:")
        root = Path(f"{drive}\\t2p-{unique}")
    else:
        root = Path(f"/tmp/t2p-{unique}")
    root.mkdir(parents=True, exist_ok=False)
    return root


def test_composed_allocator_and_projection_reproduces_run_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry-run 409's exact wall, composed end to end: the PUBLIC `work_dirs.py` CLI allocates a run
    for the real run-409 unit name, its JSON `bundle` path is fed straight into
    `run_estate.project_estate_path_ceiling`, and the deep/default allocation must project
    OVER Desktop's ceilings while the short `--runs-parent` allocation must project OK - not two
    separate assertions about the allocator and the projector in isolation.
    """
    unit = "Meridian_Calc_Gauntlet__Live_Snowflake_"

    # Learn the constant `_runs/<NNN>-<slug>/bundle` suffix the allocator appends, and the constant
    # downstream PBIR tail the projector appends, from ONE reference allocation/projection - both
    # are independent of where the root sits, so a short probe root is enough to measure them.
    probe_root = tmp_path / "probe"
    probe_alloc = _work_dirs_allocate(unit, "--repo-root", probe_root)
    probe_bundle = Path(probe_alloc["bundle"])
    suffix_len = utf16_len(str(probe_bundle)) - utf16_len(str(probe_root))
    probe_projection = run_estate.project_estate_path_ceiling(probe_bundle, [unit])
    probe_file_len = next(p["length"] for p in probe_projection["paths"] if p["kind"] == "file")
    tail_len = probe_file_len - utf16_len(str(probe_bundle))

    # From here on, wrap the REAL `subprocess.run` so every invocation still executes exactly as
    # before, but its argv is recorded - proof that the deep and short allocations below go through
    # the public CLI subprocess and not a direct `allocate_run()` library call the assertions below
    # can no longer be fooled by. A helper swapped to call the library directly would leave
    # `recorded_argv` empty and fail the count assertion below.
    real_subprocess_run = subprocess.run
    recorded_argv: list[list[str]] = []

    def _spying_run(argv, *args, **kwargs):
        recorded_argv.append([str(a) for a in argv])
        return real_subprocess_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spying_run)

    # A deep/default-shaped repo root: padded so the allocated bundle path alone (before the
    # engine's own PBIR tail) already leaves no room - reproducing the 275/259 shape from run 409.
    deep_len = FILE_CEILING - suffix_len - tail_len + 6
    deep_root = tmp_path / ("d" * max(deep_len - utf16_len(str(tmp_path)) - 1, 1))
    try:
        deep_alloc = _work_dirs_allocate(unit, "--repo-root", deep_root)
        _assert_cli_evidence("--repo-root", deep_root, deep_alloc)
        deep_projection = run_estate.project_estate_path_ceiling(Path(deep_alloc["bundle"]), [unit])
        deep_file_len = next(p["length"] for p in deep_projection["paths"] if p["kind"] == "file")
        deep_dir_len = next(p["length"] for p in deep_projection["paths"] if p["kind"] == "directory")
        assert deep_projection["status"] == "over_ceiling"
        assert deep_file_len > FILE_CEILING, f"deep file length {deep_file_len} does not exceed ceiling {FILE_CEILING}"
        assert deep_dir_len > DIR_CEILING, f"deep dir length {deep_dir_len} does not exceed ceiling {DIR_CEILING}"
    finally:
        shutil.rmtree(deep_root, ignore_errors=True)
    assert not deep_root.exists(), "the deep test's run/reservation tree must not survive the test"

    # The same unit, allocated under a genuinely short EXTERNAL --runs-parent root (directly under
    # the drive root on Windows, directly under /tmp on POSIX): reproducing run 409's 208/259 shape,
    # comfortably clear of both ceilings.
    short_root = _short_external_root()
    try:
        short_alloc = _work_dirs_allocate(unit, "--runs-parent", short_root)
        _assert_cli_evidence("--runs-parent", short_root, short_alloc)
        short_bundle = Path(short_alloc["bundle"])
        short_projection = run_estate.project_estate_path_ceiling(short_bundle, [unit])
        measured = [(p["kind"], p["length"], p["ceiling"]) for p in short_projection["paths"]]
        assert short_projection["status"] == "ok", f"measured lengths (kind, length, ceiling): {measured}"
        assert not short_projection["offenders"], f"measured lengths (kind, length, ceiling): {measured}"
        file_len = next(p["length"] for p in short_projection["paths"] if p["kind"] == "file")
        dir_len = next(p["length"] for p in short_projection["paths"] if p["kind"] == "directory")
        assert file_len <= FILE_CEILING, f"file length {file_len} exceeds ceiling {FILE_CEILING}"
        assert dir_len <= DIR_CEILING, f"dir length {dir_len} exceeds ceiling {DIR_CEILING}"
    finally:
        shutil.rmtree(short_root, ignore_errors=True)
    assert not short_root.exists(), "the external run/reservation tree must not survive the test"

    # Prove the deep/short allocations really went through the public CLI subprocess - not a direct
    # `allocate_run()` call a rewritten helper could substitute without anyone noticing. `--verify`
    # invocations (issued by `_assert_cli_evidence`) are excluded on purpose: only the two ALLOCATING
    # calls are being counted here.
    script = str(Path(__file__).resolve().parents[1] / "scripts" / "work_dirs.py")
    allocation_calls = [argv for argv in recorded_argv if "--verify" not in argv]
    verify_calls = [argv for argv in recorded_argv if "--verify" in argv]
    assert len(allocation_calls) == 2, (
        f"expected exactly two allocation subprocess invocations (deep + short), got {recorded_argv}"
    )
    assert verify_calls, "expected --verify subprocess invocations from _assert_cli_evidence"
    for argv in allocation_calls:
        assert script in argv, f"allocation subprocess did not invoke the public CLI {script}: {argv}"
        assert "--json" in argv, f"allocation subprocess did not pass --json: {argv}"
        assert unit in argv, f"allocation subprocess did not pass the exact unit {unit!r}: {argv}"
    assert any("--repo-root" in argv and str(deep_root) in argv for argv in allocation_calls), (
        f"no allocation call used --repo-root {deep_root}: {allocation_calls}"
    )
    assert any("--runs-parent" in argv and str(short_root) in argv for argv in allocation_calls), (
        f"no allocation call used --runs-parent {short_root}: {allocation_calls}"
    )


def test_estate_path_preflight_cannot_assess_missing_input(tmp_path: Path) -> None:
    engine = _versioned_engine(tmp_path / "engine", "test")
    ok, detail = run_estate.preflight_estate_path_ceiling(tmp_path / "missing", Path("/short"), engine)
    assert ok is False
    assert "CANNOT ASSESS" in detail


def test_warn_is_allowed_through() -> None:
    """`warn` is the NORMAL state of a real migration - deferred visuals, stubbed calcs.

    Blocking on it would make the coordinator useless on every workbook that has any gap, which is
    all of them. Only `failed` blocks.
    """
    ok, _ = run_estate.check_definition_of_done(_report(dod_status="warn"))
    assert ok is True


def test_a_run_without_a_definition_of_done_is_not_failed() -> None:
    """A datasource-only run has no report to bind, so DoD is not applicable. That is not a failure."""
    report = _report()
    report["definition_of_done"] = {"applicable": False}
    ok, detail = run_estate.check_definition_of_done(report)
    assert ok is True
    assert "not applicable" in detail


# ---------------------------------------------------------------------------
# The latent hazard: --approved-dax is estate-global and name-keyed
# ---------------------------------------------------------------------------


def test_same_calc_name_in_two_models_is_a_collision() -> None:
    """`_load_approved_dax` returns a flat {name: DAX} map with no model scoping.

    Measured on six real workbooks: 10 stubbed calcs, 0 collisions - but the names were
    `Calculation2` (Tableau's auto-generated default), `Rank`, `Size`, `Running Sum`. Latent, not
    observed, which is exactly when a cheap check earns its place.
    """
    report = _report(
        workbooks=[
            _workbook("Sales WB", "SalesModel", [{"name": "Running Sum", "formula": "RUNNING_SUM(SUM([A]))"}]),
            _workbook("HR WB", "HrModel", [{"name": "Running Sum", "formula": "RUNNING_SUM(SUM([B]))"}]),
        ]
    )
    collisions = run_estate.find_approval_collisions(report)
    assert "running sum" in collisions
    assert len(collisions["running sum"]) == 2


def test_the_same_name_twice_in_one_model_is_not_a_collision() -> None:
    """A collision is (same name, DIFFERENT model). One model cannot land the wrong DAX in itself."""
    report = _report(
        workbooks=[
            _workbook(
                "One WB",
                "OneModel",
                [{"name": "Rank", "formula": "RANK()"}, {"name": "Rank", "formula": "RANK()"}],
            )
        ]
    )
    assert not run_estate.find_approval_collisions(report)


def test_collision_detection_is_case_insensitive() -> None:
    """The upstream loader is a plain dict keyed by the author's spelling; ours must not be fooled."""
    report = _report(
        workbooks=[
            _workbook("A", "ModelA", [{"name": "Calculation2", "formula": "X"}]),
            _workbook("B", "ModelB", [{"name": "calculation2", "formula": "Y"}]),
        ]
    )
    assert "calculation2" in run_estate.find_approval_collisions(report)


def test_collision_carries_the_formulas_so_a_human_can_judge() -> None:
    """Identical formulas under one name are harmless; differing formulas land the WRONG DAX.

    The check must not force a caller to go and look this up - the distinction is the whole decision.
    """
    report = _report(
        workbooks=[
            _workbook("A", "ModelA", [{"name": "Size", "formula": "SUM([X])"}]),
            _workbook("B", "ModelB", [{"name": "Size", "formula": "SUM([X])"}]),
        ]
    )
    claims = run_estate.find_approval_collisions(report)["size"]
    assert len({c["formula"] for c in claims}) == 1


# ---------------------------------------------------------------------------
# Generated artifact fingerprints: downstream edits must be visible
# ---------------------------------------------------------------------------


def test_generated_artifact_manifest_records_only_stable_generated_files(tmp_path: Path) -> None:
    """A normal refresh writes .pbi/cache.abf; that must not look like artifact tampering."""
    _write(tmp_path / "fabric" / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl", "table Orders")
    _write(tmp_path / "fabric" / "M.SemanticModel" / ".pbi" / "cache.abf", "refresh cache")
    _write(tmp_path / "fabric" / "R.Report" / "definition" / "report.json", "{}")
    _write(tmp_path / "fabric" / "R.Report" / ".pbi" / "localSettings.json", "{}")
    _write(tmp_path / "fabric" / "Book.pbip", "{}")
    _write(tmp_path / "_probe" / "Probe.pbip", "{}")

    run_estate.write_generated_artifact_manifest(tmp_path)

    manifest = json.loads((tmp_path / "input_manifest.json").read_text(encoding="utf-8"))
    recorded = set(manifest["generated_artifacts"]["files"])
    assert "fabric/M.SemanticModel/definition/tables/Orders.tmdl" in recorded
    assert "fabric/R.Report/definition/report.json" in recorded
    assert "fabric/Book.pbip" in recorded
    assert "fabric/M.SemanticModel/.pbi/cache.abf" not in recorded
    assert "fabric/R.Report/.pbi/localSettings.json" not in recorded
    assert "_probe/Probe.pbip" not in recorded


def test_generated_artifact_manifest_ignores_foreign_roots_from_before_this_run(tmp_path: Path) -> None:
    """A landing run must not bless stale roots the engine did not recreate."""
    stale = _write(tmp_path / "fabric" / "Stale.Report" / "definition" / "report.json", "{}")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    started = time.time() - 1
    fresh = _write(tmp_path / "fabric" / "Fresh.Report" / "definition" / "report.json", "{}")
    assert fresh.stat().st_mtime >= started

    run_estate.write_generated_artifact_manifest(tmp_path, _report(), earliest_mtime=started)

    manifest = json.loads((tmp_path / "input_manifest.json").read_text(encoding="utf-8"))
    recorded = set(manifest["generated_artifacts"]["files"])
    assert "fabric/Fresh.Report/definition/report.json" in recorded
    assert "fabric/Stale.Report/definition/report.json" not in recorded


def test_the_generated_manifest_is_written_before_the_engine_receipt(tmp_path: Path, monkeypatch) -> None:
    """Ordering is load-bearing, not cosmetic - the two mechanisms share a file.

    ``write_generated_artifact_manifest`` UPSERTS into ``input_manifest.json``; the engine receipt
    HASHES that same file. Receipt-first therefore leaves ``input_manifest_sha256`` stale on every
    legitimate run, and the credential gate rejects the bundle the engine just produced. Measured
    when merging the two changes, which were developed independently and neither of whose suites
    could observe the interaction.

    This drives ``main()`` rather than the helpers, so re-ordering the real pipeline fails it. A
    helper-level test would document the constraint without guarding it.
    """
    sys.path.insert(0, str(Path(run_estate.__file__).resolve().parent))
    from credential_gate import _receipt_matches_bundle  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    out = tmp_path / "bundle"

    def _fake_engine(_engine: Path, _src: Path, dest: Path, _dax: Path | None) -> tuple[int, str]:
        _write(dest / "report.json", json.dumps(_report()))
        _write(dest / "input_manifest.json", '{"inputs": []}')
        _write(dest / "fabric" / "Orders.SemanticModel" / "definition" / "t.tmdl", "table Orders")
        return 0, ""

    monkeypatch.setattr(run_estate, "run_engine", _fake_engine)
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _versioned_engine(tmp_path / "engine", "test")
    argv = [
        "--engine",
        str(tmp_path / "engine"),
        "--allow-noncanonical-engine",
        "--input",
        str(src),
        "--output",
        str(out),
    ]
    assert run_estate.main(argv) == run_estate.EXIT_OK

    receipt = json.loads((out / "engine-output-receipt.json").read_text(encoding="utf-8"))
    assert _receipt_matches_bundle(out, receipt), (
        "the receipt does not describe the bundle main() just produced - "
        "the generated-artifact manifest must be written BEFORE the receipt"
    )


# ---------------------------------------------------------------------------
# One engine, and the bundle says which one (issue #107)
# ---------------------------------------------------------------------------


def test_a_noncanonical_engine_stops_the_run_instead_of_running_it(tmp_path: Path, monkeypatch) -> None:
    """The estate coordinator must not run whatever tree it is pointed at without being told to.

    Measured 2026-08-12: this script's `--engine` DEFAULT was a sibling clone at 2.126.0 while other
    steps resolved the plugin at 2.113.0, and the two emit materially different map visuals. Refusing
    here is what makes "the plugin is the single source" true at the point of execution rather than
    only in a document.
    """
    ran: list[Path] = []
    monkeypatch.setattr(run_estate, "run_engine", lambda engine, *_: (ran.append(engine), (0, ""))[1])

    src = tmp_path / "src"
    src.mkdir()
    argv = ["--engine", str(tmp_path / "elsewhere"), "--input", str(src), "--output", str(tmp_path / "bundle")]
    assert run_estate.main(argv) == run_estate.EXIT_ENGINE_SOURCE
    assert not ran, "the engine ran despite being non-canonical and unacknowledged"


def test_the_bundle_records_which_engine_built_it(tmp_path: Path, monkeypatch) -> None:
    """#107's acceptance criterion: the artifact answers "what built me?" without the machine."""
    engine = _versioned_engine(tmp_path / "engine", "2.126.0")

    out = tmp_path / "bundle"

    def _fake_engine(_engine: Path, _src: Path, dest: Path, _dax: Path | None) -> tuple[int, str]:
        _write(dest / "report.json", json.dumps(_report()))
        _write(dest / "input_manifest.json", '{"inputs": []}')
        return 0, ""

    monkeypatch.setattr(run_estate, "run_engine", _fake_engine)
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    argv = ["--engine", str(engine), "--allow-noncanonical-engine", "--input", str(src), "--output", str(out)]
    assert run_estate.main(argv) == run_estate.EXIT_OK

    receipt = json.loads((out / "engine-output-receipt.json").read_text(encoding="utf-8"))
    assert receipt["engine"]["version"] == "2.126.0"
    assert receipt["engine"]["root"] == str(engine)
    assert receipt["engine"]["canonical"] is False, "an override must be recorded AS an override"


def test_slice_only_needs_no_engine_at_all(tmp_path: Path, monkeypatch) -> None:
    """Re-deriving handovers from an existing bundle must not require the plugin to be installed."""
    import engine_source  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", tmp_path / "no-plugin-here")
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(workbooks=[_workbook("Alpha", "AlphaModel")])))
    assert run_estate.main(["--slice-only", "--output", str(out)]) == run_estate.EXIT_OK
    assert (out / "handover" / "Alpha.json").is_file()


# ---------------------------------------------------------------------------
# issue #230: --slice-only skipped the generated-artifact baseline entirely
# ---------------------------------------------------------------------------


def test_slice_only_backfills_a_missing_baseline(tmp_path: Path) -> None:
    """The defect: a bundle built with --slice-only never carried a generated_artifacts baseline.

    Reproduces the exact shape ``migrate_estate.py`` itself writes - an ``input_manifest.json`` with
    no ``generated_artifacts`` key at all - and asserts ``run_estate.py --slice-only`` now backfills
    one instead of leaving ``check_migration_progress.py --tamper`` permanently unable to check it.
    """
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report()))
    _write(out / "input_manifest.json", json.dumps({"assets": [], "root": str(out)}))
    _write(out / "fabric" / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl", "table Orders")

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK

    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    generated = manifest["generated_artifacts"]
    assert generated["coverage"] == "slice_only_backfill"
    assert "fabric/M.SemanticModel/definition/tables/Orders.tmdl" in generated["files"]


def test_slice_only_backfill_never_overwrites_an_existing_baseline(tmp_path: Path) -> None:
    """A prior full engine run through run_estate.py already recorded real evidence - never clobber it."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report()))
    original = {
        "version": 1,
        "run_id": "original-run",
        "recorded_at": "2026-08-01T00:00:00+00:00",
        "report_generated_at": _report().get("generated_at"),
        "report_sha256": run_estate.sha256_file(out / "report.json"),
        "files": {"fabric/Stale.SemanticModel/definition/t.tmdl": "deadbeef"},
    }
    _write(out / "input_manifest.json", json.dumps({"generated_artifacts": original}))

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK

    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_artifacts"] == original


def test_slice_only_backfill_does_not_clobber_an_invalid_baseline_either(tmp_path: Path) -> None:
    """Even a broken/mismatched generated_artifacts entry might be tamper evidence - leave it in place."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report()))
    invalid = {"version": 999, "files": {}}
    _write(out / "input_manifest.json", json.dumps({"generated_artifacts": invalid}))

    run_estate.backfill_slice_only_baseline(out, _report(), [])

    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_artifacts"] == invalid


# ---------------------------------------------------------------------------
# The empty-model gate: a bundle that passes everything above and holds no data
# ---------------------------------------------------------------------------


def _unlanded_model(out: Path, workbook: str = "global_superstores_db") -> None:
    """An Import partition over a flat file that was never landed - the measured silent success.

    The path is absolute and belongs to the machine the Tableau workbook was authored on. On the
    Windows host that produced the measured estate the detector calls that `foreign_path`; on a Linux
    CI runner the same path is simply `missing_file`. Both block, which is the point: these
    assertions are about the coordinator's verdict, not about which runner executed them.
    """
    tables = out / "pbip" / workbook / "Orders.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    _write(
        tables / "Orders.tmdl",
        "table Orders\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tSource = Excel.Workbook(File.Contents("/Users/<author>/Datasets/Orders.xlsx"), null, true)\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n",
    )


def _slice_only_argv(out: Path) -> list[str]:
    return ["--output", str(out), "--slice-only"]


def test_an_empty_model_blocks_a_bundle_that_the_definition_of_done_let_through(tmp_path: Path, capsys) -> None:
    """The exact measured case: `definition_of_done: warn`, report bound, model contains nothing.

    ``warn`` is deliberately allowed through (see the DoD tests above), so before this gate existed
    this bundle reached the deployer and then a customer. The verdict has to live in the exit code -
    a printed warning is what the engine already produced, and it was not enough.
    """
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="warn")))
    _unlanded_model(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_EMPTY_MODEL
    printed = capsys.readouterr().out
    assert "global_superstores_db" in printed
    assert "Orders.xlsx" in printed


def test_a_healthy_bundle_still_exits_zero(tmp_path: Path) -> None:
    """The false-positive control at coordinator level: a landed CSV must not block the estate."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="warn")))
    landed = _write(out / "data" / "Orders" / "Extract.csv", "a,b\n1,2\n")
    tables = out / "pbip" / "wb" / "Orders.SemanticModel" / "definition" / "tables"
    _write(
        tables / "Orders.tmdl",
        "table Orders\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = Csv.Document(File.Contents("{landed.as_posix()}"))\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n",
    )

    assert run_estate.main(_slice_only_argv(out)) == run_estate.EXIT_OK


def test_the_empty_model_verdict_is_printed_even_when_the_definition_of_done_already_failed(
    tmp_path: Path, capsys
) -> None:
    """Precedence is DoD-first, but the READER must still be told about both.

    A failed DoD returns before the empty-model branch, so if the render were emitted there the
    quieter defect would be invisible on exactly the runs that have more than one problem. Measured
    on the 38-workbook estate, that was the actual situation.
    """
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="failed")))
    _unlanded_model(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_DOD_FAILED
    assert "EMPTY_MODEL" in capsys.readouterr().out


def test_the_empty_model_verdict_is_persisted_for_later_steps(tmp_path: Path) -> None:
    """The deployer runs in a different process and must not have to re-derive this."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="warn")))
    _unlanded_model(out)

    run_estate.main(_slice_only_argv(out))

    verdict = json.loads((out / "empty-model-check.json").read_text(encoding="utf-8"))
    assert verdict["status"] == "EMPTY_MODELS"
    assert verdict["models"][0]["owner"] == "global_superstores_db"


# ---------------------------------------------------------------------------
# The blank-placeholder gate: a bundle whose report consumes a calc the engine refused
# ---------------------------------------------------------------------------

_REFUSED_CALC = {
    "category": "type_or_shape_mismatch",
    "fallback_reason": "IFNULL arguments return inconsistent types",
    "has_suggestion": False,
    "name": "Last Usage Filter",
    "role": "dimension",
    "target_table": "UDP_SF",
}


def _placeholder_report(dod_status: str = "warn", workbook: str = "Alpha") -> dict:
    """A report.json in the engine's real shape whose one workbook carries a refused calc.

    `pbip_folder` is the engine's own name for the folder the workbook built, and is what the
    checker keys the correlation on; it is carried here because a fixture that omits it would test
    a shape the engine does not emit.
    """
    wb = _workbook(workbook, workbook, requests=[dict(_REFUSED_CALC)])
    wb["pbip_folder"] = f"pbip/{workbook}/{workbook}.pbip"
    return _report(workbooks=[wb], dod_status=dod_status)


def _placeholder_model(out: Path, workbook: str = "Alpha") -> None:
    """The other half of the correlation: the BLANK()-only column the engine emitted instead."""
    tables = out / "pbip" / workbook / f"{workbook}.SemanticModel" / "definition" / "tables"
    _write(tables / "UDP_SF.tmdl", "table UDP_SF\n\n\tcolumn 'Last Usage Filter' = BLANK()\n\t\tsummarizeBy: none\n")


def _report_consuming_placeholder(out: Path, workbook: str = "Alpha") -> None:
    """A shipping PBIR page whose filter depends on the placeholder - the blocking case."""
    page = out / "pbip" / workbook / f"{workbook}.Report" / "definition" / "pages" / "p1"
    _write(page / "page.json", json.dumps({"name": "p1", "displayName": "Overview"}))
    _write(
        page / "visuals" / "v1" / "visual.json",
        json.dumps(
            {
                "name": "v1",
                "visual": {"visualType": "tableEx"},
                "filterConfig": {
                    "filters": [
                        {
                            "name": "f1",
                            "field": {
                                "Column": {
                                    "Expression": {"SourceRef": {"Entity": "UDP_SF"}},
                                    "Property": "Last Usage Filter",
                                }
                            },
                            "type": "Categorical",
                        }
                    ]
                },
            }
        ),
    )


def _without_pbir_validator(monkeypatch) -> None:
    """Run as if Node/the first-party validator were absent, which `check_pbir_valid` supports.

    Not a convenience: PBIR validity OUTRANKS this gate, and these fixtures are hand-written PBIR
    fragments rather than whole reports, so on a machine that has the CLI the run would stop at
    EXIT_INVALID_PBIR and never reach the branch under test. Patching `find_cli` exercises the real
    `scan` down its real SKIPPED path instead of substituting a fake verdict.
    """
    import check_pbir_valid  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(check_pbir_valid, "find_cli", lambda *_args, **_kwargs: None)


def test_a_report_referenced_blank_placeholder_blocks_a_bundle_on_its_first_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The FRESH-RUN case, and the one that matters: no `handover/` folder exists yet.

    `<bundle>/handover/` is not engine output - `slice_handovers` writes it in phase 3, while this
    gate runs in phase 2. A gate that reads the slices therefore sees nothing on a first run and
    passes the bundle, then blocks on a SECOND run over the same `--output` folder. Measured on
    identical bytes: exit 0 / "OK - 0 placeholder(s)" first, exit 8 second.

    So the assertion that the folder is absent BEFORE the run and present after is the test, not
    scenery: it pins the phase ordering that made the evidence unreadable, and it is what forces
    the correlation to come from `report.json`.
    """
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_placeholder_report()))
    _placeholder_model(out)
    _report_consuming_placeholder(out)
    assert not (out / "handover").exists(), "fixture is not a fresh run if the slices already exist"

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_BLANK_PLACEHOLDER
    assert (out / "handover").is_dir(), "phase 3 writes the slices AFTER the gate that needed them"
    printed = capsys.readouterr().out
    assert "BLANK-PLACEHOLDER CHECK: REFERENCED" in printed
    assert "Last Usage Filter" in printed


def test_the_blank_placeholder_verdict_is_printed_even_when_the_definition_of_done_already_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Precedence is DoD-first, but the reader must still be told about both."""
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_placeholder_report(dod_status="failed")))
    _placeholder_model(out)
    _report_consuming_placeholder(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_DOD_FAILED
    assert "BLANK-PLACEHOLDER CHECK: REFERENCED" in capsys.readouterr().out


def test_the_blank_placeholder_verdict_is_persisted_for_later_steps(tmp_path: Path, monkeypatch) -> None:
    """The triage step runs in a different process and must not have to re-derive this."""
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_placeholder_report()))
    _placeholder_model(out)
    _report_consuming_placeholder(out)

    run_estate.main(_slice_only_argv(out))

    verdict = json.loads((out / "blank-placeholder-check.json").read_text(encoding="utf-8"))
    assert verdict["status"] == "REFERENCED"
    assert verdict["placeholders_referenced"] == 1
    assert verdict["findings"][0]["owner"] == "Alpha"
    assert verdict["findings"][0]["name"] == "Last Usage Filter"


def test_an_unreadable_handover_input_cannot_silence_the_other_gates(tmp_path: Path, monkeypatch, capsys) -> None:
    """One truncated JSON file used to take the whole coordinator down, with the wrong exit code.

    `GateResults(...)` evaluates this gate before the empty-model one and prints all three verdicts
    afterwards, so an exception here meant NO verdict was printed at all and Python exited 1 - which
    in this script's vocabulary is EXIT_ENGINE_FAILED, "the engine itself exited non-zero".

    The report.json here deliberately does not carry the engine's `workbooks` list, which is what
    sends the checker to its `handover/` fallback and so makes the corrupt slice reachable at all.
    """
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps({"tool": "tableau-fabric-skills"}))
    _write(
        out / "handover" / "Alpha.json",
        json.dumps({"workbook": {"model_translation_handoff": {"requests": [dict(_REFUSED_CALC)]}}}),
    )
    _write(out / "handover" / "Truncated.json", '{"workbook": {')
    _placeholder_model(out)
    _report_consuming_placeholder(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_BLANK_PLACEHOLDER, "the readable slice's evidence was lost with the corrupt one"
    printed = capsys.readouterr().out
    assert "EMPTY-MODEL CHECK" in printed, "a corrupt input silenced a sibling gate"
    assert "handover/Truncated.json" in printed
    verdict = json.loads((out / "blank-placeholder-check.json").read_text(encoding="utf-8"))
    assert verdict["handover_unreadable"] == 1
    assert verdict["handover_unreadable_paths"] == ["handover/Truncated.json"]


# ---------------------------------------------------------------------------
# Slicing: the estate report must never enter a per-workbook agent's context
# ---------------------------------------------------------------------------


def test_each_workbook_gets_its_own_slice(tmp_path: Path) -> None:
    """~14 KB/workbook measured, so a 29-workbook estate is ~400 KB of mostly-irrelevant context."""
    report = _report(workbooks=[_workbook("Alpha", "AlphaModel"), _workbook("Beta", "BetaModel")])
    written = run_estate.slice_handovers(report, tmp_path)
    assert len(written) == 2
    names = {p.stem for p in written}
    assert names == {"Alpha", "Beta"}


def test_a_slice_carries_its_own_workbook_and_no_sibling(tmp_path: Path) -> None:
    """A slice that leaked a sibling would defeat the point of slicing."""
    report = _report(workbooks=[_workbook("Alpha", "AlphaModel"), _workbook("Beta", "BetaModel")])
    run_estate.slice_handovers(report, tmp_path)
    alpha = json.loads((tmp_path / "handover" / "Alpha.json").read_text(encoding="utf-8"))
    assert alpha["workbook"]["name"] == "Alpha"
    assert "Beta" not in json.dumps(alpha)


def test_a_slice_keeps_the_estate_facts_a_workbook_agent_needs(tmp_path: Path) -> None:
    """Slicing must not strip the gates - an agent that cannot see them cannot offer them."""
    gates = [{"gate": "dashboard_audit", "count": 3}]
    report = _report(workbooks=[_workbook("Alpha", "AlphaModel")], gates=gates)
    run_estate.slice_handovers(report, tmp_path)
    alpha = json.loads((tmp_path / "handover" / "Alpha.json").read_text(encoding="utf-8"))
    assert alpha["estate"]["pending_gates"] == gates
    assert alpha["estate"]["definition_of_done_status"] == "pass"


def test_a_workbook_name_with_path_characters_cannot_escape_the_folder(tmp_path: Path) -> None:
    """Workbook names come from a customer file name, so they are untrusted input."""
    report = _report(workbooks=[_workbook("../../evil", "M")])
    written = run_estate.slice_handovers(report, tmp_path)
    assert len(written) == 1
    assert written[0].parent == tmp_path / "handover"


# ---------------------------------------------------------------------------
# Phase timings
# ---------------------------------------------------------------------------


def test_phase_timings_are_persisted_with_a_total(tmp_path: Path) -> None:
    """Not a telemetry system - the session store already has tokens and duration per turn.

    What it cannot know is which migration PHASE a turn belonged to. This supplies only that, and it
    is what lets the retrospective say "where did the time go" instead of "what did we learn".
    """
    phases = [
        {"phase": "engine_run", "elapsed_sec": 120.0, "exit_code": 0},
        {"phase": "slice_handovers", "elapsed_sec": 0.4, "count": 6},
    ]
    path = run_estate.write_phase_record(tmp_path, phases)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["total_elapsed_sec"] == 120.4
    assert [p["phase"] for p in data["phases"]] == ["engine_run", "slice_handovers"]


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_a_missing_report_is_a_loud_failure(tmp_path: Path) -> None:
    """A silent empty result here would be indistinguishable from a clean estate."""
    with pytest.raises(FileNotFoundError, match="no report.json"):
        run_estate.read_report(tmp_path)


def test_the_coordinator_never_emits_model_content() -> None:
    """Architectural guard: this script runs the engine and reads its report. It is not a migrator.

    If it ever starts writing TMDL or PBIR the tier split has been violated, and that is far easier
    to catch here than in review.
    """
    source = Path(run_estate.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # skip the module docstring, which explains these very terms
    for forbidden in ("write_model_folder", "write_local_pbip", ".tmdl", "visual.json"):
        assert forbidden not in body, f"coordinator must not emit model content ({forbidden})"


# ---------------------------------------------------------------------------
# issue #250: the destructive-re-run barrier is CHECKED now, not merely documented
#
# The docstring claimed "this script owns that ordering so no agent has to remember it" and owned
# nothing: every gate ran in phase 2, reading output the engine had already written. A landing
# re-run into a bundle holding ~20 items of hand-authored fix work destroyed all of it with no
# --force, no prompt and no pre-check. These tests drive `main()` so that a guard moved back after
# the engine, or quietly turned into an opt-in, fails them.
# ---------------------------------------------------------------------------


def _versioned_engine(root: Path, version: str) -> Path:
    """An engine tree whose VERSION is what `engine_provenance` reads back off disk."""
    skill = root / "skills" / "tableau-migration"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (skill / "VERSION").write_text(version + "\n", encoding="utf-8")
    (scripts / "migrate_estate.py").write_text(
        """
import json
import re
import sys
from pathlib import Path

class LocalFilesSource:
    def __init__(self, root):
        self.root = Path(root)

    def _files(self, suffixes):
        if self.root.is_file():
            return [self.root] if self.root.suffix.lower() in suffixes else []
        return sorted(
            path for path in self.root.rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes
        )

    def list_datasources(self):
        return self._files({".tds", ".tdsx"})

    def list_workbooks(self):
        return self._files({".twb", ".twbx"})

    def asset_name(self, asset_id):
        return Path(asset_id).stem

def _safe_folder(name, used):
    base = re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]', "_", name).strip().rstrip(".") or "datasource"
    candidate = base
    number = 2
    while candidate.casefold() in used:
        candidate = f"{base}_{number}"
        number += 1
    used.add(candidate.casefold())
    return candidate

if __name__ == "__main__":
    source = LocalFilesSource(sys.argv[2])
    used = set()
    names = [
        _safe_folder(source.asset_name(asset), used)
        for asset in (*source.list_datasources(), *source.list_workbooks())
    ]
    print(json.dumps(names))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return root


def _bundle_engine(calls: list[Path] | None = None):
    """A stand-in engine emitting a stable, realistic bundle: a model, a PBIR report and a .pbip.

    DESTRUCTIVE on purpose. `migrate_estate.py` rmtree()s the `.SemanticModel` folder, the whole
    `.pbip` project dir and `<name>.Report` before rewriting them, so a stand-in that merely
    overwrites the files it happens to know about would let a test claim "nothing was destroyed"
    while a real engine had eaten the sentinel beside them. Wiping `pbip/` first is what lets a test
    assert on the DISK rather than only on a call log.

    `calls` is how a test proves the guard is PRE-engine: a refusal must leave it empty.
    """

    def _fake(_engine: Path, _src: Path, dest: Path, _dax: Path | None) -> tuple[int, str]:
        if calls is not None:
            calls.append(dest)
        shutil.rmtree(dest / "pbip", ignore_errors=True)
        shutil.rmtree(dest / "data", ignore_errors=True)
        _write(dest / "report.json", json.dumps(_report()))
        _write(dest / "input_manifest.json", '{"inputs": []}')
        model = dest / "pbip" / "WB" / "WB.SemanticModel"
        _write(model / "definition.pbism", "{}")
        _write(model / "definition" / "tables" / "Orders.tmdl", "table Orders")
        report = dest / "pbip" / "WB" / "WB.Report"
        _write(report / "definition.pbir", "{}")
        _write(report / "definition" / "report.json", '{"pages": []}')
        _write(dest / "pbip" / "WB" / "WB.Data" / "orders.txt", "id,amount\n1,10\n")
        _write(dest / "data" / "orders.csv", "id,amount\n1,10\n")
        _write(dest / "pbip" / "WB" / "WB.pbip", "{}")
        return 0, ""

    return _fake


ORDERS_TMDL = "pbip/WB/WB.SemanticModel/definition/tables/Orders.tmdl"
REPORT_JSON = "pbip/WB/WB.Report/definition/report.json"
TEXTSCAN_DATA = "pbip/WB/WB.Data/orders.txt"


def _landing_argv(engine: Path, src: Path, out: Path, *extra: str) -> list[str]:
    return [
        "--engine",
        str(engine),
        "--allow-noncanonical-engine",
        "--input",
        str(src),
        "--output",
        str(out),
        *extra,
    ]


def _first_run(tmp_path: Path, monkeypatch, version: str = "2.339.0") -> tuple[Path, Path, Path]:
    """Build the bundle the way a real run builds it, so both baselines are the real ones."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", version)
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    return engine, src, out


def _relanding(monkeypatch) -> list[Path]:
    """Re-arm the stand-in engine for a SECOND run and return the call log."""
    calls: list[Path] = []
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine(calls))
    return calls


def test_a_landing_rerun_into_a_pristine_bundle_still_proceeds(tmp_path: Path, monkeypatch) -> None:
    """The documented one-run landing flow must survive the guard (issue #250, DoD 2).

    A guard that blocked every re-run would be trivially "safe" and would break the exact workflow
    the barrier exists to protect. Both baselines are re-hashed here against a bundle nothing has
    touched, so a false positive fails this test rather than an operator's estate.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    calls = _relanding(monkeypatch)
    dax = _write(tmp_path / "approved.json", json.dumps({"Rank": "RANKX(...)"}))

    code = run_estate.main(_landing_argv(engine, src, out, "--approved-dax", str(dax)))

    assert code == run_estate.EXIT_OK
    assert calls == [out], "a pristine bundle must still be re-runnable"


def test_hand_authored_work_in_the_bundle_refuses_the_landing_rerun(tmp_path: Path, monkeypatch, capsys) -> None:
    """The reported case: bulk approved DAX landed into a bundle holding manual fix work.

    The engine's own stale-output guard exempts `--approved-dax`, so nothing upstream refuses this.
    `calls` is the load-bearing assertion: the refusal has to happen BEFORE the delete-and-recreate,
    not after it.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = SUM(Orders[Amount])\n", encoding="utf-8")
    calls = _relanding(monkeypatch)
    dax = _write(tmp_path / "approved.json", json.dumps({"Rank": "RANKX(...)"}))

    code = run_estate.main(_landing_argv(engine, src, out, "--approved-dax", str(dax)))

    assert code == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == [], "the engine ran anyway - the barrier must be PRE-engine"
    assert ORDERS_TMDL in capsys.readouterr().out, "a refusal must name what would be destroyed"


def test_a_hand_edited_report_file_is_work_the_receipt_catches(tmp_path: Path, monkeypatch) -> None:
    """PBIR JSON is now receipt-backed engine output; changing it is downstream work."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    assert REPORT_JSON in {record["path"] for record in receipt["artifacts"]}

    (out / REPORT_JSON).write_text('{"pages": [{"name": "p1"}]}', encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_new_artifact_under_pbip_counts_as_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """An agent-authored model file the engine never wrote is work too, not just an edit."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Custom.tmdl", "table Custom")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_newly_authored_pbir_file_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """HIGH 1: PBIR is `.json`, which the engine receipt's suffix allowlist does not record.

    Addition-detection used to run off that allowlist, so a hand-authored page or visual was
    invisible - while the engine deletes the whole `.Report` directory around it. The barrier now
    allowlists LOCATIONS, so anything inside a folder the engine rmtree()s is accounted for.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    authored = out / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "p1" / "visuals" / "v1"
    sentinel = _write(authored / "visual.json", json.dumps({"name": "v1"}))
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    assert not any(record["path"].endswith("visual.json") for record in receipt["artifacts"])
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file(), "the authored PBIR file was destroyed by a run the barrier let through"


def test_a_textscan_extract_beside_the_project_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """HIGH 2: `.txt` is in neither the receipt's suffix list nor the generated-artifact baseline.

    A packaged Tableau `textscan` datasource lands as flat files under `<project>.Data` and `data/`,
    both of which the engine deletes. Format allowlists cannot cover this; location coverage can.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    assert TEXTSCAN_DATA not in {record["path"] for record in receipt["artifacts"]}
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    assert TEXTSCAN_DATA not in manifest[run_estate.GENERATED_ARTIFACTS_KEY]["files"]
    assert TEXTSCAN_DATA in manifest[run_estate.ENGINE_TREE_KEY]["files"]

    (out / TEXTSCAN_DATA).write_text("id,amount\n1,10\n2,99\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_new_flat_file_under_data_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """The addition shape of the same gap: a landed extract the engine never wrote."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "data" / "hand-landed.txt", "id,amount\n7,70\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_slice_only_backfill_cannot_bless_downstream_work_as_engine_output(tmp_path: Path, monkeypatch) -> None:
    """HIGH 5: `--slice-only` hashes the WORKING COPY, downstream edits included.

    It writes that as `generated_artifacts` with `coverage: "slice_only_backfill"`. Trusting it
    would launder an agent's edits into "engine output" and hand the next destructive run a clean
    bill of health - one step removed from where the barrier was looking. The marker is now read,
    the baseline is not trusted, and the bundle stays indeterminate until acknowledged.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "report.json", json.dumps(_report()))
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    generated = manifest[run_estate.GENERATED_ARTIFACTS_KEY]
    assert generated["coverage"] == run_estate.SLICE_ONLY_COVERAGE
    assert "pbip/WB/WB.SemanticModel/definition/tables/Hand.tmdl" in generated["files"], (
        "fixture no longer reproduces the laundering shape - the backfill must have hashed the edit"
    )
    assert run_estate.ENGINE_TREE_KEY not in manifest, "--slice-only must not write an engine-output tree"

    calls = _relanding(monkeypatch)
    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_deleted_engine_artifact_counts_as_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """A bundle missing something the receipt attests to is no longer the bundle that was measured."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).unlink()
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_accepting_the_rewrite_proceeds_and_the_bundle_records_what_was_destroyed(tmp_path: Path, monkeypatch) -> None:
    """DoD 3: the opt-out works, and the ARTIFACT says the loss was deliberate.

    The record is written before the engine runs and lives at the bundle root, which the engine's
    rmtree sites do not touch - so it survives the rewrite it describes.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-bundle-rewrite"))

    assert code == run_estate.EXIT_OK
    assert calls == [out]
    record = json.loads((out / run_estate.BUNDLE_REWRITE_RECORD).read_text(encoding="utf-8"))
    assert len(record["records"]) == 1
    assert record["records"][0]["accepted_bundle_rewrite"] is True
    assert ORDERS_TMDL in record["records"][0]["destroyed"]["modified"]


def test_a_bundle_with_no_baseline_blocks_rather_than_reporting_clean(tmp_path: Path, monkeypatch, capsys) -> None:
    """HIGH 3: a pre-receipt or third-party bundle cannot be assessed, so it must not be waved through.

    The first cut treated "no baseline" as "no drift" and destroyed a sentinel at exit 0, told apart
    from a real pass only by warning TEXT. Unassessable is now its own blocking state; the flag is
    how a legacy bundle stays usable, which is exactly what the flag is for.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "report.json", json.dumps(_report()))
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file(), "the sentinel was destroyed by a run the barrier let through"
    assert "CANNOT ASSESS" in capsys.readouterr().out


def test_an_unassessable_bundle_is_recoverable_with_both_acknowledgements(tmp_path: Path, monkeypatch) -> None:
    """The escape hatch has to work, or the barrier bricks every bundle built before it existed."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "report.json", json.dumps(_report()))
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    calls = _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-bundle-rewrite", "--accept-engine-version-change"))

    assert code == run_estate.EXIT_OK
    assert calls == [out]
    record = json.loads((out / run_estate.BUNDLE_REWRITE_RECORD).read_text(encoding="utf-8"))
    assert record["records"][0]["coverage_complete"] is False
    assert record["records"][0]["coverage_gaps"], "an acknowledgement must record what could not be assessed"


def test_emptied_baselines_block_exactly_like_missing_ones(tmp_path: Path, monkeypatch) -> None:
    """HIGH 3, second shape: `artifacts: []` and `files: {}` attest to nothing.

    They previously behaved identically to a real pass and were distinguished only by warning text,
    never by exit code - which is the same defect wearing a different hat.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    receipt["artifacts"] = []
    (out / run_estate.ENGINE_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    manifest[run_estate.GENERATED_ARTIFACTS_KEY]["files"] = {}
    manifest[run_estate.ENGINE_TREE_KEY]["files"] = {}
    (out / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_an_emptied_baseline_does_not_invent_a_file_list(tmp_path: Path, monkeypatch) -> None:
    """Blocking is right; naming phantom victims is not.

    With no trustworthy baseline every file in the bundle is ambiguous, so listing them as "added"
    would imply the rest had been cleared. The block comes from the coverage gap alone.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    receipt["artifacts"] = []
    (out / run_estate.ENGINE_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    (out / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-bundle-rewrite", "--accept-engine-version-change"))

    assert code == run_estate.EXIT_OK
    record = json.loads((out / run_estate.BUNDLE_REWRITE_RECORD).read_text(encoding="utf-8"))["records"][0]
    assert record["destroyed"] == {"modified": [], "added": [], "missing": []}
    assert record["coverage_gaps"]


def test_a_bundle_built_by_a_different_engine_version_is_not_rewritten_by_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """2.113.0 and 2.126.0 are not interchangeable (#107) - rewriting in place mixes both."""
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    printed = capsys.readouterr().out
    assert "2.141.0" in printed and "2.260.0" in printed

    assert run_estate.main(_landing_argv(newer, src, out, "--accept-engine-version-change")) == run_estate.EXIT_OK
    assert calls == [out]


def test_the_same_engine_version_is_not_a_finding(tmp_path: Path, monkeypatch) -> None:
    """The version guard must not fire on the ordinary case, or it is noise that gets flagged away."""
    engine, src, out = _first_run(tmp_path, monkeypatch, version="2.339.0")
    same = _versioned_engine(tmp_path / "engine-copy", "2.339.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(same, src, out)) == run_estate.EXIT_OK
    assert calls == [out]
    assert engine != same


def test_accepting_an_engine_version_change_does_not_waive_the_destruction_guard(tmp_path: Path, monkeypatch) -> None:
    """The reason these are two flags and not one.

    A single acknowledgement would mean an operator who knows the engine moved silently also waives
    the guard on downstream work they did not know was there - which moves the failure boundary
    instead of removing it.
    """
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out, "--accept-engine-version-change")) == (
        run_estate.EXIT_BUNDLE_REWRITE
    )
    assert calls == []


def test_accepting_the_rewrite_does_not_waive_the_engine_version_guard(tmp_path: Path, monkeypatch) -> None:
    """The mirror image: accepting the loss of work says nothing about mixing engine versions."""
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out, "--accept-bundle-rewrite")) == (
        run_estate.EXIT_BUNDLE_REWRITE
    )
    assert calls == []


def test_slice_only_still_works_against_a_bundle_full_of_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """`--slice-only` legitimately points at an EXISTING bundle on every invocation.

    It never invokes the engine (see `resolve_run_engine`), so there is no delete-and-recreate to
    guard against. The call log is the load-bearing assertion and the reason this test was rewritten:
    asserting only `EXIT_OK` passed even when `--slice-only` was mutated into running the engine,
    because the exit code cannot tell "skipped the engine" from "ran it and it worked".
    """
    _, _, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Custom.tmdl", "table C")
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK
    assert calls == [], "--slice-only ran the engine, which is the destructive path it exists to avoid"
    assert sentinel.is_file(), "downstream work was destroyed by a --slice-only run"
    assert not (out / run_estate.BUNDLE_REWRITE_RECORD).exists()


def test_a_desktop_refresh_sidecar_is_not_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """A normal refresh writes `.pbi/cache.abf`; blocking on that would train operators to flag past it.

    The `.pbi` model file is the sharp case: it carries an artifact suffix the receipt DOES record
    elsewhere, so only the explicit volatile-folder exclusion keeps it out of the accounting.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / ".pbi" / "cache.abf", "refresh cache")
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / ".pbi" / "unapplied" / "Orders.tmdl", "table Orders")
    _write(out / "pbip" / "WB" / "WB.Report" / ".pbi" / "localSettings.json", "{}")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    assert calls == [out]


def test_a_replay_script_nested_under_a_destructive_root_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """`_build/` is this repo's durable replay-script convention, not scratch.

    AGENTS.md requires "every edit re-runnable from `_build/`", so `pbip/<project>/_build/replay.py`
    is exactly where an agent's re-runnable work lives - and it sits inside a directory the engine
    rmtree()s. The barrier used to borrow the generated-artifact manifest's SCRATCH predicate, which
    answers a different question, and so walked straight past it: a re-run destroyed the replay
    script for the very edits it reproduces, at exit 0.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "pbip" / "WB" / "_build" / "replay.py", "# re-runnable edit for this unit\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file(), "the replay script was destroyed by a run the barrier let through"


@pytest.mark.parametrize("scratch_dir", sorted(run_estate.SCRATCH_DIRS))
def test_no_scratch_component_survives_inside_a_destructive_root(tmp_path: Path, monkeypatch, scratch_dir: str) -> None:
    """`_build` was the reported case; the predicate had excluded the whole set.

    Parametrised over the live constant rather than a copied list, so growing `SCRATCH_DIRS` cannot
    silently re-open the hole for a name nobody thought to re-test.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "pbip" / "WB" / scratch_dir / "work.py", "# agent work\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_bundle_root_build_folder_is_not_guarded(tmp_path: Path, monkeypatch) -> None:
    """The other side of the boundary: the fix must not over-reach into what the engine never deletes.

    `<bundle>/_build/` sits outside `ENGINE_TREE_ROOTS`, survives a re-run untouched, and is where
    replay scripts for the estate as a whole live. Guarding it would refuse every second run of a
    bundle whose declared edits were recorded correctly - the scope is the destructive roots, not the
    folder name.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "_build" / "replay.py", "# estate-level re-runnable edit\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    assert calls == [out]
    assert sentinel.is_file()


def test_a_receipt_that_attests_to_nothing_is_not_read_as_a_clean_bundle(tmp_path: Path, monkeypatch, capsys) -> None:
    """An empty `artifacts` list is an absence of evidence, not evidence of absence."""
    engine, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    receipt["artifacts"] = []
    (out / run_estate.ENGINE_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    (out / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert "lists no usable artifacts" in capsys.readouterr().out
    assert engine != newer


def test_a_truncated_receipt_blocks_the_version_guard_rather_than_passing_it(tmp_path: Path, monkeypatch) -> None:
    """HIGH 4: one broken byte used to disable the engine-version guard entirely.

    A malformed receipt parses to None, `recorded_version` becomes None, and "is it different?"
    silently answered "no". Unknown is indeterminate now and blocks - and the rewrite flag must not
    answer it, because "I accept losing my work" says nothing about which engine rebuilds it.
    """
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    receipt_path = out / run_estate.ENGINE_RECEIPT
    receipt_path.write_text(receipt_path.read_text(encoding="utf-8")[:40], encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert run_estate.main(_landing_argv(newer, src, out, "--accept-bundle-rewrite")) == (
        run_estate.EXIT_BUNDLE_REWRITE
    )
    assert calls == []


def test_an_engine_tree_with_no_version_is_indeterminate_not_unchanged(tmp_path: Path, monkeypatch) -> None:
    """The mirror case: if THIS run's engine has no VERSION, nothing can be compared either."""
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    nameless = tmp_path / "engine-no-version"
    (nameless / "skills" / "tableau-migration" / "scripts").mkdir(parents=True)
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(nameless, src, out)) == run_estate.EXIT_PATH_CEILING
    assert calls == []


def test_a_slice_only_backfill_is_distrusted_even_when_the_other_baselines_look_fine(
    tmp_path: Path, monkeypatch
) -> None:
    """Defence in depth for HIGH 5, isolated from the coverage gap that usually fires first.

    In today's flows a `slice_only_backfill` block only ever coexists with a MISSING tree, so the
    tree gap blocks first and the distrust never gets to speak - which is precisely how a redundant
    check rots. This constructs the shape directly: a bundle whose tree and receipt are intact, and
    whose `generated_artifacts` came from a backfill. The backfill's hashes are not evidence of
    engine origin no matter what sits beside them, so the bundle stays indeterminate.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    hand = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    relative = "pbip/WB/WB.SemanticModel/definition/tables/Hand.tmdl"
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    manifest[run_estate.ENGINE_TREE_KEY]["files"][relative] = run_estate.sha256_file(hand)
    manifest[run_estate.GENERATED_ARTIFACTS_KEY] = {
        "version": 1,
        "coverage": run_estate.SLICE_ONLY_COVERAGE,
        "files": {relative: run_estate.sha256_file(hand)},
    }
    (out / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_legacy_bundle_with_a_receipt_but_no_tree_cannot_clear_an_added_file(tmp_path: Path, monkeypatch) -> None:
    """The realistic HIGH 1 shape: every bundle built before this barrier existed.

    Its receipt is valid, its generated-artifact baseline is valid, and the engine has not moved -
    so nothing else raises a finding. Only the missing tree makes an ADDED file undecidable, and
    only saying so blocks. Suppressing that one gap turns this bundle back into a silent exit 0.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    del manifest[run_estate.ENGINE_TREE_KEY]
    (out / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    authored = out / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "p1" / "visuals" / "v1"
    sentinel = _write(authored / "visual.json", json.dumps({"name": "v1"}))
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_corrupt_receipt_still_blocks_when_only_the_version_change_was_accepted(tmp_path: Path, monkeypatch) -> None:
    """ "I accept a possible engine change" is not "I accept not knowing what is in the bundle".

    With the version half acknowledged, an unreadable receipt is the ONLY remaining finding - so
    this is what proves the receipt gap carries its own weight rather than riding on the version
    guard that usually fires alongside it.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / run_estate.ENGINE_RECEIPT).write_text("{ truncated", encoding="utf-8")
    calls = _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-engine-version-change"))

    assert code == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_an_existing_but_non_bundle_output_folder_is_not_treated_as_a_bundle(tmp_path: Path, monkeypatch) -> None:
    """The over-reach guard: `--output` may legitimately be a folder that simply already exists.

    An operator's scratch directory holds nothing the engine wrote, so there is nothing to protect
    and blocking would be noise on a first run. "Cannot assess" must block only where the engine has
    actually been.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "notes.md", "operator scratch, nothing the engine wrote")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    assert calls == [out]


def test_a_dry_run_reports_the_refusal_and_never_writes_an_acknowledgement(tmp_path: Path, monkeypatch) -> None:
    """`--dry-run` says what WOULD happen, so it must say "this would be refused" - and change nothing."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out, "--dry-run")) == run_estate.EXIT_BUNDLE_REWRITE
    assert run_estate.main(_landing_argv(engine, src, out, "--dry-run", "--accept-bundle-rewrite")) == (
        run_estate.EXIT_OK
    )
    assert calls == []
    assert not (out / run_estate.BUNDLE_REWRITE_RECORD).exists(), "a dry run must not write into the bundle"


# ---------------------------------------------------------------------------
# issue #564: the EMITTED tree, not the pre-conversion projection
#
# The projection above is fail-open by construction - it composes the canonical PBIR visual tail onto
# names knowable BEFORE conversion, and the path that actually breaches on the committed issue-194
# repro is an uncapped SEMANTIC-MODEL table filename it never models. These tests are about the
# measurement of what the engine ACTUALLY wrote, and about WHERE it sits in the run: after the output
# is recorded, before anything downstream consumes it.
#
# Long paths are never CREATED here, for the reason `tests/test_check_path_ceiling.py` states in its
# own docstring: a stock Windows runner cannot create a 260-unit path at all, so the fixture rather
# than the assertion would fail. A tight ceiling over a short tree walks the identical comparison
# code. The shipped 259/247 pair is pinned separately, filesystem-free, below.
# ---------------------------------------------------------------------------


def _ceilings(file_ceiling: int, dir_ceiling: int) -> run_estate.Limits:
    """Tight limits for a short tree - same code path, no unwritable fixture."""
    return run_estate.Limits(file_ceiling=file_ceiling, dir_ceiling=dir_ceiling, warn_at=max(file_ceiling, 1) - 1)


def _emitted_run(
    tmp_path: Path,
    monkeypatch,
    limits: run_estate.Limits | None = None,
) -> tuple[int, str, list[Path], Path]:
    """A full run through `main` with the stand-in engine. Returns (code, stdout, provenance calls, bundle).

    `stamp_inputs` is the SENTINEL: it is the first thing that runs after the gate, so a mutation that
    deletes the gate or moves it later lets the sentinel fire on an over-ceiling tree.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    stamped: list[Path] = []
    monkeypatch.setattr(
        run_estate,
        "stamp_inputs",
        lambda _input, out_dir, *_timeout: (
            stamped.append(out_dir)
            or run_estate.ProvenanceStampResult(True, "local_only", "fixture provenance published")
        ),
    )
    if limits is not None:
        monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", limits)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_estate.main(_landing_argv(engine, src, out))
    return code, buffer.getvalue(), stamped, out


def _path_report(out: Path) -> dict:
    return json.loads((out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8"))


def _phase_names(out: Path) -> list[str]:
    return [phase["phase"] for phase in json.loads((out / "phase-timings.json").read_text(encoding="utf-8"))["phases"]]


def _minimal_bundle(out: Path) -> Path:
    """A bundle with report.json and one conditional folder only - no semantic_models/, no data/."""
    _write(out / "report.json", json.dumps(_report(workbooks=[_workbook("Alpha", "AlphaModel")])))
    _write(out / "pbip" / "Alpha" / "Alpha.pbip", "{}")
    return out


def test_an_over_ceiling_emitted_tree_refuses_before_any_consumer_sees_it(tmp_path: Path, monkeypatch) -> None:
    """THE issue-564 shape: the projection passed, the emitted tree is unopenable, everything was green.

    The exit code is asserted last: it is the weakest signal. What matters is that the refusal names
    the binding path and that the first downstream consumer never ran.
    """
    code, printed, stamped, out = _emitted_run(tmp_path, monkeypatch, _ceilings(utf16_len(str(tmp_path)), 4096))

    assert "PATH CEILING:" in printed and "EMITTED path(s) exceed" in printed, printed
    match = re.search(
        r"binding (?P<kind>file|directory) is (?P<length>\d+) UTF-16 units \(ceiling (?P<ceiling>\d+)\): (?P<path>\S+)",
        printed,
    )
    assert match, f"the refusal did not name the binding path.\nprinted:\n{printed}"
    assert int(match.group("length")) > int(match.group("ceiling"))
    named = match.group("path").rstrip(".")
    assert named.startswith(f"{run_estate.SAFE_BUNDLE_ROOT}/"), (
        f"the refusal named {named!r}, which is not the bundle-relative spelling"
    )
    tail = named[len(run_estate.SAFE_BUNDLE_ROOT) + 1 :]
    assert (out / Path(tail)).is_file(), (
        f"the refusal named {tail!r}, which is not a real path inside the bundle it judged"
    )
    assert str(out) not in printed and str(tmp_path) not in printed, "the refusal printed the absolute run root"
    assert stamped == [], "provenance ran on a tree Power BI Desktop cannot open"
    report = _path_report(out)
    assert report["status"] == "over_ceiling" and report["counted"]["over_ceiling"] >= 1
    assert code == run_estate.EXIT_PATH_CEILING


def test_a_clean_emitted_tree_continues_unchanged(tmp_path: Path, monkeypatch) -> None:
    """The false-positive control: the SAME run at the real ceilings must reach every later phase."""
    code, printed, stamped, out = _emitted_run(tmp_path, monkeypatch)

    assert code == run_estate.EXIT_OK, printed
    assert stamped == [out], "a clean tree must not stop the run"
    assert _path_report(out)["status"] == "ok"
    assert "none over Desktop" in printed, printed
    assert {"provenance", "adjudicate", "slice_handovers"} <= set(_phase_names(out))


def test_one_overlong_directory_refuses_even_when_every_file_is_legal(tmp_path: Path, monkeypatch) -> None:
    """Measured in `check_path_ceiling`: an overlong DIRECTORY makes `git add` drop its contents at exit 0."""
    code, printed, stamped, out = _emitted_run(tmp_path, monkeypatch, _ceilings(4096, utf16_len(str(tmp_path))))

    assert "binding directory is" in printed, printed
    assert stamped == [], "provenance ran on a tree whose directories git itself would silently drop"
    report = _path_report(out)
    assert {record["kind"] for record in report["worst_offenders"]} == {"directory"}
    assert report["counted"]["over_ceiling"] == report["counted"]["directories"], (
        "a file was counted as an offender in a directory-only fixture"
    )
    assert code == run_estate.EXIT_PATH_CEILING


def test_the_emitted_measurement_counts_utf16_units_not_code_points(tmp_path: Path) -> None:
    """A non-BMP character is 1 code point and 2 UTF-16 units - Desktop counts the second number."""
    out = tmp_path / "bundle"
    target = out / "pbip" / "x" / "visual\U0001f600.json"
    _write(target)
    units = utf16_len(str(target))
    assert units == len(str(target)) + 1, "the fixture no longer carries a supplementary character"

    refused, detail = run_estate.check_emitted_path_ceiling(out, [], _ceilings(units - 1, 4096))
    assert refused is False, detail
    assert "EMITTED path(s) exceed" in detail
    passed, clean = run_estate.check_emitted_path_ceiling(out, [], _ceilings(units, 4096))
    assert passed is True, clean


def test_a_walk_failure_cannot_report_a_clean_tree(tmp_path: Path, monkeypatch) -> None:
    """The walker raising outright is an indeterminate state, never a pass."""
    out = _minimal_bundle(tmp_path / "bundle")

    def _boom(*_args, **_kwargs):
        raise OSError(5, "device is not ready")

    monkeypatch.setattr(run_estate, "scan_path_ceiling", _boom)
    monkeypatch.setattr(
        run_estate, "stamp_inputs", lambda *_a, **_k: pytest.fail("provenance ran after a walk failure")
    )

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_PATH_CEILING
    printed = buffer.getvalue()
    assert "CANNOT ASSESS the emitted tree" in printed
    assert run_estate.SCAN_UNASSESSABLE_CODE in printed and "class=OSError" in printed, printed
    assert "device is not ready" not in printed, "a raw exception message was printed"
    report = _path_report(out)
    assert report["status"] == "unknown_paths"
    assert report["scan_error_code"] == run_estate.SCAN_UNASSESSABLE_CODE
    assert report["scan_error_facts"]["class"] == "OSError" and report["scan_error_facts"]["errno"] == 5
    assert "scan_error" not in report, "the free-form message is persisted again"
    assert not (out / "handover").exists(), "handover slices were written after a refusal"


def test_a_path_the_walker_could_not_measure_refuses(tmp_path: Path, monkeypatch) -> None:
    """The walker's OWN unknown classification binds - this gate never re-decides it."""
    import check_path_ceiling  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    out = _minimal_bundle(tmp_path / "bundle")
    monkeypatch.setattr(
        check_path_ceiling,
        "collect",
        lambda _root: ([], [{"path": str(out / "pbip"), "reason": "PermissionError: access is denied"}]),
    )

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_PATH_CEILING
    printed = buffer.getvalue()
    assert run_estate.UNKNOWN_PATH_CODE.format(index=1) in printed, printed
    assert "access is denied" not in printed, "the walker's free-form reason was printed"
    report = _path_report(out)
    assert report["status"] == "unknown_paths"
    assert report["unknown_paths"][0]["code"] == run_estate.UNKNOWN_PATH_CODE.format(index=1)
    assert "reason" not in report["unknown_paths"][0], "the free-form reason is persisted again"


def test_an_output_tree_with_nothing_measurable_is_not_clean(tmp_path: Path) -> None:
    """`census` of nothing must not read as a pass (the same rule the issue-194 harness enforces)."""
    empty = tmp_path / "out"
    empty.mkdir()

    proceed, detail = run_estate.check_emitted_path_ceiling(empty, [])

    assert proceed is False
    assert "nothing was measured" in detail, detail
    assert _path_report(empty)["status"] == "no_paths"


def test_a_bundle_without_the_conditional_engine_folders_is_clean(tmp_path: Path) -> None:
    """`semantic_models/` and `data/` are conditional output - their absence is not a finding."""
    out = _minimal_bundle(tmp_path / "bundle")

    assert run_estate.main(_slice_only_argv(out)) == run_estate.EXIT_OK
    assert _path_report(out)["status"] == "ok"
    assert (out / "handover" / "Alpha.json").is_file()


def test_slice_only_output_is_gated_too(tmp_path: Path, monkeypatch) -> None:
    """`--slice-only` never runs the engine, but it hands an EXISTING tree downstream all the same."""
    out = _minimal_bundle(tmp_path / "bundle")
    monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", _ceilings(utf16_len(str(tmp_path)), 4096))

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_PATH_CEILING
    assert not (out / "handover").exists()
    assert _phase_names(out) == ["slice_only_baseline_backfill", "path_ceiling"], (
        "a slice-only refusal ran a phase it should not have"
    )


def test_the_phase_record_after_a_refusal_carries_no_later_phase(tmp_path: Path, monkeypatch) -> None:
    """The timings ARE the evidence that nothing downstream started."""
    _code, _printed, _stamped, out = _emitted_run(tmp_path, monkeypatch, _ceilings(utf16_len(str(tmp_path)), 4096))

    names = _phase_names(out)
    assert names[-1] == "path_ceiling", names
    assert {"engine_run", "engine_receipt"} <= set(names), names
    assert not {"provenance", "adjudicate", "slice_handovers"} & set(names), names


def test_a_refusal_preserves_the_engine_output_and_what_built_it(tmp_path: Path, monkeypatch) -> None:
    """Permanent shortening is an upstream fix - here the tree is EVIDENCE, so nothing is removed."""
    code, _printed, _stamped, out = _emitted_run(tmp_path, monkeypatch, _ceilings(utf16_len(str(tmp_path)), 4096))

    assert code == run_estate.EXIT_PATH_CEILING
    assert (out / ORDERS_TMDL).read_text(encoding="utf-8") == "table Orders"
    assert (out / REPORT_JSON).is_file() and (out / "pbip" / "WB" / "WB.pbip").is_file()
    assert (out / run_estate.ENGINE_RECEIPT).is_file(), "the refused bundle can no longer say what built it"
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    assert manifest[run_estate.GENERATED_ARTIFACTS_KEY]["files"], "the baseline was lost with the refusal"


def test_an_engine_failure_keeps_its_precedence_and_writes_no_path_report(tmp_path: Path, monkeypatch) -> None:
    """A failed engine has no output to judge - the path gate must not claim one."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    out = tmp_path / "bundle"
    monkeypatch.setattr(run_estate, "run_engine", lambda *_args: (1, "engine exploded"))
    monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", _ceilings(1, 1))

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_ENGINE_FAILED
    assert not (out / run_estate.PATH_CEILING_REPORT).exists()


def test_no_path_report_is_written_when_there_is_no_output_to_measure(tmp_path: Path, monkeypatch) -> None:
    """A bundle with no report.json fails loudly upstream of this gate, and leaves no verdict behind."""
    out = tmp_path / "bundle"
    out.mkdir()
    monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", _ceilings(1, 1))

    with pytest.raises(FileNotFoundError):
        run_estate.main(_slice_only_argv(out))

    assert not (out / run_estate.PATH_CEILING_REPORT).exists()


def test_a_verdict_that_cannot_be_recorded_is_not_a_pass(tmp_path: Path, monkeypatch) -> None:
    """An unattributable verdict is an unassessable one: refuse rather than continue on hearsay."""
    out = _minimal_bundle(tmp_path / "bundle")
    monkeypatch.setattr(run_estate, "write_path_ceiling_report", lambda *_args: None)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_PATH_CEILING
    assert "could not be written" in buffer.getvalue()


def test_the_gate_uses_the_measured_desktop_ceilings_and_keeps_the_root_budget_advisory() -> None:
    """Filesystem-free pin: loosening the defaults, or promoting the advisory, has to fail here."""
    assert run_estate.PATH_CEILING_LIMITS.file_ceiling == FILE_CEILING == 259
    assert run_estate.PATH_CEILING_LIMITS.dir_ceiling == DIR_CEILING == 247
    assert run_estate.PATH_CEILING_LIMITS.min_root_budget is None, (
        "the tight portable root budget is ADVISORY here; gating on it is a different decision"
    )


def test_a_tight_root_budget_is_reported_and_never_refuses() -> None:
    """The portable-budget advisory stays advisory - it is about WHERE a bundle lands, not this tree.

    Driven through the verdict directly rather than through a fixture, because a tight root budget on
    a CLEAN tree requires an absolute root under ~40 units, which no `tmp_path` on any runner has.
    """
    report = {
        "status": "ok",
        "root": "bundle",
        "counted": {"measured": 4, "files": 2, "directories": 2, "over_ceiling": 0, "unknown": 0},
        "file_ceiling": FILE_CEILING,
        "dir_ceiling": DIR_CEILING,
        "root_budget": 12,
        "root_budget_is_tight": True,
        "shipping_root_budget_advisory": 40,
    }

    proceed, detail = run_estate.path_ceiling_verdict(report, Path("bundle") / run_estate.PATH_CEILING_REPORT)

    assert proceed is True, detail
    assert "ADVISORY: root budget 12" in detail, detail
    assert "not a refusal" in detail, detail


# ---------------------------------------------------------------------------
# The refusal's own evidence contract (blind-review findings on PR #587)
#
# Three properties of the REFUSAL, none of which is about path length:
#   1. publishing the report is atomic - a failure cannot destroy a previous trustworthy one;
#   2. exit 10 outranks its own evidence - failing to persist timings must not change the verdict;
#   3. what is persisted and printed is SHAREABLE - bundle-relative, with no host location in it.
# ---------------------------------------------------------------------------

TRUSTWORTHY_REPORT = b'{"status": "ok", "written_by": "a previous run"}\n'

#: A run root that is BOTH profile-shaped and carries a customer-identifying folder, so a leak of
#: either half is detectable rather than inferred.
SECRET_ACCOUNT = "j.doe"
SECRET_FOLDER = "AcmeCorp-Confidential-FY26Q3"


def _secret_rooted_bundle(tmp_path: Path) -> Path:
    """A bundle whose ABSOLUTE path discloses an account name and a customer folder."""
    out = tmp_path / "Users" / SECRET_ACCOUNT / SECRET_FOLDER / "bundle"
    return _minimal_bundle(out)


def _strings(payload) -> list[str]:
    """Every string anywhere in a JSON document, keys included."""
    if isinstance(payload, dict):
        return [k for k in payload if isinstance(k, str)] + [s for v in payload.values() for s in _strings(v)]
    if isinstance(payload, list):
        return [s for v in payload for s in _strings(v)]
    return [payload] if isinstance(payload, str) else []


def _half_writing_open(monkeypatch, suffix: str = ".tmp") -> None:
    """Make writes to `*<suffix>` fail after emitting half their bytes - a real partial write."""
    real_open = builtins.open

    class _HalfWriter:
        def __init__(self, handle):
            self._handle = handle

        def write(self, text):
            self._handle.write(text[: len(text) // 2])
            raise OSError(28, "No space left on device")

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._handle.close()
            return False

    def _open(file, *args, **kwargs):
        handle = real_open(file, *args, **kwargs)
        return _HalfWriter(handle) if str(file).endswith(suffix) else handle

    monkeypatch.setattr(builtins, "open", _open)


# ---------------------------------------------------------------------------
# issue #576: source provenance is an atomic, binding artifact
# ---------------------------------------------------------------------------


def _structured_provenance(status: str, code: str | None = None) -> dict:
    errors = [{"code": code, "operation": "collect-inputs"}] if code else []
    return {
        "schema": "tableau-source-provenance/1",
        "stamped_at": "2026-09-10T00:00:00Z",
        "input_count": 0,
        "inputs": [],
        "phase": {"status": status, "errors": errors},
    }


def _collected(monkeypatch, result: dict, *, total: int | None = None) -> None:
    """Stand in for the supervised leaf worker (#576) with one canned structured result.

    The computation moved into a spawned process, so patching `prov.build` in THIS process no longer
    reaches it. These tests are about what the parent PUBLISHES and VERDICTS, so they inject at the
    supervisor seam instead; the worker lifecycle itself is proved separately, with a real spawn.
    """
    records = result.get("inputs")
    completed = len(records) if isinstance(records, list) else 0
    outcome = run_estate.ProvenanceOutcome(
        result=result,
        completed=completed,
        total=completed if total is None else total,
        worker_pid=None,
        worker_alive=False,
        worker_exitcode=0,
        expired=False,
    )
    monkeypatch.setattr(run_estate, "collect_provenance", lambda *_args, **_kwargs: outcome)


def test_an_empty_structured_provenance_result_is_published_and_refuses(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    _collected(monkeypatch, _structured_provenance("empty", "empty-input"))

    stamped = run_estate.stamp_inputs(tmp_path, out)

    artifact = json.loads((out / run_estate.SOURCE_PROVENANCE_REPORT).read_text(encoding="utf-8"))
    assert artifact["phase"]["status"] == "empty"
    assert artifact["phase"]["errors"][0]["code"] == "empty-input"
    assert stamped == run_estate.ProvenanceStampResult(
        False,
        "empty",
        f"0 input(s) stamped, 0 confirmed against the site (empty) -> {run_estate.SAFE_SOURCE_PROVENANCE_REPORT}",
    )


def test_a_build_exception_becomes_a_safe_published_failure(tmp_path: Path, monkeypatch) -> None:
    """The worker's typed build failure is published verbatim - and carries no message text."""
    import stamp_tableau_provenance as prov  # noqa: PLC0415

    out = tmp_path / "bundle"
    out.mkdir()
    secret = str(tmp_path / "customer-secret")
    _collected(monkeypatch, prov.failure_result("build-failed", "build", OSError(5, secret)))

    stamped = run_estate.stamp_inputs(tmp_path, out)
    raw = (out / run_estate.SOURCE_PROVENANCE_REPORT).read_text(encoding="utf-8")
    artifact = json.loads(raw)

    assert stamped.ok is False and stamped.status == "failed"
    assert artifact["phase"] == {
        "status": "failed",
        "errors": [
            {
                "code": "build-failed",
                "operation": "build",
                "exception_class": "OSError",
                "errno": 5,
            }
        ],
    }
    assert secret not in raw


def test_a_publication_failure_is_a_non_success_stamp(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    _collected(monkeypatch, _structured_provenance("local_only"))
    monkeypatch.setattr(run_estate, "write_source_provenance", lambda *_args: None)

    stamped = run_estate.stamp_inputs(tmp_path, out)

    assert stamped.ok is False
    assert stamped.status == "publication_failed"


@pytest.mark.parametrize("bad_value", [object(), float("nan"), float("inf")])
def test_unserializable_or_nonfinite_provenance_never_corrupts_the_prior_artifact(
    tmp_path: Path, bad_value: object
) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    previous = out / run_estate.SOURCE_PROVENANCE_REPORT
    previous.write_bytes(TRUSTWORTHY_REPORT)

    assert run_estate.write_source_provenance(out, {"bad": bad_value}) is None
    assert previous.read_bytes() == TRUSTWORTHY_REPORT
    assert not list(out.glob("*.tmp"))


def test_a_partial_provenance_write_leaves_the_prior_artifact_byte_identical(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    previous = out / run_estate.SOURCE_PROVENANCE_REPORT
    previous.write_bytes(TRUSTWORTHY_REPORT)
    _half_writing_open(monkeypatch)

    assert run_estate.write_source_provenance(out, _structured_provenance("success")) is None
    assert previous.read_bytes() == TRUSTWORTHY_REPORT
    assert not list(out.glob("*.tmp"))


def test_a_failed_provenance_replace_leaves_the_prior_artifact_byte_identical(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    previous = out / run_estate.SOURCE_PROVENANCE_REPORT
    previous.write_bytes(TRUSTWORTHY_REPORT)

    def fail(*_args):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(run_estate.os, "replace", fail)

    assert run_estate.write_source_provenance(out, _structured_provenance("success")) is None
    assert previous.read_bytes() == TRUSTWORTHY_REPORT
    assert not list(out.glob("*.tmp"))


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        ("partial", "live-lookup-refused"),
        ("partial", "worker-reap-failed"),
        ("failed", "collect-inputs-failed"),
        ("failed", "worker-start-failed"),
        ("failed", "worker-protocol-invalid"),
    ],
)
def test_a_provenance_failure_stops_before_adjudication_and_handover(
    tmp_path: Path, monkeypatch, status: str, error_code: str
) -> None:
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    _collected(
        monkeypatch,
        {
            "schema": "tableau-source-provenance/1",
            "stamped_at": "2026-09-10T00:00:00Z",
            "input_count": 0 if status == "failed" else 1,
            "inputs": [] if status == "failed" else [{"input": {"file": "unit.twb", "sha256": "digest"}}],
            "phase": {
                "status": status,
                "errors": [{"code": error_code, "operation": "collect-inputs" if status == "failed" else "sign-in"}],
            },
        },
    )
    monkeypatch.setattr(
        run_estate,
        "check_pbir_validity",
        lambda *_args: pytest.fail("adjudication ran after provenance failure"),
    )

    exit_code = run_estate.main(_landing_argv(engine, src, out))

    assert exit_code == run_estate.EXIT_PROVENANCE_FAILED
    phase = json.loads((out / run_estate.SOURCE_PROVENANCE_REPORT).read_text(encoding="utf-8"))["phase"]
    assert phase["status"] == status and phase["errors"][0]["code"] == error_code
    assert not (out / "handover").exists()
    names = _phase_names(out)
    assert "provenance" in names
    assert not {"adjudicate", "slice_handovers"} & set(names)


# ---------------------------------------------------------------------------
# Blind-review correction on PR #594
#
# Two findings, both about what the phase says rather than what it does:
#   1. the console/log line named the artifact by its ABSOLUTE path, so the run root - drive,
#      account name, customer folder - left the machine on the success line AND the refusal line.
#      Every other shareable string in this module is bundle-relative; this one was not.
#   2. a result that CONTRADICTS ITSELF read as a pass. `input_count: 0` with `inputs: []` and a
#      `success`/`local_only` status describes no input at all, and the verdict was taken from
#      `phase.status` alone, so a run that stamped nothing continued to adjudication and handover.
# ---------------------------------------------------------------------------


def _provenance_input(name: str = "unit.twb") -> dict:
    return {"input": {"file": name, "sha256": "d" * 64}}


def _honest_local_only(count: int = 1) -> dict:
    """The result the correction must LEAVE ALONE: local-only, positive count, matching list."""
    return {
        "schema": "tableau-source-provenance/1",
        "stamped_at": "2026-09-10T00:00:00Z",
        "input_count": count,
        "inputs": [_provenance_input(f"unit{index}.twb") for index in range(count)],
        "phase": {"status": "local_only", "errors": []},
    }


def _stamp_of(monkeypatch, tmp_path: Path, out: Path, result: dict) -> run_estate.ProvenanceStampResult:
    """`stamp_inputs` over one canned structured result, with no worker and no site."""
    _collected(monkeypatch, result)
    out.mkdir(parents=True, exist_ok=True)
    return run_estate.stamp_inputs(tmp_path, out)


def _provenance_artifact(out: Path) -> dict:
    return json.loads((out / run_estate.SOURCE_PROVENANCE_REPORT).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (_honest_local_only(), "local_only"),
        (_structured_provenance("empty", "empty-input"), "empty"),
        (_structured_provenance("failed", "collect-inputs-failed"), "failed"),
    ],
)
def test_the_provenance_line_names_the_artifact_bundle_relatively(
    tmp_path: Path, monkeypatch, result: dict, expected_status: str
) -> None:
    """Success AND failure: the detail names `<bundle>/source-provenance.json`, never a host path."""
    out = tmp_path / "Users" / SECRET_ACCOUNT / SECRET_FOLDER / "bundle"

    stamped = _stamp_of(monkeypatch, tmp_path, out, result)

    assert stamped.status == expected_status
    assert f"-> {run_estate.SAFE_SOURCE_PROVENANCE_REPORT}" in stamped.detail, stamped.detail
    assert str(out) not in stamped.detail, stamped.detail
    assert SECRET_FOLDER not in stamped.detail and SECRET_ACCOUNT not in stamped.detail, stamped.detail
    assert _provenance_artifact(out)["phase"]["status"] == expected_status


def test_the_success_log_line_carries_no_host_path(tmp_path: Path, monkeypatch, caplog) -> None:
    """The line emitted on the PASSING path - the one a run emits every time - is shareable too."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    _collected(monkeypatch, _honest_local_only())

    with caplog.at_level("INFO", logger="run_estate"), contextlib.redirect_stdout(io.StringIO()):
        exit_code = run_estate.main(_landing_argv(engine, src, out))

    provenance_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PROVENANCE:")]
    assert exit_code != run_estate.EXIT_PROVENANCE_FAILED
    assert provenance_lines, [r.getMessage() for r in caplog.records]
    logged = "\n".join(provenance_lines)
    assert run_estate.SAFE_SOURCE_PROVENANCE_REPORT in logged, logged
    assert str(out) not in logged, logged
    assert str(tmp_path) not in logged, logged


def test_the_refusal_console_line_carries_no_host_path(tmp_path: Path, monkeypatch) -> None:
    """`main`'s printed refusal names the artifact bundle-relatively, not by its location on disk."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    _collected(monkeypatch, _structured_provenance("failed", "collect-inputs-failed"))

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = run_estate.main(_landing_argv(engine, src, out))
    printed = buffer.getvalue()

    assert exit_code == run_estate.EXIT_PROVENANCE_FAILED
    assert "PROVENANCE_FAILED" in printed
    assert run_estate.SAFE_SOURCE_PROVENANCE_REPORT in printed, printed
    assert str(out) not in printed, printed
    assert str(tmp_path) not in printed, printed


#: Every way a structured result can contradict itself, with the fault code it must be recorded as.
#: `input_count` is the field a consumer counts on, so a bool, a string, a negative number and a
#: count that disagrees with the list beside it are all named separately rather than collapsed.
CONTRADICTORY_RESULTS = {
    "success-with-no-inputs": ({"input_count": 0, "inputs": [], "status": "success"}, "success-without-inputs"),
    "local_only-with-no-inputs": ({"input_count": 0, "inputs": [], "status": "local_only"}, "success-without-inputs"),
    "negative-count": ({"input_count": -1, "inputs": [], "status": "success"}, "input-count-negative"),
    "bool-count": (
        {"input_count": True, "inputs": [_provenance_input()], "status": "success"},
        "input-count-not-an-integer",
    ),
    "string-count": (
        {"input_count": "1", "inputs": [_provenance_input()], "status": "success"},
        "input-count-not-an-integer",
    ),
    "count-exceeds-list": (
        {"input_count": 2, "inputs": [_provenance_input()], "status": "success"},
        "input-count-mismatch",
    ),
    "list-exceeds-count": (
        {"input_count": 1, "inputs": [_provenance_input(), _provenance_input("b.twb")], "status": "local_only"},
        "input-count-mismatch",
    ),
    "inputs-not-a-list": ({"input_count": 1, "inputs": {"unit.twb": {}}, "status": "success"}, "inputs-not-a-list"),
}


def _contradictory(shape: dict) -> dict:
    return {
        "schema": "tableau-source-provenance/1",
        "stamped_at": "2026-09-10T00:00:00Z",
        "input_count": shape["input_count"],
        "inputs": shape["inputs"],
        "phase": {"status": shape["status"], "errors": [{"code": "live-lookup-refused", "operation": "sign-in"}]},
    }


@pytest.mark.parametrize(("shape", "fault"), list(CONTRADICTORY_RESULTS.values()), ids=list(CONTRADICTORY_RESULTS))
def test_a_self_contradictory_result_publishes_a_failure_and_refuses(
    tmp_path: Path, monkeypatch, shape: dict, fault: str
) -> None:
    """One artifact is still published - and it is not success-shaped, and the verdict is False."""
    out = tmp_path / "bundle"

    stamped = _stamp_of(monkeypatch, tmp_path, out, _contradictory(shape))

    artifact = _provenance_artifact(out)
    assert stamped.ok is False, stamped
    assert artifact["phase"]["status"] not in {"success", "local_only"}, artifact
    assert artifact["phase"]["status"] == stamped.status == "failed", artifact
    codes = [error["code"] for error in artifact["phase"]["errors"]]
    assert fault in codes, codes
    assert "live-lookup-refused" in codes, "the errors the build already recorded were discarded"
    assert [error["operation"] for error in artifact["phase"]["errors"] if error["code"] == fault] == [
        "validate-result"
    ], artifact
    assert artifact["input_count"] == len(artifact["inputs"]), artifact
    assert len(list(out.glob("source-provenance*"))) == 1, list(out.iterdir())


def test_the_verdict_does_not_rest_on_normalization_having_rewritten_the_status(tmp_path: Path, monkeypatch) -> None:
    """Defence in depth: normalisation stubbed to a passthrough, the verdict must STILL refuse.

    Without this the verdict would be `phase.status in SUCCESS_STATUSES` - correct only for as long
    as something upstream keeps rewriting the status, which is exactly the assumption the reviewed
    defect made. The artifact here is deliberately left success-shaped so the only thing under test
    is whether the VERDICT reads the evidence or the claim.
    """
    import stamp_tableau_provenance as prov  # noqa: PLC0415

    monkeypatch.setattr(prov, "normalize_result", lambda result: result)
    out = tmp_path / "bundle"

    stamped = _stamp_of(monkeypatch, tmp_path, out, _contradictory(CONTRADICTORY_RESULTS["success-with-no-inputs"][0]))

    assert stamped.status == "success", "the passthrough stub is what makes this the status-only trap"
    assert _provenance_artifact(out)["phase"]["status"] == "success"
    assert stamped.ok is False, "the verdict was taken from phase.status alone"


def test_an_honest_local_only_result_is_published_unchanged_and_passes(tmp_path: Path, monkeypatch) -> None:
    """The control the correction must not break: positive count, matching list, no site available."""
    out = tmp_path / "bundle"
    honest = _honest_local_only(count=2)

    stamped = _stamp_of(monkeypatch, tmp_path, out, honest)

    assert stamped == run_estate.ProvenanceStampResult(
        True,
        "local_only",
        f"2 input(s) stamped, 0 confirmed against the site (local_only) -> {run_estate.SAFE_SOURCE_PROVENANCE_REPORT}",
    )
    assert _provenance_artifact(out) == honest


def test_a_contradictory_result_never_reaches_adjudication_or_handover(tmp_path: Path, monkeypatch) -> None:
    """The consequence the verdict exists for, measured through `main` rather than asserted."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    _collected(monkeypatch, _contradictory(CONTRADICTORY_RESULTS["success-with-no-inputs"][0]))
    monkeypatch.setattr(
        run_estate,
        "check_pbir_validity",
        lambda *_args: pytest.fail("adjudication ran on a result that stamped no input"),
    )

    exit_code = run_estate.main(_landing_argv(engine, src, out))

    assert exit_code == run_estate.EXIT_PROVENANCE_FAILED
    assert _provenance_artifact(out)["phase"]["status"] == "failed"
    assert not (out / "handover").exists()
    assert not {"adjudicate", "slice_handovers"} & set(_phase_names(out))


def test_a_partial_write_leaves_the_previous_report_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """A full disk mid-write must not turn a trustworthy report into a truncated one."""
    out = _minimal_bundle(tmp_path / "bundle")
    previous = out / run_estate.PATH_CEILING_REPORT
    previous.write_bytes(TRUSTWORTHY_REPORT)
    _half_writing_open(monkeypatch)

    written = run_estate.write_path_ceiling_report(out, {"status": "over_ceiling", "counted": {}})

    assert written is None, "a partial write reported success"
    assert previous.read_bytes() == TRUSTWORTHY_REPORT, "the previous report was destroyed by a failed write"
    assert not list(out.glob("*.tmp")), "the staging file was left behind"


def test_a_failed_swap_leaves_the_previous_report_byte_identical(tmp_path: Path, monkeypatch) -> None:
    """The other half of atomicity: the write completed, the replace did not."""
    out = _minimal_bundle(tmp_path / "bundle")
    previous = out / run_estate.PATH_CEILING_REPORT
    previous.write_bytes(TRUSTWORTHY_REPORT)

    def _boom(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(run_estate.os, "replace", _boom)

    written = run_estate.write_path_ceiling_report(out, {"status": "over_ceiling", "counted": {}})

    assert written is None
    assert previous.read_bytes() == TRUSTWORTHY_REPORT
    assert not list(out.glob("*.tmp")), "only this call's staging file may be removed, and it must be"


def test_an_unserializable_report_never_opens_the_final_file(tmp_path: Path) -> None:
    """Serialize FIRST: a document that cannot be rendered must not have truncated anything."""
    out = _minimal_bundle(tmp_path / "bundle")
    previous = out / run_estate.PATH_CEILING_REPORT
    previous.write_bytes(TRUSTWORTHY_REPORT)

    written = run_estate.write_path_ceiling_report(out, {"status": "ok", "when": object()})

    assert written is None
    assert previous.read_bytes() == TRUSTWORTHY_REPORT
    assert not list(out.glob("*.tmp"))


def test_a_path_refusal_survives_a_phase_record_failure(tmp_path: Path, monkeypatch) -> None:
    """Exit 10 outranks its own evidence: unwritable timings are secondary to an unopenable bundle."""

    def _boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(run_estate, "write_phase_record", _boom)

    code, printed, stamped, out = _emitted_run(tmp_path, monkeypatch, _ceilings(utf16_len(str(tmp_path)), 4096))

    assert code == run_estate.EXIT_PATH_CEILING, printed
    assert stamped == [], "provenance ran after a refusal whose timings could not be persisted"
    assert not (out / "handover").exists(), "a later phase ran after the refusal"
    assert not (out / "phase-timings.json").exists(), "the fixture did not actually block the write"


def test_the_published_report_carries_no_host_location(tmp_path: Path, monkeypatch) -> None:
    """The report is shared upstream, so the run root - account, customer folder - must not be in it."""
    out = _secret_rooted_bundle(tmp_path)
    monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", _ceilings(utf16_len(str(tmp_path)), 4096))

    proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert proceed is False, detail
    raw = (out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert SECRET_FOLDER not in raw and SECRET_ACCOUNT not in raw, "the report disclosed the run root"
    assert str(out) not in raw
    leaked = [value for value in _strings(payload) if discloses_host_location(value)]
    assert leaked == [], f"the report carries host location(s): {leaked}"
    assert payload["root"] == run_estate.SAFE_BUNDLE_ROOT
    assert payload["worst_offenders"], payload
    assert all(record["path"].startswith(f"{run_estate.SAFE_BUNDLE_ROOT}/") for record in payload["worst_offenders"])
    assert any("pbip/Alpha" in record["path"] for record in payload["worst_offenders"]), (
        "the relative tail was lost; the refusal has to stay actionable"
    )


def test_the_printed_refusal_carries_no_host_location(tmp_path: Path, monkeypatch) -> None:
    """A console line is what gets pasted into an issue - it may not carry the customer's root.

    ⚠️ The one drive-shaped string in the output is `_SHORT_ROOT_HINT`'s documented example command,
    a CONSTANT of this repository shared with the pre-conversion refusal - not data from this run.
    That is asserted directly rather than waved away, and everything else is judged strictly.
    """
    out = _secret_rooted_bundle(tmp_path)
    monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", _ceilings(utf16_len(str(tmp_path)), 4096))

    _proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert SECRET_FOLDER not in detail and SECRET_ACCOUNT not in detail, detail
    assert str(out) not in detail and str(tmp_path) not in detail, detail
    assert f"{run_estate.SAFE_BUNDLE_ROOT}/{run_estate.PATH_CEILING_REPORT}" in detail, (
        "the report must be named relatively, not by its absolute path"
    )
    residue = detail.replace(run_estate._SHORT_ROOT_HINT, "")
    assert not discloses_host_location(residue), residue


def test_an_error_message_embedding_the_bundle_path_is_never_persisted(tmp_path: Path, monkeypatch) -> None:
    """An OS error routinely quotes the path it failed on - so its message is not published at all.

    What survives is what can be read STRUCTURALLY off the exception: its class and its errno. There
    is no rewriting step to get wrong, which is the whole point of the simplification.
    """
    out = _secret_rooted_bundle(tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError(5, f"device is not ready: {out}")

    monkeypatch.setattr(run_estate, "scan_path_ceiling", _boom)

    proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert proceed is False
    raw = (out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8")
    assert SECRET_FOLDER not in raw and SECRET_ACCOUNT not in raw, raw
    assert SECRET_FOLDER not in detail and SECRET_ACCOUNT not in detail, detail
    assert "device is not ready" not in raw and "device is not ready" not in detail
    payload = json.loads(raw)
    assert "scan_error" not in payload
    assert payload["scan_error_code"] == run_estate.SCAN_UNASSESSABLE_CODE
    assert payload["scan_error_facts"] == {"class": "OSError", "errno": 5}
    assert [value for value in _strings(payload) if discloses_host_location(value)] == []


def test_a_path_outside_the_bundle_is_an_ordinal_not_an_echo(tmp_path: Path, monkeypatch) -> None:
    """Containment that cannot be PROVEN is reported as unassessable, never echoed or faked relative."""
    import check_path_ceiling  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    out = _minimal_bundle(tmp_path / "bundle")
    foreign = tmp_path / "Users" / SECRET_ACCOUNT / SECRET_FOLDER / "elsewhere.json"
    monkeypatch.setattr(
        check_path_ceiling,
        "collect",
        lambda _root: ([], [{"path": str(foreign), "reason": "PermissionError: access is denied"}]),
    )

    proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert proceed is False
    raw = (out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert SECRET_FOLDER not in raw and SECRET_FOLDER not in detail, raw
    assert payload["paths_not_placed"] == 1
    assert payload["unknown_paths"][0]["path"] == run_estate.UNASSESSABLE_PATH.format(index=1)
    assert payload["unknown_paths"][0]["code"] == run_estate.UNKNOWN_PATH_CODE.format(index=1)
    assert "access is denied" not in raw, "the walker's free-form reason is persisted again"


# ---------------------------------------------------------------------------
# The RELATIVE root, reported by the same reviewer (PR #587)
#
# Generic host-path detection answers "is there an ABSOLUTE location in here". A `--output` that is
# relative - `AcmeCorp-Confidential-FY26Q3\bundle` - names the customer just as plainly and walks
# straight past every absolute predicate in this repo. Structured path evidence was already safe (it
# is placed against the supplied root, relative or not); the unstructured strings beside it were not.
# ---------------------------------------------------------------------------

RELATIVE_ROOT = Path(SECRET_FOLDER) / "bundle"


def _relative_bundle(monkeypatch, tmp_path: Path) -> Path:
    """A bundle addressed by a RELATIVE, customer-named path, from inside `tmp_path`."""
    monkeypatch.chdir(tmp_path)
    return _minimal_bundle(Path(RELATIVE_ROOT))


def test_a_relative_root_does_not_survive_in_the_persisted_scan_error(tmp_path: Path, monkeypatch) -> None:
    """The reviewer's reproduction, on the report: a walk failure quoting a relative customer root.

    No rewriting: the message is not published at all. Its class and errno are.
    """
    out = _relative_bundle(monkeypatch, tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError(5, f"device is not ready: {out}")

    monkeypatch.setattr(run_estate, "scan_path_ceiling", _boom)

    proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert proceed is False
    raw = (out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert SECRET_FOLDER not in raw, raw
    assert SECRET_FOLDER not in detail, detail
    assert "scan_error" not in payload, "a free-form message is persisted again"
    assert payload["scan_error_code"] == run_estate.SCAN_UNASSESSABLE_CODE
    assert payload["scan_error_facts"] == {"class": "OSError", "errno": 5}


def test_a_relative_root_does_not_survive_in_an_unknown_path_reason(tmp_path: Path, monkeypatch) -> None:
    """The same reproduction on the OTHER unstructured field the walker fills."""
    import check_path_ceiling  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    out = _relative_bundle(monkeypatch, tmp_path)
    monkeypatch.setattr(
        check_path_ceiling,
        "collect",
        lambda root: (
            [],
            [{"path": str(Path(root) / "pbip"), "reason": f"PermissionError: cannot open {Path(root) / 'pbip'}"}],
        ),
    )

    proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert proceed is False
    raw = (out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8")
    payload = json.loads(raw)
    row = payload["unknown_paths"][0]
    assert SECRET_FOLDER not in raw and SECRET_FOLDER not in detail, raw
    assert "reason" not in row, "the walker's free-form reason is persisted again"
    assert row["code"] == run_estate.UNKNOWN_PATH_CODE.format(index=1)
    # The structured half still names WHERE, because it was placed by true containment.
    assert row["path"] == f"{run_estate.SAFE_BUNDLE_ROOT}/pbip", row


def test_a_relative_root_does_not_survive_a_publication_failure_diagnostic(tmp_path: Path, monkeypatch, caplog) -> None:
    """The third surface: the log line emitted when the report itself cannot be published."""
    out = _relative_bundle(monkeypatch, tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError(13, f"Permission denied: {out / run_estate.PATH_CEILING_REPORT}")

    monkeypatch.setattr(run_estate.os, "replace", _boom)

    with caplog.at_level("WARNING", logger="run_estate"):
        written = run_estate.write_path_ceiling_report(out, {"status": "ok", "counted": {}})

    assert written is None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET_FOLDER not in logged, logged
    assert "Permission denied" not in logged, "the exception's own message was logged"
    assert f"operation={run_estate.PUBLISH_REPORT_OPERATION}" in logged, logged
    assert "class=PermissionError" in logged and "errno=13" in logged, (
        f"the actionable class and error code were dropped with the message: {logged}"
    )


def test_a_foreign_path_in_a_message_is_never_published_only_its_class_and_code(tmp_path: Path, monkeypatch) -> None:
    """A path that is not the bundle's cannot be reasoned about - so no message is published at all."""
    out = _relative_bundle(monkeypatch, tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError(13, "cannot open OtherCustomer-Merger-Docs\\payroll.twbx")

    monkeypatch.setattr(run_estate, "scan_path_ceiling", _boom)

    proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert proceed is False
    raw = (out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert "OtherCustomer" not in raw and "payroll" not in raw, raw
    assert "OtherCustomer" not in detail and "payroll" not in detail, detail
    assert "scan_error" not in payload
    assert payload["scan_error_code"] == run_estate.SCAN_UNASSESSABLE_CODE
    assert payload["scan_error_facts"] == {"class": "PermissionError", "errno": 13}


def test_a_path_free_message_is_not_published_either(tmp_path: Path, monkeypatch) -> None:
    """The control that used to justify keeping prose: even a harmless message is not published.

    Its useful content survives structurally - `PermissionError`, `errno=13`, a stable code - which
    is the whole trade this simplification makes: no parser, no rewriting, nothing to get wrong.
    """
    out = _relative_bundle(monkeypatch, tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError(5, "device is not ready")

    monkeypatch.setattr(run_estate, "scan_path_ceiling", _boom)

    _proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    payload = json.loads((out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8"))
    assert "device is not ready" not in json.dumps(payload) and "device is not ready" not in detail
    assert payload["scan_error_code"] == run_estate.SCAN_UNASSESSABLE_CODE
    assert payload["scan_error_facts"] == {"class": "OSError", "errno": 5}
    assert run_estate.SCAN_UNASSESSABLE_CODE in detail and "class=OSError" in detail


# -- the prefix collision that ended the substitution approach -------------------------------------
#
# A root of `…\bundle` shares a PREFIX with its siblings `…\bundle-foreign` and `…\bundle2`. Any
# substring replacement rewrites those into `<bundle>-foreign` and `<bundle>2`, which reads as if
# they were inside the bundle - inventing containment that does not exist, out of a customer path.
# True path-component containment (`Path.is_relative_to`) refuses both, and there is no other path
# handling left for a collision to reach.
@pytest.mark.parametrize("sibling", ["bundle-foreign", "bundle2"])
def test_a_prefix_sharing_sibling_is_never_read_as_inside_the_bundle(tmp_path: Path, monkeypatch, sibling: str) -> None:
    import check_path_ceiling  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    out = _relative_bundle(monkeypatch, tmp_path)
    foreign = out.parent / sibling / "payroll.twbx"
    monkeypatch.setattr(
        check_path_ceiling,
        "collect",
        lambda _root: ([], [{"path": str(foreign), "reason": f"PermissionError: cannot open {foreign}"}]),
    )

    proceed, detail = run_estate.check_emitted_path_ceiling(out, [])

    assert proceed is False
    raw = (out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8")
    payload = json.loads(raw)
    for text in (raw, detail):
        assert sibling not in text, f"the sibling directory name leaked: {text}"
        assert SECRET_FOLDER not in text, text
        assert "payroll" not in text, text
        assert f"{run_estate.SAFE_BUNDLE_ROOT}-" not in text and f"{run_estate.SAFE_BUNDLE_ROOT}2" not in text, (
            "a prefix collision was rewritten as if it were inside the bundle"
        )
    assert payload["paths_not_placed"] == 1
    assert payload["unknown_paths"][0]["path"] == run_estate.UNASSESSABLE_PATH.format(index=1)
    assert payload["unknown_paths"][0]["code"] == run_estate.UNKNOWN_PATH_CODE.format(index=1)


def test_the_scan_verdict_and_structured_evidence_survive_the_code_only_diagnostics(
    tmp_path: Path, monkeypatch
) -> None:
    """Dropping free-form diagnostics must not weaken the verdict or the measured evidence beside it."""
    out = _relative_bundle(monkeypatch, tmp_path)
    monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", _ceilings(1, 4096))

    proceed, _detail = run_estate.check_emitted_path_ceiling(out, [])

    payload = json.loads((out / run_estate.PATH_CEILING_REPORT).read_text(encoding="utf-8"))
    assert proceed is False
    assert payload["status"] == "over_ceiling"
    assert payload["counted"]["over_ceiling"] >= 1
    assert payload["paths_not_placed"] == 0
    offender = payload["worst_offenders"][0]
    assert offender["length"] > offender["ceiling"] and offender["kind"] in {"file", "directory"}
    assert offender["path"].startswith(f"{run_estate.SAFE_BUNDLE_ROOT}/")
    assert (out / Path(offender["path"][len(run_estate.SAFE_BUNDLE_ROOT) + 1 :])).exists()


# ---------------------------------------------------------------------------
# issue #576: ONE leaf worker, ONE monotonic deadline, ONE parent publication
#
# The measured failure is a provenance phase that never returns: 66 harvested inputs cost 200 remote
# calls, and a trickled response body outlasts `urlopen(timeout=180)` because that bounds each socket
# read rather than the transfer. Local reads, ZIP member scans, the recursive scrub and sign-out have
# no bound at all. A thread cannot be stopped, a cooperative check cannot interrupt a blocking call
# and `Future.cancel()` returns False for a running task - all three were measured, all three are
# rejected. What is proved below is the surviving mechanism: the parent computes one absolute
# deadline BEFORE spawning one leaf worker, stops accepting IPC at expiry, terminates/kills/reaps it
# with bounded joins, and publishes exactly one honest artifact from the evidence it had accepted.
#
# Every test here spawns a REAL process (`spawn`, on every platform), because an in-process fake
# would prove the bookkeeping and nothing about preemption. The scenarios live in
# `tests/provenance_workers.py`: a spawn target is pickled by reference, so it has to be a top-level
# function in an importable module.
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so the SPAWNED child can import the fixtures

import provenance_workers  # noqa: E402  # pylint: disable=wrong-import-position

#: Long enough that a cold Windows spawn still gets its first messages out before the latch (measured
#: spawn cost on the development host: 0.46 s), short enough that seven of them stay quick.
WORKER_TIMEOUT_SEC = 4.0

#: Spawn is charged to the computation deadline. This additional allowance covers cold OS scheduling
#: and small local publication, not eight seconds of hidden cleanup after a four-second timeout.
SPAWN_ALLOWANCE_SEC = 1.0
ELAPSED_BOUND_SEC = (
    WORKER_TIMEOUT_SEC
    + run_estate.PROVENANCE_TERMINATE_JOIN_SEC
    + run_estate.PROVENANCE_KILL_JOIN_SEC
    + run_estate.PROVENANCE_RECEIVER_JOIN_SEC
    + SPAWN_ALLOWANCE_SEC
)


def _pid_is_running(pid: int) -> bool:
    """Ask the OPERATING SYSTEM whether that process still exists.

    `Process.is_alive()` is the object's opinion; after `terminate`/`kill`/`join` the interesting
    question is whether anything is still running under that PID, and only the OS can answer it.
    """
    if os.name == "nt":
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return f" {pid} " in listing.stdout or f"{pid}\t" in listing.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - it exists and is not ours
        return True
    return True


class _SupervisedRun(NamedTuple):
    stamped: run_estate.ProvenanceStampResult
    artifact: dict
    elapsed: float
    published: list[dict]
    outcome: run_estate.ProvenanceOutcome


def _supervise(
    monkeypatch,
    tmp_path: Path,
    entry,
    timeout_sec: float = WORKER_TIMEOUT_SEC,
) -> _SupervisedRun:
    """One real supervised phase against `entry`, through the production `stamp_inputs`.

    Only the worker TARGET is substituted. The deadline, the pipe, the protocol validation, the
    terminate/kill/reap and the publication are all the shipping ones.
    """
    out = tmp_path / "bundle"
    out.mkdir(parents=True, exist_ok=True)
    supervise = run_estate.collect_provenance
    seen: list[run_estate.ProvenanceOutcome] = []

    def collect(input_dir, timeout=timeout_sec, **_kwargs):
        outcome = supervise(input_dir, timeout, entry=entry)
        seen.append(outcome)
        return outcome

    publish = run_estate.write_source_provenance
    published: list[dict] = []

    def write(out_dir, result):
        published.append(result)
        return publish(out_dir, result)

    monkeypatch.setattr(run_estate, "collect_provenance", collect)
    monkeypatch.setattr(run_estate, "write_source_provenance", write)

    started = time.monotonic()
    stamped = run_estate.stamp_inputs(tmp_path, out, timeout_sec)
    elapsed = time.monotonic() - started
    assert len(published) == 1, "PUBLISH_COUNT: the parent must attempt publication exactly once"
    assert elapsed < ELAPSED_BOUND_SEC - WORKER_TIMEOUT_SEC + timeout_sec, (
        "COMPUTATION_ELAPSED: cleanup exceeded its allowance"
    )
    artifact_path = out / run_estate.SOURCE_PROVENANCE_REPORT
    artifact = json.loads(artifact_path.read_text(encoding="utf-8")) if artifact_path.exists() else {}
    return _SupervisedRun(stamped, artifact, elapsed, published, seen[0])


def _progress_events(printed: str) -> list[dict]:
    return [
        json.loads(line[len(run_estate.PROVENANCE_PROGRESS_PREFIX) :])
        for line in printed.splitlines()
        if line.startswith(run_estate.PROVENANCE_PROGRESS_PREFIX)
    ]


@pytest.mark.parametrize(
    "operation",
    ["collect-inputs", "fingerprint", "sign-in", "inventory", "content", "scrub", "sign-out"],
)
def test_a_worker_blocked_in_any_operation_class_is_preempted_at_the_deadline(
    tmp_path: Path, monkeypatch, capsys, operation: str
) -> None:
    """THE #576 claim, one operation class at a time: whatever it is stuck in, the phase ENDS.

    The parent is deliberately agnostic about WHICH operation blocked - it terminates a process - so
    the operation the artifact names is the last one the worker reported, which is what makes the
    record actionable rather than a bare "it timed out somewhere".
    """
    run = _supervise(monkeypatch, tmp_path, functools.partial(provenance_workers.blocks_in, operation))

    assert run.stamped.ok is False
    assert run.artifact["phase"]["status"] in {"partial", "failed"}
    assert run.artifact["phase"]["errors"][-1] == {
        "code": run_estate.PROVENANCE_DEADLINE_CODE,
        "operation": operation,
    }, run.artifact["phase"]
    assert run.artifact["input_count"] == len(run.artifact["inputs"]) == (0 if operation == "collect-inputs" else 2)
    if operation == "fingerprint":
        assert all(record["input"] == {"status": "unavailable"} for record in run.artifact["inputs"])
    elif operation != "collect-inputs":
        assert all(record["input"]["sha256"] for record in run.artifact["inputs"])
    assert len(run.published) == 1, "the artifact is published exactly once, by the parent"
    assert run.outcome.expired is True
    assert run.outcome.worker_alive is False and run.outcome.worker_exitcode is not None
    assert run.elapsed < ELAPSED_BOUND_SEC, f"the phase did not end: {run.elapsed:.1f}s"
    events = _progress_events(capsys.readouterr().out)
    assert [event["event"] for event in events].count("phase-finish") == 1
    assert events[-1]["status"] in {"partial", "failed"}


def test_a_terminal_result_that_arrives_after_the_deadline_is_never_accepted(tmp_path: Path, monkeypatch) -> None:
    """The fail-open shape this design exists to refuse: a late success is not a success.

    The worker sends a complete, well-formed SUCCESS result 30 s after a 4 s deadline. If the parent
    were still reading, the phase would report two happily stamped inputs it never saw finish.
    """
    run = _supervise(monkeypatch, tmp_path, provenance_workers.sends_a_late_terminal)

    raw = json.dumps(run.artifact)
    assert run.stamped.ok is False
    assert run.artifact["phase"]["status"] == "partial"
    assert run.artifact["phase"]["errors"][-1]["code"] == run_estate.PROVENANCE_DEADLINE_CODE
    assert "late-a.twb" not in raw and "late-b.twb" not in raw, "a post-deadline result was accepted"
    assert run.artifact["inputs"][0]["input"]["sha256"] == provenance_workers.CHECKPOINT_SHA
    assert run.elapsed < ELAPSED_BOUND_SEC


def test_a_worker_that_crashes_after_a_checkpoint_keeps_the_evidence_and_names_the_exit_code(
    tmp_path: Path, monkeypatch
) -> None:
    """A crash is its own fault code, and never the deadline's - the parent's latch is the only clock."""
    run = _supervise(monkeypatch, tmp_path, provenance_workers.crashes_after_a_checkpoint)

    error = run.artifact["phase"]["errors"][-1]
    assert run.stamped.ok is False
    assert run.artifact["phase"]["status"] == "partial"
    assert error["code"] == run_estate.PROVENANCE_CRASH_CODE
    assert error["exit_code"] == 7, error
    assert run.artifact["inputs"][0]["input"]["sha256"] == provenance_workers.CHECKPOINT_SHA
    assert run.artifact["inputs"][1] == {
        "input": {"status": "unavailable"},
        "fingerprint_error": {"code": run_estate.PROVENANCE_CRASH_CODE, "operation": "fingerprint"},
    }
    assert run.outcome.worker_alive is False
    assert run.outcome.expired is False, "a crash is not a timeout"
    assert run.elapsed < ELAPSED_BOUND_SEC


def test_a_stall_after_the_first_fingerprint_keeps_it_and_marks_only_the_second_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """Completed local evidence survives the kill; the unfinished input says so EXPLICITLY.

    Absent would be worse than unavailable: `input_count` must keep equalling `len(inputs)` (the
    identity #594's normalisation refuses to let a result contradict), and a reader must be able to
    tell "we did not get to this one" from "this one is fine".
    """
    run = _supervise(
        monkeypatch,
        tmp_path,
        functools.partial(provenance_workers.blocks_in_with_evidence, "fingerprint"),
    )

    kept, missing = run.artifact["inputs"]
    assert run.artifact["phase"]["status"] == "partial"
    assert kept["input"]["sha256"] == provenance_workers.CHECKPOINT_SHA
    assert "file" not in kept["input"], "a checkpoint may carry no copied string"
    assert missing["fingerprint_error"] == {
        "code": run_estate.PROVENANCE_DEADLINE_CODE,
        "operation": "fingerprint",
    }
    assert run.artifact["phase"]["errors"][-1]["operation"] == "fingerprint"


def test_a_hung_sign_out_after_the_safe_snapshot_keeps_the_complete_scrubbed_records(
    tmp_path: Path, monkeypatch
) -> None:
    """Sign-out is cleanup, and cleanup may not cost a finished run its evidence.

    Measured on the pre-cache code: a sign-out whose connection closed without a response discarded
    3 of 3 fingerprints. Here it hangs instead, which the old code could not survive at all.
    """
    run = _supervise(monkeypatch, tmp_path, provenance_workers.blocks_after_safe_snapshot)

    assert run.artifact["phase"]["status"] == "partial"
    assert run.artifact["input_count"] == 1
    assert run.artifact["inputs"][0]["input"]["file"] == "unit.twb", "the scrubbed snapshot is kept whole"
    assert run.artifact["phase"]["errors"][-1] == {
        "code": run_estate.PROVENANCE_DEADLINE_CODE,
        "operation": "sign-out",
    }
    assert run.stamped.ok is False


def test_publication_happens_once_after_a_deadline_and_a_failed_one_preserves_the_prior_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    """The artifact writer is the parent's, runs AFTER expiry, and #594's atomicity still holds."""
    out = tmp_path / "bundle"
    out.mkdir()
    previous = out / run_estate.SOURCE_PROVENANCE_REPORT
    previous.write_bytes(TRUSTWORTHY_REPORT)
    _half_writing_open(monkeypatch)

    run = _supervise(monkeypatch, tmp_path, functools.partial(provenance_workers.blocks_in, "content"))

    assert len(run.published) == 1, "exactly one publication attempt, timeout or not"
    assert run.stamped.status == "publication_failed" and run.stamped.ok is False
    assert previous.read_bytes() == TRUSTWORTHY_REPORT, "a failed publication harmed the prior artifact"
    assert not list(out.glob("*.tmp"))


def test_progress_output_carries_only_allowlisted_keys_and_nothing_from_the_environment(
    tmp_path: Path, monkeypatch, capsys, caplog
) -> None:
    """Progress is pasted into issues, so it is numbers and enums - never a payload, at any level.

    The worker here sends a legitimately scrubbed snapshot that still contains copied strings (that
    is what a scrubbed origin IS). Accepting it is correct; RENDERING it would be the leak.
    """
    secret = f"{SECRET_ACCOUNT}-{SECRET_FOLDER}-Superstore.twbx"
    with caplog.at_level("DEBUG"):
        run = _supervise(
            monkeypatch,
            tmp_path,
            functools.partial(provenance_workers.sends_a_secret_bearing_snapshot, secret),
        )

    printed = capsys.readouterr().out
    events = _progress_events(printed)
    logged = "\n".join(record.getMessage() for record in caplog.records)

    assert secret in json.dumps(run.artifact), "the snapshot itself is kept - only its RENDERING is refused"
    assert secret not in printed and secret not in logged, "a payload was rendered"
    assert SECRET_ACCOUNT not in printed and SECRET_FOLDER not in printed
    assert str(tmp_path) not in printed
    assert [event["event"] for event in events].count("phase-start") == 1
    assert [event["event"] for event in events].count("phase-finish") == 1
    assert events[0]["event"] == "phase-start" and events[-1]["event"] == "phase-finish"
    assert events[0]["timeout_sec"] == WORKER_TIMEOUT_SEC
    for event in events:
        assert set(event) <= {"event", "operation", "completed", "total", "status", "timeout_sec"}, event
        assert event["event"] in run_estate.PROVENANCE_PROGRESS_EVENTS
        assert event["operation"] in run_estate.PROVENANCE_PROGRESS_OPERATIONS
        assert isinstance(event["completed"], int) and event["completed"] >= 0
        assert event["total"] is None or (isinstance(event["total"], int) and event["total"] >= 0)
        assert "status" not in event or event["status"] in run_estate.PROVENANCE_PROGRESS_STATUSES
    fingerprint_counters = [e["completed"] for e in events if e["operation"] == "fingerprint"]
    assert fingerprint_counters == sorted(fingerprint_counters)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(provenance_workers.sends_an_unknown_message, id="unknown-kind"),
        pytest.param(provenance_workers.sends_invalid_pickle, id="invalid-pickle"),
        pytest.param(
            functools.partial(provenance_workers.sends_an_unsafe_checkpoint, "j.doe-Superstore-secret.twbx"),
            id="checkpoint-with-a-copied-string",
        ),
    ],
)
def test_a_message_outside_the_closed_protocol_is_refused_rather_than_interpreted(
    tmp_path: Path, monkeypatch, capsys, entry
) -> None:
    """Fail closed at the process boundary: the parent does not take the worker's word for safety.

    The checkpoint case is the one that matters for privacy - a mutation that checkpoints the RAW
    fingerprint (filename, member names) is caught here rather than published.
    """
    run = _supervise(monkeypatch, tmp_path, entry)

    printed = capsys.readouterr().out
    assert run.stamped.ok is False
    assert run.artifact["phase"]["errors"][-1]["code"] == run_estate.PROVENANCE_PROTOCOL_CODE
    assert run.artifact["phase"]["status"] == "failed", "a rejected message is not evidence"
    assert "secret.twbx" not in json.dumps(run.artifact) and "secret.twbx" not in printed


def test_a_successful_worker_result_is_published_unchanged_and_passes(tmp_path: Path, monkeypatch, capsys) -> None:
    """The positive control. Without it every assertion above could be satisfied by refusing always."""
    run = _supervise(monkeypatch, tmp_path, provenance_workers.succeeds)

    events = _progress_events(capsys.readouterr().out)
    assert run.stamped.ok is True and run.stamped.status == "local_only"
    assert run.artifact["input_count"] == 1
    assert run.artifact["phase"] == {"status": "local_only", "errors": []}
    assert run.outcome.expired is False and run.outcome.worker_alive is False
    assert events[-1] == {
        "event": "phase-finish",
        "operation": "phase",
        "completed": 1,
        "total": 1,
        "status": "local_only",
    }
    assert run.elapsed < ELAPSED_BOUND_SEC


@pytest.mark.parametrize("part", ["header", "body"])
def test_a_real_spawned_partial_frame_sender_cannot_hold_publication(tmp_path: Path, monkeypatch, part: str) -> None:
    """The marker proves the spawned sender reached the partial frame before the parent's deadline."""
    run = _supervise(monkeypatch, tmp_path, functools.partial(provenance_workers.sends_partial_frame, part))
    assert (tmp_path / "frame-sent").read_text(encoding="utf-8") == part
    assert run.outcome.expired and not run.stamped.ok
    assert run.artifact["phase"]["errors"][-1]["code"] == run_estate.PROVENANCE_DEADLINE_CODE
    assert len(run.published) == 1 and not _pid_is_running(run.outcome.worker_pid)


def test_the_direct_worker_process_is_gone_from_the_operating_system_after_a_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    """Reaped, as the OS sees it - not merely as the `Process` object reports it.

    ⚠️ This is a DIRECT-process claim only. A Windows control in the audit showed a grandchild
    surviving its parent's kill, which is why the worker is required to be a leaf (it is started as a
    daemon, and `multiprocessing` refuses to let a daemon have children). Nothing here claims
    descendant cleanup.
    """
    run = _supervise(monkeypatch, tmp_path, functools.partial(provenance_workers.blocks_in, "content"))

    assert run.outcome.worker_pid is not None
    assert run.outcome.worker_alive is False
    assert run.outcome.worker_exitcode is not None, "an unreaped process has no exit code"
    assert not _pid_is_running(run.outcome.worker_pid), "the worker outlived the phase that owned it"


def test_a_worker_that_survives_terminate_and_kill_cannot_certify_a_success(tmp_path: Path, monkeypatch) -> None:
    """A complete terminal result, and still not a pass: the phase could not account for its worker.

    ⚠️ The substituted piece is deliberately the ONE thing this host cannot produce honestly - a
    process that survives `terminate()` and `kill()`. Everything else is the shipping path: a real
    spawned worker really sends a real terminal result, and only `_stop_worker`'s OS-level verdict is
    replaced. It is a seam-substituted control, not a reproduction of an unkillable process.

    The direction matters in both halves. Fail-closed on the VERDICT: exit 11, never `local_only`.
    Fail-open on the EVIDENCE: the accepted records survive, because they were accepted before the
    latch and throwing away 66 real fingerprints over a failed kill would help nobody.
    """
    real_stop = run_estate._stop_worker  # noqa: SLF001

    def unknown_stop(process):
        real_stop(process)
        return run_estate._WorkerStop(True, None, True)  # noqa: SLF001

    monkeypatch.setattr(run_estate, "_stop_worker", unknown_stop)

    run = _supervise(monkeypatch, tmp_path, provenance_workers.succeeds)

    assert run.outcome.worker_alive is True
    assert run.outcome.expired is False, "an unreapable worker is not a timeout"
    assert run.stamped.ok is False, "a phase that lost track of its worker reported a pass"
    assert run.artifact["phase"]["status"] == "partial"
    assert run.artifact["phase"]["errors"][-1] == {
        "code": run_estate.PROVENANCE_REAP_CODE,
        "operation": "fingerprint",
    }
    assert run.artifact["inputs"][0]["input"]["file"] == "unit.twb", "the accepted evidence was discarded"
    assert run.artifact["input_count"] == len(run.artifact["inputs"]) == 1
    assert len(run.published) == 1


def test_an_unreapable_worker_without_a_result_is_a_failure_not_a_partial(tmp_path: Path, monkeypatch) -> None:
    """The negative control for the branch above: nothing accepted means nothing to be partial about."""
    real_stop = run_estate._stop_worker  # noqa: SLF001

    def unknown_stop(process):
        real_stop(process)
        return run_estate._WorkerStop(True, None, True)  # noqa: SLF001

    monkeypatch.setattr(run_estate, "_stop_worker", unknown_stop)

    run = _supervise(monkeypatch, tmp_path, functools.partial(provenance_workers.blocks_in, "collect-inputs"))

    assert run.stamped.ok is False
    assert run.artifact["phase"]["status"] == "failed"
    assert [error["code"] for error in run.artifact["phase"]["errors"]] == [
        run_estate.PROVENANCE_DEADLINE_CODE,
        run_estate.PROVENANCE_REAP_CODE,
    ], "the timeout and the unknown reap must both be explicit"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "-Infinity"])
def test_an_unusable_provenance_timeout_is_refused_before_any_work(tmp_path: Path, monkeypatch, capsys, value) -> None:
    """A budget that is not a duration cannot bound anything, so nothing starts.

    `0` is not "no timeout" and not "local only"; `NaN` compares false against every deadline and
    `Infinity` is the unbounded wait this phase exists to end. The `=` form is used for the negative
    values because argparse would otherwise read a leading `-` as an option, which is a different
    (and less interesting) exit 2.
    """
    for name in ("resolve_run_engine", "collect_provenance", "write_source_provenance", "stamp_inputs"):
        monkeypatch.setattr(run_estate, name, lambda *_a, **_k: pytest.fail("work started on an unusable timeout"))

    code = run_estate.main(
        ["--input", str(tmp_path), "--output", str(tmp_path / "bundle"), f"--provenance-timeout-sec={value}"]
    )

    captured = capsys.readouterr()
    assert code == run_estate.EXIT_USAGE
    assert "--provenance-timeout-sec must be a finite number greater than zero" in captured.err
    assert value not in captured.err.replace("--provenance-timeout-sec", ""), "the diagnostic echoed the input"
    assert not _progress_events(captured.out), "a refused configuration announced a phase"


def test_an_over_ceiling_emitted_tree_starts_no_worker_and_announces_no_provenance_phase(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The path gate keeps its precedence: a refused bundle never enters provenance at all.

    The sentinel is the SUPERVISOR rather than `stamp_inputs`, so a mutation that moves provenance
    ahead of the gate is caught by a started process as well as by a printed event.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    monkeypatch.setattr(run_estate, "PATH_CEILING_LIMITS", _ceilings(40, 4096))
    monkeypatch.setattr(
        run_estate,
        "collect_provenance",
        lambda *_a, **_k: pytest.fail("a worker was started for a bundle the path gate refused"),
    )

    code = run_estate.main(_landing_argv(engine, src, out))

    printed = capsys.readouterr().out
    assert code == run_estate.EXIT_PATH_CEILING
    assert not _progress_events(printed), printed
    assert not (out / run_estate.SOURCE_PROVENANCE_REPORT).exists()


def test_the_real_worker_speaks_exactly_the_protocol_the_parent_accepts(tmp_path: Path) -> None:
    """The production worker's wire shape, validated by the production parent - no fixture in between.

    Run in-process over a real pipe: this is about the CONTRACT between the two halves, which a
    scenario stand-in cannot prove. It is also where the derived-only rule is proved on real
    fingerprint output rather than on a hand-written record.
    """
    import stamp_tableau_provenance as prov  # noqa: PLC0415

    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    recv, send = multiprocessing.Pipe(duplex=False)

    prov.provenance_worker(send, None, {"input": str(src), "env": str(tmp_path / "absent.env")})

    messages = []
    with contextlib.suppress(EOFError, OSError):
        while recv.poll():
            messages.append(recv.recv())
    state = run_estate._ProvenanceState(emit=lambda *_a, **_k: None)  # noqa: SLF001
    for message in messages:
        state.accept(message)  # raises ProvenanceProtocolError on anything outside the protocol

    checkpoints = [m for m in messages if m["kind"] == prov.MSG_CHECKPOINT]
    assert state.total == 1
    assert state.terminal is not None and state.terminal["phase"]["status"] == "local_only"
    assert len(checkpoints) == 1
    assert set(checkpoints[0]["record"]["input"]) <= {"size_bytes", "sha256", "revision_key", "members", "status"}
    assert "unit.twb" not in json.dumps(checkpoints), "a checkpoint carried a filename"
    assert "unit.twb" in json.dumps(state.terminal), "the terminal result is the one that names files"


def _protocol_main(tmp_path: Path, monkeypatch, messages: list[dict]) -> tuple[int, dict, Path]:
    """Replay a spawned worker through the actual coordinator and its one publication attempt."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    src, out = tmp_path / "src", tmp_path / "bundle"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    collect, publish = run_estate.collect_provenance, run_estate.write_source_provenance
    outcomes, publications = [], []

    def replay(input_dir: Path, timeout_sec: float) -> run_estate.ProvenanceOutcome:
        outcome = collect(input_dir, timeout_sec, entry=functools.partial(provenance_workers.sends_messages, messages))
        outcomes.append(outcome)
        return outcome

    def write(directory: Path, result: dict) -> Path | None:
        publications.append(result)
        return publish(directory, result)

    monkeypatch.setattr(run_estate, "collect_provenance", replay)
    monkeypatch.setattr(run_estate, "write_source_provenance", write)
    code = run_estate.main([*_landing_argv(engine, src, out), "--provenance-timeout-sec", str(WORKER_TIMEOUT_SEC)])
    assert len(publications) == 1, "PUBLISH_COUNT: protocol outcomes must publish exactly once"
    assert len(outcomes) == 1 and outcomes[0].worker_pid is not None, "the control never spawned its worker"
    assert not outcomes[0].expired and outcomes[0].worker_alive is False, "not a protocol verdict"
    return code, _provenance_artifact(out), out


def _assert_protocol_refused(run: tuple[int, dict, Path], total: int) -> None:
    code, artifact, out = run
    assert (
        artifact["phase"]["errors"] and artifact["phase"]["errors"][-1]["code"] == run_estate.PROVENANCE_PROTOCOL_CODE
    ), "STATUS_HISTORY: an invalid worker history did not produce the protocol fault"
    assert code == run_estate.EXIT_PROVENANCE_FAILED == 11, "STATUS_HISTORY: invalid history continued"
    assert artifact["phase"]["status"] not in {"success", "local_only"}
    assert artifact["input_count"] == len(artifact["inputs"]) == total
    assert not {"adjudicate", "slice_handovers"} & set(_phase_names(out))
    assert not (out / "handover").exists()


@pytest.mark.parametrize(
    "row_count,pagination,status,error_code",
    [
        (None, ..., "local_only", None),
        (0, ..., "success", None),
        (1, ..., "success", None),
        (999, ..., "success", None),
        (1000, {"pageNumber": "1", "pageSize": "1000", "totalAvailable": "1000"}, "success", None),
        (1000, {"pageNumber": "1", "pageSize": "1000", "totalAvailable": "1001"}, "partial", "inventory-truncated"),
        (1, {"pageNumber": "1", "pageSize": "1000", "totalAvailable": "2"}, "partial", "inventory-truncated"),
        (1000, ..., "partial", "inventory-cannot-establish"),
        (1, {"totalAvailable": True}, "partial", "inventory-cannot-establish"),
    ],
    ids=[
        "offline",
        "zero",
        "one",
        "999",
        "full-proven",
        "full-truncated",
        "short-truncated",
        "full-ambiguous",
        "malformed",
    ],
)
def test_real_inventory_result_publishes_once_and_binds_later_phases(
    tmp_path: Path, monkeypatch, row_count: int | None, pagination: object, status: str, error_code: str | None
) -> None:
    """Real transport/parser/build output crosses the spawned protocol and the unchanged atomic writer."""
    import test_stamp_tableau_provenance as fixtures  # pylint: disable=import-outside-toplevel

    source = tmp_path / "recorded-input"
    source.mkdir()
    original = b"<workbook />"
    (source / "unit.twb").write_bytes(original)
    document = fixtures._inventory_page(row_count or 0, pagination)
    if row_count:
        document["workbooks"]["workbook"][0]["name"] = "unit"
    site = fixtures._install(monkeypatch, fixtures.RecordingSite(fixtures.LIVE_ENV, inventory_document=document))
    reporter = fixtures.RecordingReporter()
    result = fixtures.prov.build(source, fixtures.LIVE_ENV if row_count is not None else {}, reporter)
    reporter.terminal(result)
    messages = json.loads(json.dumps(reporter.messages, allow_nan=False))

    code, artifact, out = _protocol_main(tmp_path, monkeypatch, messages)

    assert artifact == result, "PAGINATION_PROTOCOL_RESULT: the typed result was replaced or lost"
    assert artifact["phase"]["status"] == status
    assert [error["code"] for error in artifact["phase"]["errors"]] == ([error_code] if error_code else [])
    assert artifact["input_count"] == len(artifact["inputs"]) == 1
    assert artifact["inputs"][0]["input"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert site.count("inventory") == int(row_count is not None)
    assert site.count("content") == int(bool(row_count))
    assert not list(out.glob("source-provenance.json.*.tmp"))
    phases = set(_phase_names(out))
    if error_code:
        assert code == run_estate.EXIT_PROVENANCE_FAILED == 11, "PAGINATION_EXIT_11"
        assert not {"adjudicate", "slice_handovers"} & phases, "PAGINATION_NO_LATER_PHASES"
        assert not (out / "handover").exists()
    else:
        assert code == run_estate.EXIT_OK == 0, "PAGINATION_POSITIVE_CONTROL"
        assert {"adjudicate", "slice_handovers"} <= phases


def test_spawned_success_without_any_live_history_is_protocol_invalid(tmp_path: Path, monkeypatch) -> None:
    """Exact re-review reproduction: fingerprint -> success, with no live intent or operation."""
    messages = provenance_workers.protocol_messages(live=False)
    messages = [message for message in messages if message["kind"] != "lookup-intent"]
    messages[-1]["result"]["phase"]["status"] = "success"
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 1)


@pytest.mark.parametrize(
    "stage", ["lookup-intent", "sign-in", "inventory", "content", "scrub", "safe-snapshot", "sign-out"]
)
def test_spawned_success_cannot_skip_an_applicable_stage(tmp_path: Path, monkeypatch, stage: str) -> None:
    """Each missing stage is refused independently, not hidden inside the all-stages-missing case."""
    messages = provenance_workers.protocol_messages(("first.twb", "second.twb"))
    messages = [message for message in messages if message["kind"] != stage and message.get("operation") != stage]
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 2)


@pytest.mark.parametrize("stage", ["sign-in", "inventory", "content", "scrub", "sign-out"])
@pytest.mark.parametrize("completed", [0, 1], ids=["missing-start", "missing-completion"])
def test_spawned_success_requires_both_ends_of_each_live_operation(
    tmp_path: Path, monkeypatch, stage: str, completed: int
) -> None:
    """Removing only a start or completion must not be covered by the whole-stage control alone."""
    messages = provenance_workers.protocol_messages()
    messages = [
        message for message in messages if not (message.get("operation") == stage and message["completed"] == completed)
    ]
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 1)


@pytest.mark.parametrize(
    ("stage", "before"),
    [("inventory", "sign-in"), ("content", "inventory"), ("scrub", "content"), ("sign-out", "scrub")],
)
def test_spawned_live_operations_cannot_run_out_of_order(tmp_path: Path, monkeypatch, stage: str, before: str) -> None:
    """All stages still exist; only their legal order is broken."""
    messages = provenance_workers.protocol_messages()
    moved = [message for message in messages if message.get("operation") == stage]
    messages = [message for message in messages if message.get("operation") != stage]
    index = next(index for index, message in enumerate(messages) if message.get("operation") == before)
    messages[index:index] = moved
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 1)


@pytest.mark.parametrize("stage", ["sign-in", "inventory", "content"])
@pytest.mark.parametrize("completed", [0, 1], ids=["started", "completed"])
def test_spawned_local_only_after_live_work_is_protocol_invalid(
    tmp_path: Path, monkeypatch, stage: str, completed: int
) -> None:
    """The terminal is independently local-shaped; the accepted live history is what contradicts it."""
    messages = provenance_workers.protocol_messages()
    index = next(
        index
        for index, message in enumerate(messages)
        if message.get("operation") == stage and message["completed"] == completed
    )
    messages = [*messages[: index + 1], provenance_workers.protocol_messages(live=False)[-1]]
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 1)


@pytest.mark.parametrize("requested", [None, True], ids=["missing-intent", "live-requested"])
def test_spawned_local_only_requires_explicit_local_intent(tmp_path: Path, monkeypatch, requested: bool | None) -> None:
    """No observed network operation is insufficient when live intent is absent or contradicts local-only."""
    messages = provenance_workers.protocol_messages(live=False)
    if requested is None:
        messages = [message for message in messages if message["kind"] != "lookup-intent"]
    else:
        next(message for message in messages if message["kind"] == "lookup-intent")["requested"] = requested
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 1)


def test_spawned_local_only_cannot_carry_live_result_semantics(tmp_path: Path, monkeypatch) -> None:
    """A false live-intent flag cannot license an origin-bearing local-only terminal."""
    messages = provenance_workers.protocol_messages(live=False)
    origin = provenance_workers.protocol_messages()[-1]["result"]["inputs"][0]["origin"]
    messages[-1]["result"]["inputs"][0]["origin"] = origin
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 1)


@pytest.mark.parametrize(
    ("matches", "counts"),
    [
        ((0, 1), [0, 2, 2, 2]),
        ((0, 1), [0, 1, 1, 1]),
        ((0, 0), [0, 1, 1, 2]),
        ((None, None), [0, 0, 0, 1]),
    ],
    ids=["skipped-attempt-count", "missing-distinct-attempt", "cache-hit-counted-twice", "invented-download"],
)
def test_spawned_content_counts_reconcile_to_distinct_live_results(
    tmp_path: Path, monkeypatch, matches: tuple[int | None, ...], counts: list[int]
) -> None:
    """Physical inputs are not content attempts; a finished count must agree with distinct matched LUIDs."""
    messages = provenance_workers.protocol_messages(("first.twb", "second.twb"), matches=matches)
    content = [message for message in messages if message.get("operation") == "content"]
    for message, completed in zip(content, counts):
        message["completed"] = completed
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 2)


def test_spawned_success_cannot_omit_one_inputs_content_pair(tmp_path: Path, monkeypatch) -> None:
    """Even a cache hit needs its input's start/completion pair; a matching final count cannot hide it."""
    messages = provenance_workers.protocol_messages(("first.twb", "second.twb"), matches=(0, 0))
    content_indexes = [index for index, message in enumerate(messages) if message.get("operation") == "content"]
    messages = [message for index, message in enumerate(messages) if index not in content_indexes[-2:]]
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 2)


@pytest.mark.parametrize(
    "matches", [(0, 1), (0, 0), (None, None), (0, None)], ids=["distinct", "cached", "misses", "mixed"]
)
def test_spawned_full_live_success_is_accepted(tmp_path: Path, monkeypatch, matches: tuple[int | None, ...]) -> None:
    """Positive controls preserve #582: inventory once, zero/one/two distinct content attempts, two inputs."""
    messages = provenance_workers.protocol_messages(("first.twb", "second.twb"), matches=matches)
    code, artifact, _out = _protocol_main(tmp_path, monkeypatch, messages)
    assert artifact == messages[-1]["result"], "FULL_LIVE_SUCCESS: a complete legal live history was refused"
    assert code == 0
    assert artifact["input_count"] == 2


def test_spawned_true_local_only_is_accepted(tmp_path: Path, monkeypatch) -> None:
    """Positive control: no live intent, no live operations and a genuinely local result."""
    messages = provenance_workers.protocol_messages(live=False)
    code, artifact, _out = _protocol_main(tmp_path, monkeypatch, messages)
    assert artifact == messages[-1]["result"], "TRUE_LOCAL_ONLY: an independently local run was refused"
    assert code == 0


@pytest.mark.parametrize("basename", ["Sales:Q3.twb", r"Sales\Q3.twb"])
def test_spawned_posix_scrubbed_basename_is_accepted(tmp_path: Path, monkeypatch, capsys, basename: str) -> None:
    """POSIX punctuation crosses the real worker boundary without ever being used as a path."""
    monkeypatch.setattr(run_estate, "_BASENAME_PLATFORM", "posix")
    messages = provenance_workers.protocol_messages((basename,))
    code, artifact, _out = _protocol_main(tmp_path, monkeypatch, messages)
    assert artifact == messages[-1]["result"], "POSIX_BASENAME: a legal scrubbed basename was refused"
    assert code == 0
    printed = capsys.readouterr()
    assert basename not in printed.out + printed.err
    assert str(tmp_path) not in "".join(
        line for line in printed.out.splitlines() if line.startswith(run_estate.PROVENANCE_PROGRESS_PREFIX)
    )


@pytest.mark.parametrize(
    "basename",
    [
        "Sales:Q3.twb",
        r"Sales\Q3.twb",
        r"C:unit.twb",
        r"C:\private\unit.twb",
        r"\\private-host\share\unit.twb",
        ".",
        "..",
        "CON.twb",
        "COM¹.twb",
        "unit.twb.",
        "unit.twb ",
        "unit|part.twb",
        "unit\x00part.twb",
    ],
    ids=[
        "colon",
        "separator",
        "drive-relative",
        "absolute",
        "unc",
        "dot",
        "dotdot",
        "device",
        "superscript-device",
        "trailing-dot",
        "trailing-space",
        "punctuation",
        "nul",
    ],
)
def test_spawned_windows_invalid_scrubbed_basename_is_refused(tmp_path: Path, monkeypatch, basename: str) -> None:
    """The deterministic Windows seam runs on both OSes; none of these strings is opened or stat'ed."""
    monkeypatch.setattr(run_estate, "_BASENAME_PLATFORM", "nt")
    messages = provenance_workers.protocol_messages((basename,))
    _assert_protocol_refused(_protocol_main(tmp_path, monkeypatch, messages), 1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX native filename control; both flavours also run through a seam")
@pytest.mark.parametrize("basename", ["Sales:Q3.twb", r"Sales\Q3.twb"])
def test_native_posix_worker_preserves_legal_punctuation(tmp_path: Path, monkeypatch, basename: str) -> None:
    """The shipping worker, not a wire fixture, fingerprints and publishes the actual POSIX filename."""
    monkeypatch.delenv("TABLEAU_SERVER_URL", raising=False)
    monkeypatch.delenv("TABLEAU_PAT_NAME", raising=False)
    (tmp_path / basename).write_text("<workbook />", encoding="utf-8")
    outcome = run_estate.collect_provenance(tmp_path, WORKER_TIMEOUT_SEC, env_path=tmp_path / "absent.env")
    assert outcome.result["phase"] == {"status": "local_only", "errors": []}, "NATIVE_POSIX_BASENAME"
    assert outcome.result["inputs"][0]["input"]["file"] == basename
