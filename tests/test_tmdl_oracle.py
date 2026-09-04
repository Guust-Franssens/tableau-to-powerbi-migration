"""Tests for the TMDL oracle - the gate that asks the real parser whether a model loads.

Issue #254 is a model Power BI Desktop REFUSES TO OPEN (`TMDL Format Error: Unexpected line type:
Other!`), with no file and no line named. Deciding which layouts do that means knowing the TMDL
grammar exactly, and three hand-written attempts at it each shipped false positives on valid TMDL:

  * a property-name allowlist;
  * the documented indentation contract - which capped one level at a tab (so five- and eight-space
    units sailed through) and rejected the perfectly valid `IsHidden`, because TMDL keywords are
    case-insensitive;
  * an AMO parse plus a reflected TOM vocabulary readback - which rejected `let ... in isRemoved`,
    ordinary M, because `IsRemoved` is a reflected boolean.

So the mechanism is not a grammar at all. `scripts/tmdl_oracle.py` hands the model to
`TmdlSerializer.DeserializeDatabaseFromFolder` (AMO 19.84.1) and reports its verdict. Whatever AMO
accepts is accepted here, by construction - false positives are structurally impossible.

Two properties carry most of the weight below and both are load-bearing:

  * **valid TMDL is never flagged** - a gate that rejects valid models gets switched off, which is
    strictly worse than a gate with known blind spots;
  * **unassessable never looks clean** - if the oracle cannot run, the gate exits 3, not 0. That is
    a regression test for a real defect: while it was a mere warning, a missing .NET SDK made
    `check_datamodel.py` exit 0 on parser-fatal TMDL and `check_unit.py` record a PASS.

Silent absorption is deliberately out of scope and issue #404 carries the measurement showing why:
an absorbed property and ordinary expression content produce a byte-identical parse.
"""

# pylint: disable=import-error,wrong-import-position,missing-function-docstring,redefined-outer-name
# pylint: disable=protected-access

from __future__ import annotations

import json
import os
import string
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
import check_datamodel
import tmdl_oracle
from check_datamodel import EXIT_UNASSESSABLE, check_tmdl_model
from tmdl_oracle import OracleUnavailable, check_models, dotnet_executable, pinned_amo_version

DATABASE = "database\n\tcompatibilityLevel: 1702\n"
MODEL = "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\nref table Shipments\n"
PARTITION = '\n\tpartition Shipments = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet S = #table({"Id"},{{1}}) in S\n'

needs_dotnet = pytest.mark.skipif(
    dotnet_executable() is None,
    reason="the TMDL oracle needs the .NET SDK; scripts/preflight.ps1 checks for it",
)


def _absorbing_document(unit: str, absorbed: str) -> str:
    """A measure body written AT the property indent, so it swallows what follows.

    Kept as a VALID-TMDL fixture, not a defect fixture: the parser accepts every one of these, so
    the gate must stay silent on them. That is the whole content of issue #404.
    """
    return (
        "table Shipments\n"
        f"{unit}measure Probe =\n"
        f"{unit * 2}1\n"
        f"{unit * 2}{absorbed}\n"
        "\n"
        f"{unit}partition Shipments = m\n"
        f"{unit * 2}mode: import\n"
        f"{unit * 2}source =\n"
        f'{unit * 3}let S = #table({{"Id"}},{{{{1}}}}) in S\n'
    )


# name -> (Shipments.tmdl body, the codes the gate must report)
CASES: dict[str, tuple[str, set[str]]] = {
    # --- layouts the real parser REFUSES: the model does not open at all -------------------------
    "fatal_uppercase_kind": (
        "table Shipments\n\tMeasure Probe = IF(\n\t\t\t1=1, 1, 0)\n" + PARTITION,
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_root_indent": (
        " table Shipments\n   measure Probe =\n       1\n"
        "\n   partition Shipments = m\n     mode: import\n     source =\n"
        '         let S = #table({"Id"},{{1}}) in S\n',
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_member_order": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tsource =\n"
        '\t\t\t\tlet S = #table({"Id"},{{1}}) in S\n\t\tmode: import\n',
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_fsd_then_ishidden": (
        'table Shipments\n\tmeasure Probe = 1\n\t\tformatStringDefinition =\n\t\t\t\t"0.0%"\n\t\tisHidden\n'
        + PARTITION,
        {"TMDL_PARSER_REJECTED"},
    ),
    # the shape issue #254 actually reported: an inline `=` expression continued onto the next line
    "fatal_inline_then_continuation": (
        'table Shipments\n\tmeasure Probe = IF(1=1,\n\t\t\t"a", "b")\n' + PARTITION,
        {"TMDL_PARSER_REJECTED"},
    ),
    "fatal_blank_lines_between_fragments": (
        'table Shipments\n\tmeasure Probe = IF(\n\n\t\t\t1=1,\n\n\t\t\t"a", "b")\n' + PARTITION,
        {"TMDL_PARSER_REJECTED"},
    ),
    # --- valid TMDL: the gate MUST stay silent ---------------------------------------------------
    "valid_baseline": ("table Shipments\n\tmeasure Probe = 1\n\t\tisHidden\n" + PARTITION, set()),
    # TMDL property names are case-insensitive; AMO sets IsHidden=True (round-2 false positive)
    "valid_uppercase_property": ("table Shipments\n\tmeasure Probe = 1\n\t\tIsHidden\n" + PARTITION, set()),
    # ordinary M whose last line names a reflected TOM boolean (round-3 false positive)
    "valid_m_returns_is_removed": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tmode: import\n\t\tsource =\n"
        '\t\t\t\tlet\n\t\t\t\tisRemoved = false,\n\t\t\t\tS = #table({"Id"},{{1}})\n\t\t\t\tin\n\t\t\t\tisRemoved\n',
        set(),
    ),
    "valid_m_returns_retain_data": (
        "table Shipments\n\tmeasure Probe = 1\n"
        "\n\tpartition Shipments = m\n\t\tmode: import\n\t\tsource =\n"
        '\t\t\t\tlet\n\t\t\t\tretainDataTillForceCalculate = 1,\n\t\t\t\tS = #table({"Id"},{{1}})\n'
        "\t\t\t\tin\n\t\t\t\tretainDataTillForceCalculate\n",
        set(),
    ),
    "valid_multi_line_dax_correctly_indented": (
        "table Shipments\n\tmeasure Probe =\n\t\t\tIF(\n\n\t\t\t\t1 = 1,\n\t\t\t\t2, 3\n\t\t\t)\n"
        "\t\tformatString: 0.0%\n" + PARTITION,
        set(),
    ),
    "valid_nested_format_string_definition": (
        'table Shipments\n\tmeasure Probe = 1\n\t\tformatStringDefinition =\n\t\t\t\t"0.00"\n' + PARTITION,
        set(),
    ),
    # --- absorption: OUT OF SCOPE, and the parser accepts every one of these (issue #404) --------
    "absorbed_is_not_claimed_tab": (_absorbing_document("\t", "isHidden"), set()),
    "absorbed_is_not_claimed_eight_space": (_absorbing_document("        ", "isHidden"), set()),
    "absorbed_is_not_claimed_annotation": (_absorbing_document("\t", "annotation Foo = Bar"), set()),
}

# round-2 false positive: `tablePermission` was in one hand-kept list and missing from another,
# so the SECOND permission in a role was rejected. It needs its own model shape (a roles folder).
ROLE_MODEL = (
    "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
    "\nref table Shipments\nref table Other\n\nref role R\n"
)
ROLE_DOCUMENT = (
    "role R\n\tmodelPermission: read\n\n\ttablePermission Shipments = TRUE()\n\ttablePermission Other = TRUE()\n"
)


def _write_model(root: Path, body: str, *, model: str = MODEL, role: str | None = None) -> Path:
    """Materialise a minimal one-table semantic model whose Shipments.tmdl body is `body`."""
    definition = root / "P.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True)
    (definition / "database.tmdl").write_text(DATABASE, encoding="utf-8")
    (definition / "model.tmdl").write_text(model, encoding="utf-8")
    (definition / "tables" / "Shipments.tmdl").write_text(body, encoding="utf-8")
    if role is not None:
        (definition / "roles").mkdir()
        (definition / "roles" / "R.tmdl").write_text(role, encoding="utf-8")
        (definition / "tables" / "Other.tmdl").write_text(
            "table Other\n\tmeasure Other1 = 1\n"
            "\n\tpartition Other = m\n\t\tmode: import\n\t\tsource =\n"
            '\t\t\t\tlet S = #table({"Id"},{{1}}) in S\n',
            encoding="utf-8",
        )
    return root / "P.SemanticModel"


@pytest.fixture(scope="module")
def verdicts(tmp_path_factory) -> dict[str, set[str]]:
    """Build every case once and hand them all to the oracle in a single process."""
    base = tmp_path_factory.mktemp("oracle-cases")
    roots = {name: _write_model(base / name, body) for name, (body, _) in CASES.items()}
    roots["valid_two_table_permissions"] = _write_model(
        base / "valid_two_table_permissions",
        "table Shipments\n\tmeasure Probe = 1\n" + PARTITION,
        model=ROLE_MODEL,
        role=ROLE_DOCUMENT,
    )
    findings, inspected = check_models(list(roots.values()))
    assert inspected == len(roots), "a case was silently skipped - the rest of this file would pass on nothing"
    return {name: {f.code for f in findings if str(root) in str(f.file)} for name, root in roots.items()}


@needs_dotnet
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_oracle_agrees_with_the_real_parser(case, verdicts):
    assert verdicts[case] == CASES[case][1]


@needs_dotnet
def test_two_table_permissions_in_one_role_are_valid(verdicts):
    """Round-2 false positive: AMO parses this and reports permissions=2."""
    assert verdicts["valid_two_table_permissions"] == set()


@needs_dotnet
def test_a_rejected_model_reports_the_parsers_own_document_and_line(tmp_path):
    """AMO knows exactly where it gave up; passing that through is most of the value, because
    Desktop's own dialog names no file and no line.
    """
    root = _write_model(tmp_path, CASES["fatal_member_order"][0])
    findings, _ = check_models([root])
    assert [f.code for f in findings] == ["TMDL_PARSER_REJECTED"]
    assert findings[0].file.name == "Shipments.tmdl"
    assert findings[0].line == 7


@needs_dotnet
def test_absorption_is_out_of_scope_and_the_parse_cannot_see_it(tmp_path):
    """The measurement behind issue #404, kept executable so the claim cannot rot.

    The same two lines are a SWALLOWED PROPERTY at the measure's property indent and ORDINARY
    EXPRESSION CONTENT one level deeper. If a future change makes the gate flag either of them, it
    must flag both - which is why detecting this by readback is not merely unimplemented but wrong.
    """
    absorbed = _write_model(tmp_path / "absorbed", "table Shipments\n\tmeasure Probe =\n\t\t1\n\t\tisHidden\n")
    content = _write_model(tmp_path / "content", "table Shipments\n\tmeasure Probe =\n\t\t\t1\n\t\t\tisHidden\n")
    findings, inspected = check_models([absorbed, content])
    assert inspected == 2
    assert findings == []


# --- unassessable must never look clean --------------------------------------------------------


@needs_dotnet
def test_a_missing_dotnet_exits_unassessable_on_parser_fatal_tmdl(tmp_path, monkeypatch):
    """The round-3 fail-open, as a regression test: this used to print a warning and exit 0."""
    root = _write_model(tmp_path, CASES["fatal_uppercase_kind"][0])
    monkeypatch.setenv("TMDL_ORACLE_DOTNET", str(tmp_path / "no-such-dotnet.exe"))
    assert check_datamodel.main([str(root)]) == EXIT_UNASSESSABLE


def test_an_unavailable_oracle_never_exits_zero(tmp_path, monkeypatch):
    def explode(_models):
        raise OracleUnavailable("no dotnet")

    monkeypatch.setattr(check_datamodel, "check_models", explode)
    root = _write_model(tmp_path, CASES["valid_baseline"][0])
    assert check_datamodel.main([str(root)]) == EXIT_UNASSESSABLE


def test_inspecting_nothing_is_unassessable_not_clean(tmp_path, monkeypatch):
    """ "No model reached the parser" and "every model parsed" must not share an exit code."""
    monkeypatch.setattr(check_datamodel, "check_models", lambda _models: ([], 0))
    root = _write_model(tmp_path, CASES["valid_baseline"][0])
    assert check_datamodel.main([str(root)]) == EXIT_UNASSESSABLE


@needs_dotnet
def test_no_oracle_is_an_explicit_opt_out(tmp_path):
    """--no-oracle is a human saying "I know" - and the fixture is PARSER-FATAL on purpose.

    With valid TMDL this test could not fail: ignoring the flag entirely would still exit 0, so it
    would be credited as coverage while observing nothing. Measured - mutating `if skip:` to
    `if False:` left the whole file green. Against a model the parser refuses, honouring the flag
    exits 0 and ignoring it exits 1, which is a difference the test can see.

    The first assertion anchors the fixture: if this model ever stops being parser-fatal, that line
    fails loudly instead of the second one quietly going vacuous again.
    """
    root = _write_model(tmp_path, CASES["fatal_uppercase_kind"][0])
    assert check_datamodel.main([str(root)]) == 1
    assert check_datamodel.main([str(root), "--no-oracle"]) == 0


def test_a_payload_from_an_unpinned_parser_is_rejected(monkeypatch, tmp_path):
    """A verdict is only as good as the parser behind it, so the reported AMO version is checked.

    Without this, anything that prints plausible JSON on TMDL_ORACLE_DOTNET is trusted.
    """
    _assert_version_rejected(monkeypatch, tmp_path, "0.0.0.0")


def test_a_near_miss_amo_version_is_also_rejected(monkeypatch, tmp_path):
    """The pin is exact to the patch: 19.84.0 is not 19.84.1, and version-specific parser behaviour
    is exactly what this repo's gotchas are written against.
    """
    _assert_version_rejected(monkeypatch, tmp_path, "19.84.0.0")


def _assert_version_rejected(monkeypatch, tmp_path, version: str) -> None:
    """Feed the driver a payload claiming `version` and require it to refuse."""
    fake = tmp_path / f"fake-{version}.json"
    fake.write_text(json.dumps({"amoVersion": version, "models": []}), encoding="utf-8")

    class Result:  # pylint: disable=too-few-public-methods
        returncode = 0
        stdout = fake.read_text(encoding="utf-8")
        stderr = ""

    monkeypatch.setattr(tmdl_oracle.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(OracleUnavailable, match=version):
        tmdl_oracle._run_batch("dotnet", Path("x.dll"), [Path("y")])


@needs_dotnet
def test_a_target_without_a_definition_folder_is_unassessable_not_clean(tmp_path):
    """A `.SemanticModel` with nothing in it must not be reported as a model that parses - and must
    not be reported as a broken one either. It was never handed to the parser.
    """
    (tmp_path / "Empty.SemanticModel").mkdir()
    assert check_datamodel.main([str(tmp_path)]) == EXIT_UNASSESSABLE


def test_the_pinned_amo_version_matches_the_project_file():
    """The pin is read from the csproj, so it cannot drift away from what actually gets built."""
    assert pinned_amo_version() in (REPO_ROOT / "tools" / "tmdl_oracle" / "tmdl_oracle.csproj").read_text(
        encoding="utf-8"
    )


@needs_dotnet
def test_the_helper_reports_the_pinned_amo_version():
    """End-to-end: the version check passes against the real build, not only against a fake."""
    dll = tmdl_oracle.ensure_built(dotnet_executable())
    assert dll.exists()
    payload = tmdl_oracle._run_batch(dotnet_executable(), dll, [REPO_ROOT / "tools"])
    assert payload["amoVersion"].startswith(pinned_amo_version())


# --- the committed corpus ----------------------------------------------------------------------


@needs_dotnet
def test_the_cli_reports_the_committed_corpus_clean_by_exit_code():
    """Judged by exit code, not by printed text: an earlier mutation harness scored a false
    positive by matching the string "ERROR" against the gate's own log header.
    """
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_datamodel.py"), "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- unreadable documents (text checks, deliberately stricter than the parser) -------------------


def test_undecodable_tmdl_file_is_reported_not_treated_as_clean(tmp_path):
    definition = tmp_path / "M.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    (definition / "broken.tmdl").write_bytes(b"table T\n\tmeasure 'M' = \xff\xfe not utf8\n")
    findings, scanned = check_tmdl_model(tmp_path / "M.SemanticModel")
    assert scanned == 1
    assert {f.code for f in findings} == {"TMDL_UNREADABLE"}


def test_a_bom_prefixed_tmdl_file_is_reported_not_normalised_away(tmp_path):
    """AMO tolerates a BOM, so agreeing with the oracle is not enough here. Power BI Desktop's
    project reader rejects it outright (`UTF8EncodingThrowOnBOM.CheckBom` -> "Only text with UTF8
    encoding without BOM is supported") and the file does not open - see
    .github/skills/pbip-model-refresh/SKILL.md. This is the one TMDL check that is deliberately
    STRICTER than the parser, and this comment is why.
    """
    definition = tmp_path / "M.SemanticModel" / "definition"
    definition.mkdir(parents=True)
    (definition / "t.tmdl").write_bytes(textwrap.dedent("\ufefftable T\n\tmeasure 'M' = 1\n").encode("utf-8"))
    findings, _ = check_tmdl_model(tmp_path / "M.SemanticModel")
    assert "TMDL_BOM" in {f.code for f in findings}


# --- the cross-process build lock (issue #415, first-build part only) --------------------------
#
# `ensure_built()` used to be an unlocked check-then-build: every parallel pytest worker that saw
# the DLL missing or stale would launch its own `dotnet build` into the SAME output directory. On
# Windows that is file-locking roulette; an immediate rerun then "passes" only because one process
# happened to finish the shared build first. These tests exercise the fix without needing a real
# .NET SDK: `subprocess.run` is replaced by a stub that records each invocation and writes the DLL,
# so the assertions are about how many times the SHARED build ran and whether every caller ended up
# with the same result - never about `dotnet` itself.

_LOCK_BARRIER_SCRIPT = string.Template(
    textwrap.dedent(
        """
        import json
        import os
        import sys
        import time
        from pathlib import Path

        sys.path.insert(0, "$repo_scripts")
        import tmdl_oracle  # noqa: E402  pylint: disable=wrong-import-position

        tmdl_oracle.PROJECT_DIR = Path("$project_dir")
        tmdl_oracle.DLL = Path("$dll_path")
        tmdl_oracle.PROJECT_FILE = Path("$project_file")

        BUILD_LOG = "$build_log"
        DELAY = $delay


        def fake_run(cmd, **kwargs):  # pylint: disable=unused-argument
            fd = os.open(BUILD_LOG, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
            os.write(fd, b"build\\n")
            os.close(fd)
            time.sleep(DELAY)
            dll = tmdl_oracle.DLL
            dll.parent.mkdir(parents=True, exist_ok=True)
            dll.write_text("fake dll", encoding="utf-8")

            class Result:  # pylint: disable=too-few-public-methods
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()


        tmdl_oracle.subprocess.run = fake_run

        result_file = Path("$result_file")
        try:
            built = tmdl_oracle.ensure_built("dotnet-stub")
            result_file.write_text(json.dumps({"status": "ok", "dll": str(built)}), encoding="utf-8")
        except Exception as exc:  # pylint: disable=broad-except
            result_file.write_text(json.dumps({"status": "error", "message": str(exc)}), encoding="utf-8")
        """
    )
)


def test_concurrent_processes_build_exactly_once_and_all_reuse_the_same_dll(tmp_path):
    """The issue #415 barrier: several independent OS processes start with no build output at all;
    exactly one of them invokes the (slow, observable) build, and every process ends up with the
    same valid DLL.

    Each worker is a genuinely separate `python` subprocess - not a thread, not a fork of this
    process that would inherit its already-monkeypatched module state - so this races the shared
    lock FILE on disk across real processes, the same shape parallel pytest workers hit.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    project_file = project_dir / "tmdl_oracle.csproj"
    project_file.write_text("<Project></Project>", encoding="utf-8")
    dll_path = project_dir / "bin" / "Release" / "net9.0" / "tmdl_oracle.dll"
    build_log = tmp_path / "build.log"

    workers = []
    for index in range(6):
        script_path = tmp_path / f"worker_{index}.py"
        result_path = tmp_path / f"result_{index}.json"
        script_path.write_text(
            _LOCK_BARRIER_SCRIPT.substitute(
                repo_scripts=str(REPO_ROOT / "scripts"),
                project_dir=str(project_dir),
                dll_path=str(dll_path),
                project_file=str(project_file),
                build_log=str(build_log),
                delay="0.5",
                result_file=str(result_path),
            ),
            encoding="utf-8",
        )
        workers.append(
            (subprocess.Popen([sys.executable, str(script_path)]), result_path)  # pylint: disable=consider-using-with
        )

    for proc, _ in workers:
        assert proc.wait(timeout=60) == 0, "a worker crashed instead of recording a bounded result"

    results = [json.loads(result_path.read_text(encoding="utf-8")) for _, result_path in workers]
    assert all(result["status"] == "ok" for result in results), results
    assert len({result["dll"] for result in results}) == 1, "every process must reuse the SAME built DLL"
    assert build_log.read_text(encoding="utf-8").count("build\n") == 1, (
        "exactly one process should have invoked the build; the rest must wait and reuse it "
        "instead of each launching their own"
    )


def test_a_waiting_process_rechecks_after_the_lock_and_does_not_rebuild(monkeypatch, tmp_path):
    """A process that had to wait for the lock must recheck whether a build is still needed before
    building - otherwise every waiter rebuilds in turn once it is unblocked (issue #415).
    """
    monkeypatch.setattr(tmdl_oracle, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(tmdl_oracle, "DLL", tmp_path / "tmdl_oracle.dll")
    monkeypatch.setattr(tmdl_oracle, "_sources_newer_than_build", lambda: not tmdl_oracle.DLL.exists())

    def fake_acquire(_path, **_kwargs):
        # Simulate: while we were waiting for the lock, another process finished the build.
        tmdl_oracle.DLL.write_text("built by the other process", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        raise AssertionError("must not rebuild - the recheck right after the lock should have seen the DLL")

    monkeypatch.setattr(tmdl_oracle, "_acquire_build_lock", fake_acquire)
    monkeypatch.setattr(tmdl_oracle, "_release_build_lock", lambda _path: None)
    monkeypatch.setattr(tmdl_oracle.subprocess, "run", fake_run)

    result = tmdl_oracle.ensure_built("dotnet-stub")
    assert result == tmdl_oracle.DLL
    assert result.read_text(encoding="utf-8") == "built by the other process"


def test_a_build_failure_reaches_the_caller_as_oracle_unavailable_not_a_clean_result(monkeypatch, tmp_path):
    """A failed shared build must surface to every caller as `OracleUnavailable`, never as a clean
    result nor a stuck lock that blocks the next attempt.
    """
    monkeypatch.setattr(tmdl_oracle, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(tmdl_oracle, "DLL", tmp_path / "tmdl_oracle.dll")
    monkeypatch.setattr(tmdl_oracle, "_sources_newer_than_build", lambda: True)

    class Result:  # pylint: disable=too-few-public-methods
        returncode = 1
        stdout = "restore failed: could not reach nuget.org"
        stderr = ""

    monkeypatch.setattr(tmdl_oracle.subprocess, "run", lambda *a, **k: Result())

    with pytest.raises(OracleUnavailable, match="restore failed"):
        tmdl_oracle.ensure_built("dotnet-stub")
    # The lock must be released on failure too, or one failed build blocks every later attempt.
    assert not (tmp_path / ".oracle-build.lock").exists()


def test_lock_timeout_raises_oracle_unavailable_and_cannot_hang(tmp_path):
    """A held lock must produce a bounded, actionable failure - never a hang, never a clean result."""
    lock_path = tmp_path / "held.lock"
    lock_path.write_text("{}", encoding="utf-8")
    start = time.monotonic()
    with pytest.raises(OracleUnavailable, match="timed out"):
        tmdl_oracle._acquire_build_lock(lock_path, wait=0.3, poll=0.05)
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"lock acquisition must be bounded, took {elapsed:.1f}s"


def test_a_stale_lock_is_reclaimed_instead_of_blocking_every_later_run(tmp_path):
    """A crashed builder's lock must not block every later run forever - the smallest safe
    recovery is age-based reclamation, since a live build cannot be older than its own timeout.
    """
    lock_path = tmp_path / "stale.lock"
    lock_path.write_text("{}", encoding="utf-8")
    stale_time = time.time() - (tmdl_oracle.BUILD_LOCK_STALE_SECONDS + 5)
    os.utime(lock_path, (stale_time, stale_time))

    # A short wait proves the reclaim is immediate: were staleness not checked, this would have to
    # wait out the (much longer) full timeout instead.
    start = time.monotonic()
    tmdl_oracle._acquire_build_lock(lock_path, wait=5, poll=0.05)
    elapsed = time.monotonic() - start
    tmdl_oracle._release_build_lock(lock_path)
    assert elapsed < 2, "a stale lock must be reclaimed promptly, not waited out"
