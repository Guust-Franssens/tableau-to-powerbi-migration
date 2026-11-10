"""
purpose: Pin tests/fixtures/issue-185-*.twb, the reproductions for upstream
         Yarbrdab000/tableau-fabric-skills#185, and our parser's handling of the unresolvable Set filter.
usage:   pytest -q tests/test_issue_185_set_fixture.py
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from parse_tableau import parse_workbook  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue-185-set-filter.twb"
BAR_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue-185-bar-shelf-layout.twb"
SET_NAME = "[Technology Set]"
IO_INSTANCE = "[io:Technology Set:nk]"


def test_set_fixture_is_a_conditional_set_with_a_restrictive_inout_filter():
    """The fixture only reproduces #185 while dropping the set filter would WIDEN the result."""
    root = ET.parse(FIXTURE).getroot()
    groups = {g.get("name"): g for g in root.iter("group") if g.find("groupfilter") is not None}
    domain = groups[SET_NAME].find("groupfilter")
    assert domain.get("function") == "filter"
    assert "!=" in (domain.get("expression") or "")
    io = [c for c in root.iter("column-instance") if c.get("name") == IO_INSTANCE]
    assert io and all(c.get("derivation") == "InOut" for c in io)
    filters = [f for f in root.iter("filter") if (f.get("column") or "").endswith(f".{IO_INSTANCE}")]
    assert filters
    for filt in filters:
        markers = [gf.get("function") for gf in filt.iter("groupfilter")]
        assert "member" in markers and "level-members" not in markers
        assert [gf.get("member") for gf in filt.iter("groupfilter") if gf.get("function") == "member"] == ['"In"']


def test_our_parser_records_the_filter_even_though_the_set_field_is_unresolved():
    """Unlike the engine (the #185 defect), our parser preserves the filter behind UNRESOLVED:."""
    worksheets = {w["name"]: w for w in parse_workbook(FIXTURE)["worksheets"]}
    [only] = worksheets["Score by Tail"]["filters"]
    assert only["field_id"].startswith("UNRESOLVED:")
    assert IO_INSTANCE in only["field_id"]
    assert only["members"] == ['"In"']


def _shelf_signature(root, name):
    ws = next(w for w in root.iter("worksheet") if w.get("name") == name)
    pane = ws.find(".//pane")
    encodings = sorted((child.tag, child.get("column")) for child in pane.find("encodings"))
    return pane.find("mark").get("class"), ws.find(".//rows").text.strip(), ws.find(".//cols").text.strip(), encodings


def test_bar_fixture_differs_only_in_mark_class_over_a_measure_measure_layout():
    """The A/B control: explicit Bar vs Automatic over identical measure-on-both-axes shelves."""
    root = ET.parse(BAR_FIXTURE).getroot()
    auto = _shelf_signature(root, "Auto Two Measures")
    bar = _shelf_signature(root, "Bar Two Measures")
    assert (auto[0], bar[0]) == ("Automatic", "Bar")
    assert auto[1:] == bar[1:]
    assert "sum:" in bar[1] and "sum:" in bar[2]
    assert bar[3] == [("lod", "[federated.bards].[none:Tail:nk]")]
