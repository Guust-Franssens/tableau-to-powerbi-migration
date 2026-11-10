"""Direct tests for the package filesystem/manifest integrity verifier (issue #562, slice S1).

The invariant under test, stated once so every case below can be read against it:

    for a target the merged classifier has ALREADY called a safe package, the root
    `package-manifest.json` must be strict readable JSON whose `contents.files` describes EXACTLY
    every regular file in the package and its SHA-256 bytes.

Everything that is missing, malformed, duplicated, non-finite, unsafe or unassessable is non-clean;
only exact namespace-and-hash equality is clean. The two directions matter equally, so each refusal
below is paired with a positive control that must stay clean - a verifier that refuses everything
satisfies every fail-closed test in this file and is useless.

⚠️ **Scope is a property under test, not a comment.** S1 knows nothing about roles, identity, LUIDs,
oracle semantics, source resolution or the working lifecycle. The tests that prove a role-missing or
identity-contradicting package stays S1-CLEAN are as load-bearing as the refusals: they are what
stops this slice from quietly becoming the whole of #562.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bundle_corpus  # noqa: E402  # pylint: disable=wrong-import-position
import package_filesystem as pfs  # noqa: E402  # pylint: disable=wrong-import-position

MARKER = bundle_corpus.PACKAGE_MARKER


# --------------------------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    """The digest a correct producer would have recorded for this file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(package: Path, *, files: dict | None = None, extra: dict | None = None) -> Path:
    """Write a manifest describing the package as it stands, or exactly the map given.

    ``files=None`` means "declare the truth": every regular file except the manifest itself, which
    carries the map and therefore can never be one of the files the map describes.
    """
    if files is None:
        files = {
            str(path.relative_to(package).as_posix()): sha256_of(path)
            for path in sorted(package.rglob("*"))
            if path.is_file() and path.name != MARKER
        }
    manifest = {"unit": package.name, "kind": "workbook", **(extra or {}), "contents": {"files": files}}
    target = package / MARKER
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return target


def build_package(root: Path, *, files: dict[str, str] | None = None) -> Path:
    """A minimal but realistically shaped package that verifies CLEAN."""
    package = root / "packages" / "Minimal"
    package.mkdir(parents=True)
    contents = (
        files
        if files is not None
        else {
            "README.md": "how to open this package\n",
            "report.json": '{"workbooks": [{"name": "Minimal"}]}\n',
            "fabric/Minimal.Report/definition.pbir": '{"version": "1.0"}\n',
            "oracle/oracle-manifest.json": '{"views": []}\n',
        }
    )
    for relative, text in contents.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    write_manifest(package)
    return package


def classify(package: Path) -> bundle_corpus.TargetClassification:
    """The classification the entry gate would have computed before calling the verifier."""
    return bundle_corpus.classify_target(package)


def verify(package: Path) -> pfs.PackageFilesystemResult:
    """Classify then verify, exactly as the consumer does."""
    return pfs.verify_package(package, classify(package))


def link_directory(link: Path, target: Path) -> None:
    """A junction (Windows) or a directory symlink (POSIX) - a reparse point either way."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        completed = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if completed.returncode != 0:
            pytest.skip(f"could not create junction: {completed.stderr.decode(errors='replace').strip()}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege-dependent
        pytest.skip("this platform/account cannot create symlinks without elevation")


def link_file(link: Path, target: Path) -> None:
    """A file symlink, skipped where the account cannot create one."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/account cannot create symlinks without elevation")


# --------------------------------------------------------------------------------------------
# The positive controls. Without these, refusing everything would pass every test below.
# --------------------------------------------------------------------------------------------


def test_a_package_whose_manifest_describes_its_bytes_is_clean(tmp_path: Path) -> None:
    """The vacuity control for the whole file: a correct package must verify CLEAN."""
    package = build_package(tmp_path)

    result = verify(package)

    assert result.status == pfs.STATUS_CLEAN
    assert result.is_clean
    assert result.findings == ()
    assert result.unassessable == ()
    assert result.files_declared == result.files_verified == 4


# --------------------------------------------------------------------------------------------
# Strict JSON
# --------------------------------------------------------------------------------------------


def test_a_manifest_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    """Undecodable bytes are not a manifest, and must not surface as a decoder traceback."""
    package = build_package(tmp_path)
    (package / MARKER).write_bytes(b'{"contents": {"files": {"\xff\xfe": ""}}}')

    assert verify(package).first_code == pfs.CODE_MANIFEST_NOT_UTF8


def test_a_duplicate_key_carrying_a_SECRET_is_refused_without_echoing_it(tmp_path: Path) -> None:
    """The refusal is pasted into issues; a duplicated key can hold a token or a customer host.

    Two things are asserted, not one: the manifest is refused, AND neither the key nor either of its
    values appears anywhere in the rendered rows. A diagnostic that quotes the ambiguous key to be
    helpful is how a strict-JSON guard becomes a leak.
    """
    package = build_package(tmp_path)
    secret = "sf://acme-prod.snowflakecomputing.com?token=SUPERSECRET"
    (package / MARKER).write_text(
        json.dumps({"contents": {"files": {}}})[:-1] + f', "source": "{secret}", "source": "{secret}-second"}}',
        encoding="utf-8",
    )

    result = verify(package)
    rendered = json.dumps(result.as_dict())

    assert result.first_code == pfs.CODE_MANIFEST_DUPLICATE_KEY
    assert "SUPERSECRET" not in rendered
    assert "snowflakecomputing" not in rendered


def test_a_number_that_OVERFLOWS_to_infinity_is_refused_through_the_other_door(tmp_path: Path) -> None:
    """`1e999` is an ordinary JSON token, so it never reaches `parse_constant` - same broken value."""
    package = build_package(tmp_path)
    (package / MARKER).write_text('{"notes": [1e999], "contents": {"files": {}}}', encoding="utf-8")

    assert verify(package).first_code == pfs.CODE_MANIFEST_NON_FINITE


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ('{"unit": "Minimal"}', pfs.CODE_CONTENTS_MISSING),
        ('{"contents": null}', pfs.CODE_CONTENTS_NOT_OBJECT),
        ('{"contents": {}}', pfs.CODE_FILES_MISSING),
    ],
)
def test_the_contents_map_types_are_exact(tmp_path: Path, payload: str, code: str) -> None:
    """A missing or wrongly-typed map is refused, never read as "this package declares no files"."""
    package = build_package(tmp_path)
    (package / MARKER).write_text(payload, encoding="utf-8")

    result = verify(package)

    assert result.first_code == code
    assert not result.is_clean


# --------------------------------------------------------------------------------------------
# Canonical package-relative keys
# --------------------------------------------------------------------------------------------


UNSAFE_KEYS = [
    ("backslash-separator", "fabric\\report.json"),
    ("posix-absolute", "/etc/passwd"),
    ("alternate-data-stream", "report.json:hidden"),
    ("empty", ""),
    ("dotdot-segment", "fabric/../../outside.txt"),
    ("dot-segment", "fabric/./report.json"),
    ("double-slash", "fabric//report.json"),
    ("nul", "report\x00.json"),
    ("control-char", "report\x01.json"),
    ("trailing-dot", "report.json."),
    ("trailing-space", "report.json "),
    ("leading-space", " report.json"),
    ("device-con", "CON"),
    ("device-con-extension", "con.txt"),
    ("device-nul-nested", "fabric/NUL.png"),
    ("device-aux", "aux"),
    ("device-com1", "COM1"),
    ("device-lpt9", "lpt9.log"),
    ("device-com-superscript-1", "COM\u00b9"),
    ("device-com-superscript-2", "com\u00b2.txt"),
    ("device-lpt-superscript-3", "LPT\u00b3"),
    ("device-conin", "CONIN$"),
    # Characters no Windows filename may hold. POSIX accepts every one of them, which is the point:
    # a key must be canonical on EVERY host, not merely legal on the one doing the reading.
    ("angle-open", "report<1>.json"),
    ("angle-close", "fabric/report>.json"),
    ("double-quote", 'report".json'),
    ("pipe", "report|json"),
    ("question-mark", "report?.json"),
    ("wildcard-star", "oracle/*.png"),
]

#: Punctuation a real package legitimately carries. The discriminating control for the grammar above:
#: without it, refusing every non-alphanumeric character would satisfy every arm of the census.
SAFE_PUNCTUATION_KEYS = [
    "oracle/worksheet/images/Measure Values Chart.png",
    "assets/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee_Minimal.twb",
    "fabric/Minimal.Report/definition/pages/page-ws-MeasureV051b1756/page.json",
    "handover/Sales (EMEA) [v2].json",
    "data/Region & Segment, 2026.csv",
    "notes/what's-new!.md",
    "notes/100%-coverage@final+1.md",
    "notes/a=b;c#d~e.md",
]


@pytest.mark.parametrize(
    ("name", "key"),
    UNSAFE_KEYS,
    ids=[name for name, _ in UNSAFE_KEYS],
)
def test_an_unsafe_key_is_reported_by_ORDINAL_and_never_echoed(tmp_path: Path, name: str, key: str) -> None:
    """The unsafe spelling is the one string that must not reach a shared verdict.

    It can be an absolute host path, a customer server name or a control-character payload aimed at
    whatever renders the message - and echoing it also re-introduces the ambiguity the code names.
    """
    package = build_package(tmp_path)
    write_manifest(package, files={key: "0" * 64})

    rows = [row for row in verify(package).findings if row.code == pfs.CODE_KEY_UNSAFE]

    assert rows, name
    assert all(row.path is None and row.ordinal is not None for row in rows), name


def test_an_unsafe_key_carrying_a_CUSTOMER_PATH_is_not_echoed(tmp_path: Path) -> None:
    """The concrete leak the ordinal rule prevents: a drive-rooted key IS a host path.

    ⚠️ Asserted on the whole rendered result rather than on one field, because a diagnostic that
    quotes the offending key "to be helpful" is exactly how this would come back.
    """
    package = build_package(tmp_path)
    write_manifest(package, files={"C:/Users/<name>/customer-secret-server/report.json": "0" * 64})

    result = verify(package)
    rendered = json.dumps(result.as_dict())

    assert pfs.CODE_KEY_UNSAFE in result.codes()
    assert "customer-secret-server" not in rendered
    assert "C:/Users" not in rendered


@pytest.mark.parametrize("key", ["README.md"])
def test_a_canonical_key_is_accepted(key: str) -> None:
    """The discriminating control: ordinary package paths, including spaces, must stay usable."""
    assert pfs.is_canonical_key(key)


@pytest.mark.parametrize("key", SAFE_PUNCTUATION_KEYS)
def test_legitimate_PUNCTUATION_survives_the_cross_host_grammar(key: str) -> None:
    """The control that keeps the character rule a rule rather than "refuse anything unusual".

    Every one of these is punctuation a real Tableau-derived package carries - a dashboard name with
    brackets and parentheses, an ampersand and a comma in a data file, an apostrophe, a percent sign.
    Without this arm, blacklisting the whole ASCII punctuation range would satisfy every refusal
    below while making the canonical package unverifiable.
    """
    assert pfs.is_canonical_key(key)


@pytest.mark.parametrize("key", SAFE_PUNCTUATION_KEYS[:1])
def test_a_package_declaring_legitimate_punctuation_still_verifies_clean(tmp_path: Path, key: str) -> None:
    """The same control end to end: the file is really written, declared and hashed.

    ⚠️ `is_canonical_key` alone would be a claim about the predicate, not about the verifier - and a
    key can be accepted by the predicate and still fall over in the walk-vs-declaration comparison
    (that is where a normalization difference would show up).
    """
    package = build_package(tmp_path, files={key: "content\n"})

    result = verify(package)

    assert result.is_clean, result.codes()
    assert result.files_declared == result.files_verified == 1


def test_a_non_string_key_is_refused_by_ordinal(tmp_path: Path) -> None:
    """JSON object keys are strings, but a hand-built map can be loaded from elsewhere."""
    package = build_package(tmp_path)
    (package / MARKER).write_text('{"contents": {"files": {"README.md": "x"}}}', encoding="utf-8")
    manifest = json.loads((package / MARKER).read_text(encoding="utf-8"))
    manifest["contents"]["files"] = {1: "0" * 64}

    usable, rows = pfs._classify_keys(manifest["contents"]["files"])  # pylint: disable=protected-access

    assert usable == {}
    assert [row.code for row in rows] == [pfs.CODE_KEY_NOT_STRING]
    assert rows[0].ordinal == 0 and rows[0].path is None


# --------------------------------------------------------------------------------------------
# Digests
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "digest",
    [
        "0" * 63,
        "0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef",
        "0123456789abcdefg123456789abcdef0123456789abcdef0123456789abcdef",
    ],
)
def test_a_digest_that_is_not_64_lowercase_hex_is_refused(tmp_path: Path, digest: str) -> None:
    """Exactly 64 lowercase hex, compared by equality: two spellings of one digest is one ambiguity."""
    package = build_package(tmp_path)
    files = {path: sha256_of(package / path) for path in ("README.md", "report.json")}
    files["README.md"] = digest
    write_manifest(package, files=files)

    result = verify(package)

    assert pfs.CODE_DIGEST_MALFORMED in result.codes()
    assert not result.is_clean


# --------------------------------------------------------------------------------------------
# Exact set equality
# --------------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------------
# The walk: reparse points, non-regular nodes, and never reading outside
# --------------------------------------------------------------------------------------------


def test_a_file_symlink_is_refused_and_its_target_is_never_read(tmp_path: Path) -> None:
    """A declared file could otherwise be satisfied by bytes living anywhere on the host."""
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"OUTSIDE\n")
    package = build_package(tmp_path)
    link_file(package / "aliased.bin", outside)

    result = verify(package)

    assert pfs.CODE_ENTRY_REPARSE in result.codes()
    assert not result.is_clean


def test_a_second_manifest_SPELLING_is_not_silently_absorbed(tmp_path: Path) -> None:
    """On a case-sensitive host `PACKAGE-MANIFEST.JSON` is an ordinary file, so it must be declared.

    Kills "exclude anything whose name case-folds to the marker": the self-exclusion is for the ONE
    entry that carries the map, and a second spelling beside it is a real, undeclared file.
    """
    package = build_package(tmp_path)
    original = (package / MARKER).read_text(encoding="utf-8")
    alias = package / MARKER.upper()
    alias.write_text("{}\n", encoding="utf-8")
    if (package / MARKER).read_text(encoding="utf-8") != original:
        # The alias write landed on the SAME file: there is only one manifest here, so there is no
        # second spelling to declare and nothing this control could prove.
        pytest.skip("this filesystem is case-insensitive: two manifest-name spellings are one file")

    result = verify(package)

    assert pfs.CODE_FILE_UNDECLARED in result.codes()
    assert not result.is_clean


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
def test_a_FIFO_is_neither_a_file_nor_a_directory(tmp_path: Path) -> None:
    """Opening a FIFO blocks forever, so "not regular" has to be decided before anything reads it."""
    package = build_package(tmp_path)
    os.mkfifo(package / "pipe")  # pylint: disable=no-member

    result = verify(package)

    assert pfs.CODE_ENTRY_NOT_REGULAR in result.codes()
    assert not result.is_clean


def test_the_verifier_never_uses_a_FOLLOWING_primitive(tmp_path: Path) -> None:
    """A structural guard, because the ORDER argument only holds if nothing dereferences at all.

    `resolve`, `rglob`, `glob`, `is_file`, `is_dir` and `exists` all follow links, so a single one of
    them anywhere in this module would decide a question about a path the caller never named. This
    reads the shipped source, so it also fails for a call added tomorrow.
    """
    source = (SCRIPTS / "package_filesystem.py").read_text(encoding="utf-8")
    code_lines = [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("#") and "``" not in line and "`" not in line
    ]
    body = "\n".join(code_lines)

    for forbidden in (".resolve(", ".rglob(", ".glob(", ".is_file(", ".is_dir(", ".exists(", "follow_symlinks=True"):
        assert forbidden not in body, f"{forbidden} dereferences and must not appear in the verifier"
    assert tmp_path is not None


# --------------------------------------------------------------------------------------------
# Boundary re-check and read order
# --------------------------------------------------------------------------------------------


def test_the_root_and_marker_are_lstat_ed_BEFORE_the_manifest_bytes_are_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-order sentinel: two no-follow lstats, then the open. Never the other way round."""
    package = build_package(tmp_path)
    # Classification happens BEFORE the recording starts, exactly as it does in the consumer: its own
    # lstats are the classifier's, and counting them here would say nothing about this module.
    classification = classify(package)
    calls: list[str] = []
    real_lstat = pfs.os.lstat
    real_read = Path.read_bytes

    def record_lstat(path, *args, **kwargs):
        calls.append(f"lstat:{Path(path).name}")
        return real_lstat(path, *args, **kwargs)

    def record_read(self, *args, **kwargs):
        calls.append(f"read:{self.name}")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(pfs.os, "lstat", record_lstat)
    monkeypatch.setattr(Path, "read_bytes", record_read)

    assert pfs.verify_package(package, classification).is_clean
    monkeypatch.undo()

    first_read = calls.index(f"read:{MARKER}")
    assert calls[:first_read] == [f"lstat:{package.name}", f"lstat:{MARKER}"]


def test_an_unassessable_marker_stops_BEFORE_the_manifest_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same order, proved from the failing direction: no lstat answer, no read."""
    package = build_package(tmp_path)
    classification = classify(package)
    real_lstat = pfs.os.lstat

    def deny(path, *args, **kwargs):
        if Path(path).name == MARKER:
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    def boom(*_args, **_kwargs):
        raise AssertionError("the manifest was opened before its lstat answered")

    monkeypatch.setattr(pfs.os, "lstat", deny)
    monkeypatch.setattr(Path, "read_bytes", boom)

    result = pfs.verify_package(package, classification)
    monkeypatch.undo()

    assert result.status == pfs.STATUS_UNASSESSABLE
    assert result.first_code == pfs.CODE_MARKER_UNREADABLE


def test_a_marker_DELETED_after_classification_is_caught_too(tmp_path: Path) -> None:
    """Same window, the other outcome: an absent marker is a replaced boundary, never a clean one."""
    package = build_package(tmp_path)
    classification = classify(package)

    (package / MARKER).unlink()

    assert pfs.verify_package(package, classification).first_code == pfs.CODE_MARKER_REPLACED


def test_an_unassessable_root_is_unassessable_not_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No exception-shaped success: "I could not tell" never joins "I checked and it is fine"."""
    package = build_package(tmp_path)
    classification = classify(package)
    real_lstat = pfs.os.lstat

    def deny(path, *args, **kwargs):
        if Path(path) == package:
            raise OSError(5, "I/O error")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(pfs.os, "lstat", deny)

    result = pfs.verify_package(package, classification)

    assert result.status == pfs.STATUS_UNASSESSABLE
    assert result.first_code == pfs.CODE_ROOT_UNREADABLE
    assert not result.is_clean


# --------------------------------------------------------------------------------------------
# Unassessable is never clean
# --------------------------------------------------------------------------------------------


def test_a_directory_that_cannot_be_listed_is_unassessable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory we cannot list may hold anything, including the file that was tampered with."""
    package = build_package(tmp_path)
    real_scandir = pfs.os.scandir

    def deny(path, *args, **kwargs):
        if Path(path).name == "oracle":
            raise PermissionError(13, "denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(pfs.os, "scandir", deny)

    result = verify(package)

    assert result.status == pfs.STATUS_UNASSESSABLE
    assert pfs.CODE_DIRECTORY_UNREADABLE in result.codes()
    assert not result.is_clean


def test_a_file_that_cannot_be_READ_is_unassessable_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable declared file has an unknown digest; counting it as verified is the fail-open."""
    package = build_package(tmp_path)
    monkeypatch.setattr(pfs, "_hash_file", lambda path: None if path.name == "README.md" else sha256_of(path))

    result = verify(package)

    assert result.status == pfs.STATUS_UNASSESSABLE
    assert pfs.CODE_FILE_UNREADABLE in result.codes()
    assert result.files_verified < result.files_declared


def test_an_entry_that_cannot_be_STAT_ed_is_unassessable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule one level down: an entry whose kind is unknown is not a verified regular file."""
    package = build_package(tmp_path)
    real_stat = os.DirEntry.stat

    def deny(self, *, follow_symlinks: bool = True):
        if self.name == "report.json":
            raise PermissionError(13, "denied")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os.DirEntry, "stat", deny, raising=False)

    result = verify(package)

    assert result.status == pfs.STATUS_UNASSESSABLE
    assert pfs.CODE_ENTRY_UNASSESSABLE in result.codes()


# --------------------------------------------------------------------------------------------
# Diagnostics carry no host path, no key text, no exception message
# --------------------------------------------------------------------------------------------


def test_no_finding_ever_carries_a_host_path_or_an_exception_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One assertion over EVERY failure mode this test file can reach in one package.

    The verdict is pasted into issues and shared with customers, and the supplied target is often an
    absolute path under a customer-named directory.
    """
    package = build_package(tmp_path / "customer-secret-server")
    (package / "extra.txt").write_text("x\n", encoding="utf-8")
    (package / "README.md").write_text("changed\n", encoding="utf-8")
    files = json.loads((package / MARKER).read_text(encoding="utf-8"))["contents"]["files"]
    files["../outside.txt"] = "0" * 64
    write_manifest(package, files=files)
    real_scandir = pfs.os.scandir

    def deny(path, *args, **kwargs):
        if Path(path).name == "oracle":
            raise PermissionError(13, "denied by C:\\\\Users\\\\<name>\\\\secret")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(pfs.os, "scandir", deny)

    rendered = json.dumps(verify(package).as_dict())

    assert "customer-secret-server" not in rendered
    assert str(tmp_path) not in rendered
    assert "denied" not in rendered
    assert "outside.txt" not in rendered
    assert "Permission" not in rendered


# --------------------------------------------------------------------------------------------
# Scope: what S1 deliberately does NOT judge
# --------------------------------------------------------------------------------------------


def test_every_code_has_generic_wording_and_stays_ascii() -> None:
    """These strings reach a Windows console, whose default code page cannot encode a warning glyph."""
    for code, detail in pfs._DETAILS.items():  # pylint: disable=protected-access
        assert detail == detail.encode("ascii", "ignore").decode("ascii"), code
        assert detail.strip(), code
        assert ":" not in code and " " not in code
