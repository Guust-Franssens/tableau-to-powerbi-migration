"""
purpose: Pin the integrity of tests/fixtures/issue-185-set-filter.twb, the reproduction fixture for
         upstream Yarbrdab000/tableau-fabric-skills#185 (an unresolvable Tableau Set filter is
         silently dropped and the visual still emits). The fixture only has evidential value while
         it remains (a) a real Tableau Set and (b) a RESTRICTIVE filter; either property rotting
         away would turn a live reproduction into a file that proves nothing.
usage:   pytest -q tests/test_issue_185_set_fixture.py
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from parse_tableau import parse_workbook  # noqa: E402  (path insert must precede this import)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue-185-set-filter.twb"
SET_NAME = "[Technology Set]"
IO_INSTANCE = "[io:Technology Set:nk]"


def _root():
    return ET.parse(FIXTURE).getroot()


def _sets(root):
    """Tableau encodes a SET as a <group> holding a <groupfilter>; there is no class='set'."""
    return [g for g in root.iter("group") if g.find("groupfilter") is not None]


def test_fixture_exists():
    """The fixture is the reproduction; losing the file loses the evidence."""
    assert FIXTURE.is_file(), f"reproduction fixture missing: {FIXTURE}"


def test_fixture_declares_a_real_tableau_set():
    """A synthetic file that is not a genuine Tableau Set would prove nothing upstream."""
    groups = {g.get("name"): g for g in _sets(_root())}
    assert SET_NAME in groups, f"expected a set named {SET_NAME}, found {sorted(groups)}"


def test_set_domain_is_a_condition_not_an_enumeration():
    """Hemang Patel's reported domain was a condition (`TECHNOLOGY <> "NC"`), not a member list.

    A set whose domain is an enumerated member list is a different (easier) translation target, so
    if this degrades into `function='member'` the fixture no longer reproduces the reported shape.
    """
    group = {g.get("name"): g for g in _sets(_root())}[SET_NAME]
    gf = group.find("groupfilter")
    assert gf.get("function") == "filter", f"set domain is {gf.get('function')!r}, expected 'filter'"
    assert "!=" in (gf.get("expression") or ""), "set domain lost its condition expression"


def test_set_is_referenced_through_an_inout_column_instance():
    """A view reaches a set only through its IN/OUT membership instance."""
    root = _root()
    io = [c for c in root.iter("column-instance") if c.get("name") == IO_INSTANCE]
    assert io, f"no column-instance named {IO_INSTANCE}"
    assert all(c.get("derivation") == "InOut" for c in io), "set instance must use derivation='InOut'"


def test_worksheet_filter_on_the_set_is_restrictive():
    """The whole point of the fixture: dropping this filter must WIDEN the result set.

    A Tableau set filter written as `level-members` + `ui-enumeration='all'` selects both In and
    Out, so dropping it is a no-op and proves nothing about correctness. This fixture keeps only
    the "In" members.
    """
    root = _root()
    filters = [f for f in root.iter("filter") if (f.get("column") or "").endswith(f".{IO_INSTANCE}")]
    assert filters, f"no worksheet filter on {IO_INSTANCE}"
    for filt in filters:
        markers = [gf.get("function") for gf in filt.iter("groupfilter")]
        assert "member" in markers, f"filter is not member-restricted: {markers}"
        assert "level-members" not in markers, "filter degraded to an all-members (no-op) selection"
        members = [gf.get("member") for gf in filt.iter("groupfilter") if gf.get("function") == "member"]
        assert members == ['"In"'], f"expected only the In members, got {members}"


def test_our_parser_records_the_filter_even_though_the_set_field_is_unresolved():
    """Our tier's behaviour, contrasted with the engine's.

    `parse_tableau.py` cannot resolve a set field either, but it PRESERVES the filter and its
    members behind an `UNRESOLVED:` marker. The engine drops the filter entirely and still emits
    the visual, which is the defect reported upstream. Pinning our side means a future change that
    silently discards the filter here would fail loudly.
    """
    spec = parse_workbook(FIXTURE)
    worksheets = {w["name"]: w for w in spec["worksheets"]}
    assert "Score by Tail" in worksheets, sorted(worksheets)
    filters = worksheets["Score by Tail"]["filters"]
    assert len(filters) == 1, f"expected exactly one filter, got {filters}"
    only = filters[0]
    assert only["field_id"].startswith("UNRESOLVED:"), only["field_id"]
    assert IO_INSTANCE in only["field_id"], only["field_id"]
    assert only["members"] == ['"In"'], only["members"]
