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

import hashlib
import json
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import object_identity as oid  # noqa: E402  # pylint: disable=wrong-import-position
import stamp_tableau_provenance as prov  # noqa: E402  # pylint: disable=wrong-import-position


def _twbx(tmp_path: Path, name: str = "Superstore", payload: bytes = b"<workbook/>") -> Path:
    path = tmp_path / f"{name}.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}.twb", payload)
        archive.writestr("Data/Sales.csv", b"a,b\n1,2\n")
    return path


class FakeLookup:
    """A site that answers by name, and hands back whatever content it was told to."""

    def __init__(self, workbooks, remote_sha="deadbeef", remote_key=None):
        self._workbooks = workbooks
        self._remote_sha = remote_sha
        self._remote_key = remote_key
        self.base, self.site = "https://x.online.tableau.com", "site"
        self.product_version, self.version = "2026.2.5", "3.29"
        self.signed_out = False

    def workbooks(self):
        return self._workbooks

    def content_sha256(self, workbook_id):  # noqa: ARG002
        return self._remote_sha

    def content_revision_key(self, workbook_id):  # noqa: ARG002
        return self._remote_key

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
    record = prov.fingerprint(path)
    lookup = FakeLookup(
        [{"id": "luid-1", "name": "Superstore", "project": {"name": "Samples"}}],
        remote_sha=record["sha256"],
    )
    origin = prov.find_origin(lookup, "Superstore", record)
    assert origin["match"] == "sha256"
    assert origin["workbook_luid"] == "luid-1"
    assert origin["tableau_product_version"] == "2026.2.5"


def test_a_same_named_but_different_build_is_never_claimed_as_the_source(tmp_path):
    """THE test. A workbook of the same name that is a different build must be recorded as
    `name_only`, because that is exactly the situation in which cited figures do not reproduce and
    the reader cannot see why."""
    path = _twbx(tmp_path)
    lookup = FakeLookup([{"id": "luid-2", "name": "Superstore"}], remote_sha="a-different-build")
    origin = prov.find_origin(lookup, "Superstore", prov.fingerprint(path))
    assert origin["match"] == "name_only"
    assert origin["remote_sha256"] == "a-different-build"


def test_duplicate_names_on_the_site_are_counted_not_hidden():
    """Tableau permits the same workbook name in different projects. Silently taking the first is how
    a name-keyed join produces a confident wrong answer - a hazard this toolchain has hit four times."""
    lookup = FakeLookup([{"id": "a", "name": "Sales"}, {"id": "b", "name": "Sales"}])
    assert prov.find_origin(lookup, "Sales", {"sha256": "x"})["same_name_count"] == 2


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
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Sales___Q3_Review", {"sha256": "x"})
    assert origin is not None, "a harvested workbook present on the site must be found"
    assert origin["matched_by"] == "luid"
    assert origin["workbook_name"] == "Sales - Q3 Review"


def test_luid_match_survives_a_rename_on_the_site():
    """The LUID is stable across renames, which is the whole point of preferring it: the name in our
    filename is a snapshot from harvest time and may be stale."""
    lookup = FakeLookup([{"id": HARVEST_LUID, "name": "Renamed Since Harvest"}])
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Original_Name", {"sha256": "x"})
    assert origin["matched_by"] == "luid"


def test_luid_matching_is_case_insensitive():
    """REST hands back LUIDs lowercased, but a filename can be round-tripped through tooling that
    upper-cases it; a case difference must not read as 'not on the site'."""
    lookup = FakeLookup([{"id": HARVEST_LUID, "name": "Sales"}])
    assert prov.find_origin(lookup, f"{HARVEST_LUID.upper()}_Sales", {"sha256": "x"})["matched_by"] == "luid"


def test_a_deleted_and_recreated_workbook_falls_back_to_the_sanitized_name():
    """A new LUID for the same name is the shape of a delete-and-republish. Falling back keeps the
    record useful, and `matched_by` says plainly that it was NOT an identity match."""
    lookup = FakeLookup([{"id": "a-brand-new-luid", "name": "Sales / Q3: Review"}])
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Sales___Q3__Review", {"sha256": "x"})
    assert origin["matched_by"] == "sanitized_name"


def test_the_sanitized_fallback_is_NOT_offered_to_hand_placed_files():
    """Loosening the match is only justified where we know harvest applied the transformation. A
    plain `Sales_Q3_Review.twbx` a human dropped in a folder must not fuzzy-match `Sales/Q3 Review`
    - that would be exactly the name-is-not-identity error this module exists to prevent."""
    lookup = FakeLookup([{"id": "a", "name": "Sales/Q3 Review"}])
    assert prov.find_origin(lookup, "Sales_Q3_Review", {"sha256": "x"}) is None


def test_a_plain_name_still_matches_exactly_as_before():
    """The harvested path must not regress the ordinary one."""
    lookup = FakeLookup([{"id": "a", "name": "Superstore"}])
    assert prov.find_origin(lookup, "Superstore", {"sha256": "x"})["matched_by"] == "name"


def test_a_luid_prefixed_stem_whose_luid_is_gone_still_matches_the_exact_name():
    lookup = FakeLookup([{"id": "some-other-luid", "name": "Superstore"}])
    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Superstore", {"sha256": "x"})
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
    assert prov.find_origin(lookup, f"{HARVEST_LUID}_Sales", {"sha256": "x"})["same_name_count"] == 2


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


def test_live_origin_fields_are_scrubbed_before_the_manifest_sink(tmp_path, monkeypatch):
    """Positive and mutation controls for the response-derived provenance manifest path."""
    secret = "SYNTHETIC_PROVENANCE_PAT_42"
    _twbx(tmp_path, secret)

    class ReflectingLookup(FakeLookup):
        def __init__(self, _env):
            super().__init__(
                [{"id": "luid-1", "name": secret, "project": {"name": f"Project {secret}"}}],
                remote_sha="different",
            )
            self.token = "token"

        def sign_in(self):
            pass

        def redact_text(self, text):
            return text.replace(secret, "[REDACTED]").replace(self.token, "[REDACTED]")

    env = {
        "TABLEAU_SERVER_URL": "https://x.online.tableau.com",
        "TABLEAU_SITE": "site",
        "TABLEAU_PAT_NAME": secret,
        "TABLEAU_PAT_SECRET": "an-unrelated-long-pat-secret",
    }
    live_redactor = prov.TableauLookup(env)
    assert secret not in live_redactor.redact_text(f"echo {secret}")

    monkeypatch.setattr(prov, "TableauLookup", ReflectingLookup)
    assert secret not in json.dumps(prov.build(tmp_path, env))

    monkeypatch.setattr(prov, "scrub_tree", lambda value, _redactor: (value, []))
    assert secret in json.dumps(prov.build(tmp_path, env)), "the production scrub mutation did not reopen the sink"


def test_live_lookup_errors_are_scrubbed_before_the_manifest_sink(tmp_path, monkeypatch):
    """A response error after sign-in is redacted before becoming a provenance record."""
    secret = "SYNTHETIC_LOOKUP_ERROR_PAT_42"
    _twbx(tmp_path)

    class ReflectingLookup(FakeLookup):
        def __init__(self, _env):
            super().__init__([])

        def sign_in(self):
            self.token = "token"

        def redact_text(self, text):
            return text.replace(secret, "[REDACTED]")

    def fail_origin(*_args):
        raise RuntimeError(f"Tableau reflected {secret}")

    monkeypatch.setattr(prov, "TableauLookup", ReflectingLookup)
    monkeypatch.setattr(prov, "find_origin", fail_origin)
    env = {
        "TABLEAU_SERVER_URL": "https://x.online.tableau.com",
        "TABLEAU_PAT_NAME": "pat",
        "TABLEAU_PAT_SECRET": secret,
    }
    result = prov.build(tmp_path, env)
    assert secret not in json.dumps(result)
    assert "[REDACTED]" in result["inputs"][0]["lookup_error"]


@pytest.mark.parametrize("key", ["TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME"])
def test_partial_credentials_do_not_attempt_a_lookup(tmp_path, key):
    _twbx(tmp_path)
    result = prov.build(tmp_path, {key: "present"})
    assert result["inputs"][0].get("origin") is None


# --------------------------------------------------------------------------- revision key (round 3)


def _repack(path: Path) -> bytes:
    """The same archive with reversed member ORDER and different mtimes - what the server does."""
    import io
    import zipfile

    with zipfile.ZipFile(path) as source:
        members = [(info.filename, source.read(info.filename)) for info in reversed(source.infolist())]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in members:
            target.writestr(zipfile.ZipInfo(name, date_time=(2026, 9, 3, 11, 22, 33)), data)
    return buffer.getvalue()


def test_fingerprint_records_a_reproducible_revision_key(tmp_path):
    """The outer hash was already documented here as unstable; nothing acted on it.

    Measured 2026-09-03 against the live site, three downloads of every item in one run: the raw
    digest differed for 27 of 49 archives while the content-normalised key differed for 0 of 67.
    """
    record = prov.fingerprint(_twbx(tmp_path))

    assert record["revision_key"]["algo"] == oid.REVISION_ALGO_ARCHIVE
    assert record["revision_key"]["value"] != record["sha256"], "the key is not the raw hash"


def test_a_repacked_site_copy_is_recorded_as_the_same_revision(tmp_path):
    """THE round-3 test on the producer side: a repack must read `revision_match: "same"`.

    `match` still says `name_only`, because the raw bytes genuinely differ - and that is exactly why
    it could never carry the revision claim on its own.
    """
    path = _twbx(tmp_path)
    record = prov.fingerprint(path)
    lookup = FakeLookup(
        [{"id": "luid-1", "name": "Superstore"}],
        remote_sha="a-repacked-blob",
        remote_key=oid.revision_key(_repack(path)),
    )

    origin = prov.find_origin(lookup, "Superstore", record)

    assert origin["match"] == "name_only", "the raw bytes DO differ - that is the whole point"
    assert origin["revision_match"] == "same"
    assert origin["remote_revision_key"]["algo"] == oid.REVISION_ALGO_ARCHIVE


def test_genuinely_different_site_content_is_recorded_as_differs(tmp_path):
    """The negative control: normalisation must not launder a real change into agreement."""
    lookup = FakeLookup(
        [{"id": "luid-1", "name": "Superstore"}],
        remote_sha="x",
        remote_key=oid.revision_key(_twbx(tmp_path, name="Other", payload=b"<workbook edited='1'/>").read_bytes()),
    )

    origin = prov.find_origin(lookup, "Superstore", prov.fingerprint(_twbx(tmp_path)))

    assert origin["revision_match"] == "differs"


def test_an_uncomparable_remote_key_is_recorded_as_neither(tmp_path):
    """A site copy this build cannot key says nothing - `None`, never `"differs"`.

    A false drift alarm on every capture taken before the key existed would be a nastier regression
    than the gap it closes.
    """
    lookup = FakeLookup([{"id": "luid-1", "name": "Superstore"}], remote_sha="x", remote_key=None)

    origin = prov.find_origin(lookup, "Superstore", prov.fingerprint(_twbx(tmp_path)))

    assert origin["revision_match"] is None
    assert origin["remote_revision_key"] is None


# ------------------------------------------------- remote call budget (issue #576, round 4)
#
# Measured 2026-09-09 on the pre-cache code against a recording loopback site: 66 harvested inputs
# cost **200** remote calls - `2 + N + 2M`, one site-wide inventory listing per input plus TWO full
# downloads of every matched workbook, with byte-identical repeats for 66 of 66 LUIDs. At an ordinary
# large-`.twbx` latency of ~9 s that is the >20 min stall reported in #576, with no stall required.
#
# Every count below is taken at the TRANSPORT (`_call`), never at a mocked method: the 31 tests above
# mock `workbooks`/`content_sha256`/`content_revision_key` by name and could not see the defect,
# which is exactly how the multiplier shipped.

LIVE_ENV = {
    "TABLEAU_SERVER_URL": "https://x.online.tableau.com",
    "TABLEAU_SITE": "site",
    "TABLEAU_PAT_NAME": "fixture-pat-name",
    "TABLEAU_PAT_SECRET": "fixture-pat-secret-long-enough",
}


def _fixture_luid(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-{index:012x}"


class RecordingSite(prov.TableauLookup):
    """The production client with exactly ONE substitution: its transport.

    ``_call`` is the only network boundary in the module, so recording there counts what a customer's
    Tableau site would actually be asked, and cannot be satisfied by production code that merely
    stops calling a mocked method name.

    Content is answered with DIFFERENT bytes on every call for the same LUID, so a second download
    is detectable in the recorded digests and not only in the call count - which is also the real
    behaviour: a `.twbx` is repacked per download.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        env,
        *,
        workbooks=(),
        inventory_status=200,
        inventory_error=None,
        content_errors=(),
        signout_error=None,
    ):
        super().__init__(env)
        self.calls: list[tuple[str, str]] = []
        self.served: dict[str, list[bytes]] = {}
        self._site_workbooks = list(workbooks)
        self._inventory_status = inventory_status
        self._inventory_error = inventory_error
        self._content_errors = set(content_errors)
        self._signout_error = signout_error

    def _call(self, method, path, body=None, accept=None):  # noqa: ARG002
        self.calls.append((method, path))
        if path == "/auth/signin":
            token = {"credentials": {"token": "session-token", "site": {"id": "site-id"}}}
            return 200, json.dumps(token).encode()
        if path == "/auth/signout":
            if self._signout_error is not None:
                raise self._signout_error
            return 204, b""
        if "/content" in path:
            luid = path.split("/workbooks/")[1].split("/")[0]
            if luid in self._content_errors:
                raise urllib.error.URLError("content transport is dead")
            served = self.served.setdefault(luid, [])
            payload = f"<workbook luid='{luid}' download='{len(served)}'/>".encode()
            served.append(payload)
            return 200, payload
        if self._inventory_error is not None:
            raise self._inventory_error
        if self._inventory_status != 200:
            return self._inventory_status, b"{}"
        return 200, json.dumps({"workbooks": {"workbook": self._site_workbooks}}).encode()

    def count(self, kind: str) -> int:
        """How many times the site was asked for ``signin`` / ``signout`` / ``inventory`` / ``content``."""
        if kind in ("signin", "signout"):
            return sum(1 for _method, path in self.calls if path == f"/auth/{kind}")
        if kind == "content":
            return sum(1 for _method, path in self.calls if "/content" in path)
        return sum(1 for _method, path in self.calls if "/workbooks?" in path)


def _install(monkeypatch, site: RecordingSite) -> RecordingSite:
    monkeypatch.setattr(prov, "TableauLookup", lambda _env: site)
    return site


def _harvested(tmp_path: Path, count: int) -> list[dict]:
    """``count`` harvest-shaped inputs (``<luid>_<sanitized-name>.twbx``) and the matching inventory."""
    inventory = []
    for index in range(count):
        luid = _fixture_luid(index)
        _twbx(tmp_path, f"{luid}_Fixture_Workbook_{index:03d}", payload=f"<workbook n='{index}'/>".encode())
        inventory.append({"id": luid, "name": f"Fixture Workbook {index:03d}", "project": {"name": "Fixture Project"}})
    return inventory


def test_a_many_input_run_costs_one_inventory_and_one_download_per_matched_luid(tmp_path, monkeypatch):
    """THE #576 test. 66 matched inputs cost 200 calls before this; the formula is now 2 + 1 + M.

    Mutation it must fail on: restoring the per-input ``lookup.workbooks()`` (inventory becomes 66),
    or re-fetching inside ``content_revision_key`` (content becomes 132).
    """
    inventory = _harvested(tmp_path, 66)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory))

    result = prov.build(tmp_path, LIVE_ENV)

    assert site.count("inventory") == 1, "the site inventory does not change between inputs"
    assert site.count("content") == 66, "one download per distinct matched LUID, not two"
    assert len(site.calls) == 2 + 1 + 66, "sign-in + one inventory + one content each + sign-out"
    assert result["input_count"] == 66
    assert sum(1 for record in result["inputs"] if record.get("origin")) == 66
    assert site.count("signout") == 1, "the session is still released"


def test_unmatched_inputs_still_fetch_exactly_one_inventory_and_no_content(tmp_path, monkeypatch):
    """Negative control: a cache keyed per input, or per-input listing, both break this."""
    _harvested(tmp_path, 8)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=[]))

    result = prov.build(tmp_path, LIVE_ENV)

    assert (site.count("inventory"), site.count("content")) == (1, 0)
    assert len(site.calls) == 3
    assert all(record["origin"] is None for record in result["inputs"])
    assert all(record["input"]["sha256"] for record in result["inputs"])


def test_one_downloaded_payload_feeds_both_the_raw_sha_and_the_revision_key(tmp_path, monkeypatch):
    """The two digests must describe the SAME bytes, not two downloads that merely look alike.

    The fixture answers different bytes on every content call, so a second download would show up in
    the recorded digests as well as in the call count.
    """
    inventory = _harvested(tmp_path, 1)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory))

    origin = prov.build(tmp_path, LIVE_ENV)["inputs"][0]["origin"]

    luid = inventory[0]["id"]
    assert site.count("content") == 1
    served = site.served[luid][0]
    assert origin["remote_sha256"] == hashlib.sha256(served).hexdigest()
    assert origin["remote_revision_key"] == oid.revision_key(served).as_json()


def test_duplicate_inputs_of_one_luid_reuse_the_cached_content(tmp_path, monkeypatch):
    """Two local copies of one harvested workbook are one site item, so they are one download."""
    luid = _fixture_luid(7)
    _twbx(tmp_path, f"{luid}_Copy_A")
    _twbx(tmp_path, f"{luid}_Copy_B")
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=[{"id": luid, "name": "Copy A"}]))

    result = prov.build(tmp_path, LIVE_ENV)

    assert site.count("content") == 1
    assert [record["origin"]["workbook_luid"] for record in result["inputs"]] == [luid, luid]


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"inventory_status": 500}, "RuntimeError: listing workbooks failed: HTTP 500"),
        ({"inventory_error": urllib.error.URLError("inventory transport is dead")}, "URLError:"),
    ],
)
def test_a_dead_inventory_is_asked_once_and_every_input_keeps_its_fingerprint(tmp_path, monkeypatch, kwargs, expected):
    """Measured: 66 inputs produced 66 identical failing listings. One answer is enough.

    Each input still records its own redacted reason - latching the CALL must not latch the record.
    """
    _harvested(tmp_path, 12)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, **kwargs))

    result = prov.build(tmp_path, LIVE_ENV)

    assert site.count("inventory") == 1
    assert site.count("content") == 0
    errors = {record["lookup_error"] for record in result["inputs"]}
    assert len(errors) == 1 and errors.pop().startswith(expected)
    assert len(result["inputs"]) == 12
    assert all(record["input"]["sha256"] and record["origin"] is None for record in result["inputs"])


def test_a_dead_content_call_latches_that_luid_only(tmp_path, monkeypatch):
    """A workbook that cannot be downloaded must not condemn a different workbook.

    Two inputs share the dead LUID (one attempt between them) and a third resolves to a healthy one,
    which is still fetched and still matched.
    """
    dead, alive = _fixture_luid(1), _fixture_luid(2)
    _twbx(tmp_path, f"{dead}_Dead_A")
    _twbx(tmp_path, f"{dead}_Dead_B")
    _twbx(tmp_path, f"{alive}_Alive")
    inventory = [{"id": dead, "name": "Dead"}, {"id": alive, "name": "Alive"}]
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory, content_errors=[dead]))

    records = prov.build(tmp_path, LIVE_ENV)["inputs"]

    assert site.count("content") == 2, "one attempt for the dead LUID, one download for the healthy one"
    dead_records = [r for r in records if r["input"]["file"].startswith(dead)]
    assert len(dead_records) == 2
    assert {r["lookup_error"] for r in dead_records} == {dead_records[0]["lookup_error"]}
    assert dead_records[0]["lookup_error"].startswith("URLError:")
    alive_record = next(r for r in records if r["input"]["file"].startswith(alive))
    assert alive_record["origin"]["workbook_luid"] == alive


def test_a_signout_failure_cannot_discard_the_completed_result(tmp_path, monkeypatch):
    """Measured on the pre-fix code: a closed sign-out connection lost 3 of 3 fingerprints.

    Every input was already fingerprinted and matched in memory when the session release failed.
    """
    inventory = _harvested(tmp_path, 3)
    site = _install(
        monkeypatch,
        RecordingSite(LIVE_ENV, workbooks=inventory, signout_error=ConnectionError("closed without a response")),
    )

    result = prov.build(tmp_path, LIVE_ENV)

    assert result["input_count"] == 3
    assert all(record["input"]["sha256"] for record in result["inputs"])
    assert sum(1 for record in result["inputs"] if record.get("origin")) == 3
    assert site.count("signout") == 1, "it was attempted - it simply may not cost the artifact"


def test_sign_out_swallows_a_transport_failure_and_still_drops_the_token():
    """The client's own half of the guarantee: releasing a session may never raise at its caller.

    A Tableau session expires by itself, so a failed release costs nothing - while a raise costs the
    whole stamp, which is exactly what was measured before this slice.
    """
    site = RecordingSite(LIVE_ENV, signout_error=ConnectionError("closed without a response"))
    site.token = "session-token"

    site.sign_out()

    assert site.token is None
    assert site.count("signout") == 1


def test_the_completed_result_survives_a_signout_that_raises_outright(tmp_path, monkeypatch):
    """The second, independent guard: ``sign_out`` swallows transport errors, and ``build`` refuses to
    lose a finished stamp to *any* failure of the release step - including one the client itself
    cannot anticipate. Each guard is proven separately, because either alone leaves a way to lose the
    artifact measured in #576.
    """
    inventory = _harvested(tmp_path, 2)

    class HostileSignOut(RecordingSite):
        def sign_out(self):
            raise RuntimeError("release is unavailable")

    _install(monkeypatch, HostileSignOut(LIVE_ENV, workbooks=inventory))

    result = prov.build(tmp_path, LIVE_ENV)

    assert result["input_count"] == 2
    assert sum(1 for record in result["inputs"] if record.get("origin")) == 2


def test_the_cli_still_writes_the_artifact_when_signout_fails(tmp_path, monkeypatch):
    """The same failure used to exit 1 with no file at all; the CLI is the operator-visible half."""
    inventory = _harvested(tmp_path, 3)
    _install(
        monkeypatch,
        RecordingSite(LIVE_ENV, workbooks=inventory, signout_error=ConnectionError("closed without a response")),
    )
    out = tmp_path / "source-provenance.json"
    monkeypatch.setattr(prov, "resolve_env", lambda _path: dict(LIVE_ENV))
    monkeypatch.setattr(sys, "argv", ["stamp_tableau_provenance.py", "--input", str(tmp_path), "--out", str(out)])

    assert prov.main() == 0
    assert json.loads(out.read_text(encoding="utf-8"))["input_count"] == 3


def test_a_scrub_failure_withholds_live_fields_but_keeps_the_fingerprints(tmp_path, monkeypatch):
    """Redaction failing is the one case that may NOT keep the live half - fail closed on the secret.

    The site reflects the PAT name into ``project.name``, so an unscrubbed record would carry it.
    """
    secret = "SYNTHETIC_SCRUB_FAILURE_PAT_42"
    env = dict(LIVE_ENV, TABLEAU_PAT_NAME=secret)
    inventory = _harvested(tmp_path, 2)
    for workbook in inventory:
        workbook["project"] = {"name": f"Project {secret}"}
    site = _install(monkeypatch, RecordingSite(env, workbooks=inventory))

    def explode(_value, _redactor):
        raise RuntimeError("redaction is unavailable")

    monkeypatch.setattr(prov, "scrub_tree", explode)
    result = prov.build(tmp_path, env)

    assert secret not in json.dumps(result)
    assert result["input_count"] == 2
    assert all(record["input"]["sha256"] for record in result["inputs"])
    assert all(record["origin"] is None for record in result["inputs"])
    assert "redaction failed" in result["inputs"][0]["origin_note"]
    assert site.count("signout") == 1, "the session is released even when the scrub blew up"


def test_reflected_credentials_never_reach_the_output_over_the_cached_path(tmp_path, monkeypatch):
    """The redaction control has to hold on the NEW call path, not only the old one."""
    secret = "SYNTHETIC_CACHED_PATH_PAT_42"
    env = dict(LIVE_ENV, TABLEAU_PAT_NAME=secret)
    inventory = _harvested(tmp_path, 5)
    for workbook in inventory:
        workbook["project"] = {"name": f"Project {secret}"}
    _install(monkeypatch, RecordingSite(env, workbooks=inventory))

    text = json.dumps(prov.build(tmp_path, env))

    assert secret not in text
    assert "[REDACTED]" in text, "the detector had something to detect"
