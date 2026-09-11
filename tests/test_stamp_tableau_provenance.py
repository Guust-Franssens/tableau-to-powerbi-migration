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
import multiprocessing
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

    inventory_completeness = None

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

    def content_unavailable(self, workbook_id):  # noqa: ARG002
        """This fake always answers with the content it was told to; nothing is ever unread."""
        return None

    def content_attempts(self):
        """One download per matched workbook, which is all this fake ever pretends to serve."""
        return 1

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
    assert result["phase"] == {"status": "local_only", "errors": []}
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
    assert result["phase"]["status"] == "partial"
    assert result["phase"]["errors"] == [
        {"code": "live-lookup-refused", "operation": "sign-in", "exception_class": "RuntimeError"}
    ]


def test_every_workbook_in_a_folder_is_stamped(tmp_path):
    _twbx(tmp_path, "A")
    _twbx(tmp_path, "B")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert prov.build(tmp_path, {})["input_count"] == 2


def test_an_empty_folder_is_reported_rather_than_stamped_as_success(tmp_path):
    result = prov.build(tmp_path, {})
    assert result["input_count"] == 0
    assert result["phase"] == {
        "status": "empty",
        "errors": [{"code": "empty-input", "operation": "collect-inputs"}],
    }


def test_failed_input_discovery_is_a_safe_structured_result(tmp_path, monkeypatch):
    secret = str(tmp_path / "customer-secret")

    def fail(_target):
        raise OSError(13, secret)

    monkeypatch.setattr(prov, "collect_inputs", fail)
    result = prov.build(tmp_path, {})

    assert result["input_count"] == 0 and result["inputs"] == []
    assert result["phase"] == {
        "status": "failed",
        "errors": [
            {
                "code": "collect-inputs-failed",
                "operation": "collect-inputs",
                "exception_class": "PermissionError",
                "errno": 13,
            }
        ],
    }
    assert secret not in json.dumps(result)


def test_one_failed_local_fingerprint_keeps_the_completed_sibling(tmp_path, monkeypatch):
    _twbx(tmp_path, "Broken")
    _twbx(tmp_path, "Healthy")
    real_fingerprint = prov.fingerprint

    def fingerprint(path):
        if path.stem == "Broken":
            raise OSError(5, str(path))
        return real_fingerprint(path)

    monkeypatch.setattr(prov, "fingerprint", fingerprint)
    result = prov.build(tmp_path, {})

    assert result["phase"]["status"] == "partial"
    assert result["input_count"] == 2
    assert result["inputs"][0]["fingerprint_error"] == {
        "code": "local-fingerprint-failed",
        "operation": "fingerprint",
        "exception_class": "OSError",
        "errno": 5,
    }
    assert result["inputs"][1]["input"]["sha256"]
    assert str(tmp_path) not in json.dumps(result)


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
    assert result["inputs"][0]["lookup_error"] == {
        "code": "live-lookup-failed",
        "operation": "lookup-origin",
        "exception_class": "RuntimeError",
    }


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
        inventory_document=...,
        content_errors=(),
        content_status=None,
        signout_error=None,
    ):
        super().__init__(env)
        self.calls: list[tuple[str, str]] = []
        self.served: dict[str, list[bytes]] = {}
        self._site_workbooks = list(workbooks)
        self._inventory_status = inventory_status
        self._inventory_error = inventory_error
        self._inventory_document = inventory_document
        self._content_errors = set(content_errors)
        self._content_status = dict(content_status or {})
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
            if luid in self._content_status:
                # A real refusal answers with a BODY, and an authenticated site can reflect a
                # credential into it - so the fixture puts one there.
                return self._content_status[luid], b"<error>not for you</error>"
            served = self.served.setdefault(luid, [])
            payload = f"<workbook luid='{luid}' download='{len(served)}'/>".encode()
            served.append(payload)
            return 200, payload
        if self._inventory_error is not None:
            raise self._inventory_error
        if self._inventory_status != 200:
            return self._inventory_status, b"{}"
        if self._inventory_document is not ...:
            document = self._inventory_document
            return 200, document if isinstance(document, bytes) else json.dumps(document).encode()
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
    assert result["phase"]["status"] == "success"
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
    errors = {json.dumps(record["lookup_error"], sort_keys=True) for record in result["inputs"]}
    assert len(errors) == 1
    error = json.loads(errors.pop())
    assert error["code"] == "live-lookup-failed"
    assert error["operation"] == "lookup-origin"
    assert error["exception_class"] in expected
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
    assert all(record["lookup_error"] == dead_records[0]["lookup_error"] for record in dead_records)
    assert dead_records[0]["lookup_error"] == {
        "code": "live-lookup-failed",
        "operation": "lookup-origin",
        "exception_class": "URLError",
    }
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
    assert result["phase"]["status"] == "partial"
    assert result["phase"]["errors"][-1] == {
        "code": "sign-out-failed",
        "operation": "sign-out",
        "exception_class": "ConnectionError",
    }
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
    assert result["phase"]["errors"][-1] == {
        "code": "sign-out-failed",
        "operation": "sign-out",
        "exception_class": "RuntimeError",
    }


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
    assert result["phase"]["status"] == "partial"
    assert result["phase"]["errors"][0] == {
        "code": "scrub-failed",
        "operation": "scrub",
        "exception_class": "RuntimeError",
    }
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


# ------------------------------------- correction round on PR #582 (blind review of the cache slice)
#
# Three findings, each with its own transport-level reproduction:
#   1. the withheld-live fallback persisted LOCAL strings verbatim - a filename IS a credential sink;
#   2. a non-200 content answer cached as "no bytes" and was reported as "the bytes DIFFER";
#   3. a malformed non-string inventory name was keyed by `repr` and could match a local stem.


def _twbx_with_members(path: Path, member_names: list[str]) -> Path:
    """A `.twbx` whose MEMBER names are chosen by the caller - members are a string sink too."""
    with zipfile.ZipFile(path, "w") as archive:
        for name in member_names:
            archive.writestr(name, b"<workbook/>")
    return path


def test_a_scrub_failure_still_redacts_the_local_strings_it_keeps(tmp_path, monkeypatch):
    """Finding 1a. The local half is not automatically clean - it is merely UNSCRUBBED.

    A workbook whose FILENAME is the PAT secret is not exotic: harvest names files from site data,
    and the file above already builds a fixture named after a credential. Withholding the live half
    while persisting `input.file` verbatim writes the secret into an artifact that is committed
    beside findings and pasted into issues.

    Here the first scrub (over the whole tree, live half included) fails and the retry over the
    reduced local-only tree succeeds - so the filename is REDACTED rather than dropped.
    """
    secret = "SYNTHETIC_FILENAME_SECRET_42"
    env = dict(LIVE_ENV, TABLEAU_PAT_SECRET=secret)
    _twbx(tmp_path, secret)
    site = _install(monkeypatch, RecordingSite(env, workbooks=[{"id": "luid-1", "name": secret}]))
    real_scrub, attempts = prov.scrub_tree, []

    def fails_once_on_the_live_tree(value, redactor):
        attempts.append(value)
        if len(attempts) == 1:
            raise RuntimeError("the live half could not be scrubbed")
        return real_scrub(value, redactor)

    monkeypatch.setattr(prov, "scrub_tree", fails_once_on_the_live_tree)
    result = prov.build(tmp_path, env)

    record = result["inputs"][0]
    assert secret not in json.dumps(result)
    assert "[REDACTED]" in record["input"]["file"], "redacted, not dropped - the retry worked"
    assert record["input"]["sha256"] and record["origin"] is None
    assert site.count("signout") == 1


def test_an_unusable_redactor_keeps_only_derived_evidence(tmp_path, monkeypatch):
    """Finding 1b. When redaction itself cannot be trusted, every COPIED string goes.

    PAT secret == the filename, PAT name == a member name, session token == another member name.
    All three are strings copied out of the environment, and none may survive; a digest, a byte count
    and a CRC are values this module computed and cannot carry a credential.
    """
    secret, pat_name, token = "SECRET_AS_FILENAME_42", "PAT_NAME_AS_MEMBER_42", "session-token"
    env = dict(LIVE_ENV, TABLEAU_PAT_NAME=pat_name, TABLEAU_PAT_SECRET=secret)
    _twbx_with_members(tmp_path / f"{secret}.twbx", [f"{pat_name}.twb", f"Data/{token}.csv"])

    class UnusableRedactor(RecordingSite):
        def redact_text(self, text):
            raise RuntimeError("the redactor is broken")

    site = _install(monkeypatch, UnusableRedactor(env, workbooks=[]))
    result = prov.build(tmp_path, env)

    text = json.dumps(result)
    for credential in (secret, pat_name, token):
        assert credential not in text, f"a copied string survived an untrusted redaction: {credential}"
    record = result["inputs"][0]["input"]
    assert "file" not in record, "the filename is a copied string"
    assert record["sha256"] and record["size_bytes"], "derived evidence is kept"
    assert record["members"] and all(set(member) == {"size_bytes", "crc32"} for member in record["members"])
    assert site.count("signout") == 1


def test_an_unreadable_site_copy_is_unavailable_not_a_byte_difference(tmp_path, monkeypatch):
    """Finding 2. A 404 read as `match: "name_only"` plus "the bytes DIFFER from the site copy".

    That is a drift verdict about bytes nobody ever saw, on an item the site simply refused to hand
    over - and `reference_evidence`/`package_unit` carry `name_only` forward as a build difference.
    """
    luid = _fixture_luid(3)
    _twbx(tmp_path, f"{luid}_Refused_A")
    _twbx(tmp_path, f"{luid}_Refused_B")
    site = _install(
        monkeypatch,
        RecordingSite(LIVE_ENV, workbooks=[{"id": luid, "name": "Refused"}], content_status={luid: 404}),
    )

    records = prov.build(tmp_path, LIVE_ENV)["inputs"]

    assert site.count("content") == 1, "one refusal is one call, for both inputs"
    for record in records:
        origin = record["origin"]
        assert origin["match"] == "unavailable"
        assert origin["content_unavailable"] == "HTTP 404"
        assert origin["remote_sha256"] is None and origin["remote_revision_key"] is None
        assert origin["revision_match"] is None, "nothing was compared, so nothing differs"
        assert "DIFFER" not in record["origin_note"]
        assert record["lookup_error"] == {
            "code": "content-unavailable",
            "operation": "download-workbook",
            "http_status": 404,
        }
        assert origin["workbook_luid"] == luid, "the inventory evidence we DID get is still recorded"


def test_a_refusal_reason_carries_no_response_text(tmp_path, monkeypatch):
    """The reason is the status NUMBER. An authenticated site can reflect a credential into a body,
    and the fixture's refusal body is exactly the kind of text that must never become the reason."""
    luid = _fixture_luid(6)
    _twbx(tmp_path, f"{luid}_Refused")
    _install(
        monkeypatch,
        RecordingSite(LIVE_ENV, workbooks=[{"id": luid, "name": "Refused"}], content_status={luid: 403}),
    )

    origin = prov.build(tmp_path, LIVE_ENV)["inputs"][0]["origin"]

    assert origin["content_unavailable"] == "HTTP 403"
    assert "not for you" not in json.dumps(origin), "no response body reaches the record"


def test_a_refused_download_does_not_condemn_a_healthy_sibling(tmp_path, monkeypatch):
    """Positive control beside finding 2: a real byte difference is still reported as one."""
    refused, healthy = _fixture_luid(4), _fixture_luid(5)
    _twbx(tmp_path, f"{refused}_Refused")
    _twbx(tmp_path, f"{healthy}_Healthy")
    inventory = [{"id": refused, "name": "Refused"}, {"id": healthy, "name": "Healthy"}]
    site = _install(
        monkeypatch,
        RecordingSite(LIVE_ENV, workbooks=inventory, content_status={refused: 404}),
    )

    records = {record["origin"]["workbook_luid"]: record for record in prov.build(tmp_path, LIVE_ENV)["inputs"]}

    assert site.count("content") == 2, "one refusal, one real download"
    assert records[refused]["origin"]["match"] == "unavailable"
    assert records[healthy]["origin"]["match"] == "name_only", "the site copy WAS read and it differs"
    assert records[healthy]["origin"]["content_unavailable"] is None
    assert "DIFFER" in records[healthy]["origin_note"]
    assert "lookup_error" not in records[healthy]


def test_a_malformed_inventory_name_cannot_match_a_local_file(tmp_path, monkeypatch):
    """Finding 3. A response whose `name` is `["Superstore"]` was keyed by `repr` and matched a local
    file called literally `['Superstore']` - identity invented out of a malformed field."""
    _twbx(tmp_path, "['Superstore']")
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=[{"id": "luid-1", "name": ["Superstore"]}]))

    record = prov.build(tmp_path, LIVE_ENV)["inputs"][0]

    assert record["origin"] is None, "a list is not a name"
    assert site.count("content") == 0, "no identity, so nothing to download"
    assert "local-only" in record["origin_note"]


@pytest.mark.parametrize("malformed", [{"value": "Superstore"}, ["Superstore"], 42, None])
def test_a_malformed_name_never_matches_its_own_repr(malformed):
    """The same rule stated directly, over the shapes a malformed REST answer can actually take."""
    lookup = FakeLookup([{"id": "a", "name": malformed}])
    assert prov.find_origin(lookup, repr(malformed), {"sha256": "x"}) is None


def test_a_luid_match_survives_a_malformed_name_and_counts_it_as_no_name():
    """Identity still comes from the LUID, and `same_name_count` answers safely rather than crashing."""
    lookup = FakeLookup([{"id": HARVEST_LUID, "name": ["Sales"]}])

    origin = prov.find_origin(lookup, f"{HARVEST_LUID}_Sales", {"sha256": "x"})

    assert origin["matched_by"] == "luid"
    assert origin["same_name_count"] == 0, "a non-string is not a display name, so it is not ambiguous"
    assert origin["workbook_name"] == ["Sales"], "the raw field is still recorded as the site gave it"


# ------------------------------------ blind-review correction on PR #594 (self-contradictory results)
#
# `phase.status` is a CLAIM. A result carrying `input_count: 0`, `inputs: []` and a `success` or
# `local_only` status describes NO INPUT AT ALL while reading as a pass, so a consumer that trusts
# the status alone lets a run that stamped nothing proceed to adjudication and handover. These tests
# pin the one place that decides self-consistency, and the two answers derived from it: what may be
# PUBLISHED (`normalize_result`) and what counts as a PASS (`is_success`).


def _shaped(count, inputs, status="success", errors=None):
    return {
        "schema": prov.SCHEMA,
        "stamped_at": "2026-09-10T00:00:00Z",
        "input_count": count,
        "inputs": inputs,
        "phase": {"status": status, "errors": errors if errors is not None else []},
    }


@pytest.mark.parametrize(
    ("result", "fault"),
    [
        (_shaped(0, [], "success"), "success-without-inputs"),
        (_shaped(0, [], "local_only"), "success-without-inputs"),
        (_shaped(-1, [], "success"), "input-count-negative"),
        (_shaped(True, [{"input": {}}], "success"), "input-count-not-an-integer"),
        (_shaped("1", [{"input": {}}], "success"), "input-count-not-an-integer"),
        (_shaped(None, [{"input": {}}], "local_only"), "input-count-not-an-integer"),
        (_shaped(2, [{"input": {}}], "success"), "input-count-mismatch"),
        (_shaped(1, [{"input": {}}, {"input": {}}], "local_only"), "input-count-mismatch"),
        (_shaped(1, {"unit": {}}, "success"), "inputs-not-a-list"),
        (_shaped(1, [{"input": {}}], 7), "phase-status-unassessable"),
        ("not a result at all", "result-not-a-mapping"),
    ],
)
def test_a_self_contradictory_result_is_faulted_and_never_passes(result, fault):
    assert fault in prov.consistency_faults(result)
    assert prov.is_success(result) is False


@pytest.mark.parametrize(
    "result",
    [
        _shaped(1, [{"input": {}}], "success"),
        _shaped(2, [{"input": {}}, {"input": {}}], "local_only"),
        _shaped(0, [], "empty", [{"code": "empty-input", "operation": "collect-inputs"}]),
        _shaped(0, [], "failed", [{"code": "build-failed", "operation": "build"}]),
        _shaped(1, [{"input": {}}], "partial", [{"code": "live-lookup-refused", "operation": "sign-in"}]),
    ],
)
def test_a_self_consistent_result_is_never_faulted_or_rewritten(result):
    """Including the honest non-passing ones: `empty` and `failed` are consistent, just not passes."""
    assert prov.consistency_faults(result) == []
    assert prov.normalize_result(result) is result
    assert prov.is_success(result) is (result["phase"]["status"] in prov.SUCCESS_STATUSES)


def test_normalization_rewrites_the_status_and_keeps_the_errors_already_recorded():
    prior = {"code": "live-lookup-refused", "operation": "sign-in"}
    normalized = prov.normalize_result(_shaped(3, [], "success", [prior]))

    assert normalized["phase"]["status"] == prov.UNASSESSABLE_STATUS == "failed"
    assert normalized["phase"]["status"] not in prov.SUCCESS_STATUSES
    assert normalized["phase"]["errors"][0] == prior, "the build's own evidence was discarded"
    fault = normalized["phase"]["errors"][1]
    assert fault == {
        "code": "input-count-mismatch",
        "operation": prov.CONSISTENCY_OPERATION,
        "claimed_input_count": 3,
    }
    assert normalized["input_count"] == len(normalized["inputs"]) == 0, "the contradictory count survived"
    assert prov.is_success(normalized) is False
    assert prov.consistency_faults(normalized) == [], "normalization left a result that is STILL contradictory"


def test_a_normalized_result_is_a_complete_publishable_document():
    """A non-mapping result still normalises to the schema every consumer of the artifact reads."""
    normalized = prov.normalize_result(None)

    assert normalized["schema"] == prov.SCHEMA
    assert isinstance(normalized["stamped_at"], str)
    assert normalized["input_count"] == 0 and normalized["inputs"] == []
    assert normalized["phase"]["errors"] == [{"code": "result-not-a-mapping", "operation": prov.CONSISTENCY_OPERATION}]
    assert json.loads(json.dumps(normalized)) == normalized, "the normalised result is not strict JSON"


def test_a_real_build_over_real_inputs_is_self_consistent(tmp_path):
    """The end-to-end control: what `build` actually emits must never trip its own consistency check."""
    _twbx(tmp_path)

    result = prov.build(tmp_path, {})

    assert prov.consistency_faults(result) == [], result
    assert result["phase"]["status"] == "local_only"
    assert prov.is_success(result) is True
    assert prov.normalize_result(result) is result


def test_a_build_over_an_empty_directory_is_consistent_but_not_a_pass(tmp_path):
    result = prov.build(tmp_path, {})

    assert prov.consistency_faults(result) == [], result
    assert result["phase"]["status"] == "empty"
    assert prov.is_success(result) is False


# ------------------------------------------------ the supervised leaf worker (issue #576, round 5)
#
# `build` now runs inside a process the `run_estate` parent can terminate, and reports to it over a
# deliberately tiny closed protocol. What is pinned here is the WORKER half: that the messages carry
# numbers and derived digests rather than names, that the safe snapshot leaves before the sign-out
# that can hang, that cancellation stops the next expensive operation, and that #582's call budget
# survives being instrumented.


class RecordingReporter(prov.NullReporter):
    """The supervisor channel, captured in-process.

    `checkpoint` runs the SAME reduction the real reporter does, so a mutation that checkpoints the
    raw fingerprint is visible here rather than only across a process boundary.
    """

    def __init__(self, cancel_after=None, cancel_completed=1):
        self.messages = []
        self._cancel_after = cancel_after
        self._cancel_completed = cancel_completed
        self.cancelled = False

    def inputs_discovered(self, total):
        self.messages.append({"kind": prov.MSG_INPUTS_DISCOVERED, "total": total})

    def operation(self, operation, completed, total=None):
        self.messages.append(
            {"kind": prov.MSG_OPERATION, "operation": operation, "completed": completed, "total": total}
        )
        if self._cancel_after == operation and self._cancel_completed == completed:
            self.cancelled = True

    def checkpoint(self, index, record):
        self.messages.append({"kind": prov.MSG_CHECKPOINT, "index": index, "record": prov.checkpoint_record(record)})

    def lookup_intent(self, requested):
        self.messages.append({"kind": prov.MSG_LOOKUP_INTENT, "requested": requested})

    def safe_snapshot(self, result):
        self.messages.append({"kind": prov.MSG_SAFE_SNAPSHOT, "result": result})

    def terminal(self, result):
        self.messages.append({"kind": prov.MSG_TERMINAL, "result": result})

    def kinds(self):
        return [message["kind"] for message in self.messages]

    def of_kind(self, kind):
        return [message for message in self.messages if message["kind"] == kind]

    def operations(self, name):
        return [message for message in self.of_kind(prov.MSG_OPERATION) if message["operation"] == name]


def _drain(conn):
    """Every message on the pipe, stopping at EOF - the worker closes its end when it is done."""
    messages = []
    try:
        while conn.poll():
            messages.append(conn.recv())
    except (EOFError, OSError):
        pass
    return messages


def test_a_many_input_run_reports_one_inventory_and_finishes_at_the_full_input_count(tmp_path, monkeypatch):
    """#582's budget survives instrumentation, and the progress counters describe the same run.

    The mutation this fails on: reporting per-INPUT inventory progress (66 inventory operations for
    one round trip), or counting cache hits as content attempts.
    """
    inventory = _harvested(tmp_path, 66)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory))
    reporter = RecordingReporter()

    result = prov.build(tmp_path, LIVE_ENV, reporter)

    assert (site.count("inventory"), site.count("content")) == (1, 66)
    assert reporter.of_kind(prov.MSG_INPUTS_DISCOVERED) == [{"kind": prov.MSG_INPUTS_DISCOVERED, "total": 66}]
    assert len(reporter.operations("inventory")) == 2, "one inventory operation, started and finished"
    assert reporter.operations("fingerprint")[-1]["completed"] == 66
    assert reporter.operations("fingerprint")[-1]["total"] == 66
    assert reporter.operations("content")[-1]["completed"] == 66
    assert len(reporter.of_kind(prov.MSG_CHECKPOINT)) == 66
    assert result["input_count"] == 66


def test_a_dead_inventory_is_reported_as_one_operation_not_one_per_input(tmp_path, monkeypatch):
    """Measured: 66 inputs produced 66 identical failing listings. The progress must not re-invent them."""
    _harvested(tmp_path, 12)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, inventory_status=500))
    reporter = RecordingReporter()

    result = prov.build(tmp_path, LIVE_ENV, reporter)

    assert site.count("inventory") == 1
    assert len(reporter.operations("inventory")) == 2
    assert reporter.operations("content")[-1]["completed"] == 0, "a dead inventory downloads nothing"
    assert len(result["inputs"]) == 12


def test_duplicate_luid_inputs_are_two_inputs_and_one_download(tmp_path, monkeypatch):
    """Two physical inputs, two records, two fingerprints - and exactly ONE remote content attempt.

    Duplicate semantics are load bearing: local de-duplication would silently shrink `input_count`,
    and counting the cache hit would claim remote work that never happened.
    """
    luid = _fixture_luid(7)
    _twbx(tmp_path, f"{luid}_Copy_A")
    _twbx(tmp_path, f"{luid}_Copy_B")
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=[{"id": luid, "name": "Copy A"}]))
    reporter = RecordingReporter()

    result = prov.build(tmp_path, LIVE_ENV, reporter)

    assert site.count("content") == 1
    assert reporter.of_kind(prov.MSG_INPUTS_DISCOVERED)[0]["total"] == 2
    assert reporter.operations("fingerprint")[-1]["completed"] == 2
    assert [message["completed"] for message in reporter.operations("content")] == [0, 1, 1, 1]
    assert result["input_count"] == len(result["inputs"]) == 2


def test_byte_identical_inputs_under_different_names_are_still_two_inputs(tmp_path):
    """A local content hash is not an identity either: two files are two inputs."""
    _twbx(tmp_path, "Alpha", payload=b"<workbook/>")
    _twbx(tmp_path, "Beta", payload=b"<workbook/>")
    reporter = RecordingReporter()

    result = prov.build(tmp_path, {}, reporter)

    assert result["input_count"] == len(result["inputs"]) == 2
    assert reporter.of_kind(prov.MSG_INPUTS_DISCOVERED)[0]["total"] == 2
    assert len(reporter.of_kind(prov.MSG_CHECKPOINT)) == 2
    assert [message["index"] for message in reporter.of_kind(prov.MSG_CHECKPOINT)] == [0, 1]


def test_a_checkpoint_carries_only_derived_evidence_while_the_snapshot_may_carry_names(tmp_path, monkeypatch):
    """The privacy split that makes an early checkpoint safe at all.

    A checkpoint is emitted BEFORE the live half has been scrubbed, so it holds only what this module
    COMPUTED - sizes, digests, CRCs. The safe snapshot is emitted AFTER scrub, so it may legitimately
    hold copied strings. Neither may carry raw exception text.
    """
    luid = _fixture_luid(3)
    _twbx_with_members(tmp_path / f"{luid}_Fixture.twbx", ["Superstore Extract.hyper"])
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=[{"id": luid, "name": "Fixture"}]))
    reporter = RecordingReporter()

    prov.build(tmp_path, LIVE_ENV, reporter)

    checkpoint = reporter.of_kind(prov.MSG_CHECKPOINT)[0]
    checkpointed = json.dumps(checkpoint)
    assert set(checkpoint["record"]["input"]) <= {"size_bytes", "sha256", "revision_key", "members"}
    assert all(set(member) == {"size_bytes", "crc32"} for member in checkpoint["record"]["input"]["members"])
    assert "Fixture" not in checkpointed and "Superstore Extract.hyper" not in checkpointed
    snapshot = reporter.of_kind(prov.MSG_SAFE_SNAPSHOT)[0]
    assert snapshot["result"]["inputs"][0]["input"]["file"].endswith(".twbx"), "the snapshot is the whole result"
    assert site.count("signout") == 1


def test_the_safe_snapshot_leaves_before_the_sign_out_that_can_hang(tmp_path, monkeypatch):
    """Ordering is the whole point: sign-out is a network call, and it must not gate the evidence."""
    luid = _fixture_luid(4)
    _twbx(tmp_path, f"{luid}_Fixture")
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=[{"id": luid, "name": "Fixture"}]))
    reporter = RecordingReporter()

    prov.build(tmp_path, LIVE_ENV, reporter)

    kinds = reporter.kinds()
    snapshot_at = kinds.index(prov.MSG_SAFE_SNAPSHOT)
    sign_out_at = min(
        index
        for index, message in enumerate(reporter.messages)
        if message["kind"] == prov.MSG_OPERATION and message["operation"] == "sign-out"
    )
    assert snapshot_at < sign_out_at, "the snapshot was held hostage to the cleanup call"
    assert site.count("signout") == 1


def test_a_sign_out_failure_after_the_snapshot_is_typed_and_keeps_the_result(tmp_path, monkeypatch):
    """An ordinary (non-hanging) sign-out failure stays a typed error beside a complete result."""
    luid = _fixture_luid(5)
    _twbx(tmp_path, f"{luid}_Fixture")
    site = _install(
        monkeypatch,
        RecordingSite(
            LIVE_ENV,
            workbooks=[{"id": luid, "name": "Fixture"}],
            signout_error=urllib.error.URLError("sign-out transport is dead"),
        ),
    )
    reporter = RecordingReporter()

    result = prov.build(tmp_path, LIVE_ENV, reporter)

    snapshot = reporter.of_kind(prov.MSG_SAFE_SNAPSHOT)[0]["result"]
    assert snapshot["inputs"][0]["input"]["sha256"]
    assert result["phase"]["status"] == "partial"
    assert result["phase"]["errors"][-1] == {
        "code": "sign-out-failed",
        "operation": "sign-out",
        "exception_class": "URLError",
    }
    assert site.count("signout") == 1


def test_cancellation_stops_the_run_before_the_next_expensive_operation(tmp_path, monkeypatch):
    """The cooperative half: once the supervisor latches, no further remote work is STARTED.

    It is deliberately not credited as the enforcement - a call already in flight is stopped by the
    parent killing this process - but it is what keeps a cancelled worker from spending a site's
    quota on results nobody will accept.
    """
    _harvested(tmp_path, 3)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=[]))
    reporter = RecordingReporter(cancel_after="fingerprint")

    result = prov.build(tmp_path, LIVE_ENV, reporter)

    assert site.calls == [], "a cancelled worker signed in anyway"
    assert len(reporter.of_kind(prov.MSG_CHECKPOINT)) == 1, "cancellation did not stop the fingerprint pass"
    assert result["input_count"] == len(result["inputs"]) == 3
    assert all(record["input"] == {"status": "unavailable"} for record in result["inputs"][1:])
    assert prov.consistency_faults(result) == [], result


def test_the_worker_returns_a_typed_terminal_result_when_the_build_itself_raises(tmp_path, monkeypatch):
    """A failure `build` cannot describe is still a complete, publishable, TEXT-FREE document.

    The message the exception carried is a host path here, which is exactly the kind of string that
    must not travel to the parent or reach the artifact.
    """
    secret = str(tmp_path / "customer-secret")

    def fail(*_args, **_kwargs):
        raise OSError(5, secret)

    monkeypatch.setattr(prov, "build", fail)
    recv, send = multiprocessing.Pipe(duplex=False)

    prov.provenance_worker(send, None, {"input": str(tmp_path), "env": str(tmp_path / "absent.env")})

    messages = _drain(recv)
    assert [message["kind"] for message in messages] == [prov.MSG_INPUTS_DISCOVERED, prov.MSG_TERMINAL]
    assert messages[-1]["result"]["phase"]["errors"] == [
        {"code": "build-failed", "operation": "build", "exception_class": "OSError", "errno": 5}
    ]
    assert secret not in json.dumps(messages)


def test_the_worker_reports_progress_it_can_no_longer_send(tmp_path):
    """The parent closes its end at the deadline; the worker must not die of a broken pipe.

    It is about to be terminated anyway, and raising here would replace a typed timeout artifact with
    an unhandled exception in a process nobody is reading.
    """
    _twbx(tmp_path)
    recv, send = multiprocessing.Pipe(duplex=False)
    recv.close()
    reporter = prov.WorkerReporter(send, None)

    result = prov.build(tmp_path, {}, reporter)

    assert result["input_count"] == 1, "a closed channel stopped the work it was only observing"


@pytest.mark.parametrize(
    ("operation", "completed", "expected"),
    [
        ("collect-inputs", 0, (0, 0, 0, 0, 0, 0)),
        ("fingerprint", 0, (0, 0, 0, 0, 0, 0)),
        ("fingerprint", 1, (1, 0, 0, 0, 0, 0)),
        ("sign-in", 0, (3, 0, 0, 0, 0, 0)),
        ("sign-in", 1, (3, 1, 0, 0, 0, 0)),
        ("inventory", 0, (3, 1, 0, 0, 0, 0)),
        ("inventory", 1, (3, 1, 1, 0, 0, 0)),
        ("content", 0, (3, 1, 1, 0, 0, 0)),
        ("content", 1, (3, 1, 1, 1, 0, 0)),
        ("scrub", 0, (3, 1, 1, 3, 0, 0)),
        ("scrub", 1, (3, 1, 1, 3, 1, 0)),
        ("sign-out", 0, (3, 1, 1, 3, 1, 0)),
    ],
    ids=[
        "discovery",
        "first-fingerprint",
        "next-fingerprint",
        "before-sign-in",
        "after-sign-in",
        "before-inventory",
        "after-inventory",
        "before-first-content",
        "before-next-content",
        "before-scrub",
        "after-scrub",
        "before-sign-out",
    ],
)
def test_cancellation_is_checked_at_every_expensive_boundary(
    tmp_path: Path,
    monkeypatch,
    operation: str,
    completed: int,
    expected: tuple[int, ...],
) -> None:
    """Count actual expensive calls, not just reporter labels, including both sides of sign-in."""
    inventory = _harvested(tmp_path, 3)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory))
    reporter = RecordingReporter(cancel_after=operation, cancel_completed=completed)
    fingerprint_calls, scrub_calls, discovery_calls = [], [], []
    fingerprint, scrub, discover = prov.fingerprint, prov.scrub_tree, prov.collect_inputs

    def counted_fingerprint(path):
        fingerprint_calls.append(True)
        return fingerprint(path)

    def counted_scrub(*args):
        scrub_calls.append(True)
        return scrub(*args)

    def counted_discovery(path):
        discovery_calls.append(True)
        return discover(path)

    monkeypatch.setattr(prov, "fingerprint", counted_fingerprint)
    monkeypatch.setattr(prov, "scrub_tree", counted_scrub)
    monkeypatch.setattr(prov, "collect_inputs", counted_discovery)
    result = prov.build(tmp_path, LIVE_ENV, reporter)

    actual = (
        len(fingerprint_calls),
        site.count("signin"),
        site.count("inventory"),
        site.count("content"),
        len(scrub_calls),
        site.count("signout"),
    )
    assert actual == expected, f"CANCEL_BOUNDARY_{operation}_{completed}: expensive work started after cancellation"
    assert len(discovery_calls) == (0 if operation == "collect-inputs" else 1), (
        "CANCEL_DISCOVERY: discovery ran after cancellation"
    )
    assert result["input_count"] == len(result["inputs"]) == (0 if operation == "collect-inputs" else 3)
    assert not prov.is_success(result), "a cooperatively cancelled run claimed success"


def test_cancellation_before_scrub_fallback_does_not_call_the_redactor_again(tmp_path: Path, monkeypatch) -> None:
    """A failing first scrub must not admit fallback work after the parent has cancelled."""
    inventory = _harvested(tmp_path, 2)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory))
    reporter = RecordingReporter()
    calls = []

    def failing_scrub(*_args):
        calls.append(True)
        reporter.cancelled = True
        raise RuntimeError("private-response-must-not-escape")

    monkeypatch.setattr(prov, "scrub_tree", failing_scrub)
    result = prov.build(tmp_path, LIVE_ENV, reporter)
    assert len(calls) == 1, "CANCEL_SCRUB_FALLBACK: redaction was retried after cancellation"
    assert site.count("signout") == 0
    assert result["input_count"] == 2 and result["phase"]["status"] == "partial"
    assert "private-response" not in json.dumps(result)
    assert all("file" not in record["input"] for record in result["inputs"])


def test_content_checks_cancellation_at_the_uncached_fetch_not_just_the_input_loop() -> None:
    """Cancellation arising while matching one input must still stop that input's content fetch."""
    site = RecordingSite(LIVE_ENV)
    site.reporter = RecordingReporter()
    site.reporter.cancelled = True
    cancelled = False
    try:
        site.content_sha256(_fixture_luid(1))
    except RuntimeError as exc:
        cancelled = str(exc) == "cancelled"
    assert site.count("content") == 0, "CANCEL_CONTENT_FETCH: an uncached download ignored cancellation"
    assert cancelled


def test_inventory_checks_cancellation_at_the_actual_fetch() -> None:
    """A late cancellation must still prevent the inventory's one actual network request."""
    site = RecordingSite(LIVE_ENV)
    site.reporter = RecordingReporter()
    site.reporter.cancelled = True
    cancelled = False
    try:
        site.workbooks()
    except RuntimeError as exc:
        cancelled = str(exc) == "cancelled"
    assert site.count("inventory") == 0, "CANCEL_INVENTORY_FETCH: the inventory ignored cancellation"
    assert cancelled


def test_worker_cancellation_uses_a_shared_byte_without_any_worker_owned_mutex() -> None:
    """The parent's write primitive has no Event condition lock for a dying child to hold."""
    flag = multiprocessing.get_context("spawn").RawValue("b", 0)
    reporter = prov.WorkerReporter(None, flag)
    assert not hasattr(flag, "get_lock") and not hasattr(flag, "_cond")
    assert reporter.cancelled is False
    flag.value = 1
    assert reporter.cancelled is True


def test_dynamic_exception_class_names_are_not_a_diagnostic_escape(tmp_path: Path, monkeypatch, caplog) -> None:
    """Even an exception class's name is untrusted text; only stable class labels may survive."""
    inventory = _harvested(tmp_path, 1)
    hostile_class = type(r"C:\private\credential", (Exception,), {})
    _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory, signout_error=hostile_class("private-response")))
    with caplog.at_level("DEBUG"):
        result = prov.build(tmp_path, LIVE_ENV)
    rendered = json.dumps(result) + "\n".join(record.getMessage() for record in caplog.records)
    assert "private" not in rendered
    assert result["phase"]["errors"][-1]["exception_class"] == "Exception"


@pytest.mark.parametrize(
    ("scenario", "physical", "downloads"),
    [("distinct", 66, 66), ("cached", 2, 1), ("unmatched", 2, 0)],
)
def test_production_live_wire_reconciles_with_independent_transport_counts(
    tmp_path: Path, monkeypatch, scenario: str, physical: int, downloads: int
) -> None:
    """The real reporter and parent agree; the transport, not either validator, is the #582 oracle."""
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel

    import run_estate as estate  # pylint: disable=import-outside-toplevel

    if scenario == "cached":
        luid = _fixture_luid(7)
        _twbx(tmp_path, f"{luid}_Copy_A")
        _twbx(tmp_path, f"{luid}_Copy_B")
        inventory = [{"id": luid, "name": "Copy A"}]
    else:
        inventory = _harvested(tmp_path, physical)
        if scenario == "unmatched":
            inventory = []
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, workbooks=inventory))
    messages = []
    reporter = prov.WorkerReporter(
        SimpleNamespace(send=lambda message: messages.append(json.loads(json.dumps(message))))
    )
    result = prov.build(tmp_path, LIVE_ENV, reporter)
    reporter.terminal(result)
    state = estate._ProvenanceState(emit=lambda *_args: None)  # pylint: disable=protected-access
    for message in messages:
        state.accept(message)
    assert state.terminal == result and result["phase"]["status"] == "success", "PRODUCTION_LIVE_PROTOCOL"
    assert result["input_count"] == len(result["inputs"]) == physical
    assert (site.count("signin"), site.count("inventory"), site.count("content"), site.count("signout")) == (
        1,
        1,
        downloads,
        1,
    ), "TRANSPORT_COUNTS_582: operation reconciliation changed the call budget"
    assert len(site.calls) == downloads + 3
    assert [message for message in messages if message["kind"] == prov.MSG_LOOKUP_INTENT] == [
        {"kind": prov.MSG_LOOKUP_INTENT, "requested": True}
    ]


def test_production_sign_in_refusal_needs_no_inapplicable_inventory_or_cleanup(tmp_path: Path, monkeypatch) -> None:
    """A typed live failure may stop after sign-in, but must never become local_only or a protocol fault."""
    import run_estate as estate  # pylint: disable=import-outside-toplevel

    _harvested(tmp_path, 1)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV))

    def refused() -> None:
        raise RuntimeError("fixture sign-in refusal")

    monkeypatch.setattr(site, "sign_in", refused)
    reporter = RecordingReporter()
    result = prov.build(tmp_path, LIVE_ENV, reporter)
    reporter.terminal(result)
    state = estate._ProvenanceState(emit=lambda *_args: None)  # pylint: disable=protected-access
    for message in reporter.messages:
        state.accept(message)
    assert state.terminal == result
    assert result["phase"]["status"] == "partial"
    assert [error["code"] for error in result["phase"]["errors"]] == ["live-lookup-refused"]
    assert not reporter.operations("inventory") and not reporter.operations("sign-out")


# ------------------------------------------------ single-page completeness (issue #576)


def _inventory_page(row_count: int, pagination: object = ...) -> dict:
    """The REST envelope already used by RecordingSite and assess_estate's REST parser."""
    document = {
        "workbooks": {
            "workbook": [{"id": _fixture_luid(index), "name": f"Fixture {index}"} for index in range(row_count)]
        }
    }
    if pagination is not ...:
        document["pagination"] = pagination
    return document


def _build_inventory_page(tmp_path: Path, monkeypatch, row_count: int, pagination: object = ...) -> tuple:
    """Two copies of a matched LUID plus one absent LUID: three fingerprints, at most one download."""
    for suffix in ("Copy_A", "Copy_B"):
        _twbx(tmp_path, f"{_fixture_luid(0)}_{suffix}")
    _twbx(tmp_path, f"{_fixture_luid(2000)}_Absent")
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, inventory_document=_inventory_page(row_count, pagination)))
    return site, prov.build(tmp_path, LIVE_ENV)


def _assert_page_call_budget_and_fingerprints(site: RecordingSite, result: dict, tmp_path: Path) -> None:
    assert (site.count("inventory"), site.count("content")) == (
        1,
        int(site.inventory_completeness.returned_count > 0),
    ), "PAGINATION_CACHE_BUDGET: completeness changed the one-inventory/one-content-per-LUID contract"
    assert result["input_count"] == len(result["inputs"]) == 3
    for record in result["inputs"]:
        local = record["input"]
        assert local["sha256"] == hashlib.sha256((tmp_path / local["file"]).read_bytes()).hexdigest()
        if record["origin"] is not None:
            served = site.served[record["origin"]["workbook_luid"]]
            assert len(served) == 1
            assert record["origin"]["remote_sha256"] == hashlib.sha256(served[0]).hexdigest()
            assert record["origin"]["remote_revision_key"] == oid.revision_key(served[0]).as_json()


@pytest.mark.parametrize(
    "env", [{}, {"TABLEAU_SERVER_URL": "https://tableau.invalid"}, {"TABLEAU_PAT_NAME": "fixture"}]
)
def test_offline_inventory_never_acquires_a_pagination_finding(tmp_path: Path, monkeypatch, env: dict) -> None:
    _twbx(tmp_path)
    monkeypatch.setattr(prov, "TableauLookup", lambda *_a: pytest.fail("offline run opened a live lookup"))
    monkeypatch.setattr(
        prov, "_inventory_completeness", lambda *_a: pytest.fail("offline run classified a nonexistent response")
    )
    reporter = RecordingReporter()
    result = prov.build(tmp_path, env, reporter)
    assert result["phase"] == {"status": "local_only", "errors": []}, "NO_UNCONDITIONAL_PAGINATION"
    assert prov.is_success(result) and not reporter.operations("inventory")


@pytest.mark.parametrize("row_count", [0, 1, 999])
@pytest.mark.parametrize("pagination", [..., {}, {"pageNumber": "1", "pageSize": "1000"}])
def test_short_inventory_is_complete_without_a_pagination_finding(
    tmp_path: Path, monkeypatch, row_count: int, pagination: object
) -> None:
    site, result = _build_inventory_page(tmp_path, monkeypatch, row_count, pagination)
    assert site.inventory_completeness.status == "complete", "SHORT_PAGE_COMPLETE"
    assert result["phase"] == {"status": "success", "errors": []}, "NO_SMALL_INVENTORY_RESIDUAL"
    assert prov.is_success(result)
    assert result["inputs"][-1]["origin_note"] == prov.NO_ORIGIN_NOTE
    _assert_page_call_budget_and_fingerprints(site, result, tmp_path)


@pytest.mark.parametrize(
    "pagination",
    [
        {"totalAvailable": "1000"},
        {"pageNumber": "1", "pageSize": "1000", "totalAvailable": "1000"},
        {"pageNumber": 1, "pageSize": 1000, "totalAvailable": 1000},
    ],
)
def test_exactly_full_inventory_with_explicit_total_is_complete(tmp_path: Path, monkeypatch, pagination: dict) -> None:
    site, result = _build_inventory_page(tmp_path, monkeypatch, 1000, pagination)
    assert site.inventory_completeness.status == "complete", "EXPLICIT_FULL_TOTAL_COMPLETE"
    assert result["phase"] == {"status": "success", "errors": []}
    _assert_page_call_budget_and_fingerprints(site, result, tmp_path)


@pytest.mark.parametrize("row_count,total", [(0, 1), (1, 2), (999, 1001), (1000, 1001), (1000, 2000)])
def test_inventory_total_available_overrides_short_page_inference(
    tmp_path: Path, monkeypatch, row_count: int, total: int
) -> None:
    pagination = {"pageNumber": "1", "pageSize": "1000", "totalAvailable": str(total)}
    site, result = _build_inventory_page(tmp_path, monkeypatch, row_count, pagination)
    assert site.inventory_completeness.status == "truncated", "TOTAL_AVAILABLE_BINDS"
    assert result["phase"] == {
        "status": "partial",
        "errors": [
            {
                "code": "inventory-truncated",
                "operation": "inventory",
                "returned_count": row_count,
                "requested_page_size": 1000,
                "page_number": 1,
                "page_size": 1000,
                "total_available": total,
            }
        ],
    }
    assert not prov.is_success(result)
    assert result["inputs"][-1]["origin_note"] == prov.INCOMPLETE_INVENTORY_NOTE
    _assert_page_call_budget_and_fingerprints(site, result, tmp_path)


@pytest.mark.parametrize("pagination", [..., {}, {"pageNumber": "1", "pageSize": "1000"}])
def test_full_inventory_without_total_remains_unestablished(tmp_path: Path, monkeypatch, pagination: object) -> None:
    site, result = _build_inventory_page(tmp_path, monkeypatch, 1000, pagination)
    assert site.inventory_completeness.status == "cannot_establish", "FULL_PAGE_AMBIGUITY"
    assert result["phase"]["status"] == "partial"
    assert [error["code"] for error in result["phase"]["errors"]] == ["inventory-cannot-establish"]
    assert not prov.is_success(result)
    _assert_page_call_budget_and_fingerprints(site, result, tmp_path)


@pytest.mark.parametrize("row_count", [1, 1000])
@pytest.mark.parametrize("key", ["pageNumber", "pageSize", "totalAvailable"])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        False,
        -1,
        1.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        {},
        [],
        "private-response",
        "1e3",
        " 1",
        1 << 63,
    ],
)
def test_malformed_pagination_counts_never_prove_completeness(row_count: int, key: str, bad: object) -> None:
    metadata = {"pageNumber": "1", "pageSize": "1000", "totalAvailable": str(row_count), key: bad}
    site = RecordingSite(LIVE_ENV, inventory_document=_inventory_page(row_count, metadata))
    assert len(site.workbooks()) == row_count
    assert site.inventory_completeness.status == "cannot_establish", "MALFORMED_METADATA_NOT_COMPLETE"
    error = site.inventory_completeness.error()
    assert error["code"] == "inventory-cannot-establish"
    assert all(type(value) is int for field, value in error.items() if field not in {"code", "operation"})
    assert "private-response" not in json.dumps(error, allow_nan=False)


@pytest.mark.parametrize("metadata", [None, True, -1, float("nan"), "private-response", [], [{}]])
def test_malformed_pagination_container_is_not_missing_metadata(metadata: object) -> None:
    site = RecordingSite(LIVE_ENV, inventory_document=_inventory_page(1, metadata))
    site.workbooks()
    assert site.inventory_completeness.status == "cannot_establish", "MALFORMED_CONTAINER_NOT_COMPLETE"


@pytest.mark.parametrize(
    "row_count,metadata",
    [
        (1, {"pageNumber": 0}),
        (1, {"pageNumber": 2, "pageSize": 1000, "totalAvailable": 1}),
        (1000, {"pageNumber": 2, "pageSize": 1000, "totalAvailable": 2000}),
        (1, {"pageSize": 0}),
        (1, {"pageSize": 999}),
        (1, {"pageSize": 1001}),
        (1000, {"totalAvailable": 999}),
        (1001, {"pageNumber": 1, "pageSize": 1000, "totalAvailable": 1001}),
    ],
)
def test_contradictory_page_facts_cannot_certify_a_complete_inventory(row_count: int, metadata: dict) -> None:
    site = RecordingSite(LIVE_ENV, inventory_document=_inventory_page(row_count, metadata))
    site.workbooks()
    assert site.inventory_completeness.status == "cannot_establish", "CONTRADICTORY_METADATA_NOT_COMPLETE"


@pytest.mark.parametrize(
    "document,count",
    [
        ({"workbooks": {}}, 0),
        ({"workbooks": {"workbook": {}}}, 0),
        ({"workbooks": {"workbook": {"id": "fixture", "name": "Fixture"}}}, 1),
    ],
)
def test_rest_empty_and_singleton_collection_shapes_count_rows_not_fields(document: dict, count: int) -> None:
    site = RecordingSite(LIVE_ENV, inventory_document=document)
    rows = site.workbooks()
    assert isinstance(rows, list) and len(rows) == count
    assert site.inventory_completeness.status == "complete"
    assert site.inventory_completeness.returned_count == count


@pytest.mark.parametrize(
    "kwargs,exception_class",
    [
        ({"inventory_status": 500}, "RuntimeError"),
        ({"inventory_error": urllib.error.URLError("private-response")}, "URLError"),
        ({"inventory_document": b"{"}, "JSONDecodeError"),
        ({"inventory_document": None}, "ValueError"),
        ({"inventory_document": {}}, "ValueError"),
        ({"inventory_document": {"workbooks": None}}, "ValueError"),
        ({"inventory_document": {"workbooks": {"workbook": "private-response"}}}, "ValueError"),
        ({"inventory_document": {"workbooks": {"workbook": [None]}}}, "ValueError"),
        (
            {
                "inventory_document": (
                    b'{"pagination":{"totalAvailable":2,"totalAvailable":0},"workbooks":{"workbook":[]}}'
                )
            },
            "ValueError",
        ),
    ],
)
def test_inventory_failure_does_not_acquire_a_pagination_state(
    tmp_path: Path, monkeypatch, kwargs: dict, exception_class: str
) -> None:
    _harvested(tmp_path, 2)
    site = _install(monkeypatch, RecordingSite(LIVE_ENV, **kwargs))
    result = prov.build(tmp_path, LIVE_ENV)
    assert site.inventory_completeness is None, "FAILED_INVENTORY_HAS_NO_PAGINATION_STATE"
    assert result["phase"]["status"] == "partial"
    assert (
        result["phase"]["errors"]
        == [{"code": "live-lookup-failed", "operation": "lookup-origin", "exception_class": exception_class}] * 2
    )
    assert all(record["input"]["sha256"] for record in result["inputs"])
    assert (site.count("inventory"), site.count("content")) == (1, 0)
    assert "private-response" not in json.dumps(result, allow_nan=False)


def test_inventory_rows_and_completeness_share_one_decode_and_one_cache(monkeypatch) -> None:
    site = RecordingSite(LIVE_ENV, inventory_document=_inventory_page(1000, {"totalAvailable": "1001"}))
    decoded, classified = [], []
    decode, classify = json.loads, prov._inventory_completeness

    def counted_decode(*args, **kwargs):
        decoded.append(True)
        return decode(*args, **kwargs)

    def counted_classification(count, metadata):
        classified.append((count, metadata))
        return classify(count, metadata)

    monkeypatch.setattr(prov.json, "loads", counted_decode)
    monkeypatch.setattr(prov, "_inventory_completeness", counted_classification)
    rows = site.workbooks()
    page = site.inventory_completeness
    site._inventory_document = _inventory_page(0, {"totalAvailable": "0"})
    for _ in range(3):
        assert site.workbooks() is rows and site.inventory_completeness is page
    assert len(decoded) == 1 and classified == [(1000, {"totalAvailable": "1001"})], "ONE_PAGE_ONE_PARSE"
    assert site.count("inventory") == 1


def test_pagination_diagnostics_and_progress_never_copy_response_text(tmp_path: Path, monkeypatch) -> None:
    import run_estate as estate  # pylint: disable=import-outside-toplevel

    secrets = ["https://private.invalid/token", "private-site", "private-workbook", "private-project", "private-error"]
    metadata = dict(zip(("pageNumber", "pageSize", "totalAvailable", "unknown", "exception"), secrets))
    _twbx(tmp_path, f"{_fixture_luid(2000)}_Absent")
    _install(monkeypatch, RecordingSite(LIVE_ENV, inventory_document=_inventory_page(1000, metadata)))
    reporter = RecordingReporter()
    result = prov.build(tmp_path, LIVE_ENV, reporter)
    reporter.terminal(result)
    emitted = []
    state = estate._ProvenanceState(emit=lambda *args: emitted.append(args))
    for message in reporter.messages:
        state.accept(message)
    assert state.terminal == result
    assert result["phase"] == {
        "status": "partial",
        "errors": [
            {
                "code": "inventory-cannot-establish",
                "operation": "inventory",
                "returned_count": 1000,
                "requested_page_size": 1000,
            }
        ],
    }
    rendered = json.dumps({"result": result, "messages": reporter.messages, "progress": emitted}, allow_nan=False)
    assert all(secret not in rendered for secret in secrets), "PAGINATION_DIAGNOSTIC_PRIVACY"
