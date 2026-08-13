"""Tests for the customer-text tracing harness (`scripts/trace_customer_text.py`).

The harness is evidence, not a defense: it answers "which artifacts carry a customer's field names,
formulas and titles into an LLM's context", and that answer has to be re-derivable after any change
to the parser or the (unpinned) conversion engine. So the tests here gate the two properties the
measurement depends on:

* `inject` must reach the channels that matter, and must NOT corrupt the workbook - a stamped file
  that the parser or the engine rejects measures nothing at all;
* `trace` must find a sentinel wherever it landed, including in a file or folder NAME, and must be
  honest about the ones that landed nowhere. The unreached list is the part that bounds the claim.

Every test here was mutation-checked: the behaviour it names was broken on purpose and this test was
confirmed to fail (see `docs/customer-text-exposure.md`).

**Committed fixtures are copied into `tmp_path` before being passed to `inject`.** That is not
ceremony: while mutation-testing "write the stamped XML back over the source", the deliberately
broken build overwrote `tests/fixtures/minimal.twb` and `standalone_datasource.tds` in place. The
test caught the mutation exactly as intended - and still cost a `git restore`. A test that can damage
the repo when the code under test is wrong is a test that will eventually damage the repo.
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


def _channels(stamped: list[dict]) -> set[str]:
    return {entry["channel"] for entry in stamped}


# --------------------------------------------------------------------------------------- inject


def test_caption_attributes_are_stamped(stamped: tuple[Path, list[dict]]) -> None:
    """Captions are the widest carrier: they become TMDL identifiers and report.json prose."""
    out, entries = stamped
    text = out.read_text(encoding="utf-8")
    assert {"caption:datasource", "caption:column"} <= _channels(entries)
    assert "Sales ZZINJECTZZ" in text
    assert "Revenue ZZINJECTZZ" in text


def test_title_runs_are_stamped(stamped: tuple[Path, list[dict]]) -> None:
    """The `<run>` channel is the one a hand-built fixture cannot reach - it lands in PBIR."""
    out, entries = stamped
    assert "text:title" in _channels(entries)
    assert "Quarterly revenue ZZINJECTZZ" in out.read_text(encoding="utf-8")


def test_blank_runs_are_not_stamped(stamped: tuple[Path, list[dict]]) -> None:
    """A whitespace-only run is formatting, not a customer string; stamping it wastes a sentinel."""
    _, entries = stamped
    assert "text:tooltip" not in _channels(entries)


def test_formula_comment_is_appended_without_altering_the_expression(
    stamped: tuple[Path, list[dict]],
) -> None:
    """The comment must be additive and on its own line, or the workbook stops meaning what it meant.

    This channel is the one the parser-only pipeline never had: the engine copies the formula
    VERBATIM into TMDL's `annotation TableauFormula`, comment included.
    """
    out, entries = stamped
    assert "formula-comment" in _channels(entries)
    text = out.read_text(encoding="utf-8")
    assert "SUM([Sales])&#10;// ZZINJECTZZ" in text or "SUM([Sales])\n// ZZINJECTZZ" in text


def test_sentinels_are_unique(stamped: tuple[Path, list[dict]]) -> None:
    """Two channels sharing a token would silently merge two findings into one."""
    _, entries = stamped
    tokens = [entry["sentinel"] for entry in entries]
    assert len(tokens) == len(set(tokens))


def test_sidecar_map_is_written_next_to_the_output(stamped: tuple[Path, list[dict]]) -> None:
    """`trace --sentinels` needs the channel map, so `inject` must always leave one behind."""
    out, entries = stamped
    sidecar = out.with_suffix(out.suffix + ".sentinels.json")
    assert json.loads(sidecar.read_text(encoding="utf-8")) == entries


def test_source_workbook_is_never_modified(tmp_path: Path) -> None:
    """The harness reads customer files; stamping in place would corrupt the evidence."""
    source = tmp_path / "book.twb"
    source.write_text(WORKBOOK, encoding="utf-8")
    before = source.read_bytes()
    inject(source, tmp_path / "stamped.twb")
    assert source.read_bytes() == before


def test_twbx_roundtrip_keeps_every_other_member(tmp_path: Path) -> None:
    """A `.twbx` is a zip of the workbook PLUS its extracts; losing a member breaks conversion."""
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


def test_stamped_workbook_still_parses(tmp_path: Path) -> None:
    """A stamped workbook the parser rejects measures nothing - this is the whole harness's floor."""
    source = tmp_path / "minimal.twb"
    shutil.copy(FIXTURES / "minimal.twb", source)
    out = tmp_path / "stamped.twb"
    inject(source, out)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "parse_tableau.py"), str(out), "-o", str(tmp_path / "spec.json")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ZZINJECTZZ" in (tmp_path / "spec.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------- trace


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    (root / "handover").mkdir(parents=True)
    (root / "report.json").write_text('{"reason": "ZZINJECTZZ001 stub"}', encoding="utf-8")
    (root / "handover" / "wb.json").write_text('{"note": "ZZINJECTZZ001 stub"}', encoding="utf-8")
    (root / "clean.tmdl").write_text("table Orders\n", encoding="utf-8")
    return root


def test_trace_reports_each_artifact_and_the_inverse_map(tmp_path: Path) -> None:
    """The inverse (sentinel -> artifacts) is what turns a grep into a measurement."""
    result = trace([_bundle(tmp_path)])
    assert set(result["artifacts"]) == {"report.json", "handover/wb.json"}
    assert result["sentinels"]["ZZINJECTZZ001"] == ["handover/wb.json", "report.json"]
    assert result["scanned"] == 3


def test_trace_finds_a_sentinel_in_a_file_or_folder_name(tmp_path: Path) -> None:
    """The engine names folders after customer captions - a content-only scan calls that clean."""
    root = tmp_path / "bundle"
    (root / "data" / "Sales ZZINJECTZZ002").mkdir(parents=True)
    (root / "data" / "Sales ZZINJECTZZ002" / "rows.csv").write_text("id\n1\n", encoding="utf-8")
    result = trace([root])
    label = "data/Sales ZZINJECTZZ002/rows.csv"
    assert result["paths"] == {label: ["ZZINJECTZZ002"]}
    assert result["artifacts"][label] == ["ZZINJECTZZ002"]


def test_trace_skips_binary_artifacts(tmp_path: Path) -> None:
    """A `.hyper`/`.png` is not read by an agent, and decoding one is noise, not signal."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "thumb.png").write_bytes(b"ZZINJECTZZ003")
    (root / "extract.hyper").write_bytes(b"ZZINJECTZZ004")
    result = trace([root])
    assert result["artifacts"] == {}
    assert result["scanned"] == 0


def test_trace_prefixes_the_root_name_when_several_roots_are_given(tmp_path: Path) -> None:
    """Tracing the spec AND the bundle in one pass is the normal case; labels must stay distinct."""
    bundle = _bundle(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text('{"x": "ZZINJECTZZ001"}', encoding="utf-8")
    result = trace([bundle, spec])
    assert "bundle/report.json" in result["artifacts"]
    assert "spec.json" in result["artifacts"]


def test_trace_accepts_a_custom_pattern(tmp_path: Path) -> None:
    """Someone else's sentinels (or a real customer string) must be traceable without a code edit."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "a.json").write_text("please MARKER-42 do this", encoding="utf-8")
    assert trace([root])["artifacts"] == {}
    assert trace([root], r"MARKER-\d+")["artifacts"] == {"a.json": ["MARKER-42"]}


def test_a_four_digit_sentinel_is_matched_whole(tmp_path: Path) -> None:
    """A real workbook stamps past 999 (measured: 820 on one sample, and bigger estates exist).

    A fixed three-digit pattern reads `ZZINJECTZZ1000` as `ZZINJECTZZ100` and attributes the hit to
    a different channel - a wrong answer that looks like a right one.
    """
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "a.json").write_text("ZZINJECTZZ1000", encoding="utf-8")
    assert trace([root])["artifacts"] == {"a.json": ["ZZINJECTZZ1000"]}


def test_render_names_the_channels_that_reached_nothing(tmp_path: Path) -> None:
    """The unreached list is the negative result that bounds the claim - it must not be silent."""
    expected = [
        {"sentinel": "ZZINJECTZZ001", "channel": "caption:column"},
        {"sentinel": "ZZINJECTZZ999", "channel": "caption:action"},
    ]
    text = render(trace([_bundle(tmp_path)]), expected)
    assert "Unreached (1 of 2)" in text
    assert "ZZINJECTZZ999 (caption:action)" in text
    assert "ZZINJECTZZ001 (caption:column)" not in text


def test_render_survives_a_clean_bundle(tmp_path: Path) -> None:
    """The everyday case is zero hits; a renderer that only works on findings is useless."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "report.json").write_text("{}", encoding="utf-8")
    assert "_(none)_" in render(trace([root]))


def test_committed_fixture_still_carries_its_sentinels() -> None:
    """Guards fixture rot: `sentinels.twb` is the from-scratch channel set the doc's table cites.

    23 distinct tokens were measured in the `.twb` itself (the rest of the experiment's 33 lived in
    a companion `.tds` and CSV). The floor is deliberately below that: this guards deletion, not
    extension.
    """
    result = trace([FIXTURES / "sentinels.twb"])
    assert len(result["sentinels"]) >= 20


def test_inject_handles_a_bare_datasource(tmp_path: Path) -> None:
    """`.tds` is a first-class input (the model-first path), not just a workbook's companion.

    Asserted end-to-end rather than structurally: a stamped datasource the parser rejects would
    make the whole datasource half of the measurement unrunnable.
    """
    out = tmp_path / "stamped.tds"
    source = tmp_path / "source.tds"
    shutil.copy(FIXTURES / "standalone_datasource.tds", source)
    entries = inject(source, out)
    assert entries
    assert "ZZINJECTZZ" in out.read_text(encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "parse_tableau.py"), str(out), "-o", str(tmp_path / "ds.json")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ZZINJECTZZ" in (tmp_path / "ds.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("token", ["ZZINJECTZZ001", "ZZTRACEZZ123", "ZZINJECTZZ1000"])
def test_default_pattern_shape(token: str) -> None:
    """The default pattern must match any `ZZ<WORD>ZZ<digits>` token, whatever the run named them."""
    assert re.fullmatch(SENTINEL_PATTERN, token)
