"""
purpose: Package data-access projection and the per-root coordination transactions it publishes under.

Library only, no CLI. Holds the `phase1-data-access/v1` projection (`assess_data_access`,
`reconcile_package_data_access`, `parse_data_access`, `package_spec_facts`, `provider_reference`)
consumed by `package_unit.py`, `check_unit.py`, `check_reference_readiness.py` and
`package_role_identity.py`, plus the descriptor-locked root transactions (`gate_transaction(s)`,
`_coordinated`, `_durable_write`). Moved verbatim out of `credential_gate.py` so the retired gate CLI
can be deleted whole; this module must never import `credential_gate`. The on-disk names keep their
historical `credential-gate` spelling (`.credential-gate-tx-v1-<sha>.lock`, the canonical transaction
prefix) so existing companions and locks stay the same objects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import NamedTuple

from bundle_corpus import is_reparse_entry
from package_filesystem import is_canonical_key

# Imported as a plain NAME, not reached through the module (`preflight_source_credentials._classify_legs`
# is `protected-access` to pylint, W0212). This is the SAME canonical classifier
# (`connection_target.powerbi_target`) that arms the gate in the first place; issue #354's review
# explicitly required reusing it here rather than a second, independently-maintained opinion.
from preflight_source_credentials import _classify_legs

# The historical logger name: transaction refusals keep routing exactly as before the move.
log = logging.getLogger("credential_gate")

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
        self.initialized = False

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
        sizes = (1,) if self.initialized else (0, 1)
        if _identity(info) != _identity(opened) or info.st_size not in sizes or opened.st_size not in sizes:
            raise GateTransactionError("transaction-companion-changed")

    def __enter__(self) -> GateTransaction:
        path = self.authority.companion
        try:
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            except FileExistsError:
                _safe_entry(path)
                descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            self.descriptor = descriptor
            # A peer may open the companion before its creator writes the byte (#801): initialize it
            # only under the lock, so no transaction can observe it half-made. Identity is still checked first.
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
            if os.fstat(descriptor).st_size == 0:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            self.initialized = True
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


MIGRATION_SPEC = "migration-spec.json"


# Phase-1 authority is read-only: verify() appends audit rows and cannot produce this projection.

DATA_ACCESS_SCHEMA = "phase1-data-access/v1"

DATA_ACCESS_STATES = (
    "local_import_ready",
    "live_data_ok",
    "live_unproven",
    "authorized_model_only",
    "provider_inherited",
    "blocked",
    "cannot_establish",
)
# `live_data_ok` and the human-authorize pairing are legacy: still parsed, no longer produced.
DIRECT_ACCEPTED_STATES = ("local_import_ready", "live_data_ok", "live_unproven", "authorized_model_only")
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
        "live-unproven",
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


def _assess_direct(
    live_keys: tuple[str, ...],
    has_review: bool,
    package_data_sources: object,
    policy: tuple[str, str],
) -> DataAccessAssessment:
    """Assess direct access from package facts and the brief alone; no audit, ledger or marker is read.

    A live source is admitted as `live_unproven`: dispatchable, but never `validated`. Real-data proof
    is owed at sign-off by check_unit's `data-evidence` row, not earned here.
    """
    fallback_authorization, requested_scope = policy
    local_complete, refusal = _package_local_facts(package_data_sources)
    if refusal:
        return _cannot_establish(refusal)
    findings = ["unknown-target"] if has_review else []
    if not local_complete:
        findings.append("local-import-incomplete")
    if findings:
        return _assessment("blocked", codes=findings, source_keys=live_keys)
    if not live_keys:
        return _assessment(
            "local_import_ready",
            codes=["all-flat-file", "package-self-contained"],
            validation="validated",
            effective_scope=requested_scope,
            max_phase2_claim="data_validated",
        )
    if fallback_authorization == "model_only_unvalidated" and requested_scope == "model_only":
        return _assessment(
            "authorized_model_only",
            codes=["brief-model-only"],
            source_keys=live_keys,
            validation="unvalidated",
            effective_scope="model_only",
            max_phase2_claim="structural_only",
        )
    return _assessment(
        "live_unproven",
        codes=["live-unproven"],
        source_keys=live_keys,
        validation="unvalidated",
        effective_scope=requested_scope,
        max_phase2_claim="data_validated",
    )


def assess_data_access(  # pylint: disable=too-many-arguments
    gate_root: Path,
    *,
    package_spec: Mapping,
    package_data_sources: Mapping,
    fallback_authorization: str,
    requested_scope: str,
    provider: tuple[str, DataAccessAssessment] | list | None = None,
) -> DataAccessAssessment:
    """Read-only authority over supplied package facts, the brief policy or one S2 provider.

    Invalid policy/scope raises ValueError; malformed evidence returns a typed refusal.
    No probe, mutation, audit/ledger read or ancestor search. `gate_root` is retained for caller
    compatibility only. Provider pairs carry provider_reference(), not raw names.
    """
    del gate_root
    if fallback_authorization not in FALLBACK_POLICIES:
        raise ValueError(f"fallback_authorization must be one of {FALLBACK_POLICIES}")
    if requested_scope not in EFFECTIVE_SCOPES:
        raise ValueError(f"requested_scope must be one of {EFFECTIVE_SCOPES}")

    if provider is not None or requested_scope == "report_only_shared_model":
        return _inherit_from_provider(provider, requested_scope)
    live_keys, has_review, spec_refusal = _package_spec_facts(package_spec)
    if spec_refusal:
        return _cannot_establish(spec_refusal)
    return _assess_direct(live_keys, has_review, package_data_sources, (fallback_authorization, requested_scope))


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
        elif provider_state == "live_unproven":
            _require(keys and validation == "unvalidated" and ceiling == "data_validated", "illegal-combination")
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
    elif state == "live_unproven":
        _require(keys and codes == {"live-unproven"}, "illegal-combination")
        _require(
            validation == "unvalidated" and ceiling == "data_validated" and scope in DIRECT_SCOPES,
            "illegal-combination",
        )
    elif state == "local_import_ready":
        _require(not keys and codes == {"all-flat-file", "package-self-contained"}, "illegal-combination")
        _require(
            validation == "validated" and ceiling == "data_validated" and scope in DIRECT_SCOPES, "illegal-combination"
        )
    elif state == "authorized_model_only":
        _require(
            keys and codes in ({"brief-model-only"}, {"human-authorize", "brief-model-only"}), "illegal-combination"
        )
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
