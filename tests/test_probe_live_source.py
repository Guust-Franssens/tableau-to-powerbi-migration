"""Regression tests for the throwaway PBIP scaffold `scripts/probe_live_source.py` hand-writes.

That scaffold is the only hand-written PBIP/PBIR/TMDL in the repo, and it shipped for weeks in a
state the repo's own gate rejects: `powerbi-report-author validate` returned `errorCount: 3`. The
headline test here is the one that costs a single `validate` call and would have caught it on the
day it landed.

Two deliberate design points, both learned from that miss:

* **The validate test SKIPS when the CLI is absent** (CI runs on Ubuntu without the npm bridge), so
  it can never be the reason CI is red - but it also skips on `PBIR_SCHEMA_UNREACHABLE`, because
  the validator *silently* stops schema-checking when it cannot fetch the schema and still prints
  "0 errors". Treating that as a pass would rebuild the exact false green this file exists to stop.
* **The structural tests run everywhere and encode each of the three defects separately**, so the
  guard still bites on a machine with no validator. `validate` alone is not enough anyway: measured
  2026-08-13, it walks only the `.Report` tree, so it reports 0 errors on a scaffold whose `.pbip`
  has no `$schema` at all. The last test is the only thing covering the project-level files.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import probe_live_source  # noqa: E402  # pylint: disable=wrong-import-position
from test_probe_live_custom_sql import DATABRICKS, SNOWFLAKE, SQLSERVER  # noqa: E402
from test_probe_live_source_verdict import _import_skill_modules  # noqa: E402

# Copied verbatim from the validator's own failure text for `definition/version.json`.
VERSION_PATTERN = re.compile(r"^[1-9][0-9]*\.(0|[1-9][0-9]*)\.0$")

# Committed, Desktop-opened deliverables - the ground truth the scaffold is measured against.
EXAMPLE_FABRIC = REPO / "examples" / "shipping-kpis" / "fabric"
EXAMPLE_MODEL = EXAMPLE_FABRIC / "ShippingKPIs.SemanticModel"
EXAMPLE_REPORT = EXAMPLE_FABRIC / "ShippingKPIs.Report"

PROBE_M = "let\n    Source = #table({}, {})\nin\n    Source"

VALIDATOR = shutil.which("powerbi-report-author")
requires_validator = pytest.mark.skipif(
    VALIDATOR is None,
    reason="powerbi-report-author not installed (npm bridge CLI; absent on Linux CI)",
)


@pytest.fixture(name="scaffold")
def scaffold_fixture() -> dict[str, str]:
    """The scaffold exactly as the probe emits it, keyed by relative path."""
    return probe_live_source._pbip_files("Probe", PROBE_M, "T", "C")  # pylint: disable=protected-access


@pytest.fixture(name="scaffold_dir")
def scaffold_dir_fixture(scaffold: dict[str, str], tmp_path: Path) -> Path:
    """The same scaffold materialised on disk, mirroring `_write_probe_model`."""
    for rel, content in scaffold.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def _json(scaffold: dict[str, str], rel: str) -> dict:
    return json.loads(scaffold[rel])


@requires_validator
def test_scaffold_passes_the_repos_own_pbir_gate(scaffold_dir: Path) -> None:
    """The probe must not hand Power BI Desktop a scaffold our own validator rejects."""
    proc = subprocess.run(
        [VALIDATOR, "validate", str(scaffold_dir / "Probe.pbip"), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    payload = json.loads(proc.stdout)["data"]

    if "PBIR_SCHEMA_UNREACHABLE" in payload.get("diagnostics", {}):
        pytest.skip("validator could not fetch the PBIR schema - schema checks did NOT run")

    # The `.pbip` resolves to the report; without this a scaffold that resolved to nothing would
    # also report 0 errors.
    assert Path(payload["reportPath"]).name == "Probe.Report"
    assert payload["errorCount"] == 0, proc.stdout
    assert payload["result"] == "succeeded", proc.stdout


def test_pbir_definition_declares_a_schema(scaffold: dict[str, str]) -> None:
    """`definition.pbir` without `$schema` is `PBIR_JSON_FILE_NO_SCHEMA` - Fabric rejects it."""
    pbir = _json(scaffold, "Probe.Report/definition.pbir")

    assert pbir["$schema"].endswith("/report/definitionProperties/2.0.0/schema.json")
    assert pbir["datasetReference"]["byPath"]["path"] == "../Probe.SemanticModel"


def test_report_definition_version_is_three_part(scaffold: dict[str, str]) -> None:
    """`definition/version.json` must match the schema pattern - a two-part "4.0" is an error."""
    version = _json(scaffold, "Probe.Report/definition/version.json")["version"]

    assert VERSION_PATTERN.match(version), f"{version!r} fails {VERSION_PATTERN.pattern}"


def test_report_version_at_import_is_never_top_level(scaffold: dict[str, str]) -> None:
    """Location-dependent: forbidden at the top level, required inside each theme entry.

    The probe registers no theme, so its `themeCollection` is empty and the loop below is vacuous
    *for the scaffold* - which is why the companion test pins the other half of the rule against a
    committed report that does have theme entries.
    """
    report = _json(scaffold, "Probe.Report/definition/report.json")

    assert "reportVersionAtImport" not in report
    for name, theme in report["themeCollection"].items():
        assert "reportVersionAtImport" in theme, f"themeCollection.{name} is missing it"


def test_shipped_report_keeps_report_version_at_import_inside_each_theme_entry() -> None:
    """The other half of the rule, on ground truth - so "relocate" can never decay into "delete".

    Stripping it from a theme entry is `PBIR_THEME_VERSION_AT_IMPORT_MISSING`. If the probe ever
    grows a `baseTheme`/`customTheme`, this is the shape it has to copy.
    """
    report = json.loads((EXAMPLE_REPORT / "definition" / "report.json").read_text(encoding="utf-8"))
    themes = report["themeCollection"]

    assert themes, "ground-truth example has no theme entries - this test would be vacuous"
    assert "reportVersionAtImport" not in report
    for name, theme in themes.items():
        assert "reportVersionAtImport" in theme, f"themeCollection.{name} is missing it"


def test_project_files_carry_literal_numeric_schemas(scaffold: dict[str, str]) -> None:
    """`.pbip`/`.pbism` are invisible to `validate`, so only this test covers them.

    `.pbism` is tied to a committed deliverable rather than a hard-coded literal: the point is that
    the probe emits what this repo actually ships, not what someone believed it ships.
    """
    pbip = _json(scaffold, "Probe.pbip")
    pbism = _json(scaffold, "Probe.SemanticModel/definition.pbism")

    assert (
        pbip["$schema"] == "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json"
    )
    assert "x.x" not in pbip["$schema"]

    shipped = json.loads((EXAMPLE_MODEL / "definition.pbism").read_text(encoding="utf-8"))
    assert pbism["$schema"] == shipped["$schema"]
    assert pbism["version"] == shipped["version"]


@pytest.mark.parametrize("conn", [SNOWFLAKE, DATABRICKS, SQLSERVER], ids=["snowflake", "databricks", "sqlserver"])
@pytest.mark.parametrize("rows", [0, 1], ids=["empty", "one-real-row"])
def test_ordinary_probe_requires_child_row_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: dict, rows: int
) -> None:
    """Drive resolution, emitted PBIP and real child verdicts; only external I/O is replaced."""
    # pylint: disable=protected-access
    child, verdict_module, _ = _import_skill_modules()
    observed: list[str] = []
    source = {"connection": conn, "tables": [{"name": "Orders"}]}

    def _open(pbip: Path) -> int:
        tmdl = next(pbip.parent.glob("*.SemanticModel/definition/tables/*.tmdl")).read_text(encoding="utf-8")
        assert "column 'ProbeOK'" in tmdl
        assert "sourceColumn: ProbeOK" in tmdl
        assert "Table.ColumnNames(tbl)" in tmdl
        observed.append("open")
        return 123

    def _refresh(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        assert Path(cmd[1]) == probe_live_source.SKILL_SCRIPTS / "refresh_pbip_model.py"
        args = child._build_arg_parser().parse_args(cmd[2:])
        assert args.tables == ["Orders"] and args.no_save
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = verdict_module._emit_data_verdict(None, 0.0, args, [("Orders", rows)], implicit=False)
        emitted = buffer.getvalue()
        assert ("REFRESH: NO_DATA" if rows == 0 else "REFRESH: TABLES_OK 'Orders'") in emitted
        assert code == (1 if rows == 0 else 0)
        observed.append("refresh")
        return subprocess.CompletedProcess(cmd, code, stdout=emitted, stderr="")

    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda _server: True)
    monkeypatch.setattr(probe_live_source, "_open_desktop", _open)
    monkeypatch.setattr(probe_live_source, "_wait_for_catalog", lambda _pid: True)
    monkeypatch.setattr(probe_live_source, "_network_fault_observed", lambda _conn: False)
    monkeypatch.setattr(probe_live_source, "_record_desktop_lifecycle", lambda *_args: {})
    monkeypatch.setattr(probe_live_source, "_close", lambda _pid, _pbip: True)
    monkeypatch.setattr(probe_live_source.subprocess, "run", _refresh)

    code, verdict = probe_live_source._probe_one(tmp_path, [source], 0, 7, False)

    assert observed == ["open", "refresh"]
    if rows == 0:
        assert code == 1 and verdict != "DATA_OK"
    else:
        assert (code, verdict) == (0, "DATA_OK")
