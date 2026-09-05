"""The acceptance test for issue #446: BOTH gates, on the packaged folder, with NO flags.

This is the whole point of the packaging, so it is tested as a before/after rather than as an
assertion in isolation. The same gate, on the same engine output, is run twice:

* **before** - pointed at the engine working copy (`bundle/pbip/<Unit>`), which is what an operator
  actually has. `check_reference_readiness.py` exits 3 `CANNOT_ESTABLISH` because neither `--source`
  nor `--oracle` can be derived from that path, and `check_unit.py` cannot derive an expected page
  set at all (#443). Exit 3 reads like "this unit is broken" rather than "you did not tell me where
  the workbook is", which is the defect;
* **after** - pointed at the package, no flags, and both produce a real per-page verdict.

The negative control shares a fixture with the positive one on purpose: one workbook, renders for
some of its objects and not others, so a single run has to report `ready` for the covered pages and
`blind` for the rest. Packaging that manufactured coverage would fail here, and packaging that lost
it would fail here too.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_path_ceiling as cpc  # noqa: E402  # pylint: disable=wrong-import-position
import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position
import check_unit  # noqa: E402  # pylint: disable=wrong-import-position
import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
    write_engine_report,
    write_handover,
    write_oracle,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal.twb"
UNIT = "Minimal"
DS_UNIT = "Shared_Extract"
WB_LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write_pbir(bundle: Path, unit: str, objects: list) -> None:
    """A PBIR report with one visual-bearing page per Tableau object, named as the engine names them.

    `displayName` carries the Tableau object name because `check_unit.actual_pages` matches on it,
    and each page gets a `visual.json` because `_page_visual_count` is what distinguishes a rebuilt
    page from the engine's crash-guard placeholder.
    """
    pages = bundle / "pbip" / unit / f"{unit}.Report" / "definition" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": [obj.page_id for obj in objects]}), encoding="utf-8")
    for obj in objects:
        page = pages / obj.page_id
        (page / "visuals" / "v-1").mkdir(parents=True, exist_ok=True)
        (page / "page.json").write_text(json.dumps({"name": obj.page_id, "displayName": obj.name}), encoding="utf-8")
        (page / "visuals" / "v-1" / "visual.json").write_text(json.dumps({"name": "v-1"}), encoding="utf-8")


def _bundle(tmp_path: Path, *, covered: set[str] | None, datasource_only: bool = False) -> tuple[Path, Path, list]:
    """`(bundle, oracle, source objects)` for one real workbook, covered by the named objects only."""
    bundle = tmp_path / "bundle"
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    asset = assets / f"{WB_LUID}_{UNIT}.twb"
    shutil.copy2(FIXTURE, asset)
    objects = crr.source_objects(asset) or []
    assert objects, "the fixture workbook must declare dashboards/worksheets or nothing is measured"

    write_engine_report(bundle, workbooks=[UNIT], datasources=[DS_UNIT] if datasource_only else [])
    write_handover(bundle, UNIT, source_id=str(Path("_runs") / "999-x" / "assets" / asset.name))
    _write_pbir(bundle, UNIT, objects)
    if datasource_only:
        model = bundle / "pbip" / DS_UNIT / f"{DS_UNIT}.SemanticModel" / "definition"
        model.mkdir(parents=True, exist_ok=True)
        (model / "model.tmdl").write_text("model Model\n", encoding="utf-8")

    (bundle / "source-provenance.json").write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "input": {"file": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()},
                        "origin": {"workbook_luid": WB_LUID, "workbook_name": UNIT, "match": "sha256"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    chosen = objects if covered is None else [obj for obj in objects if obj.name in covered]
    # The capture lives in its OWN subtree, never at `tmp_path`: `_collect_evidence` scans the
    # target's grandparent too, so an `_oracle/` beside `out/` would be matched alongside the
    # packaged subset and every page would read `unverifiable`. `conflicting_evidence_dirs` refuses
    # that layout; this fixture models the layout a real run actually has.
    oracle = write_oracle(
        tmp_path / "capture",
        [
            {
                "view_luid": f"{index:08d}-0000-0000-0000-000000000000",
                "view_name": obj.name,
                "workbook_luid": WB_LUID,
                "workbook_name": UNIT,
                "view_type": obj.kind,
                # ⚠️ The shape a CURRENT capture writes, all three fields together (#480 round 3).
                # `status`+`path` alone was the pre-certification shape, and since certification
                # became authoritative that record is unassessable: packaging withholds its `path`
                # and both gates below correctly report NOT_CHECKED. This is the positive end-to-end
                # control, so it has to be a capture something actually measured; the negative half
                # is `test_a_legacy_uncertified_capture_earns_no_numeric_evidence_end_to_end`.
                "data": {
                    "status": "ok",
                    "certification": "certified",
                    "path": f"data/{index}.csv",
                    "row_count": 1,
                    "columns": ["a", "b"],
                },
            }
            for index, obj in enumerate(chosen)
        ],
    )
    (oracle / "data").mkdir(exist_ok=True)
    for index, _ in enumerate(chosen):
        (oracle / "data" / f"{index}.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return bundle, oracle, objects


def _package(tmp_path: Path, bundle: Path, oracle: Path, unit: str = UNIT) -> Path:
    pkg.package_unit(bundle, unit, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets")
    return tmp_path / "out" / unit


def _readiness(target: Path, tmp_path: Path) -> tuple[int, dict]:
    """Run the ENTRY gate exactly as documented - the target, and nothing else."""
    out = tmp_path / f"readiness-{target.name}-{abs(hash(str(target))) % 9999}.json"
    code = crr.main([str(target), "--json", str(out), "--quiet"])
    return code, json.loads(out.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------
# before - what an operator actually has
# --------------------------------------------------------------------------------------------


def test_the_engine_working_copy_alone_cannot_be_assessed(tmp_path: Path) -> None:
    """The defect, reproduced: exit 3 on a unit that is fine, because the join is not on disk."""
    bundle, _, _ = _bundle(tmp_path, covered=None)
    code, payload = _readiness(bundle / "pbip" / UNIT, tmp_path)
    assert code == 3
    assert payload["status"] == "CANNOT_ESTABLISH"


def test_the_engine_working_copy_alone_has_no_expected_page_set(tmp_path: Path) -> None:
    """`check_unit`'s half of the same defect: no `migration-spec.json` on the estate route (#443)."""
    bundle, _, _ = _bundle(tmp_path, covered=None)
    parity = check_unit.check_page_parity(bundle / "pbip" / UNIT, check_unit.load_exemptions(bundle / "pbip" / UNIT))
    assert parity["status"] == check_unit.STATUS_NOT_CHECKED
    assert "no migration-spec.json" in parity["detail"]


# --------------------------------------------------------------------------------------------
# after - the positive control
# --------------------------------------------------------------------------------------------


def test_readiness_needs_no_flags_on_a_package_and_reports_every_page_ready(tmp_path: Path) -> None:
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    code, payload = _readiness(_package(tmp_path, bundle, oracle), tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == payload["pages_expected"] == len(objects)
    assert payload["pages_blind"] == 0


def test_check_unit_finds_the_spec_and_the_oracle_with_no_overrides(tmp_path: Path) -> None:
    """`reference_dir`/`oracle_dir` are None - exactly the CLI's no-flag call."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)

    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert parity["status"] == check_unit.STATUS_PASS
    assert parity["expected_count"] == parity["actual_count"] == len(objects)
    assert coverage["status"] == check_unit.STATUS_PASS
    assert coverage["pages"] == coverage["visual_present"] == coverage["numeric_present"] > 0


# --------------------------------------------------------------------------------------------
# after - the negative control, in the SAME run
# --------------------------------------------------------------------------------------------


def test_a_page_with_no_render_is_still_blind_after_packaging(tmp_path: Path) -> None:
    """Packaging must never manufacture coverage: an uncaptured page stays BLIND, and blocks exit 0."""
    bundle, oracle, objects = _bundle(tmp_path, covered={_first_object_name()})
    unit = _package(tmp_path, bundle, oracle)
    code, payload = _readiness(unit, tmp_path)

    assert (code, payload["status"]) == (1, "FINDINGS")
    assert payload["pages_ready"] == 1
    assert payload["pages_blind"] == len(objects) - 1
    readiness = {row["readiness"] for unit_row in payload["units"] for row in unit_row["pages"]}
    assert sorted(readiness) == ["blind", "ready"]


def test_oracle_coverage_reports_the_uncaptured_pages_as_missing(tmp_path: Path) -> None:
    bundle, oracle, _ = _bundle(tmp_path, covered={_first_object_name()})
    coverage = check_unit.check_oracle_coverage(_package(tmp_path, bundle, oracle), None, None)
    assert coverage["status"] == check_unit.STATUS_NOT_CHECKED
    assert coverage["visual_missing"]
    assert coverage["visual_present"] >= 1


def _first_object_name() -> str:
    """The name of the first Tableau object in the fixture - the one the negative control covers."""
    return (crr.source_objects(FIXTURE) or [])[0].name


# --------------------------------------------------------------------------------------------
# after - the LEGACY negative control, end to end (#480 round 3)
#
# The positive control above was, until round 3, `{"status": "ok", "path": ...}` and nothing else -
# which is a PRE-CERTIFICATION record, not a current one. It passed because a bare `row_count` (and
# before that, a bare `path`) was accepted as evidence. Now that certification is authoritative, the
# same fixture must be split in two: a genuinely certified capture that stays consumable, and this -
# the shape a customer's existing `_oracle/` actually holds - which must not reach a numeric gate.
# --------------------------------------------------------------------------------------------


def _legacy_oracle(oracle: Path) -> None:
    """Rewrite a captured manifest into the shape `origin/master`'s producer wrote for every 200.

    A `row_count` and `columns` derived from the body, and NO `certification` - because nothing
    certified anything. The files stay exactly where they are: this is a manifest-shape change, which
    is the only kind a pre-#480 capture on disk can have.
    """
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    for view in manifest["views"]:
        data = view.get("data") or {}
        if data.get("status") == "ok":
            data.pop("certification", None)
            data["row_count"] = 1
            data["columns"] = ["a", "b"]
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_a_legacy_uncertified_capture_earns_no_numeric_evidence_end_to_end(tmp_path: Path) -> None:
    """The whole point of #471/#480: a sign-off must not be built on numbers nobody measured.

    ⚠️ This is the SAME workbook, the SAME pages and the SAME CSV bytes as the positive control
    above; only the manifest's certification differs. So a fix that merely made packaging stricter
    for everything would fail `test_check_unit_finds_the_spec_and_the_oracle_with_no_overrides`, and
    a fix that kept trusting `row_count` would fail here. Both together are the discrimination.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    _legacy_oracle(oracle)
    unit = _package(tmp_path, bundle, oracle)

    shipped = json.loads((unit / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert shipped["views"], "the views must still ship - the bytes are retained, not deleted"
    for view in shipped["views"]:
        data = view["data"]
        assert data["status"] == "ok", "the transport DID succeed and that distinction survives"
        assert "path" not in data, "a legacy row count must not license an evidence path end to end"
        assert data["row_count"] == 1, "the recorded number is kept for forensics"
        assert data["evidence_withheld"], "the package must SAY why the number is not evidence"

    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert coverage["status"] == check_unit.STATUS_NOT_CHECKED
    assert coverage["numeric_present"] == 0, "not one page may count as numerically evidenced"
    assert coverage["visual_present"] == coverage["pages"], "the RENDER evidence is untouched by this"


def test_the_documented_check_unit_command_refuses_a_legacy_capture_as_numeric_evidence(tmp_path: Path) -> None:
    """The same claim through the CLI, because `check_unit`'s in-process API is not what an operator runs."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    _legacy_oracle(oracle)
    unit = _package(tmp_path, bundle, oracle)
    out = tmp_path / "legacy-unit.json"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS / "check_unit.py"), str(unit), "--quiet", "--json", str(out)],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert proc.returncode != check_unit.EXIT_USAGE, proc.stderr
    checks = {check["id"]: check for check in json.loads(out.read_text(encoding="utf-8"))["checks"]}
    assert checks["page-parity"]["status"] == check_unit.STATUS_PASS, "only the NUMERIC half is withheld"
    assert checks["oracle-coverage"]["status"] == check_unit.STATUS_NOT_CHECKED


# --------------------------------------------------------------------------------------------
# after - the datasource-only control
# --------------------------------------------------------------------------------------------


def test_a_datasource_only_unit_packages_and_neither_gate_crashes(tmp_path: Path) -> None:
    """18 of 67 units in the reference run are datasource-only; a model, no report, no oracle."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None, datasource_only=True)
    unit = _package(tmp_path, bundle, oracle, unit=DS_UNIT)

    assert (unit / "fabric" / f"{DS_UNIT}.SemanticModel").is_dir()
    assert not (unit / "oracle").exists()
    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "NOT_APPLICABLE")
    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    assert parity["status"] in {check_unit.STATUS_NOT_CHECKED, check_unit.STATUS_PASS}


# --------------------------------------------------------------------------------------------
# the documented command line, end to end
# --------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_the_documented_check_unit_command_runs_on_a_package(tmp_path: Path) -> None:
    """`python scripts/check_unit.py <packaged-unit>` - no flags but the ones that capture output."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    out = tmp_path / "unit.json"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS / "check_unit.py"), str(unit), "--quiet", "--json", str(out)],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert proc.returncode != check_unit.EXIT_USAGE, proc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["page-parity"]["status"] == check_unit.STATUS_PASS
    assert checks["oracle-coverage"]["status"] == check_unit.STATUS_PASS


#: The usage exit each gate returns for an argument it cannot use, keyed by the command the README
#: prints. Deliberately PER SCRIPT rather than one shared tuple.
#:
#: ⚠️ A shared `(2, check_unit.EXIT_USAGE)` was wrong, and it took ubuntu CI to show it: exit 2 is
#: argparse's usage code in `check_reference_readiness.py` (`EXIT_USAGE = 2`, verdicts 0/1/3) but it
#: is `check_unit.EXIT_NOT_CHECKED` - a genuine VERDICT - in `check_unit.py`, whose usage code is 64.
#: So a package on which check_unit legitimately reported NOT_CHECKED was read as a usage error and
#: failed this test, with an empty stderr as the only clue. Windows local runs happened to land on a
#: different verdict, so nothing but CI saw it.
#:
#: ⚠️ **`check_unit.py` emits BOTH.** 64 is its own refusal for a directory it cannot use; 2 is what
#: argparse emits for malformed SYNTAX - measured, `check_unit.py --bogus` exits 2 with a message on
#: stderr - and dropping 2 from its map classified a real usage error as a verdict (round-2 finding
#: 5). Both are mapped, and :func:`_rejected_the_argument` requires stderr, which is what separates
#: argparse's 2 from `EXIT_NOT_CHECKED`'s silent 2. That distinction is asserted directly by
#: `test_the_usage_map_separates_argparses_2_from_check_units_NOT_CHECKED`, because a mapping nobody
#: exercises is how the wrong one survived a round.
USAGE_EXITS = {
    "scripts/check_reference_readiness.py": (crr.EXIT_USAGE,),
    "scripts/check_unit.py": (2, check_unit.EXIT_USAGE),
}

#: The one placeholder the package README puts where a caller must substitute the package's path.
#: The command is executed with ONLY this token replaced, so any other malformed argument the README
#: might grow is executed AS PRINTED and fails.
PATH_PLACEHOLDER = "<path-to-this-folder>"

#: The command the package README leads with. Not a gate - it has no verdict and no usage-exit map -
#: so it is checked by RUNNING it and reading its effect, not by its exit classification.
BIND_SCRIPT = "scripts/set_data_folder.py"


def _rejected_the_argument(script: str, proc: subprocess.CompletedProcess[str]) -> bool:
    """Whether the gate refused the ARGUMENT, as opposed to returning a verdict about a package.

    Both halves are required. The exit code alone conflates a verdict with a refusal on any gate
    whose codes overlap - `check_unit.py` returns 2 for both argparse and `NOT_CHECKED` - and stderr
    alone would accept a gate that grumbles and still reports.
    """
    return proc.returncode in USAGE_EXITS[script] and bool(proc.stderr.strip())


@pytest.mark.slow
def test_every_command_the_readme_prints_produces_a_verdict_not_a_usage_error(tmp_path: Path) -> None:
    """The README showed the unit NAME where both gates require a PATH (2026-09-03 cold run).

    Measured against the shipped `HR_Dashboard` package before this fix::

        $ python scripts/check_reference_readiness.py HR_Dashboard
        error: HR_Dashboard is not a directory                       # exit 2

    An argparse usage error is not a verdict, so an agent following the package's own map learned
    nothing about its package.

    ⚠️ **The DOCUMENTED argument is what runs.** Round-2 finding 5: this used to parse the argument
    out of the README, throw it away, and run the gate on a path it had constructed itself - so any
    malformed argument other than the exact bare unit name passed, and the test could not fail for
    the defect it was written for (`mutation_survived=True`). Only the recognized
    `<path-to-this-folder>` placeholder is substituted; everything else is executed as printed.

    ⚠️ A *verdict* is any exit the gate reaches after reading the package, INCLUDING
    `check_unit.EXIT_NOT_CHECKED` (2). "I looked and could not check it" is an opinion about the
    package; "I cannot use this argument" is not. See :data:`USAGE_EXITS`.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    readme = (unit / "README.md").read_text(encoding="utf-8")
    commands = [line.split() for line in readme.splitlines() if line.startswith("    python scripts/")]
    unmapped = [command[1] for command in commands if command[1] not in {*USAGE_EXITS, BIND_SCRIPT}]
    assert not unmapped, f"the README prints a command whose usage exit is unknown here: {unmapped}"
    gates = [command for command in commands if command[1] in USAGE_EXITS]
    assert len(gates) == 2, f"expected both gate commands in the package README, got {commands}"

    for _python, script, *arguments in gates:
        as_printed = [argument.replace(PATH_PLACEHOLDER, str(unit)) for argument in arguments]
        by_doc = _run_gate(script, as_printed, tmp_path)
        assert not _rejected_the_argument(script, by_doc), (
            f"the README's own command `{script} {' '.join(arguments)}` returned no verdict: "
            f"exit {by_doc.returncode}, stderr {by_doc.stderr.strip()!r}"
        )
        by_name = _run_gate(script, [unit.name], tmp_path)
        assert _rejected_the_argument(script, by_name), (
            f"negative control failed: {script} accepted the bare unit name {unit.name!r} "
            f"(exit {by_name.returncode}), so this test could not have caught the defect it exists for"
        )


@pytest.mark.slow
def test_the_usage_map_separates_argparses_2_from_check_units_NOT_CHECKED(tmp_path: Path) -> None:
    """Round-2 finding 5: `check_unit.py`'s map omitted argparse's own 2, so a usage error read as a verdict.

    Three measured cases, and each one is a different cell of the table:

    * `check_unit.py --bogus`   -> 2, stderr  -> a USAGE error (argparse), not a verdict
    * `check_unit.py <not-a-dir>` -> 64, stderr -> the gate's own refusal
    * `check_unit.py <package>` -> a verdict, whatever it is, and NEVER classified as usage

    The third is what stops the fix for the first from swallowing `EXIT_NOT_CHECKED`, which is also
    2 and is a genuine opinion about the package.
    """
    script = "scripts/check_unit.py"
    bogus = _run_gate(script, ["--definitely-not-a-flag"], tmp_path)
    assert (bogus.returncode, bool(bogus.stderr.strip())) == (2, True)
    assert _rejected_the_argument(script, bogus), "an argparse usage error is being read as a verdict"

    missing = _run_gate(script, ["definitely-not-a-directory"], tmp_path)
    assert missing.returncode == check_unit.EXIT_USAGE
    assert _rejected_the_argument(script, missing)

    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    real = _run_gate(script, [str(unit)], tmp_path)
    assert not _rejected_the_argument(script, real), (
        f"a verdict on a real package was classified as a usage error: exit {real.returncode}, "
        f"stderr {real.stderr.strip()!r}"
    )


@pytest.mark.slow
def test_the_readme_command_that_BINDS_the_package_actually_binds_it(tmp_path: Path) -> None:
    """The package is not runnable until it is bound, so the README's first command has to work.

    Round-2 finding 4 measured the documented relocation command writing `/tmp/package\\data\\...`
    on POSIX, reporting the folder missing, exiting 1 - and leaving the file rewritten to that
    invalid value. Round-2 finding 1 makes binding load-bearing rather than a repair, so it is run
    exactly as printed and its effect is read off disk.

    ⚠️ This fixture's unit is report-only, so what it proves is that the README's own first command
    RUNS and succeeds on a package this packager actually produced - including the model-less shape,
    which is a whole class of units and used to be answered with "is this a package folder?". The
    row-level proof (placeholder in, real directory out, partition reads the file) needs a model with
    imported data and lives in `test_package_unit.py`:
    `test_a_moved_package_still_reaches_its_rows_once_it_is_BOUND`.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    readme = (unit / "README.md").read_text(encoding="utf-8")
    printed = [line.split() for line in readme.splitlines() if line.startswith(f"    python {BIND_SCRIPT}")]
    assert len(printed) == 1, f"the package README no longer prints the binding command: {readme[:400]}"

    _python, script, *arguments = printed[0]
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS.parent / script), *[a.replace(PATH_PLACEHOLDER, str(unit)) for a in arguments]],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
        timeout=900,
    )
    assert proc.returncode == 0, f"the documented binding command failed: {proc.stdout}\n{proc.stderr}"
    expressions = list(unit.glob("fabric/*.SemanticModel/definition/expressions.tmdl"))
    for path in expressions:
        text = path.read_text(encoding="utf-8")
        assert pkg.PACKAGE_ROOT_TOKEN not in text, "binding left the placeholder in place"
        for value in re.findall(r'expression\s+(?:#"[^"]+"|[^\s=]+)\s*=\s*"([^"]*)"', text):
            if value.startswith(str(unit)):
                assert Path(value.rstrip("\\/")).is_dir(), f"binding wrote a directory that is not there: {value}"


def _run_gate(script: str, arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one README command line, from a directory where the bare unit name resolves to nothing."""
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS.parent / script), *arguments, "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        timeout=900,
    )


def test_completed_flat_out_dir_layout_is_accepted_and_gates_pass(tmp_path: Path) -> None:
    """The flat `--out <run>/packages` layout is accepted and gate verdicts are clean.

    Because the package carries `package-manifest.json` (`is_self_contained`), it searches only its
    own evidence and does not shadow or double-match against the run-root oracle.
    """
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    run_oracle = tmp_path / "oracle"
    if not run_oracle.exists():
        shutil.copytree(oracle, run_oracle)
    flat_packages = tmp_path / "packages"
    exit_code = pkg.main(
        ["--bundle", str(bundle), "--out", str(flat_packages), "--oracle", str(run_oracle), "--quiet"]
    )
    assert exit_code == 0
    unit = flat_packages / UNIT
    assert (unit / "package-manifest.json").is_file()

    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == len(objects)

    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert parity["status"] == check_unit.STATUS_PASS
    assert parity["expected_count"] == parity["actual_count"] == len(objects)
    assert coverage["status"] == check_unit.STATUS_PASS
    assert coverage["pages"] == coverage["visual_present"] == coverage["numeric_present"] > 0


def test_incomplete_flat_package_without_manifest_fails_closed_when_ancestor_evidence_present(
    tmp_path: Path,
) -> None:
    """A failed/incomplete package without package-manifest.json fails closed and does not pass."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    run_oracle = tmp_path / "oracle"
    if not run_oracle.exists():
        shutil.copytree(oracle, run_oracle)
    flat_packages = tmp_path / "packages"
    pkg.main(["--bundle", str(bundle), "--out", str(flat_packages), "--oracle", str(run_oracle), "--quiet"])
    unit = flat_packages / UNIT

    # Simulate incomplete package by removing package-manifest.json
    (unit / "package-manifest.json").unlink()

    # Without manifest, ancestor evidence is walked and duplicate records make pages unverifiable
    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (1, "FINDINGS")
    readiness_states = {row["readiness"] for unit_row in payload["units"] for row in unit_row["pages"]}
    assert "unverifiable" in readiness_states


def test_nested_batch_out_dir_compatibility_is_preserved(tmp_path: Path) -> None:
    """Existing nested batch layouts (--out <run>/packages/<batch>) remain fully compatible."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    nested = tmp_path / "packages" / "coldrun2"
    exit_code = pkg.main(["--bundle", str(bundle), "--out", str(nested), "--oracle", str(oracle), "--quiet"])
    assert exit_code == 0
    unit = nested / UNIT
    assert (unit / "package-manifest.json").is_file()

    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == len(objects)

    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert parity["status"] == check_unit.STATUS_PASS
    assert coverage["status"] == check_unit.STATUS_PASS


# --------------------------------------------------------------------------------------------
# the scoped report, against BOTH gates - the positive control for round-1 finding 1
#
# `test_package_unit.py` proves the negative half (no foreign unit survives). Scoping can fail the
# other way too, and that failure is invisible in a leak test: `_engine_report` returns None unless
# `workbooks` is a LIST, so a scoped report that trimmed one field too many silently costs a
# datasource-only unit its earned `NOT_APPLICABLE`. Both halves are run here on a report in the real
# engine's 13-field shape rather than the minimal fixture, because the minimal one has nothing to
# over-trim.
# --------------------------------------------------------------------------------------------


def _plant_estate_report(bundle: Path, unit: str, *, datasources: list[str]) -> None:
    """Overwrite the fixture's minimal report with one shaped like a real estate run."""
    from test_package_unit import _estate_report  # pylint: disable=import-outside-toplevel

    full = _estate_report(unit)
    full["datasources"] = [{"name": name} for name in datasources] + full["datasources"]
    (bundle / "report.json").write_text(json.dumps(full), encoding="utf-8")


def test_a_scoped_estate_report_still_earns_every_page_ready(tmp_path: Path) -> None:
    """Positive control: full engine shape in, no flags out, and the verdict is unchanged."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    _plant_estate_report(bundle, UNIT, datasources=[])
    code, payload = _readiness(_package(tmp_path, bundle, oracle), tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == payload["pages_expected"] == len(objects)


def test_a_scoped_estate_report_still_earns_a_datasource_unit_its_not_applicable(tmp_path: Path) -> None:
    """The over-trim control: `NOT_APPLICABLE` is EARNED from `datasources[]`, and can be trimmed away.

    Dropping `workbooks` or `datasources` from the allowlist makes `_engine_report` return None here,
    and this unit stops being a datasource and starts being a broken workbook - exit 3, not exit 0.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None, datasource_only=True)
    _plant_estate_report(bundle, UNIT, datasources=[DS_UNIT])
    unit = _package(tmp_path, bundle, oracle, unit=DS_UNIT)

    scoped = json.loads((unit / "report.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in scoped["datasources"]] == [DS_UNIT]
    assert scoped["workbooks"] == []
    assert crr._engine_report(unit) is not None  # pylint: disable=protected-access
    assert check_unit._is_engine_report(unit / "report.json")  # pylint: disable=protected-access

    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "NOT_APPLICABLE")


# --------------------------------------------------------------------------------------------
# The ceilings themselves. Pinned HERE, in a suite `tests/mutate_package_unit.py` scores, because
# that was the gap: mutating `DIR_CEILING` 247 -> 260 and running the two suites the mutation
# harness uses reported `148 passed, exit 0`. The full documented gate command DOES catch it
# (`tests/test_check_path_ceiling.py` pins both, and the same mutation gives `4 failed, exit 1`),
# so the defect was never "nothing detects a regression" - it was that nothing the harness can
# score detected one, and therefore no anchor could prove the pin can fail.
# --------------------------------------------------------------------------------------------


def test_the_measured_desktop_ceilings_are_pinned_as_two_DISTINCT_literals() -> None:
    """259 and 247 are two separate end-to-end measurements, not one number and an offset.

    They come from different guards - a fully qualified FILE name and a DIRECTORY name - and were
    validated separately against Power BI Desktop 2.157.828.0, so each is pinned to its own literal.
    Deriving one from the other would let a single edit move both and still look internally
    consistent, which is exactly what a pin exists to prevent.
    """
    assert cpc.FILE_CEILING == 259, "Desktop: 'fully qualified file name must be less than 260 characters'"
    assert cpc.DIR_CEILING == 247, "Desktop: 'the directory name must be less than 248 characters'"
    assert cpc.FILE_CEILING - cpc.DIR_CEILING == 12, "the gap is CreateDirectory's 8.3 reservation, not a guess"


def test_the_packager_budgets_against_those_same_two_literals() -> None:
    """A second copy of "260" is how a repo ends up with two length rules - so there is only one.

    `package_unit` imports the pair rather than restating it, and every projected path carries the
    ceiling it was judged against, so this is the join between the pin above and the budget.
    """
    assert (pkg.WINDOWS_LIMITS.file_ceiling, pkg.WINDOWS_LIMITS.dir_ceiling) == (259, 247)
    assert pkg.platform_limits("nt") == pkg.WINDOWS_LIMITS
    assert pkg.platform_limits("posix").file_ceiling == cpc.POSIX_PATH_CEILING > cpc.FILE_CEILING
