"""
purpose: enforce the live-source credential gate at the FILESYSTEM level, so an unvalidated semantic
         model physically cannot be written, and record every gate decision for audit.
usage:   python scripts/credential_gate.py status  <migration-dir>
         python scripts/credential_gate.py list    <estate-root> [--json]
         python scripts/credential_gate.py block   <migration-dir> --sources "a" "b"
         python scripts/credential_gate.py clear    <migration-dir> --reason probe-data-ok
         python scripts/credential_gate.py verify   <migration-dir>

The kernel ACL stops ordinary writes regardless of tool or command spelling; prose and hooks alone
were bypassed in measured migrations. This is NOT a sandbox: the same OS user can remove an ACL or
forge a complete audit. Verification detects stripped enforcement and unaudited overrides, but
source-system query history remains the independent oracle for a claimed probe.

Engine receipts have the same accountability-only threat model. Exact path/size/hash matches
distinguish deterministic pre-gate output from subsequent edits; they do not confer live validation
or cryptographic non-repudiation.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import platform
import re
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import NamedTuple

from bundle_corpus import is_reparse_entry
from migration_bundle import ENGINE_OUTPUT_DIRS, ENGINE_RECEIPT, is_engine_artifact, load_bundle, sha256_file
from package_filesystem import is_canonical_key

# Imported as a plain NAME, not reached through the module (`preflight_source_credentials._classify_legs`
# is `protected-access` to pylint, W0212). This is the SAME canonical classifier
# (`connection_target.powerbi_target`) that arms the gate in the first place; issue #354's review
# explicitly required reusing it here rather than a second, independently-maintained opinion.
from preflight_source_credentials import _classify_legs

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("credential_gate")

MARKER = ".credential-gate-BLOCKED.json"
OVERRIDE = ".credential-gate-AUTHORIZED"
AUDIT = ".credential-gate-audit.log"

TX_PREFIX = ".credential-gate-tx-v1-"
TX_WRITER_TIMEOUT = 10.0
TX_READER_TIMEOUT = 2.0
TX_HOOK_TIMEOUT = 0.5
_TRANSACTIONS: ContextVar[tuple] = ContextVar("gate_transactions", default=())
_COORDINATION: ContextVar[tuple] = ContextVar("gate_coordination", default=())
_VIEWS: ContextVar[tuple] = ContextVar("gate_views", default=())


class GateTransactionError(OSError):
    """A closed coordination refusal; never include private paths or syscall messages."""


class CoordinationRoot(NamedTuple):
    """Immutable final address, independent of a package generation's transient name."""

    parent: Path
    leaf: str
    parent_identity: tuple[int, int]
    canonical: str
    companion: Path


def _identity(info: os.stat_result) -> tuple[int, int]:
    if not info.st_ino:
        raise GateTransactionError("transaction-identity-unproved")
    return info.st_dev, info.st_ino


def _safe_entry(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if is_reparse_entry(info) or not kind(info.st_mode) or (not directory and info.st_nlink != 1):
        raise GateTransactionError("transaction-unsafe-entry")
    return info


def _windows_directory(path: Path) -> tuple[str, tuple[int, int]]:  # pylint: disable=too-many-locals
    """Open the directory itself, not its reparse target, and ask the OS for its final spelling."""
    import ctypes  # pylint: disable=import-outside-toplevel
    import msvcrt  # pylint: disable=import-outside-toplevel,import-error
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes = [wintypes.LPCWSTR]
    drive_type.restype = wintypes.UINT
    if drive_type(path.anchor) == 4:
        raise GateTransactionError("transaction-alias-unproved")
    for ancestor in (path, *path.parents):
        _safe_entry(ancestor, directory=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0, 7, None, 3, 0x02000000 | 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise GateTransactionError("transaction-parent-open")
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    try:
        before = _safe_entry(path, directory=True)
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise GateTransactionError("transaction-parent-changed")
        final_name = kernel.GetFinalPathNameByHandleW
        final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_name.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        # Volume GUID spelling collapses drive-letter/subst aliases. Unproved UNC aliases refuse.
        count = final_name(handle, buffer, len(buffer), 1)
        if not count:
            raise GateTransactionError("transaction-alias-unproved")
        if not count or count >= len(buffer):
            raise GateTransactionError("transaction-parent-path")
        flags = wintypes.DWORD()
        query = kernel.GetFileInformationByHandleEx
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        if not query(handle, 23, ctypes.byref(flags), ctypes.sizeof(flags)):
            raise GateTransactionError("transaction-case-unproved")
        if flags.value & 1:
            raise GateTransactionError("transaction-case-sensitive-parent")
        return buffer.value.casefold(), _identity(opened)
    finally:
        os.close(descriptor)


def _opened_parent(path: Path) -> tuple[str, tuple[int, int]]:
    if os.name == "nt" and str(path).startswith("\\\\"):
        raise GateTransactionError("transaction-alias-unproved")
    if os.name == "nt":
        return _windows_directory(path)
    for ancestor in (path, *path.parents):
        _safe_entry(ancestor, directory=True)
    before = _safe_entry(path, directory=True)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if _identity(before) != _identity(os.fstat(descriptor)):
            raise GateTransactionError("transaction-parent-changed")
        identity = _identity(before)
        return f"posix-directory:{identity[0]}:{identity[1]}", identity
    finally:
        os.close(descriptor)


def _root_path(root: Path) -> Path:
    """Do not erase a reparse component through lexical ``..`` normalization."""
    original = Path(root)
    if not original.is_absolute():
        original = Path.cwd() / original
    if os.name == "nt" and str(original).startswith("\\\\"):
        raise GateTransactionError("transaction-alias-unproved")
    if ".." in original.parts:
        for ancestor in reversed((original.parent, *original.parent.parents)):
            _safe_entry(ancestor, directory=True)
    return Path(os.path.abspath(original))


def coordination_root(root: Path) -> CoordinationRoot:
    """Canonical opened existing parent plus final leaf; refuse aliases we cannot prove."""
    root = _root_path(root)
    canonical_parent, identity = _opened_parent(root.parent)
    leaf = root.name
    if not leaf or leaf in (".", ".."):
        raise GateTransactionError("transaction-root-invalid")
    if os.path.lexists(root):
        _safe_entry(root, directory=True)
    if os.name == "nt":
        if leaf.rstrip(". ") != leaf or ":" in leaf or "~" in leaf:
            raise GateTransactionError("transaction-alias-unproved")
        leaf = leaf.casefold()
    canonical = f"credential-gate/transaction/v1\0{canonical_parent}\0{identity[0]}:{identity[1]}\0{leaf}"
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
    return CoordinationRoot(root.parent, leaf, identity, canonical, root.parent / f"{TX_PREFIX}{digest}.lock")


class GateTransaction:
    """One persistent descriptor, released by close/process death, never by unlink."""

    def __init__(self, authority: CoordinationRoot, timeout: float) -> None:
        self.authority = authority
        self.timeout = timeout
        self.descriptor = -1

    def check(self) -> None:
        """Detect replacement even on POSIX, where unlinking a locked inode is possible."""
        authority = self.authority
        canonical, identity = _opened_parent(authority.parent)
        if identity != authority.parent_identity or not authority.canonical.startswith(
            f"credential-gate/transaction/v1\0{canonical}\0"
        ):
            raise GateTransactionError("transaction-parent-changed")
        info = _safe_entry(authority.companion)
        opened = os.fstat(self.descriptor)
        if _identity(info) != _identity(opened) or info.st_size != 1 or opened.st_size != 1:
            raise GateTransactionError("transaction-companion-changed")

    def __enter__(self) -> GateTransaction:
        path = self.authority.companion
        try:
            created = False
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                created = True
            except FileExistsError:
                _safe_entry(path)
                descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            self.descriptor = descriptor
            if created:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            self.check()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    _descriptor_lock(descriptor, True)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise GateTransactionError("transaction-busy") from None
                    time.sleep(0.025)
            self.check()
            return self
        except BaseException:
            if self.descriptor >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            raise

    def __exit__(self, kind, value, traceback) -> None:
        try:
            if kind is None:
                self.check()
        finally:
            os.close(self.descriptor)
            self.descriptor = -1


def _descriptor_lock(descriptor: int, acquire: bool) -> None:
    if os.name == "nt":
        import msvcrt  # pylint: disable=import-outside-toplevel,import-error

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # pylint: disable=import-outside-toplevel,import-error

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


def _authority(root: Path) -> CoordinationRoot:
    root = _root_path(root)
    spelling = os.path.normcase(str(root))
    for alias, authority in _COORDINATION.get():
        if spelling == alias:
            return authority
    return coordination_root(root)


@contextmanager
def gate_transaction(root: Path, *, timeout: float = TX_WRITER_TIMEOUT):
    """Join an explicitly held authority without reacquiring its OS lock."""
    authority = _authority(root)
    for transaction in _TRANSACTIONS.get():
        if transaction.authority.canonical == authority.canonical:
            transaction.check()
            yield transaction
            transaction.check()
            return
    with GateTransaction(authority, timeout) as transaction:
        token = _TRANSACTIONS.set((*_TRANSACTIONS.get(), transaction))
        try:
            yield transaction
        finally:
            _TRANSACTIONS.reset(token)


@contextmanager
def gate_transactions(
    roots: list[Path], *, aliases: Mapping[Path, Path] | None = None, timeout: float = TX_WRITER_TIMEOUT
):
    """Hold sorted final-root companions across package publication, verification and rollback."""
    authorities = {}
    bindings = list(_COORDINATION.get())
    by_spelling = {}
    for root in roots:
        authority = _authority(root)
        spelling = os.path.normcase(os.path.abspath(root))
        authorities[authority.canonical] = (root, authority)
        by_spelling[spelling] = authority
        bindings.append((spelling, authority))
    for alias, final in (aliases or {}).items():
        authority = by_spelling.get(os.path.normcase(os.path.abspath(final)))
        if authority is None:
            raise GateTransactionError("transaction-alias-without-authority")
        bindings.insert(0, (os.path.normcase(os.path.abspath(alias)), authority))
    token = _COORDINATION.set(tuple(bindings))
    try:
        with ExitStack() as stack:
            for root, _coordination in sorted(authorities.values(), key=lambda item: item[1].companion.name):
                stack.enter_context(gate_transaction(root, timeout=timeout))
            yield
    finally:
        _COORDINATION.reset(token)


def _coordinated(*, reader: bool = False, refusal=3):
    """The public boundary owns locking; helpers only join the already-held descriptor."""

    def decorate(function):
        @wraps(function)
        def run(root, *args, **kwargs):
            try:
                with gate_transaction(root, timeout=TX_READER_TIMEOUT if reader else TX_WRITER_TIMEOUT):
                    identity = _identity(_safe_entry(root, directory=True))
                    spec = root / MIGRATION_SPEC
                    snapshot = spec.read_bytes() if spec.is_file() else None
                    token = _VIEWS.set((*_VIEWS.get(), (root, identity, snapshot)))
                    try:
                        result = function(root, *args, **kwargs)
                        _check_transaction(root)
                        return result
                    finally:
                        _VIEWS.reset(token)
            except (OSError, ValueError, subprocess.SubprocessError):
                if refusal is None:
                    raise
                log.error("credential gate CANNOT_ESTABLISH: transaction refused")
                return refusal() if callable(refusal) else refusal

        return run

    return decorate


def _check_transaction(root: Path) -> None:
    for location, identity, snapshot in _VIEWS.get():
        if _identity(_safe_entry(location, directory=True)) != identity:
            raise GateTransactionError("transaction-root-changed")
        spec = location / MIGRATION_SPEC
        current = spec.read_bytes() if spec.is_file() else None
        if snapshot != current:
            raise GateTransactionError("transaction-spec-changed")
    authority = _authority(root)
    for transaction in _TRANSACTIONS.get():
        if transaction.authority.canonical == authority.canonical:
            transaction.check()
            return
    raise GateTransactionError("transaction-not-held")


def _sync_directory(directory: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _durable_write(root: Path, name: str, text: str) -> None:
    _check_transaction(root)
    target = root / name
    if os.path.lexists(target):
        _safe_entry(target)
    scratch = target.with_name(f"{name}.{uuid.uuid4().hex}.writing")
    try:
        with scratch.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _check_transaction(root)
        if os.name == "nt":
            import ctypes  # pylint: disable=import-outside-toplevel

            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            if not move(str(scratch), str(target), 1 | 8):  # replace, write-through
                raise GateTransactionError("transaction-replace-failed")
        else:
            os.replace(scratch, target)
        _sync_directory(root)
    finally:
        scratch.unlink(missing_ok=True)


def _durable_unlink(root: Path, name: str) -> None:
    _check_transaction(root)
    if os.path.lexists(root / name):
        _safe_entry(root / name)
        (root / name).unlink()
        _sync_directory(root)


# Why a trusted audit trail could not be read. Members of the closed `cannot_establish` vocabulary
# below, defined here because `_read_audit_trail` - the single parser - is the only place that can
# tell them apart.
AUDIT_MISSING = "audit-missing"
AUDIT_MALFORMED = "audit-malformed"
AUDIT_FOREIGN_SCOPE = "audit-foreign-scope"
AUDIT_CLOCK_SKEW = timedelta(minutes=5)
AUDIT_FIELDS = frozenset({"ts", "action", "detail", "user", "scope"})

# Denied rights: WD (write data / create files), AD (append data / create subdirs), WA (write
# attributes). Read and traverse stay allowed on purpose - the agent must still be able to inspect
# the tree, and a gate that blinds it produces worse reports, not safer ones.
DENY_RIGHTS = "(OI)(CI)(WD,AD,WA)"

# Both audit actions mean "the gate was armed"; they differ only in how strongly it is ENFORCED
# (kernel ACL vs marker file). Every READER must treat them alike, or the gate's ordering guarantee
# silently becomes Windows-only. Measured 2026-08-03 by simulating the non-Windows path: with only
# `block` recognised, a `probe-cleared` recorded BEFORE a re-arm still counted as earned afterwards,
# so backdated evidence survived exactly the event that exists to invalidate it. The distinct names
# are kept because the enforcement difference is real and belongs in the log.
BLOCK_ACTIONS = frozenset({"block", "block-marker-only"})

# Files that mark a directory as a legitimately gateable UNIT of work - one migration, or one engine
# bundle. A gate target should be one of these, because the hook's `_blocking_marker()` walks UPWARD
# from any write target and stops at the first marker it meets: a marker therefore governs its whole
# subtree, and one placed too high governs work it knows nothing about.
MIGRATION_SPEC = "migration-spec.json"
SCOPE_MARKERS = (MIGRATION_SPEC, ENGINE_RECEIPT, "input_manifest.json")

# Shape of a repository checkout rather than a unit of work. `.git` alone is the decisive one (it is
# what the real incident hit); the other two catch a checkout exported without its git directory.
REPO_ROOT_SIGNS = (".git", "AGENTS.md", "pyproject.toml")


def _scope_refusal(migration: Path) -> str | None:
    """Why `migration` is too broad to gate, or None when it is a legitimate scoped target.

    Measured 2026-08-18, from a real incident: `credential_gate.py block` was invoked from the wrong
    working directory and wrote its marker at the REPO ROOT. Because `_blocking_marker()` walks up
    from any write target and returns the first marker found, that one file governed every migration
    in the checkout - blocking ~13 unrelated in-flight agents at once, including bundles that had
    already independently earned their clearance, and stranding a live unsaved DAX measure in a
    Desktop session with nowhere to write.

    Nothing refused it, because `apply_block` accepted any directory at all. The blast radius of a
    gate is its entire subtree, so the target has to BE a unit of work - not merely contain some.

    Deliberately a positive check with an escape hatch: a directory carrying its own scope marker is
    always allowed, anything shaped like a checkout root is always refused, and anything else is
    refused with `--force-scope` named in the message. That keeps an unusual-but-legitimate layout
    workable without making the catastrophic case reachable by accident.
    """
    resolved = migration.resolve()
    if resolved.parent == resolved:
        return f"{resolved} is a filesystem root"
    for sign in REPO_ROOT_SIGNS:
        if (resolved / sign).exists():
            return f"{resolved} looks like a repository checkout root (contains {sign}), not one migration or bundle"
    if any((resolved / name).is_file() for name in SCOPE_MARKERS):
        return None
    return (
        f"{resolved} carries none of {', '.join(SCOPE_MARKERS)}, so it is not identifiable as a single "
        "migration or engine bundle"
    )


def _duplicate_sources(sources: list[str]) -> list[str]:
    """Source keys that appear more than once, preserving first duplicate order."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for source in sources:
        if source in seen and source not in duplicates:
            duplicates.append(source)
        seen.add(source)
    return duplicates


def _sources_detail(sources: list[str]) -> str:
    """Human-readable detail with a machine-readable JSON source array."""
    return f"sources_json={json.dumps(sources, ensure_ascii=False)}"


def _block_refusal(migration: Path, sources: list[str], force_scope: bool) -> int | None:
    """Return a refusal code for invalid block inputs, or None when arming may proceed."""
    duplicates = _duplicate_sources(sources)
    if duplicates:
        log.error("REFUSING to arm the gate: duplicate source key(s) are not unique identities: %s", duplicates)
        _append_audit(migration, "violation", f"duplicate source keys at block: {duplicates}", sources=sources)
        return 2
    refusal = _scope_refusal(migration)
    if not refusal:
        return None
    if not force_scope:
        log.error(
            "REFUSING to arm the gate: %s.\n"
            "A marker governs its ENTIRE subtree (the hook walks upward and stops at the first "
            "one), so arming here would block every migration beneath it - including any that "
            "already earned a clearance. This is usually a wrong working directory: pass the "
            "migration or bundle directory explicitly. Use --force-scope only if you really do "
            "mean to gate everything below this path.",
            refusal,
        )
        return 2
    log.warning("--force-scope: arming the gate on a target that failed the scope check (%s).", refusal)
    _append_audit(migration, "block-forced-scope", refusal)
    return None


def _audit(migration: Path, action: str, detail: str, sources: list[str] | None = None) -> None:
    """Generic producers cannot impersonate transitions; failure is never suppressed."""
    if action not in ("engine-receipt", "violation"):
        raise ValueError("dedicated gate transition required")
    with gate_transaction(migration):
        _append_audit(migration, action, detail, sources)


def _append_audit(migration: Path, action: str, detail: str, sources: list[str] | None = None) -> None:
    """Append and fsync one canonical row under the authority's descriptor."""
    _check_transaction(migration)
    path = migration / AUDIT
    if os.path.lexists(path):
        _safe_entry(path)
        if _audit_entries(migration) is None:
            raise GateTransactionError("transaction-audit-untrusted")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "detail": detail,
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        "scope": str(migration.resolve()),
    }
    if sources is not None:
        entry["sources"] = sources
    if not _canonical_audit_fields(entry):
        raise GateTransactionError("transaction-audit-shape")
    line = (json.dumps(entry) + "\n").encode("utf-8")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        if _identity(_safe_entry(path)) != _identity(os.fstat(descriptor)):
            raise GateTransactionError("transaction-audit-changed")
        _check_transaction(migration)
        if os.write(descriptor, line) != len(line):
            raise GateTransactionError("transaction-audit-torn")
        os.fsync(descriptor)
        _check_transaction(migration)
    finally:
        os.close(descriptor)
    _sync_directory(migration)


def _valid_audit_sources(value: object, *, diagnostic: bool = False) -> bool:
    """Legacy labels remain readable, but malformed keys and coercible values never do."""
    if not isinstance(value, list):
        return False
    if not all(
        isinstance(source, str)
        and source.strip()
        and not any(char in source for char in "\r\n\x00")
        and (not source.startswith("source-key:") or SOURCE_KEY_RE.fullmatch(source))
        for source in value
    ):
        return False
    # A duplicate-key REFUSAL records the offending list, not a successful arm/clear.
    return diagnostic or len(set(value)) == len(value)


def _valid_authorization_detail(detail: str) -> bool:
    """Require canonical writer syntax and authorize()'s same pure platform lineage guard."""
    who, separator, lineage = detail.removeprefix("by=").rpartition("; chain=")
    if not detail.startswith("by=") or not separator or not who.strip():
        return False
    try:
        chain = ast.literal_eval(lineage)
    except (ValueError, SyntaxError, RecursionError):
        return False
    return (
        isinstance(chain, list)
        and repr(chain) == lineage
        and all(isinstance(name, str) and name.strip() for name in chain)
        and not _has_copilot_ancestor(chain)
    )


def _canonical_audit_fields(entry: dict) -> bool:
    """Action-specific shapes of _audit's callers; diagnostic rows are not proof rows."""
    if not AUDIT_FIELDS <= entry.keys() or not all(isinstance(entry[field], str) for field in AUDIT_FIELDS):
        return False
    action, detail = entry["action"], entry["detail"]
    source_rule = AUDIT_SOURCE_RULES.get(action)
    if source_rule is None or not entry["user"].strip():
        return False
    allowed = AUDIT_FIELDS | ({"sources"} if source_rule != "absent" else set())
    names = _entry_sources(entry)
    if (
        entry.keys() - allowed
        or ("sources" in entry and not _valid_audit_sources(entry["sources"], diagnostic=action == "violation"))
        or (source_rule == "arm" and names is None)
    ):
        return False
    if action in BLOCK_ACTIONS or (action == PROBE_CLEARED and detail.startswith(("sources=", "sources_json="))):
        parsed = _parse_sources_detail(detail)
        if parsed is None or parsed != names:
            return False
    if action == "authorize":
        return _valid_authorization_detail(detail)
    return action != "engine-receipt" or re.fullmatch(r"sha256=[0-9a-f]{64}", detail) is not None


def _valid_audit_timestamp(value: str) -> bool:
    try:
        timestamp = datetime.fromisoformat(value)
        return timestamp.utcoffset() is not None and timestamp <= datetime.now(timezone.utc) + AUDIT_CLOCK_SKEW
    except (TypeError, ValueError, OverflowError):
        return False


def _scoped_audit_entry(line: str, scope: str) -> tuple[dict | None, str | None]:
    """The sole audit parser: canonical writer shapes, same scope and plausible aware times."""
    try:
        entry = json.loads(line, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
    except (ValueError, _DuplicateJsonKey, _NonFiniteJsonConstant):
        return None, AUDIT_MALFORMED
    if not isinstance(entry, dict):
        return None, AUDIT_MALFORMED
    if entry.get("scope") != scope:
        return None, AUDIT_FOREIGN_SCOPE
    if not _canonical_audit_fields(entry) or not _valid_audit_timestamp(entry["ts"]):
        return None, AUDIT_MALFORMED
    return entry, None


def _valid_scoped_audit_entry(line: str, scope: str) -> dict | None:
    """Compatibility facade over the one strict parser; a bad entry poisons the whole trail."""
    return _scoped_audit_entry(line, scope)[0]


def _read_audit_trail(migration: Path) -> tuple[list[dict] | None, str | None]:
    """Return one complete trusted snapshot or a closed audit-* refusal; an empty log is malformed."""
    path = migration / AUDIT
    if not path.is_file():
        return None, AUDIT_MISSING
    try:
        before = _safe_entry(path)
        text = path.read_text(encoding="utf-8")
        after = _safe_entry(path)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or not text.endswith("\n"):
            return None, AUDIT_MALFORMED
    except (OSError, UnicodeError):
        return None, AUDIT_MALFORMED
    scope = str(migration.resolve())
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        entry, refusal = _scoped_audit_entry(line, scope)
        if entry is None:
            return None, refusal
        entries.append(entry)
    if not entries:
        return None, AUDIT_MALFORMED
    return entries, None


def _audit_entries(migration: Path) -> list[dict] | None:
    """The sole trusted-audit reader used by every gate consumer (#354).

    Every nonblank row must parse, match this exact root and satisfy its action's writer schema.
    One corrupt, foreign or unscoped row poisons the WHOLE trail; dropping it would launder earlier
    proof. Missing, empty, directory-shaped or unreadable evidence returns None, never [].
    """
    return _read_audit_trail(migration)[0]


def _parse_sources_detail(detail: str) -> list[str] | None:
    """Parse a ``sources=[...]`` audit detail, returning None when absent or malformed."""
    json_marker = "sources_json="
    if detail.startswith(json_marker):
        decoder = json.JSONDecoder()
        try:
            parsed, _idx = decoder.raw_decode(detail[len(json_marker) :].lstrip())
        except ValueError:
            return None
        return parsed if _valid_audit_sources(parsed) else None
    marker = "sources="
    if not detail.startswith(marker):
        return None
    source_text = detail[len(marker) :].split(";", 1)[0].strip()
    try:
        parsed = ast.literal_eval(source_text)
    except (ValueError, SyntaxError):
        return None
    return parsed if _valid_audit_sources(parsed) else None


def _entry_sources(entry: dict) -> list[str] | None:
    """Structured audit sources, falling back to legacy detail parsing."""
    sources = entry.get("sources")
    if isinstance(sources, list):
        return sources
    return _parse_sources_detail(str(entry.get("detail") or ""))


def _parse_legacy_probe_source(detail: str) -> list[str] | None:
    """Source name from pre-source-aware ``probe-cleared: DATA_OK from ...`` audit details."""
    marker = "probe-cleared: DATA_OK from "
    return [detail[len(marker) :]] if detail.startswith(marker) and detail[len(marker) :] else None


def _earned_sources(migration: Path) -> tuple[dict[str, str | None], bool]:
    """Source-level gate evidence from the append-only audit log.

    A later block invalidates only the sources it names. This is the concurrency fix for sibling
    agents sharing one bundle: source Y being re-armed must not erase source X's previously earned
    proof, because those are independent reachability facts.
    """
    entries = _audit_entries(migration)
    if entries is None:
        return {}, False
    states: dict[str, tuple[str | None, str]] = {}
    authorized = False
    last_block_sources: list[str] = []
    for entry in entries:
        action = entry.get("action")
        ts = str(entry.get("ts") or "")
        detail = str(entry.get("detail") or "")
        if action in BLOCK_ACTIONS:
            sources = _entry_sources(entry)
            last_block_sources = sources or []
            for source in last_block_sources:
                states[source] = (None, ts)
        elif action == "authorize":
            authorized = True
        elif action == "probe-cleared":
            sources = _entry_sources(entry) or _parse_legacy_probe_source(detail) or last_block_sources
            for source in sources:
                _earned, blocked_at = states.get(source, (None, ""))
                if ts >= blocked_at:
                    states[source] = ("probe-cleared", blocked_at)
    return {source: earned for source, (earned, _blocked_at) in states.items()}, authorized


def _clear_was_earned(migration: Path, sources: list[str] | None = None) -> str | None:
    """Return probe-cleared/authorize only for an audit-backed lift; a bare clear earns nothing.

    Source ordering preserves sibling clearances but invalidates each re-armed source's proof.
    Same-user forgery remains possible: this text audit is accountability, not non-repudiation.
    A genuine probe also leaves an independent one-row query in the source system's query history.
    """
    states, authorized = _earned_sources(migration)
    if authorized:
        return "authorize"
    if sources is not None:
        return "probe-cleared" if sources and all(states.get(source) for source in sources) else None
    if not states:
        return None
    return "probe-cleared" if all(states.values()) else None


def _icacls(args: list[str]) -> tuple[int, str]:
    """The Windows ACL boundary, including the protection bit absent from icacls' listing."""
    try:
        if len(args) == 1 and _protected_dacl(Path(args[0])):
            return 1, "physical_acl_protected"
        proc = subprocess.run(["icacls", *args], capture_output=True, text=True, timeout=30, check=False)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return 1, "physical_acl_query_failed"


def _protected_dacl(path: Path) -> bool:
    """Read SE_DACL_PROTECTED without following an unchecked path or altering its descriptor."""
    import ctypes  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetFileSecurityW.restype = wintypes.BOOL
    security.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetSecurityDescriptorControl.restype = wintypes.BOOL
    size = wintypes.DWORD()
    security.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))  # DACL_SECURITY_INFORMATION
    if not size.value:
        raise OSError("physical_acl_query_failed")
    descriptor = ctypes.create_string_buffer(size.value)
    if not security.GetFileSecurityW(str(path), 4, descriptor, size.value, ctypes.byref(size)):
        raise OSError("physical_acl_query_failed")
    control, revision = wintypes.WORD(), wintypes.DWORD()
    if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise OSError("physical_acl_query_failed")
    return bool(control.value & 0x1000)  # SE_DACL_PROTECTED


def _user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


PROBE_DIR = "_probe"


def probe_dir(migration: Path) -> Path:
    """Writable one-table probe sandbox beside this gate's anchors, never a deliverable.

    A child inherits the deny and cannot earn the clear it needs to build. Re-granting a child
    introduces ACL ordering, recreation and temporary-lift hazards. For nested gates, use the
    outer root's sandbox and clear the outer gate first.
    """
    d = migration / PROBE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def denied_dirs(migration: Path, create: bool = True) -> list[Path]:
    """Resolve non-colliding anchors; clear/read use create=False and never synthesize a folder."""
    for parent in (migration, *migration.parents):
        _safe_acl_entry(parent, directory=True)
    native = sorted(path for path in migration.iterdir() if path.suffix.lower() in {".semanticmodel", ".report"})
    fabric, pbip = migration / "fabric", migration / "pbip"
    for directory in (*native, fabric, pbip):
        _safe_acl_entry(directory, directory=True)
    identities = (MIGRATION_SPEC, "package-manifest.json", "report.json", ENGINE_RECEIPT, "input_manifest.json")
    present = {name for name in identities if _safe_acl_entry(migration / name, directory=False)}
    if native:
        targets = native
        alternatives = (fabric, pbip)
    elif present & {MIGRATION_SPEC, "package-manifest.json"}:
        targets, alternatives = [fabric], (pbip,)
    elif pbip.exists() or present & {"report.json", ENGINE_RECEIPT, "input_manifest.json"}:
        targets, alternatives = [pbip], (fabric,)
    else:
        # The caller's existing scope guard permits this only for a promoted/forced legacy root.
        targets, alternatives = [fabric], ()
    if any(path.exists() and next(path.iterdir(), None) is not None for path in alternatives):
        raise ValueError("physical_layout_ambiguous")
    if create:
        for directory in targets:
            directory.mkdir(exist_ok=True)
    return targets


def _safe_acl_entry(path: Path, *, directory: bool) -> bool:
    """No-follow type check; missing targets are readable state, not permission to create them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if is_reparse_entry(info) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("physical_directory_unsafe")
    return True


def _inspection_dirs(migration: Path) -> list[Path]:
    """Expected anchors plus the one bounded legacy location; never a recursive removal list."""
    targets = denied_dirs(migration, create=False)
    legacy = migration / "fabric"
    if legacy not in targets and _safe_acl_entry(legacy, directory=True):
        targets.append(legacy)
    return targets


def _acl_listing(path: Path) -> str:
    code, output = _icacls([str(path)])
    if code:
        raise ValueError("physical_acl_query_failed")
    return output


def _account_aces(path: Path) -> list[tuple[frozenset[str], frozenset[str]]]:
    """Admit inherited permissions, then select current-principal ACEs; no group-membership engine."""
    listing = _acl_listing(path)
    entries = []
    parsed = False
    user = _user().casefold()
    if not user:
        raise ValueError("physical_acl_principal_unknown")
    for line in listing.splitlines():
        line = line.removeprefix(str(path)).strip()
        principal, separator, permissions = line.partition(":(")
        if not separator:
            continue
        groups = re.findall(r"\(([^)]*)\)", "(" + permissions)
        if not groups or "".join(f"({group})" for group in groups) != "(" + permissions:
            raise ValueError("physical_acl_unreadable")
        parsed = True
        flags = frozenset(group.upper() for group in groups[:-1])
        identity = principal.casefold()
        if identity != user and ("\\" in user or identity.rsplit("\\", 1)[-1] != user):
            # An explicit group allow can override this account's inherited deny on a child.
            if "I" not in flags:
                raise ValueError("physical_acl_conflict")
            continue
        entries.append((flags, frozenset(groups[-1].upper().split(","))))
    if not parsed:
        raise ValueError("physical_acl_unreadable")
    return entries


def _gate_ace(flags: frozenset[str], rights: frozenset[str]) -> bool:
    return flags - {"I"} == {"OI", "CI", "DENY"} and rights == {"WD", "AD", "WA"}


def _attributable_anchor(path: Path) -> bool:
    """Exact gate-shaped explicit denies are the only nesting/re-arm exception."""
    pending_arm = (path.parent / MARKER).is_file() and bool(_established_sources(path.parent))
    return (_gate_was_ever_applied(path.parent) or pending_arm) and path in denied_dirs(path.parent, create=False)


def _checked_acl_tree(
    targets: list[Path], *, for_mutation: bool = True
) -> dict[Path, list[tuple[frozenset[str], frozenset[str]]]]:
    """Read existing inheritance/explicit ACLs; never install or repair descendant ACEs."""
    snapshots = {}
    pending = [path for path in targets if _safe_acl_entry(path, directory=True)]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if is_reparse_entry(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("physical_directory_unsafe")
        entries = _account_aces(path)
        for flags, rights in entries:
            if "I" not in flags and not (_gate_ace(flags, rights) and (not for_mutation or _attributable_anchor(path))):
                raise ValueError("physical_acl_conflict")
        snapshots[path] = entries
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    return snapshots


def _all_targets_denied(migration: Path) -> bool:
    """Enforcement is ALL expected anchors with supported descendants, not any residual deny."""
    targets = denied_dirs(migration, create=False)
    snapshots = _checked_acl_tree(targets, for_mutation=False)
    return bool(targets) and all(any(_gate_ace(*ace) for ace in snapshots.get(path, [])) for path in targets)


def _clear_acl_targets(migration: Path) -> None:
    """Remove only this root's explicit anchors; inherited or nested gates are never removed."""
    targets = denied_dirs(migration, create=False)
    candidates = _inspection_dirs(migration)
    removable = []
    for path in candidates:
        if not _safe_acl_entry(path, directory=True):
            continue
        entries = _account_aces(path)
        explicit = [(flags, rights) for flags, rights in entries if "DENY" in flags and "I" not in flags]
        if not explicit:
            continue
        if (
            not _gate_was_ever_applied(migration)
            or any(not _gate_ace(*ace) for ace in explicit)
            or (path not in targets and next(path.iterdir(), None) is not None)
        ):
            raise ValueError("physical_acl_conflict")
        removable.append(path)
    for path in removable:
        code, _output = _icacls([str(path), "/remove:d", _user()])
        if code:
            raise ValueError("physical_acl_remove_failed")
    if _has_deny_ace(migration):
        raise ValueError("physical_acl_still_blocked")


def _acl_refusal() -> None:
    """Path/account-free remedy shared by failed physical operations."""
    log.error(
        "CANNOT ESTABLISH credential-gate enforcement/clearance: unsupported layout, ACL, or failed ACL operation. "
        "Do not build. For nested gates, probe and clear the outer gate first; otherwise have the operator "
        "resolve protected/conflicting ACLs and retry."
    )


def _mark_unenforced(migration: Path) -> None:
    """Retain an existing stop without claiming complete physical coverage after a failed arm."""
    try:
        marker = migration / MARKER
        if marker.is_file():
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["writes_blocked"] = False
                _durable_write(migration, MARKER, json.dumps(payload, indent=2) + "\n")
    except (OSError, ValueError):
        pass


# The existing authority owns this one additional read-only boundary; no fourth producer module.
# pylint: disable=too-many-lines
@_coordinated(reader=True, refusal=("cannot_establish", "transaction-refused"))
def inspect_physical_barrier(root: Path) -> tuple[str, str]:  # pylint: disable=too-many-return-statements,too-many-branches
    """Read only the physical stop, never audit/proof/authorization; return fixed path-free codes."""
    try:
        for parent in (root, *root.parents):
            info = parent.lstat()
            if is_reparse_entry(info) or not stat.S_ISDIR(info.st_mode):
                return "cannot_establish", "physical_root_unsafe"
        marker = root / MARKER
        try:
            before = marker.lstat()
        except FileNotFoundError:
            before = None
        if before is not None:
            if is_reparse_entry(before) or not stat.S_ISREG(before.st_mode):
                return "cannot_establish", "physical_marker_invalid"
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(marker, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    return "cannot_establish", "physical_marker_changed"
                payload = json.loads(
                    stream.read(), object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite
                )
            after = marker.lstat()
            if is_reparse_entry(after) or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ):
                return "cannot_establish", "physical_marker_changed"
            fields = {
                "writes_blocked",
                "reachability",
                "credential_status",
                "reason",
                "next_step",
                "read_this_before_reporting",
                "sources",
                "applied",
            }
            if (
                not isinstance(payload, dict)
                or set(payload) != fields
                or payload["writes_blocked"] is not True
                or payload["reachability"] != "UNPROVEN"
                or any(
                    not isinstance(payload[key], str) or not payload[key]
                    for key in fields - {"writes_blocked", "sources"}
                )
            ):
                return "cannot_establish", "physical_marker_invalid"
            if (
                not isinstance(payload["sources"], list)
                or not payload["sources"]
                or any(not isinstance(source, str) or not source.strip() for source in payload["sources"])
                or len(set(payload["sources"])) != len(payload["sources"])
                or not _valid_audit_timestamp(payload["applied"])
            ):
                return "cannot_establish", "physical_marker_invalid"
            return "blocked", "physical_marker_blocked"
        for directory in _inspection_dirs(root):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue
            if is_reparse_entry(info) or not stat.S_ISDIR(info.st_mode):
                return "cannot_establish", "physical_directory_unsafe"
            if platform.system() == "Windows":
                code, output = _icacls([str(directory)])
                if code != 0:
                    return "cannot_establish", "physical_acl_query_failed"
                if "(DENY)" in output.upper():
                    return "blocked", "physical_acl_blocked"
        return "clear", "physical_clear"
    except (OSError, ValueError, subprocess.SubprocessError, _DuplicateJsonKey, _NonFiniteJsonConstant):
        return "cannot_establish", "physical_query_failed"


# Model/report DEFINITION files: their existence means a model or report was built.
DEFINITION_SUFFIXES = frozenset({".tmdl", ".pbism", ".pbir", ".pbip"})

# MATERIALIZED SOURCE ROWS. These are a strictly LARGER harm than a definition file: a `.tmdl`
# describes a model, but a materialized `.csv` IS the customer's data, sitting unencrypted on a
# workstation, extracted from a source whose reachability was never proven.
#
# Measured 2026-08-04: a deterministic-tier run wrote **two 110 MB CSVs** of source rows to
# `<out>/data/`, and `verify()` reported "OK - gate applied, no model/report artifacts exist",
# because it only ever looked at `DEFINITION_SUFFIXES`. `.json` is deliberately absent from this
# set - PBIR is made of `visual.json`/`report.json`, so including it would flag every report.
MATERIALIZED_DATA_SUFFIXES = frozenset({".csv", ".tsv", ".parquet", ".hyper", ".xlsx", ".xls", ".dat"})

# Directories that are NOT harm, and must be excluded or every migration self-reports a violation:
#   `source/`    - the input workbook. Always present; it is what we were given, not what we built.
#   `reference/` - Tableau-side screenshots used as fidelity ground truth.
#   `_probe/`    - the sanctioned sandbox for the one-row reachability probe, which by design is
#                  built WHILE the gate is up. Flagging it would make earning the clear impossible.
AUDIT_EXCLUDED_DIRS = frozenset({"source", "reference", "_probe"})


def audited_paths(migration: Path) -> list[Path]:
    """Every file under `migration` whose existence would mean something was built or extracted.

    Read-only and migration-wide, including `pbip/`, `reports/`, `semantic_models/` and `data/`.
    Limiting verification to denied `fabric/` misses engine output and extracted source rows.
    """
    if not migration.exists():
        return []
    found: list[Path] = []
    for path in migration.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(migration)
        except ValueError:  # pragma: no cover - rglob results are always relative to migration
            continue
        if AUDIT_EXCLUDED_DIRS.intersection(relative.parts):
            continue
        suffix = path.suffix.lower()
        if suffix in DEFINITION_SUFFIXES or suffix in MATERIALIZED_DATA_SUFFIXES or is_engine_artifact(relative):
            found.append(path)
    return found


def _is_engine_output(path: Path, migration: Path) -> bool:
    try:
        relative = path.relative_to(migration)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in ENGINE_OUTPUT_DIRS


def _load_engine_receipt(migration: Path) -> dict[str, dict[str, str | int]] | None:
    """Load exact engine artifact receipts; malformed or stale provenance earns no exemption."""
    try:
        receipt_path = migration / ENGINE_RECEIPT
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _receipt_was_audited_before_block(migration, sha256_file(receipt_path)):
        return None
    if not _receipt_matches_bundle(migration, receipt):
        return None
    return _receipt_artifacts(receipt.get("artifacts"))


def _receipt_was_audited_before_block(migration: Path, receipt_hash: str) -> bool:
    """Was this exact receipt recorded before the latest gate arm?"""
    entries = _audit_entries(migration)
    if entries is None:
        return False
    seen = False
    valid_for_latest_block = False
    had_block = False
    for entry in entries:
        action = entry.get("action")
        detail = str(entry.get("detail") or "")
        if action == "engine-receipt" and f"sha256={receipt_hash}" in detail:
            seen = True
        elif action in BLOCK_ACTIONS:
            had_block = True
            valid_for_latest_block = seen
            seen = False
    return valid_for_latest_block if had_block else seen


def _receipt_matches_bundle(migration: Path, receipt: dict) -> bool:
    """Does the receipt describe this bundle's current run markers?"""
    if receipt.get("version") != 1:
        return False
    try:
        report_hash = sha256_file(migration / "report.json")
        manifest_hash = sha256_file(migration / "input_manifest.json")
    except OSError:
        return False
    return receipt.get("report_sha256") == report_hash and receipt.get("input_manifest_sha256") == manifest_hash


def _receipt_artifacts(records: object) -> dict[str, dict[str, str | int]] | None:
    """Validate receipt artifact records and index them by relative path."""
    if not isinstance(records, list):
        return None
    by_path: dict[str, dict[str, str | int]] = {}
    for record in records:
        if not isinstance(record, dict):
            return None
        rel = record.get("path")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(rel, str) or not isinstance(size, int) or not isinstance(digest, str):
            return None
        by_path[rel] = {"size": size, "sha256": digest}
    return by_path


def _split_pre_gate_engine_artifacts(migration: Path, artifacts: list[Path]) -> tuple[list[Path], list[Path]]:
    """Return (pre_gate_engine_output, still_violating_artifacts)."""
    receipt = _load_engine_receipt(migration)
    if receipt is None:
        return [], artifacts
    pre_gate: list[Path] = []
    violations: list[Path] = []
    for artifact in artifacts:
        relative = artifact.relative_to(migration).as_posix()
        record = receipt.get(relative)
        if (
            _is_engine_output(artifact, migration)
            and record
            and artifact.stat().st_size == record["size"]
            and sha256_file(artifact) == record["sha256"]
        ):
            pre_gate.append(artifact)
        else:
            violations.append(artifact)
    return pre_gate, violations


def _last_block_sources(migration: Path) -> list[str] | None:
    """The source list recorded by the most recent `block`, or None if unreadable/absent.

    Current audit entries carry structured JSON `sources`; the `literal_eval` fallback exists only
    for legacy `sources=[...]` detail text. Any parse failure returns None, which callers must treat
    as "cannot prove these are the same sources" and therefore re-arm - failing closed.
    """
    entries = _audit_entries(migration)
    if entries is None:
        return None
    found: list[str] | None = None
    for entry in entries:
        if entry.get("action") not in BLOCK_ACTIONS:
            continue
        sources = _entry_sources(entry)
        if sources is not None:
            found = sources
            continue
        detail = entry.get("detail", "")
        if not str(detail).startswith("sources="):
            found = None
            continue
        try:
            parsed = ast.literal_eval(detail[len("sources=") :])
        except (ValueError, SyntaxError):
            found = None
            continue
        found = [str(s) for s in parsed] if isinstance(parsed, list) else None
    return found


def _redundant_rearm(migration: Path, sources: list[str]) -> str | None:
    """Skip only an earned lift covering EVERY incoming source, never a bare manual clear.

    Unconditional re-arming invited a measured bypass on an already-proven source (2026-08-03).
    Conversely, a new/unproven source must re-arm; sibling proof cannot cover it.
    """
    return _clear_was_earned(migration, sources)


@_coordinated()
def apply_block(migration: Path, sources: list[str], force_scope: bool = False) -> int:
    """Write the marker and deny write access to the output folder.

    ⚠️ The marker states a STATE, never a VERDICT, and the distinction is load-bearing. This runs at
    PARSE time, from a static classifier that opens no socket - it knows only that a live source
    EXISTS. It cannot know whether a credential is present, whether the host resolves, or whether a
    single row could be read.

    It used to claim `"reason": "live data source(s) have no Power BI credential"`. Measured
    2026-08-03: `claude-opus-4.6` read that, reasonably treated it as an established fact, reported
    "no credential" to the user, and never ran the probe. It behaved correctly on false input. The
    same conflation was fixed in the classifier's console output first; the file kept the old claim,
    so the two disagreed and the file won.
    """
    _trusted_transition(migration)
    refusal_code = _block_refusal(migration, sources, force_scope)
    if refusal_code is not None:
        return refusal_code

    if (migration / OVERRIDE).exists():
        if _override_is_authentic(migration):
            if (migration / MARKER).exists() or _has_deny_ace(migration):
                return 3
            log.warning("Override present and audit-backed: gate NOT applied - human authorized a build-only run.")
            _append_audit(migration, "block-skipped", "authentic override")
            return 0
        log.error(
            "IGNORING FORGED OVERRIDE: %s exists with no 'authorize' audit entry - applying the gate anyway.", OVERRIDE
        )
        _append_audit(migration, "violation", "forged override ignored at block time")

    already = _redundant_rearm(migration, sources)
    if already and not _transaction_pending(migration, sources):
        log.warning(
            "Gate NOT re-applied: these sources were already proven by '%s'. Re-arming a gate "
            "that a probe has satisfied is what invited a real bypass (see _redundant_rearm). "
            "Re-probe explicitly if you need to re-verify reachability.",
            already,
        )
        _append_audit(migration, "block-skipped", f"already earned by {already}; sources={sources}")
        return 0

    pending_sources = [
        source for source in sources if source in _transaction_pending(migration, sources, include_current=False)
    ]
    if sources and not pending_sources:
        _append_audit(migration, "block-skipped", f"already earned by source state; sources={sources}")
        return 0
    return _apply_pending_block(migration, pending_sources)


def _apply_pending_block(migration: Path, pending_sources: list[str]) -> int:
    """Precheck every existing descendant before changing ACLs; claim coverage only after readback."""
    windows = platform.system() == "Windows"
    try:
        targets = denied_dirs(migration, create=False)
        if windows:
            _checked_acl_tree(targets)
            targets = denied_dirs(migration)
        probe = probe_dir(migration)
        payload = {
            "writes_blocked": not windows,
            "reachability": "UNPROVEN",
            "credential_status": "UNKNOWN - nothing has contacted this source yet",
            "reason": "live data source(s) detected; reachability has NOT been measured",
            "next_step": (
                "python scripts/probe_live_source.py --spec <this-migration>/migration-spec.json "
                "OR --bundle <engine-output-dir>"
            ),
            "read_this_before_reporting": (
                "This file was written at PARSE time by a static check that opens NO connection. "
                "It does NOT mean a credential is missing - only that nothing has proven the "
                "source is reachable. Do NOT report a credential or connection problem from this "
                "file alone: run the probe and let its verdict (DATA_OK / NO_CREDENTIAL / "
                "UNREACHABLE) decide. Only the probe can tell a missing credential (a human must "
                "act) from a wrong hostname (nobody needs to sign in)."
            ),
            "sources": list(dict.fromkeys([*_established_sources(migration), *pending_sources])),
            "applied": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _durable_write(migration, MARKER, json.dumps(payload, indent=2) + "\n")
    except (OSError, ValueError, subprocess.SubprocessError):
        _mark_unenforced(migration)
        _acl_refusal()
        return 1

    if not windows:
        log.warning("Non-Windows: marker written, but ACL enforcement is Windows-only here.")
        log.info("PROBE SANDBOX: %s (build the 1-table reachability probe here)", probe)
        # Same `sources=` detail as the enforced path. Without it `_last_block_sources` cannot read
        # this entry, so the redundant-re-arm check fails closed forever on non-Windows.
        _append_audit(migration, "block-marker-only", _sources_detail(pending_sources), sources=pending_sources)
        return 0

    try:
        for directory in targets:
            if any("I" not in flags and _gate_ace(flags, rights) for flags, rights in _account_aces(directory)):
                continue
            code, _output = _icacls([str(directory), "/deny", f"{_user()}:{DENY_RIGHTS}"])
            if code:
                raise ValueError("physical_acl_apply_failed")
        if not _all_targets_denied(migration):
            raise ValueError("physical_acl_incomplete")
        payload["writes_blocked"] = True
        _durable_write(migration, MARKER, json.dumps(payload, indent=2) + "\n")
    except (OSError, ValueError, subprocess.SubprocessError):
        _mark_unenforced(migration)
        _acl_refusal()
        return 1
    _append_audit(migration, "block", _sources_detail(pending_sources), sources=pending_sources)
    log.info("ENFORCED: write denied on every resolved model/report target.")
    log.info("PROBE SANDBOX: %s (build the 1-table reachability probe here)", probe)
    return 0


def _trusted_transition(migration: Path) -> None:
    _check_transaction(migration)
    if os.path.lexists(migration / AUDIT) and _audit_entries(migration) is None:
        raise GateTransactionError("transaction-audit-untrusted")
    if os.path.lexists(migration / OVERRIDE):
        _safe_entry(migration / OVERRIDE)
    if os.path.lexists(migration / MARKER):
        _established_sources(migration)


def _established_sources(migration: Path) -> list[str]:
    """Unreadable obligations are poison, never an empty marker to overwrite or remove."""
    marker = migration / MARKER
    if not os.path.lexists(marker):
        return []
    _safe_entry(marker)
    payload = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not _valid_audit_sources(sources):
        raise GateTransactionError("transaction-marker-untrusted")
    return sources


def _transaction_pending(migration: Path, extra: list[str] | None = None, *, include_current: bool = True) -> list[str]:
    """Full-root keys veto release without widening an audit row's exact caller scope."""
    entries = _audit_entries(migration) or []
    established = _established_sources(migration)
    recorded = [key for entry in entries for key in (_entry_sources(entry) or []) if SOURCE_KEY_RE.fullmatch(key)]
    keys = list(dict.fromkeys([*established, *recorded, *(extra or [])]))
    if include_current and ((migration / MIGRATION_SPEC).is_file() or (migration / "input_manifest.json").is_file()):
        current, refusal = _gate_root_live_keys(migration)
        if refusal:
            raise GateTransactionError("transaction-spec-untrusted")
        keys = list(dict.fromkeys([*keys, *sorted(current)]))
    canonical = tuple(key for key in keys if SOURCE_KEY_RE.fullmatch(key))
    codes, authorized = _data_access_ledger(entries, canonical)
    if authorized and _override_is_authentic(migration, entries=entries):
        return []
    legacy, legacy_authorized = _earned_sources(migration)
    legacy_authorized = legacy_authorized and _override_is_authentic(migration, entries=entries)
    failures = set(PROBE_ATTEMPT_CODES.values())
    return [key for key in keys if not (legacy_authorized or legacy.get(key)) or codes.get(key) in failures]


@_coordinated(refusal=None)
def record_probe_attempt(migration: Path, verdict: str, detail: str, sources: list[str]) -> None:
    """A measurement becomes committed only after its pending physical stop is established."""
    action = f"probe-{verdict.lower()}"
    if action not in (*PROBE_ATTEMPT_CODES, PROBE_DATA_OK) or not sources or not _valid_audit_sources(sources):
        raise ValueError("invalid probe transition")
    with gate_transaction(migration):
        _trusted_transition(migration)
        forced = any(entry["action"] == "block-forced-scope" for entry in (_audit_entries(migration) or []))
        if not forced and _block_refusal(migration, sources, False) is not None:
            raise GateTransactionError("transaction-scope-refused")
        # DATA_OK also remains blocked until its paired clear. Do not invalidate siblings' epochs.
        if _apply_pending_block(migration, sources):
            raise GateTransactionError("transaction-arm-failed")
        _append_audit(migration, action, detail, sources)


def _marker_sources(migration: Path) -> list[str]:
    """Source list currently named by the blocking marker, or an empty list when unreadable."""
    try:
        payload = json.loads((migration / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    marker_sources = payload.get("sources") if isinstance(payload, dict) else None
    return [str(source) for source in marker_sources] if isinstance(marker_sources, list) else []


def _unmatched_earned_sources(migration: Path, marker_sources: list[str], earned_sources: list[str]) -> list[str]:
    """Earned sources that neither remain in the marker nor have prior source-level evidence."""
    states, _authorized = _earned_sources(migration)
    marker_set = set(marker_sources)
    return [source for source in earned_sources if source not in marker_set and not states.get(source)]


@_coordinated()
def clear_block(migration: Path, reason: str, earned: bool = False, sources: list[str] | None = None) -> int:
    """Remove the ACL and marker.

    `earned=True` is for the probe only: it records `probe-cleared`, which is the evidence `verify`
    looks for. A bare clear is recorded as `manual-clear` and earns nothing, so artifacts built
    after one are reported as UNVALIDATED. The verb has to keep existing for teardown, but it must
    never quietly confer the guarantee.
    """
    _trusted_transition(migration)
    marker = migration / MARKER
    detail = reason
    earned_sources = sources if sources is not None else _last_block_sources(migration)
    if earned and earned_sources:
        duplicates = _duplicate_sources(earned_sources)
        if duplicates:
            log.error("credential gate NOT cleared (%s): duplicate earned source key(s): %s", reason, duplicates)
            _append_audit(
                migration, "violation", f"duplicate source keys at clear: {duplicates}", sources=earned_sources
            )
            return 1
        marker_sources = _marker_sources(migration)
        unknown_sources = _unmatched_earned_sources(migration, marker_sources, earned_sources)
        if marker.exists() and unknown_sources:
            log.error(
                "credential gate NOT cleared (%s): earned source(s) not named by the marker: %s",
                reason,
                unknown_sources,
            )
            return 1
        detail = f"{_sources_detail(earned_sources)}; reason={reason}"
    _append_audit(
        migration,
        "probe-cleared" if earned else "manual-clear",
        detail,
        sources=earned_sources if earned else None,
    )
    remaining_sources = _transaction_pending(migration)
    if not earned:
        # The teardown verb still earns nothing. It cannot release a current keyed live source.
        remaining_sources = [key for key in remaining_sources if SOURCE_KEY_RE.fullmatch(key)]
    if remaining_sources:
        if not marker.exists():
            if _apply_pending_block(migration, remaining_sources):
                return 3
        else:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["sources"] = remaining_sources
            _durable_write(migration, MARKER, json.dumps(payload, indent=2) + "\n")
        log.info("credential gate PARTIALLY CLEARED (%s); still blocked: %s", reason, remaining_sources)
        # Source-scoped success does not claim the root was released.
        return 0 if earned and earned_sources and not set(earned_sources).intersection(remaining_sources) else 1
    _release_physical(migration)
    log.info("credential gate CLEARED (%s)", reason)
    return 0


def _release_physical(migration: Path) -> None:
    """Write-ahead evidence alone never credits an interrupted release."""
    _check_transaction(migration)
    previous = (migration / MARKER).read_text(encoding="utf-8") if (migration / MARKER).is_file() else None
    try:
        if platform.system() == "Windows":
            _has_deny_ace(migration)  # Refuse an unqueryable barrier before changing its marker.
            _durable_unlink(migration, MARKER)
            _clear_acl_targets(migration)
        else:
            _durable_unlink(migration, MARKER)
        _check_transaction(migration)
        state, _code = inspect_physical_barrier(migration)
        if state != "clear":
            raise GateTransactionError("transaction-release-incomplete")
    except (OSError, ValueError, subprocess.SubprocessError):
        if previous is not None:
            payload = json.loads(previous)
            payload["writes_blocked"] = False
            _durable_write(migration, MARKER, json.dumps(payload, indent=2) + "\n")
        raise


def _override_is_authentic(migration: Path, *, entries: list[dict] | None = None) -> bool:
    """Require the override FILE and a canonical authorization; either alone earns nothing.

    The assessor supplies its already-validated same-root snapshot, avoiding a second mutable read
    between its epoch fold and authorization decision. authorize() owns the process-lineage guard.
    """
    if not (migration / OVERRIDE).is_file():
        return False
    if entries is None:
        entries = _audit_entries(migration)
    if entries is None:
        return False
    keys = {key for entry in entries for key in (_entry_sources(entry) or []) if SOURCE_KEY_RE.fullmatch(key)}
    if keys:
        current = _current_live_source_keys(migration)
        keys.update(current or ())
        return _data_access_ledger(entries, tuple(sorted(keys)))[1]
    return any(entry.get("action") == "authorize" for entry in entries)


def _ancestry() -> list[str]:
    """Process names from this process up to the root, for provenance checks and audit."""
    if platform.system() != "Windows":
        return []
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$id=$PID; $all=Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name;"
                "for($i=0;$i -lt 12;$i++){$p=$all|Where-Object{$_.ProcessId -eq $id}|Select-Object -First 1;"
                "if(-not $p){break}; Write-Output $p.Name; $id=$p.ParentProcessId}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return ["<lineage-unavailable>"]


def _has_copilot_ancestor(chain: list[str]) -> bool:
    """Pure platform guard shared by authorize() and its audit reader; never queries processes.

    On Windows an empty/unavailable lineage is a refusal, not permission. A failed query once let
    an agent authorize its own build (2026-08-02); a TTY check also failed because agent shells had
    one. Only _ancestry() queries the process tree. Non-Windows keeps the writer's existing policy.
    """
    if platform.system() != "Windows":
        return False
    if not chain or "<lineage-unavailable>" in chain:
        return True
    return any("copilot" in name.lower() for name in chain)


@_coordinated()
def authorize(migration: Path, who: str) -> int:
    """Record a human's explicit build-only authorization and lift the ACL.

    The ONLY supported way to proceed without a successful probe, and deliberately hostile to being
    run by an agent - see `_has_copilot_ancestor`. The ancestry chain is recorded either way, so a
    forged authorization is attributable after the fact even if the check is somehow evaded.
    """
    chain = _ancestry()
    if _has_copilot_ancestor(chain):
        log.error("REFUSED: this command was invoked from inside a Copilot agent session.")
        log.error("  Authorizing an unvalidated build is a HUMAN decision. An agent running this")
        log.error("  command is impersonating the user - measured 2026-08-02, one did exactly that.")
        log.error("  If you are a human: run this from a plain terminal, outside any Copilot session.")
        log.error("  Process chain seen: %s", " <- ".join(chain))
        _append_audit(migration, "violation", f"authorize refused; chain={chain}")
        return 2
    _trusted_transition(migration)
    _durable_write(
        migration,
        OVERRIDE,
        f"Build-only migration authorized by {who} at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}.\n"
        "Validation against live data was explicitly deferred by a human.\n",
    )
    _append_audit(migration, "authorize", f"by={who}; chain={chain}")
    if _transaction_pending(migration):
        return 1
    _release_physical(migration)
    return 0


@_coordinated(reader=True)
def status(migration: Path) -> int:  # pylint: disable=too-many-return-statements
    """Report the marker and physical state. Exit 1 blocked, 3 cannot establish."""
    blocked = (migration / MARKER).exists()
    override = (migration / OVERRIDE).exists()
    log.info("marker=%s override=%s", "BLOCKED" if blocked else "none", "yes" if override else "no")
    try:
        denied = _has_deny_ace(migration)
        if platform.system() == "Windows":
            if blocked and not _all_targets_denied(migration):
                _acl_refusal()
                return 3
            log.info("acl=%s", "deny present" if denied else "no deny ACE")
    except (OSError, ValueError, subprocess.SubprocessError):
        _acl_refusal()
        return 3
    if blocked or denied:
        return 1
    if os.path.lexists(migration / AUDIT):
        if _audit_entries(migration) is None:
            return 3
    if os.path.lexists(migration / AUDIT) or (migration / MIGRATION_SPEC).is_file():
        if any(SOURCE_KEY_RE.fullmatch(key) for key in _transaction_pending(migration)):
            return 3
    if override and not _override_is_authentic(migration):
        return 3
    return 0


@_coordinated(reader=True, refusal="CANNOT_ESTABLISH")
def _unit_state(unit: Path) -> str:  # pylint: disable=too-many-return-statements
    """Classify one unit's gate state from artifacts on disk, never from prose.

    Ordered most-alarming-first, because a forged override coexisting with a marker is a bypass
    attempt and must not be reported as the benign state that happens to also be true.
    """
    marker = (unit / MARKER).exists()
    override = (unit / OVERRIDE).exists()
    if override and not _override_is_authentic(unit):
        return "FORGED-OVERRIDE"
    if marker:
        return "BLOCKED"
    current = status(unit)
    if current:
        return "BLOCKED" if current == 1 else "CANNOT_ESTABLISH"
    if override:
        return "authorized-unearned"
    entries = _audit_entries(unit)
    if entries is None:
        return "clean"
    actions = {entry.get("action") for entry in entries}
    if "probe-cleared" in actions:
        return "cleared-earned"
    return "clean"


def list_units(root: Path, as_json: bool = False) -> int:
    """Report gate state for EVERY unit beneath `root`. Read-only.

    Exists because every other subcommand takes exactly one migration, so "what is still gated?"
    across an estate cost one invocation per unit. Field report 2026-08-26, a ~44-unit estate:
    *"I am always asked to run these for all the dashboards manually"*.

    The agent needs this as much as the human. After a human signs in, a credential caches
    machine-wide (DPAPI), so units sharing that source may now be probeable -- but with no way to
    enumerate what is gated, an agent cannot discover what became retryable and cannot resume.

    Exit codes are for scripting, and deliberately rank the security signal above the workflow one:
    **3 = a forged override or cannot-establish transaction exists**, 1 = still blocked, 0 = nothing gated,
    4 = a bad `<root>`. **2 is reserved for argparse's usage errors** and is never returned here.

    That numbering is the *second* correction to this contract, and the reason is worth keeping.
    Blind review 2026-08-27 found `2` meant three unrelated things -- forged override, bad root, and
    argparse usage error -- while the docs sold it as forgery alone, so a mistyped estate root raised
    the most alarming state in the vocabulary. The first fix moved only *bad root* off `2` and
    documented the argparse overlap, which left the collision intact: `list <root> --badflag` still
    exited `2`. argparse hard-codes that and it is not ours to move, so the **security signal** moved
    instead. Two independent reviewers landed on this, and `3 = forged` now also matches
    `reprobe_blocked.py` -- its sibling in the documented pipeline -- which had the two codes swapped.

    Discovery uses marker/override/audit files, not a recursive ACL census. Each discovered root's
    locked snapshot reconciles physical state too; a surviving deny cannot become `clean`.
    This is still a resume signal, never a ship gate or estate-completeness proof.
    """
    units = sorted({p.parent for name in (MARKER, OVERRIDE, AUDIT) for p in root.rglob(name)})
    rows = [
        {"unit": str(u), "relative": str(u.relative_to(root)) if u != root else ".", "state": _unit_state(u)}
        for u in units
    ]

    if as_json:
        print(json.dumps({"root": str(root), "units": rows}, indent=2))
    elif not rows:
        log.info("no gated units found under %s", root)
    else:
        width = max(len(r["relative"]) for r in rows)
        for r in rows:
            log.info("  %-*s  %s", width, r["relative"], r["state"])
        tally: dict[str, int] = {}
        for r in rows:
            tally[r["state"]] = tally.get(r["state"], 0) + 1
        log.info("")
        log.info("  %d unit(s): %s", len(rows), ", ".join(f"{n} {s}" for s, n in sorted(tally.items())))
        blocked = tally.get("BLOCKED", 0)
        if blocked:
            log.info("")
            log.info("  %d still BLOCKED. Two ways out, and they are NOT equivalent:", blocked)
            log.info("    EARNED   - sign in, then re-probe. The clear is recorded as 'probe-cleared'")
            log.info("               and the model counts as validated. Prefer this.")
            log.info('    UNEARNED - credential_gate.py authorize <unit> --who "<name>"')
            log.info("               marks the build UNVALIDATED, permanently, in the audit log.")
            log.info("    A credential caches machine-wide, so ONE sign-in may earn several of these.")

    states = {r["state"] for r in rows}
    if states & {"FORGED-OVERRIDE", "CANNOT_ESTABLISH", "BUSY"}:
        return 3
    return 1 if "BLOCKED" in states else 0


def _has_deny_ace(migration: Path) -> bool:
    """Is the kernel-level write-deny still applied? This is the real gate state.

    Reads `denied_dirs` WITHOUT creating them: this is called from `verify`, which must not mutate
    the tree it judges. A directory that does not exist cannot carry a deny ACE, so skipping it is
    also the correct answer, not merely the safe one.
    """
    if platform.system() != "Windows":
        return (migration / MARKER).exists()
    for directory in _inspection_dirs(migration):
        if not directory.exists():
            continue
        if "(DENY)" in _acl_listing(directory).upper():
            return True
    return False


def _gate_was_ever_applied(migration: Path) -> bool:
    """Did this migration EVER have a gate, or was one never needed?

    `verify`'s unearned-clear check asks "was the lift earned?", which is only a meaningful question
    if something was ever lifted. For an extract-only migration -- every datasource a packaged
    `.hyper`/flat file -- step 6 correctly never applies a gate at all, so there is no lift, nothing
    to earn, and no probe to run (`probe_live_source.py` has no live source to probe).

    Measured 2026-08-08 on `book_5-2-LOD` (one embedded `excel-direct` datasource, zero live
    sources): `verify` reported `UNEARNED CLEAR - ... this model is UNVALIDATED. Do not ship it.` and
    exited 1 against a migration that was never gated. That is a false BLOCK on the *final* gate, and
    it fires for every extract-only migration -- i.e. exactly the shape most likely to be run
    offline, where a spurious "do not ship" is most likely to be believed.

    The signal is a committed arm row. An interrupted arm can leave only its physical marker/ACL;
    readers retain that stop rather than interpreting missing commit evidence as release.
    Same trust model as `_clear_was_earned`
    (an accountability trail, not proof) -- and no weaker, because anyone who could delete the log to
    fake "never gated" could equally append a fake `probe-cleared` to fake "earned".
    """
    entries = _audit_entries(migration)
    if entries is None:
        return False
    return any(entry.get("action") in BLOCK_ACTIONS for entry in entries)


def _spec_all_sources_are_flat_file(migration: Path) -> bool | None:
    """Does `migration-spec.json` classify EVERY declared data source as a valid flat file?

    Reuses the arming classifier, never a separate interpretation of extract-only (#354).
    Every leg must explicitly yield `no-creds`; live/review is False and unreadable is None.
    An empty/absent list is vacuously all-flat. Neither False nor None permits a never-gated claim.
    """
    try:
        spec = json.loads((migration / MIGRATION_SPEC).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict):
        return None
    sources = spec.get("data_sources", [])
    if not isinstance(sources, list):
        return None
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            return None
        for _key, _display, verdict, _reason in _classify_legs(source, index):
            if verdict != "no-creds":
                return False
    return True


def _current_live_source_keys(migration: Path) -> set[str] | None:
    """Current spec keys from the arming `_classify_legs`/`_leg_key`; None means unreadable, not matched."""
    try:
        spec = json.loads((migration / MIGRATION_SPEC).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict):
        return None
    sources = spec.get("data_sources", [])
    if not isinstance(sources, list):
        return None
    keys: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            return None
        for key, _display, verdict, _reason in _classify_legs(source, index):
            if verdict == "needs-credential":
                keys.add(key)
    return keys


def _source_set_mismatch_reason(migration: Path) -> str | None:
    """Why a trusted clearance no longer covers what the spec names TODAY, or None when it does.

    Compare current `_leg_key` identities with the COMPLETE per-source earned ledger (#354).
    The last block alone loses earlier, independently earned sources. Only a new uncovered key
    mismatches; dropping keys does not. Global human authorization bypasses this comparison.
    No earned sources, or unreadable/empty current keys, cannot establish a positive mismatch.
    """
    states, authorized = _earned_sources(migration)
    if authorized:
        return None
    recorded = {source for source, earned in states.items() if earned}
    if not recorded:
        return None
    current = _current_live_source_keys(migration)
    if not current:
        return None
    uncovered = current - recorded
    if not uncovered:
        return None
    return (
        f"{migration}'s {MIGRATION_SPEC} now names live source key(s) {sorted(uncovered)} not "
        f"covered by the recorded clearance for {sorted(recorded)}"
    )


def _no_audit_trail_reason(migration: Path, artifacts: list[Path]) -> str | None:
    """Why `verify` cannot even ask whether a gate applied here, or None when it legitimately can.

    Check trusted audit FIRST: a forced-scope arm can legitimately cover an unusual target.
    Engine roots require audit; only an explicitly all-flat parser spec can stand alone (#354).
    Packaged specs are copies, not gating history. Never search for an originating bundle or borrow
    ancestor audit. A copied flat spec beside unrelated artifacts remains the #391 residual.
    """
    if not artifacts:
        return None
    if _audit_entries(migration) is not None:
        return None
    refusal = _scope_refusal(migration)
    if refusal is not None:
        return refusal
    if (migration / ENGINE_RECEIPT).is_file() or (migration / "input_manifest.json").is_file():
        return (
            f"{migration} carries an engine-bundle scope marker but no trusted audit entry - a "
            "genuine engine bundle root always has one (run_estate.py records an 'engine-receipt' "
            "entry unconditionally), so this is a copy of that marker, not the bundle root itself"
        )
    all_flat = _spec_all_sources_are_flat_file(migration)
    if all_flat is not True:
        return f"{migration}'s {MIGRATION_SPEC} " + (
            "could not be read to confirm every data source is an explicitly, validly classified flat file"
            if all_flat is None
            else "does not classify every declared data source as flat_file - an unknown, missing, "
            "malformed, unsupported, or live target cannot be assumed extract-only"
        )
    return None


def _log_cannot_assess(reason: str) -> None:
    """Explain a state-3 verdict: report only, never returns a code (`_verify_one` owns that)."""
    log.error("GATE VERIFY: CANNOT ASSESS - no '%s' audit log exists at this path, and it is", AUDIT)
    log.error("  not identifiable as a place a gate could ever have been armed (%s).", reason)
    log.error("  This is the shape of a ship-destination copy: built artifacts are copied to")
    log.error("  'migrations/{workbooks,datasources}/<slug>/fabric/', but the audit log is not - it")
    log.error("  lives at the bundle/spec root where the gate was armed. Run 'verify' THERE instead.")
    log.error("  Reporting OK here would be indistinguishable from a migration that was genuinely")
    log.error("  never gated, which is exactly the false-clean issue #354 exists to close.")


def _log_source_mismatch(reason: str) -> None:
    """Explain a state-3 verdict from a stale/swapped source clearance (issue #354 review)."""
    log.error("GATE VERIFY: CANNOT ASSESS - the recorded clearance no longer covers what this")
    log.error("  migration's %s names today (%s).", MIGRATION_SPEC, reason)
    log.error("  A trusted 'block'/earned-clear history is scoped to the SOURCES it named, not just")
    log.error("  the directory: re-arm the gate and re-prove reachability for the new source before")
    log.error("  this can verify clean.")


def _log_ok_verdict(migration: Path, *, authentic: bool, marker: bool, deny: bool) -> None:
    """Report WHY a clean `verify` passed - never returns a code (`_verify_one` owns that)."""
    if authentic:
        log.info("GATE VERIFY: OK - build-only run authorized by a human (audit-backed).")
    elif marker or deny:
        log.info("GATE VERIFY: OK - gate applied, no model/report artifacts exist.")
    elif not _gate_was_ever_applied(migration):
        # Say WHY it passed, so an extract-only pass is never confused with a gate that was lifted.
        log.info("GATE VERIFY: OK - no gate was ever applied to this migration (no 'block' entry in")
        log.info("  the audit log), so there was no lift to earn. Expected for an extract-only")
        log.info("  migration where every datasource is a packaged/flat file and step 6 correctly")
        log.info("  raised no gate. NOTE: this attests the gate's own history, not that the source")
        log.info("  classification was right - that is step 6/6b's job.")
    else:
        log.info("GATE VERIFY: OK - gate not applied.")


def _verify_one(migration: Path) -> int:
    """Authoritative post-hoc check: did anything get built while the gate was up?

    This is the compensating control for everything enforcement cannot guarantee, and it is
    deliberately based on the ACL and the audit log rather than on the marker or override files -
    because those are files, and an agent can write files. Four distinct violations are reported:

      1. artifacts exist while the gate is still applied;
      2. an override file exists with no matching `authorize` audit entry (forged);
      3. the ACL is gone while the marker remains (someone lifted enforcement out of band);
      4. artifacts were built after a bare `clear` that earned nothing (UNEARNED CLEAR).

    None of which applies when a gate was never raised in the first place - see
    `_gate_was_ever_applied`.

    A FIFTH state is checked before any of those: no audit history exists at this path AT ALL, and
    the path does not look like a place a gate could ever have been armed either (issue #354). That
    is neither "passed" nor "failed" - it is "this check could not be performed here" - and it must
    exit differently from both, or a ship-destination copy with no audit log reads exactly like a
    migration that was legitimately never gated. See `_no_audit_trail_reason`.

    A SIXTH state (issue #354 review) is checked right after: a trusted `block`/earned-`clear`
    history CAN exist and still not certify what is here TODAY, if the spec was re-pointed at a
    different live source in the same directory without ever being re-gated. See
    `_source_set_mismatch_reason`.
    """
    artifacts = audited_paths(migration)
    no_audit_trail = _no_audit_trail_reason(migration, artifacts)
    if no_audit_trail is not None:
        _log_cannot_assess(no_audit_trail)
        return 3

    source_mismatch = _source_set_mismatch_reason(migration)
    if source_mismatch is not None:
        _log_source_mismatch(source_mismatch)
        return 3

    marker = (migration / MARKER).exists()
    override_file = (migration / OVERRIDE).exists()
    authentic = _override_is_authentic(migration)
    try:
        deny = _has_deny_ace(migration)
        covered = _all_targets_denied(migration) if marker and platform.system() == "Windows" else deny
    except (OSError, ValueError, subprocess.SubprocessError):
        _acl_refusal()
        return 3
    violations = 0

    if override_file and not authentic:
        log.error("GATE VERIFY: FORGED OVERRIDE - %s exists but no 'authorize' audit entry backs it.", OVERRIDE)
        log.error("  An agent created this file. It authorizes nothing. Treat this run as unvalidated.")
        _audit(migration, "violation", "forged override")
        violations += 1

    if marker and not covered and not authentic:
        log.error("GATE VERIFY: ENFORCEMENT REMOVED - marker present but not every target has its write-deny ACE.")
        violations += 1

    # A gate that is down must have been EARNED - by a successful probe or a human authorization.
    # Measured: `clear --reason "I decided it is fine"` lifted the ACL and the build proceeded with
    # no probe ever run. Enforcement cannot prevent that (clear has to exist for teardown), but it
    # must never pass silently, or the guarantee is gone via the front door.
    pre_gate_engine_artifacts, gate_artifacts = _split_pre_gate_engine_artifacts(migration, artifacts)
    if artifacts and not deny and not _clear_was_earned(migration) and _gate_was_ever_applied(migration):
        log.error("GATE VERIFY: UNEARNED CLEAR - artifacts exist, but no successful probe and no")
        log.error("  human authorization is recorded in the audit log. The gate was lifted without")
        log.error("  proving the source is reachable, so this model is UNVALIDATED. Do not ship it.")
        _audit(migration, "violation", "artifacts built after an unearned clear")
        violations += 1

    if (marker or deny) and not authentic:
        if pre_gate_engine_artifacts:
            log.warning(
                "GATE VERIFY: PRE-GATE TIER OUTPUT - %d engine artifact(s) predate the latest gate arm.",
                len(pre_gate_engine_artifacts),
            )
            log.warning(
                "  On the engine path the gate is a detection control: these files are unvalidated "
                "until the probe clears the gate, but they were not built while blocked."
            )
        if gate_artifacts:
            log.error("GATE VERIFY: VIOLATION - %d artifact(s) exist while the gate is applied:", len(gate_artifacts))
            for p in gate_artifacts[:10]:
                log.error("  %s", p)
            log.error("Built against a source whose reachability was never proven. Do not ship them.")
            _audit(migration, "violation", f"{len(gate_artifacts)} artifacts while blocked")
            violations += 1

    if violations:
        return 1
    _log_ok_verdict(migration, authentic=authentic, marker=marker, deny=deny)
    return 0


@_coordinated(reader=True)
def verify(migration: Path) -> int:
    """Verify one audit-bearing migration/bundle target.

    Exit 0 = earned/legitimately never needed; 1 = failed/unearned; 3 = no attributable audit.
    A ship-destination copy is not its audit-bearing origin (#354). Exit 3 is never clean.
    """
    if ((migration / MARKER).exists() or os.path.lexists(migration / AUDIT)) and _audit_entries(migration) is None:
        _log_cannot_assess("untrusted audit trail")
        return 3
    result = _verify_one(migration)
    if (
        result == 0
        and not (migration / MARKER).exists()
        and any(SOURCE_KEY_RE.fullmatch(key) for key in _transaction_pending(migration))
    ):
        return 3
    if (
        result == 0
        and _override_is_authentic(migration)
        and ((migration / MARKER).exists() or _has_deny_ace(migration))
    ):
        return 3
    return result


# Phase-1 authority is read-only: verify() appends audit rows and cannot produce this projection.

DATA_ACCESS_SCHEMA = "phase1-data-access/v1"

DATA_ACCESS_STATES = (
    "local_import_ready",
    "live_data_ok",
    "authorized_model_only",
    "provider_inherited",
    "blocked",
    "cannot_establish",
)
DIRECT_ACCEPTED_STATES = ("local_import_ready", "live_data_ok", "authorized_model_only")
VALIDATION_STATES = ("validated", "unvalidated", "not_established")
EFFECTIVE_SCOPES = ("model_and_report", "model_only", "report_only_shared_model")
DIRECT_SCOPES = ("model_and_report", "model_only")
CLAIM_CEILINGS = ("data_validated", "structural_only", "none")
FALLBACK_POLICIES = ("stop", "model_only_unvalidated")

ACCEPTED_CODES = frozenset(
    {
        "all-flat-file",
        "package-self-contained",
        "probe-data-ok",
        "probe-cleared",
        "human-authorize",
        "brief-model-only",
        "provider-exact",
    }
)
BLOCKING_CODES = frozenset(
    {
        "credential-present-only",
        "marker-only",
        "manual-clear",
        "stale-clear",
        "unknown-target",
        "local-import-incomplete",
        "probe-operator-required",
        "probe-no-credential",
        "probe-access-denied",
        "probe-unreachable",
        "probe-error",
        "probe-bad-table",
        "live-probe-skipped",
        "authorization-mismatch",
        "provider-model-only",
    }
)
CANNOT_CODES = frozenset(
    {
        AUDIT_MISSING,
        AUDIT_MALFORMED,
        AUDIT_FOREIGN_SCOPE,
        "forced-scope",
        "spec-unreadable",
        "source-key-invalid",
        "source-key-set-changed",
        "projection-invalid",
        "provider-missing",
        "provider-ambiguous",
        "provider-foreign",
        "transaction-refused",
    }
)
DATA_ACCESS_CODES = ACCEPTED_CODES | BLOCKING_CODES | CANNOT_CODES

# Closed projection identities: no endpoint/display text, paths or normalization.
SOURCE_KEY_RE = re.compile(r"^source-key:[0-9a-f]{16}$")
PROVIDER_REFERENCE_RE = re.compile(r"provider-ref:v1:sha256:[0-9a-f]{64}")

# Attempts are not clears. Reserved operator_required/credential_present remain blocking even
# though today's classifier does not emit them; only probe-data_ok can supply measured success.
PROBE_ATTEMPT_CODES = {
    "probe-operator_required": "probe-operator-required",
    "probe-no_credential": "probe-no-credential",
    "probe-access_denied": "probe-access-denied",
    "probe-unreachable": "probe-unreachable",
    "probe-error": "probe-error",
    "probe-bad_table": "probe-bad-table",
    "probe-skipped": "live-probe-skipped",
    "probe-credential_present": "credential-present-only",
}
PROBE_DATA_OK = "probe-data_ok"
PROBE_CLEARED = "probe-cleared"

# Gate transitions and probe attempts share _audit's base fields, not its optional sources field.
# Unkeyed historical attempts/clears remain readable; the ledger never earns proof from them.
AUDIT_SOURCE_RULES = {
    **dict.fromkeys(BLOCK_ACTIONS, "arm"),
    **dict.fromkeys((*PROBE_ATTEMPT_CODES, PROBE_DATA_OK, PROBE_CLEARED, "violation"), "optional"),
    **dict.fromkeys(("authorize", "manual-clear", "block-skipped", "block-forced-scope", "engine-receipt"), "absent"),
}

DATA_ACCESS_REJECTIONS = (
    "unreadable",
    "malformed-json",
    "duplicate-key",
    "nonfinite",
    "not-an-object",
    "unknown-field",
    "missing-field",
    "bad-type",
    "unknown-value",
    "source-key-invalid",
    "provider-unit-invalid",
    "source-keys-unsorted",
    "codes-unsorted",
    "illegal-combination",
)

DATA_ACCESS_FIELDS = (
    "schema",
    "state",
    "source_keys",
    "provider_unit",
    "provider_state",
    "validation",
    "effective_scope",
    "max_phase2_claim",
    "codes",
)


class DataAccessProjectionError(ValueError):
    """Closed projection-invalid refusal; diagnostics carry only a DATA_ACCESS_REJECTIONS reason."""

    code = "projection-invalid"

    def __init__(self, reason: str) -> None:
        super().__init__(f"data-access projection rejected: {reason}")
        self.reason = reason


class _DuplicateJsonKey(Exception):
    """A duplicate object key seen by `json.loads`' pairs hook. Never escapes this module."""


class _NonFiniteJsonConstant(Exception):
    """`NaN`/`Infinity` seen by `json.loads`. Never escapes this module."""


class DataAccessAssessment(NamedTuple):
    """Opaque provider-reference projection; NamedTuple supports the hook's unregistered exec_module."""

    state: str
    source_keys: tuple[str, ...]
    provider_unit: str | None
    provider_state: str | None
    validation: str
    effective_scope: str | None
    max_phase2_claim: str
    codes: tuple[str, ...]

    def to_json(self) -> dict:
        """Transcribe validated fields without repairing their identities or ordering."""
        return {
            "schema": DATA_ACCESS_SCHEMA,
            "state": self.state,
            "source_keys": list(self.source_keys),
            "provider_unit": self.provider_unit,
            "provider_state": self.provider_state,
            "validation": self.validation,
            "effective_scope": self.effective_scope,
            "max_phase2_claim": self.max_phase2_claim,
            "codes": list(self.codes),
        }

    def dumps(self) -> str:
        """Byte-stable serialization. Same input -> same bytes, so S1 can hash it meaningfully."""
        return json.dumps(self.to_json(), indent=2, allow_nan=False, ensure_ascii=False) + "\n"


def _assessment(  # pylint: disable=too-many-arguments
    state: str,
    *,
    codes: tuple[str, ...] | list[str],
    source_keys: tuple[str, ...] | list[str] = (),
    provider_unit: str | None = None,
    provider_state: str | None = None,
    validation: str = "not_established",
    effective_scope: str | None = None,
    max_phase2_claim: str = "none",
) -> DataAccessAssessment:
    """Build from already-validated source identities; canonicalize only the finding codes."""
    return DataAccessAssessment(
        state=state,
        source_keys=tuple(source_keys),
        provider_unit=provider_unit,
        provider_state=provider_state,
        validation=validation,
        effective_scope=effective_scope,
        max_phase2_claim=max_phase2_claim,
        codes=tuple(sorted(set(codes))),
    )


def _cannot_establish(*codes: str) -> DataAccessAssessment:
    """A refusal that carries NO source keys: if the authority is untrusted, so are its keys."""
    return _assessment("cannot_establish", codes=codes)


def _spec_source_legs(source: object, index: int) -> list[tuple[str, str, str, str]] | None:
    """Validate shapes before the canonical classifier's fallback/equality operations."""
    if not isinstance(source, Mapping) or not isinstance(source.get("connection", {}), Mapping):
        return None
    connection = source.get("connection", {})
    legs = connection.get("connections", [])
    if not isinstance(legs, list) or not all(isinstance(leg, Mapping) for leg in legs):
        return None
    for field in ("tables", "fields"):
        if field in source and (
            not isinstance(source[field], list) or not all(isinstance(item, Mapping) for item in source[field])
        ):
            return None
    for leg in (connection, *legs):
        port = leg.get("port")
        if any(
            leg.get(field) is not None and not isinstance(leg[field], str)
            for field in (
                "class",
                "mode",
                "powerbi_target",
                "powerbi_target_reason",
                "server",
                "database",
                "dbname",
                "schema",
                "http_path",
                "warehouse",
                "role",
            )
        ) or (port is not None and (isinstance(port, bool) or not isinstance(port, (str, int)))):
            return None
    try:
        return _classify_legs(dict(source), index)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _package_spec_facts(package_spec: object) -> tuple[tuple[str, ...], bool, str | None]:
    """(sorted live keys, has review legs, refusal); an unstable/duplicate identity cannot bind."""
    if not isinstance(package_spec, Mapping):
        return (), False, "spec-unreadable"
    sources = package_spec.get("data_sources", [])
    if not isinstance(sources, list):
        return (), False, "spec-unreadable"
    keys: list[str] = []
    review = False
    for index, source in enumerate(sources):
        legs = _spec_source_legs(source, index)
        if legs is None:
            return (), False, "spec-unreadable"
        for key, _display, verdict, _reason in legs:
            if verdict == "needs-credential":
                if not SOURCE_KEY_RE.fullmatch(key):
                    return (), False, "source-key-invalid"
                keys.append(key)
            elif verdict != "no-creds":
                review = True
    if len(set(keys)) != len(keys):
        return (), False, "source-key-invalid"
    return tuple(sorted(keys)), review, None


class PackageSpecFacts(NamedTuple):
    """Current direct-source facts only; applicability flags never authorize provider inheritance."""

    live_source_keys: tuple[str, ...]
    has_review: bool
    direct_applicable: bool
    published_only: bool
    refusal_code: str | None

    @property
    def all_flat(self) -> bool:
        """The canonical classifier found only no-credential legs, not an absent published provider."""
        return self.direct_applicable and not self.live_source_keys and not self.has_review


def _published_only_row(row: object) -> bool:
    """A scalar sqlproxy reference; legacy annotations may be absent, never explicitly unknown."""
    if not isinstance(row, Mapping) or set(row) - {
        "id",
        "caption",
        "internal_name",
        "connection",
        "published_datasource",
        "tables",
        "joins",
        "fields",
    }:
        return False
    if any(
        key in row and (not isinstance(row[key], list) or not all(isinstance(item, Mapping) for item in row[key]))
        for key in ("tables", "joins", "fields")
    ) or any(row.get(key) is not None and not isinstance(row[key], str) for key in ("id", "caption", "internal_name")):
        return False
    connection, published = row.get("connection"), row.get("published_datasource")
    for value, fields in (
        (
            connection,
            {"class", "mode", "server", "database", "hyper_file", "powerbi_target", "powerbi_target_reason", "note"},
        ),
        (published, {"id", "site", "path", "derived_from", "revision", "name_source", "id_attribute", "luid", "key"}),
    ):
        if (
            not isinstance(value, Mapping)
            or set(value) - fields
            or any(item is not None and not isinstance(item, str) for item in value.values())
        ):
            return False
    luid = published.get("luid")
    valid_luid = not luid or re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", luid
    )
    valid_target = connection.get("powerbi_target", "live_source") in ("live_source", "flat_file")
    if (
        connection.get("class") != "sqlproxy"
        or connection.get("mode") not in ("live", "extract")
        or not valid_target
        or not any(isinstance(published.get(key), str) and published[key].strip() for key in ("luid", "key"))
        or not valid_luid
    ):
        return False
    pending = [value for key, value in row.items() if key not in ("connection", "published_datasource")]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            if {"class", "connection", "connections"} & value.keys():
                return False
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return True


def package_spec_facts(package_spec: object) -> PackageSpecFacts:
    """Sole package source authority; strict published references have no direct database leg.

    Keep parser metadata intact. All other source sets retain the canonical direct-leg derivation;
    self-publication metadata does not erase a datasource's own connection. S2 owns provider selection.
    """
    sources = package_spec.get("data_sources") if isinstance(package_spec, Mapping) else None
    if not isinstance(sources, list):
        return PackageSpecFacts((), False, False, False, "spec-unreadable")
    if sources and all(_published_only_row(row) for row in sources):
        return PackageSpecFacts((), False, False, True, None)
    keys, review, refusal = _package_spec_facts(package_spec)
    direct = refusal is None and all(
        isinstance(row, Mapping)
        and isinstance(row.get("connection", {}), Mapping)
        and row.get("connection", {}).get("class") != "sqlproxy"
        for row in sources
    )
    return PackageSpecFacts(keys, review, direct, False, refusal)


def _gate_root_live_keys(gate_root: Path) -> tuple[frozenset[str], str | None]:
    """Validate raw spec facts before the bundle adapter can deduplicate/filter current keys."""
    try:
        spec_path = gate_root / MIGRATION_SPEC
        spec = (
            json.loads(
                spec_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
            if spec_path.is_file()
            else {"data_sources": load_bundle(gate_root).data_sources}
        )
    except (OSError, ValueError, TypeError, AttributeError, _DuplicateJsonKey, _NonFiniteJsonConstant, RecursionError):
        return frozenset(), "spec-unreadable"
    keys, _review, refusal = _package_spec_facts(spec)
    return frozenset(keys), refusal


def _package_local_facts(package_data_sources: object) -> tuple[bool, str | None]:
    """Validate localization types before interpreting completeness; binding stays Phase-2 work."""
    if not isinstance(package_data_sources, Mapping):
        return False, "spec-unreadable"
    fields = {"self_contained": bool, "omissions": list, "neutralized": list, "retained_network": list}
    optional = {"shipped": list, "binding": (Mapping, type(None)), "parameter": (str, type(None)), "bytes": int}
    for field, expected in (fields | optional).items():
        if field in package_data_sources and not isinstance(package_data_sources[field], expected):
            return False, "spec-unreadable"
    if isinstance(package_data_sources.get("bytes"), bool):
        return False, "spec-unreadable"
    for field, item_type in (
        ("omissions", Mapping),
        ("shipped", Mapping),
        ("neutralized", str),
        ("retained_network", str),
    ):
        if not all(isinstance(item, item_type) for item in package_data_sources.get(field, [])):
            return False, "spec-unreadable"
    complete = (
        fields.keys() <= package_data_sources.keys()
        and package_data_sources["self_contained"]
        and not any(package_data_sources[field] for field in ("omissions", "neutralized", "retained_network"))
    )
    return bool(complete), None


def _new_key_state() -> dict:
    """One live key's per-epoch ledger slot."""
    return {"armed": None, "data_ok": None, "earned": False, "stale": False, "manual": False, "failure": None}


def _apply_arm(tracked: dict[str, dict], sources: list[str] | None, timestamp: datetime) -> None:
    """Start named keys' epochs; unattributable arms invalidate every tracked key."""
    targets = list(tracked) if sources is None else sources
    for key in targets:
        if key in tracked:
            tracked[key] = _new_key_state()
            tracked[key]["armed"] = timestamp if sources is not None else None


def _apply_probe_attempt(tracked: dict[str, dict], action: str, sources: list[str] | None, timestamp: datetime) -> None:
    """Ignore unkeyed successes; unknown failures invalidate all keys. New successes need a clear."""
    if action == PROBE_DATA_OK:
        for key in sources or []:
            state = tracked.get(key)
            if state is not None:
                state["earned"] = False
                if state["armed"] is not None and timestamp >= state["armed"]:
                    state["data_ok"] = timestamp
                    state["failure"] = None
                else:
                    state["data_ok"] = None
                    state["stale"] = True
        return
    code = PROBE_ATTEMPT_CODES.get(action, "probe-error")
    for key in list(tracked) if sources is None else sources:
        state = tracked.get(key)
        if state is not None:
            state["failure"] = code
            state["data_ok"] = None
            state["earned"] = False


def _apply_clear(tracked: dict[str, dict], sources: list[str] | None, timestamp: datetime) -> None:
    """Require a current-epoch keyed pair; unattributable clears cannot earn or erase prior proof."""
    attributable = sources is not None
    for key, state in tracked.items():
        if attributable and key not in sources:
            continue
        if attributable and state["data_ok"] is not None and timestamp >= state["data_ok"] and state["failure"] is None:
            state["earned"] = True
        elif not state["earned"]:
            state["stale"] = True


def _key_block_code(state: dict) -> str | None:
    """Missing identity coverage outranks findings; then measurement, stale/manual clear, marker."""
    if state["armed"] is None:
        return "source-key-set-changed"
    if state["earned"]:
        return None
    if state["failure"]:
        return str(state["failure"])
    if state["stale"]:
        return "stale-clear"
    if state["manual"]:
        return "manual-clear"
    return "marker-only"


def _data_access_ledger(entries: list[dict], live_keys: tuple[str, ...]) -> tuple[dict[str, str | None], bool]:
    """Fold one trusted snapshot by file order, retaining each key's arm/measurement timestamps."""
    tracked = {key: _new_key_state() for key in live_keys}
    authorized = False
    for entry in entries:
        action = entry["action"]
        timestamp = datetime.fromisoformat(entry["ts"])
        sources = _entry_sources(entry) or None
        # Legacy names/empty lists cannot identify a key; they must not turn failures into no-ops.
        if sources and not all(SOURCE_KEY_RE.fullmatch(source) for source in sources):
            sources = None
        if action in BLOCK_ACTIONS:
            _apply_arm(tracked, sources, timestamp)
            authorized = False
        elif action == "authorize":
            authorized = bool(tracked) and all(
                state["armed"] is not None and timestamp >= state["armed"] for state in tracked.values()
            )
        elif action == "manual-clear":
            for state in tracked.values():
                state["manual"] = True
        elif action == PROBE_CLEARED:
            _apply_clear(tracked, sources, timestamp)
        elif action.startswith("probe-"):
            _apply_probe_attempt(tracked, action, sources, timestamp)
    return {key: _key_block_code(state) for key, state in tracked.items()}, authorized


def _authorization_state(
    gate_root: Path, ledger_authorized: bool, fallback_authorization: str, requested_scope: str, trail: list[dict]
) -> tuple[bool, bool]:
    """(complete authorization, mismatched inputs), using the same already-read audit snapshot."""
    authorized = ledger_authorized and _override_is_authentic(gate_root, entries=trail)
    wants = fallback_authorization == "model_only_unvalidated"
    if authorized and wants and requested_scope == "model_only":
        return True, False
    return False, authorized or wants


def _blocked_assessment(
    live_keys: tuple[str, ...], key_codes: dict[str, str | None], extra: list[str]
) -> DataAccessAssessment:
    """An untrusted identity is cannot-establish, not a finding about that source's data."""
    codes = [code for code in key_codes.values() if code] + extra
    fatal = [code for code in codes if code in CANNOT_CODES]
    if fatal:
        return _cannot_establish(*fatal)
    return _assessment("blocked", codes=codes or ["marker-only"], source_keys=live_keys)


def provider_reference(unit: str) -> str:
    """Versioned SHA-256 reference of an exact S2 component; no search, normalization or lost code points."""
    _require(isinstance(unit, str) and "/" not in unit and is_canonical_key(unit), "provider-unit-invalid")
    digest = hashlib.sha256(
        b"phase1-data-access/provider-unit/v1\0" + unit.encode("utf-8", "surrogatepass")
    ).hexdigest()
    return f"provider-ref:v1:sha256:{digest}"


def _is_data_access_assessment(assessment: object) -> bool:
    """Reuse projection legality without replacing or normalizing the supplied assessment."""
    if not isinstance(assessment, DataAccessAssessment):
        return False
    if not isinstance(assessment.source_keys, tuple) or not isinstance(assessment.codes, tuple):
        return False
    try:
        return parse_data_access(assessment.dumps()) == assessment
    except (ValueError, TypeError, RecursionError):
        return False


def _is_provider_pair(provider: object) -> bool:
    """Validate one opaque reference/assessment pair, never search or normalize identities."""
    return (
        isinstance(provider, tuple)
        and len(provider) == 2
        and _valid_provider_reference(provider[0])
        and _is_data_access_assessment(provider[1])
    )


def _valid_provider_reference(reference: object) -> bool:
    """Accept only the versioned digest token, never a raw S2 unit or a path."""
    return isinstance(reference, str) and PROVIDER_REFERENCE_RE.fullmatch(reference) is not None


def _provider_candidates(provider: object) -> list | None:
    """Snapshot S2's supplied candidates; None means malformed, [] means no match. Never search."""
    if provider is None:
        return []
    if _is_provider_pair(provider):
        return [provider]
    if isinstance(provider, list):
        candidates = list(provider)
        return candidates if all(_is_provider_pair(candidate) for candidate in candidates) else None
    return None


def _resolve_single_provider(provider: object) -> tuple[tuple | None, str | None]:
    """Check supplied cardinality, not identity matching: zero, one, or ambiguous multiple pairs."""
    candidates = _provider_candidates(provider)
    if candidates is None:
        return None, "provider-foreign"
    if not candidates:
        return None, "provider-missing"
    if len(candidates) > 1:
        return None, "provider-ambiguous"
    return candidates[0], None


def _inherit_from_provider(provider: object, requested_scope: str) -> DataAccessAssessment:
    """Inherit S2's one exact direct provider, without search, recursion or strengthening its claim."""
    candidate, refusal = _resolve_single_provider(provider)
    if refusal:
        return _cannot_establish(refusal)
    reference, assessment = candidate
    if assessment.state == "provider_inherited":
        return _cannot_establish("provider-ambiguous")
    if assessment.state not in DIRECT_ACCEPTED_STATES:
        return _cannot_establish("provider-missing")
    if assessment.state == "authorized_model_only" and requested_scope != "model_only":
        return _assessment("blocked", codes=["provider-model-only"])
    return _assessment(
        "provider_inherited",
        codes=["provider-exact"],
        source_keys=assessment.source_keys,
        provider_unit=reference,
        provider_state=assessment.state,
        validation=assessment.validation,
        effective_scope=requested_scope,
        max_phase2_claim=assessment.max_phase2_claim,
    )


def _live_evidence_refusal(gate_root: Path, live_keys: tuple[str, ...], trail: list[dict] | None) -> str | None:
    """Reject forced scope and missing root-key coverage before interpreting per-key evidence."""
    if trail is None:
        return None  # caller already turned this into an audit-* refusal
    if any(entry.get("action") == "block-forced-scope" for entry in trail):
        return "forced-scope"
    if not live_keys:
        return None
    root_keys, refusal = _gate_root_live_keys(gate_root)
    if refusal:
        return refusal
    if set(live_keys) - root_keys:
        return "source-key-set-changed"
    return None


def _direct_evidence(gate_root: Path, live_keys: tuple[str, ...], policy: str) -> tuple[list[dict] | None, str | None]:
    """Read once. Live/fallback authority requires a trusted trail; local bytes alone do not."""
    trail, trail_refusal = _read_audit_trail(gate_root)
    if not (bool(live_keys) or policy == "model_only_unvalidated" or (gate_root / OVERRIDE).exists()):
        return [], None
    if trail is None:
        return None, trail_refusal or AUDIT_MISSING
    refusal = _live_evidence_refusal(gate_root, live_keys, trail)
    return (None, refusal) if refusal else (trail, None)


def _package_findings(has_review: bool, local_complete: bool, auth_mismatch: bool) -> list[str]:
    """Package-level blocking codes that are true regardless of any single source key."""
    findings = ["unknown-target"] if has_review else []
    if not local_complete:
        findings.append("local-import-incomplete")
    if auth_mismatch:
        findings.append("authorization-mismatch")
    return findings


def _accepted_direct(live_keys: tuple[str, ...], requested_scope: str) -> DataAccessAssessment:
    """The accepted direct state once every check has passed: live if there are keys, else local."""
    if live_keys:
        return _assessment(
            "live_data_ok",
            codes=["probe-data-ok", "probe-cleared"],
            source_keys=live_keys,
            validation="validated",
            effective_scope=requested_scope,
            max_phase2_claim="data_validated",
        )
    return _assessment(
        "local_import_ready",
        codes=["all-flat-file", "package-self-contained"],
        validation="validated",
        effective_scope=requested_scope,
        max_phase2_claim="data_validated",
    )


@_coordinated(reader=True, refusal=lambda: _cannot_establish("transaction-refused"))
def _assess_direct(
    gate_root: Path,
    live_keys: tuple[str, ...],
    has_review: bool,
    package_data_sources: object,
    policy: tuple[str, str],
) -> DataAccessAssessment:
    """Conjoin the existing ledger policy with the physical state under the same descriptor."""
    result, trail = _assess_direct_locked(gate_root, live_keys, has_review, package_data_sources, policy)
    if result.state == "local_import_ready":
        return result
    if result.state in DIRECT_ACCEPTED_STATES:
        physical = inspect_physical_barrier(gate_root)[0]
        if physical == "cannot_establish":
            return _cannot_establish("transaction-refused")
        if physical != "clear" and not _completed_partial_clear(gate_root, live_keys, trail or []):
            return _assessment("blocked", codes=["marker-only"], source_keys=live_keys)
    return result


def _completed_partial_clear(root: Path, selected: tuple[str, ...], entries: list[dict]) -> bool:
    """A completed scoped projection is not permission to release the root's sibling stop."""
    marked = set(_established_sources(root))
    if not marked or marked.intersection(selected):
        return False
    keys, refusal = _gate_root_live_keys(root)
    if refusal:
        return False
    states, authorized = _data_access_ledger(entries, tuple(sorted(keys)))
    pending = {key for key, code in states.items() if code is not None}
    return not authorized and marked == pending


def _assess_direct_locked(
    gate_root: Path,
    live_keys: tuple[str, ...],
    has_review: bool,
    package_data_sources: object,
    policy: tuple[str, str],
) -> tuple[DataAccessAssessment, list[dict] | None]:
    """Assess direct authority against package facts and one same-root ledger snapshot."""
    fallback_authorization, requested_scope = policy
    local_complete, local_refusal = _package_local_facts(package_data_sources)
    if local_refusal:
        return _cannot_establish(local_refusal), None

    trail, evidence_refusal = _direct_evidence(gate_root, live_keys, fallback_authorization)
    if evidence_refusal:
        return _cannot_establish(evidence_refusal), None

    key_codes, ledger_authorized = _data_access_ledger(trail or [], live_keys)
    authorized, auth_mismatch = _authorization_state(
        gate_root, ledger_authorized, fallback_authorization, requested_scope, trail or []
    )
    if not has_review and local_complete and not any(key_codes.values()):
        return _accepted_direct(live_keys, requested_scope), trail
    if (
        authorized
        and live_keys
        and not has_review
        and local_complete
        and not any(code in CANNOT_CODES for code in key_codes.values())
    ):
        return _assessment(
            "authorized_model_only",
            codes=["human-authorize", "brief-model-only"],
            source_keys=live_keys,
            validation="unvalidated",
            effective_scope="model_only",
            max_phase2_claim="structural_only",
        ), trail
    return _blocked_assessment(
        live_keys, key_codes, _package_findings(has_review, local_complete, auth_mismatch)
    ), trail


def assess_data_access(  # pylint: disable=too-many-arguments
    gate_root: Path,
    *,
    package_spec: Mapping,
    package_data_sources: Mapping,
    fallback_authorization: str,
    requested_scope: str,
    provider: tuple[str, DataAccessAssessment] | list | None = None,
) -> DataAccessAssessment:
    """Read-only authority over this root's audit/spec and supplied package facts or S2 provider.

    Invalid policy/scope raises ValueError; malformed evidence returns a typed refusal.
    No probe, mutation or ancestor search. Provider pairs carry provider_reference(), not raw names.
    """
    if fallback_authorization not in FALLBACK_POLICIES:
        raise ValueError(f"fallback_authorization must be one of {FALLBACK_POLICIES}")
    if requested_scope not in EFFECTIVE_SCOPES:
        raise ValueError(f"requested_scope must be one of {EFFECTIVE_SCOPES}")

    if provider is not None or requested_scope == "report_only_shared_model":
        return _inherit_from_provider(provider, requested_scope)
    live_keys, has_review, spec_refusal = _package_spec_facts(package_spec)
    if spec_refusal:
        return _cannot_establish(spec_refusal)
    return _assess_direct(
        gate_root, live_keys, has_review, package_data_sources, (fallback_authorization, requested_scope)
    )


def reconcile_package_data_access(  # pylint: disable=too-many-return-statements,too-many-branches
    assessment: DataAccessAssessment,
    facts: PackageSpecFacts,
    *,
    requested_scope: str | None,
    fallback_authorization: str | None,
    provider: tuple[str, DataAccessAssessment] | None = None,
) -> DataAccessAssessment:
    """Conjoin held projection/facts/brief and one already-checked S2 provider, without I/O.

    This never earns proof or repairs a projection. Accepted and pre-existing refused assessments
    retain their exact object and ceiling. Callers check each provider against its own facts first.
    """
    if not _is_data_access_assessment(assessment):
        return _cannot_establish("projection-invalid")
    if assessment.state in ("blocked", "cannot_establish"):
        return assessment
    if not isinstance(facts, PackageSpecFacts):
        return _cannot_establish("projection-invalid")
    if assessment.state == "provider_inherited" and not facts.published_only:
        return _cannot_establish(
            "provider-ambiguous"
            if facts.direct_applicable and requested_scope in DIRECT_SCOPES
            else "projection-invalid"
        )
    if facts.refusal_code is not None:
        return _cannot_establish(facts.refusal_code)
    if facts.has_review:
        return _assessment("blocked", codes=["unknown-target"], source_keys=facts.live_source_keys)
    if (
        requested_scope not in EFFECTIVE_SCOPES
        or fallback_authorization not in FALLBACK_POLICIES
        or assessment.effective_scope != requested_scope
    ):
        return _assessment("blocked", codes=["authorization-mismatch"], source_keys=facts.live_source_keys)
    if assessment.state != "provider_inherited":
        if not facts.direct_applicable or facts.published_only:
            return _cannot_establish("projection-invalid")
        if provider is not None:
            return _cannot_establish("provider-foreign")
        if assessment.source_keys != facts.live_source_keys:
            return _cannot_establish("source-key-set-changed")
        if assessment.state == "local_import_ready" and not facts.all_flat:
            return _cannot_establish("projection-invalid")
        if assessment.state == "authorized_model_only" and fallback_authorization != "model_only_unvalidated":
            return _assessment("blocked", codes=["authorization-mismatch"], source_keys=facts.live_source_keys)
        return assessment
    if not facts.published_only or facts.direct_applicable or facts.live_source_keys:
        return _cannot_establish("projection-invalid")
    if requested_scope != "report_only_shared_model":
        return _assessment("blocked", codes=["authorization-mismatch"])
    if provider is None:
        return _cannot_establish("provider-missing")
    if not _is_provider_pair(provider):
        return _cannot_establish("provider-foreign")
    reference, supplied = provider
    if supplied.state == "provider_inherited":
        return _cannot_establish("provider-ambiguous")
    if supplied.state not in DIRECT_ACCEPTED_STATES:
        return _cannot_establish("provider-missing")
    if supplied.state == "authorized_model_only":
        return _assessment("blocked", codes=["provider-model-only"])
    if (
        assessment.provider_unit,
        assessment.provider_state,
        assessment.source_keys,
        assessment.validation,
        assessment.max_phase2_claim,
    ) != (
        reference,
        supplied.state,
        supplied.source_keys,
        supplied.validation,
        supplied.max_phase2_claim,
    ):
        return _cannot_establish("provider-foreign")
    return assessment


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """Refuse last-one-wins JSON without retaining the duplicate key in an exception."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateJsonKey from None
        seen.add(key)
    return dict(pairs)


def _reject_nonfinite(_constant: str) -> float:
    """`json.loads` constant hook: `NaN`/`Infinity` are not JSON and never round-trip."""
    raise _NonFiniteJsonConstant from None


def _require(condition: object, reason: str) -> None:
    """Raise the typed projection refusal when `condition` is falsy."""
    if not condition:
        raise DataAccessProjectionError(reason) from None


def _sorted_unique_strings(value: object, unsorted_reason: str) -> tuple[str, ...]:
    """A JSON list that must be strings, strictly ascending and duplicate-free."""
    _require(isinstance(value, list), "bad-type")
    items = list(value)  # type: ignore[arg-type]
    for item in items:
        _require(isinstance(item, str), "bad-type")
    _require(items == sorted(set(items)), unsorted_reason)
    return tuple(items)


def _check_enum(value: object, allowed: tuple[str, ...], *, nullable: bool = False) -> None:
    """Reject anything outside a closed enum, telling a wrong TYPE from a wrong VALUE."""
    if value is None:
        _require(nullable, "bad-type")
        return
    _require(isinstance(value, str), "bad-type")
    _require(value in allowed, "unknown-value")


def _check_state_combination(payload: dict) -> None:  # pylint: disable=too-many-branches
    """Require each state's keys, codes, scope and claim ceiling to agree, not merely type-check."""
    state = payload["state"]
    keys, codes = payload["source_keys"], set(payload["codes"])
    scope, validation, ceiling = payload["effective_scope"], payload["validation"], payload["max_phase2_claim"]
    provider_unit, provider_state = payload["provider_unit"], payload["provider_state"]

    if state == "provider_inherited":
        _require(isinstance(provider_unit, str) and provider_unit, "illegal-combination")
        _require(provider_state in DIRECT_ACCEPTED_STATES, "illegal-combination")
        _require(codes == {"provider-exact"} and scope in EFFECTIVE_SCOPES, "illegal-combination")
        if provider_state == "authorized_model_only":
            _require(
                keys and validation == "unvalidated" and ceiling == "structural_only" and scope == "model_only",
                "illegal-combination",
            )
        else:
            _require(validation == "validated" and ceiling == "data_validated", "illegal-combination")
            _require(bool(keys) == (provider_state == "live_data_ok"), "illegal-combination")
        return

    _require(provider_unit is None and provider_state is None, "illegal-combination")
    if state == "live_data_ok":
        _require(keys and codes == {"probe-data-ok", "probe-cleared"}, "illegal-combination")
        _require(
            validation == "validated" and ceiling == "data_validated" and scope in DIRECT_SCOPES, "illegal-combination"
        )
    elif state == "local_import_ready":
        _require(not keys and codes == {"all-flat-file", "package-self-contained"}, "illegal-combination")
        _require(
            validation == "validated" and ceiling == "data_validated" and scope in DIRECT_SCOPES, "illegal-combination"
        )
    elif state == "authorized_model_only":
        _require(keys and codes == {"human-authorize", "brief-model-only"}, "illegal-combination")
        _require(
            validation == "unvalidated" and ceiling == "structural_only" and scope == "model_only",
            "illegal-combination",
        )
    elif state == "blocked":
        _require(codes and codes <= BLOCKING_CODES, "illegal-combination")
        _require(validation == "not_established" and ceiling == "none" and scope is None, "illegal-combination")
    else:
        _require(codes and codes <= CANNOT_CODES and not keys, "illegal-combination")
        _require(validation == "not_established" and ceiling == "none" and scope is None, "illegal-combination")


def parse_data_access(text: str) -> DataAccessAssessment:
    """Strictly parse the closed projection; a refusal never falls back to audit or ancestor data."""
    _require(isinstance(text, str), "bad-type")
    rejection = None
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
    except _DuplicateJsonKey:
        rejection = "duplicate-key"
    except _NonFiniteJsonConstant:
        rejection = "nonfinite"
    except (ValueError, RecursionError):
        rejection = "malformed-json"
    if rejection:
        # Outside the except suite: even __context__ must not retain raw JSON/keys.
        raise DataAccessProjectionError(rejection) from None

    _require(isinstance(payload, dict), "not-an-object")
    _require(not set(payload) - set(DATA_ACCESS_FIELDS), "unknown-field")
    _require(not set(DATA_ACCESS_FIELDS) - set(payload), "missing-field")
    _require(payload["schema"] == DATA_ACCESS_SCHEMA, "unknown-value")

    _check_enum(payload["state"], DATA_ACCESS_STATES)
    _check_enum(payload["validation"], VALIDATION_STATES)
    _check_enum(payload["effective_scope"], EFFECTIVE_SCOPES, nullable=True)
    _check_enum(payload["max_phase2_claim"], CLAIM_CEILINGS)
    _check_enum(payload["provider_state"], DIRECT_ACCEPTED_STATES, nullable=True)
    if payload["provider_unit"] is not None:
        _require(isinstance(payload["provider_unit"], str) and payload["provider_unit"], "bad-type")
        _require(_valid_provider_reference(payload["provider_unit"]), "provider-unit-invalid")

    keys = _sorted_unique_strings(payload["source_keys"], "source-keys-unsorted")
    for key in keys:
        _require(SOURCE_KEY_RE.fullmatch(key), "source-key-invalid")
    codes = _sorted_unique_strings(payload["codes"], "codes-unsorted")
    for code in codes:
        _require(code in DATA_ACCESS_CODES, "unknown-value")

    _check_state_combination(payload)
    return DataAccessAssessment(
        state=payload["state"],
        source_keys=keys,
        provider_unit=payload["provider_unit"],
        provider_state=payload["provider_state"],
        validation=payload["validation"],
        effective_scope=payload["effective_scope"],
        max_phase2_claim=payload["max_phase2_claim"],
        codes=codes,
    )


def read_data_access(path: Path) -> DataAccessAssessment:
    """`parse_data_access` over a file. An unreadable projection is a refusal, not an empty state."""
    text = None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    if text is None:
        raise DataAccessProjectionError("unreadable") from None
    return parse_data_access(text)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    lst = sub.add_parser("list", help="report gate state for every unit beneath a root (read-only)")
    lst.add_argument("root", type=Path)
    lst.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    for name in ("status", "block", "clear", "verify", "authorize"):
        p = sub.add_parser(name)
        p.add_argument("migration", type=Path)
        if name == "block":
            p.add_argument("--sources", nargs="*", default=[])
            p.add_argument(
                "--force-scope",
                action="store_true",
                help=(
                    "arm the gate even when the target is not identifiable as a single migration or "
                    "bundle. A marker governs its whole subtree, so this can block unrelated work."
                ),
            )
        if name == "clear":
            p.add_argument("--reason", default="manual")
            p.add_argument("--earned", action="store_true", help=argparse.SUPPRESS)
            p.add_argument("--sources", nargs="*", help="source(s) proven by this earned clear")
        if name == "authorize":
            p.add_argument("--who", required=True, help="who is authorizing this unvalidated build")
    args = parser.parse_args(argv)

    target = (args.root if args.cmd == "list" else args.migration).resolve()
    if not target.is_dir():
        log.error("not a directory: %s", target)
        # `list` gets its own code: its security signal is 3 (forged override), and a mistyped
        # estate root must not raise it. 2 belongs to argparse. Every other subcommand keeps 2.
        return 4 if args.cmd == "list" else 2

    handlers = {
        "list": lambda: list_units(target, as_json=args.as_json),
        "block": lambda: apply_block(target, args.sources, force_scope=args.force_scope),
        "clear": lambda: clear_block(target, args.reason, earned=args.earned, sources=args.sources),
        "authorize": lambda: authorize(target, args.who),
        "verify": lambda: verify(target),
        "status": lambda: status(target),
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
