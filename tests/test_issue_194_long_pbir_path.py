"""The issue-194 downloadable repro: reproducible, public-safe, and boundary-crossing.

This is an **end-to-end upstream repro**, not another identifier-cap matrix. It deliberately does not
restate what `tests/test_datasource_path_envelope.py` already owns.

⚠️ **Engine-version and HOST provenance, stated rather than implied.** The Desktop A/B and the
numbers in `fixtures/upstream-repros/issue-194-long-pbir-path/README.md` were measured locally on
Windows with canonical engine **2.368.0**. The repository's required engine-integration job pins
**2.356.0** (`.github/workflows/checks.yml`) and runs on **ubuntu-latest**, so the engine-dependent
assertions below are written to hold on either:

* the **boundary and offender** claims are properties of the emitted relative paths, which are
  host-independent, so they are asserted unconditionally (the offender *identity* is version-scoped
  to the version it was measured on, recording the observed version either way);
* the engine's **MAX_PATH warning** is not an engine invariant at all - canonical 2.368.0 guards it
  with `if os.name == "nt" and len(projected) >= MAX_PATH:`, so it cannot appear on a Linux runner.
  It is asserted only on Windows at or above the measured version, and merely recorded elsewhere.

This fixture PR does **not** roll the repository's pinned engine.

⚠️ Two safety rules this module exists to keep, both learned the hard way:

* engine output goes to a **process-unique** directory obtained from `tmp_path_factory`, and the
  public script allocates a fresh id with an atomic `mkdir` - nothing is ever deleted or reused;
* an unmeasurable run must be reported as INVALID, never as a clean verdict.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engine_source  # noqa: E402  # pylint: disable=wrong-import-position
import host_paths  # noqa: E402  # pylint: disable=wrong-import-position
import run_estate  # noqa: E402  # pylint: disable=wrong-import-position
from check_path_ceiling import utf16_len  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURE = REPO / "fixtures" / "upstream-repros" / "issue-194-long-pbir-path"
BUILDER = FIXTURE / "build_repro.py"
MEASURE = FIXTURE / "measure_repro.py"


def _load(path: Path, name: str):
    """Import a fixture-local script as a module, so tests drive the SAME implementation."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


measure_repro = _load(MEASURE, "issue194_measure_repro")
_BUILDER = _load(BUILDER, "issue194_build_repro")
CASES = _BUILDER.CASES

#: Archive names are DERIVED from the builder, never hard-coded: a mutation that renames a case must
#: reach the artifact the test reads, or the mutation silently tests nothing.
LONG_ARCHIVE = f"{CASES['long']['stem']}.twbx"
SHORT_ARCHIVE = f"{CASES['short']['stem']}.twbx"

SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"
ENGINE_SKIP_REASON = "deterministic tier not installed"

#: The skill's ordinary run root is 22 UTF-16 units (`C:\tfmig\runs\NNNN\out`). The engine's emitted
#: relative tails are root-independent, so the census is exercised at that LENGTH without creating
#: the path - which is also what lets this run on a host that has no `C:\`.
SKILL_ROOT_LEN = 22

#: The version the offender-identity claim was measured on. Below it, the claim is recorded but not
#: asserted (see the module docstring).
OFFENDER_MEASURED_ON = (2, 368, 0)

#: ⚠️ CI finding: the engine's MAX_PATH warning is a **Windows-only** code path, not a cross-platform
#: engine invariant. Read-only in canonical 2.368.0,
#: `skills/tableau-migration/scripts/migrate_estate.py` guards it with
#: `if os.name == "nt" and len(projected) >= MAX_PATH:` - so on a Linux runner the engine emits
#: nothing however long the projected path is, and both the pinned-2.356 and latest-2.368 ubuntu jobs
#: failed an unconditional assertion for that reason alone. The warning is asserted only where it was
#: established (Windows, at or above the version it was measured on) and merely RECORDED elsewhere;
#: the boundary and offender claims stay unconditional because they are properties of the emitted
#: paths, which are host-independent.
WARNING_ESTABLISHED_ON_NT_ONLY = True

#: Exactly what a public archive may contain: one root-level `.twb`, one CSV under `Data/`.
CSV_MEMBER_RE = re.compile(r"^Data/[A-Za-z0-9._-]+/[A-Za-z0-9 ._-]+\.csv$")
TWB_MEMBER_RE = re.compile(r"^[A-Za-z0-9 ._-]+\.twb$")

#: ⚠️ Round-2 review: the structural allowlist below accepted arbitrary customer identity in
#: ordinary captions and arbitrary bytes in the CSV. These constants are the INDEPENDENT control -
#: written out here, never read from `build_repro.CASES`, so a builder edit is caught rather than
#: followed. `test_the_builder_still_carries_the_intended_generic_identity` pins the two against
#: each other.
EXPECTED_IDENTITY = {
    "long": {
        "stem": "Regional Sales Performance and Inventory Turnover Review FY2026 Q3 Final",
        "datasource": "Regional Sales Performance and Inventory Turnover Consolidated Source",
        "dashboard": "Regional Sales Performance and Inventory Turnover Review Dashboard",
        "worksheet": "Regional Net Revenue by Sales Region and Fiscal Period Detail",
        "csv": "Regional Sales Performance and Inventory Turnover FY2026 Q3 Detail Extract.csv",
    },
    "short": {
        "stem": "Regional Sales FY26Q3",
        "datasource": "Regional Sales",
        "dashboard": "Regional Sales Review",
        "worksheet": "Net Revenue by Region",
        "csv": "regional_sales.csv",
    },
}

#: The fixed, non-identity vocabulary a legitimate build emits: column captions and internal names,
#: the federated/textscan connection ids, the archive's data folder and two layout edge names.
STRUCTURAL_LITERALS = frozenset(
    {
        "Region",
        "Fiscal Period",
        "Units Shipped",
        "Net Revenue",
        "region",
        "fiscal_period",
        "units_shipped",
        "net_revenue",
        "[region]",
        "[fiscal_period]",
        "[units_shipped]",
        "[net_revenue]",
        "[none:region:nk]",
        "[sum:net_revenue:qk]",
        "federated.regionalsales",
        "textscan.regionalsales",
        "Data/regional-sales",
        "left",
        "top",
    }
)

#: Attributes that can carry a human-authored caption or an identity.
IDENTITY_ATTRS = ("caption", "name", "table", "column", "filename", "directory")

#: The synthetic payload, pinned independently of `src/regional_sales.csv`. ⚠️ This is the digest of
#: the **LF-normalised** bytes the builder writes into the archive (`build_repro.payload_bytes`), not
#: of whatever the working tree holds - a Windows checkout with `core.autocrlf=true` has the same
#: file at 156 CRLF bytes. Pinning the normalised form is the point: it is the same on every host.
EXPECTED_CSV_SHA256 = "91285f691f812e2ec0b0255cde697e90cd85f1b8e6e8a47603c153988d6ac396"
EXPECTED_CSV_HEADER = ("region", "fiscal_period", "units_shipped", "net_revenue")
EXPECTED_CSV_ROWS = (
    ("North", "2026-Q1", "120", "48250.00"),
    ("South", "2026-Q1", "95", "37110.00"),
    ("East", "2026-Q1", "143", "55980.00"),
    ("West", "2026-Q1", "88", "31420.00"),
)

#: Payload shapes that must never appear in ANY archive member, caption or data cell.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET_RE = re.compile(r"(?i)\b(pass(?:word|wd)|secret|token|api[_-]?key|bearer|credential|apikey)\b|\*{4,}|-----BEGIN")

#: Element and attribute names that carry a location, an identity or a secret in Tableau XML.
FORBIDDEN_ELEMENTS = {"repository-location", "repository", "user", "credential", "credentials"}
FORBIDDEN_ATTRS = {
    "server",
    "host",
    "hostname",
    "dbname",
    "username",
    "user",
    "password",
    "token",
    "auth",
    "authentication",
    "site",
    "sitename",
    "xml:base",
    "port",
}
#: Attributes above that Tableau writes as an EMPTY placeholder in a legitimate packaged flat-file
#: workbook. Empty is allowed; any value is not.
EMPTY_ONLY_ATTRS = {"server"}

URLISH_RE = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|\\\\[^\\]|^[A-Za-z]:[\\/])")

#: ⚠️ CI finding, and the attribution matters. A rebuild differed on ubuntu-latest for a **proven**
#: reason - a CRLF working tree vs an LF blob for `src/regional_sales.csv`, covered by
#: `test_the_build_is_immune_to_the_checkouts_line_endings`. Deflate variance was the *proposed*
#: cause and was never isolated; the archives are STORED anyway, because a DEFLATE stream does depend
#: on the linked zlib build and removing that class costs nothing at 7 KB. The constants below pin
#: every remaining header field `zipfile` would otherwise derive from the host.
EXPECTED_MEMBER_COMPRESS_TYPE = zipfile.ZIP_STORED
EXPECTED_MEMBER_DATE = (1980, 1, 1, 0, 0, 0)
EXPECTED_MEMBER_CREATE_SYSTEM = 0
EXPECTED_MEMBER_EXTERNAL_ATTR = 0o600 << 16


def _contract() -> Path | None:
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


def _members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist()}


def _version_tuple(text: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text or "0")[:3]) or (0,)


# -- offline: the archives are reproducible and public-safe ----------------------------------------
def test_the_committed_archives_rebuild_byte_for_byte() -> None:
    """A maintainer must be able to regenerate exactly what they downloaded."""
    done = subprocess.run(
        [sys.executable, str(BUILDER), "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, (
        "the committed .twbx archives no longer match a rebuild from src/. Re-run build_repro.py and "
        f"commit the result, or the download and the recipe disagree.\n{done.stdout}\n{done.stderr}"
    )
    assert "DIFFER" not in done.stdout and "MISSING" not in done.stdout, done.stdout


@pytest.mark.parametrize("archive_name", [LONG_ARCHIVE, SHORT_ARCHIVE])
def test_the_archive_encoding_is_pinned_and_host_independent(archive_name: str) -> None:
    """Reproducibility must hold ACROSS operating systems, not merely across runs on one machine.

    This is the *hardening* half of the cross-platform guarantee: no compressor implementation and no
    host filesystem may leak a byte into the archive. The half that was actually broken is
    `test_the_build_is_immune_to_the_checkouts_line_endings`.

    * the COMMITTED archive is STORED with every host-derived header field pinned;
    * rebuilding with `sys.platform` reporting Linux produces the IDENTICAL bytes - which exercises
      the one platform branch `zipfile.ZipInfo.__init__` actually has (`create_system` 0 vs 3).
    """
    with zipfile.ZipFile(FIXTURE / archive_name) as zf:
        infos = zf.infolist()
        assert infos, f"{archive_name} has no members"
        for info in infos:
            where = f"{archive_name}:{info.filename}"
            assert info.compress_type == EXPECTED_MEMBER_COMPRESS_TYPE, (
                f"{where} is compress_type {info.compress_type}, not STORED. A compressed member "
                "makes the archive digest depend on the machine's zlib build - see CI on ubuntu."
            )
            assert info.compress_size == info.file_size, (
                f"{where} stores {info.compress_size} bytes for a {info.file_size}-byte payload; "
                "a STORED member must be its payload verbatim"
            )
            assert info.date_time == EXPECTED_MEMBER_DATE, f"{where} timestamp {info.date_time}"
            assert info.create_system == EXPECTED_MEMBER_CREATE_SYSTEM, (
                f"{where} create_system {info.create_system}; zipfile derives this from the host OS "
                "(0 on Windows, 3 elsewhere) unless it is pinned"
            )
            assert info.external_attr == EXPECTED_MEMBER_EXTERNAL_ATTR, f"{where} attr {info.external_attr:#o}"

            if info.filename.endswith(".csv"):
                assert b"\r\n" not in zf.read(info), (
                    f"{where} carries CRLF, so the committed archive was built from a Windows "
                    "checkout without normalisation - a Linux rebuild will not match it"
                )

    case = "long" if archive_name == LONG_ARCHIVE else "short"
    _, native = _BUILDER.build(case)
    original = sys.platform
    try:
        sys.platform = "linux"
        _, as_linux = _BUILDER.build(case)
    finally:
        sys.platform = original
    assert hashlib.sha256(as_linux).hexdigest() == hashlib.sha256(native).hexdigest(), (
        f"{archive_name} rebuilds to different bytes when the interpreter reports a different "
        "platform, so `--check` cannot hold across operating systems"
    )


@pytest.mark.parametrize("archive_name", [LONG_ARCHIVE, SHORT_ARCHIVE])
def test_the_build_is_immune_to_the_checkouts_line_endings(archive_name: str, tmp_path: Path) -> None:
    """The PROVEN cause of the Linux-only mismatch: a CRLF working tree and an LF blob.

    ⚠️ Evidence, not hypothesis. `src/regional_sales.csv` is **156 bytes with CRLF** in a Windows
    working tree (`core.autocrlf=true`) and **151 bytes with LF** in the git blob a Linux runner
    checks out - `git cat-file blob` against the index says so directly. The old builder read it with
    `read_bytes()`, so the archive contained a *different member* on each platform, and no ZIP
    setting could have fixed that. The `.twb` template was accidentally immune because `read_text()`
    applies universal newlines; `build_repro.payload_bytes` now applies the same rule to both.

    This control feeds the builder BOTH checkouts of the same logical source and requires one digest.
    It is deliberately separate from the encoding pin above: they fail for different reasons, and
    conflating them is how the wrong cause got blamed in the first place.
    """
    case = "long" if archive_name == LONG_ARCHIVE else "short"
    digests = set()
    for label, newline in (("lf", b"\n"), ("crlf", b"\r\n")):
        checkout = tmp_path / label
        checkout.mkdir()
        for source in (_BUILDER.TEMPLATE, _BUILDER.CSV):
            body = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline)
            (checkout / source.name).write_bytes(body)
        original = (_BUILDER.TEMPLATE, _BUILDER.CSV)
        try:
            _BUILDER.TEMPLATE = checkout / original[0].name
            _BUILDER.CSV = checkout / original[1].name
            _, payload = _BUILDER.build(case)
        finally:
            _BUILDER.TEMPLATE, _BUILDER.CSV = original
        digests.add(hashlib.sha256(payload).hexdigest())

    assert len(digests) == 1, (
        f"{archive_name} builds to different bytes from a CRLF checkout than from an LF checkout "
        f"({sorted(digests)}). `build_repro.py --check` would then pass on the machine that built "
        "the archives and fail on every other one - which is exactly what CI reported."
    )
    assert hashlib.sha256((FIXTURE / archive_name).read_bytes()).hexdigest() == digests.pop(), (
        f"{archive_name} on disk is not what a line-ending-normalised build produces; regenerate it"
    )


def test_the_builder_still_carries_the_intended_generic_identity() -> None:
    """The independent identity pin: what the builder emits must be what this file expects.

    ⚠️ Round-2 review: every other identity check derived its expectation from `build_repro.CASES`,
    so editing the builder moved the goalposts with the code. `EXPECTED_IDENTITY` is written out
    here instead, and a mismatch is a failure rather than a new baseline.
    """
    assert CASES == EXPECTED_IDENTITY, (
        "the builder's identity values no longer match the generic set this fixture is allowed to "
        "publish. If the change is deliberate, update EXPECTED_IDENTITY *and* re-check that the new "
        f"names carry no customer identity.\n  builder : {CASES}\n  expected: {EXPECTED_IDENTITY}"
    )


@pytest.mark.parametrize("archive_name", [LONG_ARCHIVE, SHORT_ARCHIVE])
def test_each_archive_ships_exactly_the_synthetic_dataset(archive_name: str) -> None:
    """The data control: exact digest, exact schema, exact four rows.

    A structural allowlist says nothing about payload bytes; a real customer extract would sail
    through it. This pins the CSV three ways - an independent digest, the header, and every cell.
    """
    members = _members(FIXTURE / archive_name)
    csv_name = next(name for name in members if name.endswith(".csv"))
    payload = members[csv_name]
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_CSV_SHA256, (
        f"{archive_name}: the shipped CSV is not the pinned synthetic dataset "
        f"(sha256 {hashlib.sha256(payload).hexdigest()})"
    )
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
    assert tuple(rows[0]) == EXPECTED_CSV_HEADER, f"{archive_name}: CSV header {rows[0]}"
    body = tuple(tuple(row) for row in rows[1:] if row)
    assert body == EXPECTED_CSV_ROWS, f"{archive_name}: CSV rows changed:\n  {body}"


@pytest.mark.parametrize("archive_name", [LONG_ARCHIVE, SHORT_ARCHIVE])
def test_no_archive_member_carries_email_or_secret_shaped_text(archive_name: str) -> None:
    """Payload content, not just structure: no address and no credential shape, anywhere."""
    for name, payload in _members(FIXTURE / archive_name).items():
        text = payload.decode("utf-8", "replace")
        for label, pattern in (("email-shaped", EMAIL_RE), ("credential/secret-shaped", SECRET_RE)):
            found = pattern.search(text) or pattern.search(name)
            assert not found, f"{archive_name}:{name} contains {label} text {found.group(0)!r}"


@pytest.mark.parametrize("archive_name", [LONG_ARCHIVE, SHORT_ARCHIVE])
def test_every_caption_and_name_comes_from_the_intended_identity_set(archive_name: str) -> None:
    """A caption is where customer identity actually leaks - so enumerate what may appear.

    Allowed: the fixed structural vocabulary, this case's five intended identity values, and the
    bracketed form of its CSV name. Anything else - `Contoso Confidential`, a person, a project
    codename - fails here rather than shipping.
    """
    side = "long" if archive_name.startswith(EXPECTED_IDENTITY["long"]["stem"]) else "short"
    identity = EXPECTED_IDENTITY[side]
    allowed = set(STRUCTURAL_LITERALS) | set(identity.values()) | {f"[{identity['csv']}]"}

    members = _members(FIXTURE / archive_name)
    root = ET.fromstring(members[next(n for n in members if n.endswith(".twb"))].decode("utf-8"))
    seen: set[str] = set()
    for element in root.iter():
        for raw_attr, value in element.attrib.items():
            if raw_attr.rsplit("}", 1)[-1].lower() in IDENTITY_ATTRS:
                seen.add(value)
    unexpected = sorted(seen - allowed)
    assert not unexpected, (
        f"{archive_name}: caption/name value(s) outside the intended generic identity set: "
        f"{unexpected}. A public repro may only carry the fixed vocabulary and its own five names."
    )


@pytest.mark.parametrize("archive_name", [LONG_ARCHIVE, SHORT_ARCHIVE])
def test_each_archive_matches_the_allowed_public_shape(archive_name: str) -> None:
    """ALLOWLIST, not blacklist: exactly the members and XML this fixture is permitted to ship.

    ⚠️ Round-1 review: an earlier version searched for five substrings, so anything not on that list
    shipped. This enumerates what is ALLOWED - two members, and an XML tree with no location,
    identity or secret bearing element or attribute - and rejects everything else.
    """
    members = _members(FIXTURE / archive_name)
    twbs = [name for name in members if TWB_MEMBER_RE.fullmatch(name)]
    csvs = [name for name in members if CSV_MEMBER_RE.fullmatch(name)]
    assert len(twbs) == 1, f"{archive_name}: expected exactly one root-level .twb, found {twbs}"
    assert len(csvs) == 1, f"{archive_name}: expected exactly one Data/<dir>/<file>.csv, found {csvs}"
    extra = sorted(set(members) - set(twbs) - set(csvs))
    assert not extra, f"{archive_name}: unexpected archive member(s) {extra}; only a .twb and its CSV may ship"

    for name, payload in members.items():
        text = payload.decode("utf-8", "replace")
        assert not host_paths.discloses_host_path(text), f"{archive_name}:{name} discloses a host profile path"
        assert not host_paths.discloses_host_location(text), f"{archive_name}:{name} discloses a host location"

    root = ET.fromstring(members[twbs[0]].decode("utf-8"))
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        assert tag not in FORBIDDEN_ELEMENTS, f"{archive_name}: <{tag}> may not ship in a public repro"
        for raw_attr, value in element.attrib.items():
            attr = raw_attr.rsplit("}", 1)[-1].lower()
            if attr in FORBIDDEN_ATTRS:
                assert attr in EMPTY_ONLY_ATTRS and value == "", (
                    f"{archive_name}: <{tag} {raw_attr}={value!r}> - this attribute may not carry a value"
                )
            assert not URLISH_RE.search(value), (
                f"{archive_name}: <{tag} {raw_attr}={value!r}> looks like a URL, UNC or drive-absolute path"
            )


def test_the_two_cases_differ_only_in_their_identity_names() -> None:
    """One variable. Same CSV bytes, and the same workbook XML once the names are mapped back."""
    long_members = _members(FIXTURE / LONG_ARCHIVE)
    short_members = _members(FIXTURE / SHORT_ARCHIVE)

    long_csv = next(n for n in long_members if n.endswith(".csv"))
    short_csv = next(n for n in short_members if n.endswith(".csv"))
    assert hashlib.sha256(long_members[long_csv]).hexdigest() == hashlib.sha256(short_members[short_csv]).hexdigest(), (
        "the two cases no longer ship identical data; the A/B would then have two variables"
    )

    tokens = {"datasource": "@@D@@", "dashboard": "@@B@@", "worksheet": "@@W@@", "csv": "@@C@@"}
    mapped = {}
    for side, members in (("long", long_members), ("short", short_members)):
        text = members[next(n for n in members if n.endswith(".twb"))].decode("utf-8")
        # Longest value first: the short case's datasource name ("Regional Sales") is a PREFIX of its
        # dashboard name ("Regional Sales Review"), so a naive order rewrites half a title.
        for key in sorted(tokens, key=lambda k, s=side: -len(CASES[s][k])):
            text = text.replace(CASES[side][key], tokens[key])
        mapped[side] = ET.canonicalize(text)
    assert mapped["long"] == mapped["short"], (
        "with the identity names mapped back to placeholders the two workbooks must be identical; "
        "anything else means the A/B changes more than the names"
    )


def test_the_long_case_carries_a_plausible_name_not_padding() -> None:
    """A repro a maintainer will act on cannot be `AAAA...`."""
    for key, value in CASES["long"].items():
        words = re.findall(r"[A-Za-z][a-z]+", value)
        assert len(words) >= 4, f"long {key} {value!r} does not read like a real title"
        assert not re.search(r"(.)\1{4,}", value), f"long {key} {value!r} looks like padding"


# -- the public script's own safety and honesty ----------------------------------------------------
def test_allocation_never_deletes_or_reuses_an_existing_run(tmp_path: Path) -> None:
    """The rule the incident bought: allocate, never clear.

    A sentinel is planted in a pre-existing candidate directory; allocation must step over it and
    leave it byte-identical. Two allocations must never return the same root.
    """
    parent = tmp_path / "runs"
    occupied = parent / "0001"
    occupied.mkdir(parents=True)
    sentinel = occupied / "precious.json"
    sentinel.write_text('{"approved": ["BP text", "BMI text", "HR text"]}', encoding="utf-8")
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()

    first = measure_repro.allocate_run(parent)
    second = measure_repro.allocate_run(parent)

    assert occupied.is_dir() and sentinel.is_file(), "allocation removed a pre-existing run directory"
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before, "allocation modified a pre-existing run"
    assert first != second, f"two allocations shared one root: {first}"
    assert first.name != "0001" and second.name != "0001", "allocation reused the occupied id"
    assert {first.name, second.name} == {"0002", "0003"}, f"unexpected ids {first.name}, {second.name}"


def test_concurrent_allocations_do_not_collide(tmp_path: Path) -> None:
    """The claim is atomicity, so it is exercised from separate PROCESSES, not one loop."""
    parent = tmp_path / "runs"
    parent.mkdir()
    snippet = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('m', r'{MEASURE}')\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "from pathlib import Path\n"
        "print(m.allocate_run(Path(sys.argv[1])).name)\n"
    )
    children = [
        subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
            [sys.executable, "-c", snippet, str(parent)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    names = []
    for child in children:
        out, err = child.communicate(timeout=300)
        assert child.returncode == 0, err
        names.append(out.strip())
    assert len(set(names)) == len(names), f"concurrent allocations collided: {names}"


def test_a_missing_engine_cannot_print_a_clean_verdict(tmp_path: Path) -> None:
    """The negative control for 'invalid measurements look successful'."""
    done = subprocess.run(
        [
            sys.executable,
            str(MEASURE),
            "--engine",
            str(tmp_path / "no-such-engine"),
            "--runs-parent",
            str(tmp_path / "runs"),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    assert done.returncode != 0, f"a missing engine exited 0:\n{done.stdout}"
    assert done.returncode == measure_repro.EXIT_INVALID, f"expected EXIT_INVALID, got {done.returncode}"
    assert "INVALID" in done.stdout, done.stdout
    assert "documented A/B held" not in done.stdout, "a run that measured nothing claimed the A/B result"


def test_an_empty_output_tree_is_invalid_not_within_ceilings(tmp_path: Path) -> None:
    """`census` of nothing must not read as a pass."""
    empty = tmp_path / "out"
    empty.mkdir()
    measured = measure_repro.census(empty, root_len=SKILL_ROOT_LEN)
    assert measured["entries"] == 0
    assert not measured["offenders"]
    reasons = measure_repro.invalid_reasons("long", measured, engine_exit=0)
    assert reasons, "an empty output tree produced no INVALID reason"
    assert any("no entries" in reason for reason in reasons), reasons


def test_a_nonzero_engine_exit_is_invalid_however_clean_the_tree_looks(tmp_path: Path) -> None:
    """Exit code first: a tree can look perfect and still be the product of a failed run."""
    out = tmp_path / "out"
    (out / "pbip").mkdir(parents=True)
    (out / "pbip" / "x.pbip").write_text("{}", encoding="utf-8")
    measured = measure_repro.census(out, root_len=SKILL_ROOT_LEN)
    assert measure_repro.invalid_reasons("long", measured, engine_exit=0) == []
    reasons = measure_repro.invalid_reasons("long", measured, engine_exit=1)
    assert any("engine exited 1" in reason for reason in reasons), reasons


# -- engine-dependent: the boundary is crossed by real emitted output ------------------------------
@pytest.fixture(scope="session", name="engine_runs")
def _engine_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the canonical engine on BOTH archives, each into its own per-process temp root."""
    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles collection-time absence
        pytest.skip(ENGINE_SKIP_REASON)
    base = tmp_path_factory.mktemp("issue-194")
    runs: dict[str, dict[str, Any]] = {}
    for case, archive in (("long", LONG_ARCHIVE), ("short", SHORT_ARCHIVE)):
        source = base / case / "in"
        source.mkdir(parents=True)
        shutil.copy2(FIXTURE / archive, source / archive)
        out = base / case / "out"
        code, _command, output = measure_repro.run_engine(engine, source, out)
        assert code == 0, f"harness failure running the engine on {archive}:\n{output[-4000:]}"
        # THE production census, at the skill's own root LENGTH - one implementation, not two.
        measured = measure_repro.census(out, root_len=SKILL_ROOT_LEN)
        assert measure_repro.invalid_reasons(case, measured, code) == [], measure_repro.invalid_reasons(
            case, measured, code
        )
        measured["engine_output"] = output
        measured["max_path_warning"] = "MAX_PATH" in output
        measured["out_dir"] = out
        runs[case] = measured
    return {"version": engine_source.engine_version(engine), "cases": runs}


@requires_engine
def test_the_long_case_crosses_the_file_ceiling_at_the_skill_default_root(engine_runs) -> None:
    """The repro's claim, on emitted paths, judged by the production census implementation."""
    long_case = engine_runs["cases"]["long"]
    version = engine_runs["version"]

    assert long_case["entries"] == 51 and long_case["files"] == 27 and long_case["directories"] == 24, (
        f"emitted structure changed: {long_case['entries']} entries "
        f"({long_case['files']} files, {long_case['directories']} dirs) on engine {version}"
    )
    assert not long_case["unknown"], f"unreadable path(s): {long_case['unknown']}"
    assert len(long_case["pbip_tails"]) == 1, f"expected one .pbip, found {long_case['pbip_tails']}"
    assert long_case["pbip_len"] <= measure_repro.FILE_CEILING, (
        f"the .pbip pointer is itself {long_case['pbip_len']} units. The point of this repro is that a "
        "LEGAL entry file still cannot be opened because of a nested required child."
    )

    files = [o for o in long_case["offenders"] if o["kind"] == "file"]
    dirs = [o for o in long_case["offenders"] if o["kind"] == "directory"]
    assert not dirs, f"unexpected overlong directory offender(s): {[o['tail'] for o in dirs]}"
    assert len(files) == 1, (
        f"expected exactly ONE overlong file at a {SKILL_ROOT_LEN}-unit root, found {len(files)}: "
        f"{[(o['tail'], o['length']) for o in files]} on engine {version}"
    )

    offender = files[0]
    if _version_tuple(version) >= OFFENDER_MEASURED_ON:
        assert measure_repro.OFFENDER_FRAGMENT in offender["tail"] and offender["tail"].endswith(
            measure_repro.OFFENDER_SUFFIX
        ), (
            f"the offender moved to {offender['tail']!r} on engine {version}. This repro is "
            "specifically about the UNCAPPED semantic-model table filename; a different offender is a "
            "different report. If an upstream cap now covers it, retire the fixture rather than pad it."
        )


@requires_engine
def test_the_engine_warning_is_asserted_only_where_it_was_established(engine_runs) -> None:
    """The engine's MAX_PATH warning is a WINDOWS observation, not a cross-platform invariant.

    ⚠️ CI finding. An earlier revision asserted `"MAX_PATH" in engine_output` unconditionally inside
    the boundary test above, and both ubuntu engine jobs (pinned 2.356.0 and latest 2.368.0) failed
    on it. The cause is not a version regression: canonical 2.368.0 guards the warning with
    `if os.name == "nt" and len(projected) >= MAX_PATH:`, so a Linux runner emits nothing however
    long the projected path is - and the failure was attributed to the wrong thing.

    So the claim is asserted only where it is established - on Windows, at or above the version it
    was measured on - and merely RECORDED everywhere else. Recording is deliberate and is not a
    weaker assertion: off Windows this test claims neither presence nor absence, because the
    observation has no evidence there in either direction.
    """
    long_case = engine_runs["cases"]["long"]
    version = engine_runs["version"]
    observed = long_case.get("max_path_warning")

    assert isinstance(observed, bool), (
        "the harness stopped recording whether the engine's MAX_PATH warning appeared. Recording is "
        "the outcome off Windows, so losing the field silently turns this test into a no-op."
    )
    print(f"engine {version} on os.name={os.name!r}: MAX_PATH warning emitted = {observed}")

    established = os.name == "nt" and _version_tuple(version) >= OFFENDER_MEASURED_ON
    if not established:
        # Deliberately NOT a skip: the repository's engine-dependent contract treats a skipped
        # engine test under T2P_REQUIRE_ENGINE_TESTS=1 as a CI failure, and a reason-keyed skip
        # would also have to be baselined. Recording the field IS the outcome here - this run
        # claims neither presence nor absence, because it has no evidence either way.
        assert WARNING_ESTABLISHED_ON_NT_ONLY, "the Windows-only provenance note was removed"
        return

    assert observed, (
        f"engine {version} on Windows no longer emits its MAX_PATH warning for this case. The "
        "README states that the engine exits 0 AND emits a non-binding warning; that provenance "
        "note would now be stale. This is about the warning only - the boundary and offender "
        "assertions above are unaffected and remain unconditional."
    )


@requires_engine
def test_the_output_root_length_is_what_decides_this_boundary(engine_runs) -> None:
    """R2: the engine's shorter-root advice DOES avoid this boundary - state it precisely.

    The relative tail is fixed at 250 units, so the verdict is a pure function of the output root
    length: the ordinary 22-unit `C:\\tfmig\\runs\\NNNN\\out` measures 273 and fails, while an
    8-unit root (`-o C:\\tfmig` itself) measures exactly 259 and is legal. That is an extreme
    placement workaround, not long-path support - `LongPathsEnabled = 1` was set throughout and
    Desktop still refused the 22-unit case.
    """
    out = engine_runs["cases"]["long"]["out_dir"]
    at22 = measure_repro.census(out, root_len=22)
    at8 = measure_repro.census(out, root_len=8)

    assert at22["longest_file_len"] == 273, f"expected 273 at a 22-unit root, got {at22['longest_file_len']}"
    assert len([o for o in at22["offenders"] if o["kind"] == "file"]) == 1, at22["offenders"]

    assert at8["longest_file_len"] == measure_repro.FILE_CEILING, (
        f"expected exactly {measure_repro.FILE_CEILING} at an 8-unit root, got {at8['longest_file_len']}"
    )
    assert not at8["offenders"], (
        f"an 8-unit output root must be legal for this fixture, but it reports "
        f"{[(o['kind'], o['length']) for o in at8['offenders']]}"
    )
    tail = at22["longest_file_len"] - 22 - 1
    assert tail == 250, f"the relative tail moved to {tail}; the 22-vs-8 arithmetic above is derived from it"


@requires_engine
def test_the_short_control_stays_inside_both_ceilings_at_the_same_root(engine_runs) -> None:
    """The A/B control: same shape, same data, short names, comfortably legal."""
    short_case = engine_runs["cases"]["short"]
    long_case = engine_runs["cases"]["long"]
    assert not short_case["offenders"], (
        f"the short control has offender(s) {[(o['kind'], o['tail']) for o in short_case['offenders']]}; "
        "an A/B with two failing arms has no control"
    )
    assert short_case["pbip_len"] <= measure_repro.FILE_CEILING
    assert not short_case["unknown"]
    assert (short_case["files"], short_case["directories"]) == (long_case["files"], long_case["directories"]), (
        f"the two cases emitted different structures ({short_case['files']}/{short_case['directories']} vs "
        f"{long_case['files']}/{long_case['directories']}); the A/B would then differ in more than names"
    )


# -- issue #564: the semantic-model table path family ----------------------------------------------
#
# The projector's model term is a GENERIC source-component envelope, not a class inventory, because
# three successive class-by-class projectors were each defeated by a filename class the engine had
# and this repository lacked: `combine_descriptors`' duplicate-relation `Relation (<datasource>)`, a
# long range/what-if parameter caption, and the per-island `Date (<datasource>)` calendar. Every
# control below therefore judges `projected >= actual` on what the engine REALLY wrote - an
# expected-filename assertion is blind to exactly the class that defeats it.
TEMPLATE_TWB = (FIXTURE / "src" / "workbook-template.twb").read_text(encoding="utf-8")
TEMPLATE_CSV = (FIXTURE / "src" / "regional_sales.csv").read_text(encoding="utf-8")

#: The kill control's identity names, deliberately the SAME length: the emitted collision name is
#: then `<relation> (<caption>)`, i.e. two full-length source-owned components plus fixed
#: punctuation, which is the only shape that can distinguish a two-component bound from a
#: one-component one AND exceed twice its longest component. ⚠️ The relation is a RENAMED logical
#: table over a short flat file, not the file name itself: a packaged flat file also contributes its
#: bracketed `[<file>#csv]` form, six units longer than the name, so a file-named relation can never
#: exceed twice the pool maximum and the fixed-overhead mutation would be unkillable. Renaming a
#: logical table is ordinary Tableau authoring. Plausible enterprise names, never repeated
#: characters (see `test_the_long_case_carries_a_plausible_name_not_padding`).
KILL_RELATION = "Regional Sales and Inventory Turnover FY2026 Q3 Detail Snapshot"
KILL_CAPTION_A = "Alpha Regional Sales and Inventory Turnover Consolidated Source"
KILL_CAPTION_B = "Delta Regional Sales and Inventory Turnover Consolidated Source"
KILL_CSV = "regional_sales.csv"

ISLAND_CAPTION_A = "Alpha Consolidated Regional Sales and Inventory Turnover Source"
ISLAND_CAPTION_B = "Beta Consolidated Regional Sales and Inventory Turnover Source"
PARAMETER_CAPTION = "Rolling Inventory Turnover Threshold for FY2026 Q3 Review"
ASTRAL_CAPTION = "Ventas 🌍🌎🌏 Consolidado Regional 📊 FY2026"
ASTRAL_CSV = "ventas_🌍_consolidado.csv"

DATE_METADATA_RECORD = """<metadata-record class='column'>
            <remote-name>order_date</remote-name>
            <remote-type>7</remote-type>
            <local-name>[order_date]</local-name>
            <parent-name>[{csv}]</parent-name>
            <remote-alias>order_date</remote-alias>
            <ordinal>4</ordinal>
            <local-type>date</local-type>
            <aggregation>Year</aggregation>
            <contains-null>true</contains-null>
          </metadata-record>"""


def _island(
    caption: str,
    ds_id: str,
    csv_name: str,
    worksheet: str,
    dashboard: str,
    *,
    dates: bool = False,
    relation_name: str | None = None,
) -> str:
    """One datasource island built from the committed repro template.

    `relation_name` renames the LOGICAL table over the same flat file - ordinary Tableau authoring,
    and the only way to give a relation an identity that is not derived from the file name.
    """
    text = TEMPLATE_TWB
    for token, value in (
        ("@@DATASOURCE@@", caption),
        ("@@CSVFILE@@", csv_name),
        ("@@WORKSHEET@@", worksheet),
        ("@@DASHBOARD@@", dashboard),
    ):
        assert token in text, f"the committed template lost its {token} placeholder"
        text = text.replace(token, value)
    text = text.replace("federated.regionalsales", ds_id)
    text = text.replace("textscan.regionalsales", "textscan." + ds_id.split(".", 1)[-1])
    if relation_name is not None:
        marker = f"name='{csv_name}' table="
        assert text.count(marker) == 1, f"the template's relation name is no longer {marker!r}"
        text = text.replace(marker, f"name='{relation_name}' table=")
    if not dates:
        return text
    text = text.replace(
        "<column datatype='real' name='net_revenue' ordinal='3' />",
        "<column datatype='real' name='net_revenue' ordinal='3' />\n"
        "            <column datatype='date' name='order_date' ordinal='4' />",
    )
    text = text.replace(
        "</metadata-records>", DATE_METADATA_RECORD.format(csv=csv_name) + "\n        </metadata-records>"
    )
    return text.replace(
        "<column caption='Net Revenue' datatype='real' name='[net_revenue]' role='measure' type='quantitative' />",
        "<column caption='Net Revenue' datatype='real' name='[net_revenue]' role='measure' type='quantitative' />\n"
        "      <column caption='Order Date' datatype='date' name='[order_date]' role='dimension' type='ordinal' />",
        1,
    )


def _combine(first: str, second: str) -> ET.Element:
    """Two islands in ONE workbook - what makes `combine_descriptors` and the per-island calendar run."""
    root = ET.fromstring(first)
    other = ET.fromstring(second)
    for container, tag in (("datasources", "datasource"), ("worksheets", "worksheet"), ("dashboards", "dashboard")):
        root.find(container).append(other.find(f"{container}/{tag}"))
    for window in other.findall("windows/window"):
        root.find("windows").append(window)
    return root


def _with_value_parameter(root: ET.Element, caption: str) -> ET.Element:
    """A range parameter plus the calc that references it, so `emit_value_parameters` fires."""
    root.find("datasources").insert(
        0,
        ET.fromstring(
            "<datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>"
            f"<column caption='{caption}' datatype='real' name='[Parameter 1]' "
            "param-domain-type='range' role='measure' type='quantitative' value='5.0'>"
            "<calculation class='tableau' formula='5.0' />"
            "<range granularity='1.0' max='100.0' min='0.0' />"
            "</column></datasource>"
        ),
    )
    root.findall("datasources/datasource")[1].append(
        ET.fromstring(
            "<column caption='Flagged Revenue' datatype='boolean' name='[Calculation_flag]' "
            "role='dimension' type='ordinal'>"
            "<calculation class='tableau' formula='[net_revenue] &gt; [Parameters].[Parameter 1]' />"
            "</column>"
        )
    )
    return root


def _write_control(base: Path, root: ET.Element, csv_names: tuple[str, ...], stem: str) -> Path:
    """Materialise one control as a loose `.twb` plus the flat files its relations name."""
    source = base / "in"
    (source / "Data" / "regional-sales").mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(source / f"{stem}.twb", encoding="utf-8", xml_declaration=True)
    for csv_name in csv_names:
        (source / "Data" / "regional-sales" / csv_name).write_text(TEMPLATE_CSV, encoding="utf-8")
    return source


def _model_controls() -> dict[str, tuple[ET.Element, tuple[str, ...], str]]:
    """The four source-visible naming shapes this module drives through the real engine."""
    duplicate = _combine(
        _island(ISLAND_CAPTION_A, "federated.alpha", "regional_sales.csv", "Sheet A", "Dash A", dates=True),
        _island(ISLAND_CAPTION_B, "federated.beta", "regional_sales.csv", "Sheet B", "Dash B", dates=True),
    )
    parameter = _with_value_parameter(
        _combine(
            _island(ISLAND_CAPTION_A, "federated.alpha", "regional_sales.csv", "Sheet A", "Dash A"),
            _island(ISLAND_CAPTION_B, "federated.beta", "regional_sales_b.csv", "Sheet B", "Dash B"),
        ),
        PARAMETER_CAPTION,
    )
    astral = ET.fromstring(_island(ASTRAL_CAPTION, "federated.astral", ASTRAL_CSV, "Hoja", "Panel"))
    kill = _combine(
        _island(KILL_CAPTION_A, "federated.alpha", KILL_CSV, "Sheet A", "Dash A", relation_name=KILL_RELATION),
        _island(KILL_CAPTION_B, "federated.beta", KILL_CSV, "Sheet B", "Dash B", relation_name=KILL_RELATION),
    )
    return {
        "duplicate_relation_and_island_date": (duplicate, ("regional_sales.csv",), "Islands"),
        "value_parameter": (parameter, ("regional_sales.csv", "regional_sales_b.csv"), "WhatIf"),
        "astral_identity": (astral, (ASTRAL_CSV,), "Astral"),
        "two_long_components": (kill, (KILL_CSV,), "Collision"),
    }


@pytest.fixture(scope="session", name="model_runs")
def _model_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the canonical engine on every model-family control, once, into per-process directories."""
    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles collection-time absence
        pytest.skip(ENGINE_SKIP_REASON)
    base = tmp_path_factory.mktemp("issue-564")
    runs: dict[str, dict[str, Any]] = {}
    for name, (root, csv_names, stem) in _model_controls().items():
        source = _write_control(base / name, root, csv_names, stem)
        out = base / name / "out"
        code, _command, output = measure_repro.run_engine(engine, source, out)
        assert code == 0, f"harness failure running the engine on control {name}:\n{output[-4000:]}"
        tables = sorted(path.name for path in out.rglob("*") if path.is_file() and path.parent.name == "tables")
        model_files = [
            str(path.relative_to(out)).replace("\\", "/")
            for path in out.rglob("*")
            if path.is_file() and any(part.endswith(".SemanticModel") for part in path.relative_to(out).parts)
        ]
        assert tables and model_files, f"control {name} emitted no semantic model at all"
        runs[name] = {
            "source": source,
            "out": out,
            "tables": tables,
            "longest_model_tail": max(model_files, key=utf16_len),
            "units": run_estate._engine_unit_names(engine, source),  # pylint: disable=protected-access
            "evidence": run_estate.model_envelope_evidence(
                run_estate._input_candidates(source),  # pylint: disable=protected-access
                engine,
            ),
        }
    return {"version": engine_source.engine_version(engine), "engine": engine, "controls": runs}


def _projected_model_file(run: dict[str, Any], root: Path, evidence: dict | None = None) -> int:
    """The longest projected semantic-model FILE for one control, at an exact root."""
    projection = run_estate.project_estate_path_ceiling(root, run["units"], evidence or run["evidence"])
    assert projection["status"] != "cannot_establish", projection.get("reason")
    return max(
        record["length"]
        for record in projection["paths"]
        if record["family"] == run_estate._FAMILY_MODEL  # pylint: disable=protected-access
        and record["kind"] == "file"
    )


def _actual_model_file(run: dict[str, Any], root: Path) -> int:
    """The longest semantic-model FILE the engine really wrote, rebased onto the same root."""
    return utf16_len(str(root / run["longest_model_tail"]))


def _actual_table_filename(run: dict[str, Any]) -> int:
    """The longest table FILENAME the engine really wrote for one control."""
    return max(utf16_len(name) for name in run["tables"])


def _projected_table_filename(evidence: dict) -> int:
    """The longest table filename production would allow for the same evidence."""
    return evidence["table_stem"] + utf16_len(run_estate._MODEL_TABLE_SUFFIX)  # pylint: disable=protected-access


@requires_engine
def test_the_model_projection_covers_the_committed_long_and_short_pair(engine_runs) -> None:
    """The A/B pair, judged on the SEMANTIC-MODEL family rather than the report family.

    The long arm's offender is itself a model table part (`measure_repro.OFFENDER_SUFFIX`), so this
    is the control where the two families' verdicts must agree in direction: the projection covers
    the emitted model path in both arms, and refuses the long one at the skill's ordinary root.
    """
    engine = _contract()
    root = Path(Path.cwd().anchor + "r" * (SKILL_ROOT_LEN - utf16_len(Path.cwd().anchor))).resolve()
    for case, archive in (("long", LONG_ARCHIVE), ("short", SHORT_ARCHIVE)):
        out = engine_runs["cases"][case]["out_dir"]
        source = out.parent / "in"
        units = run_estate._engine_unit_names(engine, source)  # pylint: disable=protected-access
        evidence = run_estate.model_envelope_evidence(
            run_estate._input_candidates(source),  # pylint: disable=protected-access
            engine,
        )
        assert evidence["status"] == "ok", f"{archive}: {evidence.get('reason')}"
        projection = run_estate.project_estate_path_ceiling(root, units, evidence)
        projected = max(
            record["length"]
            for record in projection["paths"]
            if record["family"] == run_estate._FAMILY_MODEL  # pylint: disable=protected-access
            and record["kind"] == "file"
        )
        model_tails = [
            str(path.relative_to(out)).replace("\\", "/")
            for path in out.rglob("*")
            if path.is_file() and any(part.endswith(".SemanticModel") for part in path.relative_to(out).parts)
        ]
        actual = max(utf16_len(str(root / tail)) for tail in model_tails)
        assert projected >= actual, (
            f"{case}: projected {projected} < emitted {actual} on engine {engine_runs['version']}"
        )
        if case == "long":
            assert projection["status"] == "over_ceiling", (
                "the long arm emits a model table part measured at 273 units at this root; the "
                "projection must refuse it"
            )
            assert projection["family"] == run_estate._FAMILY_MODEL  # pylint: disable=protected-access


@requires_engine
def test_each_control_reproduces_the_naming_class_it_exists_for(model_runs) -> None:
    """Guard the controls themselves: a control that stopped emitting its class proves nothing.

    ⚠️ These are the three classes that defeated class-by-class projection, plus the astral case.
    They are asserted on the engine's OWN emitted filenames, so an upstream naming change is loud
    here rather than silently making every projection assertion below vacuous.
    """
    version = model_runs["version"]
    duplicate = model_runs["controls"]["duplicate_relation_and_island_date"]["tables"]
    assert any(name.startswith(f"regional_sales.csv ({ISLAND_CAPTION_B}") for name in duplicate), (
        f"the duplicate-relation disambiguation class disappeared on engine {version}: {duplicate}"
    )
    assert sum(name.startswith("Date (") for name in duplicate) == 2, (
        f"the per-island calendar class disappeared on engine {version}: {duplicate}"
    )

    parameter = model_runs["controls"]["value_parameter"]["tables"]
    assert any(name.startswith(PARAMETER_CAPTION) for name in parameter), (
        f"the what-if parameter table class disappeared on engine {version}: {parameter}"
    )

    astral = model_runs["controls"]["astral_identity"]["tables"]
    assert any("🌍" in name for name in astral), (
        f"the astral identity never reached a table filename on engine {version}: {astral}"
    )

    kill = model_runs["controls"]["two_long_components"]["tables"]
    combined = next((name for name in kill if name.startswith(f"{KILL_RELATION} ({KILL_CAPTION_B}")), None)
    assert combined, f"the two-long-component control lost its collision on engine {version}: {kill}"
    stem = combined[: -utf16_len(run_estate._MODEL_TABLE_SUFFIX)]  # pylint: disable=protected-access
    assert utf16_len(stem) > 2 * utf16_len(KILL_CAPTION_A), (
        f"the emitted name {stem!r} ({utf16_len(stem)} units) no longer exceeds twice the longest "
        f"source component ({utf16_len(KILL_CAPTION_A)}); the fixed-overhead mutation below would "
        "then be unkillable and this control has stopped doing its job"
    )


@requires_engine
def test_the_model_projection_covers_every_emitted_table_part(model_runs) -> None:
    """The invariant: projected >= actual, on the bytes the canonical engine really wrote."""
    root = Path(Path.cwd().anchor + "r" * (SKILL_ROOT_LEN - utf16_len(Path.cwd().anchor))).resolve()
    for name, run in model_runs["controls"].items():
        assert run["evidence"]["status"] == "ok", (
            f"{name}: production could not establish its own evidence on engine {model_runs['version']}: "
            f"{run['evidence'].get('reason')}"
        )
        projected = _projected_model_file(run, root)
        actual = _actual_model_file(run, root)
        print(f"{name}: projected {projected} >= actual {actual} ({run['longest_model_tail']})")
        assert projected >= actual, (
            f"{name}: production projects {projected} units where the engine wrote {actual} "
            f"({run['longest_model_tail']!r}) on engine {model_runs['version']}. Understating the "
            "semantic-model family is the fail-open defect of issue #564."
        )


@requires_engine
def test_dropping_the_second_source_component_understates_real_engine_output(model_runs, monkeypatch) -> None:
    """MUTATION 1 - the two-component term. Remove it and this control must fail.

    `combine_descriptors` composes `f"{name} ({caption})"`, so an emitted filename can carry TWO
    full-length source-owned components. A one-component envelope is the class-blind mistake this
    replaces, and it understates measured output rather than merely being tighter.

    The comparison is on the FILENAME term, which is the term being mutated: the whole-path
    comparison (`test_the_model_projection_covers_every_emitted_table_part`) also carries the
    projected model-folder base, whose deliberate over-projection would absorb the mutation and make
    this test vacuous.
    """
    run = model_runs["controls"]["two_long_components"]
    actual = _actual_table_filename(run)
    assert _projected_table_filename(run["evidence"]) >= actual, "the unmutated bound must cover the control"

    monkeypatch.setattr(
        run_estate,
        "_MODEL_NAME_SHAPES",
        tuple((1, literal) for _count, literal in run_estate._MODEL_NAME_SHAPES),  # pylint: disable=protected-access
    )
    mutated = run_estate._model_table_stem_bound(  # pylint: disable=protected-access
        run["evidence"]["component"]
    ) + utf16_len(run_estate._MODEL_TABLE_SUFFIX)  # pylint: disable=protected-access
    assert mutated < actual, (
        f"a ONE-component envelope ({mutated}) still covers the {actual}-unit two-component filename "
        f"the engine emitted ({max(run['tables'], key=utf16_len)!r}), so this control cannot detect "
        "the defect it exists for. Lengthen the control's identity names rather than deleting the "
        "assertion."
    )


@requires_engine
def test_dropping_the_fixed_overhead_understates_real_engine_output(model_runs, monkeypatch) -> None:
    """MUTATION 2 - the fixed punctuation and uniquification allowance. Remove them and it must fail.

    ⚠️ Honest limit: the two sub-terms are killed TOGETHER, not independently. The emitted collision
    name exceeds twice its longest component by the three units of `" ("` + `")"`, which the 16-unit
    uniquifier allowance would absorb on its own - so no committed control kills the allowance
    alone. It is deliberate slack for the `" 2"` / `"_2"` climbs, not a measured boundary, and this
    is stated rather than implied.
    """
    run = model_runs["controls"]["two_long_components"]
    actual = _actual_table_filename(run)

    monkeypatch.setattr(
        run_estate,
        "_MODEL_NAME_SHAPES",
        tuple((count, 0) for count, _literal in run_estate._MODEL_NAME_SHAPES),  # pylint: disable=protected-access
    )
    monkeypatch.setattr(run_estate, "_MODEL_UNIQUIFIER_UTF16", 0)
    mutated = run_estate._model_table_stem_bound(  # pylint: disable=protected-access
        run["evidence"]["component"]
    ) + utf16_len(run_estate._MODEL_TABLE_SUFFIX)  # pylint: disable=protected-access
    assert mutated < actual, (
        f"with NO fixed overhead the bound ({mutated}) still covers the {actual}-unit emitted "
        "filename, so the overhead term is unproven by this control"
    )


@requires_engine
def test_the_version_gate_is_what_refuses_an_unaudited_engine(model_runs, monkeypatch) -> None:
    """MUTATION 3 - the version gate. Widen it and the refusal disappears.

    The gate is the reason the bound is a claim about a SPECIFIC engine tree. Reported against the
    canonical tree with a simulated version, so the census half is genuinely satisfied and only the
    version decides - a stub tree would fail the census too and prove nothing about the gate.
    """
    engine = model_runs["engine"]
    monkeypatch.setattr(run_estate, "engine_version", lambda _root=None: "9.9.9-unaudited")

    refused = run_estate._model_write_site_census(engine)  # pylint: disable=protected-access
    assert refused["status"] == "cannot_establish"
    assert "9.9.9-unaudited" in refused["reason"]

    monkeypatch.setattr(run_estate, "_MODEL_AUDITED_ENGINE_VERSIONS", frozenset({"9.9.9-unaudited"}))
    widened = run_estate._model_write_site_census(engine)  # pylint: disable=protected-access
    assert widened["status"] == "ok", (
        f"with the gate widened the census still refuses ({widened.get('reason')}), so the assertion "
        "above was passing for the census's reasons rather than the version gate's - the mutation "
        "would not be independent"
    )
