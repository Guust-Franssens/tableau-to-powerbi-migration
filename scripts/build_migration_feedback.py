"""
purpose: Validate recorded migration feedback and build a private, offline evidence bundle.
usage:   python -B scripts/build_migration_feedback.py --input <request.json>
         [--run <absolute-run>] [--out <absolute-dir>]
internal: true
internal-reason: implementation helper for the migration-feedback skill, not a diagnostics or readiness exporter.

The request is authored by the session, not extracted from source text. Evidence is pinned by
size, digest and filesystem identity, held through assembly, and never executed.
Observations are not signed attestations:
this validates their consistency, not the honesty of their producer or historical filesystem state.
Only issue-payload.json is a public-safe projection. All other outputs remain private.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import stat
import sys
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lxml import etree

import engine_source
from harvest_estate_assets import OutputPathNotIgnoredError, unignored_output_paths
from object_identity import REVISION_ALGO_ARCHIVE, REVISION_ALGO_XML, RevisionKey, revision_key
from work_dirs import check_run_location

LOG = logging.getLogger("migration-feedback")
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "migration-feedback/1"
OUTPUT_FILES = ("feedback.json", "evidence-index.json", "reproduction.md", "issue-payload.json")
REQUEST_FIELDS = {
    "schema_version",
    "flow",
    "source_mode",
    "claim_scope",
    "owner",
    "engine_involved",
    "contrast",
    "reproducer",
    "evidence",
    "private_notes",
}
EVIDENCE_ROLES = {
    "predicate",
    "owner",
    "oracle",
    "positive_input",
    "positive_output",
    "positive_record",
    "negative_input",
    "negative_output",
    "negative_record",
    "candidate_input",
    "candidate_output",
    "candidate_record",
    "candidate_negative_input",
    "candidate_negative_output",
    "candidate_negative_record",
    "engine_receipt",
    "input_manifest",
    "engine_report",
    "fresh_output",
    "baseline_output",
    "source_provenance",
    "migration_spec",
    "run_status",
    "package_manifest",
    "gate_results",
    "parse_sweep",
    "engine_gap_report",
    "external_evidence",
    "external_confirmation",
    "runtime",
    "wrapper",
    "baseline_owner",
    "baseline_record",
    "baseline_witness",
    "baseline_wrapper_record",
}
PREFIXES = ("positive", "negative", "candidate", "candidate_negative")
EVIDENCE_ROLES |= {
    f"{prefix}_{part}"
    for prefix in PREFIXES
    for part in ("witness", "oracle_record", "oracle_result", "wrapper_record")
}
CONTEXT_ROLES = {"migration_spec", "run_status", "package_manifest", "gate_results", "parse_sweep", "engine_gap_report"}
RECORD_FIELDS = {
    "schema_version",
    "input_sha256",
    "output_sha256",
    "owner_sha256",
    "oracle_sha256",
    "predicate_sha256",
    "command",
    "cwd",
    "started_at",
    "finished_at",
    "exit_code",
    "setup",
    "runtime_sha256",
    "input_binding",
    "witness_sha256",
    "oracle_record_sha256",
}
PROCESS_FIELDS = {
    "schema_version",
    "command",
    "cwd",
    "started_at",
    "finished_at",
    "exit_code",
    "setup",
    "runtime_sha256",
    "input_sha256",
    "output_sha256",
    "input_binding",
}
ORACLE_RECORD_FIELDS = PROCESS_FIELDS | {"oracle_sha256", "predicate_sha256"}
WITNESS_FIELDS = {"schema_version", "command", "cwd", "owner_sha256", "input_sha256", "output_sha256"}
ORACLE_RESULT_FIELDS = {
    "schema_version",
    "command",
    "cwd",
    "oracle_sha256",
    "input_sha256",
    "predicate_sha256",
    "expected",
}
FAILURE_CLASSES = {"incorrect_output", "missing_output", "unexpected_refusal", "runtime_failure", "external_block"}
ROUTES = {
    "engine": "ENGINE_UPSTREAM",
    "repository": "AGENTIC_REPOSITORY",
    "external": "EXTERNAL_OR_CONFIGURATION",
}
REPOSITORIES = {
    "ENGINE_UPSTREAM": "Yarbrdab000/tableau-fabric-skills",
    "AGENTIC_REPOSITORY": "Guust-Franssens/tableau-to-powerbi-migration",
}
REPRO_SUFFIXES = {".twb", ".tds", ".json", ".csv", ".txt"}
REPRODUCER_FIELDS = {"authorship", "redistributable", "reviewed_sha256"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")
LUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class FeedbackError(Exception):
    """An explicit refusal (1), usage error (2), or missing proof (3)."""

    def __init__(self, reason: str, exit_code: int = 3) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


def _require(condition: bool, reason: str, exit_code: int = 3) -> None:
    if not condition:
        raise FeedbackError(reason, exit_code)


def _choice(value: Any, choices: set[str], reason: str) -> None:
    _require(isinstance(value, str) and value in choices, reason, 1)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _object(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    _require(isinstance(value, dict), f"{label}:object_required", 1)
    _require(not set(value) - allowed, f"{label}:unknown_field", 1)
    _require(required <= set(value), f"{label}:missing_field")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, "json:duplicate_key", 1)
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise FeedbackError("json:nonfinite_number", 1)


def _float(value: str) -> float:
    number = float(value)
    _require(math.isfinite(number), "json:nonfinite_number", 1)
    return number


def _json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_nonfinite, parse_float=_float
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FeedbackError("json:invalid_document", 1) from exc


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _timestamp(value: Any, label: str) -> datetime:
    _require(isinstance(value, str), f"{label}:timestamp_missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackError(f"{label}:timestamp_invalid") from exc
    _require(result.tzinfo is not None, f"{label}:timezone_missing")
    return result


def _path(value: Any, base: Path | None = None) -> Path:
    _require(isinstance(value, str) and bool(value), "path:missing", 1)
    _require(not value.startswith(("\\\\", "//")), "path:network_or_device", 1)
    path = Path(value)
    _require(".." not in path.parts and "~" not in path.parts, "path:unsafe_component", 1)
    _require(all(ord(char) >= 32 for char in value), "path:control_character", 1)
    _require(not any(":" in part for part in path.parts[1:]), "path:alternate_stream", 1)
    if not path.is_absolute() and base is not None:
        path = base / path
    _require(path.is_absolute(), "path:absolute_required", 2)
    for entry in [*reversed(path.parents), path]:
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        _require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "path:link_or_reparse",
            1,
        )
        _require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), "path:not_regular", 1)
    return path


def _identity(info: os.stat_result) -> tuple[int, int]:
    _require(bool(info.st_ino), "filesystem:identity_unavailable", 1)
    return info.st_dev, info.st_ino


def _regular(info: os.stat_result, *, directory: bool = False) -> None:
    _require(
        not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
        "filesystem:link_or_reparse",
        1,
    )
    _require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "filesystem:not_regular",
        1,
    )
    if not directory:
        _require(info.st_nlink == 1, "filesystem:hardlink", 1)


def _file_state(info: os.stat_result) -> tuple[int, ...]:
    _regular(info)
    # Windows path stat and handle stat can expose creation vs change time as st_ctime.
    change_time = info.st_ctime_ns if os.name != "nt" else 0
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, change_time)


def _windows_api() -> Any:
    import ctypes  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _windows_open(path: Path, *, directory: bool, create: bool, movable: bool) -> int:
    import ctypes  # pylint: disable=import-outside-toplevel
    import msvcrt  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    kernel = _windows_api()
    access = (0x10000 if movable else 0) if directory else (0xC0000000 if create else 0x80000000)
    # OPEN_REPARSE_POINT, no write/delete sharing: ancestor and input swaps fail at the OS boundary.
    handle = kernel.CreateFileW(str(path), access, 1, None, 1 if create else 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, (os.O_RDWR if create else os.O_RDONLY) | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(handle)
        raise


def _windows_move(descriptor: int, destination: Path | None) -> None:
    import ctypes  # pylint: disable=import-outside-toplevel
    import msvcrt  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    if destination is None:
        information = wintypes.BOOLEAN(True)
        information_class = 4  # FileDispositionInfo: remove the empty, held stage on close.
    else:
        name = str(destination)

        class RenameInfo(ctypes.Structure):  # pylint: disable=too-few-public-methods
            """FILE_RENAME_INFO, with a complete variable-length UTF-16 target."""

            _fields_ = [
                ("replace", wintypes.BOOLEAN),
                ("root", wintypes.HANDLE),
                ("length", wintypes.DWORD),
                ("name", ctypes.c_wchar * (len(name) + 1)),
            ]

        information = RenameInfo(False, None, len(name.encode("utf-16-le")), name)
        information_class = 3
    if not _windows_api().SetFileInformationByHandle(
        msvcrt.get_osfhandle(descriptor), information_class, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_permissions(path: Path, *, writable: bool) -> None:
    import ctypes  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    security.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel = _windows_api()
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    # The owner can explicitly change the ACL for disposal; ordinary writes/renames are sealed.
    rights = "FA" if writable else "FRFXWD"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;;{rights};;;OW)", 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not security.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        error = security.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


def _stage_permissions(filesystem: _Filesystem, stage: Path, files: list[Path], *, writable: bool) -> None:
    for path in (stage, stage / "repro", *files):
        directory = path in {stage, stage / "repro"}
        if directory:
            descriptor = filesystem.directory(path)
        else:
            filesystem.read(path)
            descriptor = filesystem.files[path].descriptor
        info = os.fstat(descriptor)
        _regular(info, directory=directory)
        if os.name == "nt":
            _windows_permissions(path, writable=writable)
        else:
            os.fchmod(descriptor, (0o700 if writable else 0o500) if directory else (0o600 if writable else 0o400))


def _posix_publish(parent_fd: int, stage: str, destination: str) -> None:
    import ctypes  # pylint: disable=import-outside-toplevel

    # rename() replaces an existing empty directory on POSIX. That is not this contract.
    library = ctypes.CDLL(None, use_errno=True)
    _require(sys.platform.startswith("linux") and hasattr(library, "renameat2"), "output:atomic_publish_unavailable", 1)
    rename = library.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(stage), parent_fd, os.fsencode(destination), 1):
        raise OSError(ctypes.get_errno(), "exclusive directory publication failed")


@dataclass
class _HeldFile:
    path: Path
    descriptor: int
    state: tuple[int, ...]
    raw: bytes


class _Filesystem:
    """No-follow, identity-bound handles, including every ancestor used by a read or write."""

    def __init__(self) -> None:
        self.stack = ExitStack()
        self.directories: dict[Path, tuple[int, tuple[int, int]]] = {}
        self.files: dict[Path, _HeldFile] = {}
        self.descriptors: set[int] = set()

    def __enter__(self) -> _Filesystem:
        return self

    def __exit__(self, *args: Any) -> None:
        self.stack.__exit__(*args)

    def _keep(self, descriptor: int) -> None:
        self.descriptors.add(descriptor)
        self.stack.callback(self._close, descriptor)

    def _close(self, descriptor: int) -> None:
        if descriptor in self.descriptors:
            self.descriptors.remove(descriptor)
            os.close(descriptor)

    def release_below(self, root: Path) -> None:
        """Close descendants before a Windows directory rename, retaining its locked ancestors."""
        for path in list(self.files):
            if path.is_relative_to(root):
                self._close(self.files.pop(path).descriptor)
        for path in sorted(self.directories, key=lambda item: len(item.parts), reverse=True):
            if path != root and path.is_relative_to(root):
                self._close(self.directories.pop(path)[0])

    def directory(self, path: Path, *, create: bool = False, movable: bool = False) -> int:
        """Hold an ancestor before looking up a child; never follow a replacement entry."""
        if path in self.directories:
            self.verify_directory(path)
            return self.directories[path][0]
        parent = None if path.parent == path else self.directory(path.parent, create=create)
        try:
            before = path.lstat()
        except FileNotFoundError:
            if not create:
                raise
            if os.name == "nt":
                os.mkdir(path, 0o700)
            else:
                os.mkdir(path.name, 0o700, dir_fd=parent)
            before = path.lstat()
        _regular(before, directory=True)
        if os.name == "nt":
            descriptor = _windows_open(path, directory=True, create=False, movable=movable)
        else:
            descriptor = os.open(
                path if parent is None else path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,  # pylint: disable=no-member
                dir_fd=parent,
            )
        self._keep(descriptor)
        held = os.fstat(descriptor)
        _regular(held, directory=True)
        _require(_identity(before) == _identity(held), "filesystem:directory_changed", 1)
        self.directories[path] = descriptor, _identity(held)
        self.verify_directory(path)
        return descriptor

    def verify_directory(self, path: Path) -> None:
        """Check that a held directory still occupies the chosen, non-reparse spelling."""
        descriptor, identity = self.directories[path]
        current = path.lstat()
        _regular(current, directory=True)
        _regular(os.fstat(descriptor), directory=True)
        _require(_identity(current) == identity == _identity(os.fstat(descriptor)), "filesystem:directory_changed", 1)

    def read(self, path: Path, *, create: bytes | None = None) -> bytes:
        """Keep one file handle and verify identity, link count and bytes before and after use."""
        parent = self.directory(path.parent)
        if path in self.files:
            self.verify_file(self.files[path])
            return self.files[path].raw
        before = None if create is not None else _file_state(path.lstat())
        if os.name == "nt":
            descriptor = _windows_open(path, directory=False, create=create is not None, movable=False)
        else:
            flags = os.O_RDONLY if create is None else os.O_RDWR | os.O_CREAT | os.O_EXCL
            descriptor = os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)  # pylint: disable=no-member
        self._keep(descriptor)
        opened = _file_state(os.fstat(descriptor))
        _require(before is None or opened == before, "filesystem:file_changed", 1)
        if create is not None:
            # Track even a partial write so failure cleanup owns exactly the files it created.
            self.files[path] = _HeldFile(path, descriptor, opened, create)
            with os.fdopen(os.dup(descriptor), "wb") as stream:
                stream.write(create)
                stream.flush()
                os.fsync(stream.fileno())
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            raw = stream.read()
        state = _file_state(os.fstat(descriptor))
        _require(before is None or state == before, "filesystem:file_changed", 1)
        _require(create is None or raw == create, "filesystem:written_bytes_changed", 1)
        held = _HeldFile(path, descriptor, state, raw)
        self.files[path] = held
        self.verify_file(held)
        return raw

    def verify_file(self, held: _HeldFile) -> None:
        """Re-read through the held descriptor, not a path that may now name another inode."""
        self.verify_directory(held.path.parent)
        _require(
            _file_state(held.path.lstat()) == held.state == _file_state(os.fstat(held.descriptor)),
            "filesystem:file_changed",
            1,
        )
        os.lseek(held.descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(held.descriptor), "rb") as stream:
            _require(stream.read() == held.raw, "filesystem:bytes_changed", 1)
        _require(
            _file_state(os.fstat(held.descriptor)) == held.state == _file_state(held.path.lstat()),
            "filesystem:file_changed",
            1,
        )

    def verify(self) -> None:
        """Validate the entire held set before any private bytes are staged."""
        for path in self.directories:
            self.verify_directory(path)
        for held in self.files.values():
            self.verify_file(held)


@dataclass(frozen=True)
class Evidence:
    """One immutable byte snapshot; paths and original digests stay private."""

    role: str
    path: Path
    raw: bytes

    @property
    def sha256(self) -> str:
        """Digest of the bytes actually inspected, never a caller-supplied claim."""
        return _sha(self.raw)

    def document(self) -> Any:
        """Decode structured evidence without treating any value as instructions."""
        return _json(self.raw)

    def index_entry(self) -> dict:
        """Private source locator, without copying the original."""
        return {"role": self.role, "path": str(self.path), "size_bytes": len(self.raw), "sha256": self.sha256}


@dataclass
class Assessment:
    """Private result and the independently projected public facts."""

    route: str = "CANNOT_ESTABLISH"
    reasons: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    controls: dict = field(default_factory=dict)
    repro_files: dict[str, bytes] = field(default_factory=dict)
    exit_code: int = 3

    @property
    def reproducer_status(self) -> str:
        """A consistent status derived from the result, not a second writable verdict."""
        if self.exit_code == 0 and self.route == "EXTERNAL_OR_CONFIGURATION":
            return "not_applicable"
        return "established" if self.exit_code == 0 and self.repro_files else "reproducer_not_established"


def _read_evidence(request: dict, base: Path, filesystem: _Filesystem) -> dict[str, Evidence]:
    declarations = _object(request.get("evidence"), EVIDENCE_ROLES, set(), "evidence")
    evidence = {}
    planned = {}
    identities = {_identity(os.fstat(item.descriptor)) for item in filesystem.files.values()}
    for role, declaration in declarations.items():
        _object(declaration, {"path", "size_bytes", "sha256"}, {"path", "size_bytes", "sha256"}, role)
        _require(_integer(declaration["size_bytes"]) and declaration["size_bytes"] >= 0, f"{role}:size_invalid", 1)
        _require(
            isinstance(declaration["sha256"], str) and HEX64.fullmatch(declaration["sha256"]) is not None,
            f"{role}:digest_invalid",
            1,
        )
        path = _path(declaration["path"], base)
        try:
            state = _file_state(path.lstat())
        except OSError as exc:
            raise FeedbackError(f"{role}:unreadable") from exc
        _require(state[:2] not in identities, "evidence:aliased_roles", 1)
        identities.add(state[:2])
        planned[role] = path, state
    for role, (path, state) in planned.items():
        _require(_file_state(path.lstat()) == state, f"{role}:identity_changed", 1)
        raw = filesystem.read(path)
        _require(filesystem.files[path].state == state, f"{role}:identity_changed", 1)
        declaration = declarations[role]
        _require(
            len(raw) == declaration["size_bytes"] and _sha(raw) == declaration["sha256"], f"{role}:changed_bytes", 1
        )
        evidence[role] = Evidence(role, path, raw)
    return evidence


def _get(evidence: dict[str, Evidence], role: str) -> Evidence:
    _require(role in evidence, f"missing_evidence:{role}")
    return evidence[role]


def _predicate(evidence: dict[str, Evidence]) -> dict:
    predicate = _get(evidence, "predicate").document()
    _object(
        predicate,
        {"schema_version", "defined_at", "kind", "pointer", "expected", "failure_class"},
        {"schema_version", "defined_at", "kind", "expected", "failure_class"},
        "predicate",
    )
    _require(
        _integer(predicate["schema_version"]) and predicate["schema_version"] == 1,
        "predicate:unsupported_schema",
        1,
    )
    _choice(predicate["kind"], {"json_equals", "json_missing", "text_contains"}, "predicate:unsupported_kind")
    _choice(predicate["failure_class"], FAILURE_CLASSES, "predicate:unknown_failure_class")
    _timestamp(predicate["defined_at"], "predicate")
    if predicate["kind"] == "text_contains":
        _require(
            isinstance(predicate["expected"], str) and bool(predicate["expected"].strip()), "predicate:empty_signature"
        )
    else:
        _require(
            isinstance(predicate.get("pointer"), str) and predicate["pointer"].startswith("/"),
            "predicate:json_pointer_required",
        )
        if predicate["kind"] == "json_missing":
            _require(predicate["expected"] is True, "predicate:presence_expected")
    return predicate


def _failed(predicate: dict, output: Evidence) -> bool:
    if predicate["kind"] == "text_contains":
        try:
            return predicate["expected"] in output.raw.decode("utf-8")
        except UnicodeError as exc:
            raise FeedbackError("predicate:output_not_text") from exc
    value = output.document()
    found = True
    for token in predicate["pointer"].split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdecimal() and int(token) < len(value):
            value = value[int(token)]
        else:
            found = False
            break
    if predicate["kind"] == "json_missing":
        return not found
    _require(found, "predicate:assertion_not_reached")
    # JSON booleans must not compare equal to numeric 0/1.
    return json.dumps(value, sort_keys=True) != json.dumps(predicate["expected"], sort_keys=True)


def _schema(document: dict, version: int, label: str) -> None:
    _require(
        _integer(document.get("schema_version")) and document["schema_version"] == version,
        f"{label}:unsupported_schema",
        1,
    )


def _python_file(code: Evidence) -> None:
    _require(code.path.suffix.lower() == ".py", "invocation:python_file_required")
    try:
        tree = ast.parse(code.raw)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        raise FeedbackError("invocation:entrypoint_not_python") from exc
    _require(bool(tree.body), "invocation:empty_entrypoint")


def _invocation(record: dict, evidence: dict[str, Evidence], prefix: str, code_role: str) -> tuple:
    command = record["command"]
    _require(
        isinstance(command, list) and len(command) >= 4 and all(isinstance(arg, str) and arg for arg in command),
        f"{prefix}:command_not_recorded",
    )
    input_role = "positive_input" if prefix == "baseline" else f"{prefix}_input"
    runtime, code, source = (_get(evidence, role) for role in ("runtime", code_role, input_role))
    cwd = _path(record["cwd"])
    _require(record["runtime_sha256"] == runtime.sha256, f"{prefix}:runtime_mismatch")
    _require(_path(command[0], cwd) == runtime.path and command[1] == "-B", f"{prefix}:runtime_not_invoked")
    _require(_path(command[2], cwd) == code.path, f"{prefix}:entrypoint_not_invoked")
    _python_file(code)
    binding = _object(
        record["input_binding"], {"role", "kind", "argument_index"}, {"role", "kind", "argument_index"}, "input_binding"
    )
    _require(_integer(binding["argument_index"]), f"{prefix}:argument_index_invalid", 1)
    _require(binding["role"] == input_role, f"{prefix}:input_role_mismatch")
    if code_role == "oracle":
        _require(
            binding["kind"] == "file" and binding["argument_index"] == 3 and len(command) == 5,
            f"{prefix}:oracle_invocation_unsupported",
        )
        _require(_path(command[4], cwd) == _get(evidence, "predicate").path, f"{prefix}:predicate_not_invoked")
    elif binding["kind"] == "file":
        _require(binding["argument_index"] == 3 and len(command) == 4, f"{prefix}:file_invocation_unsupported")
    else:
        _require(
            binding["kind"] == "directory"
            and binding["argument_index"] == 4
            and len(command) == 7
            and command[3] == "--input"
            and command[5] == "--output",
            f"{prefix}:directory_invocation_unsupported",
        )
        destination = _path(command[6], cwd)
        _require(
            _get(evidence, f"{prefix}_output").path.is_relative_to(destination), f"{prefix}:output_not_in_invocation"
        )
    index = binding["argument_index"]
    expected_path = source.path.parent if binding["kind"] == "directory" else source.path
    _require(_path(command[index], cwd) == expected_path, f"{prefix}:input_not_in_invocation")
    return runtime.sha256, code.sha256, binding["kind"]


def _process(record: dict, predicate: dict, prefix: str) -> None:
    _require(record["setup"] == "ready", f"{prefix}:setup_not_ready")
    _require(_integer(record["exit_code"]), f"{prefix}:exit_not_recorded", 1)
    start = _timestamp(record["started_at"], prefix)
    end = _timestamp(record["finished_at"], prefix)
    _require(_timestamp(predicate["defined_at"], "predicate") <= start <= end, f"{prefix}:predicate_not_defined_first")


def _oracle_observation(evidence: dict[str, Evidence], prefix: str, predicate: dict, record: dict) -> None:
    oracle_record = _get(evidence, f"{prefix}_oracle_record")
    _require(record["oracle_record_sha256"] == oracle_record.sha256, f"{prefix}:oracle_record_mismatch")
    invocation = oracle_record.document()
    _object(invocation, ORACLE_RECORD_FIELDS, ORACLE_RECORD_FIELDS, f"{prefix}_oracle_record")
    _schema(invocation, 1, "oracle_record")
    _process(invocation, predicate, prefix)
    _require(invocation["exit_code"] == 0, f"{prefix}:oracle_did_not_complete")
    _invocation(invocation, evidence, prefix, "oracle")
    result_file = _get(evidence, f"{prefix}_oracle_result")
    result = _object(result_file.document(), ORACLE_RESULT_FIELDS, ORACLE_RESULT_FIELDS, "oracle_result")
    _schema(result, 1, "oracle_result")
    for field_name, role in (
        ("input_sha256", f"{prefix}_input"),
        ("oracle_sha256", "oracle"),
        ("predicate_sha256", "predicate"),
    ):
        _require(
            invocation[field_name] == result[field_name] == _get(evidence, role).sha256,
            f"{prefix}:oracle_{field_name}_mismatch",
        )
    _require(invocation["output_sha256"] == result_file.sha256, f"{prefix}:oracle_result_mismatch")
    _require(
        result["command"] == invocation["command"] and result["cwd"] == invocation["cwd"],
        f"{prefix}:oracle_witness_invocation_mismatch",
    )
    _require(_bytes(result["expected"]) == _bytes(predicate["expected"]), f"{prefix}:oracle_expectation_mismatch")


def _witness(evidence: dict[str, Evidence], prefix: str, record: dict) -> None:
    witness_file = _get(evidence, f"{prefix}_witness")
    _require(record["witness_sha256"] == witness_file.sha256, f"{prefix}:witness_mismatch")
    witness = _object(witness_file.document(), WITNESS_FIELDS, WITNESS_FIELDS, "witness")
    _schema(witness, 1, "witness")
    for name in WITNESS_FIELDS - {"schema_version"}:
        _require(witness[name] == record[name], f"{prefix}:witness_{name}_mismatch")


def _record(evidence: dict[str, Evidence], prefix: str, predicate: dict) -> dict:
    record = _get(evidence, f"{prefix}_record").document()
    _object(record, RECORD_FIELDS, RECORD_FIELDS, f"{prefix}_record")
    _schema(record, 2, prefix)
    _process(record, predicate, prefix)
    _invocation(record, evidence, prefix, "owner")
    for field_name, role in (
        ("input_sha256", f"{prefix}_input"),
        ("output_sha256", f"{prefix}_output"),
        ("owner_sha256", "owner"),
        ("oracle_sha256", "oracle"),
        ("predicate_sha256", "predicate"),
    ):
        _require(record[field_name] == _get(evidence, role).sha256, f"{prefix}:{field_name}_mismatch")
    _witness(evidence, prefix, record)
    _oracle_observation(evidence, prefix, predicate, record)
    if f"{prefix}_wrapper_record" in evidence:
        try:
            _wrapper_record(evidence, prefix, engine_source.engine_root())
        except engine_source.EngineNotFoundError as exc:
            raise FeedbackError("engine:canonical_plugin_unavailable") from exc
    return record


def _controls(evidence: dict[str, Evidence], predicate: dict, positive: str, negative: str) -> dict:
    records = [_record(evidence, prefix, predicate) for prefix in (positive, negative)]
    _require(
        _invocation(records[0], evidence, positive, "owner") == _invocation(records[1], evidence, negative, "owner"),
        "controls:different_invocation",
    )
    _require(
        _get(evidence, f"{positive}_input").sha256 != _get(evidence, f"{negative}_input").sha256,
        "controls:no_input_contrast",
    )
    _require(_failed(predicate, _get(evidence, f"{positive}_output")), f"{positive}:predicate_not_reproduced")
    _require(not _failed(predicate, _get(evidence, f"{negative}_output")), f"{negative}:negative_control_failed")
    owner, oracle = _get(evidence, "owner"), _get(evidence, "oracle")
    _require(owner.raw != oracle.raw, "controls:oracle_not_independent")
    _require(
        oracle.path
        not in {
            _get(evidence, f"{prefix}_{part}").path for prefix in (positive, negative) for part in ("input", "output")
        },
        "controls:oracle_is_test_data",
    )
    return {
        "positive": "same_predicate_failed",
        "negative": "same_predicate_passed",
        "oracle": "invocation_result_and_expectation_bound",
    }


def _origin(source: Evidence, key: RevisionKey, provenance: Evidence | None) -> dict:
    if provenance is None:
        return {"status": "not_provided"}
    document = provenance.document()
    _require(
        isinstance(document, dict) and document.get("schema") == "tableau-source-provenance/1",
        "provenance:unsupported_schema",
    )
    rows = document.get("inputs")
    _require(isinstance(rows, list), "provenance:inputs_missing")
    _require(
        _integer(document.get("input_count")) and document["input_count"] == len(rows),
        "provenance:input_count_invalid",
        1,
    )
    phase = document.get("phase")
    if not rows and isinstance(phase, dict) and phase.get("status") == "failed":
        return {"status": "origin_unavailable"}
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("input"), dict) and row["input"].get("sha256") == source.sha256
    ]
    _require(len(matches) == 1, "provenance:input_identity_ambiguous_or_missing")
    local = matches[0]["input"]
    _require(_integer(local.get("size_bytes")), "provenance:input_size_invalid", 1)
    _require(local["size_bytes"] == len(source.raw), "provenance:input_size_mismatch")
    recorded_key = RevisionKey.from_json(local.get("revision_key"))
    _require(recorded_key is not None and key.agrees_with(recorded_key) is True, "provenance:revision_key_mismatch")
    origin = matches[0].get("origin")
    if not isinstance(origin, dict):
        return {"status": "origin_unavailable"}
    fields = {
        "server",
        "site",
        "workbook_luid",
        "datasource_luid",
        "project",
        "updated_at",
        "tableau_product_version",
        "rest_api_version",
        "remote_revision_key",
        "revision_match",
    }
    result = {name: origin[name] for name in sorted(fields) if name in origin}
    agreement = key.agrees_with(RevisionKey.from_json(origin.get("remote_revision_key")))
    result["status"] = (
        "confirmed" if agreement is True and origin.get("revision_match") == "same" else "origin_unavailable"
    )
    return result


def _source(request: dict, evidence: dict[str, Evidence]) -> dict:
    source = _get(evidence, "positive_input")
    result = {"mode": request["source_mode"], "engine_input": source.index_entry()}
    if request["flow"] == "script":
        _require(request["source_mode"] == "not_applicable", "source:script_mode_conflict")
        _require(request["claim_scope"] == "local_artifact", "source:script_remote_claim")
        result["origin"] = {"status": "not_provided"}
        return result
    _require(request["source_mode"] in {"local_download", "remote_capture"}, "source:mode_missing")
    suffixes = {".twb", ".twbx"} if request["flow"] == "workbook" else {".tds", ".tdsx"}
    _require(source.path.suffix.lower() in suffixes, "source:kind_mismatch")
    key = revision_key(source.raw)
    _require(
        key is not None and key.algo in {REVISION_ALGO_ARCHIVE, REVISION_ALGO_XML}, "source:revision_key_unavailable"
    )
    result["revision_key"] = key.as_json()
    result["origin"] = _origin(source, key, evidence.get("source_provenance"))
    spec = _get(evidence, "migration_spec").document()
    _require(
        isinstance(spec, dict)
        and isinstance(spec.get("source"), dict)
        and spec["source"].get("file_name") == source.path.name,
        "migration_spec:source_mismatch",
    )
    return result


def _remote_claim(request: dict, source: dict, evidence: dict[str, Evidence]) -> None:
    if request["claim_scope"] == "remote_state":
        origin = source["origin"]
        luid = origin.get(f"{request['flow']}_luid")
        provenance = evidence.get("source_provenance")
        phase = provenance.document().get("phase") if provenance is not None else None
        _require(
            origin["status"] == "confirmed"
            and isinstance(luid, str)
            and LUID.fullmatch(luid) is not None
            and all(
                isinstance(origin.get(key), str) and bool(origin[key].strip())
                for key in ("site", "server", "tableau_product_version", "rest_api_version")
            )
            and isinstance(phase, dict)
            and phase.get("status") == "success",
            "source:remote_revision_unconfirmed",
        )


def _manifest_input(evidence: dict[str, Evidence], flow: str) -> None:
    source = _get(evidence, "positive_input")
    manifest = _get(evidence, "input_manifest").document()
    _require(isinstance(manifest, dict) and isinstance(manifest.get("assets"), list), "engine:manifest_inputs_missing")
    matches = [
        row
        for row in manifest["assets"]
        if isinstance(row, dict) and row.get("staged_input_path") == str(source.path) and row.get("kind") == flow
    ]
    _require(len(matches) == 1, "engine:consumed_identity_ambiguous_or_missing")
    _require(_integer(matches[0].get("size_bytes")), "engine:consumed_size_invalid", 1)
    _require(
        matches[0].get("sha256") == source.sha256 and matches[0].get("size_bytes") == len(source.raw),
        "engine:consumed_bytes_mismatch",
    )


def _wrapper_record(evidence: dict[str, Evidence], prefix: str, root: Path) -> dict | None:
    wrapper = evidence.get(f"{prefix}_wrapper_record")
    if wrapper is None:
        return None
    fields = PROCESS_FIELDS | {"wrapper_sha256", "child_record_sha256"}
    record = _object(wrapper.document(), fields, fields, "wrapper_record")
    _schema(record, 1, "wrapper_record")
    code = _get(evidence, "wrapper")
    _require(code.path == REPO_ROOT / "scripts" / "run_estate.py", "engine:wrapper_not_canonical")
    child = _get(evidence, f"{prefix}_record")
    _require(record["child_record_sha256"] == child.sha256, "engine:child_record_mismatch")
    _require(record["wrapper_sha256"] == code.sha256, "engine:wrapper_code_mismatch")
    input_role = "positive_input" if prefix == "baseline" else f"{prefix}_input"
    owner_role = "baseline_owner" if prefix == "baseline" else "owner"
    _require(
        record["input_sha256"] == _get(evidence, input_role).sha256
        and record["output_sha256"] == _get(evidence, f"{prefix}_output").sha256,
        "engine:wrapper_bytes_mismatch",
    )
    _invocation(record, evidence, prefix, "wrapper")
    _process(record, _get(evidence, "predicate").document(), prefix)
    child_record = child.document()
    _require(
        _get(evidence, owner_role).path == engine_source.engine_scripts_dir(root) / "migrate_estate.py"
        and _timestamp(record["started_at"], "wrapper")
        <= _timestamp(child_record["started_at"], "child")
        <= _timestamp(child_record["finished_at"], "child")
        <= _timestamp(record["finished_at"], "wrapper"),
        "engine:child_invocation_unestablished",
    )
    _require(record["command"][3:] == child_record["command"][3:], "engine:wrapper_child_arguments_differ")
    return record


def _baseline_record(evidence: dict[str, Evidence]) -> dict:
    fields = PROCESS_FIELDS | {"owner_sha256", "witness_sha256"}
    record = _object(_get(evidence, "baseline_record").document(), fields, fields, "baseline_record")
    _schema(record, 1, "baseline_record")
    _process(record, _get(evidence, "predicate").document(), "baseline")
    _invocation(record, evidence, "baseline", "baseline_owner")
    for name, role in (
        ("input_sha256", "positive_input"),
        ("output_sha256", "baseline_output"),
        ("owner_sha256", "baseline_owner"),
    ):
        _require(record[name] == _get(evidence, role).sha256, f"baseline:{name}_mismatch")
    _witness(evidence, "baseline", record)
    return record


def _fresh_output(evidence: dict[str, Evidence], root: Path, version: str, *, baseline: bool = False) -> None:
    receipt = _get(evidence, "engine_receipt")
    proof = _get(evidence, "fresh_output").document()
    fields = {
        "output_dir",
        "observed_absent_at",
        "started_at",
        "finished_at",
        "before_state",
        "scope",
        "receipt_sha256",
        "input_sha256",
        "engine_root",
        "engine_version",
        "command",
        "exit_code",
    }
    _object(proof, fields | {"cwd"}, fields, "fresh_output")
    _require(proof["before_state"] == "absent" and proof["scope"] == "full", "engine:not_a_fresh_full_output")
    _require(_path(proof["output_dir"]) == receipt.path.parent, "engine:fresh_output_path_mismatch")
    _require(
        proof["receipt_sha256"] == receipt.sha256 and proof["input_sha256"] == _get(evidence, "positive_input").sha256,
        "engine:fresh_output_identity_mismatch",
    )
    _require(_path(proof["engine_root"]) == root and proof["engine_version"] == version, "engine:fresh_engine_mismatch")
    _require(
        _timestamp(proof["observed_absent_at"], "fresh_output")
        <= _timestamp(proof["started_at"], "fresh_output")
        <= _timestamp(proof["finished_at"], "fresh_output"),
        "engine:fresh_output_time_conflict",
    )
    prefix = "baseline" if baseline else "positive"
    record = _baseline_record(evidence) if baseline else _get(evidence, "positive_record").document()
    record = _wrapper_record(evidence, prefix, root) or record
    _require(_integer(proof["exit_code"]), "engine:exit_missing", 1)
    _require(
        proof["command"] == record["command"] and proof["exit_code"] == record["exit_code"],
        "engine:fresh_invocation_mismatch",
    )
    cwd = _path(proof["cwd"]) if "cwd" in proof else _path(record["cwd"])
    _require(cwd == _path(record["cwd"]), "engine:fresh_cwd_mismatch")
    _require(
        proof["started_at"] == record["started_at"]
        and _timestamp(record["finished_at"], prefix) <= _timestamp(proof["finished_at"], "fresh_output"),
        "engine:fresh_record_time_mismatch",
    )
    _require(record["input_binding"]["kind"] == "directory", "engine:directory_invocation_required")
    destination = _path(record["command"][6], cwd)
    _require(destination == receipt.path.parent, "engine:command_output_mismatch")
    _require(
        _get(evidence, "baseline_owner" if baseline else "owner").path
        == engine_source.engine_scripts_dir(root) / "migrate_estate.py",
        "engine:canonical_entrypoint_missing",
    )


def _engine(request: dict, evidence: dict[str, Evidence]) -> dict:
    receipt_file = _get(evidence, "engine_receipt")
    receipt = receipt_file.document()
    _require(isinstance(receipt, dict), "engine:receipt_invalid", 1)
    _require(_integer(receipt.get("version")) and receipt["version"] == 1, "engine:receipt_invalid", 1)
    recorded = receipt.get("engine")
    _require(isinstance(recorded, dict), "engine:receipt_identity_missing")
    try:
        root = engine_source.engine_root()
    except engine_source.EngineNotFoundError as exc:
        raise FeedbackError("engine:canonical_plugin_unavailable") from exc
    version = engine_source.engine_version(root)
    _require(isinstance(version, str) and VERSION.fullmatch(version) is not None, "engine:version_unavailable")
    _require(
        recorded.get("canonical") is True
        and recorded.get("source") == "plugin"
        and _path(recorded.get("root")) == root
        and _path(recorded.get("plugin_root")) == root
        and recorded.get("version") == version,
        "engine:canonical_receipt_mismatch",
    )
    for name, role in (("report_sha256", "engine_report"), ("input_manifest_sha256", "input_manifest")):
        _require(receipt.get(name) == _get(evidence, role).sha256, f"engine:{role}_mismatch")
        _require(_get(evidence, role).path.parent == receipt_file.path.parent, f"engine:{role}_location_mismatch")
    _manifest_input(evidence, request["flow"])
    _fresh_output(evidence, root, version, baseline=request["owner"] != "engine")
    output = _get(evidence, "positive_output" if request["owner"] == "engine" else "baseline_output")
    _require(output.path.is_relative_to(receipt_file.path.parent), "engine:baseline_outside_fresh_output")
    relative = output.path.relative_to(receipt_file.path.parent).as_posix()
    artifacts = receipt.get("artifacts")
    _require(isinstance(artifacts, list), "engine:artifact_receipt_missing")
    matches = [row for row in artifacts if isinstance(row, dict) and row.get("path") == relative]
    _require(
        all(isinstance(row, dict) and _integer(row.get("size")) and row["size"] >= 0 for row in artifacts),
        "engine:artifact_size_invalid",
        1,
    )
    _require(
        len(matches) == 1 and matches[0].get("sha256") == output.sha256 and matches[0].get("size") == len(output.raw),
        "engine:baseline_not_receipt_backed",
    )
    return {"root": str(root), "version": version, "canonical": True, "fresh_output": str(receipt_file.path.parent)}


def _external(evidence: dict[str, Evidence]) -> None:
    evidence_file = _get(evidence, "external_evidence")
    finding = evidence_file.document()
    fields = {"system", "condition", "record_sha256", "confirmation_sha256"}
    _object(finding, fields, fields, "external")
    _choice(
        finding["system"], {"tableau", "powerbi", "credentials", "network", "environment"}, "external:system_unknown"
    )
    _choice(
        finding["condition"],
        {"credential_modal", "permission_denied", "service_unavailable", "configuration_mismatch"},
        "external:condition_unknown",
    )
    confirmation = _get(evidence, "external_confirmation")
    _require(finding["record_sha256"] == _get(evidence, "positive_record").sha256, "external:observation_mismatch")
    _require(finding["confirmation_sha256"] == confirmation.sha256, "external:confirmation_mismatch")
    observed = confirmation.document()
    fields = {"system", "condition", "input_sha256", "observed", "observed_at"}
    _object(observed, fields, fields, "external_confirmation")
    _require(
        observed["system"] == finding["system"]
        and observed["condition"] == finding["condition"]
        and observed["observed"] is True
        and observed["input_sha256"] == _get(evidence, "positive_input").sha256,
        "external:positive_confirmation_missing",
    )
    _timestamp(observed["observed_at"], "external_confirmation")
    _require(
        confirmation.sha256 != _get(evidence, "positive_output").sha256,
        "external:confirmation_not_independent",
    )


def _route(request: dict, evidence: dict[str, Evidence], predicate: dict, result: Assessment) -> None:
    owner = _get(evidence, "owner")
    _require(bool(owner.raw.strip()), "owner:empty_code_evidence")
    layer = request["owner"]
    if layer == "external":
        _external(evidence)
    elif layer == "engine":
        _require(request["engine_involved"] is True, "owner:engine_involvement_missing")
        _require(owner.path.is_relative_to(Path(result.engine["root"])), "owner:outside_canonical_plugin")
    elif layer == "repository":
        _require(
            any(owner.path.is_relative_to(REPO_ROOT / part) for part in ("scripts", ".github", "docs")),
            "owner:outside_agentic_repository",
        )
        if request["engine_involved"]:
            _require(not _failed(predicate, _get(evidence, "baseline_output")), "owner:baseline_also_fails")
    else:
        raise FeedbackError("owner:cannot_establish_code_owner")
    result.route = ROUTES[layer]


def _reproducer(request: dict, evidence: dict[str, Evidence], predicate: dict) -> dict[str, bytes]:
    _require("reproducer" in request, "reproducer:not_provided")
    declaration = request.get("reproducer")
    _require(
        isinstance(declaration, dict) and set(declaration) == REPRODUCER_FIELDS, "reproducer:declaration_invalid", 1
    )
    _require(
        declaration["authorship"] == "fictitious_from_scratch" and declaration["redistributable"] is True,
        "reproducer:privacy_not_established",
        1,
    )
    reviewed = declaration["reviewed_sha256"]
    _require(
        isinstance(reviewed, dict) and set(reviewed) == {"candidate_input", "candidate_negative_input"},
        "reproducer:review_invalid",
        1,
    )
    original_hashes = {
        item.sha256 for role, item in evidence.items() if role not in {"candidate_input", "candidate_negative_input"}
    }
    files = {}
    for role, name in (("candidate_input", "positive"), ("candidate_negative_input", "negative")):
        candidate = _get(evidence, role)
        _require(reviewed[role] == candidate.sha256, "reproducer:reviewed_bytes_mismatch", 1)
        _require(candidate.sha256 not in original_hashes, "reproducer:original_reused", 1)
        _reproducer_format(candidate)
        if candidate.path.suffix.lower() in {".twb", ".tds"}:
            candidate_key = revision_key(candidate.raw)
            originals = [revision_key(_get(evidence, f"{prefix}_input").raw) for prefix in ("positive", "negative")]
            _require(
                candidate_key is not None
                and candidate_key.algo == REVISION_ALGO_XML
                and not any(candidate_key.agrees_with(key) is True for key in originals),
                "reproducer:original_revision_reused",
                1,
            )
        files[f"{name}{candidate.path.suffix.lower()}"] = candidate.raw
    original_end = max(
        _timestamp(_get(evidence, f"{prefix}_record").document()["finished_at"], prefix)
        for prefix in ("positive", "negative")
    )
    for prefix in ("candidate", "candidate_negative"):
        record = _get(evidence, f"{prefix}_record").document()
        _require(isinstance(record, dict), f"{prefix}:record_missing")
        _require(_timestamp(record.get("started_at"), prefix) >= original_end, "reproducer:original_controls_not_first")
    _controls(evidence, predicate, "candidate", "candidate_negative")
    _require(
        _invocation(_get(evidence, "candidate_record").document(), evidence, "candidate", "owner")
        == _invocation(_get(evidence, "positive_record").document(), evidence, "positive", "owner"),
        "reproducer:different_invocation",
    )
    return files


def _reproducer_format(candidate: Evidence) -> None:
    suffix = candidate.path.suffix.lower()
    _require(suffix in REPRO_SUFFIXES, "reproducer:unsupported_asset_type")
    try:
        if suffix in {".twb", ".tds"}:
            root = etree.fromstring(
                candidate.raw, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, recover=False)
            )
            _require(
                root.tag == ("workbook" if suffix == ".twb" else "datasource")
                and not root.getroottree().docinfo.doctype,
                "reproducer:tableau_xml_kind_invalid",
            )
        elif suffix == ".json":
            _json(candidate.raw)
        else:
            text = candidate.raw.decode("utf-8-sig")
            _require(
                bool(text.strip())
                and not any((ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) <= 159 for char in text),
                "reproducer:text_controls_or_empty",
            )
            if suffix == ".csv":
                rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
                _require(
                    len(rows) >= 2
                    and len(rows[0]) >= 2
                    and all(len(row) == len(rows[0]) for row in rows)
                    and all(cell.strip() for cell in rows[0])
                    and len(set(rows[0])) == len(rows[0]),
                    "reproducer:csv_structure_invalid",
                )
    except (ValueError, UnicodeError, etree.XMLSyntaxError, csv.Error, FeedbackError) as exc:
        raise FeedbackError("reproducer:format_not_established") from exc


def assess(request: dict, evidence: dict[str, Evidence]) -> Assessment:
    """Decide from supplied observations; a candidate failure never erases valid private attribution."""
    result = Assessment()
    try:
        result.source = _source(request, evidence)
        _remote_claim(request, result.source, evidence)
        predicate = _predicate(evidence)
        result.controls = _controls(evidence, predicate, "positive", "negative")
        result.controls["failure_class"] = predicate["failure_class"]
        if request["engine_involved"]:
            result.engine = _engine(request, evidence)
        _route(request, evidence, predicate, result)
        if result.route != "EXTERNAL_OR_CONFIGURATION":
            result.repro_files = _reproducer(request, evidence, predicate)
        result.exit_code = 0
    except FeedbackError as exc:
        if exc.exit_code != 3:
            raise
        result.reasons.append(exc.reason)
    return result


def _request(raw: bytes) -> dict:
    request = _object(_json(raw), REQUEST_FIELDS, REQUEST_FIELDS - {"private_notes", "reproducer"}, "request")
    _require(_integer(request["schema_version"]) and request["schema_version"] == 1, "request:unsupported_schema", 1)
    for name, choices in (
        ("flow", {"workbook", "datasource", "script"}),
        ("source_mode", {"local_download", "remote_capture", "not_applicable"}),
        ("claim_scope", {"local_artifact", "remote_state"}),
        ("owner", {"engine", "repository", "external", "unknown"}),
        ("contrast", {"feature_removed", "corrected_input", "known_good_case", "configuration_changed"}),
    ):
        _require(isinstance(request[name], str) and request[name] in choices, f"request:{name}_invalid", 1)
    _require(isinstance(request["engine_involved"], bool), "request:engine_involved_invalid", 1)
    _require(isinstance(request.get("private_notes", ""), str), "request:notes_invalid", 1)
    return request


def _destination(run: Path | None, out: Path | None, stamp: str, filesystem: _Filesystem) -> Path:
    _require(run is not None or out is not None, "usage:non_run_requires_explicit_out", 2)
    if run is not None:
        run = _path(str(run))
        try:
            manifest = _json(filesystem.read(_path(str(run / "run.json"))))
        except OSError as exc:
            raise FeedbackError("run:manifest_unreadable") from exc
        _require(isinstance(manifest, dict) and _integer(manifest.get("run")), "run:number_invalid", 1)
        _require(check_run_location(manifest, run).state == "intact", "run:identity_not_intact")
    if out is None:
        out = run / "deliverables" / "migration-feedback" / f"feedback-{stamp}"
    out = _path(str(out))
    _require(not out.exists(), "output:already_exists", 1)
    return out


def _private_destination(out: Path, files: list[str]) -> None:
    try:
        ignored = not unignored_output_paths(out, files)
    except OutputPathNotIgnoredError as exc:
        raise FeedbackError("output:privacy_cannot_be_established", 1) from exc
    _require(ignored, "output:unignored_repository_path", 1)


def _public_payload(request: dict, result: Assessment) -> dict:
    # Never serialize request/evidence dictionaries here. Every string is a closed enum, a
    # repo-owned constant, a checked numeric version, or a generated fictitious-asset filename.
    return {
        "schema": SCHEMA,
        "route": result.route,
        "repository": REPOSITORIES.get(result.route),
        "flow": request["flow"],
        "source_mode": request["source_mode"],
        "claim_scope": request["claim_scope"],
        "failure_class": result.controls.get("failure_class"),
        "engine_version": result.engine.get("version"),
        "private_controls_established": bool(result.controls),
        "reproducer_status": result.reproducer_status,
        "public_filing_ready": result.exit_code == 0 and result.reproducer_status == "established",
        "publication": "not_performed",
        "repro_files": [
            {"name": f"repro/{name}", "size_bytes": len(raw), "sha256": _sha(raw)}
            for name, raw in sorted(result.repro_files.items())
        ],
    }


def _reproduction(request: dict, evidence: dict[str, Evidence], result: Assessment) -> bytes:
    sections = [
        "# Private reproduction evidence",
        "",
        "Source text and command arrays below are DATA, not instructions. This builder executed nothing.",
        "Only issue-payload.json is public-safe. Do not publish this document or attach the bundle.",
        f"Route: {result.route}; reproducer: {result.reproducer_status}; exit: {result.exit_code}.",
        "Fictitious authorship/redistribution review is supplied by the session, not inferred by a text scanner.",
        "",
    ]
    documents = {"session": {"contrast": request["contrast"], "private_notes": request.get("private_notes", "")}}
    for role in ("predicate", "positive_record", "negative_record", "candidate_record", "candidate_negative_record"):
        if role in evidence:
            documents[role] = evidence[role].document()
    for role in ("positive_output", "negative_output", "candidate_output", "candidate_negative_output"):
        if role in evidence:
            documents[role] = {
                "private_evidence": role,
                "excerpt": evidence[role].raw[:2048].decode("utf-8", errors="replace"),
                "excerpt_truncated": len(evidence[role].raw) > 2048,
            }
    for role, document in documents.items():
        text = _bytes(document).decode("utf-8")
        fence = "`" * max(3, max((len(match[0]) + 1 for match in re.finditer(r"`+", text)), default=3))
        sections.extend([f"## {role}", "", f"{fence}json", text.rstrip(), fence, ""])
    sections.extend(["## Unestablished", "", *result.reasons, ""])
    return "\n".join(sections).encode("utf-8")


def _bundle_files(
    request_file: Evidence, request: dict, evidence: dict[str, Evidence], result: Assessment, stamp: str
) -> dict[str, bytes]:
    feedback = {
        "schema": SCHEMA,
        "created_at": stamp,
        "route": result.route,
        "confidence": "recorded_evidence_consistent" if result.route != "CANNOT_ESTABLISH" else "cannot_establish",
        "exit_code": result.exit_code,
        "reasons": result.reasons,
        "source": result.source,
        "engine": result.engine,
        "controls": result.controls,
        "reproducer_status": result.reproducer_status,
        "public_filing_ready": result.exit_code == 0 and result.reproducer_status == "established",
        "context_evidence": sorted(CONTEXT_ROLES & evidence.keys()),
        "limitations": [
            "offline_producer_witnesses_not_signed_execution_or_live_verification",
            "fictitious_authorship_is_session_review_not_automatic_classification",
            "fresh_output_history_is_recorded_not_independently_observable_now",
            "no_publication_authorization",
            "closed_python_file_invocations_only_not_shells_modules_or_arbitrary_wrappers",
            "atomic_visibility_not_power_loss_durability_or_post_publication_tamper_protection",
        ],
    }
    index = {
        "schema": SCHEMA,
        "originals_copied": False,
        "files": [
            request_file.index_entry(),
            *(evidence[role].index_entry() for role in sorted(evidence)),
        ],
    }
    return {
        "feedback.json": _bytes(feedback),
        "evidence-index.json": _bytes(index),
        "reproduction.md": _reproduction(request, evidence, result),
        **{f"repro/{name}": raw for name, raw in result.repro_files.items()},
        "issue-payload.json": _bytes(_public_payload(request, result)),
    }


def _discard_stage(
    filesystem: _Filesystem,
    stage: Path,
    created: dict[Path, tuple[int, ...]],
    repro_identity: tuple[int, int] | None,
) -> None:
    repro = stage / "repro"
    filesystem.release_below(stage)
    for path, identity in created.items():
        parent = filesystem.directory(path.parent)
        _require(_identity(path.lstat()) == identity, "output:cleanup_identity_changed", 1)
        if os.name == "nt":
            os.unlink(path)
        else:
            os.unlink(path.name, dir_fd=parent)
    if repro_identity is not None:
        _require(_identity(repro.lstat()) == repro_identity, "output:cleanup_identity_changed", 1)
        filesystem.release_below(stage)
        if os.name == "nt":
            os.rmdir(repro)
        else:
            os.rmdir("repro", dir_fd=filesystem.directory(stage))
    if os.name == "nt":
        _windows_move(filesystem.directory(stage), None)
    else:
        os.rmdir(stage.name, dir_fd=filesystem.directory(stage.parent))


def _write_outputs(destination: Path, outputs: dict[str, bytes]) -> None:
    with _Filesystem() as filesystem:
        parent = filesystem.directory(destination.parent, create=True)
        stage = destination.with_name(f".migration-feedback-{uuid.uuid4().hex}.staging")
        _private_destination(stage, list(outputs))
        if os.name == "nt":
            os.mkdir(stage, 0o700)
        else:
            os.mkdir(stage.name, 0o700, dir_fd=parent)
        filesystem.directory(stage, movable=True)
        published = False
        created = {}
        repro_identity = None
        sealed = False
        try:
            filesystem.directory(stage / "repro", create=True)
            repro_identity = filesystem.directories[stage / "repro"][1]
            for name, raw in outputs.items():
                filesystem.read(stage / name, create=raw)
            filesystem.verify()
            sealed = True
            _stage_permissions(filesystem, stage, list(filesystem.files), writable=False)
            for held in filesystem.files.values():
                held.state = _file_state(os.fstat(held.descriptor))
            filesystem.verify()
            if os.name != "nt":
                os.fsync(filesystem.directory(stage / "repro"))
                os.fsync(filesystem.directory(stage))
                os.fsync(parent)
            # Windows will not rename a directory with non-delete-shared children open.
            # Keep the stage and ALL ancestors locked across their close and the atomic rename.
            created = {path: held.state[:2] for path, held in filesystem.files.items()}
            filesystem.release_below(stage)
            filesystem.verify()
            if os.name == "nt":
                _windows_move(filesystem.directory(stage), destination)
            else:
                _posix_publish(parent, stage.name, destination.name)
            published = True
        finally:
            if not published:
                created.update({path: held.state[:2] for path, held in filesystem.files.items()})
                if sealed:
                    _stage_permissions(filesystem, stage, list(created), writable=True)
                _discard_stage(filesystem, stage, created, repro_identity)


def build(input_path: Path, *, run: Path | None = None, out: Path | None = None) -> tuple[Path, Assessment]:
    """Build without rerunning, copying originals, or publishing; output creation is exclusive."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with _Filesystem() as filesystem:
        destination = _destination(run, out, stamp, filesystem)
        ancestor = destination.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        filesystem.directory(ancestor)
        input_path = _path(str(input_path), Path.cwd())
        request_file = Evidence("request", input_path, filesystem.read(input_path))
        request = _request(request_file.raw)
        evidence = _read_evidence(request, input_path.parent, filesystem)
        for role in CONTEXT_ROLES & evidence.keys():
            _require(isinstance(evidence[role].document(), dict), f"{role}:object_required", 1)
        if run is not None and "run_status" in evidence:
            _require(evidence["run_status"].document().get("selected_run") == str(run), "run_status:run_mismatch")
        result = assess(request, evidence)
        outputs = _bundle_files(request_file, request, evidence, result, stamp)
        _private_destination(destination, list(outputs))
        filesystem.verify()
        _write_outputs(destination, outputs)
    return destination, result


def main(argv: list[str] | None = None) -> int:
    """CLI exit contract: 0 established, 1 refusal, 2 usage, 3 incomplete."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Session-authored, hash-pinned request JSON.")
    parser.add_argument(
        "--run", type=Path, help="Explicit absolute existing run; default output stays under deliverables."
    )
    parser.add_argument("--out", type=Path, help="Explicit absolute NEW private directory; required without --run.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        destination, result = build(args.input, run=args.run, out=args.out)
    except FeedbackError as exc:
        LOG.error("FEEDBACK exit=%d reason=%s", exc.exit_code, exc.reason)
        return exc.exit_code
    except (OSError, ValueError, RecursionError) as exc:
        LOG.error("FEEDBACK exit=1 reason=filesystem_or_encoding_failure type=%s", type(exc).__name__)
        return 1
    LOG.info(
        "FEEDBACK route=%s exit=%d reason=%s output=%s",
        result.route,
        result.exit_code,
        ",".join(result.reasons) or "established",
        destination,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
