"""purpose: Explicit, private, exact-run retained popup publication and mechanical cleanup.
usage:   prepare_operator_pause(scratch, model, pid); load_operator_pause(scratch, pause_id, sha256).

This module travels with the skill. It reads the selected run.json directly, never a repository
allocator, environment variable, current directory, newest pause, or source/credential material.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

SCHEMA = "pbip.operator-pause.v1"
_PAUSE_ID = re.compile(r"[0-9a-f]{32}")
_HASH = re.compile(r"[0-9a-f]{64}")
_DECIMAL = re.compile(r"[1-9][0-9]{0,19}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_CHECKS = {"before", "after_render", "after_write"}
_FILES = {"prompt.png", "pause.json", "READY"}
_MAX_IMAGE_BYTES = 20_000_000
_FORBIDDEN = {".git", "packages", "deliverables", "fabric", "pbip", "reports", "semantic_models"}
CleanupResult = Literal["removed", "absent", "cannot_establish", "cleanup_failed"]


class OperatorPauseUnavailable(ValueError):
    """A closed refusal, safe to print without leaking paths, prompts or OS error messages."""

    def __init__(self) -> None:
        super().__init__("OPERATOR_PAUSE_CANNOT_ESTABLISH")


def _require(condition: bool) -> None:
    if not condition:
        raise OperatorPauseUnavailable()


def _keys(value: object, expected: set[str]) -> dict:
    _require(type(value) is dict and set(value) == expected)  # pylint: disable=unidiomatic-typecheck
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    _require(len(data) <= 65_536)
    return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)


def _identity(path: Path, *, directory: bool) -> tuple[int, int]:
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400)
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    return info.st_dev, info.st_ino


def _absolute(path: Path) -> Path:
    path = Path(path)
    text = str(path)
    _require(path.is_absolute() and not text.startswith(("\\\\", "//")))
    _require(".." not in path.parts and not any(":" in part for part in path.parts[1:]))
    _require(not any(part.endswith((" ", ".")) for part in path.parts[1:]))
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes, kernel.GetDriveTypeW.restype = [wintypes.LPCWSTR], wintypes.UINT
        _require(kernel.GetDriveTypeW(path.anchor) in (3, 6))
    for ancestor in (path, *path.parents):
        _identity(ancestor, directory=True)
    _require(path == path.resolve(strict=True))
    return path


def _ignored(path: Path) -> None:
    repo = next((parent for parent in (path, *path.parents) if (parent / ".git").exists()), None)
    if repo is not None:
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--quiet", "--", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        _require(result.returncode == 0)


def _run(run_scratch: Path) -> tuple[Path, int, str]:
    scratch = _absolute(run_scratch)
    _require(scratch.name == "scratch")
    for parent in (scratch, *scratch.parents):
        name = parent.name.casefold()
        _require(name not in _FORBIDDEN and not name.endswith((".report", ".semanticmodel")))
        _require(
            not any(
                (parent / marker).exists()
                for marker in ("definition.pbism", "definition.pbir", "package-manifest.json")
            )
            and not any(parent.glob("*.pbip"))
        )
    root = scratch.parent
    manifest_path = root / "run.json"
    _identity(manifest_path, directory=False)
    with manifest_path.open("rb") as handle:
        manifest = _json(handle.read(65_537))
        _require(
            _identity(manifest_path, directory=False)
            == (os.fstat(handle.fileno()).st_dev, os.fstat(handle.fileno()).st_ino)
        )
    _require(type(manifest) is dict)  # pylint: disable=unidiomatic-typecheck
    number, unit = manifest.get("run"), manifest.get("unit_key")
    _require(type(number) is int and 0 <= number <= 999_999_999)  # pylint: disable=unidiomatic-typecheck
    _require(isinstance(unit, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", unit) is not None)
    _require(manifest.get("allocated_dir_name") == root.name == f"{number:03d}-{unit}")
    recorded = manifest.get("allocated_abs_path")
    _require(isinstance(recorded, str) and _absolute(Path(recorded)) == root)
    _ignored(scratch)
    return scratch, number, root.name


def _model(root: Path, model: Path) -> str:
    model = _absolute(model)
    _require(model != root and model.is_relative_to(root) and not model.is_relative_to(root / "scratch"))
    relative = model.relative_to(root)
    _require(not any(part.casefold() in {"packages", "deliverables"} for part in relative.parts))
    return relative.as_posix()


@dataclass(frozen=True)
class OperatorPauseContext:
    """Immutable selected run/model and process-creation identity; not a prompt classification."""

    # These identities are one binding, not separately mutable path/manifest lookups.
    # pylint: disable=too-many-instance-attributes
    run_scratch: Path
    model_dir: Path
    desktop_pid: int
    process_start: str
    run_number: int
    run_directory: str
    directory_identities: tuple[tuple[int, int], ...]


def process_start_identity(pid: int) -> str:
    """Exact Windows creation FILETIME, not wall-clock inference or a PID-only liveness check."""
    _require(sys.platform == "win32" and type(pid) is int and 0 < pid <= 0xFFFFFFFF)  # pylint: disable=unidiomatic-typecheck
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = (
        [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
        wintypes.HANDLE,
    )
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    _require(bool(handle))
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        _require(
            bool(
                kernel.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
            )
        )
        _require(exit_time.dwHighDateTime == exit_time.dwLowDateTime == 0)
        return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def prepare_operator_pause(run_scratch: Path, model_dir: Path, desktop_pid: int) -> OperatorPauseContext:
    """Validate an explicit existing canonical run before refresh. Creates nothing."""
    try:
        scratch, number, directory = _run(run_scratch)
        _model(scratch.parent, model_dir)
        paths = (scratch.parent, scratch, model_dir)
        return OperatorPauseContext(
            scratch,
            Path(model_dir),
            desktop_pid,
            process_start_identity(desktop_pid),
            number,
            directory,
            tuple(_identity(path, directory=True) for path in paths),
        )
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise OperatorPauseUnavailable() from None


def recheck_operator_pause(context: OperatorPauseContext) -> None:
    """Reject moved/replaced bindings and PID reuse; never refresh the immutable snapshot."""
    _require(type(context) is OperatorPauseContext)  # pylint: disable=unidiomatic-typecheck
    _require(prepare_operator_pause(context.run_scratch, context.model_dir, context.desktop_pid) == context)


class _SecurityAttributes(ctypes.Structure):  # pylint: disable=too-few-public-methods
    """Windows SECURITY_ATTRIBUTES; all created evidence has a protected owner-only DACL."""

    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]


@contextmanager
def _private_attributes() -> Iterator[_SecurityAttributes]:
    _require(sys.platform == "win32")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    try:
        _require(
            bool(
                advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    "D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None
                )
            )
        )
        yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)


def _require_private(path: Path) -> None:
    """Read the OS DACL, never assume that creation or an inherited ACL made it private."""
    _require(sys.platform == "win32")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.c_void_p] * 5
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p,
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    descriptor, text = ctypes.c_void_p(), wintypes.LPWSTR()
    try:
        _require(advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor)) == 0)
        _require(
            bool(
                advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None)
            )
        )
        _require(text.value == "D:P(A;;FA;;;OW)")
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


def _private_directory(path: Path, *, existing: bool = False) -> None:
    with _private_attributes() as attributes:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
        kernel.CreateDirectoryW.restype = wintypes.BOOL
        created = kernel.CreateDirectoryW(str(path), ctypes.byref(attributes))
        _require(bool(created) or (existing and ctypes.get_last_error() == 183))
    _identity(path, directory=True)
    _require_private(path)


def _open_file(
    path: Path, *, create: bool = False, exclusive: bool = False, provisional: bool = False, deleting: bool = False
) -> BinaryIO:
    """CreateNew with protected ACL, or pin an ordinary existing file; never truncate or replace."""
    # pylint: disable=import-outside-toplevel,too-many-arguments
    import msvcrt

    with _private_attributes() as attributes:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        access = (0xC0000000 if create else 0x80000000) | (0x10000 if provisional or deleting else 0)
        flags = 0x00200080 | (0x04000000 if provisional else 0)  # OPEN_REPARSE_POINT; provisional READY only
        handle = kernel.CreateFileW(
            str(path),
            access,
            0 if exclusive else 7 if create else 1,
            ctypes.byref(attributes),
            1 if create else 3,
            flags,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise OSError("private file unavailable")
        try:
            descriptor = msvcrt.open_osfhandle(
                handle, (os.O_RDWR if create else os.O_RDONLY) | os.O_BINARY | os.O_NOINHERIT
            )
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    stream = os.fdopen(descriptor, "r+b" if create else "rb")
    try:
        info = os.fstat(stream.fileno())
        _require(_identity(path, directory=False) == (info.st_dev, info.st_ino))
        _require_private(path)
        return stream
    except BaseException:
        stream.close()
        raise


def _disposition(stream: BinaryIO, flags: int, *, extended: bool = False) -> None:
    # pylint: disable=import-outside-toplevel
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    value = wintypes.DWORD(flags) if extended else ctypes.c_ubyte(flags)
    _require(
        bool(
            kernel.SetFileInformationByHandle(
                msvcrt.get_osfhandle(stream.fileno()), 21 if extended else 4, ctypes.byref(value), ctypes.sizeof(value)
            )
        )
    )


def _rename(source: Path, target: Path) -> None:
    _require(source.parent == target.parent and sys.platform == "win32")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.MoveFileExW.argtypes, kernel.MoveFileExW.restype = (
        [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD],
        wintypes.BOOL,
    )
    _require(bool(kernel.MoveFileExW(str(source), str(target), 0)))  # no replacement or cross-volume copy


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identifier(value: object, pattern: re.Pattern[str]) -> None:
    _require(isinstance(value, str) and pattern.fullmatch(value) is not None)


def validate_pause(record: object) -> dict:
    """Closed pause-only v1 schema. No host paths, free text or future policy fields."""
    # Exact JSON types exclude bool-as-number and string-as-boolean coercions.
    # pylint: disable=unidiomatic-typecheck
    data = _keys(
        record,
        {
            "schema",
            "pause_id",
            "state",
            "phase",
            "reason",
            "captured_at_utc",
            "published_at_utc",
            "run",
            "model_path",
            "desktop",
            "image",
        },
    )
    _require(
        data["schema"] == SCHEMA
        and data["state"] == "waiting"
        and data["phase"] == "pbip_refresh"
        and data["reason"] == "DIALOG_UNREADABLE"
    )
    _identifier(data["pause_id"], _PAUSE_ID)
    for key in ("captured_at_utc", "published_at_utc"):
        _identifier(data[key], _STAMP)
        datetime.fromisoformat(data[key])
    _require(data["captured_at_utc"] <= data["published_at_utc"])
    run = _keys(data["run"], {"number", "directory"})
    _require(type(run["number"]) is int and 0 <= run["number"] <= 999_999_999)
    _require(
        isinstance(run["directory"], str)
        and re.fullmatch(rf"{run['number']:03d}-[a-z0-9]+(?:-[a-z0-9]+)*", run["directory"]) is not None
    )
    model = data["model_path"]
    _require(isinstance(model, str) and bool(model) and "\\" not in model and ":" not in model)
    _require(not model.startswith("/") and all(part not in ("", ".", "..") for part in model.split("/")))
    _require(not any(part.endswith((" ", ".")) for part in model.split("/")))
    desktop = _keys(data["desktop"], {"pid", "process_start", "dialog_hwnd", "owner_hwnd"})
    for value in desktop.values():
        _identifier(value, _DECIMAL)
    _require(int(desktop["pid"]) <= 0xFFFFFFFF and desktop["dialog_hwnd"] != desktop["owner_hwnd"])
    image = _keys(data["image"], {"path", "sha256", "dimensions", "ownership_checks"})
    _require(image["path"] == "prompt.png")
    _identifier(image["sha256"], _HASH)
    dimensions = _keys(image["dimensions"], {"width", "height"})
    _require(all(type(value) is int and value > 0 for value in dimensions.values()))
    _require(dimensions["width"] * dimensions["height"] <= 4_000_000)
    checks = _keys(image["ownership_checks"], _CHECKS)
    _require(all(value is True for value in checks.values()))
    return data


def _bound_record(record: dict, scratch: Path, pause_id: str, expected_hash: str) -> None:
    data = validate_pause(record)
    _, number, directory = _run(scratch)
    _require(data["pause_id"] == pause_id and data["image"]["sha256"] == expected_hash)
    _require(data["run"] == {"number": number, "directory": directory})
    model = scratch.parent.joinpath(*PurePosixPath(data["model_path"]).parts)
    _require(_model(scratch.parent, model) == data["model_path"])


def _checked_image(stream: BinaryIO, record: dict) -> None:
    stream.seek(0)
    image = stream.read(_MAX_IMAGE_BYTES + 1)
    dimensions = record["image"]["dimensions"]
    _require(33 <= len(image) <= _MAX_IMAGE_BYTES and image[:8] == b"\x89PNG\r\n\x1a\n")
    _require(struct.unpack(">II", image[16:24]) == (dimensions["width"], dimensions["height"]))
    _require(hashlib.sha256(image).hexdigest() == record["image"]["sha256"])


@contextmanager
def _read_pause(
    scratch: Path, pause_id: str, expected_hash: str, *, deleting: bool = False
) -> Iterator[tuple[dict, list]]:
    _identifier(pause_id, _PAUSE_ID)
    _identifier(expected_hash, _HASH)
    _run(scratch)
    parent = scratch / "operator-pauses"
    directory = parent / pause_id
    for path in (parent, directory):
        _absolute(path)
        _require_private(path)
    _require({entry.name for entry in directory.iterdir()} == _FILES)
    for name in _FILES:
        _ignored(directory / name)
    with ExitStack() as stack:
        streams = [
            stack.enter_context(_open_file(directory / name, exclusive=deleting, deleting=deleting))
            for name in ("prompt.png", "pause.json", "READY")
        ]
        _require(os.fstat(streams[2].fileno()).st_size == 0)
        record = _json(streams[1].read(65_537))
        _bound_record(record, scratch, pause_id, expected_hash)
        _checked_image(streams[0], record)
        for path in (parent, directory):
            _absolute(path)
            _require_private(path)
        yield record, streams


def load_operator_pause(run_scratch: Path, pause_id: str, expected_hash: str) -> dict:
    """Load only the exact caller-pinned READY pause; remnants and missing evidence are refusals."""
    try:
        with _read_pause(Path(run_scratch), pause_id, expected_hash) as (record, _streams):
            return record
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise OperatorPauseUnavailable() from None


def cleanup_operator_pause(run_scratch: Path, pause_id: str, expected_hash: str) -> CleanupResult:
    """Mechanical exact-directory removal. Absence says nothing about a later policy decision."""
    try:
        _identifier(pause_id, _PAUSE_ID)
        _identifier(expected_hash, _HASH)
        scratch, _, _ = _run(run_scratch)
        directory = scratch / "operator-pauses" / pause_id
        parent = directory.parent
        try:
            _identity(parent, directory=True)
        except FileNotFoundError:
            return "absent"
        _absolute(parent)
        _require_private(parent)
        try:
            _identity(directory, directory=True)
        except FileNotFoundError:
            return "absent"
        load_operator_pause(scratch, pause_id, expected_hash)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "cannot_establish"
    try:
        # Claim ALL exact files before deleting ANY. An ordinary viewer lock leaves the entire pause.
        with _read_pause(scratch, pause_id, expected_hash, deleting=True) as (_record, streams):
            marked = []
            try:
                for stream in streams:
                    _disposition(stream, 1)
                    marked.append(stream)
            except (OSError, ValueError):
                for stream in marked:
                    _disposition(stream, 0)
                raise
        directory.rmdir()
        return "removed"
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "cleanup_failed"


@dataclass
class PausePublication:
    """One producer-owned staging reservation; the child writes directly into its image."""

    context: OperatorPauseContext
    pause_id: str
    directory: Path
    lease: BinaryIO
    image_identity: tuple[int, int]
    record: dict | None = None

    @property
    def image_path(self) -> Path:
        """The single pixel path, before or after the same-parent rename."""
        return self.directory / "prompt.png"

    def _check_files(self, record: dict) -> None:
        _absolute(self.directory)
        _require_private(self.directory)
        _require({entry.name for entry in self.directory.iterdir()} == {"prompt.png", "pause.json"})
        _require(_identity(self.image_path, directory=False) == self.image_identity)
        with _open_file(self.directory / "pause.json") as metadata, _open_file(self.image_path) as image:
            actual = _json(metadata.read(65_537))
            _bound_record(actual, self.context.run_scratch, self.pause_id, record["image"]["sha256"])
            _require(actual == record)
            _checked_image(image, actual)

    def publish(self, acquisition: dict, deadline: float, closed: Callable[[], bool]) -> dict:
        """Flush metadata, revalidate, rename without replacement, commit READY last."""

        def guard() -> None:
            _require(not closed() and time.monotonic() < deadline)

        guard()
        recheck_operator_pause(self.context)
        self.lease.flush()
        os.fsync(self.lease.fileno())
        record = {
            "schema": SCHEMA,
            "pause_id": self.pause_id,
            "state": "waiting",
            "phase": "pbip_refresh",
            "reason": "DIALOG_UNREADABLE",
            "captured_at_utc": acquisition["captured_at_utc"],
            "published_at_utc": _utc(),
            "run": {"number": self.context.run_number, "directory": self.context.run_directory},
            "model_path": self.context.model_dir.relative_to(self.context.run_scratch.parent).as_posix(),
            "desktop": {
                "pid": str(self.context.desktop_pid),
                "process_start": self.context.process_start,
                "dialog_hwnd": acquisition["dialog_hwnd"],
                "owner_hwnd": acquisition["owner_hwnd"],
            },
            "image": {
                "path": "prompt.png",
                "sha256": acquisition["sha256"],
                "dimensions": {key: int(value) for key, value in acquisition["dimensions"].items()},
                "ownership_checks": acquisition["ownership_checks"],
            },
        }
        _bound_record(record, self.context.run_scratch, self.pause_id, acquisition["sha256"])
        _checked_image(self.lease, record)
        with _open_file(self.directory / "pause.json", create=True) as metadata:
            metadata.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            metadata.flush()
            os.fsync(metadata.fileno())
        guard()
        recheck_operator_pause(self.context)
        _require(_identity(self.image_path, directory=False) == self.image_identity)
        _require_private(self.image_path)
        self.lease.close()
        self._check_files(record)
        target = self.directory.parent / self.pause_id
        guard()
        _rename(self.directory, target)
        self.directory = target
        guard()
        recheck_operator_pause(self.context)
        self._check_files(record)
        guard()
        # A slow CreateNew cannot expose a late READY: until committed it is exclusive and
        # kernel-delete-on-close. Only this zero-byte marker has a provisional lease, never pixels.
        with _open_file(target / "READY", create=True, exclusive=True, provisional=True) as ready:
            ready.flush()
            os.fsync(ready.fileno())
            guard()
            _disposition(ready, 8, extended=True)  # clear FILE_DELETE_ON_CLOSE, preserving an ordinary marker
            try:
                guard()
            except OperatorPauseUnavailable:
                _disposition(ready, 1)
                raise
        self.record = record
        return record


def reserve_operator_pause(
    context: OperatorPauseContext, deadline: float, closed: Callable[[], bool]
) -> PausePublication:
    """Lazily reserve a protected unique directory and normal image, under the original deadline."""
    _require(not closed() and time.monotonic() < deadline)
    recheck_operator_pause(context)
    parent = context.run_scratch / "operator-pauses"
    _private_directory(parent, existing=True)
    _absolute(parent)
    pause_id = uuid.uuid4().hex
    _identifier(pause_id, _PAUSE_ID)
    for name in _FILES:
        _ignored(parent / pause_id / name)
    directory = parent / f".pending-{pause_id}-{uuid.uuid4().hex}"
    _require(not closed() and time.monotonic() < deadline)
    _private_directory(directory)
    image = directory / "prompt.png"
    lease = _open_file(image, create=True)
    try:
        _require(not closed() and time.monotonic() < deadline)
        return PausePublication(context, pause_id, directory, lease, _identity(image, directory=False))
    except BaseException:
        lease.close()
        raise
