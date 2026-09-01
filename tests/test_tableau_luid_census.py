"""Offline contract for `tableau_luid_census`.

⚠️ The point of this file is that the census cannot be trusted to be safe by inspection. It handles a
credentialed response, so "it only prints counts" has to be ENFORCED and then TESTED, not asserted in
a docstring. `_emit`'s refusal is the enforcement; these are the tests.

The live half -- whether a real site actually emits blank LUIDs -- cannot be tested offline, and
deliberately is not faked here. It is recorded as a measurement in the #402 fixtures' docstrings, and
re-measurable at any time by running the script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tableau_luid_census as census_mod  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_view_types as view_types_mod  # noqa: E402  # pylint: disable=wrong-import-position

LUID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def _payload(workbooks):
    return {"data": {"workbooks": workbooks}}


# --------------------------------------------------------------------------- the safety enforcement


@pytest.mark.parametrize(
    "value",
    [
        "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
        "Regional Sales Dashboard",
        ["a", "b"],
        {"luid": "x"},
        3.5,
        b"bytes",
    ],
)
def test_emit_refuses_anything_that_could_carry_an_identifier(value):
    """⚠️ The load-bearing safety property, and it must be a REFUSAL rather than a convention.

    This module reads a credentialed, site-wide response. The promise that it reports "counts and
    shapes only" is worth nothing if a later edit can quietly print a workbook name -- so `_emit`
    raises instead, and a careless change fails loudly at the first call.
    """
    with pytest.raises(SystemExit):
        census_mod._emit("label", value)  # pylint: disable=protected-access


@pytest.mark.parametrize("value", [0, 42, True, False, None])
def test_emit_allows_counts_and_flags(value):
    """The other half: refusing everything would be safe and useless."""
    census_mod._emit("label", value)  # pylint: disable=protected-access


def test_the_census_never_reads_a_luid_VALUE_only_its_shape():
    """Two sites, identical shapes, completely different identifiers -> identical census.

    That is the structural statement of "no identity is ever recorded". A census that varied with the
    luid values would be carrying them, however indirectly.
    """
    one = census_mod.census(_payload([{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}]}]))
    two = census_mod.census(_payload([{"dashboards": [{"luid": OTHER}], "sheets": [{"luid": "   "}]}]))
    assert one == two


# --------------------------------------------------------------------------- the census itself


def test_a_blank_luid_is_counted_separately_from_a_malformed_one():
    """⚠️ The distinction the whole #402 round-2 fix rests on, asserted at the census layer too.

    If these ever landed in one bucket the census would report a healthy site as broken, or hide a
    real malformed identifier among the expected hidden-sheet blanks.
    """
    counts = census_mod.classify([{"luid": LUID}, {"luid": ""}, {"luid": "   "}, {"luid": "D-1"}, {"luid": None}])
    assert counts == {
        "total": 5,
        "missing_key": 0,
        "non_string": 1,
        "blank": 2,
        "uuid": 1,
        "non_uuid_non_blank": 1,
    }


def test_a_node_with_no_luid_key_is_its_own_bucket():
    """Absent and null are different failures upstream, so they stay different here."""
    counts = census_mod.classify([{"name": "x"}, {"luid": None}])
    assert counts["missing_key"] == 1
    assert counts["non_string"] == 1


def test_a_workbook_is_counted_once_however_many_blanks_it_holds():
    """ "How many workbooks are affected" is the number an operator acts on, not the node total."""
    totals = census_mod.census(
        _payload([{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}, {"luid": ""}, {"luid": ""}]}])
    )
    assert totals["blank_luids"] == 3
    assert totals["workbooks_with_a_blank_luid"] == 1


def test_a_workbook_missing_a_declared_collection_is_reported_not_skipped_silently():
    """The schema declares both non-null, so an absent one is a finding rather than an empty list."""
    totals = census_mod.census(_payload([{"dashboards": [{"luid": LUID}]}]))
    assert totals["workbooks_missing_a_collection"] == 1
    assert totals["nodes"] == 0


@pytest.mark.parametrize(
    "totals, expected",
    [
        ({"blank_luids": 116, "nodes": 476}, "CONFIRMED"),
        ({"blank_luids": 0, "nodes": 476}, "NOT-PRESENT"),
        ({"blank_luids": 0, "nodes": 0}, "CANNOT-TELL"),
    ],
)
def test_the_three_verdicts_are_distinguishable(totals, expected):
    """⚠️ CANNOT-TELL must not collapse into NOT-PRESENT.

    "No blank luids" and "nothing here could have had one" are different claims, and only the first
    is evidence about the site. Collapsing them is how a vacuous run gets cited as a clean result --
    the defect class this repository keeps finding in its own gates.
    """
    assert census_mod.verdict(totals) == expected


def test_the_census_and_the_parser_agree_on_what_counts_as_a_luid():
    """Two implementations of the shape rule would drift; there is deliberately only one.

    `classify` calls the same public `is_luid` the parser uses, so a change to the rule cannot leave
    the census describing a site by a rule the parser no longer applies.
    """
    assert census_mod.classify([{"luid": LUID}])["uuid"] == 1
    assert view_types_mod.is_luid(LUID)
    assert not view_types_mod.is_luid("")
    assert not view_types_mod.is_luid("D-1")


def test_the_json_output_carries_only_integer_counts(tmp_path):
    """`--json` writes a file someone may paste into an issue, so it must be safe by construction."""
    totals = census_mod.census(_payload([{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}]}]))
    out = tmp_path / "census.json"
    out.write_text(json.dumps(totals, indent=2, sort_keys=True), encoding="utf-8")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded and all(isinstance(v, int) for v in loaded.values())
    assert LUID not in out.read_text(encoding="utf-8")
