"""A standalone datasource unit is NOT a visual-free PBIP shell — the fail-open control for any
kind-aware PBIP path-ceiling envelope, plus the three-shape envelope matrix.

**The finding this file exists to keep true.** Run 409's pre-conversion path-ceiling refusal
(`run_estate.project_estate_path_ceiling`, file 275 / dir 263 at a 92-unit output root) was driven
entirely by the longest unit name, `Meridian_Calc_Gauntlet__Live_Snowflake_` — a **standalone
datasource**. The 437 paths canonical engine 2.368.0 actually emitted from the same three inputs,
rebased onto that root, max out at file 237 / dir 225 with 0 offenders: the tree fitted, and Power
BI Desktop opened it at that exact root. The projector over-projected that unit by +48 file / +54
dir while projecting both workbook units accurate to +4, because it takes only NAMES and hands
every unit the workbook worst case regardless of kind.

**The obvious remedy — a visual-free datasource envelope — is FAIL-OPEN.** `migrate_estate.py`
passes `swap_specs` into `write_local_pbip` on the datasource branch, so
`assemble_model.build_swap_report_parts` → `twb_to_pbir.build_field_parameter_page` emits a real
`pageSelfService` page with a field-parameter table and one `listSlicer` per swap parameter.

⚠️ **And a SOUND kind-aware envelope still would not have rescued run 409** (262 / 250 at that root,
still over 259 / 247). The tree fitted only because that datasource carried no field-swap calcs,
which is **data**-dependent, not **kind**-dependent. Do not let "carry the unit kind" be sold as the
fix for the over-refusal.

Assertion families, with deliberately different lifetimes (the shape
`tests/test_issue_424_chart_type_pin.py` established):

* the **input specification** (`test_the_fixture_is_swap_shaped`,
  `test_every_swap_controller_is_a_declared_parameter`) is hand-written, parsed straight from the
  `.tds`, and runs with **no engine** — so CI still guards what makes the fixture a fixture;
* the **three-shape matrix** proves the envelope is calibrated per source kind against real engine
  output, not against one artifact;
* the **permanent invariant** (`test_a_visual_free_datasource_envelope_would_be_fail_open`) must
  hold before AND after any kind-aware envelope lands — it is what kills the over-broad remedy.

⚠️ **Engine output is written to a per-session, per-process temporary directory** obtained from
`tmp_path_factory`, never to a fixed path under `.pytest_cache`. An earlier revision used one shared
`RUN_ROOT` and `rmtree`d it on entry, so two concurrent pytest processes could delete each other's
output mid-read (reproduced as `WinError 145` and an empty page list).
`test_two_concurrent_pytest_processes_do_not_collide` is the control for that.

Fixture and provenance: `tests/fixtures/datasource-field-parameter-page/README.md`.
"""

from __future__ import annotations

import json
import os
import re
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
from check_path_ceiling import DIR_CEILING, FILE_CEILING, utf16_len  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURE_DIR = REPO / "tests" / "fixtures" / "datasource-field-parameter-page"
SWAP_UNIT = "Swap_Datasource_With_Field_Parameters"
THIN_UNIT = "CustomSQL_Parameter_And_Doubled_Operators"
WORKBOOK_UNIT = "issue-424-d-explicit-bar-mark"

#: The three inputs of the shape matrix. All three are EXISTING committed fixtures - the swap
#: datasource is the only one this module added, and the workbook is borrowed from the #424 repro
#: set rather than duplicating a long-name proof that `tests/test_run_estate.py` already owns.
MATRIX_SOURCES = {
    THIN_UNIT: REPO / "tests" / "fixtures" / f"{THIN_UNIT}.tds",
    SWAP_UNIT: FIXTURE_DIR / f"{SWAP_UNIT}.tds",
    WORKBOOK_UNIT: REPO
    / "fixtures"
    / "upstream-repros"
    / "issue-424-automatic-mark-discrete-date"
    / f"{WORKBOOK_UNIT}.twb",
}

SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"
CHILD_SENTINEL = "T2P_DS_ENVELOPE_CONCURRENCY_CHILD"
ENGINE_SKIP_REASON = "deterministic tier not installed"

#: What `build_field_parameter_page` writes. The page name is a hard-coded default in the engine,
#: so observing it is itself the proof that the swap branch — not the thin branch — was reached.
SELF_SERVICE_PAGE = "pageSelfService"

#: The engine's visual-free datasource shell page.
THIN_PAGE = "page1"

#: `twb_to_pbir._sanitize` returns `name[:24]`, so no emitted identifier can exceed this.
ENGINE_IDENTIFIER_CAP = 24


#: The envelope tail each source kind would be modelled with, in UTF-16 units below `<unit>.Report/`.
#: ⚠️ Round-2 review: these are **documentation of a capability comparison**, never production
#: coverage. `run_estate` owns the envelope that actually gates a run; anything asserted against the
#: constants below is a statement about what a kind-aware envelope COULD look like, which is why the
#: two tests that gate on production call `project_estate_path_ceiling` directly instead.
def _tail(page: str, visual: str | None) -> int:
    if visual is None:
        return utf16_len(f"definition/pages/{page}/page.json")
    return utf16_len(f"definition/pages/{page}/visuals/{visual}/visual.json")


def _root_for_length(length: int) -> Path:
    """A synthetic resolved root with an exact UTF-16 length on the current host.

    Same idiom as `tests/test_run_estate.py::_root_for_length`, kept local rather than imported so
    this module has no cross-test-module dependency.
    """
    anchor = Path.cwd().anchor
    root = Path(anchor + "r" * (length - utf16_len(anchor))).resolve()
    assert utf16_len(str(root)) == length, f"could not build a {length}-unit root; got {root}"
    return root


ENVELOPE_TAIL = {
    WORKBOOK_UNIT: _tail("p" * ENGINE_IDENTIFIER_CAP, "v" * ENGINE_IDENTIFIER_CAP),
    SWAP_UNIT: _tail(SELF_SERVICE_PAGE, "v" * ENGINE_IDENTIFIER_CAP),
    THIN_UNIT: _tail(THIN_PAGE, None),
}

#: Model-family evidence whose source component is one unit, so the semantic-model term cannot bind
#: and the PBIR assertions in this module stay statements about PBIR. The model family's own control
#: (`test_the_model_family_projection_covers_every_emitted_shape`) uses the PRODUCTION evidence.
PBIR_ONLY_EVIDENCE = {
    "status": "ok",
    "engine_version": "test-only",
    "component": 1,
    "component_value": "x",
    "table_stem": run_estate._model_table_stem_bound(1),  # pylint: disable=protected-access
}

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

#: Each swap's controlling parameter, as Tableau would really declare it. A controller that is not
#: declared is a dangling name the engine happens to accept, and the fixture would then be proving
#: regex tolerance rather than a real swap.
EXPECTED_CONTROLLERS = {
    "Metric": {"domain": "list", "default": "Sales", "members": ["Sales", "Profit"]},
    "Grouping": {"domain": "list", "default": "Order", "members": ["Order", "Customer"]},
}


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
def _fixture_root() -> ET.Element:
    return ET.parse(MATRIX_SOURCES[SWAP_UNIT]).getroot()


def _fixture_swaps() -> dict[str, tuple[str, str]]:
    """Every `[Parameters].[X]`-driven calculated column, as `{caption: (role, formula)}`."""
    found: dict[str, tuple[str, str]] = {}
    for column in _fixture_root().iter("column"):
        calculation = column.find("calculation")
        caption = column.get("caption")
        if calculation is None or not caption:
            continue
        formula = (calculation.get("formula") or "").strip()
        if formula.lower().startswith("case [parameters]"):
            found[caption] = (column.get("role") or "", formula)
    return found


def _declared_parameters() -> dict[str, dict[str, Any]]:
    """Parameters the fixture DECLARES, read the way the engine reads them (`param-domain-type`)."""
    declared: dict[str, dict[str, Any]] = {}
    for column in _fixture_root().iter("column"):
        if column.get("param-domain-type") is None:
            continue
        members = [
            (member.get("value") or "").strip('"')
            for group in column
            if group.tag.endswith("members")
            for member in group
            if member.tag.endswith("member")
        ]
        declared[column.get("caption") or column.get("name") or ""] = {
            "domain": column.get("param-domain-type"),
            "default": (column.get("value") or "").strip('"'),
            "members": members,
        }
    return declared


def _controllers() -> dict[str, list[str]]:
    """`{controller: [branch label, ...]}` read straight out of the swap formulas."""
    out: dict[str, list[str]] = {}
    for _caption, (_role, formula) in _fixture_swaps().items():
        head = re.match(r"(?is)^case\s*\[Parameters\]\.\[([^\]]+)\]\s*(.*)$", formula)
        assert head, f"formula is not a `[Parameters].[X]`-driven CASE: {formula!r}"
        out[head.group(1)] = re.findall(r'(?i)\bwhen\s*"([^"]+)"', head.group(2))
    return out


def test_the_fixture_is_swap_shaped() -> None:
    """Without these two calcs the engine falls back to the thin shell and the fixture proves nothing.

    Pinned against the source rather than the output so it still guards the fixture on a machine
    with no engine — and because `build_swap_report_parts` silently returns
    `build_thin_report_parts` when no spec is usable, which would turn every engine assertion below
    into a vacuous pass rather than a failure.
    """
    assert _fixture_swaps() == EXPECTED_SWAPS, (
        "The fixture's field-swap calcs changed. `detect_field_swap` needs a `[Parameters].[X]`-driven "
        "CASE with >= 2 BARE-field branches; anything else falls through to ordinary calc translation "
        "and the engine emits the thin `page1` shell instead of a self-service page.\n"
        f"  expected: {EXPECTED_SWAPS}\n  observed: {_fixture_swaps()}"
    )


def test_every_swap_controller_is_a_declared_parameter() -> None:
    """A swap whose controller is not declared is a dangling name, not a swap.

    `detect_field_swap` only pattern-matches the FORMULA, so a fixture can reach the self-service
    branch while referencing a parameter that does not exist — proving the engine's regex tolerance
    rather than a real Tableau shape. This pins the controller, its domain, its default and its
    members against the swap's own branch labels.
    """
    declared = _declared_parameters()
    controllers = _controllers()
    missing = sorted(set(controllers) - set(declared))
    assert not missing, (
        f"swap controller(s) {missing} are referenced by a calc but not declared as parameters in "
        f"the fixture. Declared: {sorted(declared)}. A dangling controller makes this fixture a test "
        "of regex tolerance, not of a real field swap."
    )
    for controller, labels in controllers.items():
        spec = declared[controller]
        expected = EXPECTED_CONTROLLERS[controller]
        assert spec["domain"] == expected["domain"], f"{controller}: domain {spec['domain']!r}"
        assert spec["members"] == expected["members"], f"{controller}: members {spec['members']!r}"
        assert spec["default"] == expected["default"], f"{controller}: default {spec['default']!r}"
        assert spec["members"] == labels, (
            f"{controller}: declared members {spec['members']} do not match the swap's own branch "
            f"labels {labels}. The parameter and the calc must describe the same choice."
        )
        assert spec["default"] in labels, f"{controller}: default {spec['default']!r} is not a branch"


# -- what the ENGINE emits -------------------------------------------------------------------------
@pytest.fixture(scope="session", name="engine_bundle")
def _engine_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:  # pylint: disable=too-many-locals
    """Run the canonical engine ONCE over all three source shapes, into a per-process directory.

    `tmp_path_factory` gives a base temp that is unique per pytest process and per xdist worker, so
    nothing here is shared and nothing is destructively removed. That is the whole point: the
    previous fixed `.pytest_cache` root let two concurrent runs delete each other's output.
    """
    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles collection-time absence
        pytest.skip(ENGINE_SKIP_REASON)

    base = tmp_path_factory.mktemp("ds-path-envelope")
    source_dir = base / "input"
    source_dir.mkdir()
    for unit, path in MATRIX_SOURCES.items():
        assert path.is_file(), f"matrix source for {unit} is missing: {path}"
        (source_dir / path.name).write_bytes(path.read_bytes())
    out = base / "bundle"

    cmd = [
        sys.executable,
        str(engine_source.engine_scripts_dir(engine) / "migrate_estate.py"),
        "-i",
        str(source_dir),
        "-o",
        str(out),
    ]
    completed = subprocess.run(
        cmd, cwd=REPO, text=True, capture_output=True, errors="replace", timeout=1800, check=False
    )
    assert completed.returncode == 0, (
        "This harness could not run the canonical engine. That is a harness failure, not a pinned "
        f"behaviour change.\nCommand: {cmd}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )

    shapes: dict[str, Any] = {}
    for unit in MATRIX_SOURCES:
        report = out / "pbip" / unit / f"{unit}.Report"
        pages_dir = report / "definition" / "pages"
        page_dirs = sorted(p.name for p in pages_dir.iterdir() if p.is_dir()) if pages_dir.is_dir() else []
        visuals = sorted(
            v.name
            for page in (pages_dir.iterdir() if pages_dir.is_dir() else [])
            if page.is_dir()
            for v in ((page / "visuals").iterdir() if (page / "visuals").is_dir() else [])
            if v.is_dir()
        )
        tails = (
            [str(p.relative_to(report)).replace("\\", "/") for p in report.rglob("*") if p.is_file()]
            if report.is_dir()
            else []
        )
        dir_tails = (
            [str(p.relative_to(report)).replace("\\", "/") for p in report.rglob("*") if p.is_dir()]
            if report.is_dir()
            else []
        )
        deepest = max(tails, key=utf16_len, default="")
        deepest_dir = max(dir_tails, key=utf16_len, default="")
        # The SEMANTIC-MODEL family (issue #564): the model folder beside the report, and the
        # longest table part the engine actually wrote into it. Recorded per unit so the
        # projection >= actual control below compares production against real emitted bytes.
        unit_root = out / "pbip" / unit
        model_dirs = (
            [path for path in unit_root.iterdir() if path.is_dir() and path.name.endswith(".SemanticModel")]
            if unit_root.is_dir()
            else []
        )
        model_files = [
            str(path.relative_to(unit_root)).replace("\\", "/")
            for model in model_dirs
            for path in model.rglob("*")
            if path.is_file()
        ]
        deepest_model = max(model_files, key=utf16_len, default="")
        shapes[unit] = {
            "report": report,
            "pages": page_dirs,
            "visuals": visuals,
            "deepest_tail": deepest,
            "deepest_tail_len": utf16_len(deepest),
            "deepest_dir_tail": deepest_dir,
            "deepest_dir_tail_len": utf16_len(deepest_dir),
            "longest_visual_id": max((utf16_len(v) for v in visuals), default=0),
            "longest_page_id": max((utf16_len(p) for p in page_dirs), default=0),
            "model_folders": [path.name for path in model_dirs],
            "deepest_model_tail": deepest_model,
            "deepest_model_tail_len": utf16_len(deepest_model),
        }
    return {"version": engine_source.engine_version(engine), "root": out, "shapes": shapes}


@requires_engine
def test_a_standalone_datasource_emits_a_self_service_page_with_visuals(engine_bundle) -> None:
    """The refutation: a `.tds` unit is not structurally visual-free.

    `pageSelfService` is a hard-coded default of `twb_to_pbir.build_field_parameter_page`, so seeing
    it is the proof that the engine's **swap** branch was reached rather than the thin one — the
    production code path this fixture exists to exercise.
    """
    run = engine_bundle["shapes"][SWAP_UNIT]
    version = engine_bundle["version"]
    assert run["pages"] == [SELF_SERVICE_PAGE], (
        f"A standalone datasource emitted pages {run['pages']} on canonical engine {version}, not the "
        f"expected [{SELF_SERVICE_PAGE!r}]. If it now emits ['{THIN_PAGE}'], the swap branch was NOT "
        "reached and every engine assertion in this module is vacuous - fix the fixture before "
        "trusting any datasource path-envelope conclusion."
    )
    assert len(run["visuals"]) >= 1, (
        f"A standalone datasource emitted NO visuals on canonical engine {version}. That is the belief "
        "this fixture exists to refute; if the engine genuinely changed, a visual-free datasource "
        "envelope becomes arguable - re-derive it, do not simply delete this test."
    )
    assert run["longest_visual_id"] == ENGINE_IDENTIFIER_CAP, (
        f"The longest emitted datasource visual identifier is {run['longest_visual_id']} UTF-16 units, "
        f"not the {ENGINE_IDENTIFIER_CAP} that `twb_to_pbir._sanitize` caps at (observed "
        f"{run['visuals']} on engine {version}). Any datasource path envelope must cover whatever "
        "this is."
    )


@requires_engine
def test_the_engine_itself_resolves_both_declared_swap_controllers() -> None:
    """The engine's OWN parser must see the parameters, not just our XML reading of them.

    `test_every_swap_controller_is_a_declared_parameter` reads the `.tds` with this repository's
    interpretation of Tableau's shape. This runs `parameters.parse_parameters` and
    `parameters.detect_field_swap` from the canonical plugin over the same bytes, so the fixture is
    pinned against the consumer that actually matters.
    """
    engine = _contract()
    snippet = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from parameters import parse_parameters, detect_field_swap\n"
        "xml = open(sys.argv[2], encoding='utf-8').read()\n"
        "params = {p['caption']: {'domain': p['domain'], 'default': (p['default'] or '').strip('\\\"'),\n"
        "                          'members': p['members']} for p in parse_parameters(xml)}\n"
        "swaps = []\n"
        "import xml.etree.ElementTree as ET\n"
        "for col in ET.fromstring(xml).iter('column'):\n"
        "    calc = col.find('calculation')\n"
        "    if calc is None:\n"
        "        continue\n"
        "    sw = detect_field_swap(calc.get('formula') or '', role=col.get('role') or 'measure')\n"
        "    if sw:\n"
        "        swaps.append({'controller': sw['controller'],\n"
        "                      'branches': [b['label'] for b in sw['branches']]})\n"
        "print(json.dumps({'params': params, 'swaps': swaps}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", snippet, str(engine_source.engine_scripts_dir(engine)), str(MATRIX_SOURCES[SWAP_UNIT])],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, f"could not run the engine's own parameter parser:\n{done.stderr}"
    seen = json.loads(done.stdout)
    assert len(seen["swaps"]) == len(EXPECTED_CONTROLLERS), (
        f"the engine detected {len(seen['swaps'])} field swap(s), expected {len(EXPECTED_CONTROLLERS)}: {seen['swaps']}"
    )
    for swap in seen["swaps"]:
        controller = swap["controller"]
        assert controller in seen["params"], (
            f"the engine detected a swap controlled by {controller!r} but its own `parse_parameters` "
            f"does not return that parameter (it returns {sorted(seen['params'])}). The fixture would "
            "be exercising regex tolerance, not a declared Tableau parameter."
        )
        declared = seen["params"][controller]
        expected = EXPECTED_CONTROLLERS[controller]
        assert declared["domain"] == expected["domain"], f"{controller}: engine saw domain {declared['domain']!r}"
        assert declared["members"] == expected["members"], f"{controller}: engine saw members {declared['members']}"
        assert declared["default"] == expected["default"], f"{controller}: engine saw default {declared['default']!r}"
        assert swap["branches"] == expected["members"], (
            f"{controller}: the swap's branch labels {swap['branches']} and the parameter's members "
            f"{expected['members']} must describe the same choice"
        )


@requires_engine
@pytest.mark.parametrize(
    ("unit", "expected_page", "expect_visuals"),
    [
        (THIN_UNIT, THIN_PAGE, False),
        (SWAP_UNIT, SELF_SERVICE_PAGE, True),
        (WORKBOOK_UNIT, None, True),
    ],
)
def test_the_three_source_shapes_emit_their_documented_reports(
    engine_bundle, unit: str, expected_page: str | None, expect_visuals: bool
) -> None:
    """One engine run, three source kinds - so no conclusion rests on a single artifact.

    (a) an ordinary datasource -> the thin `page1` shell with no visuals;
    (b) a field-swap datasource -> `pageSelfService` with capped visual identifiers;
    (c) a real visual-owning workbook -> a `_sanitize`d page and a capped visual identifier.
    """
    shape = engine_bundle["shapes"][unit]
    version = engine_bundle["version"]
    assert len(shape["pages"]) == 1, f"{unit} emitted pages {shape['pages']} on engine {version}"
    if expected_page is not None:
        assert shape["pages"] == [expected_page], (
            f"{unit} emitted {shape['pages']}, expected [{expected_page!r}] on engine {version}"
        )
    else:
        assert utf16_len(shape["pages"][0]) <= ENGINE_IDENTIFIER_CAP, (
            f"{unit} emitted page id {shape['pages'][0]!r} longer than `_sanitize`'s cap"
        )
    if expect_visuals:
        assert shape["visuals"], f"{unit} emitted no visuals on engine {version}"
        assert shape["longest_visual_id"] == ENGINE_IDENTIFIER_CAP, (
            f"{unit}: longest visual id {shape['longest_visual_id']}, expected the "
            f"{ENGINE_IDENTIFIER_CAP}-unit `_sanitize` cap (observed {shape['visuals']})"
        )
    else:
        assert not shape["visuals"], (
            f"{unit} emitted visuals {shape['visuals']} on engine {version}. An ordinary datasource "
            "is expected to be the thin shell; if that changed, the whole kind distinction moves."
        )


@requires_engine
@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("unit", sorted(MATRIX_SOURCES))
def test_the_production_projection_refuses_each_emitted_shape_at_its_own_boundary(
    engine_bundle, unit: str, kind: str
) -> None:
    """The PRODUCTION projector, at a root derived from what the engine really wrote.

    ⚠️ Round-2 review: the assertion this replaces compared a **test-owned** `ENVELOPE_TAIL`
    constant against the emitted tail, and closed with the tautology
    `(CEILING - (actual - 1) - 1) + 1 + actual == CEILING + 1`, which is true for every `actual`.
    Understating production's own `_PBIR_VISUAL_ID` by four units left all of it green.

    So the root here is sized so the **actually emitted** path measures exactly `ceiling + 1`, and
    `run_estate.project_estate_path_ceiling` — the real function — must refuse it, naming the right
    kind, ceiling and unit, and projecting a length that COVERS the real one.

    ⚠️ Honest limit of this boundary, stated rather than implied: it is only as sensitive as the
    slack between production's envelope and *this fixture's* instance. The workbook fixture emits a
    19-unit page id and a 24-unit visual id against a 24 + 28 envelope, i.e. 9 units of slack, so a
    four-unit understatement still covers here. What bounds the envelope against the engine's own
    maximum is `test_the_production_identifier_envelope_stays_above_the_engines_measured_cap`.
    """
    shape = engine_bundle["shapes"][unit]
    tail = shape["deepest_tail"] if kind == "file" else shape["deepest_dir_tail"]
    ceiling = FILE_CEILING if kind == "file" else DIR_CEILING
    assert tail, f"{unit}: the engine emitted no {kind} under its .Report folder"

    relative = f"pbip/{unit}/{unit}.Report/{tail}"
    root = _root_for_length(ceiling + 1 - 1 - utf16_len(relative))
    at_root = utf16_len(str(root)) + 1 + utf16_len(relative)
    assert at_root == ceiling + 1, (
        f"harness error: the emitted {kind} measures {at_root} at the constructed root, not the intended {ceiling + 1}"
    )

    projection = run_estate.project_estate_path_ceiling(root, [unit], PBIR_ONLY_EVIDENCE)
    record = next(
        r
        for r in projection["paths"]
        if r["kind"] == kind and r["family"] == run_estate._FAMILY_PBIR  # pylint: disable=protected-access
    )

    assert record["length"] >= at_root, (
        f"{unit}/{kind}: the PRODUCTION envelope projects {record['length']} units where the engine "
        f"really wrote {at_root} ({relative!r}). An envelope that does not cover real output is "
        "fail-open by construction - this is the assertion a shortened production identifier breaks."
    )
    assert projection["status"] == "over_ceiling", (
        f"{unit}/{kind}: production reported {projection['status']!r} at a root where its own emitted "
        f"{kind} measures {at_root}, one unit over the {ceiling} ceiling"
    )
    assert record["ceiling"] == ceiling, f"{unit}/{kind}: production judged against ceiling {record['ceiling']}"
    assert projection["longest_unit"] == unit, f"{unit}/{kind}: production named unit {projection['longest_unit']!r}"
    assert any(offender["kind"] == kind for offender in projection["offenders"]), (
        f"{unit}/{kind}: production refused, but not on the {kind} rule: "
        f"{[(o['kind'], o['length'], o['ceiling']) for o in projection['offenders']]}"
    )


@requires_engine
def test_the_production_identifier_envelope_stays_above_the_engines_measured_cap(engine_bundle) -> None:
    """Production's projected identifiers must bound what the engine can actually emit.

    `_sanitize` returns `name[:24]`, and this bundle's three shapes are measured, not assumed. The
    projector deliberately carries `_PBIR_IDENTIFIER_SAFETY_MARGIN` on top of that
    ("so the envelope remains conservative for a future engine identifier", `run_estate.py`), so an
    envelope that has been trimmed back to — or below — the observed cap has spent a margin the
    module documents as intentional. This is the assertion that a four-unit understatement breaks,
    where a per-fixture boundary cannot: it compares production against the ENGINE's maximum rather
    than against one artifact's instance.
    """
    measured_visual = max(shape["longest_visual_id"] for shape in engine_bundle["shapes"].values())
    measured_page = max(shape["longest_page_id"] for shape in engine_bundle["shapes"].values())
    projected_visual = utf16_len(run_estate._PBIR_VISUAL_ID)  # pylint: disable=protected-access
    projected_page = utf16_len(run_estate._PBIR_PAGE_ID)  # pylint: disable=protected-access
    margin = run_estate._PBIR_IDENTIFIER_SAFETY_MARGIN  # pylint: disable=protected-access

    assert measured_visual == ENGINE_IDENTIFIER_CAP, (
        f"the engine's longest emitted visual identifier across all three shapes is {measured_visual}, "
        f"not `_sanitize`'s {ENGINE_IDENTIFIER_CAP}-unit cap, on engine {engine_bundle['version']}"
    )
    assert projected_visual >= measured_visual + margin, (
        f"production projects a {projected_visual}-unit visual identifier, which is not the measured "
        f"engine cap ({measured_visual}) plus the documented safety margin ({margin}). Trimming this "
        "spends a margin `run_estate` states it keeps on purpose, and no per-fixture boundary test "
        "will notice, because every committed fixture emits shorter identifiers than the cap allows."
    )
    assert projected_page >= measured_page, (
        f"production projects a {projected_page}-unit page identifier, under the {measured_page} the "
        f"engine emitted on engine {engine_bundle['version']}"
    )


@requires_engine
def test_a_visual_free_datasource_envelope_would_be_fail_open(engine_bundle) -> None:
    """PERMANENT INVARIANT — must hold before AND after any kind-aware envelope lands.

    A datasource envelope modelled on the thin shell understates the SWAP datasource's own emitted
    path. Fail-open is the direction that ships a tree Power BI Desktop refuses, so this is the
    assertion that kills the over-broad remedy.
    """
    swap = engine_bundle["shapes"][SWAP_UNIT]["deepest_tail_len"]
    thin_envelope = ENVELOPE_TAIL[THIN_UNIT]
    assert swap > thin_envelope, (
        f"A visual-free datasource envelope ({thin_envelope}) no longer understates the swap "
        f"datasource's emitted tail ({swap}) on engine {engine_bundle['version']}."
    )
    assert swap - thin_envelope >= 40, (
        f"The understatement shrank to {swap - thin_envelope} characters (emitted {swap} vs "
        f"visual-free {thin_envelope}). Measured 45 on engine 2.368.0. A shrinking gap is a signal "
        "the engine changed its datasource report shape, not a licence to relax the envelope."
    )
    boundary_root = FILE_CEILING - thin_envelope - 1
    assert boundary_root + 1 + swap > FILE_CEILING, (
        "a thin-shell envelope would allow a root at which the swap datasource's own emitted path "
        f"measures {boundary_root + 1 + swap}, over the {FILE_CEILING} ceiling - that is the "
        "fail-open outcome, stated as the refusal it should have produced"
    )


@requires_engine
def test_the_projector_is_blind_to_unit_kind_and_over_projects_this_datasource(engine_bundle) -> None:
    """The classification defect, stated against the production function and real emitted paths.

    `project_estate_path_ceiling` takes only NAMES, so it cannot tell a datasource from a workbook
    and assigns both the workbook worst case.

    ⚠️ The gap is real but it is NOT the whole of run 409's over-refusal: with the *sound*
    datasource envelope this fixture measures (`pageSelfService` + a 24-unit visual) that unit still
    projects over the ceiling. Deleting the pessimism is worth doing; it is not a fix.
    """
    shape = engine_bundle["shapes"][SWAP_UNIT]
    root = engine_bundle["root"]
    projection = run_estate.project_estate_path_ceiling(root, [SWAP_UNIT], PBIR_ONLY_EVIDENCE)
    projected_file = next(
        r
        for r in projection["paths"]
        if r["kind"] == "file" and r["family"] == run_estate._FAMILY_PBIR  # pylint: disable=protected-access
    )["length"]
    actual_file = max(utf16_len(str(p)) for p in shape["report"].rglob("*") if p.is_file())

    assert projected_file > actual_file, (
        f"The projection ({projected_file}) no longer exceeds the emitted report path ({actual_file}) "
        f"for datasource unit {SWAP_UNIT!r} on engine {engine_bundle['version']}. If the projector "
        "became kind-aware, re-derive this expectation from the new envelope rather than deleting it."
    )
    sound = utf16_len(str(root)) + 1 + utf16_len(f"pbip/{SWAP_UNIT}/{SWAP_UNIT}.Report") + 1 + ENVELOPE_TAIL[SWAP_UNIT]
    assert sound < projected_file, (
        f"A kind-aware datasource envelope ({sound}) is no cheaper than the workbook worst case "
        f"({projected_file}); the classification this module argues for would buy nothing."
    )
    assert sound >= actual_file, (
        f"The sound datasource envelope ({sound}) does not cover the emitted path ({actual_file}); an "
        "envelope that fails to cover real output is fail-open by construction."
    )


@requires_engine
def test_the_model_family_projection_covers_every_emitted_shape(engine_bundle) -> None:
    """Issue #564, against real engine output: an ORDINARY datasource, a FIELD-SWAP datasource and a
    real workbook, each judged on the semantic-model table part the engine actually wrote.

    The comparison is `projected >= actual` on the emitted bytes, not against an expected filename:
    an expected-string test cannot see a naming class the engine has and this repository does not,
    which is exactly how three successive class-by-class projectors were defeated (issue #564).
    """
    engine = _contract()
    evidence = run_estate.model_envelope_evidence(sorted(MATRIX_SOURCES.values()), engine)
    assert evidence["status"] == "ok", (
        f"the production evidence could not be established on engine {engine_bundle['version']}: "
        f"{evidence.get('reason')}. If the engine moved, re-audit the write-site census - do not "
        "relax the gate."
    )

    root = engine_bundle["root"]
    projection = run_estate.project_estate_path_ceiling(root, sorted(MATRIX_SOURCES), evidence)
    projected = max(
        record["length"]
        for record in projection["paths"]
        if record["family"] == run_estate._FAMILY_MODEL  # pylint: disable=protected-access
        and record["kind"] == "file"
    )

    for unit, shape in engine_bundle["shapes"].items():
        assert shape["model_folders"], (
            f"{unit} emitted no `.SemanticModel` folder on engine {engine_bundle['version']}; this "
            "control would then be vacuous"
        )
        actual = utf16_len(str(root / "pbip" / unit / shape["deepest_model_tail"]))
        assert projected >= actual, (
            f"{unit}: production projects {projected} units for the semantic-model family where the "
            f"engine really wrote {actual} ({shape['deepest_model_tail']!r}). A projection that does "
            "not cover real output is fail-open by construction - this is issue #564."
        )


@requires_engine
@pytest.mark.skipif(bool(os.environ.get(CHILD_SENTINEL)), reason="child of the concurrency control - would recurse")
def test_two_concurrent_pytest_processes_do_not_collide() -> None:
    """The control for the shared-root race this module used to have.

    Two pytest processes run the engine-backed shape matrix at the same time. With the old fixed
    `.pytest_cache` root one would `rmtree` the other's output mid-read; with a per-process
    `tmp_path_factory` directory both must succeed and must report different output roots.
    """
    env = dict(os.environ)
    env[CHILD_SENTINEL] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    selector = "test_the_three_source_shapes_emit_their_documented_reports"
    children = [
        subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
            [
                sys.executable,
                "-m",
                "pytest",
                f"{Path(__file__).name}::{selector}",
                "-q",
                "--no-header",
                "-p",
                "no:randomly",
            ],
            cwd=REPO / "tests",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for _ in range(2)
    ]
    outputs = [child.communicate(timeout=1800)[0] for child in children]
    codes = [child.returncode for child in children]
    assert codes == [0, 0], (
        "concurrent pytest processes did not both succeed - a shared, destructively cleared engine "
        f"output root is the usual cause.\nexit codes: {codes}\n" + "\n---\n".join(out[-3000:] for out in outputs)
    )
