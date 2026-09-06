"""A standalone datasource unit is NOT a visual-free shell — the fail-open control for any
kind-aware PBIP path-ceiling envelope.

**The finding this file exists to keep true.** Run 409's pre-conversion refusal
(`run_estate.project_estate_path_ceiling`, file 275 / dir 263 at a 92-unit output root) was driven
entirely by the longest unit name, `Meridian_Calc_Gauntlet__Live_Snowflake_` — a **standalone
datasource**. The 437 paths canonical engine 2.368.0 actually emitted from the same three inputs,
rebased onto that same root, max out at **file 237 / dir 225 with 0 offenders**: the tree fitted.
The projector over-projected that one unit by **+48 file / +54 dir** while projecting both workbook
units accurate to **+4**, because it hands every unit the workbook worst case — a 24-unit page id
and a 26+2-unit visual id — regardless of kind.

**The obvious remedy is fail-open, which is why this fixture is committed.** Giving a datasource
unit a *visual-free* envelope would understate its own emitted path by **45 characters**
(measured below), i.e. it would pass a tree Desktop refuses. `migrate_estate.py` passes `swap_specs`
into `write_local_pbip` on the datasource branch, so `assemble_model.build_swap_report_parts` →
`twb_to_pbir.build_field_parameter_page` emits a real `pageSelfService` page with a field-parameter
table and one `listSlicer` per swap parameter whenever the `.tds` carries field-swap calcs.

⚠️ **And a SOUND kind-aware envelope still would not have rescued run 409.** At that root the
datasource projects 262 / 250 with `pageSelfService` + a 24-unit visual — still over 259 / 247. The
real tree fitted only because that datasource happened to carry **no** field-swap calcs, which is a
**data**-dependent property the projector cannot read from a name. So "carry the unit kind" is a
correct reduction in pessimism, not a fix for the over-refusal; do not let it be sold as one.

Two kinds of assertion live here, with opposite lifetimes, in the shape
`tests/test_issue_424_chart_type_pin.py` established:

* the **input specification** (`test_the_fixture_is_swap_shaped`) is hand-written, parsed straight
  from the `.tds`, and runs with no engine — so CI still guards the thing that makes the fixture a
  fixture;
* the **permanent invariant** (`test_a_visual_free_datasource_envelope_would_be_fail_open`) must
  hold before AND after any kind-aware envelope lands. It is what kills the over-broad remedy.

Fixture and provenance: `tests/fixtures/datasource-field-parameter-page/README.md`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engine_source  # noqa: E402  # pylint: disable=wrong-import-position
import run_estate  # noqa: E402  # pylint: disable=wrong-import-position
from check_path_ceiling import utf16_len  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "datasource-field-parameter-page"
UNIT = "Swap_Datasource_With_Field_Parameters"
RUN_ROOT = REPO / ".pytest_cache" / "datasource-path-envelope"
SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"
ENGINE_SKIP_REASON = "deterministic tier not installed"

#: What `build_field_parameter_page` writes. The page name is a hard-coded default in the engine,
#: so observing it is itself the proof that the swap branch — not the thin branch — was reached.
SELF_SERVICE_PAGE = "pageSelfService"

#: `twb_to_pbir._sanitize` returns `name[:24]`, so no emitted identifier can exceed this.
ENGINE_IDENTIFIER_CAP = 24

#: The engine's visual-free datasource shell, for the understatement arithmetic below.
THIN_TAIL = "definition/pages/page1/page.json"

#: The two calcs grafted onto the base fixture, and the exact shape `parameters.detect_field_swap`
#: accepts: a `[Parameters].[X]`-driven CASE whose every branch is a BARE field reference, >= 2
#: branches. One measure-role and one dimension-role, so the emitted page carries the table AND a
#: slicer per parameter. Hand-written; never derived from a parse.
EXPECTED_SWAPS = {
    "Metric Swap": (
        "measure",
        'CASE [Parameters].[Metric] WHEN "Sales" THEN [Sales] WHEN "Profit" THEN [Profit] END',
    ),
    "Grouping Swap": (
        "dimension",
        'CASE [Parameters].[Grouping] WHEN "Order" THEN [Order ID] WHEN "Customer" THEN [Customer Name] END',
    ),
}

_ENGINE_RUN: dict[str, Any] | None = None


def _contract() -> Path | None:
    """The canonical engine root, or None when the deterministic tier is not installed."""
    if os.environ.get(SIMULATE_ENGINE_ABSENT):
        return None
    try:
        return engine_source.engine_root()
    except engine_source.EngineNotFoundError:
        return None


def requires_engine(test):
    """Mark an engine-dependent test and skip it when the canonical engine is absent."""
    test = pytest.mark.engine_dependency(expected_skip_reason=ENGINE_SKIP_REASON)(test)
    return pytest.mark.skipif(_contract() is None, reason=ENGINE_SKIP_REASON)(test)


# -- the fixture INPUT (no engine; this half runs in CI) -------------------------------------------
def _fixture_swaps() -> dict[str, tuple[str, str]]:
    """Every calculated column in the fixture, as `{caption: (role, formula)}`."""
    root = ET.parse(FIXTURE / f"{UNIT}.tds").getroot()
    found: dict[str, tuple[str, str]] = {}
    for column in root.iter("column"):
        calculation = column.find("calculation")
        caption = column.get("caption")
        if calculation is None or not caption:
            continue
        formula = (calculation.get("formula") or "").strip()
        if formula.lower().startswith("case [parameters]"):
            found[caption] = (column.get("role") or "", formula)
    return found


def test_the_fixture_is_swap_shaped() -> None:
    """Without these two calcs the engine falls back to the thin shell and the fixture proves nothing.

    Pinned against the source rather than the output so it still guards the fixture on a machine
    with no engine — and because `build_swap_report_parts` silently returns
    `build_thin_report_parts` when no spec is usable, which would turn every assertion below into a
    vacuous pass rather than a failure.
    """
    assert _fixture_swaps() == EXPECTED_SWAPS, (
        "The fixture's field-swap calcs changed. `detect_field_swap` needs a `[Parameters].[X]`-driven "
        "CASE with >= 2 BARE-field branches; anything else falls through to ordinary calc translation "
        "and the engine emits the thin `page1` shell instead of a self-service page.\n"
        f"  expected: {EXPECTED_SWAPS}\n  observed: {_fixture_swaps()}"
    )


# -- what the ENGINE emits ------------------------------------------------------------------------
def _run_engine_once() -> dict[str, Any]:
    global _ENGINE_RUN  # pylint: disable=global-statement
    if _ENGINE_RUN is not None:
        return _ENGINE_RUN

    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles collection-time absence.
        pytest.skip(ENGINE_SKIP_REASON)

    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    RUN_ROOT.mkdir(parents=True)

    cmd = [
        sys.executable,
        str(engine_source.engine_scripts_dir(engine) / "migrate_estate.py"),
        "-i",
        str(FIXTURE),
        "-o",
        str(RUN_ROOT),
    ]
    completed = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=900, check=False)
    assert completed.returncode == 0, (
        "This harness could not run the canonical engine. That is a harness failure, not a pinned "
        f"behaviour change.\nCommand: {cmd}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )

    pages = RUN_ROOT / "pbip" / UNIT / f"{UNIT}.Report" / "definition" / "pages"
    page_dirs = sorted(p.name for p in pages.iterdir() if p.is_dir()) if pages.is_dir() else []
    visuals = sorted(
        v.name
        for page in (pages.iterdir() if pages.is_dir() else [])
        if page.is_dir()
        for v in ((page / "visuals").iterdir() if (page / "visuals").is_dir() else [])
        if v.is_dir()
    )
    _ENGINE_RUN = {
        "version": engine_source.engine_version(engine),
        "pages": page_dirs,
        "visuals": visuals,
        "deepest_tail": max(
            (
                str(p.relative_to(RUN_ROOT / "pbip" / UNIT / f"{UNIT}.Report")).replace("\\", "/")
                for p in pages.rglob("*")
                if p.is_file()
            ),
            key=utf16_len,
            default="",
        ),
    }
    return _ENGINE_RUN


@requires_engine
def test_a_standalone_datasource_emits_a_self_service_page_with_visuals() -> None:
    """The refutation: a `.tds` unit is not structurally visual-free.

    `pageSelfService` is a hard-coded default of `twb_to_pbir.build_field_parameter_page`, so seeing
    it is the proof that the engine's **swap** branch was reached rather than the thin one — the
    production code path this fixture exists to exercise.
    """
    run = _run_engine_once()
    assert run["pages"] == [SELF_SERVICE_PAGE], (
        f"A standalone datasource emitted pages {run['pages']} on canonical engine {run['version']}, "
        f"not the expected [{SELF_SERVICE_PAGE!r}]. If it now emits ['page1'], the swap branch was "
        "NOT reached and every assertion in this module is vacuous - fix the fixture before trusting "
        "any datasource path-envelope conclusion."
    )
    assert len(run["visuals"]) >= 1, (
        f"A standalone datasource emitted NO visuals on canonical engine {run['version']}. That is "
        "the belief this fixture exists to refute; if the engine genuinely changed, a visual-free "
        "datasource envelope becomes arguable - re-derive it, do not simply delete this test."
    )
    longest = max(utf16_len(name) for name in run["visuals"])
    assert longest == ENGINE_IDENTIFIER_CAP, (
        f"The longest emitted datasource visual identifier is {longest} UTF-16 units, not the "
        f"{ENGINE_IDENTIFIER_CAP} that `twb_to_pbir._sanitize` caps at (observed {run['visuals']} on "
        f"engine {run['version']}). Any datasource path envelope must cover whatever this is."
    )


@requires_engine
def test_a_visual_free_datasource_envelope_would_be_fail_open() -> None:
    """PERMANENT INVARIANT — must hold before AND after any kind-aware envelope lands.

    A datasource envelope that models only `pages/page1/page.json` understates this unit's own
    emitted deepest path. Fail-open is the direction that ships a tree Power BI Desktop refuses, so
    this is the assertion that kills the over-broad remedy.
    """
    run = _run_engine_once()
    emitted = utf16_len(run["deepest_tail"])
    thin = utf16_len(THIN_TAIL)
    assert emitted > thin, (
        f"A visual-free datasource envelope ({thin}) no longer understates the emitted deepest tail "
        f"({emitted}, {run['deepest_tail']!r}) on engine {run['version']}."
    )
    assert emitted - thin >= 40, (
        f"The understatement shrank to {emitted - thin} characters (emitted {emitted} "
        f"{run['deepest_tail']!r} vs visual-free {thin}). Measured 45 on engine 2.368.0. A shrinking "
        "gap is a signal the engine changed its datasource report shape, not a licence to relax the "
        "envelope."
    )


@requires_engine
def test_the_projector_is_blind_to_unit_kind_and_over_projects_this_datasource() -> None:
    """The classification defect, stated against the production function and real emitted paths.

    `project_estate_path_ceiling` takes only NAMES, so it cannot tell a datasource from a workbook
    and assigns both the workbook worst case. Measured here as the gap between what it projects and
    what the engine actually wrote for the same unit at the same root.

    ⚠️ The gap is real but it is NOT the whole of run 409's over-refusal: with the *sound*
    datasource envelope this fixture measures (`pageSelfService` + a 24-unit visual) that unit still
    projects over the ceiling. Deleting the pessimism is worth doing; it is not a fix.
    """
    run = _run_engine_once()
    root = RUN_ROOT
    projection = run_estate.project_estate_path_ceiling(root, [UNIT])
    projected_file = next(r for r in projection["paths"] if r["kind"] == "file")["length"]

    report_root = root / "pbip" / UNIT / f"{UNIT}.Report"
    actual_file = max(utf16_len(str(p)) for p in report_root.rglob("*") if p.is_file())

    assert projected_file > actual_file, (
        f"The projection ({projected_file}) no longer exceeds the emitted report path ({actual_file}) "
        f"for datasource unit {UNIT!r} on engine {run['version']}. If the projector became "
        "kind-aware, re-derive this expectation from the new envelope rather than deleting it."
    )
    sound = utf16_len(
        str(
            report_root
            / "definition"
            / "pages"
            / SELF_SERVICE_PAGE
            / "visuals"
            / ("v" * ENGINE_IDENTIFIER_CAP)
            / "visual.json"
        )
    )
    assert sound < projected_file, (
        f"A kind-aware datasource envelope ({sound}) is no cheaper than the workbook worst case "
        f"({projected_file}); the classification this module argues for would buy nothing."
    )
    assert sound >= actual_file, (
        f"The sound datasource envelope ({sound}) does not cover the emitted path ({actual_file}); an "
        "envelope that fails to cover real output is fail-open by construction."
    )
    assert sound - actual_file <= 2, (
        f"The sound datasource envelope ({sound}) now has {sound - actual_file} characters of slack "
        f"over the emitted path ({actual_file}). Measured 0 on engine 2.368.0 - the emitted visual "
        f"identifiers sit exactly at `_sanitize`'s {ENGINE_IDENTIFIER_CAP}-unit cap, so a datasource "
        "envelope built on `pageSelfService` + that cap is EXACTLY tight and carries no margin."
    )
