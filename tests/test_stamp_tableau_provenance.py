"""Provenance is what makes a finding reproducible weeks later.

Every test here pins a way the stamp could quietly lie. The motivating case is real: three defects
were filed against Tableau's Superstore sample, and each cited exact figures (`SUM(Sales) =
15,357,898`, `41 rows`, one distinct date). Tableau's samples differ between releases and between the
Desktop copy and the Cloud *Samples* copy, so a reader with a different build gets different numbers
and no way to tell that is what happened. The stamp exists so they can tell.

The rule these tests exist to enforce: **a same-named workbook is not the same workbook.** Recording
"this came from Superstore" without confirming the bytes is the failure, not the absence of a record.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import stamp_tableau_provenance as prov  # noqa: E402  # pylint: disable=wrong-import-position


def _twbx(tmp_path: Path, name: str = "Superstore", payload: bytes = b"<workbook/>") -> Path:
    path = tmp_path / f"{name}.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}.twb", payload)
        archive.writestr("Data/Sales.csv", b"a,b\n1,2\n")
    return path


class FakeLookup:
    """A site that answers by name, and hands back whatever content it was told to."""

    def __init__(self, workbooks, remote_sha="deadbeef"):
        self._workbooks = workbooks
        self._remote_sha = remote_sha
        self.base, self.site = "https://x.online.tableau.com", "site"
        self.product_version, self.version = "2026.2.5", "3.29"
        self.signed_out = False

    def workbooks(self):
        return self._workbooks

    def content_sha256(self, workbook_id):  # noqa: ARG002
        return self._remote_sha

    def sign_out(self):
        self.signed_out = True


# --------------------------------------------------------------------------- fingerprint


def test_fingerprint_records_member_crcs_not_just_the_outer_hash(tmp_path):
    """A .twbx is a zip, and zip metadata differs between two downloads of identical content.

    Comparing outer hashes alone can therefore report a false difference. Member CRCs compare the
    content, which is what a third party needs in order to check their copy without either side
    redistributing a vendor's sample workbook.
    """
    record = prov.fingerprint(_twbx(tmp_path))
    assert record["sha256"]
    names = [m["name"] for m in record["members"]]
    assert names == sorted(names), "members must be ordered so two stamps are diffable"
    assert all(len(m["crc32"]) == 8 for m in record["members"])


def test_a_plain_twb_has_no_members(tmp_path):
    path = tmp_path / "Plain.twb"
    path.write_text("<workbook/>", encoding="utf-8")
    assert "members" not in prov.fingerprint(path)


# --------------------------------------------------------------------------- origin matching


def test_matching_bytes_are_recorded_as_a_sha256_match(tmp_path):
    path = _twbx(tmp_path)
    local = prov.fingerprint(path)["sha256"]
    lookup = FakeLookup([{"id": "luid-1", "name": "Superstore", "project": {"name": "Samples"}}], remote_sha=local)
    origin = prov.find_origin(lookup, "Superstore", local)
    assert origin["match"] == "sha256"
    assert origin["workbook_luid"] == "luid-1"
    assert origin["tableau_product_version"] == "2026.2.5"


def test_a_same_named_but_different_build_is_never_claimed_as_the_source(tmp_path):
    """THE test. A workbook of the same name that is a different build must be recorded as
    `name_only`, because that is exactly the situation in which cited figures do not reproduce and
    the reader cannot see why."""
    path = _twbx(tmp_path)
    lookup = FakeLookup([{"id": "luid-2", "name": "Superstore"}], remote_sha="a-different-build")
    origin = prov.find_origin(lookup, "Superstore", prov.fingerprint(path)["sha256"])
    assert origin["match"] == "name_only"
    assert origin["remote_sha256"] == "a-different-build"


def test_duplicate_names_on_the_site_are_counted_not_hidden():
    """Tableau permits the same workbook name in different projects. Silently taking the first is how
    a name-keyed join produces a confident wrong answer - a hazard this toolchain has hit four times."""
    lookup = FakeLookup([{"id": "a", "name": "Sales"}, {"id": "b", "name": "Sales"}])
    assert prov.find_origin(lookup, "Sales", "x")["same_name_count"] == 2


def test_a_workbook_absent_from_the_site_yields_no_origin():
    assert prov.find_origin(FakeLookup([{"id": "a", "name": "Other"}]), "Superstore", "x") is None


# ------------------------------------------------------- harvested filenames (`<luid>_<name>`)

HARVEST_LUID = "4f2c1a9e-3b7d-4c21-9a55-8e0b6d1f7c34"


def test_a_harvested_filename_matches_by_luid_not_by_its_mangled_stem():
    """THE regression. `harvest_estate_assets.py` writes `<luid>_<sanitized-name><ext>` because
    display names are not unique across projects. Comparing that whole stem against the site's
    `name` can never match, so the stamper reported `no workbook of this name on the site` for
    every harvested file - 20/20 false negatives in one measured run, each for a workbook that
    demonstrably existed. The LUID in the filename is exact identity; use it.
    """
    lookup = FakeLookup([{"id": HARVEST_LUID, "name": "Sales - Q3 Review", "project": {"name": "Finance"}}])
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Sales___Q3_Review", "x")
    assert origin is not None, "a harvested workbook present on the site must be found"
    assert origin["matched_by"] == "luid"
    assert origin["workbook_name"] == "Sales - Q3 Review"


def test_luid_match_survives_a_rename_on_the_site():
    """The LUID is stable across renames, which is the whole point of preferring it: the name in our
    filename is a snapshot from harvest time and may be stale."""
    lookup = FakeLookup([{"id": HARVEST_LUID, "name": "Renamed Since Harvest"}])
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Original_Name", "x")
    assert origin["matched_by"] == "luid"


def test_luid_matching_is_case_insensitive():
    """REST hands back LUIDs lowercased, but a filename can be round-tripped through tooling that
    upper-cases it; a case difference must not read as 'not on the site'."""
    lookup = FakeLookup([{"id": HARVEST_LUID, "name": "Sales"}])
    assert prov.find_origin(lookup, f"{HARVEST_LUID.upper()}_Sales", "x")["matched_by"] == "luid"


def test_a_deleted_and_recreated_workbook_falls_back_to_the_sanitized_name():
    """A new LUID for the same name is the shape of a delete-and-republish. Falling back keeps the
    record useful, and `matched_by` says plainly that it was NOT an identity match."""
    lookup = FakeLookup([{"id": "a-brand-new-luid", "name": "Sales / Q3: Review"}])
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Sales___Q3__Review", "x")
    assert origin["matched_by"] == "sanitized_name"


def test_the_sanitized_fallback_is_NOT_offered_to_hand_placed_files():
    """Loosening the match is only justified where we know harvest applied the transformation. A
    plain `Sales_Q3_Review.twbx` a human dropped in a folder must not fuzzy-match `Sales/Q3 Review`
    - that would be exactly the name-is-not-identity error this module exists to prevent."""
    lookup = FakeLookup([{"id": "a", "name": "Sales/Q3 Review"}])
    assert prov.find_origin(lookup, "Sales_Q3_Review", "x") is None


def test_a_plain_name_still_matches_exactly_as_before():
    """The harvested path must not regress the ordinary one."""
    lookup = FakeLookup([{"id": "a", "name": "Superstore"}])
    assert prov.find_origin(lookup, "Superstore", "x")["matched_by"] == "name"


def test_a_luid_prefixed_stem_whose_luid_is_gone_still_matches_the_exact_name():
    lookup = FakeLookup([{"id": "some-other-luid", "name": "Superstore"}])
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Superstore", "x")
    assert origin["matched_by"] == "name"


@pytest.mark.parametrize(
    "stem,expected",
    [
        (f"{HARVEST_LUID}_Sales", (HARVEST_LUID, "Sales")),
        ("Sales", (None, "Sales")),
        ("not-a-uuid_Sales", (None, "not-a-uuid_Sales")),
        (f"{HARVEST_LUID}", (None, HARVEST_LUID)),  # prefix with no name after it is not a harvest stem
    ],
)
def test_split_harvest_stem(stem, expected):
    assert prov.split_harvest_stem(stem) == expected


def test_same_name_count_still_counts_names_when_matched_by_luid():
    """`same_name_count` answers 'is this name ambiguous on the site', which stays worth knowing even
    when we resolved the file by LUID."""
    lookup = FakeLookup([{"id": HARVEST_LUID, "name": "Sales"}, {"id": "other", "name": "Sales"}])
    assert prov.find_origin(lookup, f"{HARVEST_LUID}_Sales", "x")["same_name_count"] == 2


# --------------------------------------------------------------------------- build()


def test_fingerprints_still_land_with_no_credentials(tmp_path):
    """The stamp must be useful offline: no site access is the common case for a handed-over file."""
    _twbx(tmp_path)
    result = prov.build(tmp_path, {})
    assert result["input_count"] == 1
    assert result["inputs"][0]["input"]["sha256"]
    assert result["inputs"][0].get("origin") is None


def test_a_lookup_failure_degrades_to_fingerprints_rather_than_failing(tmp_path, monkeypatch):
    """A dead site must not cost us the local half of the record."""
    _twbx(tmp_path)

    def boom(_env):
        raise RuntimeError("site unreachable")

    monkeypatch.setattr(prov, "TableauLookup", boom)
    result = prov.build(tmp_path, {"TABLEAU_SERVER_URL": "https://x", "TABLEAU_PAT_NAME": "n"})
    assert result["inputs"][0]["input"]["sha256"]


def test_every_workbook_in_a_folder_is_stamped(tmp_path):
    _twbx(tmp_path, "A")
    _twbx(tmp_path, "B")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert prov.build(tmp_path, {})["input_count"] == 2


def test_an_empty_folder_is_reported_rather_than_stamped_as_success(tmp_path):
    assert prov.build(tmp_path, {})["input_count"] == 0


# --------------------------------------------------------------------------- secrets


def test_the_pat_secret_never_reaches_the_output(tmp_path):
    """The stamp is committed alongside findings and pasted into issues. It must carry no secret."""
    _twbx(tmp_path)
    env = {
        "TABLEAU_SERVER_URL": "https://x.online.tableau.com",
        "TABLEAU_SITE": "site",
        "TABLEAU_PAT_NAME": "pat-name",
        "TABLEAU_PAT_SECRET": "SUPER-SECRET-VALUE",
    }
    text = json.dumps(prov.build(tmp_path, env))
    assert "SUPER-SECRET-VALUE" not in text


@pytest.mark.parametrize("key", ["TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME"])
def test_partial_credentials_do_not_attempt_a_lookup(tmp_path, key):
    _twbx(tmp_path)
    result = prov.build(tmp_path, {key: "present"})
    assert result["inputs"][0].get("origin") is None
