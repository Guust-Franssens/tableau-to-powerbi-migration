"""Tests for the multi-source LIVE fixture (`tests/fixtures/live/multi-source-live.twbx`).

The fixture embeds real endpoint hostnames, so it is gitignored and generated on demand by
`scripts/make_live_source_fixture.py`. Every test here SKIPS when it is absent, so a clean clone
and CI stay green without it - the fixture is an optional depth test, not a required one.

Why it exists: a federated datasource spanning several live systems is normal in real Tableau
workbooks and has failure modes a single-source fixture cannot reach. Measured 2026-08-05 on such
a workbook, the deterministic tier emitted ONE `Server`/`Database` pair for THREE connectors, so
Databricks and Snowflake both pointed at the Azure SQL host and referenced undefined parameters -
and the model still passed both of that tier's own validators.
"""

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


def test_every_federated_leg_is_reported(spec: dict) -> None:
    """The parser must report EVERY named connection, not just the first.

    Under-reporting live sources is the one direction the credential gate must never fail in: a
    federated datasource that collapses to its first connection arms the gate for one system and
    reports the others nowhere.
    """
    sources = spec["data_sources"]
    assert sources, "no data sources parsed"

    legs = sources[0]["connection"].get("connections") or []
    classes = [leg.get("class") for leg in legs]

    assert len(legs) >= 2, f"expected a federated datasource with several legs, got {classes}"
    assert len(set(classes)) == len(classes), f"legs should be distinct systems, got {classes}"


def test_each_leg_keeps_its_own_endpoint(spec: dict) -> None:
    """Each leg must carry its OWN server - a shared one is the upstream bug this guards against."""
    legs = spec["data_sources"][0]["connection"]["connections"]
    servers = [leg.get("server") for leg in legs if leg.get("server")]

    assert len(servers) == len(legs), f"a leg is missing its server: {legs}"
    assert len(set(servers)) == len(servers), f"legs share a server, which is the defect: {servers}"


def test_connector_specific_details_survive_per_leg(spec: dict) -> None:
    """Databricks needs its http path and Snowflake its warehouse; neither may be dropped."""
    legs = {leg.get("class"): leg for leg in spec["data_sources"][0]["connection"]["connections"]}

    if "databricks" in legs:
        assert legs["databricks"].get("http_path"), f"databricks leg lost its http path: {legs['databricks']}"
    if "snowflake" in legs:
        assert legs["snowflake"].get("warehouse"), f"snowflake leg lost its warehouse: {legs['snowflake']}"


def test_the_credential_gate_arms_for_every_live_leg(spec: dict, tmp_path: Path) -> None:
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
