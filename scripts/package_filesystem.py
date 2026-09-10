"""
purpose: prove a handover package's root manifest still describes exactly the bytes on disk.
usage:   import package_filesystem; package_filesystem.verify_package(root, classification)

⚠️ **This is the filesystem/manifest INTEGRITY slice of issue #562, and nothing else.** It runs on a
target that `bundle_corpus.classify_target` has ALREADY classified as a safe package
(:attr:`bundle_corpus.TargetClassification.declares_self_contained`), and it answers one question:

    is the root ``package-manifest.json`` strict readable JSON whose ``contents.files`` describes
    EXACTLY every regular file in the package, and do those files still hash to the recorded bytes?

It deliberately does **not** look at roles (`artifacts.*`), identity (`workbook_identity`,
LUIDs), oracle semantics, source resolution, promotion or the working/dispatched lifecycle. Those
are separate slices with separate invariants; mixing them here is how a package verifier turns into
a second gate nobody can review.

Order is the invariant, not an implementation detail
----------------------------------------------------
1. no-follow ``lstat`` of the root and of the root marker **again** (defense in depth: the classifier
   already did it, but its answer is a value that travelled, and the bytes are opened HERE);
2. open and strictly parse the manifest;
3. canonicalize every declared key **before** any of them reaches the filesystem;
4. one top-down ``os.scandir``/``lstat`` walk that treats a reparse point as a dead end;
5. exact set equality between declared keys and walked regular files;
6. rehash every declared file that the walk itself found.

⚠️ **Nothing in here dereferences.** No ``resolve``, ``rglob``, ``is_file``, ``is_dir``, ``exists``,
``glob`` or ``stat`` with ``follow_symlinks=True``. A path that came out of the manifest is never
opened; only a path the walk produced is, and only after that walk proved it a regular file. That is
what makes "outside bytes are never read" a property of the code rather than a promise.

⚠️ **Threat model, stated rather than implied.** The manifest is unsigned and excludes itself from
``contents.files``, so this proves *internal consistency*. It detects accidental damage, a partial
copy, a confused composition of two packages, and an edit made without re-packaging. It does NOT
detect an adversary who rewrites a file and its manifest entry together - that needs an external
anchor (a signature or a digest held outside the package), which this slice does not invent.

⚠️ **Residual race, stated rather than papered over.** Between the ``lstat`` that proves an entry is
a regular non-reparse file and the ``open`` that reads it, the entry can be replaced. Closing that
needs handle-level verification (``O_NOFOLLOW`` plus ``fstat`` identity comparison) which is not
portable to Windows; the non-adversarial threat model above does not require it, and building a
handle-level proof here would be machinery larger than the claim it supports.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from bundle_corpus import PACKAGE_MARKER, TargetClassification, is_reparse_entry

#: Result states. ``CLEAN`` is the only one a consumer may continue past.
STATUS_CLEAN = "clean"
#: The package is provably wrong: something is missing, extra, changed or unsafe.
STATUS_FINDINGS = "findings"
#: The package could not be assessed at all. **Never grouped with clean** - "I could not tell" and
#: "I checked and it is fine" are the two answers this module exists to keep apart.
STATUS_UNASSESSABLE = "unassessable"

# ---------------------------------------------------------------------------------------------
# Stable diagnostic codes.
#
# ⚠️ These strings are printed into verdicts that get pasted into issues and shared with customers.
# A code carries NO host path, NO exception text and NO manifest key text. A finding may carry a
# package-RELATIVE path (which the package itself already publishes) or a bare ordinal, and an unsafe
# key is ordinal-only, because the unsafe spelling is exactly the thing that must not be echoed.
# ---------------------------------------------------------------------------------------------
CODE_NOT_A_DECLARED_PACKAGE = "package_boundary_not_declared"
CODE_ROOT_REPLACED = "package_root_replaced"
CODE_ROOT_UNREADABLE = "package_root_unreadable"
CODE_MARKER_REPLACED = "package_marker_replaced"
CODE_MARKER_UNREADABLE = "package_marker_unreadable"
CODE_MANIFEST_UNREADABLE = "package_manifest_unreadable"
CODE_MANIFEST_NOT_UTF8 = "package_manifest_not_utf8"
CODE_MANIFEST_NOT_JSON = "package_manifest_not_json"
CODE_MANIFEST_NOT_OBJECT = "package_manifest_not_object"
CODE_MANIFEST_DUPLICATE_KEY = "package_manifest_duplicate_key"
CODE_MANIFEST_NON_FINITE = "package_manifest_non_finite_number"
CODE_CONTENTS_MISSING = "package_contents_missing"
CODE_CONTENTS_NOT_OBJECT = "package_contents_not_object"
CODE_FILES_MISSING = "package_contents_files_missing"
CODE_FILES_NOT_OBJECT = "package_contents_files_not_object"
CODE_KEY_NOT_STRING = "package_declared_key_not_a_string"
CODE_KEY_UNSAFE = "package_declared_key_unsafe"
CODE_KEY_ALIASES_MARKER = "package_declared_key_aliases_the_manifest"
CODE_KEY_COLLISION = "package_declared_keys_collide"
CODE_DIGEST_NOT_STRING = "package_declared_digest_not_a_string"
CODE_DIGEST_MALFORMED = "package_declared_digest_malformed"
CODE_ENTRY_REPARSE = "package_entry_reparse"
CODE_ENTRY_NOT_REGULAR = "package_entry_not_regular"
CODE_ENTRY_UNASSESSABLE = "package_entry_unassessable"
CODE_DIRECTORY_UNREADABLE = "package_directory_unreadable"
CODE_FILE_UNDECLARED = "package_file_undeclared"
CODE_FILE_MISSING = "package_file_missing"
CODE_FILE_UNREADABLE = "package_file_unreadable"
CODE_DIGEST_MISMATCH = "package_file_digest_mismatch"

#: Generic wording per code. ASCII only: these reach a Windows console, whose default code page
#: cannot encode the arrows and warning glyphs the docstrings use.
_DETAILS = {
    CODE_NOT_A_DECLARED_PACKAGE: (
        "this target does not declare a package boundary, so its contents cannot be verified here"
    ),
    CODE_ROOT_REPLACED: "the package root is no longer a plain directory",
    CODE_ROOT_UNREADABLE: "the package root could not be assessed without following it",
    CODE_MARKER_REPLACED: f"{PACKAGE_MARKER} is no longer a regular file",
    CODE_MARKER_UNREADABLE: f"{PACKAGE_MARKER} could not be assessed without following it",
    CODE_MANIFEST_UNREADABLE: f"{PACKAGE_MARKER} could not be read",
    CODE_MANIFEST_NOT_UTF8: f"{PACKAGE_MARKER} is not valid UTF-8",
    CODE_MANIFEST_NOT_JSON: f"{PACKAGE_MARKER} is not parseable JSON",
    CODE_MANIFEST_NOT_OBJECT: f"{PACKAGE_MARKER} does not hold a JSON object at the top level",
    CODE_MANIFEST_DUPLICATE_KEY: (
        f"{PACKAGE_MARKER} repeats an object key, so the same name has two values and the manifest "
        "means different things to different readers"
    ),
    CODE_MANIFEST_NON_FINITE: (f"{PACKAGE_MARKER} carries NaN or Infinity, which is not JSON and does not round-trip"),
    CODE_CONTENTS_MISSING: f"{PACKAGE_MARKER} declares no contents, so it describes no files",
    CODE_CONTENTS_NOT_OBJECT: f"{PACKAGE_MARKER} contents is not an object",
    CODE_FILES_MISSING: f"{PACKAGE_MARKER} contents declares no files map",
    CODE_FILES_NOT_OBJECT: f"{PACKAGE_MARKER} contents.files is not an object",
    CODE_KEY_NOT_STRING: "a declared content key is not a string",
    CODE_KEY_UNSAFE: (
        "a declared content key is not a canonical package-relative POSIX path (it is absolute, "
        "escaping, device-reserved, ambiguously separated or otherwise unsafe)"
    ),
    CODE_KEY_ALIASES_MARKER: (
        f"a declared content key names {PACKAGE_MARKER} itself, which carries the map and is "
        "therefore never one of the files the map describes"
    ),
    CODE_KEY_COLLISION: (
        "two declared content keys name the same file once host aliasing is applied, so they cannot "
        "describe distinct bytes"
    ),
    CODE_DIGEST_NOT_STRING: "a declared digest is not a string",
    CODE_DIGEST_MALFORMED: "a declared digest is not exactly 64 lowercase hex characters",
    CODE_ENTRY_REPARSE: "a package entry is a link, junction or other reparse point",
    CODE_ENTRY_NOT_REGULAR: "a package entry is neither a regular file nor a directory",
    CODE_ENTRY_UNASSESSABLE: "a package entry could not be assessed without following it",
    CODE_DIRECTORY_UNREADABLE: "a package directory could not be listed",
    CODE_FILE_UNDECLARED: f"a regular file in the package is absent from {PACKAGE_MARKER}",
    CODE_FILE_MISSING: f"a file declared by {PACKAGE_MARKER} is not a regular file in the package",
    CODE_FILE_UNREADABLE: "a declared file could not be read to verify its digest",
    CODE_DIGEST_MISMATCH: "a declared file no longer hashes to its recorded digest",
}

#: Codes that mean "I could not tell", as opposed to "I checked and it is wrong".
_UNASSESSABLE_CODES = frozenset(
    {
        CODE_ROOT_UNREADABLE,
        CODE_MARKER_UNREADABLE,
        CODE_MANIFEST_UNREADABLE,
        CODE_ENTRY_UNASSESSABLE,
        CODE_DIRECTORY_UNREADABLE,
        CODE_FILE_UNREADABLE,
    }
)

#: Windows reserved device names. The superscript variants are real: Windows resolves ``COM1``,
#: ``COM\u00b9``, ``COM\u00b2`` and ``COM\u00b3`` to the SAME device, so a manifest key spelled with a
#: superscript digit is a device alias that a plain ``COM1`` check waves straight through.
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{name}{digit}" for name in ("com", "lpt") for digit in "123456789\u00b9\u00b2\u00b3"}
)

#: Digits Windows treats as device ordinals beyond ASCII.
_SUPERSCRIPT_DIGITS = {"\u00b9": "1", "\u00b2": "2", "\u00b3": "3"}

_HEX_DIGITS = frozenset("0123456789abcdef")

#: One read of a file, for hashing. Big enough to be cheap, small enough that a large asset does not
#: land in memory whole.
_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class Finding:
    """One reason a package is not clean.

    ``path`` is a package-RELATIVE POSIX path or ``None``; ``ordinal`` is the zero-based position of
    a declared key whose own text is unsafe to print. Exactly one of them is usually set, and neither
    ever carries a host path, a manifest key spelling or an exception message.
    """

    code: str
    detail: str
    path: str | None = None
    ordinal: int | None = None

    def as_dict(self) -> dict[str, object]:
        """The JSON shape a consumer embeds. Keys with no value are dropped, not nulled."""
        row: dict[str, object] = {"code": self.code, "detail": self.detail}
        if self.path is not None:
            row["path"] = self.path
        if self.ordinal is not None:
            row["ordinal"] = self.ordinal
        return row


@dataclass(frozen=True)
class PackageFilesystemResult:
    """What the manifest/filesystem comparison established, and nothing more.

    ``findings`` are proven defects; ``unassessable`` are the places the verifier could not look.
    They are kept apart on purpose: an operator fixes a finding, but investigates an unassessable.
    Only an empty-and-empty result is :attr:`is_clean`.
    """

    status: str
    findings: tuple[Finding, ...] = ()
    unassessable: tuple[Finding, ...] = ()
    files_declared: int = 0
    files_verified: int = 0

    @property
    def is_clean(self) -> bool:
        """``True`` only when the manifest exactly describes the bytes on disk."""
        return self.status == STATUS_CLEAN

    @property
    def first_code(self) -> str | None:
        """The leading reason, unassessable first: it is the one that stopped the verification."""
        for row in (*self.unassessable, *self.findings):
            return row.code
        return None

    def codes(self) -> tuple[str, ...]:
        """Every distinct code, in first-seen order, for a consumer that prints a summary."""
        seen: list[str] = []
        for row in (*self.unassessable, *self.findings):
            if row.code not in seen:
                seen.append(row.code)
        return tuple(seen)

    def as_dict(self) -> dict[str, object]:
        """The machine-readable shape. Safe to embed in a shared verdict."""
        return {
            "status": self.status,
            "files_declared": self.files_declared,
            "files_verified": self.files_verified,
            "findings": [row.as_dict() for row in self.findings],
            "unassessable": [row.as_dict() for row in self.unassessable],
        }


class _ManifestError(Exception):
    """A strict-parse refusal, carrying only a stable code. Never the offending text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _finding(code: str, *, path: str | None = None, ordinal: int | None = None) -> Finding:
    return Finding(code=code, detail=_DETAILS[code], path=path, ordinal=ordinal)


def _result(rows: list[Finding], *, files_declared: int = 0, files_verified: int = 0) -> PackageFilesystemResult:
    """Split rows into findings and unassessable, and derive the status from what is present."""
    findings = tuple(row for row in rows if row.code not in _UNASSESSABLE_CODES)
    unassessable = tuple(row for row in rows if row.code in _UNASSESSABLE_CODES)
    if unassessable:
        status = STATUS_UNASSESSABLE
    elif findings:
        status = STATUS_FINDINGS
    else:
        status = STATUS_CLEAN
    return PackageFilesystemResult(
        status=status,
        findings=findings,
        unassessable=unassessable,
        files_declared=files_declared,
        files_verified=files_verified,
    )


# ---------------------------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse a repeated key at ANY object depth, without echoing the key text.

    ``json.loads`` keeps the LAST value for a repeated key and says nothing, so
    ``{"contents": {...}, "contents": {"files": {}}}`` silently becomes an empty package that
    verifies clean. The hook is installed once and therefore fires for every nested object; a check
    written at the top level only would be a guard on the one shape someone happened to think of.
    """
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _ManifestError(CODE_MANIFEST_DUPLICATE_KEY)
        seen.add(key)
    return dict(pairs)


def _no_constants(_literal: str) -> object:
    """Refuse ``NaN``/``Infinity``/``-Infinity``, which Python accepts and JSON does not define."""
    raise _ManifestError(CODE_MANIFEST_NON_FINITE)


def _finite_float(literal: str) -> float:
    """Refuse a numeric literal that OVERFLOWS to infinity, which ``parse_constant`` never sees.

    ``1e999`` is a perfectly ordinary JSON number token, so it reaches ``parse_float`` rather than
    ``parse_constant`` and becomes ``inf``. Same non-round-tripping value, different door.
    """
    value = float(literal)
    if not math.isfinite(value):
        raise _ManifestError(CODE_MANIFEST_NON_FINITE)
    return value


def parse_manifest_text(text: str) -> dict[str, object]:
    """Strictly parse manifest text, or raise :class:`_ManifestError` with a stable code.

    ⚠️ ``_ManifestError`` is deliberately NOT a ``ValueError``: the strict hooks raise it from inside
    ``json.loads``, and if it shared an ancestor with ``JSONDecodeError`` the handler below would
    relabel a duplicate key or a NaN as "not parseable JSON" and lose the specific reason.
    """
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constants,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise _ManifestError(CODE_MANIFEST_NOT_JSON) from exc
    if not isinstance(parsed, dict):
        raise _ManifestError(CODE_MANIFEST_NOT_OBJECT)
    return parsed


def declared_files(manifest: dict[str, object]) -> dict[str, object]:
    """``contents.files``, with every containing type checked exactly.

    ⚠️ ``isinstance(x, dict)`` is the whole check on purpose: a list of pairs, a string, ``null`` and
    a number are all "not a mapping of path to digest", and treating any of them as an empty map
    would declare a package with no files - which then verifies clean if the package is empty and
    reports every real file as undeclared if it is not.
    """
    if "contents" not in manifest:
        raise _ManifestError(CODE_CONTENTS_MISSING)
    contents = manifest["contents"]
    if not isinstance(contents, dict):
        raise _ManifestError(CODE_CONTENTS_NOT_OBJECT)
    if "files" not in contents:
        raise _ManifestError(CODE_FILES_MISSING)
    files = contents["files"]
    if not isinstance(files, dict):
        raise _ManifestError(CODE_FILES_NOT_OBJECT)
    return files


# ---------------------------------------------------------------------------------------------
# Canonical package-relative keys
# ---------------------------------------------------------------------------------------------


def _segment_is_unsafe(segment: str) -> bool:
    """Whether one path segment is unusable as a portable, unambiguous package-relative name."""
    if segment in ("", ".", ".."):
        return True
    if segment != segment.strip():
        return True
    if segment.endswith(".") or segment.endswith(" "):
        # Windows silently strips both, so `report.json.` and `report.json` are one file there and
        # two everywhere else - an alias that lets one byte stream answer to two declarations.
        return True
    if any(ord(char) < 32 or ord(char) == 127 for char in segment):
        return True
    if ":" in segment:
        # A drive qualifier (`C:name`) or an NTFS alternate data stream (`file.txt:hidden`).
        return True
    stem = segment.split(".", 1)[0].strip().lower()
    return stem in _RESERVED_STEMS


def is_canonical_key(key: str) -> bool:
    """Whether a declared key is a canonical, package-relative POSIX path on EVERY host.

    Refused, each because it makes the same bytes answer to two names or reaches outside the package:
    a backslash (POSIX filename vs Windows separator - the SAME string means two things), a leading
    ``/`` (POSIX absolute), ``//`` (UNC), a drive qualifier or colon (also an NTFS stream), ``.`` and
    ``..``, an empty segment (``a//b`` or a trailing ``/``), a NUL or control character, a trailing
    dot or space, and a reserved Windows device name including its superscript-digit spellings.
    """
    if not key or "\\" in key or "\x00" in key:
        return False
    if key.startswith("/"):
        return False
    return not any(_segment_is_unsafe(segment) for segment in key.split("/"))


def alias_key(key: str) -> str:
    """The spelling a case-insensitive, trailing-punctuation-eating host would collapse this key to.

    Used only to DETECT collisions; never to repair one. Two keys sharing an alias are refused rather
    than merged, because the manifest then cannot say which bytes it meant.
    """
    segments = []
    for segment in key.split("/"):
        folded = segment.casefold().rstrip(". ")
        for superscript, digit in _SUPERSCRIPT_DIGITS.items():
            folded = folded.replace(superscript, digit)
        segments.append(folded)
    return "/".join(segments)


def _classify_keys(files: dict[str, object]) -> tuple[dict[str, object], list[Finding]]:
    """Split declared entries into usable ``{key: digest}`` and the findings that refuse the rest."""
    usable: dict[str, object] = {}
    rows: list[Finding] = []
    aliases: dict[str, str] = {}
    marker_alias = alias_key(PACKAGE_MARKER)
    for ordinal, (key, digest) in enumerate(files.items()):
        if not isinstance(key, str):
            rows.append(_finding(CODE_KEY_NOT_STRING, ordinal=ordinal))
            continue
        if not is_canonical_key(key):
            # Ordinal only. The unsafe spelling is precisely the string that must not be echoed into
            # a shared verdict, and printing it would also re-introduce the ambiguity it names.
            rows.append(_finding(CODE_KEY_UNSAFE, ordinal=ordinal))
            continue
        alias = alias_key(key)
        if alias == marker_alias:
            rows.append(_finding(CODE_KEY_ALIASES_MARKER, path=key))
            continue
        if alias in aliases:
            rows.append(_finding(CODE_KEY_COLLISION, path=key))
            continue
        aliases[alias] = key
        usable[key] = digest
    return usable, rows


def _digest_findings(usable: dict[str, object]) -> tuple[dict[str, str], list[Finding]]:
    """Keep only entries whose digest is exactly 64 lowercase hex characters."""
    digests: dict[str, str] = {}
    rows: list[Finding] = []
    for key, digest in usable.items():
        if not isinstance(digest, str):
            rows.append(_finding(CODE_DIGEST_NOT_STRING, path=key))
            continue
        if len(digest) != 64 or not set(digest) <= _HEX_DIGITS:
            # Uppercase is refused too: `contents.files` is compared by equality, and two spellings
            # of one digest is the same "which one did you mean" problem as two spellings of a path.
            rows.append(_finding(CODE_DIGEST_MALFORMED, path=key))
            continue
        digests[key] = digest
    return digests, rows


# ---------------------------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------------------------


def walk_package(root: Path) -> tuple[dict[str, Path], list[Finding], list[str]]:
    """Every regular file under ``root``, top-down, following nothing.

    Returns the package-relative POSIX path of each regular file mapped to the path the walk built
    (never one reconstructed from a manifest key), the findings for entries that are neither regular
    files nor directories, and the relative paths of directories that held no entries.

    ⚠️ **A reparse point is a finding AND a dead end, in that order and before any recursion.** A
    junction is reported (so a package that crosses one is never clean) and is not descended into (so
    the bytes on its far side are never listed, let alone opened). Descending first and judging
    afterwards would already have read outside the package.
    """
    files: dict[str, Path] = {}
    rows: list[Finding] = []
    empty_dirs: list[str] = []
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as entries:
                listed = sorted(entries, key=lambda entry: entry.name)
        except (OSError, ValueError):
            rows.append(_finding(CODE_DIRECTORY_UNREADABLE, path=prefix.rstrip("/") or "."))
            continue
        if not listed and prefix:
            empty_dirs.append(prefix.rstrip("/"))
        for entry in listed:
            relative = f"{prefix}{entry.name}"
            try:
                info = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                rows.append(_finding(CODE_ENTRY_UNASSESSABLE, path=relative))
                continue
            if is_reparse_entry(info):
                rows.append(_finding(CODE_ENTRY_REPARSE, path=relative))
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), f"{relative}/"))
                continue
            if not stat.S_ISREG(info.st_mode):
                rows.append(_finding(CODE_ENTRY_NOT_REGULAR, path=relative))
                continue
            files[relative] = Path(entry.path)
    return files, rows, empty_dirs


def _hash_file(path: Path) -> str | None:
    """The SHA-256 of a walked regular file, or ``None`` when it could not be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------


def _recheck_boundary(root: Path) -> list[Finding]:
    """Re-assert, with no-follow ``lstat``, that the root and its marker are what we were told.

    Defense in depth, deliberately duplicating :func:`bundle_corpus.classify_target`. The classifier
    ran earlier and its answer is now a value that has travelled; the bytes are opened HERE. A root
    or marker swapped in between is exactly the accident this module exists to notice, and the cost
    of noticing is two syscalls.
    """
    try:
        root_info = os.lstat(root)
    except (OSError, ValueError):
        return [_finding(CODE_ROOT_UNREADABLE)]
    if is_reparse_entry(root_info) or not stat.S_ISDIR(root_info.st_mode):
        return [_finding(CODE_ROOT_REPLACED)]
    try:
        marker_info = os.lstat(root / PACKAGE_MARKER)
    except FileNotFoundError:
        return [_finding(CODE_MARKER_REPLACED)]
    except (OSError, ValueError):
        return [_finding(CODE_MARKER_UNREADABLE)]
    if is_reparse_entry(marker_info) or not stat.S_ISREG(marker_info.st_mode):
        return [_finding(CODE_MARKER_REPLACED)]
    return []


def _read_manifest(root: Path) -> tuple[dict[str, object] | None, Finding | None]:
    """Read and strictly parse the root manifest, or return the single refusal that stopped it."""
    try:
        raw = (root / PACKAGE_MARKER).read_bytes()
    except (OSError, ValueError):
        return None, _finding(CODE_MANIFEST_UNREADABLE)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, _finding(CODE_MANIFEST_NOT_UTF8)
    try:
        return parse_manifest_text(text), None
    except _ManifestError as exc:
        return None, _finding(exc.code)


def _declared_digests(files: dict[str, object]) -> tuple[dict[str, str], list[Finding]]:
    """The usable ``{canonical key: 64-hex digest}`` pairs, plus the findings that refused the rest."""
    usable, key_rows = _classify_keys(files)
    digests, digest_rows = _digest_findings(usable)
    return digests, [*key_rows, *digest_rows]


def _walked_files(root: Path) -> tuple[dict[str, Path], list[Finding]]:
    """The walk's regular files and its findings, with the manifest itself removed.

    Empty directories are deliberately dropped here: :func:`walk_package` reports them because a
    caller may want to see them, but a directory holds no bytes, so it is neither declarable nor an
    extra file. The manifest is the ONLY excluded file - it carries the map.
    """
    walked, rows, _empty_dirs = walk_package(root)
    walked.pop(PACKAGE_MARKER, None)
    # Sorted so a verdict is byte-stable across hosts: the walk uses a stack, so its natural order
    # depends on directory ordering rather than on anything a reader can predict or diff.
    return walked, sorted(rows, key=lambda row: (row.path or "", row.code))


def verify_package(root: Path, classification: TargetClassification) -> PackageFilesystemResult:
    """Prove ``root``'s manifest describes exactly the regular files under it.

    ``classification`` is the verdict the caller ALREADY computed with
    :func:`bundle_corpus.classify_target`; this function never reclassifies and never resolves. A
    target that does not declare a package boundary is refused rather than silently verified, because
    "there is nothing to check here" and "I checked" must not share an answer.

    ⚠️ **Non-clean is returned, never raised.** Every failure mode - unreadable directory, unparsable
    manifest, unsafe key, missing file, changed byte - becomes a typed row. A verifier that raises on
    the interesting cases hands its caller a traceback carrying the very host paths this module
    refuses to print.
    """
    if not classification.declares_self_contained:
        return _result([_finding(CODE_NOT_A_DECLARED_PACKAGE)])

    boundary = _recheck_boundary(root)
    if boundary:
        return _result(boundary)

    manifest, refusal = _read_manifest(root)
    if manifest is None:
        return _result([refusal or _finding(CODE_MANIFEST_UNREADABLE)])

    try:
        files = declared_files(manifest)
    except _ManifestError as exc:
        return _result([_finding(exc.code)])

    digests, declared_rows = _declared_digests(files)
    walked, walk_rows = _walked_files(root)
    compare_rows, verified = _compare_declared_with_walked(digests, walked)
    rows = [*declared_rows, *walk_rows, *compare_rows]
    return _result(rows, files_declared=len(files), files_verified=verified)


def _compare_declared_with_walked(digests: dict[str, str], walked: dict[str, Path]) -> tuple[list[Finding], int]:
    """Exact set equality in both directions, then a rehash of every file present on both sides.

    ⚠️ **The path hashed is the one the WALK produced, never ``root / key``.** A manifest key is
    untrusted input, and joining it onto the root is how a verifier ends up reading a file the
    package does not contain - the walk has already proved each of these a regular, non-reparse
    entry inside the package.
    """
    rows = [_finding(CODE_FILE_UNDECLARED, path=name) for name in sorted(set(walked) - set(digests))]
    rows.extend(_finding(CODE_FILE_MISSING, path=name) for name in sorted(set(digests) - set(walked)))
    verified = 0
    for relative in sorted(set(digests) & set(walked)):
        actual = _hash_file(walked[relative])
        if actual is None:
            rows.append(_finding(CODE_FILE_UNREADABLE, path=relative))
            continue
        if actual != digests[relative]:
            rows.append(_finding(CODE_DIGEST_MISMATCH, path=relative))
            continue
        verified += 1
    return rows, verified
