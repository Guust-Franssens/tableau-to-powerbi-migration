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
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
CODE_ROOT_BINDING = "package_root_binding_invalid"
CODE_MEMBER_UNVERIFIED = "package_member_not_verified"
CODE_MEMBER_REPLACED = "package_member_replaced"

#: Generic wording per code. ASCII only: these reach a Windows console, whose default code page
#: cannot encode the arrows and warning glyphs the docstrings use.
_DETAILS = {
    CODE_NOT_A_DECLARED_PACKAGE: (
        "this target does not declare a package boundary, so its contents cannot be verified here"
    ),
    CODE_ROOT_REPLACED: "the package root is no longer the original plain directory",
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
        "escaping, device-reserved, ambiguously separated, or holds a character no Windows filename "
        "may contain)"
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
    CODE_ROOT_BINDING: "the verified namespace belongs to a different exact package root",
    CODE_MEMBER_UNVERIFIED: "the requested member has no exact verified declaration",
    CODE_MEMBER_REPLACED: "a verified member no longer has its original regular-file identity",
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

#: Characters no Windows filename may contain. A declared key holding one of them is a path that
#: **cannot be created on half the hosts this toolkit runs on**, so a package carrying it is either
#: not the package it claims to be or is unpackable there - either way it is not describable.
#:
#: Each earns its place rather than being copied from a list:
#:
#: * ``:`` is a drive qualifier (``C:name``) or an NTFS alternate data stream
#:   (``report.json:hidden``) - the second is bytes hiding behind a name that looks declared;
#: * ``?`` and ``*`` are wildcards, so ONE declared key would match many files on any host that
#:   expands them, and "which bytes did the manifest mean" stops having an answer;
#: * ``<``, ``>`` and ``|`` are shell redirection/pipe operators, which is how a key ends up
#:   interpreted rather than opened by whatever consumes the manifest next;
#: * ``"`` terminates the quoting of every diagnostic, command line and JSON string this key travels
#:   through.
#:
#: ⚠️ POSIX would accept all six in a filename, which is exactly why they are refused: the rule is
#: "canonical on EVERY host", not "legal on the host that happens to be reading".
_FORBIDDEN_SEGMENT_CHARS = frozenset('<>:"|?*')

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
class VerifiedFile:
    """One S1-verified name, digest and walk-produced file identity; no asset bytes are retained."""

    relative_path: str
    sha256: str
    path: Path = field(repr=False)
    file_identity: tuple[int, int, int] = field(repr=False)


@dataclass(frozen=True)
class HeldVerifiedMember:
    """Immutable bytes from one exact S1 namespace. Not a serialized diagnostic or a live path."""

    relative_path: str
    sha256: str
    content: bytes = field(repr=False)
    root_identity: str = field(repr=False)


@dataclass(frozen=True)
class PackageFilesystemResult:  # pylint: disable=too-many-instance-attributes
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
    root_identity: str | None = field(default=None, repr=False, compare=False)
    verified_files: tuple[VerifiedFile, ...] = field(default=(), repr=False, compare=False)
    manifest: HeldVerifiedMember | None = field(default=None, repr=False, compare=False)
    boundary_identity: tuple[tuple[int, ...], ...] = field(default=(), repr=False, compare=False)
    _authority: Callable[[object], bool] | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def is_clean(self) -> bool:
        """``True`` only when the manifest exactly describes the bytes on disk."""
        return self.status == STATUS_CLEAN

    def has_read_authority(self) -> bool:
        """Only the original, internally unchanged verification can authorize held reads.

        An in-process ownership check, not a signature or protection against arbitrary Python code.
        """
        try:
            return self._authority is not None and self._authority(self)
        except (AttributeError, TypeError):
            return False

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


def _read_authority_state(result: PackageFilesystemResult) -> tuple:
    """Capture immutable metadata values, not another copy of the package's bytes."""
    manifest = result.manifest
    return (
        result.status,
        result.findings,
        result.unassessable,
        result.files_declared,
        result.files_verified,
        result.root_identity,
        tuple((row.relative_path, row.sha256, str(row.path), row.file_identity) for row in result.verified_files),
        (manifest.relative_path, manifest.sha256, manifest.root_identity) if manifest is not None else None,
        result.boundary_identity,
    )


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
    if any(char in _FORBIDDEN_SEGMENT_CHARS for char in segment):
        return True
    stem = segment.split(".", 1)[0].strip().lower()
    return stem in _RESERVED_STEMS


def is_canonical_key(key: str) -> bool:
    """Whether a declared key is a canonical, package-relative POSIX path on EVERY host.

    Refused, each because it makes the same bytes answer to two names or reaches outside the package:
    a backslash (POSIX filename vs Windows separator - the SAME string means two things), a leading
    ``/`` (POSIX absolute), ``//`` (UNC), a leading ``/`` on a relative spelling, ``.`` and ``..``, an
    empty segment (``a//b`` or a trailing ``/``), a NUL or control character, any character Windows
    cannot put in a filename (:func:`_FORBIDDEN_SEGMENT_CHARS`), a trailing dot or space, and a
    reserved Windows device name including its superscript-digit spellings.
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
    return _walk_package(root)


def _walk_package(root: Path) -> tuple[dict[str, Path], list[Finding], list[str]]:
    """Shared no-follow traversal for published namespaces and later freshness checks."""
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
    return _boundary_identity(root)[1]


def _file_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_nlink


def _boundary_identity(root: Path) -> tuple[tuple[tuple[int, ...], ...], list[Finding]]:
    """The original root/marker identities, checked without following either entry."""
    try:
        root_info = os.lstat(root)
    except (OSError, ValueError):
        return (), [_finding(CODE_ROOT_UNREADABLE)]
    if is_reparse_entry(root_info) or not stat.S_ISDIR(root_info.st_mode):
        return (), [_finding(CODE_ROOT_REPLACED)]
    try:
        marker_info = os.lstat(root / PACKAGE_MARKER)
    except FileNotFoundError:
        return (), [_finding(CODE_MARKER_REPLACED)]
    except (OSError, ValueError):
        return (), [_finding(CODE_MARKER_UNREADABLE)]
    if is_reparse_entry(marker_info) or not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink != 1:
        return (), [_finding(CODE_MARKER_REPLACED)]
    return ((root_info.st_dev, root_info.st_ino), _file_identity(marker_info)), []


def _read_manifest(root: Path) -> tuple[dict[str, object] | None, bytes | None, Finding | None]:
    """Read and strictly parse the root manifest, or return the single refusal that stopped it."""
    try:
        raw = (root / PACKAGE_MARKER).read_bytes()
    except (OSError, ValueError):
        return None, None, _finding(CODE_MANIFEST_UNREADABLE)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, _finding(CODE_MANIFEST_NOT_UTF8)
    try:
        return parse_manifest_text(text), raw, None
    except _ManifestError as exc:
        return None, None, _finding(exc.code)


def _declared_digests(files: dict[str, object]) -> tuple[dict[str, str], list[Finding]]:
    """The usable ``{canonical key: 64-hex digest}`` pairs, plus the findings that refused the rest."""
    usable, key_rows = _classify_keys(files)
    digests, digest_rows = _digest_findings(usable)
    return digests, [*key_rows, *digest_rows]


def _walked_files(root: Path, *, publish: bool = False) -> tuple[dict[str, Path], list[Finding]]:
    """The walk's regular files and its findings, with the manifest itself removed.

    Empty directories are deliberately dropped here: :func:`walk_package` reports them because a
    caller may want to see them, but a directory holds no bytes, so it is neither declarable nor an
    extra file. The manifest is the ONLY excluded file - it carries the map.

    Only namespace creation publishes Path objects through `walk_package`. Freshness checks share
    the traversal implementation without publishing replacement objects for S1's retained paths.
    """
    walked, rows, _empty_dirs = walk_package(root) if publish else _walk_package(root)
    walked.pop(PACKAGE_MARKER, None)
    # Sorted so a verdict is byte-stable across hosts: the walk uses a stack, so its natural order
    # depends on directory ordering rather than on anything a reader can predict or diff.
    return walked, sorted(rows, key=lambda row: (row.path or "", row.code))


def verify_package(  # pylint: disable=too-many-locals,too-many-return-statements
    root: Path, classification: TargetClassification
) -> PackageFilesystemResult:
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

    identity, boundary = _boundary_identity(root)
    if boundary:
        return _result(boundary)

    manifest, raw_manifest, refusal = _read_manifest(root)
    if manifest is None:
        return _result([refusal or _finding(CODE_MANIFEST_UNREADABLE)])

    try:
        files = declared_files(manifest)
    except _ManifestError as exc:
        return _result([_finding(exc.code)])

    digests, declared_rows = _declared_digests(files)
    walked, walk_rows = _walked_files(root, publish=True)
    members: list[VerifiedFile] = []
    compare_rows, verified = _compare_declared_with_walked(digests, walked, members=members)
    rows = [*declared_rows, *walk_rows, *compare_rows]
    result = _result(rows, files_declared=len(files), files_verified=verified)
    if not result.is_clean:
        return result
    current, boundary = _boundary_identity(root)
    if boundary or current != identity:
        return _result(boundary or [_finding(CODE_ROOT_REPLACED)])
    result = replace(
        result,
        root_identity=str(root),
        boundary_identity=identity,
        verified_files=tuple(members),
        manifest=HeldVerifiedMember(PACKAGE_MARKER, hashlib.sha256(raw_manifest).hexdigest(), raw_manifest, str(root)),
    )
    owner = weakref.ref(result)
    state = _read_authority_state(result)
    object.__setattr__(
        result, "_authority", lambda candidate: owner() is candidate and _read_authority_state(candidate) == state
    )
    return result


def _compare_declared_with_walked(
    digests: dict[str, str], walked: dict[str, Path], *, members: list[VerifiedFile] | None = None
) -> tuple[list[Finding], int]:
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
        info, refusal = _regular_identity(walked[relative])
        if refusal:
            rows.append(_finding(refusal, path=relative))
            continue
        actual = _hash_file(walked[relative])
        if actual is None:
            rows.append(_finding(CODE_FILE_UNREADABLE, path=relative))
            continue
        if actual != digests[relative]:
            rows.append(_finding(CODE_DIGEST_MISMATCH, path=relative))
            continue
        verified += 1
        if members is not None:
            members.append(VerifiedFile(relative, digests[relative], walked[relative], info))
    return rows, verified


def _regular_identity(path: Path) -> tuple[tuple[int, int, int] | None, str | None]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, CODE_FILE_MISSING
    except (OSError, ValueError):
        return None, CODE_ENTRY_UNASSESSABLE
    if is_reparse_entry(info):
        return None, CODE_ENTRY_REPARSE
    if not stat.S_ISREG(info.st_mode):
        return None, CODE_ENTRY_NOT_REGULAR
    if info.st_nlink != 1:
        return None, CODE_MEMBER_REPLACED
    return _file_identity(info), None


def member_refusal(code: str = CODE_MEMBER_UNVERIFIED) -> PackageFilesystemResult:
    """A typed held-read refusal with no path, artifact text or exception attached."""
    return _result([_finding(code)])


def read_verified_member(  # pylint: disable=too-many-return-statements,too-many-branches
    root: Path, verified: PackageFilesystemResult, relative_path: str
) -> HeldVerifiedMember | PackageFilesystemResult:
    """Read one canonical member ONCE against S1's original namespace and digest, never a new manifest.

    Only the requested member and manifest are rehashed; unrelated content requires a new S1 check.
    Namespace/identity checks still refuse extra, missing, aliased or replaced members. Copies and
    reconstructions carry no originating read authority. This is not cryptographic security or a
    handle-level concurrency guarantee; S1's unsigned-manifest and lstat/open race limits remain.
    """
    if (
        type(verified) is not PackageFilesystemResult  # pylint: disable=unidiomatic-typecheck
        or type(root) is not type(Path())  # pylint: disable=unidiomatic-typecheck
        or type(verified.root_identity) is not str  # pylint: disable=unidiomatic-typecheck
        or str(root) != verified.root_identity
    ):
        return member_refusal(CODE_ROOT_BINDING)
    if not verified.has_read_authority():
        return member_refusal()
    if not verified.is_clean or verified.manifest is None or not verified.boundary_identity:
        return member_refusal(verified.first_code or CODE_MEMBER_UNVERIFIED)
    if (
        not isinstance(verified.manifest.content, bytes)
        or hashlib.sha256(verified.manifest.content).hexdigest() != verified.manifest.sha256
    ):
        return member_refusal(CODE_DIGEST_MISMATCH)
    members = {member.relative_path: member for member in verified.verified_files}
    if type(relative_path) is not str or relative_path not in members:  # pylint: disable=unidiomatic-typecheck
        return member_refusal()
    boundary, rows = _boundary_identity(root)
    if rows or boundary != verified.boundary_identity:
        return member_refusal(rows[0].code if rows else CODE_ROOT_REPLACED)
    walked, rows = _walked_files(root)
    if rows:
        return member_refusal(rows[0].code)
    if set(walked) != set(members):
        return member_refusal(CODE_FILE_UNDECLARED if set(walked) - set(members) else CODE_FILE_MISSING)
    for key, member in members.items():
        identity, refusal = _regular_identity(walked[key])
        if refusal or str(walked[key]) != str(member.path) or identity != member.file_identity:
            return member_refusal(refusal or CODE_MEMBER_REPLACED)
    if _hash_file(root / PACKAGE_MARKER) != verified.manifest.sha256:
        return member_refusal(CODE_DIGEST_MISMATCH)
    member = members[relative_path]
    try:
        raw = walked[relative_path].read_bytes()
    except FileNotFoundError:
        return member_refusal(CODE_FILE_MISSING)
    except (OSError, ValueError):
        return member_refusal(CODE_FILE_UNREADABLE)
    if hashlib.sha256(raw).hexdigest() != member.sha256:
        return member_refusal(CODE_DIGEST_MISMATCH)
    current, rows = _boundary_identity(root)
    identity, refusal = _regular_identity(walked[relative_path])
    if rows or current != boundary:
        return member_refusal(rows[0].code if rows else CODE_ROOT_REPLACED)
    if refusal or identity != member.file_identity:
        return member_refusal(refusal or CODE_MEMBER_REPLACED)
    return HeldVerifiedMember(relative_path, member.sha256, raw, verified.root_identity)
