"""The issue-194 downloadable repro: reproducible, public-safe, and boundary-crossing.

This is an **end-to-end upstream repro**, not another identifier-cap matrix. It deliberately does not
restate what `tests/test_datasource_path_envelope.py` already owns.

⚠️ **Engine-version provenance, stated rather than implied.** The Desktop A/B and the numbers in
`fixtures/upstream-repros/issue-194-long-pbir-path/README.md` were measured locally on canonical
engine **2.368.0**. The repository's required engine-integration job pins **2.356.0**
(`.github/workflows/checks.yml`), so the engine-dependent assertions below are written to hold on
either: they assert the *boundary* unconditionally and the *specific uncapped table-file offender*
only at or above the version it was measured on, recording the observed version either way. This
fixture PR does **not** roll the repository's pinned engine.

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

#: The synthetic payload, pinned independently of `src/regional_sales.csv`.
EXPECTED_CSV_SHA256 = "da5fc2aeea765c03d0180b368adf2143a19ffac1c6251ef68f671d527269fde5"
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
    assert "MAX_PATH" in long_case["engine_output"], (
        "the engine no longer emits its MAX_PATH warning for this case; the README's provenance note "
        "about a non-binding warning would then be stale"
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
