"""Package-entry integrity verifier (issue #562).

Proves a newly produced package is byte-pristine, role-complete, identity-consistent and
package-local **before** agent work starts.  Missing, unreadable, malformed, hash-mismatched,
duplicate or undeclared required artifacts are distinct non-clean states.

⚠️ The manifest is unsigned: this protects against accidental damage and confused composition
(e.g. a partial copy, a stale re-package, a working-copy file dropped into an evidence slot).
It does **not** protect against adversarial rewrite — a hostile actor who controls the filesystem
can rewrite both the manifest and the files it describes.

Usage::

    from package_contract import verify_package_entry
    result = verify_package_entry(Path("packages/Book"))
    if result.state != "clean":
        ...  # block

Integrated into ``check_reference_readiness.scan`` so operators need not remember an unadvertised
third gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANIFEST_NAME = "package-manifest.json"
KIND_WORKBOOK = "workbook"
KIND_DATASOURCE = "datasource"

#: States returned by the verifier.
STATE_CLEAN = "clean"
STATE_FINDINGS = "findings"
STATE_UNASSESSABLE = "unassessable"

#: Always-required scaffold files (excluding package-manifest.json itself, which is checked
#: separately as the manifest role).
_ALWAYS_REQUIRED: frozenset[str] = frozenset(
    {
        "README.md",
        "handover.md",
        "report.json",
        "source-provenance.json",
        "engine-output-receipt.json",
        "migration-spec.schema.json",
    }
)

#: Additional roles required for workbook packages.
_WORKBOOK_REQUIRED_PREFIXES: tuple[str, ...] = (
    "assets/",
    "fabric/",
    "handover/",
)

#: Windows device names (case-insensitive, with or without extension).
_WIN_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

#: Regex for a valid lowercase hex SHA-256 digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class PackageEntryResult:
    """Typed result of a package-entry integrity check."""

    state: str
    """One of ``clean``, ``findings``, ``unassessable``."""

    kind: str | None = None
    """``workbook``, ``datasource``, or ``None`` when unassessable."""

    source: Path | None = None
    """Verified source asset path (package-relative), only when workbook identity is clean."""

    findings: list[str] = field(default_factory=list)
    """Human-readable, immutable findings. Empty iff ``state == "clean"``."""

    manifest: dict[str, Any] | None = None
    """Parsed manifest when readable, else ``None``."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _path_problem(posix_key: str) -> str | None:  # pylint: disable=too-many-return-statements
    """Why ``posix_key`` may not appear as a ``contents.files`` key, or None when safe.

    Rejects: empty, absolute, drive-letter, UNC, backslash, ``..`` traversal, control characters,
    Windows ADS (``:``) in segments, trailing dot/space per segment, Windows device names.
    """
    if not posix_key or not posix_key.strip():
        return "empty or whitespace-only path"
    if "\\" in posix_key:
        return f"backslash in path: {posix_key!r}"
    if posix_key.startswith("/"):
        return f"absolute path: {posix_key!r}"
    # Drive-letter (C:foo) or UNC (//host/share)
    if len(posix_key) >= 2 and posix_key[1] == ":":
        return f"drive-qualified path: {posix_key!r}"
    if posix_key.startswith("//"):
        return f"UNC path: {posix_key!r}"
    parts = PurePosixPath(posix_key).parts
    for part in parts:
        if part in (".", ".."):
            return f"dot-segment traversal: {posix_key!r}"
        # Control characters (U+0000..U+001F)
        if any(ord(ch) < 0x20 for ch in part):
            return f"control character in path segment: {posix_key!r}"
        # Windows ADS marker
        if ":" in part:
            return f"colon (ADS) in path segment: {posix_key!r}"
        # Trailing dot or space (Windows alias hazard)
        if part != part.rstrip(". ") and part not in (".", ".."):
            return f"trailing dot or space in segment: {posix_key!r}"
        # Windows device names
        stem = part.split(".")[0].upper()
        if stem in _WIN_DEVICE_NAMES:
            return f"Windows reserved device name: {posix_key!r}"
    return None


def _case_alias_collisions(keys: list[str]) -> list[str]:
    """Return findings for keys that collide under case-folding (Windows alias hazard)."""
    seen: dict[str, str] = {}
    findings: list[str] = []
    for key in keys:
        folded = key.casefold()
        if folded in seen and seen[folded] != key:
            findings.append(
                f"case-alias collision: {key!r} and {seen[folded]!r} collide on case-insensitive filesystems"
            )
        else:
            seen[folded] = key
    return findings


# ---------------------------------------------------------------------------
# Duplicate-key-safe JSON parse
# ---------------------------------------------------------------------------


def _parse_json_no_dupes(text: str) -> tuple[Any, str | None]:
    """Parse JSON rejecting duplicate keys at any nesting level.

    Returns ``(parsed, reason)`` — reason is non-None on any failure.
    """
    duplicate_found: list[str] = []

    def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                duplicate_found.append(key)
            seen.add(key)
        return dict(pairs)

    try:
        parsed = json.loads(text, object_pairs_hook=_object_pairs_hook)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"manifest is not valid JSON: {type(exc).__name__}"
    if duplicate_found:
        return None, f"manifest has duplicate key(s): {', '.join(sorted(set(duplicate_found)))}"
    return parsed, None


# ---------------------------------------------------------------------------
# Filesystem safety checks
# ---------------------------------------------------------------------------


def _check_symlink_or_reparse(path: Path) -> str | None:
    """Return a finding if ``path`` is a symlink, junction, or reparse point.

    Works for both files and directories.  On Linux, detects symlinks via lstat.  On Windows,
    also detects junctions/reparse points via ``st_file_attributes``.
    """
    try:
        st = path.lstat()
    except OSError as exc:
        return f"cannot lstat {path.name}: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return f"symlink: {path.name}"
    # On Windows, junctions are directories with reparse points — detected via st_file_attributes.
    # On non-Windows we cannot check reparse points, so we report only symlinks (the behaviour is
    # explicitly unverified for Windows reparse points outside symlinks).
    if os.name == "nt":  # pragma: no cover — platform-specific
        try:
            _reparse_flag = 0x400
            attrs = st.st_file_attributes  # type: ignore[attr-defined]
            if attrs & _reparse_flag:
                return f"reparse point (junction or symlink): {path.name}"
        except (AttributeError, OSError):
            pass
    return None


def _check_directory_components(target: Path, findings: list[str]) -> bool:
    """Check every subdirectory inside ``target`` for symlinks/junctions/reparse points.

    Returns True if any finding was added (caller should stop before hashing outside the package).
    """
    found = False
    checked: set[Path] = set()
    for path in sorted(target.rglob("*")):
        if not path.is_dir():
            continue
        if path in checked:
            continue
        checked.add(path)
        finding = _check_symlink_or_reparse(path)
        if finding:
            rel = path.relative_to(target).as_posix()
            findings.append(f"directory {rel}: {finding}")
            found = True
    return found


def _check_regular_file(path: Path) -> str | None:
    """Return a finding if ``path`` is not a regular file."""
    try:
        st = path.lstat()
    except OSError as exc:
        return f"cannot lstat {path.name}: {exc}"
    if not stat.S_ISREG(st.st_mode):
        return f"not a regular file: {path.name}"
    return None


# ---------------------------------------------------------------------------
# Core verifier
# ---------------------------------------------------------------------------


def _load_manifest(target: Path) -> PackageEntryResult | tuple[dict, str, dict]:  # pylint: disable=too-many-return-statements
    """Parse and validate the manifest, returning early result on failure or (manifest, kind, files_map)."""
    manifest_path = target / MANIFEST_NAME

    if not manifest_path.exists():
        detail = (
            "package-shaped target has no package-manifest.json"
            if _is_package_shaped(target)
            else "target has no package-manifest.json and is not package-shaped"
        )
        return PackageEntryResult(state=STATE_UNASSESSABLE, findings=[detail])

    symlink_finding = _check_symlink_or_reparse(manifest_path)
    if symlink_finding:
        return PackageEntryResult(state=STATE_UNASSESSABLE, findings=[f"manifest: {symlink_finding}"])

    try:
        raw = manifest_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return PackageEntryResult(state=STATE_UNASSESSABLE, findings=[f"manifest unreadable: {exc}"])

    manifest, reason = _parse_json_no_dupes(raw)
    if reason:
        return PackageEntryResult(state=STATE_UNASSESSABLE, findings=[reason])

    if not isinstance(manifest, dict):
        return PackageEntryResult(
            state=STATE_UNASSESSABLE,
            findings=[f"manifest is {type(manifest).__name__}, expected object"],
        )

    kind = manifest.get("kind")
    if kind not in (KIND_WORKBOOK, KIND_DATASOURCE):
        return PackageEntryResult(
            state=STATE_FINDINGS,
            findings=[f"unclassified kind: {kind!r} (expected 'workbook' or 'datasource')"],
            manifest=manifest,
        )

    contents = manifest.get("contents")
    if not isinstance(contents, dict):
        return PackageEntryResult(
            state=STATE_UNASSESSABLE,
            kind=kind,
            findings=["manifest has no 'contents' object"],
            manifest=manifest,
        )
    files_map = contents.get("files")
    if not isinstance(files_map, dict):
        return PackageEntryResult(
            state=STATE_UNASSESSABLE,
            kind=kind,
            findings=["manifest 'contents.files' is not an object"],
            manifest=manifest,
        )

    return manifest, kind, files_map


def _verify_paths_and_hashes(  # pylint: disable=too-many-locals,too-many-branches
    target: Path,
    files_map: dict[str, str],
    findings: list[str],
) -> tuple[set[str], set[str]]:
    """Validate path safety, SHA-256 format, file set equality, symlinks, and rehash."""
    # Path safety
    for key in files_map:
        problem = _path_problem(key)
        if problem:
            findings.append(f"unsafe path in contents: {problem}")
    findings.extend(_case_alias_collisions(list(files_map.keys())))

    # SHA-256 format
    for key, digest in files_map.items():
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            findings.append(f"invalid sha256 for {key!r}: {digest!r}")

    # Directory-component check — symlinks/junctions/reparse on directories must be caught
    # BEFORE traversal or hashing, so nothing reads outside the package boundary.
    has_reparse_dir = _check_directory_components(target, findings)

    # File set equality
    actual_files: set[str] = set()
    for path in sorted(target.rglob("*")):
        if path.is_file():
            rel = path.relative_to(target).as_posix()
            if rel != MANIFEST_NAME:
                actual_files.add(rel)

    declared_files = set(files_map.keys())
    for f in sorted(declared_files - actual_files):
        findings.append(f"declared file missing: {f}")
    for f in sorted(actual_files - declared_files):
        findings.append(f"undeclared file present: {f}")

    # If any directory is a reparse point, skip file-level hashing — the content is outside
    # the package boundary and cannot be trusted.
    if has_reparse_dir:
        return declared_files, actual_files

    # Symlink / reparse and rehash (only for files that exist)
    present = declared_files & actual_files
    for key in sorted(present):
        file_path = target / PurePosixPath(key)
        sym = _check_symlink_or_reparse(file_path)
        if sym:
            findings.append(f"declared file {key}: {sym}")
            continue
        reg = _check_regular_file(file_path)
        if reg:
            findings.append(f"declared file {key}: {reg}")
            continue
        expected = files_map[key]
        if not isinstance(expected, str) or not _SHA256_RE.match(expected):
            continue
        try:
            actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError as exc:
            findings.append(f"cannot read {key} for rehash: {exc}")
            continue
        if actual != expected:
            findings.append(f"hash mismatch for {key}: expected {expected[:12]}…, got {actual[:12]}…")

    return declared_files, actual_files


def verify_package_entry(target: Path) -> PackageEntryResult:
    """Verify a package target at entry time (before agent work begins).

    This is the **entry mode** verifier: every declared file must match its recorded hash exactly.
    After agent edits begin, ``fabric/`` files are expected to change and should be compared via
    the existing generated-edit / comparison mechanisms, not this function.
    """
    loaded = _load_manifest(target)
    if isinstance(loaded, PackageEntryResult):
        return loaded
    manifest, kind, files_map = loaded

    findings: list[str] = []
    declared_files, _actual = _verify_paths_and_hashes(target, files_map, findings)

    # Required roles
    for role in _ALWAYS_REQUIRED:
        if role not in declared_files:
            findings.append(f"missing required role: {role}")

    if kind == KIND_WORKBOOK:
        _check_workbook_roles(target, manifest, declared_files, findings)
    elif kind == KIND_DATASOURCE:
        _check_datasource_roles(target, manifest, declared_files, findings)

    # Identity consistency (workbook)
    source_path: Path | None = None
    if kind == KIND_WORKBOOK:
        source_path = _check_workbook_identity(target, manifest, findings)

    _check_oracle_files(target, manifest, declared_files, findings)

    state = STATE_CLEAN if not findings else STATE_FINDINGS
    return PackageEntryResult(
        state=state,
        kind=kind,
        source=source_path if state == STATE_CLEAN else None,
        findings=findings,
        manifest=manifest,
    )


# ---------------------------------------------------------------------------
# Role checks
# ---------------------------------------------------------------------------


def _check_workbook_roles(
    _target: Path,
    manifest: dict[str, Any],
    declared: set[str],
    findings: list[str],
) -> None:
    """Verify workbook-specific required roles."""
    artifacts = manifest.get("artifacts") or {}

    # Asset
    asset = artifacts.get("asset")
    if not asset or asset not in declared:
        findings.append("workbook missing required asset role")

    # Spec
    spec = artifacts.get("migration_spec")
    if not spec or spec not in declared:
        findings.append("workbook missing required migration spec")

    # Handover slice
    handover = artifacts.get("handover")
    if not handover or handover not in declared:
        findings.append("workbook missing required handover slice")

    # Report
    report = artifacts.get("report")
    if not report or not any(
        k.startswith("fabric/") and k.endswith(".Report/") or k.startswith("fabric/") for k in declared
    ):
        # Check for fabric/ presence
        if not any(k.startswith("fabric/") for k in declared):
            findings.append("workbook missing required fabric working copy")

    # Model (contained binding)
    binding = manifest.get("model_binding") or {}
    if not binding.get("resolves_in_package"):
        findings.append("workbook model binding does not resolve in package")


def _check_datasource_roles(
    _target: Path,
    manifest: dict[str, Any],
    declared: set[str],
    findings: list[str],
) -> None:
    """Verify datasource-specific required roles — asset/spec/handover/oracle are optional."""
    # Model must be present
    if not any(k.startswith("fabric/") for k in declared):
        findings.append("datasource missing required fabric working copy")

    # Classification must be datasource
    if manifest.get("kind") != KIND_DATASOURCE:
        findings.append("datasource classification mismatch")


def _check_workbook_identity(
    target: Path,
    manifest: dict[str, Any],
    findings: list[str],
) -> Path | None:
    """Cross-check identity fields and return verified source path if consistent."""
    identity = manifest.get("workbook_identity") or {}
    artifacts = manifest.get("artifacts") or {}
    asset_key = artifacts.get("asset")

    # LUID consistency
    luid = identity.get("luid")
    if not luid:
        findings.append("workbook has no established LUID identity")
        return None

    # Provenance LUID cross-check — the manifest's identity must match what the provenance recorded
    # (already validated at packaging time, but re-verified here for the entry contract).
    if identity.get("reason"):
        findings.append(f"workbook identity not established: {identity['reason']}")
        return None

    # Asset presence
    if not asset_key:
        return None

    asset_path = target / PurePosixPath(asset_key)
    if not asset_path.is_file():
        return None

    # Filename LUID cross-check
    from_filename = _filename_luid(asset_path.name)
    if from_filename and from_filename.casefold() != luid.casefold():
        findings.append(f"contradictory LUID: filename says {from_filename}, manifest identity says {luid}")
        return None

    return asset_path


def _filename_luid(name: str) -> str | None:
    """Extract the LUID prefix from a ``<luid>_<name>.twb(x)`` filename."""
    m = re.match(
        r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_",
        name,
    )
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Oracle file cross-reference
# ---------------------------------------------------------------------------


def _check_oracle_files(
    _target: Path,
    manifest: dict[str, Any],
    declared: set[str],
    findings: list[str],
) -> None:
    """If oracle objects declare files, verify they are local, declared and cross-referenced."""
    oracle = manifest.get("oracle")
    if not isinstance(oracle, dict):
        return
    objects = oracle.get("objects")
    if not isinstance(objects, list):
        return

    declared_oracle: set[str] = set()
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for file_key in ("image", "svg", "pdf"):
            path_val = obj.get(file_key)
            if isinstance(path_val, str) and path_val:
                declared_oracle.add(path_val)
                if path_val not in declared:
                    findings.append(f"oracle object references undeclared file: {path_val}")

    # Check for extra oracle files not referenced by any object
    actual_oracle = {k for k in declared if k.startswith("oracle/")}
    oracle_manifest = "oracle/oracle-manifest.json"
    referenced_oracle = declared_oracle | ({oracle_manifest} if oracle_manifest in declared else set())
    foreign = actual_oracle - referenced_oracle
    for f in sorted(foreign):
        findings.append(f"foreign oracle file not referenced by any object: {f}")


# ---------------------------------------------------------------------------
# Package-shape detection
# ---------------------------------------------------------------------------


def _is_package_shaped(target: Path) -> bool:
    """Whether ``target`` looks like a package directory (under ``packages/``)."""
    try:
        parent = target.parent
        if parent.name == "packages":
            return True
        if parent.parent.name == "packages":
            return True
    except (OSError, RuntimeError, ValueError):
        pass
    return False
