"""Direct tests for the package filesystem/manifest integrity precondition (#562, slice 1).

Scope, deliberately: does the manifest parse, and is the file namespace/bytes it declares EXACTLY
the package on disk. Nothing here asserts anything about workbook/datasource roles, asset identity,
LUIDs or oracle semantics - those are a later slice and are NOT covered by these tests.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position
import package_filesystem as pfs  # noqa: E402  # pylint: disable=wrong-import-position

WINDOWS = os.name == "nt"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(root: Path, relative: str, data: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def build_package(
    tmp_path: Path,
    *,
    files: dict[str, bytes] | None = None,
    unit: str = "Minimal",
    manifest: object | None = None,
    manifest_text: str | None = None,
) -> Path:
    """A package-shaped target under ``packages/`` whose manifest declares exactly ``files``."""
    files = {"README.md": b"map\n", "report.json": b"{}\n"} if files is None else files
    root = tmp_path / "packages" / unit
    root.mkdir(parents=True)
    for relative, data in files.items():
        _write(root, relative, data)
    if manifest_text is None:
        payload = (
            {"unit": unit, "contents": {"files": {key: _digest(value) for key, value in files.items()}}}
            if manifest is None
            else manifest
        )
        manifest_text = json.dumps(payload, indent=2)
    (root / pfs.PACKAGE_MARKER).write_text(manifest_text, encoding="utf-8")
    return root


def codes(result: pfs.PackageFilesystem) -> list[str]:
    return [finding.code for finding in result.findings]


class _ReparseStatus:  # pylint: disable=too-few-public-methods
    """A real `stat_result` with the Windows reparse-point attribute forced on.

    The symlink controls cannot run on an unprivileged Windows account (`WinError 1314`), so the
    classifier is also driven directly: a junction and a file symlink reach it through this same
    attribute bit, and `S_ISLNK` is 0 for a junction.
    """

    def __init__(self, status: os.stat_result) -> None:
        self.st_mode = status.st_mode
        self.st_file_attributes = getattr(status, "st_file_attributes", 0) | 0x400


def _explode(reason: str):  # type: ignore[no-untyped-def]
    """A stand-in that fails the test if it is ever called."""

    def refuse(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(reason)

    return refuse


# --------------------------------------------------------------------------------------- positives


def test_canonical_package_is_clean_and_counts_every_declared_file(tmp_path: Path) -> None:
    """Kills: a verifier that can never return clean, which would make the gate useless."""
    root = build_package(tmp_path, files={"README.md": b"a\n", "fabric/x.json": b"{}\n"})

    result = pfs.verify_package_filesystem(root)

    assert result.state == pfs.STATE_CLEAN
    assert result.clean is True
    assert result.findings == ()
    assert (result.declared_files, result.verified_files) == (2, 2)


def test_empty_directory_is_reported_but_never_becomes_a_declared_file(tmp_path: Path) -> None:
    """An empty dir has no bytes to declare, so it must not read as a missing file."""
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    (root / "oracle" / "images").mkdir(parents=True)

    result = pfs.verify_package_filesystem(root)

    assert result.clean is True
    assert "oracle/images" in result.empty_directories


def test_manifest_is_excluded_from_the_actual_set_and_may_not_declare_itself(tmp_path: Path) -> None:
    """The producer writes the map last, so the manifest cannot carry its own digest."""
    clean = pfs.verify_package_filesystem(build_package(tmp_path / "a", files={"README.md": b"a\n"}))
    assert clean.clean is True

    root = build_package(
        tmp_path / "b",
        files={"README.md": b"a\n"},
        manifest={"contents": {"files": {"README.md": _digest(b"a\n"), pfs.PACKAGE_MARKER: "0" * 64}}},
    )
    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["declared-manifest-self"]
    assert result.clean is False


# ------------------------------------------------------------------------------------ manifest shape


def test_missing_manifest_is_unassessable_not_clean(tmp_path: Path) -> None:
    """Kills: treating "nothing declared" as "nothing wrong" - the legacy-fallback control."""
    root = tmp_path / "packages" / "Minimal"
    (root / "fabric").mkdir(parents=True)

    result = pfs.verify_package_filesystem(root)

    assert result.state == pfs.STATE_UNASSESSABLE
    assert codes(result) == ["manifest-missing"]


def test_malformed_json_manifest_is_non_clean(tmp_path: Path) -> None:
    root = build_package(tmp_path, manifest_text='{"contents": {"files": {')

    assert codes(pfs.verify_package_filesystem(root)) == ["manifest-not-json"]


def test_non_object_manifest_is_non_clean(tmp_path: Path) -> None:
    root = build_package(tmp_path, manifest_text='["contents"]')

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["manifest-not-object"]
    assert "list" in result.findings[0].detail


def test_duplicate_top_level_key_is_rejected(tmp_path: Path) -> None:
    """Kills: plain json.loads, which silently keeps the LAST value for a repeated key."""
    root = build_package(tmp_path, manifest_text='{"kind": "workbook", "kind": "datasource", "contents": {}}')

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["manifest-duplicate-key"]
    assert "#2" in result.findings[0].detail
    assert "kind" not in result.findings[0].detail


def test_a_duplicated_key_is_never_echoed_into_a_shareable_finding(tmp_path: Path) -> None:
    """`contents.files` is keyed BY PATH, so the duplicated key can itself be a customer path."""
    secret = "D:/Payroll/Customer Secret.twbx"
    digest = _digest(b"a\n")
    root = build_package(
        tmp_path,
        files={"README.md": b"a\n"},
        manifest_text=(
            '{"contents": {"files": {"README.md": "%s", "%s": "%s", "%s": "%s"}}}'
            % (digest, secret, digest, secret, digest)
        ),
    )

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["manifest-duplicate-key"]
    rendered = result.summary(limit=10) + " | " + " | ".join(f.describe() for f in result.findings)
    assert secret not in rendered
    assert "Payroll" not in rendered
    assert "Customer Secret" not in rendered
    assert "D:" not in rendered


@pytest.mark.parametrize(
    "manifest_text",
    [
        '{"contents": {"files": {}}, "score": NaN}',
        '{"contents": {"files": {}}, "score": Infinity}',
        '{"contents": {"files": {}}, "score": -Infinity}',
        '{"contents": {"files": {}}, "nested": {"deep": [1, {"x": NaN}]}}',
    ],
)
def test_non_standard_json_constants_are_refused_anywhere_in_the_tree(tmp_path: Path, manifest_text: str) -> None:
    """Python's decoder accepts NaN/Infinity by default; a strict reader does not, so two readers
    would disagree about what the package declared - the duplicate-key ambiguity in another suit."""
    root = build_package(tmp_path, files={"README.md": b"a\n"}, manifest_text=manifest_text)

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["manifest-not-json"]
    assert result.clean is False


def test_duplicate_key_is_rejected_at_every_depth(tmp_path: Path) -> None:
    """A repeated key inside contents.files is the one that changes which byte is required."""
    digest = _digest(b"a\n")
    root = build_package(
        tmp_path,
        files={"README.md": b"a\n"},
        manifest_text=('{"contents": {"files": {"README.md": "%s", "README.md": "%s"}}}' % (digest, "0" * 64)),
    )

    assert codes(pfs.verify_package_filesystem(root)) == ["manifest-duplicate-key"]


@pytest.mark.parametrize("contents", [None, [], "files", 3])
def test_contents_must_be_an_object(tmp_path: Path, contents: object) -> None:
    payload: dict[str, object] = {"unit": "Minimal"}
    if contents is not None:
        payload["contents"] = contents
    root = build_package(tmp_path, files={"README.md": b"a\n"}, manifest=payload)

    assert codes(pfs.verify_package_filesystem(root)) == ["contents-not-object"]


@pytest.mark.parametrize("files", [None, [], "README.md", 3])
def test_contents_files_must_be_a_mapping(tmp_path: Path, files: object) -> None:
    contents: dict[str, object] = {}
    if files is not None:
        contents["files"] = files
    root = build_package(tmp_path, files={"README.md": b"a\n"}, manifest={"contents": contents})

    assert codes(pfs.verify_package_filesystem(root)) == ["contents-files-not-object"]


# ------------------------------------------------------------------------------------ declared paths


@pytest.mark.parametrize(
    "key",
    [
        "",
        ".",
        "..",
        "../escape.txt",
        "a/../../escape.txt",
        "a//b.txt",
        "./a.txt",
        "fabric\\model.tmdl",
        "\\\\host\\share\\x.txt",
        "/etc/passwd",
        "//host/share/x.txt",
        "C:/Payroll/secret.txt",
        "C:secret.txt",
        "c:/x.txt",
        "a\x00b.txt",
        "a\nb.txt",
        "a\x7f.txt",
        "stream.txt:hidden",
        "trailing./x.txt",
        "trailing /x.txt",
        "x.txt.",
        "x.txt ",
        "CON",
        "nul.txt",
        "oracle/COM1.png",
        "LPT9.dat",
    ],
)
def test_unsafe_declared_paths_are_refused_lexically(key: str) -> None:
    """Every flavour is judged on BOTH platforms: the packaging host is not the reading host."""
    assert pfs.declared_path_problem(key) is not None


@pytest.mark.parametrize("key", ["README.md", "fabric/Minimal.Report/definition.pbir", "oracle/a b/c.png", "a.b.c"])
def test_canonical_posix_relative_paths_are_accepted(key: str) -> None:
    assert pfs.declared_path_problem(key) is None


def test_non_string_declared_path_is_refused() -> None:
    assert pfs.declared_path_problem(3) is not None
    assert pfs.declared_path_problem(None) is not None


def test_unsafe_declared_key_is_named_by_ordinal_and_never_echoed(tmp_path: Path) -> None:
    """An unsafe key can BE an absolute customer path; these findings are printed into verdicts."""
    secret = "D:/Payroll/Customer Secret.twbx"
    root = build_package(
        tmp_path,
        files={"README.md": b"a\n"},
        manifest={"contents": {"files": {"README.md": _digest(b"a\n"), secret: "0" * 64}}},
    )

    result = pfs.verify_package_filesystem(root)

    assert "declared-path-unsafe" in codes(result)
    rendered = "; ".join(finding.describe() for finding in result.findings)
    assert secret not in rendered
    assert "Customer Secret" not in rendered
    assert "#2" in rendered


def test_windows_case_alias_is_refused_before_any_filesystem_access(tmp_path: Path) -> None:
    """Kills: removing alias normalization. Two keys cannot name two files on a Windows reader."""
    root = build_package(
        tmp_path,
        files={"README.md": b"a\n"},
        manifest={"contents": {"files": {"README.md": _digest(b"a\n"), "readme.MD": _digest(b"a\n")}}},
    )

    result = pfs.verify_package_filesystem(root)

    assert "declared-path-alias" in codes(result)
    assert result.clean is False


def test_alias_key_folds_case_and_windows_trailing_dot_space() -> None:
    assert pfs.alias_key("README.md") == pfs.alias_key("readme.MD")
    assert pfs.alias_key("a/b.txt") != pfs.alias_key("a/c.txt")


@pytest.mark.parametrize("digest", ["0" * 63, "0" * 65, "Z" * 64, "A" * 64, "", 7, None])
def test_declared_digest_must_be_lowercase_64_hex(tmp_path: Path, digest: object) -> None:
    root = build_package(
        tmp_path,
        files={"README.md": b"a\n"},
        manifest={"contents": {"files": {"README.md": digest}}},
    )

    assert "declared-digest-invalid" in codes(pfs.verify_package_filesystem(root))


# ------------------------------------------------------------------------------- namespace and bytes


def test_deleted_declared_file_blocks(tmp_path: Path) -> None:
    root = build_package(tmp_path, files={"README.md": b"a\n", "engine-output-receipt.json": b"{}\n"})
    (root / "engine-output-receipt.json").unlink()

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["file-missing"]
    assert result.findings[0].path == "engine-output-receipt.json"


def test_extra_undeclared_file_blocks(tmp_path: Path) -> None:
    """Kills: comparing only declared-to-actual. Evidence nobody declared is foreign composition."""
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    _write(root, "oracle/foreign-unlisted.bin", b"\x00")

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["file-undeclared"]
    assert result.findings[0].path == "oracle/foreign-unlisted.bin"


def test_changed_byte_blocks(tmp_path: Path) -> None:
    """Kills: skipping the rehash and trusting that a present file is the declared file."""
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    (root / "README.md").write_bytes(b"b\n")

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["digest-mismatch"]
    assert result.verified_files == 0


def test_every_declared_file_is_rehashed_not_just_the_first(tmp_path: Path) -> None:
    root = build_package(tmp_path, files={"a.txt": b"a\n", "b.txt": b"b\n", "c/d.txt": b"d\n"})
    (root / "c" / "d.txt").write_bytes(b"tampered\n")

    result = pfs.verify_package_filesystem(root)

    assert [finding.path for finding in result.findings] == ["c/d.txt"]
    assert result.verified_files == 2


def test_findings_never_carry_an_absolute_or_host_path(tmp_path: Path) -> None:
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    (root / "README.md").write_bytes(b"b\n")
    _write(root, "extra.bin", b"\x00")

    result = pfs.verify_package_filesystem(root)

    rendered = result.summary(limit=10)
    assert str(root) not in rendered
    assert str(tmp_path) not in rendered
    for finding in result.findings:
        assert finding.path is None or not Path(finding.path).is_absolute()


# ---------------------------------------------------------------------------------- reparse points


def _make_junction(link: Path, target: Path) -> bool:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False, shell=False
    )
    return completed.returncode == 0


@pytest.mark.skipif(not WINDOWS, reason="directory junctions are a Windows reparse point")
def test_directory_junction_is_a_dead_end_and_outside_bytes_are_never_read(tmp_path: Path) -> None:
    """Measured on master: package_contents() followed a junction and hashed a file outside."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not in this package\n")
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    (root / "oracle").mkdir()
    if not _make_junction(root / "oracle" / "linked-outside", outside):
        pytest.skip("could not create junction: mklink /J failed on this host")

    result = pfs.verify_package_filesystem(root)

    assert "reparse-point" in codes(result)
    assert [finding.path for finding in result.findings if finding.code == "reparse-point"] == ["oracle/linked-outside"]
    assert not any("secret.txt" in (finding.path or "") for finding in result.findings)


def test_file_symlink_is_refused(tmp_path: Path) -> None:
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    try:
        (root / "linked.txt").symlink_to(root / "README.md")
    except OSError:  # Windows without SeCreateSymbolicLinkPrivilege (WinError 1314)
        pytest.skip("this platform/account cannot create symlinks without elevation")

    assert "reparse-point" in codes(pfs.verify_package_filesystem(root))


# ------------------------------------------------------------- root and manifest are classified first


@pytest.mark.skipif(not WINDOWS, reason="directory junctions are a Windows reparse point")
def test_a_junctioned_package_root_is_refused_and_its_destination_is_never_read(tmp_path: Path) -> None:
    """A root that is a reparse point must not silently BECOME its destination."""
    real = build_package(tmp_path / "real", files={"README.md": b"a\n"})
    assert pfs.verify_package_filesystem(real).clean is True

    link = tmp_path / "packages" / "Linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not _make_junction(link, real):
        pytest.skip("could not create junction: mklink /J failed on this host")

    result = pfs.verify_package_filesystem(link)

    assert codes(result) == ["reparse-point"]
    assert result.findings[0].path == "."
    assert result.declared_files == 0


def test_an_injected_reparse_root_is_refused_before_the_manifest_is_ever_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portable half of the control above - a file symlink cannot be created on this host."""
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    real_lstat = os.lstat

    def reparse_root(path, **kwargs):  # type: ignore[no-untyped-def]
        status = real_lstat(path, **kwargs)
        if Path(path) == root:
            return _ReparseStatus(status)
        return status

    monkeypatch.setattr(pfs.os, "lstat", reparse_root)
    monkeypatch.setattr(Path, "read_text", _explode("read_text ran on a reparse-point root"))

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["reparse-point"]


def test_a_reparse_point_manifest_is_refused_before_any_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`read_text()` on a linked manifest reads bytes from OUTSIDE the package. lstat comes first."""
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    manifest = root / pfs.PACKAGE_MARKER
    real_lstat = os.lstat

    def reparse_manifest(path, **kwargs):  # type: ignore[no-untyped-def]
        status = real_lstat(path, **kwargs)
        if Path(path) == manifest:
            return _ReparseStatus(status)
        return status

    monkeypatch.setattr(pfs.os, "lstat", reparse_manifest)
    monkeypatch.setattr(Path, "read_text", _explode("read_text ran on a reparse-point manifest"))

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["reparse-point"]
    assert result.findings[0].path == pfs.PACKAGE_MARKER


def test_a_directory_where_the_manifest_should_be_is_refused_without_reading(tmp_path: Path) -> None:
    root = tmp_path / "packages" / "Minimal"
    (root / pfs.PACKAGE_MARKER).mkdir(parents=True)

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["non-regular-file"]
    assert result.findings[0].path == pfs.PACKAGE_MARKER


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no os.mkfifo")
def test_a_fifo_manifest_is_refused_without_blocking(tmp_path: Path) -> None:
    """Opening a FIFO for read BLOCKS until a writer appears - the verifier must never get there."""
    root = tmp_path / "packages" / "Minimal"
    root.mkdir(parents=True)
    os.mkfifo(root / pfs.PACKAGE_MARKER)  # pylint: disable=no-member

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["non-regular-file"]


# ---------------------------------------------------------------- cross-host Windows alias namespace


@pytest.mark.parametrize(
    "key",
    ["COM\u00b9", "com\u00b2.png", "oracle/LPT\u00b3.dat", "lpt\u00b9.txt", "COM\u00b3.tar.gz"],
)
def test_windows_superscript_device_names_are_refused(key: str) -> None:
    """Windows best-fit-maps COM<sup>1..3</sup> onto COM1-COM3, so an ordinary file on a
    case-sensitive host is a DEVICE on the host that reads the package."""
    assert pfs.declared_path_problem(key) is not None


@pytest.mark.parametrize("key", ["PACKAGE-MANIFEST.JSON", "Package-Manifest.json", "package-manifest.json "])
def test_an_alias_of_the_excluded_manifest_may_not_be_declared_as_another_file(tmp_path: Path, key: str) -> None:
    """The manifest exclusion is exact-match; Windows is not. On a case-sensitive host these are
    ordinary second files that verify clean, and on the reading host they ARE the manifest."""
    root = build_package(
        tmp_path,
        files={"README.md": b"a\n"},
        manifest={"contents": {"files": {"README.md": _digest(b"a\n"), key: _digest(b"a\n")}}},
    )

    result = pfs.verify_package_filesystem(root)

    assert result.clean is False
    assert set(codes(result)) & {"declared-path-alias", "declared-path-unsafe", "declared-manifest-self"}


def test_the_exact_manifest_key_still_reports_declaring_itself(tmp_path: Path) -> None:
    """Seeding the alias map must not reclassify the exact self-declaration control."""
    root = build_package(
        tmp_path,
        files={"README.md": b"a\n"},
        manifest={"contents": {"files": {"README.md": _digest(b"a\n"), pfs.PACKAGE_MARKER: "0" * 64}}},
    )

    assert codes(pfs.verify_package_filesystem(root)) == ["declared-manifest-self"]


def test_reparse_predicate_recognises_the_windows_attribute_without_a_symlink() -> None:
    """The symlink control is unavailable on an unprivileged Windows host, so pin the predicate.

    A junction and a file symlink reach the classifier through the SAME attribute bit; `S_ISLNK` is
    0 for a junction, which is exactly why the attribute is checked as well.
    """

    class _Status:  # pylint: disable=too-few-public-methods
        st_mode = stat.S_IFREG | 0o644
        st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

    assert pfs._is_reparse_point(_Status()) is True  # pylint: disable=protected-access

    class _Plain:  # pylint: disable=too-few-public-methods
        st_mode = stat.S_IFREG | 0o644
        st_file_attributes = 0x20  # FILE_ATTRIBUTE_ARCHIVE

    assert pfs._is_reparse_point(_Plain()) is False  # pylint: disable=protected-access


def test_walk_never_uses_rglob_is_dir_is_file_or_resolve() -> None:
    """Those four all FOLLOW links, which is the defect this walk exists to avoid.

    Read from the parsed AST rather than the source text, so the module may DISCUSS `resolve()` in
    its prose (it has to - the pre-resolve defect is the reason the root check exists) without the
    guard reading the explanation as the offence.
    """
    tree = ast.parse(Path(pfs.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called.isdisjoint({"rglob", "glob", "is_dir", "is_file", "resolve", "samefile"}), sorted(called)


# ------------------------------------------------------------------------------------ read failures


def test_unreadable_directory_is_unassessable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = build_package(tmp_path, files={"README.md": b"a\n", "oracle/x.png": b"p\n"})
    real_scandir = os.scandir

    def refuse(path):  # type: ignore[no-untyped-def]
        if Path(path).name == "oracle":
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(pfs.os, "scandir", refuse)
    result = pfs.verify_package_filesystem(root)

    assert "directory-unreadable" in codes(result)
    assert result.clean is False


def test_unreadable_file_is_never_counted_as_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    monkeypatch.setattr(pfs, "_digest", lambda path: None)

    result = pfs.verify_package_filesystem(root)

    assert codes(result) == ["file-unreadable"]
    assert result.state == pfs.STATE_UNASSESSABLE
    assert result.verified_files == 0


def test_a_read_failure_beside_real_damage_still_reports_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills: letting one unassessable entry downgrade a package that is provably wrong."""
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    _write(root, "extra.bin", b"\x00")
    monkeypatch.setattr(pfs, "_digest", lambda path: None)

    result = pfs.verify_package_filesystem(root)

    assert set(codes(result)) == {"file-undeclared", "file-unreadable"}
    assert result.state == pfs.STATE_FINDINGS


# --------------------------------------------------------------------- check_reference_readiness entry


def test_package_target_detection_reuses_bundle_corpus_shape(tmp_path: Path) -> None:
    """Flat, nested and moved-marker packages are all packages, even with no usable manifest."""
    flat = tmp_path / "packages" / "Unit"
    nested = tmp_path / "packages" / "batch" / "Unit"
    moved = tmp_path / "elsewhere" / "Unit"
    legacy = tmp_path / "bundle" / "pbip" / "Unit"
    for path in (flat, nested, moved, legacy):
        path.mkdir(parents=True)
    (moved / pfs.PACKAGE_MARKER).write_text("{}", encoding="utf-8")

    assert [pfs.is_package_target(path) for path in (flat, nested, moved)] == [True, True, True]
    assert pfs.is_package_target(legacy) is False


def test_damaged_package_cannot_establish_before_source_or_evidence_are_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering IS the invariant: a damaged package must never be read as its own evidence."""
    root = build_package(tmp_path, manifest_text="{ not json")

    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("discovery ran before the package was verified")

    monkeypatch.setattr(crr, "_collect_evidence", explode)
    monkeypatch.setattr(crr, "resolve_source", explode)

    report = crr.scan(root)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert "manifest-not-json" in report["units"][0]["detail"]


def test_damaged_package_exits_three_from_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_package(tmp_path, files={"README.md": b"a\n"})
    (root / "README.md").write_bytes(b"b\n")

    assert crr.main([str(root)]) == crr.EXIT_CANNOT_ESTABLISH
    assert "digest-mismatch" in capsys.readouterr().out


def test_non_package_legacy_root_is_not_subjected_to_the_package_precondition(tmp_path: Path) -> None:
    """A legacy bundle unit has no manifest by design; the precondition must not invent one."""
    legacy = tmp_path / "bundle" / "pbip" / "Unit"
    legacy.mkdir(parents=True)

    assert crr._package_integrity_refusal(legacy) is None  # pylint: disable=protected-access


@pytest.mark.skipif(not WINDOWS, reason="directory junctions are a Windows reparse point")
def test_scan_refuses_a_junctioned_target_and_never_reads_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supplied path is verified, not its destination.

    `Path.resolve()` substitutes the destination silently, so resolving before the precondition would
    verify - and then take source and evidence from - a directory the operator never named, with the
    reparse hop erased from the verdict. The destination here is a package that WOULD be assessed.
    """
    real = build_package(
        tmp_path / "real",
        files={"report.json": b'{"workbooks": [{"name": "Minimal"}], "datasources": []}\n'},
    )
    link = tmp_path / "packages" / "Linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not _make_junction(link, real):
        pytest.skip("could not create junction: mklink /J failed on this host")

    monkeypatch.setattr(crr, "_collect_evidence", _explode("evidence discovery ran on a reparse-point target"))
    monkeypatch.setattr(crr, "resolve_source", _explode("source resolution ran on a reparse-point target"))

    report = crr.scan(link)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert "reparse-point" in report["units"][0]["detail"]
    assert report["evidence_records"] == 0


def test_scan_does_not_resolve_the_target_before_the_package_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portable half: an injected reparse root must reach the refusal, not the assessment."""
    root = build_package(
        tmp_path,
        files={"report.json": b'{"workbooks": [{"name": "Minimal"}], "datasources": []}\n'},
    )
    real_lstat = os.lstat

    def reparse_root(path, **kwargs):  # type: ignore[no-untyped-def]
        status = real_lstat(path, **kwargs)
        if Path(path) == root:
            return _ReparseStatus(status)
        return status

    monkeypatch.setattr(pfs.os, "lstat", reparse_root)
    monkeypatch.setattr(crr, "_collect_evidence", _explode("evidence discovery ran on a reparse-point target"))
    monkeypatch.setattr(crr, "resolve_source", _explode("source resolution ran on a reparse-point target"))
    monkeypatch.setattr(Path, "resolve", _explode("the target was resolved before the package precondition"))

    report = crr.scan(root)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert "reparse-point" in report["units"][0]["detail"]


def test_clean_package_continues_unchanged_and_is_not_rescued(tmp_path: Path) -> None:
    """A clean package still fails for its own reasons - here, an unresolvable source (#558).

    This is the discriminator that keeps the slice honest: the precondition proves BYTES, so it must
    not turn a package whose source cannot be resolved into a pass, and its refusal must not be
    mistaken for one.
    """
    report_dir = "fabric/Minimal.Report/definition"
    root = build_package(
        tmp_path,
        files={
            "report.json": b'{"workbooks": [{"name": "Minimal"}], "datasources": []}\n',
            f"{report_dir}/pages/pages.json": b'{"pages": []}\n',
        },
    )

    assert pfs.verify_package_filesystem(root).clean is True

    report = crr.scan(root)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    detail = report["units"][0]["detail"]
    assert "no Tableau source workbook could be resolved" in detail
    assert "declared contents" not in detail


def test_a_package_the_precondition_refuses_never_reaches_the_source_verdict(tmp_path: Path) -> None:
    """The same package, one byte changed: the refusal replaces the assessment rather than joining it."""
    report_dir = "fabric/Minimal.Report/definition"
    root = build_package(
        tmp_path,
        files={
            "report.json": b'{"workbooks": [{"name": "Minimal"}], "datasources": []}\n',
            f"{report_dir}/pages/pages.json": b'{"pages": []}\n',
        },
    )
    (root / "report.json").write_bytes(b'{"workbooks": [], "datasources": []}\n')

    report = crr.scan(root)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    detail = report["units"][0]["detail"]
    assert "digest-mismatch" in detail
    assert "no Tableau source workbook could be resolved" not in detail
