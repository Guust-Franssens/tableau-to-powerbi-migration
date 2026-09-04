"""Tests for scripts/resolve_datasource_target.py (issue #368).

The field report: a request to migrate the PUBLISHED datasource `DS_CAPS` was silently applied
instead to a similarly (but not identically) named, workbook-EMBEDDED datasource `IA_CAPS_DS`. It
was NOT a strict name collision - a duplicate-name check would not have caught it - so the guard
under test is a CLASS guard: a named-target request must resolve within its declared class, and
must REFUSE (never fall back to a near match) when it does not.

Three outcomes must stay distinguishable, and each gets its own test: resolved unambiguously
(`test_an_exact_published_name_request_resolves_without_refusing`), genuinely absent
(`test_a_name_that_matches_only_the_wrong_class_is_absent_not_resolved`), and ambiguous across
classes (`test_a_name_matching_both_classes_is_ambiguous_not_resolved_by_preference`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import resolve_datasource_target as rdt  # noqa: E402  # pylint: disable=wrong-import-position


def _raw(
    *,
    published: list[dict] | None = None,
    workbooks: list[dict] | None = None,
    embedded_by_workbook: dict[str, list[str]] | None = None,
) -> dict:
    """Build a raw evidence dict shaped like `assess_estate.py`'s `<out>/raw/*.json` files."""
    workbooks = workbooks or []
    embedded_by_workbook = embedded_by_workbook or {}
    return {
        "datasources": published or [],
        "workbooks": workbooks,
        "structure": {
            "workbooks": [
                {"name": name, "embeddedDatasources": [{"name": n} for n in names]}
                for name, names in embedded_by_workbook.items()
            ]
        },
    }


# --------------------------------------------------------------------------------------------
# The three outcomes, kept distinguishable.
# --------------------------------------------------------------------------------------------


def test_an_exact_published_name_request_resolves_without_refusing() -> None:
    """AC5: an exact published-name request resolves without prompting."""
    raw = _raw(
        published=[{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}}],
        workbooks=[{"id": "wb-1", "name": "IA CAPS Dashboard", "project": {"name": "Ops"}}],
        embedded_by_workbook={"IA CAPS Dashboard": ["IA_CAPS_DS"]},
    )
    resolution = rdt.resolve(raw, "DS_CAPS", rdt.CLASS_PUBLISHED)

    assert resolution.outcome == rdt.RESOLVED
    assert resolution.target is not None
    assert resolution.target.cls == rdt.CLASS_PUBLISHED
    assert resolution.target.luid == "pub-1"
    assert resolution.target.project == "Finance"


def test_a_name_that_matches_only_the_wrong_class_is_absent_not_resolved() -> None:
    """AC2: a request matching only a similar (not identical) EMBEDDED name refuses.

    `IA_CAPS_DS` genuinely exists, but only as an embedded datasource - requesting it as PUBLISHED
    must not silently resolve to the embedded hit (the exact #368 failure, inverted so the request
    itself targets the wrong class).
    """
    raw = _raw(
        published=[{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}}],
        workbooks=[{"id": "wb-1", "name": "IA CAPS Dashboard", "project": {"name": "Ops"}}],
        embedded_by_workbook={"IA CAPS Dashboard": ["IA_CAPS_DS"]},
    )
    resolution = rdt.resolve(raw, "IA_CAPS_DS", rdt.CLASS_PUBLISHED)

    assert resolution.outcome == rdt.ABSENT
    assert resolution.target is None
    # The trap: do not turn this into a silent no-match either - the other-class hit is reported.
    assert len(resolution.other_class_matches) == 1
    assert resolution.other_class_matches[0].cls == rdt.CLASS_EMBEDDED


def test_a_name_matching_both_classes_is_ambiguous_not_resolved_by_preference() -> None:
    """AC3: a name matching entities of BOTH classes is surfaced as the dangerous case."""
    raw = _raw(
        published=[{"id": "pub-1", "name": "SharedName", "project": {"name": "Finance"}}],
        workbooks=[{"id": "wb-1", "name": "WB2", "project": {"name": "Ops"}}],
        embedded_by_workbook={"WB2": ["SharedName"]},
    )
    resolution = rdt.resolve(raw, "SharedName", rdt.CLASS_PUBLISHED)

    assert resolution.outcome == rdt.AMBIGUOUS
    assert resolution.target is None
    assert len(resolution.requested_class_matches) == 1
    assert len(resolution.other_class_matches) == 1


def test_a_name_absent_everywhere_is_absent() -> None:
    """Genuinely absent from the whole estate, not merely from the requested class."""
    raw = _raw(published=[{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}}])
    resolution = rdt.resolve(raw, "NoSuchThing", rdt.CLASS_PUBLISHED)

    assert resolution.outcome == rdt.ABSENT
    assert not resolution.other_class_matches
    assert not resolution.requested_class_matches


def test_a_duplicate_name_within_one_class_is_ambiguous() -> None:
    """Two published datasources sharing an exact name (different projects) is also ambiguous."""
    raw = _raw(
        published=[
            {"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}},
            {"id": "pub-2", "name": "DS_CAPS", "project": {"name": "Ops"}},
        ]
    )
    resolution = rdt.resolve(raw, "DS_CAPS", rdt.CLASS_PUBLISHED)

    assert resolution.outcome == rdt.AMBIGUOUS
    assert len(resolution.requested_class_matches) == 2


def test_a_near_match_never_resolves_case_or_whitespace_normalized() -> None:
    """Matching is EXACT ONLY - no normalized fallback. A near-spelling must REFUSE, never resolve."""
    raw = _raw(published=[{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}}])

    resolution = rdt.resolve(raw, "ds_caps", rdt.CLASS_PUBLISHED)

    assert resolution.outcome == rdt.ABSENT


# --------------------------------------------------------------------------------------------
# outcome/exit-code contract and the CLI
# --------------------------------------------------------------------------------------------


def test_resolution_outcome_names_are_the_three_distinguishable_states() -> None:
    """The trap: resolved / genuinely absent / ambiguous must stay three distinct string values."""
    assert len({rdt.RESOLVED, rdt.ABSENT, rdt.AMBIGUOUS}) == 3


def test_exit_codes_match_outcome_for_each_of_the_three_cases(tmp_path: Path) -> None:
    """Gates are judged by EXIT CODE: resolved=0, absent=1, ambiguous=2."""
    _write_raw_dir(
        tmp_path,
        published=[
            {"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}},
            {"id": "pub-2", "name": "Dup", "project": {"name": "A"}},
        ],
    )
    _write_raw_dir_append_dup(tmp_path)

    assert rdt.main(["--raw", str(tmp_path), "--name", "DS_CAPS", "--class", "published"]) == 0
    assert rdt.main(["--raw", str(tmp_path), "--name", "Nope", "--class", "published"]) == 1
    assert rdt.main(["--raw", str(tmp_path), "--name", "Dup", "--class", "published"]) == 2


def _write_raw_dir(tmp_path: Path, *, published: list[dict]) -> None:
    """Write a minimal `<out>/raw/*.json` triple with only the published datasources given."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "datasources.json").write_text(json.dumps(published), encoding="utf-8")
    (raw_dir / "workbooks.json").write_text("[]", encoding="utf-8")
    (raw_dir / "structure.json").write_text(json.dumps({"workbooks": []}), encoding="utf-8")


def _write_raw_dir_append_dup(tmp_path: Path) -> None:
    """Append a second published datasource named "Dup" to an already-written raw dir."""
    raw_dir = tmp_path / "raw"
    data = json.loads((raw_dir / "datasources.json").read_text(encoding="utf-8"))
    data.append({"id": "pub-3", "name": "Dup", "project": {"name": "B"}})
    (raw_dir / "datasources.json").write_text(json.dumps(data), encoding="utf-8")


def test_load_raw_accepts_either_the_out_dir_or_its_raw_subfolder(tmp_path: Path) -> None:
    """`--raw` may point at `assess_estate.py --out` itself, or directly at its `raw/` subfolder."""
    _write_raw_dir(tmp_path, published=[{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "F"}}])

    from_root = rdt.load_raw(tmp_path)
    from_subfolder = rdt.load_raw(tmp_path / "raw")

    assert (
        from_root["datasources"]
        == from_subfolder["datasources"]
        == [{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "F"}}]
    )


def test_json_output_round_trips_the_resolution(tmp_path: Path) -> None:
    """`--json` writes the same outcome the CLI prints, machine-readably."""
    _write_raw_dir(tmp_path, published=[{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}}])
    out = tmp_path / "resolution.json"

    exit_code = rdt.main(["--raw", str(tmp_path), "--name", "DS_CAPS", "--class", "published", "--json", str(out)])

    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["outcome"] == "resolved"
    assert payload["target"]["luid"] == "pub-1"


# --------------------------------------------------------------------------------------------
# estate-wide hazard surfacing (AC4)
# --------------------------------------------------------------------------------------------


def test_estate_wide_hazards_report_cross_class_and_within_class_collisions() -> None:
    """AC4: same-name hazards across BOTH classes and within one class are surfaced together."""
    raw = _raw(
        published=[
            {"id": "pub-1", "name": "SharedName", "project": {"name": "Finance"}},
            {"id": "pub-2", "name": "DupPub", "project": {"name": "A"}},
            {"id": "pub-3", "name": "DupPub", "project": {"name": "B"}},
        ],
        workbooks=[{"id": "wb-1", "name": "WB2", "project": {"name": "Ops"}}],
        embedded_by_workbook={"WB2": ["SharedName"]},
    )
    hazards = rdt.datasource_class_hazards(raw)

    assert {"name": "SharedName", "classes": ["embedded", "published"]} in hazards["cross_class"]
    assert {"name": "DupPub", "class": "published", "count": 2} in hazards["duplicate_within_class"]


def test_estate_wide_hazards_are_empty_when_every_name_is_a_safe_unique_lookup() -> None:
    """A clean estate reports both hazard lists empty, never absent."""
    raw = _raw(published=[{"id": "pub-1", "name": "DS_CAPS", "project": {"name": "Finance"}}])

    hazards = rdt.datasource_class_hazards(raw)

    assert hazards == {"duplicate_within_class": [], "cross_class": []}


# --------------------------------------------------------------------------------------------
# a Resolution is never mistaken for a truthy proceed signal
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cls", list(rdt.CLASSES))
def test_only_resolved_outcome_carries_a_target(cls: str) -> None:
    """An ABSENT or AMBIGUOUS resolution must never carry a `.target` a caller could mistake for one."""
    raw = _raw()
    resolution = rdt.resolve(raw, "Anything", cls)
    assert resolution.outcome == rdt.ABSENT
    assert resolution.target is None
