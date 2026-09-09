"""
purpose: prove that a handover package's `package-manifest.json` is structurally readable and that
         the file namespace and bytes it declares are EXACTLY the package on disk - before any
         consumer reads that package as evidence.
usage:   library only - imported by scripts/check_reference_readiness.py.

Why this exists (issue #562, slice 1)
-------------------------------------
`bundle_corpus.is_self_contained()` checks only that `package-manifest.json` EXISTS, and that marker
is what stops both gates inheriting ancestor evidence. So a package could declare itself a complete
evidence boundary while its manifest was malformed, its declared bytes had changed, a declared file
had been deleted, or a foreign file had been dropped in - and the entry gate still returned
`READY 4/4`. An independent audit measured that on 14 controlled packages (session evidence
`files/audit-package-contract/audit-report.md`, `files/customer-ready-package-baseline/`), every one
of them false-clean on `master`.

What this slice proves, and what it deliberately does NOT
--------------------------------------------------------
It proves ONE thing: **the declared file namespace and bytes are the package**. Nothing here knows
what a workbook, a datasource, an asset role, a LUID or an oracle render is; there is no identity
join, no source return and no working/edited lifecycle. Those are separate invariants and are
deliberately later slices - a verifier that mixes "are these the bytes" with "is this the right
workbook" cannot be reviewed as one claim.

⚠️ **The manifest is UNSIGNED and excludes itself from `contents.files`** (`package_unit.py`, which
writes the map last and therefore cannot list its own digest). So this proves INTERNAL consistency:
it detects accidental damage - a deleted file, a changed byte, a half-copied tree - and confused
composition - a foreign file, or two packages' contents merged. It CANNOT detect an adversary who
rewrites a file and the manifest together. A claim stronger than that needs an external signed
digest and is out of scope.

Fail closed
-----------
"I could not read it" is never "it is fine". A read error, an unreadable manifest and a missing
manifest are all `unassessable`, which is non-clean; only a package whose namespace and every digest
match exactly is `clean`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bundle_corpus import PACKAGE_MARKER, is_package_target

__all__ = [
    "PACKAGE_MARKER",
    "STATE_CLEAN",
    "STATE_FINDINGS",
    "STATE_UNASSESSABLE",
    "PackageFilesystem",
    "PackageFinding",
    "is_package_target",
    "verify_package_filesystem",
]

#: The namespace and every declared digest matched exactly.
STATE_CLEAN = "clean"
#: Something is provably wrong: damaged JSON, an unsafe key, a missing/extra file, a changed byte.
STATE_FINDINGS = "findings"
#: Nothing could be established - no manifest, or the tree could not be read. NOT a pass.
STATE_UNASSESSABLE = "unassessable"

#: Finding codes that mean "could not be established" rather than "is provably wrong". Both are
#: non-clean; they are separated because they call for different operator actions - repackage versus
#: fix permissions/re-copy.
UNASSESSABLE_CODES = frozenset(
    {
        "manifest-missing",
        "manifest-unreadable",
        "directory-unreadable",
        "entry-unreadable",
        "file-unreadable",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: `C:`, `c:/x`, `C:x` - a drive qualifier anywhere at the head, including the DRIVE-RELATIVE form
#: with no separator, which `os.path.join` on Windows resolves against that drive's current
#: directory rather than against the package.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

#: Windows reserved device names. Reserved WITH an extension too (`CON.txt` is `CON`), so the check
#: is on the component's stem.
#:
#: WARNING: **The SUPERSCRIPT digits are reserved too** - `COM<sup>1..3</sup>` / `LPT<sup>1..3</sup>`
#: (U+00B9 / U+00B2 / U+00B3). Windows applies a best-fit mapping that folds them onto `COM1`-`COM3`,
#: so a package built on a case-sensitive host can carry a name that is an ordinary file there and a
#: DEVICE on the host that reads it. `"com\u00b9".upper()` is `"COM\u00b9"`, so the stem check catches
#: them only if they are listed here explicitly.
_RESERVED_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in "123456789\u00b9\u00b2\u00b3"}
    | {f"LPT{digit}" for digit in "123456789\u00b9\u00b2\u00b3"}
)

#: `FILE_ATTRIBUTE_REPARSE_POINT`. Named here because `stat` only exposes it on Windows, and this
#: module must be readable (and testable) on POSIX.
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class PackageFinding:
    """One reason a package is not clean.

    ``code`` is stable and machine-readable. ``path`` is a package-RELATIVE POSIX key or None - never
    an absolute or host path, because these strings are printed into shareable verdicts. A declared
    key that failed the path rules is deliberately NOT echoed back either: an unsafe key can itself
    be an absolute customer path, so it is named by its ordinal in the manifest instead.
    """

    code: str
    path: str | None
    detail: str

    def describe(self) -> str:
        """One line naming the code, the relative path when there is one, and the reason."""
        where = f" [{self.path}]" if self.path else ""
        return f"{self.code}{where}: {self.detail}"


@dataclass(frozen=True)
class PackageFilesystem:
    """The typed verdict for one package's declared-versus-actual filesystem state."""

    root: Path
    state: str
    findings: tuple[PackageFinding, ...]
    declared_files: int
    verified_files: int
    empty_directories: tuple[str, ...]

    @property
    def clean(self) -> bool:
        """Whether the namespace and every declared digest matched exactly."""
        return self.state == STATE_CLEAN

    def summary(self, limit: int = 4) -> str:
        """A short, shareable reason line - the first ``limit`` findings, then a count of the rest."""
        if not self.findings:
            return f"{self.declared_files} declared file(s) verified"
        shown = [finding.describe() for finding in self.findings[:limit]]
        remaining = len(self.findings) - len(shown)
        if remaining > 0:
            shown.append(f"and {remaining} more")
        return "; ".join(shown)


class _DuplicateKey(ValueError):
    """A JSON object repeated a key, at any depth.

    WARNING: it deliberately carries **no key text**. A duplicated key can itself be an absolute
    customer path (`contents.files` is keyed by path), and this exception's message reaches a
    shareable finding, so it names the POSITION and nothing else.
    """


class _NonStandardConstant(ValueError):
    """The document used `NaN`, `Infinity` or `-Infinity`, which JSON does not define."""


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    """`object_pairs_hook` that refuses a repeated key ANYWHERE in the document.

    WARNING: Plain `json.loads` silently keeps the LAST value, so `{"kind": "workbook", "kind": "x"}`
    parses happily and two readers can legitimately disagree about what the package declared. The
    controlled duplicate-key package returned production `READY 4/4`.
    """
    seen: dict[str, object] = {}
    for ordinal, (key, value) in enumerate(pairs, start=1):
        if key in seen:
            raise _DuplicateKey(f"repeats a key already given earlier in the same object (entry #{ordinal})")
        seen[key] = value
    return seen


def _reject_constant(_name: str) -> object:
    """`parse_constant` hook: `NaN`/`Infinity`/`-Infinity` are not JSON and are refused everywhere.

    Python's decoder accepts them by default, so a manifest carrying one parses on this reader and is
    rejected by a strict one - the same "two readers disagree about what was declared" ambiguity the
    duplicate-key hook exists for. The name is not echoed: the refusal is generic.
    """
    raise _NonStandardConstant("uses a non-standard JSON constant")


def _is_reparse_point(status: os.stat_result) -> bool:
    """Whether an `lstat` result describes a symlink, a junction or any other reparse point.

    ONE predicate, used by the root check, the manifest check and the walker alike - a second copy is
    how the three drift apart. `S_ISLNK` is 0 for a Windows junction, which is why the file-attribute
    bit is tested as well.
    """
    if stat.S_ISLNK(status.st_mode):
        return True
    return bool(getattr(status, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _stat_failure(relative: str, error: OSError, *, expect_directory: bool) -> PackageFinding:
    """The finding for a path whose own `lstat` failed."""
    if isinstance(error, FileNotFoundError):
        if expect_directory:
            return PackageFinding("directory-unreadable", relative, "does not exist")
        return PackageFinding("manifest-missing", relative, "the package carries no manifest")
    code = "directory-unreadable" if expect_directory else "manifest-unreadable"
    return PackageFinding(code, relative, f"could not be stat'd: {type(error).__name__}")


def _classify_entry(path: Path, relative: str, *, expect_directory: bool) -> PackageFinding | None:
    """Classify ONE path from its own `lstat`, following nothing, or None when it is as expected.

    WARNING: **this runs before anything opens or reads the path, and that ordering is the point.**
    `read_text()` on a symlinked, junctioned or FIFO `package-manifest.json` follows or BLOCKS - it
    reads bytes from outside the package, or never returns at all - so the manifest is classified by
    `os.lstat` first, with the SAME reparse predicate the walker uses rather than a second copy of it.

    Residual, stated rather than mechanised: a path replaced between the `lstat` and the subsequent
    open is not detected. That is an adversarial race, and this module's guarantee is accidental
    damage and confused composition (see the module docstring), not adversarial rewrite.
    """
    try:
        status = os.lstat(path)
    except OSError as error:
        return _stat_failure(relative, error, expect_directory=expect_directory)
    if _is_reparse_point(status):
        return PackageFinding("reparse-point", relative, "is a symlink, junction or other reparse point")
    if expect_directory:
        if stat.S_ISDIR(status.st_mode):
            return None
        return PackageFinding("non-regular-file", relative, "is not a directory")
    if not stat.S_ISREG(status.st_mode):
        return PackageFinding("non-regular-file", relative, "is not a regular file")
    return None


def _parse_manifest_text(raw: str) -> tuple[object, PackageFinding | None]:
    """Strictly decode the manifest text, or the finding explaining why it cannot be decoded."""
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant), None
    except _DuplicateKey as duplicate:
        return None, PackageFinding("manifest-duplicate-key", PACKAGE_MARKER, str(duplicate))
    except _NonStandardConstant:
        return None, PackageFinding(
            "manifest-not-json", PACKAGE_MARKER, "is not valid JSON: it uses a non-standard constant"
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        return None, PackageFinding("manifest-not-json", PACKAGE_MARKER, f"is not valid JSON: {type(error).__name__}")


def _load_manifest(root: Path) -> tuple[Mapping[str, object] | None, PackageFinding | None]:
    """The parsed manifest object, or the finding explaining why there is none."""
    manifest_path = root / PACKAGE_MARKER
    refusal = _classify_entry(manifest_path, PACKAGE_MARKER, expect_directory=False)
    if refusal is not None:
        return None, refusal
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return None, PackageFinding("manifest-unreadable", PACKAGE_MARKER, f"could not be read: {type(error).__name__}")
    parsed, refusal = _parse_manifest_text(raw)
    if refusal is not None:
        return None, refusal
    if not isinstance(parsed, dict):
        return None, PackageFinding(
            "manifest-not-object", PACKAGE_MARKER, f"top level is {type(parsed).__name__}, not an object"
        )
    return parsed, None


def _declared_map(manifest: Mapping[str, object]) -> tuple[Mapping[str, object] | None, PackageFinding | None]:
    """`contents.files`, or the finding explaining why it cannot be used."""
    contents = manifest.get("contents")
    if not isinstance(contents, dict):
        detail = "is absent" if contents is None else f"is {type(contents).__name__}, not an object"
        return None, PackageFinding("contents-not-object", PACKAGE_MARKER, f"`contents` {detail}")
    files = contents.get("files")
    if not isinstance(files, dict):
        detail = "is absent" if files is None else f"is {type(files).__name__}, not an object"
        return None, PackageFinding("contents-files-not-object", PACKAGE_MARKER, f"`contents.files` {detail}")
    return files, None


def _component_problem(component: str) -> str | None:
    """Why one path component may not be used, or None when it is safe."""
    if component in ("", ".", ".."):
        return "an empty, `.` or `..` component"
    if ":" in component:
        return "a colon (drive qualifier or NTFS alternate data stream)"
    if component != component.rstrip(". "):
        return "a trailing dot or space, which Windows silently strips"
    if component.split(".", 1)[0].upper() in _RESERVED_DEVICES:
        return "a reserved Windows device name"
    return None


def _whole_string_problem(value: str) -> str | None:
    """Why the WHOLE declared key is unusable, before it is split into components."""
    if not value:
        return "is empty"
    if any(character == "\x00" or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return "contains a NUL or control character"
    if "\\" in value:
        return "contains a backslash; the producer writes POSIX separators only"
    if value.startswith("/"):
        return "is POSIX-absolute or a UNC path, not package-relative"
    if _DRIVE_RE.match(value):
        return "is drive-qualified (absolute, rooted or drive-relative), not package-relative"
    return None


def declared_path_problem(value: object) -> str | None:
    """Why a declared `contents.files` key is not a canonical package-relative POSIX path.

    Judged LEXICALLY and on BOTH flavours regardless of the host: a packaging host is not necessarily
    the host that reads the package, and `..\\x` is a traversal on Windows while `PurePosixPath`
    reads it as one innocent filename. Nothing here touches the filesystem - these strings are
    untrusted, and `Path("/etc/hosts")` on Windows resolves against the current drive.
    """
    if not isinstance(value, str):
        return f"is {type(value).__name__}, not a string"
    problem = _whole_string_problem(value)
    if problem is not None:
        return problem
    for component in value.split("/"):
        problem = _component_problem(component)
        if problem is not None:
            return f"has {problem}"
    return None


def alias_key(value: str) -> str:
    """The key two declared paths collide on when the package is read on Windows.

    Windows compares case-insensitively and strips trailing dots/spaces from each component, so
    `README.md` and `readme.md` cannot name two distinct immutable files there. This is built and
    compared BEFORE any filesystem access: on a case-insensitive host, probing would already have
    conflated them.
    """
    return "/".join(component.rstrip(". ").casefold() for component in value.split("/"))


def _check_declared(files: Mapping[str, object]) -> tuple[dict[str, str], list[PackageFinding]]:
    """`{safe declared path: declared digest}` plus a finding for every entry that cannot be used."""
    findings: list[PackageFinding] = []
    safe: dict[str, str] = {}
    # SEEDED with the excluded root manifest, because the exclusion is exact-match while Windows is
    # not. On a case-sensitive host `PACKAGE-MANIFEST.JSON` is a second, ordinary file that verifies
    # clean; on the Windows host that later reads the package it IS the manifest, so the package
    # silently describes a file that cannot exist beside itself.
    aliases: dict[str, str] = {alias_key(PACKAGE_MARKER): PACKAGE_MARKER}
    for ordinal, (key, digest) in enumerate(files.items(), start=1):
        problem = declared_path_problem(key)
        if problem is not None:
            # The key itself is NOT echoed: an unsafe key can be an absolute customer path, and these
            # findings are printed into shareable verdicts.
            findings.append(PackageFinding("declared-path-unsafe", None, f"declared entry #{ordinal} {problem}"))
            continue
        if key == PACKAGE_MARKER:
            findings.append(
                PackageFinding("declared-manifest-self", key, "the manifest may not declare its own digest")
            )
            continue
        collision = alias_key(key)
        if collision in aliases:
            findings.append(
                PackageFinding(
                    "declared-path-alias",
                    key,
                    f"collides with `{aliases[collision]}` once Windows case and trailing dot/space are applied",
                )
            )
            continue
        aliases[collision] = key
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            findings.append(
                PackageFinding("declared-digest-invalid", key, "declared digest is not a lowercase 64-hex sha256")
            )
            continue
        safe[key] = digest
    return safe, findings


def _walk(root: Path) -> tuple[dict[str, Path], list[PackageFinding], list[str]]:
    """Every REGULAR file under ``root``, top-down, refusing every reparse point BEFORE recursing.

    ⚠️ `rglob`/`is_dir`/`is_file`/`resolve` all follow links, so a directory junction inside a package
    makes them walk and hash bytes from OUTSIDE it - measured: `package_contents()` followed
    `oracle/linked-outside` and hashed a file that was never in the package. Descent here is by
    `os.scandir` + `lstat`, and a reparse point is a finding and a DEAD END, never a door.
    """
    files: dict[str, Path] = {}
    findings: list[PackageFinding] = []
    empty: list[str] = []
    root_problem = _classify_entry(root, ".", expect_directory=True)
    if root_problem is not None:
        return files, [root_problem], empty
    pending: list[tuple[str, Path]] = [("", root)]
    while pending:
        prefix, directory = pending.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = list(scanner)
        except OSError as error:
            findings.append(
                PackageFinding("directory-unreadable", prefix or ".", f"could not be listed: {type(error).__name__}")
            )
            continue
        if not entries:
            empty.append(prefix or ".")
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            _classify(entry, relative, files, findings, pending)
    return files, findings, sorted(empty)


def _classify(
    entry: os.DirEntry[str],
    relative: str,
    files: dict[str, Path],
    findings: list[PackageFinding],
    pending: list[tuple[str, Path]],
) -> None:
    """Sort one directory entry into file / recurse / refused, from its `lstat` alone."""
    try:
        status = entry.stat(follow_symlinks=False)
    except OSError as error:
        findings.append(PackageFinding("entry-unreadable", relative, f"could not be stat'd: {type(error).__name__}"))
        return
    if _is_reparse_point(status):
        findings.append(PackageFinding("reparse-point", relative, "is a symlink, junction or other reparse point"))
        return
    if stat.S_ISDIR(status.st_mode):
        pending.append((relative, Path(entry.path)))
        return
    if not stat.S_ISREG(status.st_mode):
        findings.append(PackageFinding("non-regular-file", relative, "is not a regular file"))
        return
    files[relative] = Path(entry.path)


def _compare_namespace(declared: Mapping[str, str], actual: Mapping[str, Path]) -> list[PackageFinding]:
    """The namespace difference: the actual regular-file set must EQUAL the declared one.

    The only exclusion is the root `package-manifest.json`, which carries the map and so cannot list
    itself. An extra file is a finding for the same reason a missing one is: a package is an evidence
    boundary, and evidence nobody declared is foreign composition, not a bonus.
    """
    findings = [
        PackageFinding("file-missing", key, "is declared by the manifest but is not in the package")
        for key in sorted(set(declared) - set(actual))
    ]
    findings.extend(
        PackageFinding("file-undeclared", key, "is in the package but is not declared by the manifest")
        for key in sorted(set(actual) - set(declared))
    )
    return findings


def _digest(path: Path) -> str | None:
    """sha256 of one already-verified regular file, or None when its bytes could not be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _rehash(declared: Mapping[str, str], actual: Mapping[str, Path]) -> tuple[int, list[PackageFinding]]:
    """Rehash every file present on BOTH sides; return how many matched and every failure."""
    findings: list[PackageFinding] = []
    verified = 0
    for key in sorted(set(declared) & set(actual)):
        found = _digest(actual[key])
        if found is None:
            findings.append(PackageFinding("file-unreadable", key, "bytes could not be read, so it cannot be verified"))
            continue
        if found != declared[key]:
            findings.append(PackageFinding("digest-mismatch", key, "bytes differ from the digest the manifest records"))
            continue
        verified += 1
    return verified, findings


def _state(findings: Iterable[PackageFinding]) -> str:
    """`clean` / `findings` / `unassessable` - never clean while anything is outstanding."""
    codes = [finding.code for finding in findings]
    if not codes:
        return STATE_CLEAN
    if all(code in UNASSESSABLE_CODES for code in codes):
        return STATE_UNASSESSABLE
    return STATE_FINDINGS


def _refused(root: Path, finding: PackageFinding) -> PackageFilesystem:
    """The verdict when the manifest itself could not be turned into a declared namespace."""
    return PackageFilesystem(
        root=root,
        state=_state([finding]),
        findings=(finding,),
        declared_files=0,
        verified_files=0,
        empty_directories=(),
    )


def verify_package_filesystem(root: Path) -> PackageFilesystem:
    """Prove ``root``'s manifest is readable and its declared namespace/bytes ARE the package.

    This is a precondition, not a verdict about the migration: it says nothing about workbook or
    datasource roles, identity or oracle evidence. Callers run it BEFORE discovering source or
    evidence, so a damaged package can never be read as its own evidence boundary.

    WARNING: **``root`` must be the path the caller was HANDED, not a resolved one.** The whole point
    of the root classification below is that a package root which is itself a symlink or junction is
    refused rather than silently becoming its destination - and `Path.resolve()` performs exactly
    that substitution before this function can see it.
    """
    root_problem = _classify_entry(root, ".", expect_directory=True)
    if root_problem is not None:
        return _refused(root, root_problem)
    manifest, refusal = _load_manifest(root)
    if refusal is not None or manifest is None:
        return _refused(root, refusal or PackageFinding("manifest-missing", PACKAGE_MARKER, "no manifest"))
    files, refusal = _declared_map(manifest)
    if refusal is not None or files is None:
        return _refused(root, refusal or PackageFinding("contents-files-not-object", PACKAGE_MARKER, "no file map"))

    declared, findings = _check_declared(files)
    actual, walk_findings, empty = _walk(root)
    actual.pop(PACKAGE_MARKER, None)
    findings.extend(walk_findings)
    findings.extend(_compare_namespace(declared, actual))
    verified, hash_findings = _rehash(declared, actual)
    findings.extend(hash_findings)
    return PackageFilesystem(
        root=root,
        state=_state(findings),
        findings=tuple(findings),
        declared_files=len(files),
        verified_files=verified,
        empty_directories=tuple(empty),
    )
