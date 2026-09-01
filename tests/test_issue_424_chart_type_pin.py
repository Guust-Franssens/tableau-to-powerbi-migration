"""Pins for issue #424 — an `Automatic` mark over a DISCRETE date becomes a stacked `columnChart`.

Tableau's documentation (*Change the Type of Marks in the View*) says the Line mark is chosen
whenever **a date field** and a measure are the inner fields — *"If the dimension is a date
dimension, the Line mark is used instead"* — with no continuous/discrete qualification. The engine
instead gates on continuity (``twb_to_pbir.py:2366`` ``_has_continuous_date``, true only for a
``*-Trunc`` derivation), so a discrete date falls through to ``VT_COLUMN``. With a colour dimension
on the marks card that becomes Power BI's **stacked** ``columnChart``, which sums the series.

**Two kinds of assertion live here and they have opposite lifetimes.** Round-1 review of PR #427
showed the original three fixtures (A/B/C) could not tell a correct fix from two wrong ones: a
Year-only partial fix and an over-broad mark-agnostic fix produce output identical to the right one
on all three. So the set was extended until each candidate remedy is killed by some fixture.

``DEFECT_PINS`` — currently wrong, must FLIP to ``lineChart`` when upstream fixes it. A failure here
is the signal to verify the new output and retire the pin.

``PERMANENT_INVARIANTS`` — currently RIGHT. ⚠️ **Do not retire these with the rest of the pin.** They
are what stops the fix from being over-broad: an explicit ``Bar`` mark stacks in Tableau too, so its
``columnChart`` is faithful, and a non-date discrete dimension is a genuine bar chart. A remedy that
rewrites every date-on-Columns chart — or every discrete-dimension chart — to a line breaks these.

It deliberately does NOT assert an engine version. The shared harness in
``tests/test_upstream_repro_pins.py`` pins one, which makes every test in that file fail the moment
the canonical plugin moves; this file reports the observed version in its failure text instead, so
the behaviour it describes stays checkable on whatever engine is installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engine_source  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURE = REPO / "fixtures" / "upstream-repros" / "issue-424-automatic-mark-discrete-date"
RUN_ROOT = REPO / ".pytest_cache" / "issue-424-chart-type-pin"
SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"

A_AUTO_YEAR = "issue-424-a-discrete-date-part"
B_AUTO_TRUNC = "issue-424-b-continuous-date-trunc"
C_LINE_YEAR = "issue-424-c-explicit-line-mark"
D_BAR_YEAR = "issue-424-d-explicit-bar-mark"
E_AUTO_MDY = "issue-424-e-discrete-exact-date"
F_AUTO_DATETIME = "issue-424-f-datetime-date-part"
G_AUTO_CALCDATE = "issue-424-g-date-valued-calc"
H_AUTO_STRING = "issue-424-h-non-date-dimension"

# Every fixture is the SAME workbook but for the one variable named here.
THE_ONE_DIFFERENCE = {
    A_AUTO_YEAR: "mark Automatic, discrete date PART (derivation Year)",
    B_AUTO_TRUNC: "mark Automatic, continuous truncation (derivation Month-Trunc)",
    C_LINE_YEAR: "mark Line, discrete date PART",
    D_BAR_YEAR: "mark Bar, discrete date PART",
    E_AUTO_MDY: "mark Automatic, discrete EXACT date (derivation MDY)",
    F_AUTO_DATETIME: "mark Automatic, discrete date PART over a datetime column",
    G_AUTO_CALCDATE: "mark Automatic, discrete date PART over a date-valued CALCULATED field",
    H_AUTO_STRING: "mark Automatic, a non-date string dimension (the negative control)",
}

# Currently wrong. Each must become "lineChart" when upstream fixes the predicate.
DEFECT_PINS = {
    A_AUTO_YEAR: "columnChart",
    E_AUTO_MDY: "columnChart",
    F_AUTO_DATETIME: "columnChart",
    G_AUTO_CALCDATE: "columnChart",
}

# Currently RIGHT. These must hold before AND after the fix - they bound its over-broad failure mode.
PERMANENT_INVARIANTS = {
    B_AUTO_TRUNC: "lineChart",
    C_LINE_YEAR: "lineChart",
    D_BAR_YEAR: "columnChart",
    H_AUTO_STRING: "columnChart",
}

CURRENT_MATRIX = {**DEFECT_PINS, **PERMANENT_INVARIANTS}

# The exact binding the #424 reproduction claims, as (queryRef, entity, property, aggregation).
# ``None`` aggregation means a bare Column projection; ``0`` is Sum (Avg=1, Count=2, Min=3, Max=4).
EXPECTED_A_BINDING = {
    "Category": [("Date.Year", "Date", "Year", None)],
    "Y": [("Sum(SLA.AVAILABILITY_PCT)", "SLA", "AVAILABILITY_PCT", 0)],
    "Series": [("SLA.AIRLINE_CODE", "SLA", "AIRLINE_CODE", None)],
}


class FixtureInput(NamedTuple):
    """The distinguishing INPUT of one fixture, read from its `.twb` source."""

    mark_class: str
    axis_field: str
    derivation: str
    datatype: str
    is_calc: bool


# ⚠️ THE SPECIFICATION, hand-written, never derived from a parse. Round-2 review found that the
# emitted-type matrix alone is vacuous as a discrimination claim: D/E/F/G/H all currently emit
# ``columnChart``, so replacing any of their distinguishing inputs with A's leaves EVERY type
# assertion green - including the permanent-invariant test whose entire purpose is guarding D's
# `Bar` mark. Reproduced before fixing: deleting D's `<mark class='Bar'/>` passed all 9 tests.
#
# So the input side is pinned separately, against the parsed source, and WITHOUT the engine - which
# also means this is the one part of the file that actually runs in CI, where the plugin is absent.
EXPECTED_INPUTS = {
    A_AUTO_YEAR: FixtureInput("Automatic", "DATES", "Year", "date", False),
    B_AUTO_TRUNC: FixtureInput("Automatic", "DATES", "Month-Trunc", "date", False),
    C_LINE_YEAR: FixtureInput("Line", "DATES", "Year", "date", False),
    D_BAR_YEAR: FixtureInput("Bar", "DATES", "Year", "date", False),
    E_AUTO_MDY: FixtureInput("Automatic", "DATES", "MDY", "date", False),
    F_AUTO_DATETIME: FixtureInput("Automatic", "DATES", "Year", "datetime", False),
    G_AUTO_CALCDATE: FixtureInput("Automatic", "Calculation_424", "Year", "date", True),
    H_AUTO_STRING: FixtureInput("Automatic", "TECHNOLOGY_SET", "None", "string", False),
}

# Shared by all eight. The stacking consequence needs a colour DIMENSION and a measure on the value
# shelf; without them a fixture stops demonstrating anything even if its axis pill is intact.
EXPECTED_COMMON = {
    "dashboard": "Detail",
    "worksheet": "SLA Availability by Airline",
    "colour_pill": "none:AIRLINE_CODE:nk",
    "value_pill": "sum:AVAILABILITY_PCT:qk",
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


requires_engine = pytest.mark.skipif(_contract() is None, reason="deterministic tier not installed")


# -- fixture INPUT parsing (no engine; this half runs in CI) ---------------------------------------
def _pill_instance(ref: str | None) -> str:
    """``[federated.sla].[yr:DATES:ok]`` -> ``yr:DATES:ok``."""
    return (ref or "").strip().split("].[")[-1].strip("[]")


def _axis_shape(worksheet: ET.Element, slug: str) -> FixtureInput:
    """Resolve the Columns pill back through its ``column-instance`` to the declaring ``column``."""
    marks = worksheet.findall("./table/panes/pane/mark")
    assert len(marks) == 1, f"{slug}: expected exactly one mark card, found {len(marks)}"

    deps = worksheet.find("./table/view/datasource-dependencies")
    assert deps is not None, f"{slug}: worksheet declares no datasource-dependencies"
    instances = {inst.get("name", "").strip("[]"): inst for inst in deps.findall("./column-instance")}
    columns = {col.get("name", "").strip("[]"): col for col in deps.findall("./column")}

    axis_instance = _pill_instance(worksheet.findtext("./table/cols"))
    assert axis_instance in instances, f"{slug}: the cols pill {axis_instance!r} has no column-instance"
    instance = instances[axis_instance]

    axis_field = (instance.get("column") or "").strip("[]")
    assert axis_field in columns, f"{slug}: the axis pill resolves to {axis_field!r}, which is not declared"
    column = columns[axis_field]

    return FixtureInput(
        mark_class=marks[0].get("class") or "",
        axis_field=axis_field,
        derivation=instance.get("derivation") or "",
        datatype=column.get("datatype") or "",
        is_calc=column.find("./calculation") is not None,
    )


def _common_facts(root: ET.Element, worksheet: ET.Element, slug: str) -> dict[str, str]:
    """The reproduction ingredients every variant shares: the colour dimension and the value pill."""
    dashboards = root.findall("./dashboards/dashboard")
    assert len(dashboards) == 1, f"{slug}: expected exactly one dashboard, found {len(dashboards)}"
    colour = worksheet.findall("./table/panes/pane/encodings/color")
    return {
        "dashboard": dashboards[0].get("name") or "",
        "worksheet": worksheet.get("name") or "",
        "colour_pill": _pill_instance(colour[0].get("column"))
        if len(colour) == 1
        else f"<{len(colour)} colour encodings>",
        "value_pill": _pill_instance(worksheet.findtext("./table/rows")),
    }


def _parse_fixture_source(slug: str) -> tuple[FixtureInput, dict[str, str]]:
    """Read one fixture's `.twb` and return its distinguishing input plus the shared facts.

    ⚠️ Deliberately parses the XML directly rather than reading a constant, a filename or the
    engine's IR. A pin that reads its own description back to itself proves nothing, and going
    through the engine would make this skip in CI, where the plugin is absent.
    """
    root = ET.parse(FIXTURE / f"{slug}.twb").getroot()
    worksheets = root.findall("./worksheets/worksheet")
    assert len(worksheets) == 1, f"{slug}: expected exactly one worksheet, found {len(worksheets)}"
    worksheet = worksheets[0]
    return _axis_shape(worksheet, slug), _common_facts(root, worksheet, slug)


@pytest.mark.parametrize("slug", sorted(EXPECTED_INPUTS))
def test_every_fixture_declares_the_input_it_claims(slug: str) -> None:
    """⚠️ THE ANTI-VACUITY PIN. Without it the whole discrimination claim can silently evaporate.

    Round-2 review: because D/E/F/G/H all currently emit ``columnChart``, each one's distinguishing
    input can be replaced with A's while every emitted-type assertion stays green — measured, and it
    passed all nine tests with D's ``<mark class='Bar'/>`` deleted. The suite would go on claiming
    five-way remedy discrimination having lost the controls that provide it.

    Parsed from the `.twb` source, compared against a hand-written specification. Parametrized so a
    drifted fixture fails its OWN case and no other. Runs WITHOUT the engine, so unlike everything
    else in this file it is also live in CI.
    """
    observed = _parse_fixture_source(slug)[0]
    assert observed == EXPECTED_INPUTS[slug], (
        f"{slug} no longer declares the input it is named for ({THE_ONE_DIFFERENCE[slug]}), so this "
        f"module's remedy-discrimination claim is void for it.\n"
        f"  expected: {EXPECTED_INPUTS[slug]}\n  observed: {observed}\n"
        "Restore the input, or if the change is deliberate re-derive which candidate remedies the set "
        "still separates before updating this expectation."
    )


def test_no_two_fixtures_share_an_input_shape() -> None:
    """The discrimination property itself: eight fixtures, eight distinct inputs.

    Stated independently of the per-fixture pin above, because this is the property the module's
    central claim rests on — if two fixtures collapse onto one input, the set separates fewer
    remedies than its matrix test says it does, whatever the individual expectations were updated to.
    """
    observed = {slug: _parse_fixture_source(slug)[0] for slug in EXPECTED_INPUTS}
    collisions = {
        shape: sorted(s for s, v in observed.items() if v == shape)
        for shape in set(observed.values())
        if sum(1 for v in observed.values() if v == shape) > 1
    }
    assert not collisions, (
        f"Two or more fixtures now carry the SAME input shape: {collisions}. The remedy matrix "
        "claims each wrong fix is separated by exactly one fixture; duplicated inputs make that false."
    )


@pytest.mark.parametrize("slug", sorted(EXPECTED_INPUTS))
def test_every_fixture_keeps_the_shared_stacking_ingredients(slug: str) -> None:
    """A colour DIMENSION and a measure on the value shelf are what make the defect arithmetic.

    An axis pill can be perfectly correct while the fixture has quietly stopped demonstrating a
    stacked ratio, so the parts every variant shares are pinned too.
    """
    common = _parse_fixture_source(slug)[1]
    assert common == EXPECTED_COMMON, (
        f"{slug} no longer carries the shared reproduction ingredients.\n"
        f"  expected: {EXPECTED_COMMON}\n  observed: {common}"
    )


def _signal(expectation: str, direction: str) -> str:
    run = _ENGINE_RUN or {}
    return (
        f"Upstream repro pin #424 changed: {expectation}. Observed on canonical engine "
        f"{run.get('version', 'unknown')}. This is a signal, not necessarily a local bug: {direction}. "
        "Verify against the current canonical engine before editing this test."
    )


def _run_engine_once() -> dict[str, Any]:
    global _ENGINE_RUN  # pylint: disable=global-statement
    if _ENGINE_RUN is not None:
        return _ENGINE_RUN

    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles normal collection-time absence.
        pytest.skip("deterministic tier not installed")

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
        "The #424 repro harness could not run the canonical engine. This is a harness failure, not a "
        f"pinned behaviour change.\nCommand: {cmd}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )

    report = json.loads((RUN_ROOT / "report.json").read_text(encoding="utf-8"))
    _ENGINE_RUN = {
        "version": engine_source.engine_version(engine),
        "workbooks": {workbook["name"]: workbook for workbook in report["workbooks"]},
    }
    return _ENGINE_RUN


def _only_visual(slug: str) -> dict[str, Any]:
    """The single emitted ``visual.json`` for one fixture workbook (each declares exactly one zone)."""
    _run_engine_once()
    pages = RUN_ROOT / "pbip" / slug / f"{slug}.Report" / "definition" / "pages"
    visuals = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(pages.rglob("visual.json"))]
    assert len(visuals) == 1, _signal(
        f"{slug} emitted {len(visuals)} visuals, not the one zone it declares",
        "a fixture that stopped emitting exactly one visual no longer isolates anything; re-derive it",
    )
    return visuals[0]


def _visual_type(slug: str) -> str:
    return _only_visual(slug)["visual"]["visualType"]


def _binding(doc: dict[str, Any]) -> dict[str, list[tuple[Any, Any, Any, Any]]]:
    """Resolve each well to ``(queryRef, entity, property, aggregation)`` per projection.

    Takes the whole ``visual.json`` document, not its ``visual`` node, so a caller cannot silently
    hand it the wrong level and get an empty result that looks like "no bindings".

    Flattening to the entity/property pair is the point: ``visualType`` alone says nothing about
    whether the visual still draws the date axis and the ratio measure the reproduction is about.
    """
    out: dict[str, list[tuple[Any, Any, Any, Any]]] = {}
    query_state = ((doc.get("visual") or {}).get("query") or {}).get("queryState") or {}
    for well, payload in query_state.items():
        projections = []
        for proj in payload.get("projections") or []:
            field = proj.get("field") or {}
            aggregation = None
            if "Aggregation" in field:
                aggregation = field["Aggregation"].get("Function")
                field = field["Aggregation"].get("Expression") or {}
            column = field.get("Column") or field.get("Measure") or {}
            entity = ((column.get("Expression") or {}).get("SourceRef") or {}).get("Entity")
            projections.append((proj.get("queryRef"), entity, column.get("Property"), aggregation))
        out[well] = projections
    return out


def test_engine_absence_contract_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same contract shape used by ``requires_engine`` becomes None when the plugin is absent."""

    def missing_engine() -> Path:
        raise engine_source.EngineNotFoundError("simulated missing engine")

    monkeypatch.setattr(engine_source, "engine_root", missing_engine)
    assert _contract() is None


@requires_engine
def test_the_fixture_set_discriminates_between_candidate_remedies() -> None:
    """The whole matrix at once. Each row kills a different WRONG fix.

    Measured on 2.339.0. What each fixture is for:

    * ``A`` / ``E`` together kill a **Year-only** partial fix — one that special-cases the ``Year``
      derivation and leaves the rest of ``_DATE_PARTS`` / ``_DATE_EXACT_DERIVATIONS`` alone. ``E``
      is a discrete *exact date*, which the engine's own comment at ``:404`` calls "an ORDINARY date
      column".
    * ``F`` kills a fix keyed on ``datatype='date'`` that forgets ``datetime``.
    * ``G`` kills a fix that resolves only base columns and not a date-valued **calculated** field.
    * ``D`` kills a **mark-agnostic** fix — one that rewrites every date-on-Columns chart to a line.
      An explicit ``Bar`` mark must keep emitting ``columnChart``: Tableau's own doc says of the Bar
      mark that "Marks are automatically stacked", so that rebuild is faithful and flipping it would
      *introduce* a defect.
    * ``H`` kills a fix keyed on "discrete dimension" rather than "date": a string dimension with a
      measure is a genuine bar chart in Tableau and must stay one.
    * ``B`` / ``C`` are the already-correct paths and must not regress.
    """
    observed = {slug: _visual_type(slug) for slug in CURRENT_MATRIX}
    moved = {slug: (CURRENT_MATRIX[slug], got) for slug, got in observed.items() if CURRENT_MATRIX[slug] != got}
    assert observed == CURRENT_MATRIX, _signal(
        f"the emitted-type matrix moved (slug: expected -> observed) {moved}",
        "if ONLY the DEFECT_PINS flipped to lineChart, upstream fixed #424 correctly - verify, then retire "
        "those four and KEEP the PERMANENT_INVARIANTS; if any invariant moved, the fix is over-broad",
    )


@requires_engine
def test_variant_a_still_emits_the_stacked_column_defect() -> None:
    """The reproduction anchor: the defective type AND the exact binding that makes it matter.

    Asserting ``visualType`` plus "some Series projection" was not enough — round-1 review injected a
    ``columnChart`` with a Series well but **no Category and no Y** and this test passed, so it could
    have stayed green while the visual no longer represented the reproduction at all.
    """
    visual = _only_visual(A_AUTO_YEAR)

    assert visual["visual"]["visualType"] == "columnChart", _signal(
        "an Automatic mark over a discrete date part no longer emits columnChart",
        "this failing may mean upstream FIXED #424; confirm the emitted type is lineChart, then retire this pin",
    )
    binding = _binding(visual)
    observed = {well: binding.get(well) for well in EXPECTED_A_BINDING}
    assert observed == EXPECTED_A_BINDING, _signal(
        f"the reproduction's binding changed.\n  expected: {EXPECTED_A_BINDING}\n  observed: {binding}",
        "the defect is 'a stacked column of a ratio over a date axis'; without the date Category, the "
        "Sum(AVAILABILITY_PCT) Y and the AIRLINE_CODE Series this fixture no longer demonstrates it",
    )


@requires_engine
@pytest.mark.parametrize("slug", sorted(PERMANENT_INVARIANTS))
def test_permanent_invariant_survives_any_future_fix(slug: str) -> None:
    """⚠️ DO NOT RETIRE WITH THE REST OF THE PIN. These are already correct.

    Each is a case a careless widening of ``_has_continuous_date`` would break. They are the only
    thing standing between "fixed" and "rewrote every bar chart into a line chart".
    """
    assert _visual_type(slug) == PERMANENT_INVARIANTS[slug], _signal(
        f"{slug} ({THE_ONE_DIFFERENCE[slug]}) no longer emits {PERMANENT_INVARIANTS[slug]}",
        "this is NOT an upstream fix landing - it is a correct rebuild being broken. An explicit Bar mark "
        "stacks in Tableau too, and a non-date dimension is a genuine bar chart; report it as a regression",
    )


@requires_engine
def test_the_shared_visual_id_is_input_derived_not_an_engine_inconsistency() -> None:
    """#424 read a shared visual id across workbooks as engine inconsistency. It is not.

    ``_sanitize(f"v-{page_name}-{i}-{ws['name']}")`` hashes (dashboard name, zone index, worksheet
    name) and nothing else, so workbooks that agree on those three emit the identical id while
    disagreeing on ``visualType``.
    """
    names = {slug: _only_visual(slug)["name"] for slug in CURRENT_MATRIX}
    types = {slug: _visual_type(slug) for slug in CURRENT_MATRIX}

    assert len(set(names.values())) == 1, _signal(
        f"the fixtures no longer share one visual id (got {names})",
        "this failing may mean the engine now salts the visual name with something beyond "
        "(dashboard, zone index, worksheet); if so, a shared id becomes stronger evidence than it is today",
    )
    assert len(set(types.values())) == 2, _signal(
        f"the shared id no longer spans two different visualTypes (got {types})",
        "the falsification only holds while one id demonstrably carries both types",
    )


@requires_engine
def test_the_engine_reports_no_warning_for_the_defect() -> None:
    """The silence is the dangerous part: stacking is structurally valid, so nothing flags it."""
    workbook = _run_engine_once()["workbooks"][A_AUTO_YEAR]
    worksheet_rows = [row for row in workbook["viz_fidelity"] if row["visual_type"] == "column"]

    assert worksheet_rows, _signal(
        "no viz_fidelity row describes the emitted column visual",
        "this failing may mean the fidelity record changed shape; re-read report.json before editing",
    )
    assert all(row["status"] == "rebuilt" and not row["reason"] for row in worksheet_rows), _signal(
        f"the column visual is no longer reported clean (got {worksheet_rows})",
        "this failing may mean upstream now WARNS about the chart-type choice, which would be a partial fix",
    )
    assert not workbook["remediation_worklist"]["items"], _signal(
        "the remediation worklist is no longer empty for the defective workbook",
        "this failing may mean upstream now routes the chart-type choice to a human",
    )
