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
import stat
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


def test_the_manifest_excludes_itself_and_that_is_the_only_exclusion(tmp_path: Path) -> None:
    """`package-manifest.json` carries the map, so it is never one of the mapped files.

    Kills "exclude anything that looks like metadata": every other file is either declared or a
    finding, which is what makes an extra README or a stray oracle render visible.
    """
    package = build_package(tmp_path)
    declared = json.loads((package / MARKER).read_text(encoding="utf-8"))["contents"]["files"]

    assert MARKER not in declared
    assert verify(package).is_clean

    (package / "notes.txt").write_text("added after packaging\n", encoding="utf-8")

    assert not verify(package).is_clean


def test_an_empty_directory_is_reported_but_is_not_a_file(tmp_path: Path) -> None:
    """A directory holds no bytes, so it cannot be declared - and must not read as an extra file."""
    package = build_package(tmp_path)
    (package / "data").mkdir()

    _files, rows, empty = pfs.walk_package(package)

    assert rows == []
    assert "data" in empty
    assert verify(package).is_clean


# --------------------------------------------------------------------------------------------
# Strict JSON
# --------------------------------------------------------------------------------------------


def test_a_manifest_that_is_not_json_is_refused(tmp_path: Path) -> None:
    """A truncated or hand-edited manifest describes nothing, so nothing can be established."""
    package = build_package(tmp_path)
    (package / MARKER).write_text('{"contents": {"files": {', encoding="utf-8")

    result = verify(package)

    assert result.status == pfs.STATUS_FINDINGS
    assert result.first_code == pfs.CODE_MANIFEST_NOT_JSON


@pytest.mark.parametrize("payload", ["[]", '"a string"', "42", "null", "true"])
def test_a_manifest_whose_top_level_is_not_an_object_is_refused(tmp_path: Path, payload: str) -> None:
    """`contents` can only be reached through an object; a list of anything is not a manifest."""
    package = build_package(tmp_path)
    (package / MARKER).write_text(payload, encoding="utf-8")

    assert verify(package).first_code == pfs.CODE_MANIFEST_NOT_OBJECT


def test_a_manifest_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    """Undecodable bytes are not a manifest, and must not surface as a decoder traceback."""
    package = build_package(tmp_path)
    (package / MARKER).write_bytes(b'{"contents": {"files": {"\xff\xfe": ""}}}')

    assert verify(package).first_code == pfs.CODE_MANIFEST_NOT_UTF8


def test_a_duplicate_key_at_the_top_level_is_refused(tmp_path: Path) -> None:
    """`json.loads` keeps the LAST value silently, so a repeated key means two different manifests."""
    package = build_package(tmp_path)
    (package / MARKER).write_text('{"contents": {"files": {"README.md": "x"}}, "contents": {"files": {}}}', "utf-8")

    result = verify(package)

    assert result.status == pfs.STATUS_FINDINGS
    assert result.first_code == pfs.CODE_MANIFEST_DUPLICATE_KEY


def test_a_duplicate_key_NESTED_deep_in_the_manifest_is_refused_too(tmp_path: Path) -> None:
    """Kills a top-level-only duplicate check: the hook must fire at EVERY object depth.

    The duplicate here is four levels down and inside a list, which is exactly where a check written
    against the shape someone happened to picture would not be looking.
    """
    package = build_package(tmp_path)
    (package / MARKER).write_text(
        '{"oracle": {"objects": [{"name": "main", "name": "other"}]}, "contents": {"files": {}}}',
        encoding="utf-8",
    )

    assert verify(package).first_code == pfs.CODE_MANIFEST_DUPLICATE_KEY


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


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_json_constant_is_refused(tmp_path: Path, literal: str) -> None:
    """Python accepts these; JSON does not define them, so two readers disagree about the document.

    ⚠️ The literal is placed in a field S1 does not otherwise read (`notes`), on purpose. Put it in
    a digest and the type check would catch it, and this control would then prove nothing about
    `parse_constant`.
    """
    package = build_package(tmp_path)
    (package / MARKER).write_text(f'{{"notes": [{literal}], "contents": {{"files": {{}}}}}}', encoding="utf-8")

    result = verify(package)

    assert result.status == pfs.STATUS_FINDINGS
    assert result.first_code == pfs.CODE_MANIFEST_NON_FINITE


def test_a_number_that_OVERFLOWS_to_infinity_is_refused_through_the_other_door(tmp_path: Path) -> None:
    """`1e999` is an ordinary JSON token, so it never reaches `parse_constant` - same broken value."""
    package = build_package(tmp_path)
    (package / MARKER).write_text('{"notes": [1e999], "contents": {"files": {}}}', encoding="utf-8")

    assert verify(package).first_code == pfs.CODE_MANIFEST_NON_FINITE


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ('{"unit": "Minimal"}', pfs.CODE_CONTENTS_MISSING),
        ('{"contents": []}', pfs.CODE_CONTENTS_NOT_OBJECT),
        ('{"contents": "files"}', pfs.CODE_CONTENTS_NOT_OBJECT),
        ('{"contents": null}', pfs.CODE_CONTENTS_NOT_OBJECT),
        ('{"contents": {}}', pfs.CODE_FILES_MISSING),
        ('{"contents": {"files": []}}', pfs.CODE_FILES_NOT_OBJECT),
        ('{"contents": {"files": "README.md"}}', pfs.CODE_FILES_NOT_OBJECT),
        ('{"contents": {"files": null}}', pfs.CODE_FILES_NOT_OBJECT),
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
    ("backslash-in-name", "a\\b"),
    ("posix-absolute", "/etc/passwd"),
    ("unc", "//server/share/file.txt"),
    ("drive-rooted", "C:/Windows/system.ini"),
    ("drive-relative", "C:report.json"),
    ("drive-rooted-backslash", "C:\\Windows\\system.ini"),
    ("alternate-data-stream", "report.json:hidden"),
    ("empty", ""),
    ("dot", "."),
    ("dotdot", ".."),
    ("dotdot-segment", "fabric/../../outside.txt"),
    ("dot-segment", "fabric/./report.json"),
    ("double-slash", "fabric//report.json"),
    ("trailing-slash", "fabric/"),
    ("leading-slash-relative", "/fabric/report.json"),
    ("nul", "report\x00.json"),
    ("control-char", "report\x01.json"),
    ("newline", "report\n.json"),
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
]


@pytest.mark.parametrize(("name", "key"), UNSAFE_KEYS, ids=[name for name, _ in UNSAFE_KEYS])
def test_an_unsafe_declared_key_is_refused(tmp_path: Path, name: str, key: str) -> None:
    """Every spelling that is absolute, escaping, ambiguous or device-reserved is non-clean.

    Each one makes the manifest mean two things on two hosts, or reach outside the package. The
    parametrization is the census: one arm per class, so a guard that covers three of them cannot
    look complete.
    """
    package = build_package(tmp_path)
    write_manifest(package, files={key: "0" * 64})

    result = verify(package)

    assert not result.is_clean, name
    assert pfs.CODE_KEY_UNSAFE in result.codes(), name


@pytest.mark.parametrize(("name", "key"), UNSAFE_KEYS, ids=[name for name, _ in UNSAFE_KEYS])
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
    write_manifest(package, files={"C:/Users/someone/customer-secret-server/report.json": "0" * 64})

    result = verify(package)
    rendered = json.dumps(result.as_dict())

    assert pfs.CODE_KEY_UNSAFE in result.codes()
    assert "customer-secret-server" not in rendered
    assert "C:/Users" not in rendered


@pytest.mark.parametrize(
    "key",
    ["README.md", "fabric/Minimal.Report/definition.pbir", "assets/aaaa_My Workbook.twb", "a/b/c/d.json"],
)
def test_a_canonical_key_is_accepted(key: str) -> None:
    """The discriminating control: ordinary package paths, including spaces, must stay usable."""
    assert pfs.is_canonical_key(key)


def test_two_keys_that_differ_only_by_case_cannot_describe_distinct_bytes(tmp_path: Path) -> None:
    """On a case-insensitive host they are ONE file, so the manifest cannot say which bytes it meant."""
    package = build_package(tmp_path)
    write_manifest(package, files={"README.md": "0" * 64, "readme.MD": "1" * 64})

    result = verify(package)

    assert pfs.CODE_KEY_COLLISION in result.codes()
    assert not result.is_clean


def test_two_keys_that_differ_only_by_a_trailing_dot_collide_too(tmp_path: Path) -> None:
    """Windows eats a trailing dot, so `report.json.` and `report.json` are the same file there.

    The trailing spelling is refused as unsafe in its own right; this asserts the pair is ALSO not
    quietly accepted as two distinct declarations.
    """
    package = build_package(tmp_path)
    write_manifest(package, files={"report.json": "0" * 64, "report.json.": "1" * 64})

    assert not verify(package).is_clean


@pytest.mark.parametrize("spelling", [MARKER, MARKER.upper(), "Package-Manifest.Json", "package-manifest.json."])
def test_a_key_naming_the_manifest_itself_is_refused(tmp_path: Path, spelling: str) -> None:
    """The manifest carries the map; a self-declaration is either a lie or an alias for one.

    ⚠️ Every case spelling matters: on a case-insensitive host `PACKAGE-MANIFEST.JSON` IS the marker,
    so accepting it would let the map claim to describe itself and make the excluded-file rule
    ambiguous.
    """
    package = build_package(tmp_path)
    files = {"README.md": sha256_of(package / "README.md"), spelling: "0" * 64}
    write_manifest(package, files=files)

    result = verify(package)

    assert not result.is_clean
    assert {pfs.CODE_KEY_ALIASES_MARKER, pfs.CODE_KEY_UNSAFE} & set(result.codes())


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
        "0" * 65,
        "",
        "not-a-digest",
        "0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef",
        "0123456789abcdefg123456789abcdef0123456789abcdef0123456789abcdef",
        " 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde",
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


def test_a_non_string_digest_is_refused(tmp_path: Path) -> None:
    """`null` or a number is not a digest, and must not be coerced into one."""
    package = build_package(tmp_path)
    write_manifest(package, files={"README.md": None})

    assert pfs.CODE_DIGEST_NOT_STRING in verify(package).codes()


def test_a_changed_byte_is_caught_because_every_declared_file_is_REHASHED(tmp_path: Path) -> None:
    """Kills "verify the file exists": presence is not integrity, and this is the whole claim."""
    package = build_package(tmp_path)
    assert verify(package).is_clean

    (package / "README.md").write_text("how to open this package!\n", encoding="utf-8")

    result = verify(package)

    assert result.status == pfs.STATUS_FINDINGS
    assert [row.code for row in result.findings] == [pfs.CODE_DIGEST_MISMATCH]
    assert result.findings[0].path == "README.md"


def test_two_files_that_swapped_contents_are_both_caught(tmp_path: Path) -> None:
    """A same-size, same-name-set swap keeps every count identical - only rehashing sees it."""
    package = build_package(tmp_path)
    first, second = package / "one.txt", package / "two.txt"
    first.write_text("AAAA\n", encoding="utf-8")
    second.write_text("BBBB\n", encoding="utf-8")
    write_manifest(package)
    assert verify(package).is_clean

    first.write_text("BBBB\n", encoding="utf-8")
    second.write_text("AAAA\n", encoding="utf-8")

    result = verify(package)

    assert sorted(row.path for row in result.findings) == ["one.txt", "two.txt"]
    assert {row.code for row in result.findings} == {pfs.CODE_DIGEST_MISMATCH}


def test_a_correct_digest_recorded_in_UPPERCASE_is_still_refused(tmp_path: Path) -> None:
    """The bytes match; the declaration does not. Case-insensitive comparison is a second spelling."""
    package = build_package(tmp_path)
    files = {
        str(path.relative_to(package).as_posix()): sha256_of(path)
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.name != MARKER
    }
    files["README.md"] = files["README.md"].upper()
    write_manifest(package, files=files)

    assert pfs.CODE_DIGEST_MALFORMED in verify(package).codes()


# --------------------------------------------------------------------------------------------
# Exact set equality
# --------------------------------------------------------------------------------------------


def test_an_extra_file_is_refused(tmp_path: Path) -> None:
    """Anything the manifest does not account for is foreign to this package's composition."""
    package = build_package(tmp_path)
    (package / "oracle" / "stray.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    result = verify(package)

    assert result.status == pfs.STATUS_FINDINGS
    assert [row.code for row in result.findings] == [pfs.CODE_FILE_UNDECLARED]
    assert result.findings[0].path == "oracle/stray.png"


def test_a_new_file_under_fabric_is_an_extra_file_at_ENTRY(tmp_path: Path) -> None:
    """The dispatched working lifecycle is a LATER slice; at entry a package is byte-exact.

    ⚠️ Deliberately explicit rather than left implicit: `fabric/**` becomes legitimately mutable once
    an agent has been dispatched, and reading that backwards into the entry gate would make every
    half-finished unit look pristine.
    """
    package = build_package(tmp_path)
    (package / "fabric" / "Minimal.Report" / "added.json").write_text("{}", encoding="utf-8")

    result = verify(package)

    assert pfs.CODE_FILE_UNDECLARED in result.codes()
    assert result.findings[0].path == "fabric/Minimal.Report/added.json"


def test_a_deleted_file_is_refused(tmp_path: Path) -> None:
    """A declared file that is not there means the package is no longer what it says it is."""
    package = build_package(tmp_path)
    (package / "report.json").unlink()

    result = verify(package)

    assert [row.code for row in result.findings] == [pfs.CODE_FILE_MISSING]
    assert result.findings[0].path == "report.json"


def test_a_declared_path_that_is_a_DIRECTORY_is_missing_not_satisfied(tmp_path: Path) -> None:
    """A directory holds no bytes. "The name exists" is not "the file is there"."""
    package = build_package(tmp_path)
    (package / "report.json").unlink()
    (package / "report.json").mkdir()

    result = verify(package)

    assert pfs.CODE_FILE_MISSING in result.codes()


def test_both_directions_are_reported_at_once(tmp_path: Path) -> None:
    """Set equality is two inclusions; reporting one would let the other stay invisible."""
    package = build_package(tmp_path)
    (package / "report.json").unlink()
    (package / "extra.txt").write_text("x\n", encoding="utf-8")

    codes = set(verify(package).codes())

    assert codes == {pfs.CODE_FILE_UNDECLARED, pfs.CODE_FILE_MISSING}


# --------------------------------------------------------------------------------------------
# The walk: reparse points, non-regular nodes, and never reading outside
# --------------------------------------------------------------------------------------------


def test_a_directory_junction_is_a_finding_and_a_DEAD_END(tmp_path: Path) -> None:
    """The bytes on the far side of a junction are not this package's bytes.

    Two claims, and the second is the load-bearing one: the junction is reported, AND nothing behind
    it is listed. A verifier that descends first and judges afterwards has already read outside.
    """
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "secret.txt").write_text("CUSTOMER-SECRET\n", encoding="utf-8")
    (outside / "nested" / "deep.txt").write_text("ALSO-SECRET\n", encoding="utf-8")
    package = build_package(tmp_path)
    link_directory(package / "linked", outside)

    files, rows, _empty = pfs.walk_package(package)
    result = verify(package)

    assert [row.code for row in rows] == [pfs.CODE_ENTRY_REPARSE]
    assert rows[0].path == "linked"
    assert not any(path.startswith("linked/") for path in files)
    assert pfs.CODE_ENTRY_REPARSE in result.codes()
    assert "secret" not in json.dumps(result.as_dict()).lower()


def test_a_file_symlink_is_refused_and_its_target_is_never_read(tmp_path: Path) -> None:
    """A declared file could otherwise be satisfied by bytes living anywhere on the host."""
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"OUTSIDE\n")
    package = build_package(tmp_path)
    link_file(package / "aliased.bin", outside)

    result = verify(package)

    assert pfs.CODE_ENTRY_REPARSE in result.codes()
    assert not result.is_clean


def test_a_declared_file_satisfied_by_a_SYMLINK_to_matching_bytes_is_still_refused(tmp_path: Path) -> None:
    """The strongest reparse control: the hash would have MATCHED had the link been followed.

    Following it would mean the package's contents are decided by a file the package does not carry,
    which is the whole reason a boundary exists.
    """
    package = build_package(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("linked bytes\n", encoding="utf-8")
    (package / "linked.txt").write_text("linked bytes\n", encoding="utf-8")
    write_manifest(package)
    assert verify(package).is_clean
    (package / "linked.txt").unlink()
    link_file(package / "linked.txt", outside)

    result = verify(package)

    assert pfs.CODE_ENTRY_REPARSE in result.codes()
    assert pfs.CODE_FILE_MISSING in result.codes()


def test_a_reparse_ATTRIBUTE_on_a_plain_file_is_refused_on_every_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host-independent half of the file-symlink control.

    ⚠️ Creating a file symlink needs elevation on Windows and the test above SKIPS there, so without
    this arm the "a linked file is not this package's bytes" rule would be unproven on the platform
    this toolkit actually runs on. The *entry* is forged - in whichever form this host can express,
    the Windows `FILE_ATTRIBUTE_REPARSE_POINT` bit or the POSIX link mode - and the classification is
    the production predicate's own. Both halves of `is_reparse_entry` are therefore exercised
    somewhere: this test covers the one its host has, and the real-symlink test above covers the
    other wherever the account may create one.
    """
    package = build_package(tmp_path)
    real_stat = os.DirEntry.stat

    def fake_stat(self, *, follow_symlinks: bool = True):
        info = real_stat(self, follow_symlinks=follow_symlinks)
        if self.name != "README.md":
            return info
        if hasattr(info, "st_file_attributes"):
            extras = {
                "st_file_attributes": info.st_file_attributes | bundle_corpus.FILE_ATTRIBUTE_REPARSE_POINT,
                "st_reparse_tag": getattr(info, "st_reparse_tag", 0),
            }
            return os.stat_result(list(info), extras)
        # POSIX has no attribute word, and an unknown key in the extras dict is not readable back as
        # an attribute there - so the link is expressed the way POSIX expresses one, in the mode.
        fields = list(info)
        fields[0] = (info.st_mode & ~stat.S_IFMT(info.st_mode)) | stat.S_IFLNK
        return os.stat_result(fields)

    monkeypatch.setattr(os.DirEntry, "stat", fake_stat, raising=False)

    result = verify(package)

    assert pfs.CODE_ENTRY_REPARSE in result.codes()
    # Reported AND never counted as a verified file: a reparse entry is a dead end, not a fallback.
    assert pfs.CODE_FILE_MISSING in result.codes()
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


def test_a_non_regular_node_is_refused_on_every_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The host-independent half of the FIFO control: Windows cannot make one, and still must refuse.

    The mode is forged rather than the verdict, so the code under test does its own classification.
    """
    package = build_package(tmp_path)
    (package / "device").write_text("pretend\n", encoding="utf-8")
    write_manifest(package)
    real_stat = os.DirEntry.stat

    def fake_stat(self, *, follow_symlinks: bool = True):
        info = real_stat(self, follow_symlinks=follow_symlinks)
        if self.name == "device":
            fields = list(info)
            fields[0] = (info.st_mode & ~stat.S_IFMT(info.st_mode)) | stat.S_IFCHR
            # The platform extras have to be carried over explicitly: a `stat_result` rebuilt from
            # the 10-tuple alone reports `st_file_attributes` as None on Windows, which is a property
            # of this forgery rather than of any real entry.
            extras = {
                name: getattr(info, name) for name in ("st_file_attributes", "st_reparse_tag") if hasattr(info, name)
            }
            return os.stat_result(fields, extras)
        return info

    monkeypatch.setattr(os.DirEntry, "stat", fake_stat, raising=False)

    result = verify(package)

    assert pfs.CODE_ENTRY_NOT_REGULAR in result.codes()


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


def test_a_marker_REPLACED_after_classification_is_caught_by_the_defence_in_depth_lstat(
    tmp_path: Path,
) -> None:
    """The classifier's answer is a value that travelled; the bytes are opened here.

    A marker swapped for a directory between the two steps is exactly the accident this slice exists
    to notice, and noticing costs two syscalls.
    """
    package = build_package(tmp_path)
    classification = classify(package)
    assert classification.declares_self_contained

    (package / MARKER).unlink()
    (package / MARKER).mkdir()

    result = pfs.verify_package(package, classification)

    assert result.first_code == pfs.CODE_MARKER_REPLACED
    assert not result.is_clean


def test_a_marker_DELETED_after_classification_is_caught_too(tmp_path: Path) -> None:
    """Same window, the other outcome: an absent marker is a replaced boundary, never a clean one."""
    package = build_package(tmp_path)
    classification = classify(package)

    (package / MARKER).unlink()

    assert pfs.verify_package(package, classification).first_code == pfs.CODE_MARKER_REPLACED


def test_a_root_REPLACED_after_classification_is_caught(tmp_path: Path) -> None:
    """A root that is no longer a plain directory cannot be the package that was classified."""
    package = build_package(tmp_path)
    classification = classify(package)

    for path in sorted(package.rglob("*"), key=lambda item: len(str(item)), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    package.rmdir()
    package.write_text("not a directory\n", encoding="utf-8")

    assert pfs.verify_package(package, classification).first_code == pfs.CODE_ROOT_REPLACED


def test_a_root_that_became_a_LINK_after_classification_is_caught(tmp_path: Path) -> None:
    """Defense in depth against the alias the classifier already refuses, at the moment of reading."""
    package = build_package(tmp_path)
    classification = classify(package)
    real = tmp_path / "real"
    package.rename(real)
    link_directory(package, real)

    assert pfs.verify_package(package, classification).first_code == pfs.CODE_ROOT_REPLACED


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


def test_a_target_that_does_not_DECLARE_a_package_is_refused_rather_than_verified(tmp_path: Path) -> None:
    """ "There is nothing to check here" and "I checked" must not share an answer.

    The verifier consumes the classification the caller already computed; it never reclassifies, so
    a caller that hands it an ordinary bundle gets a refusal instead of a vacuous clean.
    """
    ordinary = tmp_path / "bundle"
    (ordinary / "pbip").mkdir(parents=True)
    classification = bundle_corpus.classify_target(ordinary)

    result = pfs.verify_package(ordinary, classification)

    assert classification.kind == bundle_corpus.TARGET_ORDINARY
    assert result.first_code == pfs.CODE_NOT_A_DECLARED_PACKAGE
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


def test_an_unassessable_result_is_never_clean_even_when_NOTHING_else_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-open this whole state exists to prevent, isolated so nothing else can mask it.

    ⚠️ The unreadable directory here is EMPTY, so the manifest is otherwise satisfied and there are
    zero findings. That is the point: with a declared file inside it, a "not clean" assertion would
    hold for the missing file instead, and folding unassessable into clean would go unnoticed.
    """
    package = build_package(tmp_path)
    (package / "empty").mkdir()
    real_scandir = pfs.os.scandir

    def deny(path, *args, **kwargs):
        if Path(path).name == "empty":
            raise PermissionError(13, "denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(pfs.os, "scandir", deny)

    result = verify(package)

    assert result.findings == ()
    assert result.status == pfs.STATUS_UNASSESSABLE
    assert not result.is_clean
    assert [row.code for row in result.unassessable] == [pfs.CODE_DIRECTORY_UNREADABLE]


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
            raise PermissionError(13, "denied by C:\\Users\\someone\\secret")
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


def test_a_manifest_with_NO_role_declarations_is_still_S1_clean(tmp_path: Path) -> None:
    """Role-graph verification is a later slice, and pulling it in here would be scope creep.

    ⚠️ This is a scope control, not an endorsement: a package with no `artifacts.asset` is a real
    problem - it is simply not THIS invariant's problem, and the independent audit's
    `missing_asset_role` control is expected to stay exit 0 at S1 for exactly this reason.
    """
    package = build_package(tmp_path)
    write_manifest(package, extra={"artifacts": {}})

    assert verify(package).is_clean


def test_a_manifest_whose_IDENTITY_contradicts_itself_is_still_S1_clean(tmp_path: Path) -> None:
    """Identity/LUID reconciliation is a later slice too, with its own evidence and its own oracle."""
    package = build_package(tmp_path)
    write_manifest(
        package,
        extra={
            "workbook_identity": {"luid": "11111111-1111-1111-1111-111111111111", "match": "sha256"},
            "artifacts": {"asset": "assets/22222222-2222-2222-2222-222222222222_Minimal.twb"},
        },
    )

    assert verify(package).is_clean


def test_every_code_has_generic_wording_and_stays_ascii() -> None:
    """These strings reach a Windows console, whose default code page cannot encode a warning glyph."""
    for code, detail in pfs._DETAILS.items():  # pylint: disable=protected-access
        assert detail == detail.encode("ascii", "ignore").decode("ascii"), code
        assert detail.strip(), code
        assert ":" not in code and " " not in code
