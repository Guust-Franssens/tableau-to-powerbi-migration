"""The grouping step must never invent a destination, and never lose the capture.

`capture_tableau_oracle.py`'s flat, LUID-keyed layout is the authoritative artifact - it survives a
workbook or view rename, which a folder-per-workbook layout cannot. This script only makes that
capture browsable, so every test here pins one of the two ways "convenience" could cost evidence:

1. **guessing a destination** - slugifying a workbook name into a path creates folders that look
   like deliverables and are not, and silently splits a workbook across two spellings; and
2. **downgrading the evidence grade** - copying only the views that succeeded, so a per-workbook
   folder reads as a complete capture when it is not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import group_oracle_by_workbook as grp  # noqa: E402  # pylint: disable=wrong-import-position


def _view(workbook: str, name: str, luid: str, *, data="ok", image="ok", rows: int = 5, columns=("a", "b")):
    stem = f"{name}__{luid}"
    view: dict = {
        "view_luid": luid,
        "view_name": name,
        "workbook_luid": f"wb-{workbook}",
        "workbook_name": workbook,
    }
    view["data"] = (
        # ⚠️ `certification` is what makes this a CURRENT capture rather than a legacy one. Since
        # #480 round 3 a recorded `row_count` alone does not license an evidence `path` -- every
        # pre-certification manifest has one -- so a fixture that omits it is exercising the
        # unassessable path, not the happy one. The negative half is asserted deliberately in
        # `test_a_legacy_row_count_without_a_certification_is_not_evidence`.
        {
            "status": "ok",
            "certification": "certified",
            "path": f"data/{stem}.csv",
            "row_count": rows,
            "columns": list(columns),
        }
        if data == "ok"
        else {"status": data}
    )
    view["image"] = {"status": "ok", "path": f"images/{stem}.png"} if image == "ok" else {"status": image}
    return view


def _capture(tmp_path: Path, views: list[dict]) -> Path:
    """A capture directory on disk: the manifest plus every file its views claim."""
    oracle = tmp_path / "_oracle"
    (oracle / "data").mkdir(parents=True)
    (oracle / "images").mkdir(parents=True)
    for view in views:
        for kind in ("data", "image"):
            entry = view.get(kind) or {}
            if entry.get("status") == "ok" and entry.get("path"):
                (oracle / entry["path"]).write_bytes(b"payload")
    manifest = {"schema": "tableau-oracle/1", "captured_at": "2026-08-18T00:00:00Z", "views": views}
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return oracle


def _migrations(tmp_path: Path, *slugs: str) -> Path:
    root = tmp_path / "migrations" / "workbooks"
    for slug in slugs:
        (root / slug).mkdir(parents=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- happy path


def test_views_land_in_the_matching_existing_folder(tmp_path):
    oracle = _capture(tmp_path, [_view("Availability Summary", "Detail", "aaa")])
    root = _migrations(tmp_path, "availability-summary")
    assert grp.run(oracle, root, dry_run=False) == 0
    reference = root / "availability-summary" / "reference"
    assert (reference / "data" / "Detail__aaa.csv").is_file()
    assert (reference / "images" / "Detail__aaa.png").is_file()


def test_the_capture_is_copied_never_moved(tmp_path):
    """The flat capture stays authoritative; grouping must not consume it."""
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    grp.run(oracle, _migrations(tmp_path, "sales"), dry_run=False)
    assert (oracle / "data" / "V__aaa.csv").is_file(), "the source capture must survive grouping"
    assert (oracle / "images" / "V__aaa.png").is_file()


@pytest.mark.parametrize("folder", ["ds-tail-level", "DS_Tail_Level", "dstaillevel"])
def test_punctuation_and_case_do_not_prevent_a_match(tmp_path, folder):
    oracle = _capture(tmp_path, [_view("DS Tail Level", "V", "aaa")])
    root = _migrations(tmp_path, folder)
    assert grp.run(oracle, root, dry_run=False) == 0
    assert (root / folder / "reference" / "data" / "V__aaa.csv").is_file()


# --------------------------------------------------------------------------- never invent a folder


def test_a_workbook_with_no_folder_is_reported_and_no_folder_is_created(tmp_path):
    """THE test. Slugifying a name into a path manufactures something that looks like a deliverable.
    An absent destination is a fact to report, not a directory to create."""
    oracle = _capture(tmp_path, [_view("Not Migrated Yet", "V", "aaa")])
    root = _migrations(tmp_path, "something-else")
    assert grp.run(oracle, root, dry_run=False) == 1
    assert not (root / "not-migrated-yet").exists()
    assert not (root / "notmigratedyet").exists()
    report = json.loads((oracle / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert report["workbooks_unmatched"] == 1
    assert report["unmatched"][0]["workbook"] == "Not Migrated Yet"


def test_an_ambiguous_name_is_reported_rather_than_resolved_by_picking_one(tmp_path):
    """Two folders normalizing to one key is precisely when a confident answer is wrong."""
    oracle = _capture(tmp_path, [_view("Tail Level", "V", "aaa")])
    root = _migrations(tmp_path, "tail-level", "tail_level")
    assert grp.run(oracle, root, dry_run=False) == 1
    report = json.loads((oracle / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert report["workbooks_ambiguous"] == 1
    assert len(report["ambiguous"][0]["folders"]) == 2
    for slug in ("tail-level", "tail_level"):
        assert not (root / slug / "reference").exists(), "an ambiguous match must copy nothing"


def test_a_cross_project_caption_suffix_is_reported_unmatched_not_silently_matched(tmp_path):
    """`normalize()` drops punctuation, never words, so Tableau's ' | Project : X' suffix does not
    match the bare folder. Reporting that is honest; quietly matching it would be a guess."""
    oracle = _capture(tmp_path, [_view("DS Tail Level | Project : Enterprise Dashboards", "V", "aaa")])
    root = _migrations(tmp_path, "ds-tail-level")
    assert grp.run(oracle, root, dry_run=False) == 1
    assert not (root / "ds-tail-level" / "reference").exists()


# --------------------------------------------------------------------------- evidence grade


def test_a_failed_view_is_not_copied_but_is_still_counted_in_the_workbook_manifest(tmp_path):
    """A folder holding only the successes reads as a complete capture. It is not one."""
    views = [_view("Sales", "Good", "aaa"), _view("Sales", "Blocked", "bbb", data="source_credential")]
    oracle = _capture(tmp_path, views)
    root = _migrations(tmp_path, "sales")
    grp.run(oracle, root, dry_run=False)
    subset = json.loads((root / "sales" / "reference" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert subset["view_count"] == 2
    assert subset["data_ok"] == 1
    assert subset["credential_blocked"] == 1
    assert not (root / "sales" / "reference" / "data" / "Blocked__bbb.csv").exists()


def test_the_workbook_manifest_carries_capture_provenance(tmp_path):
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    root = _migrations(tmp_path, "sales")
    grp.run(oracle, root, dry_run=False)
    subset = json.loads((root / "sales" / "reference" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert subset["grouped_from"] == "tableau-oracle/1"
    assert subset["captured_at"] == "2026-08-18T00:00:00Z"
    assert subset["workbook_luid"] == "wb-Sales"


def test_a_zero_row_view_is_counted_AND_named_in_the_workbook_manifest(tmp_path):
    """#471 at this level too. A per-workbook subset that only COUNTS empties is the capture-wide
    defect one folder down: the reviewer working from `migrations/<slug>/reference/` is exactly the
    reader who has to know which page carries no evidence."""
    views = [_view("Sales", "Good", "aaa"), _view("Sales", "Blank", "bbb", rows=0, columns=("Region",))]
    oracle = _capture(tmp_path, views)
    root = _migrations(tmp_path, "sales")
    grp.run(oracle, root, dry_run=False)
    subset = json.loads((root / "sales" / "reference" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert subset["data_ok"] == 2, "an empty capture is still a SUCCESSFUL capture"
    assert subset["data_empty"] == 1
    assert [entry["view_name"] for entry in subset["data_empty_views"]] == ["Blank"]
    assert subset["data_empty_views"][0]["classification"] == "empty_query_no_rows"


def test_the_workbook_subset_uses_the_SAME_empty_predicate_as_the_capture(tmp_path):
    """Positive control plus the anti-drift claim. This module used to carry its own copy of
    `row_count == 0`, which is how a subset and the capture it came from disagree about the same
    views -- and that copy raised KeyError on a record that never recorded a row count."""
    no_row_count = _view("Sales", "Old", "ccc")
    del no_row_count["data"]["row_count"]
    oracle = _capture(tmp_path, [_view("Sales", "Good", "aaa"), no_row_count])
    root = _migrations(tmp_path, "sales")
    assert grp.run(oracle, root, dry_run=False) == 0
    subset = json.loads((root / "sales" / "reference" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert subset["data_empty"] == 0, "absence of a row count is not a zero"
    assert subset["data_empty_views"] == []


def test_a_view_with_no_row_count_is_counted_AND_named_UNASSESSABLE_in_the_workbook_manifest(tmp_path):
    """#480 finding 1, at this level. The reviewer ran their record through `subset_manifest()` and
    got `data_ok=1 data_empty=0 data_empty_views=[] failed=0` -- which is what a good capture looks
    like. "Not empty" was being read as "fine", so a per-workbook reader saw four clean captures
    where nothing had measured the rows of one of them.

    `data_ok` deliberately stays as it was: the export DID succeed, and the new pair beside it is
    what stops that number being read as evidence."""
    no_row_count = _view("Sales", "Old", "ccc")
    del no_row_count["data"]["row_count"]
    subset = grp.subset_manifest({"schema": "tableau-oracle/1"}, "Sales", [_view("Sales", "Good", "aaa"), no_row_count])
    assert subset["data_ok"] == 2
    assert subset["data_empty"] == 0
    assert subset["data_empty_views"] == []
    assert subset["data_unassessable"] == 1
    assert [entry["view_name"] for entry in subset["data_unassessable_views"]] == ["Old"]
    assert subset["data_unassessable_views"][0]["reason"] == "row_count_unrecorded"


def test_the_workbook_subset_reports_no_unassessable_views_when_every_row_count_was_measured(tmp_path):
    """Control for the test above: a list that names every view is a view count renamed."""
    subset = grp.subset_manifest(
        {"schema": "tableau-oracle/1"}, "Sales", [_view("Sales", "Good", "aaa"), _view("Sales", "Blank", "bbb", rows=0)]
    )
    assert subset["data_unassessable"] == 0
    assert subset["data_unassessable_views"] == []


def test_a_view_whose_file_vanished_is_reported_and_never_claimed_as_copied(tmp_path):
    """⚠️ This test used to assert exit 0 -- it PINNED the defect. `copy_view_files` warned and moved
    on while `subset_manifest` kept the source manifest's `status: ok`, its `path`, and its place in
    the success counts, so the grouped folder asserted evidence that was never copied. That is the
    exact shape an earlier round of this review had already fixed one level up."""
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    (oracle / "data" / "V__aaa.csv").unlink()
    root = _migrations(tmp_path, "sales")
    assert grp.run(oracle, root, dry_run=False) == 1
    # What DID copy still copies: partial evidence, honestly labelled, beats nothing.
    assert (root / "sales" / "reference" / "images" / "V__aaa.png").is_file()
    subset = json.loads((root / "sales" / "reference" / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert subset["data_ok"] == 0
    assert subset["not_copied"] == 1
    assert subset["views"][0]["data"]["status"] == grp.NOT_COPIED_STATUS
    assert "path" not in subset["views"][0]["data"]
    assert "V__aaa.csv" in subset["views"][0]["data"]["not_copied_reason"]


def test_a_missing_render_artifact_is_not_counted_in_its_ok_column(tmp_path):
    """`--reference-best` normally yields SVG now, so the render legs are where this bites."""
    view = _view("Sales", "V", "aaa")
    view["svg"] = {"status": "ok", "path": "images/V__aaa.svg"}
    oracle = _capture(tmp_path, [view])  # _capture only materialises data + image
    root = _migrations(tmp_path, "sales")
    assert grp.run(oracle, root, dry_run=False) == 1
    subset = json.loads((root / "sales" / "reference" / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert subset["svg_ok"] == 0
    assert subset["not_copied"] == 1
    assert not (root / "sales" / "reference" / "images" / "V__aaa.svg").exists()


def test_a_grouping_that_lost_an_artifact_is_not_reported_as_grouped(tmp_path):
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    (oracle / "images" / "V__aaa.png").unlink()
    root = _migrations(tmp_path, "sales")
    grp.run(oracle, root, dry_run=False)
    report = json.loads((oracle / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert report["workbooks_grouped"] == 0
    assert report["workbooks_incomplete"] == 1
    assert report["incomplete"][0]["not_copied"] == 1


def test_a_complete_grouping_is_still_reported_as_grouped_and_exits_zero(tmp_path):
    """The clean path must stay clean, or the new non-zero exit is just noise."""
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    root = _migrations(tmp_path, "sales")
    assert grp.run(oracle, root, dry_run=False) == 0
    report = json.loads((oracle / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert (report["workbooks_grouped"], report["workbooks_incomplete"]) == (1, 0)
    subset = json.loads((root / "sales" / "reference" / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert (subset["data_ok"], subset["image_ok"], subset["not_copied"]) == (1, 1, 0)


def test_a_dry_run_still_detects_a_missing_artifact(tmp_path):
    """A dry run's whole job is to report what WOULD happen; a missing source is exactly that."""
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    (oracle / "data" / "V__aaa.csv").unlink()
    root = _migrations(tmp_path, "sales")
    assert grp.run(oracle, root, dry_run=True) == 1
    assert not (root / "sales" / "reference").exists()


def test_the_capture_manifest_is_never_mutated_by_grouping(tmp_path):
    """The flat capture stays authoritative -- downgrading a leg must happen on a COPY."""
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    (oracle / "data" / "V__aaa.csv").unlink()
    before = (oracle / grp.MANIFEST_NAME).read_text(encoding="utf-8")
    grp.run(oracle, _migrations(tmp_path, "sales"), dry_run=False)
    assert (oracle / grp.MANIFEST_NAME).read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- dry run / errors


def test_dry_run_writes_absolutely_nothing(tmp_path):
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    root = _migrations(tmp_path, "sales")
    assert grp.run(oracle, root, dry_run=True) == 0
    assert not (root / "sales" / "reference").exists()
    assert not (oracle / grp.UNMATCHED_REPORT).exists()


def test_a_missing_manifest_names_the_file_it_wanted(tmp_path):
    with pytest.raises(FileNotFoundError, match="oracle-manifest.json"):
        grp.run(tmp_path, tmp_path, dry_run=False)


def test_a_missing_migrations_root_is_survivable(tmp_path):
    """Reported as unmatched, not crashed: the capture is still intact and worth saying so."""
    oracle = _capture(tmp_path, [_view("Sales", "V", "aaa")])
    assert grp.run(oracle, tmp_path / "nope", dry_run=False) == 1


@pytest.mark.parametrize(
    "name,expected",
    [("DS_Tail_Level", "dstaillevel"), ("DS Tail Level", "dstaillevel"), ("ds-tail-level", "dstaillevel"), ("", "")],
)
def test_normalize(name, expected):
    assert grp.normalize(name) == expected
