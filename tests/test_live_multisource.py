"""Tests for the multi-source LIVE fixture (`tests/fixtures/live/multi-source-live.twbx`)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "live" / "multi-source-live.twbx"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason=f"{FIXTURE.relative_to(REPO)} not present - generate it with scripts/make_live_source_fixture.py",
)


@pytest.fixture(name="spec", scope="module")
def spec_fixture(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Parse the live fixture once per module."""
    out = tmp_path_factory.mktemp("live") / "migration-spec.json"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "parse_tableau.py"), str(FIXTURE), "-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"parser failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(out.read_text(encoding="utf-8"))


def test_every_federated_leg_keeps_its_own_endpoint_and_details(spec: dict) -> None:
    """Every named connection is reported with its OWN server and connector-specific details."""
    sources = spec["data_sources"]
    assert sources, "no data sources parsed"
    legs = sources[0]["connection"].get("connections") or []
    classes = [leg.get("class") for leg in legs]
    assert len(legs) >= 2, f"expected a federated datasource with several legs, got {classes}"
    assert len(set(classes)) == len(classes), f"legs should be distinct systems, got {classes}"

    servers = [leg.get("server") for leg in legs if leg.get("server")]
    assert len(servers) == len(legs), f"a leg is missing its server: {legs}"
    assert len(set(servers)) == len(servers), f"legs share a server, which is the defect: {servers}"

    by_class = {leg.get("class"): leg for leg in legs}
    if "databricks" in by_class:
        assert by_class["databricks"].get("http_path"), f"databricks leg lost its http path: {by_class['databricks']}"
    if "snowflake" in by_class:
        assert by_class["snowflake"].get("warehouse"), f"snowflake leg lost its warehouse: {by_class['snowflake']}"


def test_the_preflight_classifies_every_live_leg(spec: dict, tmp_path: Path) -> None:
    """The preflight must classify each leg, not just the datasource as a whole."""
    spec_path = tmp_path / "migration-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "preflight_source_credentials.py"), "--spec", str(spec_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    legs = spec["data_sources"][0]["connection"]["connections"]

    assert output.count("LIVE source") >= len(legs), f"expected a LIVE line per leg ({len(legs)}), got:\n{output}"
    assert proc.returncode != 0, "a workbook with live sources must not exit 0 from the credential preflight"
