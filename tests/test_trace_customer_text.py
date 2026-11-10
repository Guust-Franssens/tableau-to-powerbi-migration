"""Tests for the customer-text tracing harness (`scripts/trace_customer_text.py`).

`inject` must reach the channels that matter without corrupting the workbook; `trace` must find a
sentinel wherever it landed (including file and folder names) and name the channels that reached
nothing. Committed fixtures are copied into `tmp_path` first, so a broken `inject` cannot overwrite them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from trace_customer_text import SENTINEL_PATTERN, inject, render, trace

FIXTURES = REPO_ROOT / "tests" / "fixtures"

WORKBOOK = """<?xml version='1.0' encoding='utf-8' ?>
<workbook version='18.1'>
  <datasources>
    <datasource caption='Sales' name='federated.abc'>
      <column caption='Revenue' datatype='real' name='[rev]' role='measure'>
        <calculation class='tableau' formula='SUM([Sales])' />
      </column>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Sheet 1'>
      <layout-options>
        <title><formatted-text><run>Quarterly revenue</run></formatted-text></title>
      </layout-options>
      <tooltip><formatted-text><run>   </run></formatted-text></tooltip>
    </worksheet>
  </worksheets>
</workbook>
"""


@pytest.fixture(name="stamped")
def _stamped(tmp_path: Path) -> tuple[Path, list[dict]]:
    source = tmp_path / "book.twb"
    source.write_text(WORKBOOK, encoding="utf-8")
    out = tmp_path / "stamped.twb"
    return out, inject(source, out)


# --------------------------------------------------------------------------------------- inject


def test_inject_stamps_each_channel_once_and_writes_the_sidecar(stamped: tuple[Path, list[dict]]) -> None:
    """Captions, title runs and formula comments are stamped; blank runs are not; tokens are unique."""
    out, entries = stamped
    text = out.read_text(encoding="utf-8")
    channels = {entry["channel"] for entry in entries}
    assert {"caption:datasource", "caption:column", "text:title", "formula-comment"} <= channels
    assert "text:tooltip" not in channels
    for stamped_text in ("Sales ZZINJECTZZ", "Revenue ZZINJECTZZ", "Quarterly revenue ZZINJECTZZ"):
        assert stamped_text in text
    assert "SUM([Sales])&#10;// ZZINJECTZZ" in text or "SUM([Sales])\n// ZZINJECTZZ" in text
    tokens = [entry["sentinel"] for entry in entries]
    assert len(tokens) == len(set(tokens))
    sidecar = out.with_suffix(out.suffix + ".sentinels.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == entries


def test_source_workbook_is_never_modified(tmp_path: Path) -> None:
    source = tmp_path / "book.twb"
    source.write_text(WORKBOOK, encoding="utf-8")
    before = source.read_bytes()
    inject(source, tmp_path / "stamped.twb")
    assert source.read_bytes() == before


def test_twbx_roundtrip_keeps_every_other_member(tmp_path: Path) -> None:
    source = tmp_path / "book.twbx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("book.twb", WORKBOOK)
        archive.writestr("Data/sales.csv", "id,amount\n1,2\n")
    out = tmp_path / "stamped.twbx"
    inject(source, out)

    with zipfile.ZipFile(out) as archive:
        assert sorted(archive.namelist()) == ["Data/sales.csv", "book.twb"]
        assert archive.read("Data/sales.csv") == b"id,amount\n1,2\n"
        assert b"ZZINJECTZZ" in archive.read("book.twb")


@pytest.mark.parametrize("fixture", ["minimal.twb", "standalone_datasource.tds"])
def test_stamped_input_still_parses(tmp_path: Path, fixture: str) -> None:
    """A stamped workbook or datasource the parser rejects measures nothing."""
    source = tmp_path / fixture
    shutil.copy(FIXTURES / fixture, source)
    out = tmp_path / f"stamped{source.suffix}"
    assert inject(source, out)
    spec = tmp_path / "spec.json"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "parse_tableau.py"), str(out), "-o", str(spec)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ZZINJECTZZ" in spec.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------- trace


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    (root / "report.json").write_text('{"reason": "ZZINJECTZZ001 stub"}', encoding="utf-8")
    (root / "handover" / "wb.json").write_text('{"note": "ZZINJECTZZ001 stub"}', encoding="utf-8")
    (root / "clean.tmdl").write_text("table Orders\n", encoding="utf-8")
    return root


def test_trace_reports_each_artifact_and_the_inverse_map(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    result = trace([bundle])
    assert set(result["artifacts"]) == {"report.json", "handover/wb.json"}
    assert result["sentinels"]["ZZINJECTZZ001"] == ["handover/wb.json", "report.json"]
    assert result["scanned"] == 3

    spec = tmp_path / "spec.json"
    spec.write_text('{"x": "ZZINJECTZZ001"}', encoding="utf-8")
    assert {"bundle/report.json", "spec.json"} <= set(trace([bundle, spec])["artifacts"])


def test_trace_finds_a_sentinel_in_a_file_or_folder_name(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / "data" / "Sales ZZINJECTZZ002").mkdir(parents=True)
    (root / "data" / "Sales ZZINJECTZZ002" / "rows.csv").write_text("id\n1\n", encoding="utf-8")
    result = trace([root])
    label = "data/Sales ZZINJECTZZ002/rows.csv"
    assert result["paths"] == {label: ["ZZINJECTZZ002"]}
    assert result["artifacts"][label] == ["ZZINJECTZZ002"]


def test_trace_skips_binary_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "thumb.png").write_bytes(b"ZZINJECTZZ003")
    (root / "extract.hyper").write_bytes(b"ZZINJECTZZ004")
    result = trace([root])
    assert result["artifacts"] == {}
    assert result["scanned"] == 0


def test_trace_patterns_match_whole_tokens(tmp_path: Path) -> None:
    """The default matches any `ZZ<WORD>ZZ<digits>` whole (past 999); a custom pattern needs no code edit."""
    for token in ("ZZINJECTZZ001", "ZZTRACEZZ123", "ZZINJECTZZ1000"):
        assert re.fullmatch(SENTINEL_PATTERN, token)
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "a.json").write_text("ZZINJECTZZ1000 please MARKER-42 do this", encoding="utf-8")
    assert trace([root])["artifacts"] == {"a.json": ["ZZINJECTZZ1000"]}
    assert trace([root], r"MARKER-\d+")["artifacts"] == {"a.json": ["MARKER-42"]}


def test_render_names_unreached_channels_and_survives_a_clean_bundle(tmp_path: Path) -> None:
    expected = [
        {"sentinel": "ZZINJECTZZ001", "channel": "caption:column"},
        {"sentinel": "ZZINJECTZZ999", "channel": "caption:action"},
    ]
    text = render(trace([_bundle(tmp_path)]), expected)
    assert "Unreached (1 of 2)" in text
    assert "ZZINJECTZZ999 (caption:action)" in text
    assert "ZZINJECTZZ001 (caption:column)" not in text

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "report.json").write_text("{}", encoding="utf-8")
    assert "_(none)_" in render(trace([clean]))


def test_committed_fixture_still_carries_its_sentinels() -> None:
    """Guards fixture rot: `sentinels.twb` carries the channel set the doc's table cites."""
    assert len(trace([FIXTURES / "sentinels.twb"])["sentinels"]) >= 20
