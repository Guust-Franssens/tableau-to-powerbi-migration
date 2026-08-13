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
