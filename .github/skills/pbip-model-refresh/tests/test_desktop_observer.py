"""Offline tests for the Desktop credential-modal fast path and polling loop."""

from __future__ import annotations

# These tests deliberately monkeypatch module-internal seams so no Desktop, network, or Fabric
# dependency is required.
# pylint: disable=protected-access,too-few-public-methods,invalid-name

import threading
import time
import ctypes
import hashlib
import json
import re
import os
import shutil
import struct
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from ctypes import wintypes
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
import _desktop_observer
import probe_desktop_query
import refresh_pbip_model
from _desktop_observer import (
    Observation,
    CredentialModal,
    CredentialUnknownError,
    DESKTOP_MAIN_CLASS_PREFIX,
    DesktopGoneError,
    DesktopUnreadyError,
    DialogFinding,
    DialogFoundError,
    DesktopWindow,
    _enumerate_pid_windows_with_count,
    classify_dialog,
    inspect_desktop_observer,
    join_with_credential_poll,
)
from refresh_pbip_model import CredentialMissingError, refresh


# Keep one function per GUI case and the real -ReadyFile argument at Popen for the AST marker census.


class _NativeCall:
    """Injectable native function with ctypes' assignable argtypes/restype."""

    def __init__(self, function) -> None:
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class _ImageWin32:
    """Only the OS boundary is fake: production does identity, DIB, PNG and callback/lifecycle work."""

    def __init__(self) -> None:
        self.pid = 111
        self.owner_pid = 111
        self.owner = MAIN_HWND
        self.visible = True
        self.owner_enabled = False
        self.thread = 7
        self.extent = (2, 2)
        self.virtual_extent = None
        self.origin = (0, 0)
        self.physical_bounds = (0, 0, 2, 2)
        self.dwm_result = 0
        self.dpi_context = -1
        self.dpi_calls = []
        self.SetThreadDpiAwarenessContext = _NativeCall(self.set_dpi_context)
        self.dwm = SimpleNamespace(DwmGetWindowAttribute=_NativeCall(self.get_physical_bounds))
        self.frame = (2, 2, b"\x00\x00\x00\x00" * 2 + b"\xff\xff\xff\x00" * 2)
        self.render_mode = "paint"
        self.after_render = lambda: None
        self.after_write = lambda: None
        self.buffer = None
        self.dib_size = None
        self.captures = []
        self.writes = []
        self.cleanup_ok = True
        self.gdi = SimpleNamespace(
            CreateCompatibleDC=_NativeCall(lambda _dc: 31),
            CreateDIBSection=_NativeCall(self.create_dib),
            SelectObject=_NativeCall(lambda *_args: 33),
            DeleteObject=_NativeCall(lambda _handle: self.cleanup_ok),
            DeleteDC=_NativeCall(lambda _handle: self.cleanup_ok),
            GdiFlush=_NativeCall(lambda: True),
        )

    def GetWindowThreadProcessId(self, hwnd, pointer):
        pointer._obj.value = self.owner_pid if hwnd == MAIN_HWND else self.pid
        return self.thread

    def GetWindow(self, _hwnd, flag):
        assert flag == _desktop_observer.GW_OWNER
        return self.owner

    def GetForegroundWindow(self):
        return 0x33333  # a sibling: the capture path must never consult it

    def IsWindowVisible(self, _hwnd):
        return self.visible

    def IsWindowEnabled(self, _hwnd):
        return self.owner_enabled

    def set_dpi_context(self, context):
        self.dpi_calls.append(context)
        previous, self.dpi_context = self.dpi_context, context
        return previous

    def get_physical_bounds(self, _hwnd, attribute, pointer, size):
        assert attribute == 9 and size == ctypes.sizeof(wintypes.RECT)
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.physical_bounds
        return self.dwm_result

    def GetWindowRect(self, _hwnd, pointer):
        rect = pointer._obj
        width, height = self.virtual_extent if self.virtual_extent and self.dpi_context != -3 else self.extent
        rect.left, rect.top = self.origin
        rect.right, rect.bottom = rect.left + width, rect.top + height
        return True

    def create_dib(self, _dc, header, _colors, pointer, _section, _offset):
        _size, width, height = struct.unpack_from("<Iii", header.raw)
        assert width > 0 and height < 0, "production must request a top-down DIB"
        self.dib_size = (width, -height)
        self.buffer = ctypes.create_string_buffer(width * -height * 4)
        pointer._obj.value = ctypes.addressof(self.buffer)
        return 32

    def PrintWindow(self, hwnd, dc, flags):
        self.captures.append((hwnd, dc, flags))
        if self.render_mode == "false":
            return False
        if self.render_mode == "paint":
            # The fixture frame is independent of the requested DIB; a small DIB really crops it.
            width, height = self.dib_size
            frame_width, frame_height, pixels = self.frame
            count = min(width, frame_width) * 4
            for row in range(min(height, frame_height)):
                start = row * frame_width * 4
                ctypes.memmove(ctypes.addressof(self.buffer) + row * width * 4, pixels[start : start + count], count)
        elif self.render_mode == "blank":
            self.buffer.raw = b"\xff\xff\xff\x00" * (self.dib_size[0] * self.dib_size[1])
        elif self.render_mode == "partial":
            ctypes.memmove(self.buffer, b"\xff\xff\xff\x00", 4)
        self.after_render()
        return True

    def write(self, path: Path, data: bytes, **_kwargs) -> None:
        self.writes.append(path)
        path.write_bytes(data)
        self.after_write()


@pytest.fixture(name="visual_runtime")
def visual_runtime(monkeypatch, tmp_path):
    """Exercise the production child body without a real window; record flushed in-flight notices."""
    api = _ImageWin32()
    noticed = threading.Event()
    records = []
    wire_records = []
    children = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_desktop_observer.IMAGE_DIRECTORY_ENV, str(tmp_path))
    monkeypatch.setattr(_desktop_observer, "_evidence_directory", lambda directory, _name: directory)
    monkeypatch.setattr(_desktop_observer.sys, "platform", "win32")
    monkeypatch.setattr(_desktop_observer, "_image_user32", lambda: api)
    monkeypatch.setattr(
        _desktop_observer.ctypes,
        "WinDLL",
        lambda name, **_k: {"gdi32": api.gdi, "dwmapi": api.dwm}[name],
        raising=False,
    )
    monkeypatch.setattr(_desktop_observer, "_write_private_image", api.write)
    monkeypatch.setattr(_desktop_observer.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    real_print = print

    class Lease:
        """Platform-neutral protected handle; successful evidence now survives close."""

        def __init__(self, path):
            self.path = path
            path.touch(exist_ok=False)

        def seek(self, offset):
            assert offset == 0

        def read(self, size):
            return self.path.read_bytes()[:size]

        def close(self):
            pass

    def open_lease(path, *, delete_on_close=False):
        assert delete_on_close is False
        return Lease(path)

    monkeypatch.setattr(_desktop_observer, "_open_private_image", open_lease)

    original_notice = _desktop_observer._image_notice

    def observe_notice(record, status):
        original_notice(record, status)
        records.append((dict(record), {"flush": True}))
        noticed.set()

    monkeypatch.setattr(_desktop_observer, "_image_notice", observe_notice)

    def observe_print(message, **kwargs):
        real_print(message, **kwargs)
        if message.startswith("LOCAL_IMAGE:"):
            wire_records.append(message)

    monkeypatch.setattr(_desktop_observer, "print", observe_print, raising=False)

    class ImageChild:
        """Popen boundary double; communicate runs the real exact-target acquisition body."""

        def __init__(self, argv, **kwargs) -> None:
            assert Path(argv[1]) == Path(_desktop_observer.__file__).resolve()
            assert kwargs["creationflags"] == 0x08000000
            assert kwargs["stderr"] == subprocess.DEVNULL
            self.argv = argv
            self.cwd = Path(kwargs["cwd"])
            self.returncode = None
            self.finished = threading.Event()
            self.killed = False
            children.append(self)

        def communicate(self, timeout):
            assert 0 < timeout <= _desktop_observer.IMAGE_CAPTURE_SECONDS
            pid, hwnd, owner = map(int, self.argv[2:5])
            window = DesktopWindow("", "", 0, 0, hwnd=hwnd, owner_hwnd=owner, owner_enabled=False)
            try:
                payload = _desktop_observer._capture_exact_image(pid, window, self.cwd / self.argv[5], existing=True)
            except _desktop_observer._ImageUnavailable as exc:
                code = str(exc)
                payload = {"status": code, "ownership_checks": exc.checks}
            else:
                code = "ACQUIRED"
            self.returncode = 0 if code == "ACQUIRED" else 4
            self.finished.set()
            return json.dumps(payload).encode("ascii"), None

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -1
            self.finished.set()

        def wait(self, timeout):
            assert self.finished.wait(timeout), "child teardown must rendezvous before image cleanup"
            return self.returncode

    monkeypatch.setattr(_desktop_observer.subprocess, "Popen", ImageChild)
    return SimpleNamespace(
        api=api, noticed=noticed, records=records, wires=wire_records, children=children, root=tmp_path
    )


@pytest.fixture(name="dpi_runtime")
def dpi_runtime(visual_runtime):
    """A 125% physical-frame oracle; both the old crop and the full image are nonblank."""
    api = visual_runtime.api
    api.extent = (627, 330)
    api.virtual_extent = (502, 264)
    api.physical_bounds = (0, 0, 627, 330)
    pixels = bytearray(b"\xff\xff\xff\x00" * (627 * 330))
    for x_start, y_start, x_end, y_end, color in (
        (5, 5, 25, 25, b"\x00\x00\x00\x00"),
        (607, 310, 627, 330, b"\x00\x00\xff\x00"),
    ):
        for row in range(y_start, y_end):
            pixels[(row * 627 + x_start) * 4 : (row * 627 + x_end) * 4] = color * (x_end - x_start)
    api.frame = (627, 330, bytes(pixels))
    return visual_runtime


def _visual_child_result(runtime, capsys):
    basename = "_ui-image-" + "a" * 32 + ".png"
    result = _desktop_observer._image_child(["111", str(DIALOG_HWND), str(MAIN_HWND), basename])
    return result, json.loads(capsys.readouterr().out), runtime.root / basename


@pytest.mark.parametrize("initial_context", [-1, -2, -3])
def test_visual_dpi_capture_includes_independent_bottom_right_marker(dpi_runtime, capsys, initial_context) -> None:
    image_decoder = pytest.importorskip("PIL.Image", reason="independent PNG decoder is a repo dev extra")
    dpi_runtime.api.dpi_context = initial_context
    result, payload, path = _visual_child_result(dpi_runtime, capsys)
    assert result == 0 and payload["status"] == "ACQUIRED", "complete physical-frame acquisition was not established"
    with image_decoder.open(path) as image:
        # A crop outside the image is padded, not an IndexError; the intended assertion must fail.
        assert image.crop((607, 310, 627, 330)).getcolors() == [(400, (255, 0, 0))], (
            "ACQUIRED must include the independent bottom/right marker, not just the nonblank top-left crop"
        )
        assert image.size == (627, 330), "DWM's physical frame, not child-reported dimensions, is the oracle"
        assert image.getpixel((10, 10)) == (0, 0, 0) and image.getpixel((0, 0)) == (255, 255, 255)
    assert dpi_runtime.api.dpi_calls == [-3, initial_context]
    assert dpi_runtime.api.dpi_context == initial_context, "capture must restore the caller's DPI context"


def test_visual_dpi_virtualized_nonblank_crop_is_refused(monkeypatch, dpi_runtime, capsys) -> None:
    # Even a setter claiming success cannot substitute for the independently physical extent.
    dpi_runtime.api.SetThreadDpiAwarenessContext = _NativeCall(lambda _context: -1)
    result, payload, path = _visual_child_result(dpi_runtime, capsys)
    assert result == 4 and payload["status"] == "BLANK_OR_INCOMPLETE", (
        "the 502x264 virtualized crop cannot earn ACQUIRED against the independent 627x330 physical frame"
    )
    assert dpi_runtime.api.captures == [] and not path.exists()


@pytest.mark.parametrize("failure", ["missing", "refused"])
def test_visual_dpi_setup_failure_cannot_fall_back_to_virtualized_bounds(
    monkeypatch, dpi_runtime, capsys, failure
) -> None:
    if failure == "missing":
        monkeypatch.delattr(dpi_runtime.api, "SetThreadDpiAwarenessContext")
    else:
        dpi_runtime.api.SetThreadDpiAwarenessContext = _NativeCall(lambda _context: None)
    result, payload, path = _visual_child_result(dpi_runtime, capsys)
    assert result == 4 and payload["status"] == ("UNSUPPORTED" if failure == "missing" else "BLANK_OR_INCOMPLETE")
    assert dpi_runtime.api.captures == [] and not path.exists()
    assert dpi_runtime.api.dpi_context == -1


@pytest.mark.parametrize("failure", ["missing", "hresult", "empty", "right", "bottom", "left", "top"])
def test_visual_dpi_physical_extent_must_be_established(monkeypatch, dpi_runtime, capsys, failure) -> None:
    api = dpi_runtime.api
    if failure == "missing":
        monkeypatch.delattr(api.dwm, "DwmGetWindowAttribute")
    elif failure == "hresult":
        api.dwm_result = -1
    else:
        api.physical_bounds = {
            "empty": (0, 0, 0, 0),
            "right": (1, 0, 628, 330),
            "bottom": (0, 1, 627, 331),
            "left": (-1, 0, 626, 330),
            "top": (0, -1, 627, 329),
        }[failure]
    result, payload, path = _visual_child_result(dpi_runtime, capsys)
    assert result == 4 and payload["status"] == "BLANK_OR_INCOMPLETE"
    assert api.captures == [] and not path.exists()
    assert api.dpi_context == -1


def test_visual_dpi_physical_extent_is_rechecked_after_render(dpi_runtime, capsys) -> None:
    api = dpi_runtime.api
    api.after_render = lambda: setattr(api, "physical_bounds", (0, 0, 628, 330))
    result, payload, path = _visual_child_result(dpi_runtime, capsys)
    assert result == 4 and payload["status"] == "BLANK_OR_INCOMPLETE"
    assert len(api.captures) == 1 and api.writes == [] and not path.exists()
    assert api.dpi_context == -1


def test_visual_dpi_nonblank_render_missing_bottom_right_pixels_is_refused(dpi_runtime, capsys) -> None:
    api = dpi_runtime.api
    frame_width, _height, pixels = api.frame
    api.frame = (
        502,
        264,
        b"".join(pixels[row * frame_width * 4 : (row * frame_width + 502) * 4] for row in range(264)),
    )
    result, payload, path = _visual_child_result(dpi_runtime, capsys)
    assert result == 4 and payload["status"] == "BLANK_OR_INCOMPLETE", (
        "a nonblank top-left render missing the bottom/right marker cannot earn ACQUIRED"
    )
    assert api.writes == [] and not path.exists()


@pytest.mark.parametrize("origin", [(0, 0), (-700, -400)])
def test_visual_dpi_physical_extent_allows_invisible_borders(dpi_runtime, origin) -> None:
    api = dpi_runtime.api
    api.dpi_context = -3
    api.origin = origin
    api.extent = (643, 346)
    api.physical_bounds = (origin[0] + 8, origin[1] + 8, origin[0] + 635, origin[1] + 338)
    assert _desktop_observer._image_extent(api, DIALOG_HWND) == (643, 346), (
        "PrintWindow needs the whole physical window, not a visible-frame-sized crop that drops resize borders"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="native protected file DACL is Windows-only")
def test_visual_native_file_is_private_readable_and_never_overwrites(tmp_path) -> None:
    """The OS, not the writer's return value, proves atomic owner-only ACL and CreateNew semantics."""
    path = tmp_path / "_ui-image-native.png"
    expected = b"synthetic image control"
    try:
        _desktop_observer._write_private_image(path, expected)
        assert path.read_bytes() == expected
        escaped = str(path).replace("'", "''")
        command = (
            f"$ErrorActionPreference='Stop'; $path = '{escaped}'; "
            "$a = if ($PSVersionTable.PSEdition -eq 'Core') { "
            "[System.IO.FileSystemAclExtensions]::GetAccessControl([System.IO.FileInfo]::new($path)) "
            "} else { [System.IO.File]::GetAccessControl($path) }; "
            "$rules = @($a.GetAccessRules($true,$false,[System.Security.Principal.SecurityIdentifier])); "
            "[ordered]@{protected=$a.AreAccessRulesProtected; "
            "sids=@($rules | ForEach-Object {$_.IdentityReference.Value}); "
            "inherited=@($rules | ForEach-Object {$_.IsInherited})} | ConvertTo-Json -Compress"
        )
        done = subprocess.run(
            [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert done.returncode == 0, "native ACL read failed"
        actual = json.loads(done.stdout)
        assert actual == {"protected": True, "sids": ["S-1-3-4"], "inherited": [False]}
        with pytest.raises(_desktop_observer._ImageUnavailable, match="WRITE_FAILED"):
            _desktop_observer._write_private_image(path, b"must not overwrite")
        assert path.read_bytes() == expected
    finally:
        path.unlink(missing_ok=True)


def test_visual_child_bad_arguments_cannot_echo_private_data_or_create_a_file(tmp_path) -> None:
    done = subprocess.run(
        [sys.executable, str(Path(_desktop_observer.__file__)), "PRIVATE authentication C:\\source"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert done.returncode == 4
    assert json.loads(done.stdout)["status"] == "CAPTURE_FAILED"
    assert done.stderr == ""
    assert not list(tmp_path.iterdir())


_NODE_IMAGE_READER = (
    "const fs=require('fs'), crypto=require('crypto'); "
    "fs.writeSync(1,'READY\\n'); "
    "const path=JSON.parse(fs.readFileSync(0,'utf8')); "
    "const b=fs.readFileSync(path); "
    "console.log(JSON.stringify({bytes:b.length,sha256:crypto.createHash('sha256').update(b).digest('hex')}))"
)


@contextmanager
def _ready_native_process(argv: list[str], process_role: str) -> Iterator[subprocess.Popen]:
    """Bound setup separately from file access; always reap the exact child, including failed setup."""
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
        # Keep the existing 10s setup watchdog, but never let it kill a successfully prepared lease.
        deadline = threading.Timer(10, child.kill)
        try:
            deadline.start()
            try:
                ready = child.stdout.readline().strip()
            finally:
                deadline.cancel()
                deadline.join()
            assert ready == b"READY", (
                f"{process_role} startup failed before READY; sharing was not tested "
                f"(pid={child.pid}, exit={child.poll()}, output={ready!r})"
            )
            yield child
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)


def _read_native_image(reader: subprocess.Popen, path: Path) -> dict:
    """The unchanged 5s bound now measures a prepared external reader, not Node bootstrap."""
    try:
        output, _ = reader.communicate(json.dumps(str(path)).encode("utf-8"), timeout=5)
    except subprocess.TimeoutExpired:
        pytest.fail(f"reader read/hash timed out after READY (pid={reader.pid})")
    assert reader.returncode == 0, (
        f"reader read/hash failed after READY (pid={reader.pid}, exit={reader.returncode}, output={output!r})"
    )
    return json.loads(output)


def test_visual_reader_failed_startup_is_not_a_sharing_failure() -> None:
    """An exited reader cannot be mistaken for a sharing-contract failure."""
    with pytest.raises(AssertionError, match="reader startup failed before READY; sharing was not tested"):
        with _ready_native_process([sys.executable, "-c", "raise SystemExit(19)"], "reader"):
            pytest.fail("a reader that never became READY must not reach the sharing assertion")


@pytest.mark.skipif(sys.platform != "win32", reason="native sharing violation control is Windows-only")
def test_visual_reader_sharing_failure_is_distinct_from_failed_startup(tmp_path) -> None:
    """A running Node reader must report EBUSY, not a startup failure, for an exclusive native handle."""
    path = tmp_path / "_ui-image-exclusive.png"
    path.write_bytes(b"reader control")
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
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)  # GENERIC_READ, no sharing
    assert handle != ctypes.c_void_p(-1).value, "exclusive control handle setup failed"
    try:
        with _ready_native_process(["node", "-e", _NODE_IMAGE_READER], "reader") as reader:
            with pytest.raises(AssertionError, match="reader read/hash failed after READY.*EBUSY"):
                _read_native_image(reader, path)
    finally:
        assert kernel.CloseHandle(handle)
        path.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="kernel delete-on-close handles are Windows-only")
def test_visual_native_lease_allows_shared_reader_and_dies_with_its_process(tmp_path) -> None:
    """A real OS process kill, not Python finally/atexit, must remove its leased image."""
    path = tmp_path / "_ui-image-process-exit.png"
    script = (
        "import sys,time; from pathlib import Path; "
        f"sys.path.insert(0,{str(Path(_desktop_observer.__file__).parent)!r}); "
        "import _desktop_observer as m; "
        "lease=m._open_private_image(Path(sys.argv[1]),delete_on_close=True); "
        "lease.write(m._image_png(2,2,b'\\0\\0\\0'*2+b'\\xff\\xff\\xff'*2)); lease.flush(); "
        "print('READY',flush=True); time.sleep(60)"
    )
    try:
        # #650's communicate timeout did not establish that Node ever reached readFileSync.
        # Preload the SAME reader before creating the lease; send its path only after both are READY.
        with _ready_native_process(["node", "-e", _NODE_IMAGE_READER], "reader") as reader:
            with _ready_native_process([sys.executable, "-c", script, str(path)], "lease") as child:
                observed = _read_native_image(reader, path)
                expected = _desktop_observer._image_png(2, 2, b"\0\0\0" * 2 + b"\xff\xff\xff" * 2)
                assert observed == {"bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest()}
                child.kill()
                child.wait(timeout=3)
                cleanup_deadline = time.monotonic() + 1
                while path.exists() and time.monotonic() < cleanup_deadline:
                    time.sleep(0.005)  # native control: handle teardown followed process signalling by 5–6 ms
                assert not path.exists(), "kernel cleanup must remove pixels on forced observer exit"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="shared-delete handle behavior is Windows-only")
def test_visual_native_external_handling_marks_file_for_deletion(tmp_path) -> None:
    path = tmp_path / "_ui-image-handled.png"
    with _desktop_observer._open_private_image(path, delete_on_close=True) as lease:
        lease.write(b"synthetic")
        lease.flush()
        path.unlink()
        assert not path.exists(), "positive handling may remove the name while the observer holds its lease"
    assert not path.exists()


@pytest.mark.parametrize("missing", ["platform", "O_BINARY", "O_NOINHERIT"])
def test_visual_private_stream_requires_real_windows_support(monkeypatch, tmp_path, missing) -> None:
    def no_native_call(*_args, **_kwargs):
        pytest.fail("unsupported private-file flags must refuse before any Windows API call")

    monkeypatch.setattr(_desktop_observer.ctypes, "WinDLL", no_native_call, raising=False)
    if missing == "platform":
        monkeypatch.setattr(_desktop_observer.sys, "platform", "linux")
    else:
        monkeypatch.setattr(_desktop_observer.sys, "platform", "win32")
        monkeypatch.delattr(_desktop_observer.os, missing, raising=False)
    with pytest.raises(_desktop_observer._ImageUnavailable, match="UNSUPPORTED"):
        _desktop_observer._open_private_image(tmp_path / "_ui-image-unsupported.png")
    assert not list(tmp_path.iterdir())


def test_visual_enqueue_is_bounded_immutable_and_has_no_acquisition_side_effects(monkeypatch, tmp_path) -> None:
    # Freeze the consumer at its startup boundary so the queue, not a scheduler race, is the oracle.
    monkeypatch.setattr(_desktop_observer.threading.Thread, "start", lambda _self: None)
    evidence = _desktop_observer.ModalVisualEvidence(tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("enqueue must not traverse storage, inspect pixels, launch a process or start a thread")

    monkeypatch.setattr(_desktop_observer, "_evidence_directory", forbidden)
    monkeypatch.setattr(_desktop_observer, "_image_user32", forbidden)
    monkeypatch.setattr(_desktop_observer.subprocess, "Popen", forbidden)
    monkeypatch.setattr(_desktop_observer.threading.Thread, "start", forbidden)
    original = inspect_desktop_observer(111, lambda _pid: [owned_dialog(), main_window()])
    replacement = inspect_desktop_observer(111, lambda _pid: [owned_dialog(hwnd=DIALOG_HWND + 9), main_window()])
    try:
        evidence.enqueue(111, original)
        evidence.enqueue(222, replacement)
        assert evidence._pending.qsize() == 1, "a blocked observer must not accumulate an unbounded queue"
        pid, state, _deadline = evidence._pending.get_nowait()
        assert pid == 111 and state is original, "the queued exact target must not follow a later detector state"
        assert (state.dialog.window.hwnd, state.dialog.window.owner_hwnd) == (DIALOG_HWND, MAIN_HWND)
        with pytest.raises(FrozenInstanceError):
            state.dialog.window.hwnd = DIALOG_HWND + 9
        assert not list(tmp_path.iterdir())
    finally:
        evidence.close()


@pytest.mark.parametrize("kind", ["unreadable", "mixed-content", "unrecognized", "credential", "needs-human", "benign"])
@pytest.mark.parametrize("owner_enabled", [False, True, None])
def test_visual_capture_admission_uses_structural_stop_not_legacy_text_kind(monkeypatch, kind, owner_enabled) -> None:
    """A structural stop is eligible even when its owner is enabled or unavailable."""
    monkeypatch.setattr(_desktop_observer.threading.Thread, "start", lambda _self: None)
    window = _desktop_observer.replace(owned_dialog(), owner_enabled=owner_enabled)
    finding = DialogFinding(kind, _desktop_observer.DIALOG_KIND_VERDICTS[kind], window)
    state = Observation(dialog=finding)
    with _desktop_observer.ModalVisualEvidence() as evidence:
        eligible = kind == "needs-human"
        assert evidence.enqueue(111, state) is eligible
        assert evidence._pending.qsize() == int(eligible)
        assert state.dialog is finding and state.modal is None


@pytest.mark.parametrize("captured", [False, True])
def test_terminal_image_hold_waits_only_for_capture_not_image_expiry(monkeypatch, captured) -> None:
    """Success and failure both return after acquisition; no 60-second lease wait remains."""
    monkeypatch.setattr(_desktop_observer.threading.Thread, "start", lambda _self: None)
    events = []
    with _desktop_observer.ModalVisualEvidence() as evidence:
        monkeypatch.setattr(evidence, "enqueue", lambda pid, state: events.append(("enqueue", pid)) or True)
        monkeypatch.setattr(
            evidence,
            "_capture_finished",
            SimpleNamespace(wait=lambda seconds: events.append(("capture", seconds)) or True),
        )
        existence = iter([True, False] if captured else [])
        evidence._requests = (
            [SimpleNamespace(record={"capture_success": True}, path=SimpleNamespace(exists=lambda: next(existence)))]
            if captured
            else []
        )
        monkeypatch.setattr(_desktop_observer.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
        try:
            evidence.hold_refusal(111, Observation(dialog=_desktop_observer.classify_dialog(owned_dialog())))
            assert events == [("enqueue", 111), ("capture", 8.0)]
        finally:
            evidence._requests = []


@pytest.mark.parametrize("fault", ["unowned", "missing_hwnd", "capture_timeout"])
def test_terminal_image_hold_never_waits_for_ineligible_or_timed_out_capture(monkeypatch, fault) -> None:
    """No owner/identity amnesty and no lease wait when the eight-second acquisition fails."""
    monkeypatch.setattr(_desktop_observer.threading.Thread, "start", lambda _self: None)
    window = owned_dialog()
    if fault == "unowned":
        window = _desktop_observer.replace(window, owner_hwnd=0)
    if fault == "missing_hwnd":
        window = _desktop_observer.replace(window, hwnd=0)
    waits = []
    with _desktop_observer.ModalVisualEvidence() as evidence:
        monkeypatch.setattr(
            evidence, "_capture_finished", SimpleNamespace(wait=lambda seconds: waits.append(seconds) or False)
        )
        monkeypatch.setattr(_desktop_observer.time, "sleep", lambda _seconds: pytest.fail("no eligible acquired lease"))
        evidence.hold_refusal(111, Observation(dialog=DialogFinding("needs-human", "DIALOG_NEEDS_HUMAN", window)))
        assert waits == ([] if fault == "missing_hwnd" else [8.0])


def test_visual_directory_must_be_ignored_inside_a_checkout(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    initialized = subprocess.run(
        ["git", "init", "--quiet", str(repo)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    )
    assert initialized.returncode == 0
    (repo / ".gitignore").write_text("/scratch/\n", encoding="utf-8")
    safe = repo / "scratch"
    safe.mkdir()
    (repo / "fabric").mkdir()  # a sibling artifact tree does not turn ignored run scratch into an artifact
    tracked = repo / "source"
    tracked.mkdir()
    basename = "_ui-image-" + "a" * 32 + ".png"
    assert _desktop_observer._evidence_directory(safe, basename) == safe.resolve()
    with pytest.raises(_desktop_observer._ImageUnavailable, match="UNSAFE_EVIDENCE_DIR"):
        _desktop_observer._evidence_directory(tracked, basename)
    assert not list(repo.rglob("*.png"))


@pytest.mark.parametrize("sibling", ["fabric", "pbip"])
def test_visual_directory_accepts_scratch_with_unrelated_ancestor_siblings(tmp_path, sibling) -> None:
    # Reproduces a drive root with C:\pbip beside C:\Users\...\scratch, without touching the drive root.
    drive = tmp_path / "drive"
    safe = drive / "Users" / "operator" / "scratch"
    safe.mkdir(parents=True)
    unrelated = drive / sibling
    unrelated.mkdir()
    basename = "_ui-image-" + "a" * 32 + ".png"
    assert _desktop_observer._evidence_directory(safe, basename) == safe.resolve(), (
        "an unrelated ancestor sibling must not make private scratch an artifact"
    )
    contained = unrelated / "scratch"
    contained.mkdir()
    with pytest.raises(_desktop_observer._ImageUnavailable, match="UNSAFE_EVIDENCE_DIR"):
        _desktop_observer._evidence_directory(contained, basename)
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.parametrize(
    "artifact",
    ["packages", "deliverables", "fabric", "pbip", "reports", "semantic_models", "Unit.Report", "Unit.SemanticModel"],
)
def test_visual_directory_refuses_artifact_trees(tmp_path, artifact) -> None:
    directory = tmp_path / artifact / "scratch"
    directory.mkdir(parents=True)
    with pytest.raises(_desktop_observer._ImageUnavailable, match="UNSAFE_EVIDENCE_DIR"):
        _desktop_observer._evidence_directory(directory, "_ui-image-" + "a" * 32 + ".png")
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.parametrize("marker", ["definition.pbir", "definition.pbism", "unit.pbip", "package-manifest.json"])
@pytest.mark.parametrize("checkout", [False, True], ids=["ordinary-root", "git-root"])
def test_visual_directory_refuses_renamed_artifacts(tmp_path, marker, checkout) -> None:
    unit = tmp_path / "renamed"
    directory = unit / "scratch"
    directory.mkdir(parents=True)
    (unit / marker).write_text("{}", encoding="utf-8")
    if checkout:
        result = subprocess.run(["git", "init", "--quiet", str(unit)], capture_output=True, check=False)
        assert result.returncode == 0
        (unit / ".gitignore").write_text("/scratch/\n", encoding="utf-8")
    for destination in (unit, directory):
        with pytest.raises(_desktop_observer._ImageUnavailable, match="UNSAFE_EVIDENCE_DIR"):
            _desktop_observer._evidence_directory(destination, "_ui-image-" + "a" * 32 + ".png")


@pytest.mark.gui
@pytest.mark.serial
@pytest.mark.skipif(sys.platform != "win32", reason="native PrintWindow control is Windows-only")
def test_visual_native_background_capture_reaches_production_without_focus(monkeypatch, tmp_path) -> None:
    """Real child/GDI/ACL/PNG against a physical-frame oracle; NOACTIVATE, never Desktop."""
    image_decoder = pytest.importorskip("PIL.Image", reason="independent PNG decoder is a repo dev extra")
    native = _NativeWindowProbe("ImageOnly")
    ui = native.user32
    ui.EnableWindow.argtypes, ui.EnableWindow.restype = [wintypes.HWND, wintypes.BOOL], wintypes.BOOL
    ui.GetForegroundWindow.argtypes, ui.GetForegroundWindow.restype = [], wintypes.HWND
    ui.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    ui.PeekMessageW.restype = wintypes.BOOL
    ui.TranslateMessage.argtypes, ui.TranslateMessage.restype = [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL
    ui.DispatchMessageW.argtypes, ui.DispatchMessageW.restype = [ctypes.POINTER(wintypes.MSG)], ctypes.c_longlong
    ui.UpdateWindow.argtypes, ui.UpdateWindow.restype = [wintypes.HWND], wintypes.BOOL
    ui.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    ui.SetWindowLongPtrW.restype = ctypes.c_void_p
    ui.CallWindowProcW.argtypes = [
        ctypes.c_void_p,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    ui.CallWindowProcW.restype = ctypes.c_longlong
    ui.FillRect.argtypes, ui.FillRect.restype = (
        [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH],
        ctypes.c_int,
    )
    ui.SetThreadDpiAwarenessContext.argtypes, ui.SetThreadDpiAwarenessContext.restype = (
        [ctypes.c_void_p],
        ctypes.c_void_p,
    )
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.GetStockObject.argtypes, gdi.GetStockObject.restype = [ctypes.c_int], wintypes.HANDLE
    original_proc = [None]

    @native.wndproc_type
    def paint(window, message, dc, parameter):
        if message in (0x0317, 0x0318):  # WM_PRINT / WM_PRINTCLIENT: independent, deterministic fixture pixels
            whole = wintypes.RECT(0, 0, 627, 330)
            square = wintypes.RECT(5, 5, 25, 25)
            marker = wintypes.RECT(607, 310, 627, 330)
            ui.FillRect(dc, ctypes.byref(whole), gdi.GetStockObject(0))
            ui.FillRect(dc, ctypes.byref(square), gdi.GetStockObject(4))
            ui.FillRect(dc, ctypes.byref(marker), gdi.GetStockObject(4))
            return 0
        return ui.CallWindowProcW(original_proc[0], window, message, dc, parameter)

    before = ui.GetForegroundWindow()
    released = threading.Event()
    notices = []
    outcome = {}
    thread = None
    previous_dpi = None
    real_print = print

    def record(message, **kwargs):
        real_print(message, **kwargs)
        if message.startswith("LOCAL_IMAGE "):
            notices.append(json.loads(message.removeprefix("LOCAL_IMAGE ")))

    def pump():
        message = wintypes.MSG()
        while ui.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            ui.TranslateMessage(ctypes.byref(message))
            ui.DispatchMessageW(ctypes.byref(message))

    try:
        # Only this fixture thread becomes aware here; the production child is a fresh process.
        previous_dpi = ui.SetThreadDpiAwarenessContext(-3)
        assert previous_dpi, "could not establish the native fixture's physical coordinates"
        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: neither setup nor acquisition touches foreground.
        owner = ui.CreateWindowExW(0x08000080, "STATIC", "", 0x90000006, 0, 0, 700, 400, None, None, None, None)
        assert owner
        native.created.append(owner)
        hwnd = ui.CreateWindowExW(0x08000080, "STATIC", "", 0x90000006, 0, 0, 627, 330, owner, None, None, None)
        assert hwnd
        native.created.append(hwnd)
        original_proc[0] = ui.SetWindowLongPtrW(hwnd, -4, ctypes.cast(paint, ctypes.c_void_p))
        assert original_proc[0]
        rectangle = ui.CreateWindowExW(0, "STATIC", "", 0x50000004, 5, 5, 20, 20, hwnd, None, None, None)
        assert rectangle
        # PW_RENDERFULLCONTENT can use the compositor instead of WM_PRINT. Both paths need markers.
        edge_marker = ui.CreateWindowExW(0, "STATIC", "", 0x50000004, 607, 310, 20, 20, hwnd, None, None, None)
        assert edge_marker
        ui.EnableWindow(owner, False)
        pump()
        ui.UpdateWindow(hwnd)
        ui.UpdateWindow(rectangle)
        ui.UpdateWindow(edge_marker)
        physical = wintypes.RECT()
        assert dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(physical), ctypes.sizeof(physical)) == 0
        physical_size = (physical.right - physical.left, physical.bottom - physical.top)
        assert physical_size == (627, 330), "the independent DWM oracle must describe the complete fixture"
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(_desktop_observer.IMAGE_DIRECTORY_ENV, str(tmp_path))
        monkeypatch.setattr(_desktop_observer, "print", record, raising=False)
        monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: ParkedConnection(released))
        monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-native")
        monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.02)

        def state(pid, *, in_flight=False, **_kwargs):
            return inspect_desktop_observer(pid) if in_flight else Observation()

        monkeypatch.setattr(refresh_pbip_model, "_credential_state", state)

        def run():
            try:
                outcome["result"] = refresh(
                    port=1234, tables=None, desktop_pid=os.getpid(), progress_enabled=False, timeout_sec=20
                )
            except BaseException as exc:
                outcome["error"] = type(exc).__name__

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 12
        while not notices and time.monotonic() < deadline:
            pump()
            time.sleep(0.005)
        assert notices and notices[0]["dialog_hwnd"] == str(hwnd), "the fixture must exercise its intended dialog"
        assert notices and notices[0]["status"] == "ACQUIRED", "native acquisition never produced usable evidence"
        assert thread.is_alive()
        assert ui.GetForegroundWindow() == before, "capture must not alter foreground"
        with (
            _desktop_observer._open_private_image(tmp_path / notices[0]["path"], existing=True) as stream,
            image_decoder.open(stream) as image,
        ):
            outside, inside = image.getpixel((0, 0)), image.getpixel((10, 10))
            assert inside != outside, "the modal has a child rectangle; its owner is uniformly filled"
            assert (
                image.width >= physical_size[0]
                and image.height >= physical_size[1]
                and image.crop((607, 310, 627, 330)).getcolors() == [(400, inside)]
            ), "ACQUIRED must retain the physical bottom/right marker, even when the top-left crop is nonblank"
            assert image.size == physical_size, "DWM bounds must agree independently of the image child's metadata"
            # Both markers are supplied independently of the production extent, GDI and PNG code.
            for y in range(physical_size[1]):
                for x in range(physical_size[0]):
                    marked = (5 <= x < 25 and 5 <= y < 25) or (607 <= x and 310 <= y)
                    expected = inside if marked else outside
                    assert image.getpixel((x, y)) == expected
    finally:
        released.set()
        if thread is not None:
            # Settle the started bounded native/UIA helper (<=8s), not just the former Win32 call.
            deadline = time.monotonic() + 12
            while thread.is_alive() and time.monotonic() < deadline:
                # The production Win32 text check can send messages to this test's UI thread too.
                pump()
                thread.join(0.005)
        native.close()
        if previous_dpi:
            assert ui.SetThreadDpiAwarenessContext(previous_dpi), "fixture DPI restoration failed"
    assert outcome.get("result", (False,))[0] is True, outcome
    assert not list(tmp_path.glob("_ui-image-*.png"))


class ParkedConnection:
    """ADOMD stand-in whose command never returns until the test releases it."""

    def __init__(self, released: threading.Event) -> None:
        self.released = released

    def Open(self) -> None:
        """Match the ADOMD API surface."""

    def CreateCommand(self):
        """Return a command that blocks until released."""
        return ParkedCommand(self.released)

    def Close(self) -> None:
        """Match the ADOMD API surface."""


class ParkedCommand:
    """Command stand-in that ignores CommandTimeout."""

    CommandText = ""
    CommandTimeout = 0

    def __init__(self, released: threading.Event) -> None:
        self.released = released

    def ExecuteNonQuery(self) -> None:
        """Block long enough that only the outer poll/deadline can end the test."""
        self.released.wait(timeout=600)


@pytest.fixture(name="parked")
def parked_fixture(monkeypatch):
    """Wire refresh() to a parked ADOMD connection."""
    released = threading.Event()
    conn = ParkedConnection(released)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: conn)
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 0.15)
    yield released
    released.set()


class _ParkedAdomd:
    """ADOMD stand-in whose command never returns, driven as a whole connection factory result.

    Distinct from the ``parked`` fixture: the finding-1 tests need to control the release event and to
    run with a fake progress monitor, so they wire the seams themselves.
    """

    CommandText = ""
    CommandTimeout = 0

    def __init__(self, released: threading.Event) -> None:
        self.released = released

    def Open(self) -> None:
        """Match the ADOMD API surface."""

    def CreateCommand(self):
        """This object is its own command."""
        return self

    def ExecuteNonQuery(self) -> None:
        """Block until the test releases it, so only the deadline can end the wait."""
        self.released.wait(timeout=600)

    def Close(self) -> None:
        """Match the ADOMD API surface."""


class _FakeProgressMonitor:
    """Minimal ``RefreshProgressMonitor`` stand-in so the progress-monitor wait branch is reachable.

    That branch needs an AMO trace, which needs pythonnet and a live server; without this the branch is
    untestable off Windows and its own copy of the poll loop went unexercised - which is how it kept a
    t=0 raise helper and discarded its latches (#400 review, finding 1).
    """

    def mark_refresh_started(self) -> None:
        """No-op."""

    def print_liveness_warning_if_due(self) -> None:
        """No-op."""

    def seconds_until_liveness_warning(self) -> float:
        """Never due, so the poll interval is what drives the loop."""
        return 999.0

    def print_evidence_heartbeat(self, elapsed: float, total: float) -> None:
        """No-op."""

    def close(self) -> None:
        """No-op."""

    def summary(self) -> str:
        """Match the monitor's reporting surface."""
        return "fake"


MAIN_HWND = 0x10001
DIALOG_HWND = 0x20002


def main_window(
    title: str = "sample-superstore",
    texts: tuple[str, ...] = ("Report",),
    *,
    minimized: bool = False,
    width: int = 2011,
    height: int = 1298,
) -> DesktopWindow:
    """The Power BI Desktop application FRAME: unowned, and the window a modal would disable."""
    return DesktopWindow(
        title,
        DESKTOP_MAIN_CLASS_PREFIX + ".app.0.33c0d9d",
        width,
        height,
        texts,
        minimized=minimized,
        hwnd=MAIN_HWND,
    )


def owned_dialog(
    texts: tuple[str, ...] = (),
    *,
    title: str = "",
    class_name: str = "WindowsForms10.Window.20008.app.0.33c0d9d",
    width: int = 702,
    height: int = 355,
    owner_enabled: bool | None = False,
    hwnd: int = DIALOG_HWND,
) -> DesktopWindow:
    """A dialog OWNED by :func:`main_window`.

    ``owner_enabled=False`` by default - the owner is disabled, which is what a modal does. That does
    not convict the window (Power BI's own refresh dialog disables the owner too); it only means the
    one-way enabled-owner exoneration does not apply, so the window has to be classified on its text.
    """
    return DesktopWindow(
        title,
        class_name,
        width,
        height,
        texts,
        hwnd=hwnd,
        owner_hwnd=MAIN_HWND,
        owner_enabled=owner_enabled,
    )


def modal() -> CredentialModal:
    """A credential dialog matching the measured incident window."""
    return CredentialModal(
        "Please specify how to connect to this data source",
        DesktopWindow("", "WindowsForms10.Window.20008", 702, 355, ("Please specify how to connect",)),
    )


def modal_state() -> Observation:
    """Observation containing the standard modal."""
    return Observation(modal=modal())


def unreadable_dialog_window() -> DesktopWindow:
    """The live SQL credential-dialog shape: visible, owned, empty title, NO readable text at all."""
    return owned_dialog()


def progress_dialog_window() -> DesktopWindow:
    """Power BI's own Refresh progress dialog: a caption AND content that positively reads as status.

    This is the shape the size-only rule reported as a hard stop (issue #376) - >= 100x100, non-main
    class, and therefore indistinguishable from a sign-in prompt to a detector that never read it.
    """
    return owned_dialog(("Refresh", "Evaluating...", "Cancel"), title="Refresh")


def dialog_finding(window: DesktopWindow | None = None) -> DialogFinding:
    """A legacy refusal fixture; the wire/exception adapters must remain readable."""
    return DialogFinding("unreadable", "DIALOG_UNREADABLE", window or unreadable_dialog_window())


def dialog_state(window: DesktopWindow | None = None) -> Observation:
    """Observation carrying a real dialog finding."""
    return Observation(dialog=dialog_finding(window))


def minimized_main_windows() -> list[DesktopWindow]:
    """The measured minimized-owner shape: a small, iconic Desktop main window and nothing else."""
    return [main_window(title="Report", minimized=True, width=159, height=27)]


def restored_main_windows() -> list[DesktopWindow]:
    """The same owner restored: full-size, not iconic, and - measured - the dialog does NOT return."""
    return [main_window(title="Report")]


def visible_unreadable_dialog_windows() -> list[DesktopWindow]:
    """A visible owned dialog (empty title, unreadable) alongside the healthy main window."""
    return [owned_dialog(), main_window(title="Report")]


def visible_progress_dialog_windows() -> list[DesktopWindow]:
    """Power BI's own Refresh progress dialog alongside the healthy main window (issue #376)."""
    return [progress_dialog_window(), main_window(title="Report")]


def harvested_minimized_reason() -> str:
    """The REAL minimized-owner ``unknown_reason``, harvested from the detector (never hand-written).

    The prior #153 blind-spot was a test that hand-wrote a reason production could not emit; the latch
    tests must assert against the exact string ``inspect_desktop_observer`` really produces.
    """
    reason = inspect_desktop_observer(999, lambda _pid: minimized_main_windows()).unknown_reason
    assert reason is not None, "detector did not report the minimized-owner UNKNOWN reason"
    return reason


def harvested_zero_window_alive_reason() -> str:
    """The REAL zero-window-but-alive readiness reason, harvested from the detector (issue #158).

    Never hand-written: the same #153 blind-spot discipline as the minimized reason - the latch tests
    must assert the exact string ``inspect_desktop_observer`` really produces when enumeration returns
    an empty list while the process is still alive.
    """
    reason = inspect_desktop_observer(999, lambda _pid: [], process_is_alive=lambda _pid: True).desktop_unready
    assert reason is not None, "detector did not report the zero-window alive DESKTOP_UNREADY reason"
    return reason


def harvested_desktop_gone_reason() -> str:
    """The REAL ``process_gone`` string, harvested from the detector (issue #158).

    Driven with an empty enumerator and a liveness check that reports the process dead, so the
    ``DesktopGoneError`` tests assert the exact terminal string production emits, never a hand-written
    one.
    """
    reason = inspect_desktop_observer(999, lambda _pid: [], process_is_alive=lambda _pid: False).process_gone
    assert reason is not None, "detector did not report the process-gone terminal reason"
    return reason


def explode(name: str):
    """Return a stub that proves an operation was not reached."""

    def boom(*_args, **_kwargs):
        raise AssertionError(f"{name}() must not run once the credential modal is visible")

    return boom


def real_detector_for_zero_windows(*, alive: bool):
    """A `_credential_state` built from the REAL detector, with the Win32 primitives injected.

    The production `_credential_state` in both entry points opens with
    ``if os.name != "nt": return Observation()``, so calling it un-stubbed runs *different code
    on different platforms*. That is not a reason to leave the classification untested off Windows:
    `inspect_desktop_observer` already takes `enumerate_windows` and `process_is_alive` as parameters,
    so composing it with fakes here exercises the genuine branch logic - the same code Windows runs -
    on every platform, and drives it through the real CLI entry point rather than a hand-written
    `Observation`.
    """

    def detect(pid: int, **_kwargs) -> Observation:
        return inspect_desktop_observer(pid, lambda _pid: [], process_is_alive=lambda _pid: alive)

    return detect


def model_folder(root: Path, name: str) -> Path:
    """Create a minimal PBIP sibling model and return its cache destination."""
    tables_dir = root / f"{name}.SemanticModel" / "definition" / "tables"
    tables_dir.mkdir(parents=True)
    (tables_dir / "Orders.tmdl").write_text(
        '/// doc\ntable \'Orders\'\n\n\tcolumn X\n\npartition p = m\n\tSource = Sql.Database("server", "db")\n',
        encoding="utf-8",
    )
    (root / f"{name}.pbip").write_text("{}", encoding="utf-8")
    return root / f"{name}.SemanticModel" / ".pbi" / "cache.abf"


def test_win32_fixture_finds_owned_dialog_and_keeps_instances_separate() -> None:
    """Faithful fixture: owned empty-title dialog on PID A, no dialog on concurrent PID B."""
    windows_by_pid = {
        111: [
            owned_dialog(("Enter your credentials",)),
            main_window(),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
        222: [main_window(title="cached-model")],
    }
    hit = inspect_desktop_observer(111, lambda pid: windows_by_pid[pid]).dialog
    miss = inspect_desktop_observer(222, lambda pid: windows_by_pid[pid])

    assert hit is not None
    assert hit.window.class_name.startswith("WindowsForms10.Window.20008")
    assert miss.dialog is None
    assert miss.unknown_reason is None


def test_unreadable_owned_dialog_reports_unreadable_not_no_modal() -> None:
    """Live SQL dialog shape: visible owned dialog, empty title, no readable text.

    Absence of text is its own finding - ``DIALOG_UNREADABLE`` - and must never decay into "healthy".
    """
    windows_by_pid = {
        58104: [
            owned_dialog(),
            main_window(texts=("sample",)),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
        46256: [main_window(title="cached", texts=("cached",))],
    }
    blocked = inspect_desktop_observer(58104, lambda pid: windows_by_pid[pid])
    healthy = inspect_desktop_observer(46256, lambda pid: windows_by_pid[pid])

    assert blocked.modal is None
    assert blocked.dialog is not None
    assert blocked.dialog.kind == "needs-human"
    assert blocked.dialog.verdict == "DIALOG_NEEDS_HUMAN"
    assert blocked.dialog.window.width == 702
    assert healthy.modal is None
    assert healthy.dialog is None


def test_an_unowned_zero_area_helper_window_does_not_block() -> None:
    """A window with NO owner and NO pixels blocks nothing - on two independent grounds, not on size.

    `Internet Explorer_Hidden` at 0x0 rides along on healthy instances. It is excluded because it has
    no owner to disable AND nothing to display, never because of its name or its geometry: the same
    class at 900x700, and the same 0x0 shape with an owner, are both classified (see the round-3
    tests below).
    """
    state = inspect_desktop_observer(
        111,
        lambda _pid: [
            main_window(title="cached", texts=("cached",)),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
    )

    assert state.modal is None
    assert state.dialog is None


def test_minimized_owner_reports_unknown_not_no_dialog() -> None:
    """When the owner is minimized, Windows hides owned dialogs; absence is indeterminate."""
    state = inspect_desktop_observer(111, lambda _pid: [main_window(minimized=True)])

    assert state.modal is None
    assert state.unknown_reason is not None
    assert "minimized" in state.unknown_reason


def test_zero_windows_alive_reports_desktop_unready_not_no_dialog() -> None:
    """#158: an empty enumeration while the process is ALIVE is a local readiness failure, not healthy.

    A live Desktop always owns at least its main window, so zero windows is never proof of health. When
    the process is still running (starting up or wedged) the detector must report ``desktop_unready``
    and must NOT set ``process_gone`` (the process is not gone) nor a modal/blocking dialog.
    """
    state = inspect_desktop_observer(111, lambda _pid: [], process_is_alive=lambda _pid: True)

    assert state.modal is None
    assert state.dialog is None
    assert state.process_gone is None
    assert state.unknown_reason is None
    assert state.desktop_unready is not None
    assert "no windows" in state.desktop_unready
    assert state.desktop_unready == harvested_zero_window_alive_reason()


def test_zero_windows_dead_reports_process_gone_not_no_dialog() -> None:
    """#158: an empty enumeration while the process is DEAD is a definitive terminal state.

    Zero windows plus a confirmed-dead PID means Desktop exited or crashed before any dialog state
    could be observed. This is the distinct ``process_gone`` terminal outcome - it must NOT masquerade
    as ``none`` (healthy) and must NOT be reported as ``unknown_reason`` (indeterminate/latch), because
    a dead process is not going to recover and the data source was never contacted.
    """
    state = inspect_desktop_observer(111, lambda _pid: [], process_is_alive=lambda _pid: False)

    assert state.modal is None
    assert state.dialog is None
    assert state.unknown_reason is None
    assert state.process_gone is not None
    assert "no longer running" in state.process_gone
    assert state.process_gone == harvested_desktop_gone_reason()


@pytest.mark.timing
def test_direct_refresh_returns_credential_missing_fast_at_t0(monkeypatch) -> None:
    """A t=0 credential modal must not wait for the XMLA deadline."""
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: modal_state())
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(CredentialMissingError):
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111, source_hint="Sql.Database(server)")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"t=0 modal path waited {elapsed:.3f}s instead of returning immediately"


@pytest.mark.timing
def test_direct_refresh_stops_at_t0_for_a_dialog_it_could_not_read(monkeypatch) -> None:
    """An unreadable dialog present BEFORE we start anything must stop immediately, not wait for XMLA.

    t=0 is the one moment a dialog finding is acted on rather than latched: nothing of ours is running,
    the dialog is somebody else's, and stacking a refresh on an unclassified dialog is what the
    2026-08-28 field report had to unpick by hand.
    """
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: dialog_state())
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(DialogFoundError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111)
    elapsed = time.monotonic() - started

    assert excinfo.value.finding.verdict == "DIALOG_UNREADABLE"
    assert elapsed < 0.5


@pytest.mark.timing
def test_direct_refresh_raises_desktop_gone_fast_at_t0(monkeypatch) -> None:
    """#158: a Desktop already dead at t=0 must bail immediately, never start the XMLA wait.

    The t=0 pre-check (``_raise_if_blocked``) is load-bearing: if the process was gone before the worker
    even started, waiting out the deadline learns nothing. The reason is harvested from the real
    detector, so this pins the terminal path against the exact string production emits.
    """
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: Observation(process_gone=reason))
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(DesktopGoneError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111)
    elapsed = time.monotonic() - started

    assert excinfo.value.reason == reason
    assert elapsed < 0.5, f"t=0 process-gone path waited {elapsed:.3f}s instead of returning immediately"


@pytest.mark.timing
def test_refresh_poll_catches_late_modal(monkeypatch, parked) -> None:
    """A credential dialog appearing after XMLA starts is caught on the next poll."""
    calls = {"count": 0}

    def late_modal(_pid: int):
        calls["count"] += 1
        return modal() if calls["count"] >= 2 else None

    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda pid, **_kw: Observation(modal=late_modal(pid)),
    )
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)

    started = time.monotonic()
    with pytest.raises(CredentialMissingError):
        refresh(port=1234, tables=["Orders"], timeout_sec=10, desktop_pid=111, progress_enabled=False)

    assert time.monotonic() - started < 1.0, "shortened test poll interval should catch the modal quickly"
    assert calls["count"] >= 2
    parked.set()


def test_refresh_banner_is_flushed_and_heartbeat_reports_elapsed_total(monkeypatch, parked, capsys) -> None:
    """The warning arrives before the wait, then emits elapsed/total countdowns."""
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid, **_kw: Observation())
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_HEARTBEAT_SECONDS", 0.05)
    printed: list[dict[str, object]] = []
    original_print = print

    def recording_print(*args, **kwargs):
        printed.append({"text": " ".join(str(arg) for arg in args), "flush": kwargs.get("flush")})
        original_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", recording_print)

    with pytest.raises(TimeoutError):
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    out = capsys.readouterr().out
    assert "No blocking dialog on PID 111. Refreshing, bounded at 0.1s XMLA + 0.15s grace" in out
    assert "DO NOT kill this process" in out
    assert "still refreshing," in out and "/ 0s" in out
    assert printed[0]["flush"] is True
    parked.set()


def test_unknown_refresh_banner_does_not_claim_no_dialog(monkeypatch, parked, capsys) -> None:
    """A minimized owner emits the UNKNOWN banner and, once latched, ends as CREDENTIAL_UNKNOWN.

    Before #154 this fell through to a bare ``TimeoutError`` (which the parent blamed on a slow
    source). The reason is harvested from the real detector, not hand-written (the #153 blind-spot).
    """
    reason = harvested_minimized_reason()
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid, **_kw: Observation(unknown_reason=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_HEARTBEAT_SECONDS", 0.05)

    with pytest.raises(CredentialUnknownError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    assert excinfo.value.reason == reason
    out = capsys.readouterr().out
    assert "Blocking-dialog check on PID 111 is UNKNOWN" in out
    assert "No blocking dialog on PID 111" not in out
    parked.set()


def test_refresh_latches_unknown_seen_only_by_initial_precheck(monkeypatch, parked) -> None:
    """#154: UNKNOWN at t=0 cannot disappear before the first poll."""
    reason = harvested_minimized_reason()
    states = iter([Observation(unknown_reason=reason)])

    def initial_unknown_then_healthy(_pid: int, **_kw) -> Observation:
        return next(states, Observation())

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", initial_unknown_then_healthy)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)

    with pytest.raises(CredentialUnknownError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    assert excinfo.value.reason == reason
    parked.set()


def test_refresh_latches_desktop_unready_seen_only_by_initial_precheck(monkeypatch, parked) -> None:
    """#158's twin of the #154 latch: DESKTOP_UNREADY at t=0 must survive a healthy first poll.

    The untested twin of `test_refresh_latches_unknown_seen_only_by_initial_precheck`. It is reachable
    only through a direct ``refresh(desktop_pid=...)`` call - `main` terminates at exit 2 before this -
    but that is exactly why it needs pinning rather than skipping: without the initial-state carry, a
    t=0-only readiness failure decays into a bare ``TimeoutError``, which the parent classifier reads
    as a slow source. That wrong-verdict family is the whole reason this change exists, so the fact
    that the *unknown* half is tested and the *unready* half is not is a coverage hole, not a
    reachability argument.
    """
    reason = harvested_zero_window_alive_reason()
    states = iter([Observation(desktop_unready=reason)])

    def initial_unready_then_healthy(_pid: int, **_kw) -> Observation:
        return next(states, Observation())

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", initial_unready_then_healthy)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)

    with pytest.raises(DesktopUnreadyError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    assert excinfo.value.reason == reason
    parked.set()


class _FakeClock:
    """A monotonic clock the test advances explicitly, so the poll deadline is deterministic."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current frozen time."""
        return self.now

    def advance(self, step: float) -> None:
        """Move time forward by ``step`` seconds."""
        self.now += step


class _ImmortalWorker:
    """A worker that never finishes; each ``join`` advances the injected clock toward the deadline."""

    def __init__(self, clock: _FakeClock, step: float) -> None:
        self._clock = clock
        self._step = step

    def is_alive(self) -> bool:
        """Always alive - this simulates the parked mashup engine that only the deadline can end."""
        return True

    def join(self, timeout: float | None = None) -> None:
        """Ignore the requested wait and advance the fake clock by a fixed step."""
        del timeout
        self._clock.advance(self._step)


class _ScriptedEnumerator:
    """Feed ``inspect_desktop_observer`` a scripted sequence of window-lists; the last phase sticks."""

    def __init__(self, phases: list[list[DesktopWindow]]) -> None:
        self._phases = phases
        self.calls = 0

    def __call__(self, _pid: int) -> list[DesktopWindow]:
        phase = self._phases[min(self.calls, len(self._phases) - 1)]
        self.calls += 1
        return list(phase)


def _real_detector(enumerator: _ScriptedEnumerator, process_is_alive=lambda _pid: True, *, in_flight: bool = False):
    """Inject only facts; caller-phase handling never changes the observer's factual state."""
    del in_flight
    return lambda pid: inspect_desktop_observer(pid, enumerator, process_is_alive=process_is_alive)


def test_poll_loop_latches_minimize_restore_and_raises_unknown(monkeypatch) -> None:
    """#154 core: the measured minimize -> restore -> deadline sequence must latch and raise UNKNOWN.

    Drives the REAL ``join_with_credential_poll`` and REAL ``inspect_desktop_observer`` through an
    injected fake enumerator. Poll 1 sees the iconic owner (UNKNOWN); every later poll sees the
    restored owner with the dialog gone (``none``). The first observation must be latched so the loop
    raises :class:`CredentialUnknownError` at the deadline instead of returning ``False`` (a bare
    timeout the parent blames on a slow source). This is the fails-before/passes-after test.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_desktop_observer, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([minimized_main_windows(), restored_main_windows()])

    with pytest.raises(CredentialUnknownError) as excinfo:
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator),
        )

    assert excinfo.value.pid == 111
    assert excinfo.value.reason == harvested_minimized_reason()
    assert enumerator.calls >= 2, "the loop must have kept polling after the owner was restored"


def test_poll_loop_without_unknown_returns_false(monkeypatch) -> None:
    """No-false-positive guard: a run that is healthy throughout returns False, never latching UNKNOWN.

    A never-minimized sequence keeps the loop on its normal deadline path (``False`` -> the caller's
    ordinary ``TimeoutError``), proving the latch cannot fire without a real UNKNOWN observation.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_desktop_observer, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([restored_main_windows()])

    result = join_with_credential_poll(
        worker,
        pid=111,
        total_timeout=1.0,
        heartbeat_seconds=1000.0,
        poll_seconds=0.1,
        detector=_real_detector(enumerator),
    )

    assert result is False
    assert enumerator.calls >= 1


def test_poll_loop_raises_desktop_gone_when_process_dead(monkeypatch) -> None:
    """#158 PRIMARY (fails-before/passes-after): a dead Desktop is TERMINAL, raised immediately.

    Drives the REAL ``join_with_credential_poll`` and REAL ``inspect_desktop_observer`` through an
    injected empty enumerator plus a liveness check that reports the process dead. Zero windows on a
    gone PID is definitive - Desktop exited/crashed and the source was never contacted - so the loop
    must raise :class:`DesktopGoneError` on the FIRST poll, never run out the clock and never return
    ``False`` (the bare-timeout path the parent blames on a slow source).

    Before the #158 detector guard + poll-loop raise, the empty enumeration fell through to ``none`` and
    this loop returned ``False`` (``pytest.raises`` failure: DID NOT RAISE). This is the controlled test
    that fails before the change and passes after it.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_desktop_observer, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([[]])

    with pytest.raises(DesktopGoneError) as excinfo:
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator, process_is_alive=lambda _pid: False),
        )

    assert excinfo.value.pid == 111
    assert excinfo.value.reason == harvested_desktop_gone_reason()
    assert enumerator.calls == 1, "process_gone is terminal - the loop must raise on the first poll, not wait"


def test_poll_loop_latches_desktop_unready_when_process_alive_zero_windows(monkeypatch) -> None:
    """#158: an alive-but-window-less Desktop latches as a local readiness failure.

    Empty enumeration with the process still alive is a startup/wedged state, not a crash: the loop must
    keep waiting (a startup that grows a window and finishes is never overridden), latch the UNKNOWN
    observation, and raise :class:`DesktopUnreadyError` at the deadline - never ``process_gone``,
    credential UNKNOWN, or a bare timeout.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_desktop_observer, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([[]])

    with pytest.raises(DesktopUnreadyError) as excinfo:
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator, process_is_alive=lambda _pid: True),
        )

    assert excinfo.value.pid == 111
    assert excinfo.value.reason == harvested_zero_window_alive_reason()
    assert enumerator.calls >= 2, "alive zero-window is latch-and-wait, not terminal"


def test_refresh_and_save_wires_credential_unknown_to_exit_3(monkeypatch, capsys) -> None:
    """#154 wiring: a latched CredentialUnknownError must surface as the DISTINCT exit 3.

    Exit 3 (UNKNOWN) must never collapse into exit 1 (blocked) or a healthy path - the four exit
    states are load-bearing for the parent's taxonomy (probe_desktop_query uses exit 3 for UNKNOWN,
    exit 1 for blocked). The reason is harvested from the real detector, and the verdict line is the
    real emitter's, so this pins the whole raise -> emit -> exit-code path with no hand-written text.
    """
    reason = harvested_minimized_reason()

    def _raise_unknown(*_args, **_kwargs):
        raise CredentialUnknownError(111, reason)

    monkeypatch.setattr(refresh_pbip_model, "refresh", _raise_unknown)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--tables", "Orders"])

    exit_code = refresh_pbip_model._refresh_and_save(111, 5000, None, args)
    out = capsys.readouterr().out

    assert exit_code == 3, "CREDENTIAL_UNKNOWN must be exit 3, distinct from blocked's exit 1"
    assert "REFRESH: CREDENTIAL_UNKNOWN pid=111" in out
    assert "Dialog evidence stayed indeterminate" in out


def test_refresh_and_save_wires_a_dialog_finding_to_exit_3(monkeypatch, capsys) -> None:
    """#376 wiring: a DialogFoundError must surface at exit 3, never in the exit-1 hard-stop band.

    Exit 1 is reserved for a matched credential signature. This module cannot establish that a dialog
    BLOCKS anything, so nothing it observes about one may enter that band - and ``probe_live_source``
    maps the exit-1 credential family to "you may NOT build; a person must sign in". The finding is a
    real classification and the verdict line is the real emitter's, so this pins raise -> emit -> exit
    code with no hand-written text.
    """

    def _raise_dialog(*_args, **_kwargs):
        raise DialogFoundError(111, dialog_finding())

    monkeypatch.setattr(refresh_pbip_model, "refresh", _raise_dialog)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--tables", "Orders"])

    exit_code = refresh_pbip_model._refresh_and_save(111, 5000, None, args)
    out = capsys.readouterr().out

    assert exit_code == 3, "a dialog finding must not reuse the credential hard stop's exit 1"
    assert "REFRESH: DIALOG_UNREADABLE pid=111" in out
    assert "BLOCKED_BY_DIALOG" not in out


def test_refresh_main_reports_a_t0_dialog_at_exit_3_before_any_mutation(monkeypatch, tmp_path: Path, capsys) -> None:
    """#376: the MUTATING CLI stops at t=0 for a dialog it could not read - at exit 3, and before XMLA.

    Two properties in one, and both matter: it must not stack a refresh on somebody else's dialog
    (``discover_port`` explodes if it gets that far), and it must not call it a credential wall.
    """
    model_folder(tmp_path, "MyMigration")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: dialog_state())
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert out.startswith("REFRESH: DIALOG_UNREADABLE")


def test_refresh_and_save_wires_desktop_gone_to_exit_2(monkeypatch, capsys) -> None:
    """#158 wiring: a DesktopGoneError must surface as the ERROR-family exit 2, distinct from UNKNOWN.

    A dead Desktop is a local-environment failure, not an indeterminate credential state: it must NOT
    reuse exit 3 (CREDENTIAL_UNKNOWN) nor exit 1 (blocked). Exit 2 is the ERROR family the parent maps
    to ``ERROR`` (source not implicated). The reason is harvested from the real detector and the verdict
    line is the real emitter's, so this pins the whole raise -> emit -> exit-code path with no
    hand-written text.
    """
    reason = harvested_desktop_gone_reason()

    def _raise_gone(*_args, **_kwargs):
        raise DesktopGoneError(111, reason)

    monkeypatch.setattr(refresh_pbip_model, "refresh", _raise_gone)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--tables", "Orders"])

    exit_code = refresh_pbip_model._refresh_and_save(111, 5000, None, args)
    out = capsys.readouterr().out

    assert exit_code == 2, "DESKTOP_GONE must be the ERROR-family exit 2, distinct from UNKNOWN's exit 3"
    assert "REFRESH: DESKTOP_GONE pid=111" in out
    assert reason in out


@pytest.mark.timing
def test_refresh_main_returns_credential_missing_fast_at_t0(monkeypatch, tmp_path: Path, capsys) -> None:
    """refresh_pbip_model.main stops before port discovery, identity checks, refresh, or row counts."""
    model_folder(tmp_path, "MyMigration")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: modal_state())
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    started = time.monotonic()
    exit_code = refresh_pbip_model.main(["--pid", "111"])
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out

    assert exit_code == 1
    assert elapsed < 0.5, f"t=0 credential verdict waited {elapsed:.3f}s"
    assert "REFRESH: CREDENTIAL_MISSING" in out
    assert "Sql.Database(server)" not in out, "provider/source text stays private"


def test_refresh_main_returns_desktop_gone_before_port_discovery(monkeypatch, tmp_path: Path, capsys) -> None:
    """#158: the real mutating CLI entry point must terminate on a dead Desktop."""
    model_folder(tmp_path, "MyMigration")
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid: Observation(process_gone=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.startswith("REFRESH: DESKTOP_GONE")
    assert reason in out


def test_refresh_main_returns_desktop_unready_from_the_real_detector(monkeypatch, tmp_path: Path, capsys) -> None:
    """#158: an alive-but-window-less Desktop terminates the mutating CLI, on EVERY platform.

    Composed from the real `inspect_desktop_observer` with injected Win32 primitives rather than a
    hand-built `Observation`, so the detector's own zero-window/alive branch and the CLI's
    branch on it are proven as one chain - and proven identically on Linux CI and on Windows, which the
    un-stubbed `_credential_state` cannot do (its `os.name` guard returns a healthy state off Windows).
    """
    model_folder(tmp_path, "MyMigration")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", real_detector_for_zero_windows(alive=True))
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2, "DESKTOP_UNREADY is a local ERROR-family exit 2, not UNKNOWN's exit 3"
    assert out.startswith("REFRESH: DESKTOP_UNREADY")
    assert harvested_zero_window_alive_reason() in out
    assert "minimiz" not in out.lower(), "a window-less Desktop must never be reported as minimized"


def test_refresh_main_latches_unknown_from_its_own_precheck(monkeypatch, tmp_path: Path, capsys) -> None:
    """#154: main's first UNKNOWN survives port and identity work before refresh starts."""
    model_folder(tmp_path, "MyMigration")
    reason = harvested_minimized_reason()
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid: Observation(unknown_reason=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda _port, _cache: True)

    def assert_initial_state(_port, _tables, _timeout, *, desktop_pid, source_hint, initial_state=None):
        del desktop_pid, source_hint
        assert initial_state is not None
        assert initial_state.unknown_reason == reason
        raise CredentialUnknownError(111, initial_state.unknown_reason)

    monkeypatch.setattr(refresh_pbip_model, "refresh", assert_initial_state)

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert "REFRESH: CREDENTIAL_UNKNOWN" in out
    assert reason in out


@pytest.mark.timing
def test_probe_query_returns_credential_missing_fast_at_t0(monkeypatch, capsys) -> None:
    """probe_desktop_query.main stops before port discovery or DAX when the modal is already open."""
    monkeypatch.setattr(probe_desktop_query, "_credential_state", lambda _pid: modal_state())
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    started = time.monotonic()
    exit_code = probe_desktop_query.main(["--pid", "111"])
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out

    assert exit_code == 1
    assert elapsed < 0.5, f"t=0 credential verdict waited {elapsed:.3f}s"
    assert "PREFLIGHT: CREDENTIAL_MISSING" in out


def test_probe_query_main_returns_desktop_gone_before_port_discovery(monkeypatch, capsys) -> None:
    """#158: the real read-only CLI entry point must terminate on a dead Desktop."""
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda _pid: Observation(process_gone=reason),
    )
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.startswith("PREFLIGHT: DESKTOP_GONE")
    assert reason in out


def test_probe_query_main_returns_desktop_unready_from_the_real_detector(monkeypatch, capsys) -> None:
    """#158: the read-only CLI terminates on an alive-but-window-less Desktop, on EVERY platform.

    The read-only twin of the refresh test above, and composed the same way: the real detector with
    injected Win32 primitives, so Linux CI exercises the branch Windows actually takes.
    """
    monkeypatch.setattr(probe_desktop_query, "_credential_state", real_detector_for_zero_windows(alive=True))
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.startswith("PREFLIGHT: DESKTOP_UNREADY")
    assert harvested_zero_window_alive_reason() in out


def test_the_two_entry_points_get_an_explicit_desktop_state_baseline() -> None:
    """`conftest`'s autouse baseline must really be in force on BOTH entry points.

    17 tests drive `main(["--pid", "111", ...])` without naming a Desktop state. If the baseline stops
    applying they fall back to the production `_credential_state`, whose result is decided by the host
    OS: a healthy `Observation()` on Linux, and a real `process_gone` detection on Windows
    (pid 111 does not exist). Measured 2026-08-15, that split is exactly how 17 Windows-only failures
    passed green on `ubuntu-latest`.

    Asserting on the RETURNED VALUE could not catch it - off Windows the un-stubbed function also
    returns a bare `Observation()`, so a value check is a test that cannot fail on the one
    platform CI runs. The marker on the stub is checkable on every platform.
    """
    for module in (refresh_pbip_model, probe_desktop_query):
        assert getattr(module._credential_state, "is_test_baseline", False), (
            f"{module.__name__}._credential_state is not conftest's baseline stub, so un-stubbed tests "
            "would silently exercise different code on Windows and on Linux"
        )


def test_probe_query_stops_at_exit_3_for_a_dialog_it_could_not_read(monkeypatch, capsys) -> None:
    """#376: an unreadable dialog stops the GATE OF RECORD at exit 3, never in the exit-1 hard-stop band.

    Master emitted ``PREFLIGHT: BLOCKED_BY_DIALOG`` at exit 1 from a SIZE-ONLY test, which
    ``probe_live_source`` maps to ``NO_CREDENTIAL`` ("you may NOT build; a person must sign in"). Exit 3
    means "could not probe" and makes no claim about the source at all.
    """
    monkeypatch.setattr(probe_desktop_query, "_credential_state", lambda _pid, **_kw: dialog_state())
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3, "a dialog we could not read must not enter the exit-1 hard-stop band"
    assert out.startswith("PREFLIGHT: DIALOG_UNREADABLE")
    assert "BLOCKED_BY_DIALOG" not in out


def test_probe_query_returns_unknown_for_minimized_owner(monkeypatch, capsys) -> None:
    """A minimized owner is indeterminate, not evidence that no credential dialog exists."""
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda _pid, **_kwargs: Observation(
            unknown_reason="Power BI Desktop owner window is minimized",
            complete=False,
            reason="minimized",
        ),
    )
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert out.startswith("PREFLIGHT: DIALOG_UNREADABLE")
    assert "minimized" in out


def test_probe_query_poll_catches_late_modal(monkeypatch, capsys) -> None:
    """probe_desktop_query catches a modal that appears after the read-only query starts."""
    started = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    def fake_probe(_port: int, _tables: list[str] | None, emit=print) -> int:
        del emit
        started.set()
        release.wait(timeout=600)
        return 0

    def late_modal(_pid: int):
        calls["count"] += 1
        return modal() if started.is_set() and calls["count"] >= 2 else None

    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda pid, **_kw: Observation(modal=late_modal(pid)),
    )
    monkeypatch.setattr(probe_desktop_query, "PREFLIGHT_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", fake_probe)

    try:
        exit_code = probe_desktop_query.main(["--pid", "111", "--canaries", "Orders"])
    finally:
        release.set()
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "PREFLIGHT: CREDENTIAL_MISSING" in out
    assert calls["count"] >= 2


def test_probe_query_worker_cannot_print_after_credential_verdict(monkeypatch, capsys) -> None:
    """Reviewer's race: late worker DATA_OK text must not print after CREDENTIAL_MISSING."""
    started = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    def fake_probe(_port: int, _tables: list[str] | None, emit=print) -> int:
        started.set()
        release.wait(timeout=600)
        emit("PREFLIGHT: DATA_OK_FROM_WORKER_AFTER_RELEASE")
        return 0

    def late_modal(_pid: int):
        calls["count"] += 1
        return modal() if started.is_set() and calls["count"] >= 2 else None

    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda pid, **_kw: Observation(modal=late_modal(pid)),
    )
    monkeypatch.setattr(probe_desktop_query, "PREFLIGHT_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", fake_probe)

    exit_code = probe_desktop_query.main(["--pid", "111", "--canaries", "Orders"])
    first_out = capsys.readouterr().out
    release.set()
    time.sleep(0.1)
    later_out = capsys.readouterr().out

    assert exit_code == 1
    assert first_out.splitlines() == [first_out.splitlines()[-1]]
    assert first_out.startswith("PREFLIGHT: CREDENTIAL_MISSING")
    assert "DATA_OK_FROM_WORKER_AFTER_RELEASE" not in first_out
    assert later_out == ""


# ==================================================================================================
# _desktop_observer.classify_dialog - the PYTHON detector's own dialog classifiers (issue #376)
# ==================================================================================================
#
# The Python fast path had the same defect the PowerShell arbiter shed in #367, on a more dangerous
# route: `inspect_desktop_observer` returned the FIRST visible non-main window >= 100x100 as a
# `blocking_dialog`, and both callers mapped that to `BLOCKED_BY_DIALOG` at exit 1 - which
# `probe_live_source` classifies `NO_CREDENTIAL`. It feeds `probe_desktop_query.py`, the gate of
# record, so a Power BI Refresh progress dialog could halt a migration and send an operator to a
# sign-in screen that was never on show.
#
# These drive the REAL classifier against synthesised windows: no Desktop, no Win32, every platform.


def test_the_python_detector_cannot_emit_blocked_by_dialog_again(capsys) -> None:
    """Anti-regression: the size-only rule and its verdict token must not come back (issue #376).

    Checked BEHAVIOURALLY, not by grepping the source - the token is named in several docstrings on
    purpose, because a reader who meets it in an old transcript needs to find out why it went. So this
    drives every classifiable kind through both real emitters and asserts none of them prints it, and
    pins that the size-only helper and its field are gone from the module's namespace.
    """
    windows = {
        "unreadable": DesktopWindow("", "Cls", 702, 355, ()),
        "unrecognized": DesktopWindow("Save?", "Cls", 702, 355, ("Save?", "Discard")),
        "needs-human": DesktopWindow("", "Cls", 702, 355, ("Permission is required to run this native query",)),
        "benign": progress_dialog_window(),
        "benign-title-only": DesktopWindow("Refresh", "Cls", 702, 355, ("Refresh",)),
        "mixed-content": DesktopWindow(
            "Refresh", "Cls", 702, 355, ("Refresh", "Evaluating...", "This will discard all unsaved work")
        ),
    }
    for window in windows.values():
        finding = classify_dialog(window)
        refresh_pbip_model._emit_dialog_finding(111, finding)
        probe_desktop_query._emit_dialog_finding(111, finding)
    out = capsys.readouterr().out

    assert out.strip(), "the emitters printed nothing at all, so this test proved nothing"
    assert "BLOCKED_BY_DIALOG" not in out, "an emitter still prints the size-only hard-stop token"
    assert "BLOCKED_BY_DIALOG" not in set(_desktop_observer.DIALOG_KIND_VERDICTS.values())
    assert not hasattr(_desktop_observer, "blocking_dialog_candidates"), "the size-only detector came back"
    assert "blocking_dialog" not in Observation.__dataclass_fields__, "the size-only field came back"


# --------------------------------------------------------------------------------------------------
# Blind-review findings on PR #400. Each one is a case where "we could not establish it" was still
# collapsing into the clean bucket - the same defect class issue #376 exists to remove, found again on
# the code that removed it. Every test below fails on the PR-#400 build and passes on this one.
# --------------------------------------------------------------------------------------------------


def test_an_owned_dialog_sharing_its_owners_class_is_still_a_credential_wall() -> None:
    """Round 3, finding 2: a class PREFIX names a WinForms family, not one HWND.

    A native WinForms experiment showed an owner and its owned ``FixedDialog`` both reporting the exact
    class ``WindowsForms10.Window.8.app.0.2b2196a_r3_ad1``. The previous ``is_desktop_main_window``
    predicate treated any such window as the application, so this shape returned **no modal, no dialog
    finding, no unknown state** - a real credential dialog removed from the prepass AND from
    classification by the very fix that was meant to stop a report title fabricating one.
    """
    shared_class = "WindowsForms10.Window.8.app.0.2b2196a_r3_ad1"
    frame = DesktopWindow("sample - Power BI Desktop", shared_class, 2011, 1298, ("sample",), hwnd=MAIN_HWND)
    dialog = owned_dialog(("Enter your credentials",), class_name=shared_class)

    state = inspect_desktop_observer(111, lambda _pid: [frame, dialog])

    assert state.dialog is not None, "an owned dialog sharing its owner's class is still a dialog"
    assert state.dialog.window is dialog
    assert state.dialog.window.class_name == shared_class


def test_an_owned_dialog_sharing_its_owners_class_is_classified_when_unreadable() -> None:
    """The same shape without a signature match must still surface, not disappear."""
    shared_class = "WindowsForms10.Window.8.app.0.2b2196a_r3_ad1"
    frame = DesktopWindow("sample", shared_class, 2011, 1298, ("sample",), hwnd=MAIN_HWND)
    dialog = owned_dialog(class_name=shared_class)

    state = inspect_desktop_observer(111, lambda _pid: [frame, dialog])

    assert state.modal is None
    assert state.dialog is not None
    assert state.dialog.verdict == "DIALOG_NEEDS_HUMAN"


@pytest.mark.parametrize(
    ("texts", "verdict"),
    [
        ((), "DIALOG_NEEDS_HUMAN"),
        (("Password:",), "DIALOG_NEEDS_HUMAN"),
        (("Sign in",), "DIALOG_NEEDS_HUMAN"),
        (("Continue?",), "DIALOG_NEEDS_HUMAN"),
    ],
)
def test_the_aad_host_is_classified_by_modality_not_by_its_name(texts, verdict: str) -> None:
    """Round 3, finding 1: a NAME allowlist hid the AAD sign-in host whatever it displayed.

    ``Internet Explorer_Hidden`` was excluded from classification unconditionally, so keeping it in the
    credential prepass only ever rescued EXACT signature matches. Measured on the round-2 build: a
    visible 900x700 host reading ``Password:`` / ``Sign in`` / ``Continue?`` - or nothing at all -
    returned ``modal=None, dialog=None, unknown=None``.

    There is no name allowlist any more. This window is classified because it is OWNED and its owner is
    not proven enabled, which is what "blocking" means; its class is not consulted at all.
    """
    host = owned_dialog(texts, class_name="Internet Explorer_Hidden", width=900, height=700)

    state = inspect_desktop_observer(111, lambda _pid: [main_window(), host])

    assert state.dialog is not None, "the AAD host must never collapse into the healthy state"
    assert state.dialog.verdict == verdict


def test_an_enabled_owner_is_the_only_thing_that_exonerates_a_dialog() -> None:
    """Modality is a ONE-WAY test, and it is the only suppression mechanism left.

    An ENABLED owner proves this window is blocking nothing, because a modal disables its owner. A
    DISABLED owner proves nothing either way - Power BI's refresh dialog disables the owner too - and
    ``None`` (no owner) means the test did not apply, which is not the same as passing it.
    """
    exonerated = owned_dialog(owner_enabled=True)
    disabled_owner = owned_dialog(owner_enabled=False)
    no_owner_test = DesktopWindow("", "Cls", 702, 355, (), hwnd=0x40004, owner_hwnd=MAIN_HWND, owner_enabled=None)

    assert _desktop_observer.is_proven_non_blocking(exonerated)
    assert not _desktop_observer.is_proven_non_blocking(disabled_owner)
    assert not _desktop_observer.is_proven_non_blocking(no_owner_test)

    kept = _desktop_observer.dialog_candidates([main_window(), exonerated, disabled_owner, no_owner_test])

    assert exonerated not in kept, "an enabled owner is positive proof this window blocks nothing"
    assert disabled_owner in kept
    assert no_owner_test in kept, "'the test did not apply' is not 'the test passed'"


def test_no_proxy_for_blocking_survives_in_the_candidate_rule() -> None:
    """Anti-regression: size, class prefix and a name allowlist each hid a real blocker. None returns.

    Three review rounds killed three correlates. This asserts the module has no surface for a fourth,
    and that the only exclusions left are the modality ones.

    ⚠️ **Asserted BEHAVIOURALLY, not by reading the source (#400 review round 5).** This used to grep
    ``inspect.getsource(dialog_candidates)`` for the tokens ``class_name`` and ``width``. That caught
    an inline mutation and nothing else: a helper called from that function could consult either
    without those tokens appearing in it, and the test would still pass. So instead the matrix below
    feeds windows that differ ONLY in class and size and requires candidacy to be identical - which no
    proxy anywhere behind :func:`dialog_candidates` can satisfy.
    """
    for gone in ("MIN_DIALOG_WIDTH", "MIN_DIALOG_HEIGHT", "HELPER_WINDOW_CLASSES", "is_desktop_main_window"):
        assert not hasattr(_desktop_observer, gone), f"{gone} is a proxy for blocking and must stay deleted"

    classes = (
        DESKTOP_MAIN_CLASS_PREFIX + ".app.0.33c0d9d",  # the frame's own WinForms family (round 2)
        "Internet Explorer_Hidden",  # the AAD sign-in host's class (round 3)
        "#32770",
        "tooltips_class32",
    )
    # Every size is > 0 in both axes: `renders_nothing` is a MODALITY rule (unowned AND no pixels), not
    # a size test, so a genuinely zero-area unowned window is excluded on purpose and does not belong
    # in a matrix asserting that size is never consulted.
    sizes = ((2011, 1298), (900, 700), (80, 60), (1, 1))
    frame = main_window()

    verdicts: dict[tuple[bool, str, tuple[int, int]], bool] = {}
    for owned in (True, False):
        for class_name in classes:
            for width, height in sizes:
                window = DesktopWindow(
                    "",
                    class_name,
                    width,
                    height,
                    ("Enter your credentials",),
                    hwnd=0x80008,
                    owner_hwnd=MAIN_HWND if owned else 0,
                    owner_enabled=False if owned else None,
                )
                kept = _desktop_observer.dialog_candidates([frame, window], frame=frame)
                verdicts[(owned, class_name, (width, height))] = window in kept

    assert all(verdicts.values()), (
        "candidacy must not vary with class or size; these combinations were dropped: "
        f"{sorted(key for key, kept in verdicts.items() if not kept)}"
    )


def test_the_frame_is_identified_by_ownership_before_any_convention() -> None:
    """`main_frame` names the frame from ownership evidence - and only when it is UNAMBIGUOUS.

    Ownership names the frame outright when a dialog is up, which is the case where getting it wrong
    removes a real blocker: the credential dialog's chain roots at the frame no matter where Z-order
    puts it in the enumeration.

    ⚠️ The decoy assertion INVERTED in round 5, and that is the fix rather than a regression. A second
    unowned window that renders pixels is itself a possible frame - a "decoy" and an unowned credential
    host are structurally the same window - so identity is AMBIGUOUS and nothing is excluded. Believing
    the ownership-derived root instead is exactly what crowned an unowned credential host that owned
    one tooltip.
    """
    frame = main_window()
    dialog = owned_dialog(("Enter your credentials",))
    decoy = DesktopWindow("decoy", "Cls", 300, 200, ("decoy",), hwnd=0x60006)

    # Ownership evidence wins even when the dialog is enumerated FIRST (Z-order puts a modal on top).
    assert _desktop_observer.main_frame([dialog, frame]) is frame
    assert _desktop_observer.main_frame([frame, dialog]) is frame
    # A second rendering unowned window is a second possible frame, so identity fails closed.
    assert _desktop_observer.main_frame([dialog, decoy, frame]) is None
    assert _desktop_observer.main_frame([decoy, frame, dialog]) is None
    # With nothing owned, one rendering unowned window is still unambiguous.
    lone = DesktopWindow("only", "Cls", 800, 600, ("only",), hwnd=0x50005)
    assert _desktop_observer.main_frame([lone]) is lone
    assert _desktop_observer.main_frame([]) is None


def test_an_unowned_window_that_renders_pixels_is_still_classified() -> None:
    """`renders_nothing` needs BOTH conjuncts: no owner AND no pixels. Neither alone suppresses.

    An unowned 900x700 window displays something to a human even though it disables nobody. Dropping
    the area conjunct would silently exclude every unowned window - including the AAD host in its
    unowned form - and would also empty ``main_frame``'s fallback candidate set.
    """
    frame = main_window()
    unowned_visible = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, ("Password:",), hwnd=0x70007)

    assert not _desktop_observer.renders_nothing(unowned_visible)

    kept = _desktop_observer.dialog_candidates([frame, unowned_visible], frame=frame)
    assert unowned_visible in kept, "an unowned window with pixels must not vanish into healthy"

    state = inspect_desktop_observer(111, lambda _pid: [frame, unowned_visible])

    assert state.dialog is not None
    assert state.dialog.verdict == "DIALOG_NEEDS_HUMAN"


def test_ownership_is_followed_transitively_to_the_unowned_root() -> None:
    """Round 4: "first owner" is itself a proxy for "the root", and it hid a credential modal.

    An owned window can own another popup. With a Z-order of ``tooltip -> credential dialog -> frame``
    the first owner reached is the CREDENTIAL DIALOG, and whatever ``main_frame`` picks is excluded
    from the prepass AND from classification - so the modal disappeared. Measured on the round-3 build:
    the frame came back as the ``#32770`` credential dialog and no modal was reported.
    """
    frame_hwnd, cred_hwnd, tip_hwnd = 0xF001, 0xC002, 0x7003
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=frame_hwnd)
    cred = DesktopWindow(
        "", "#32770", 702, 355, ("Enter your credentials",), hwnd=cred_hwnd, owner_hwnd=frame_hwnd, owner_enabled=False
    )
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=cred_hwnd, owner_enabled=True
    )

    # Z-order puts the tooltip first, so a one-edge walk lands on the credential dialog.
    assert _desktop_observer.main_frame([tip, cred, frame]) is frame
    state = inspect_desktop_observer(111, lambda _pid: [tip, cred, frame])

    assert state.dialog is not None, "a nested owner chain must not hide the credential dialog"
    assert state.dialog.window is cred


def test_a_nested_chain_still_finds_the_modal_while_our_own_refresh_is_in_flight() -> None:
    """Round 4's third reproduction: the frame is busy with a benign refresh, so nothing else reports.

    With the frame misidentified this returned no modal, no dialog finding and no unknown state - the
    exact "unassessable input in the clean bucket" shape, at depth two.
    """
    frame_hwnd, cred_hwnd, tip_hwnd = 0xF001, 0xC002, 0x7003
    frame = DesktopWindow(
        "sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample", "Refresh", "Evaluating..."), hwnd=frame_hwnd
    )
    cred = DesktopWindow(
        "", "#32770", 702, 355, ("Enter your credentials",), hwnd=cred_hwnd, owner_hwnd=frame_hwnd, owner_enabled=False
    )
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=cred_hwnd, owner_enabled=True
    )

    state = inspect_desktop_observer(111, lambda _pid: [tip, cred, frame])

    assert state.dialog is not None
    assert state.dialog.window is cred


def test_a_topmost_unowned_dialog_cannot_present_itself_as_the_application() -> None:
    """Round 4's second reproduction: the fallback must not crown the FIRST of several unowned windows.

    An unowned ``Internet Explorer_Hidden`` enumerated ahead of the real frame, reading
    ``Enter your credentials``, was selected as the frame and therefore skipped by the prepass. With no
    ownership evidence to settle it, identity is AMBIGUOUS and the rule fails closed: ``main_frame``
    returns ``None``, nothing is excluded, and the prompt is found.
    """
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=0xF001)
    aad = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, hwnd=0x9001, ie_hosted=True)

    assert _desktop_observer.main_frame([aad, frame]) is None, "ambiguous identity must fail closed"

    state = inspect_desktop_observer(111, lambda _pid: [aad, frame])

    assert state.dialog is not None, "a topmost unowned dialog must not skip the prepass"
    assert state.dialog.window is aad


def test_an_unowned_credential_host_owning_a_tooltip_is_never_crowned_the_frame() -> None:
    """Round 5: collecting roots ONLY through ownership chains made a credential host the application.

    The reviewer's construction, and the fourth distinct topology to defeat frame identity. A real
    unowned frame shows ``Refresh`` / ``Evaluating...``; an unowned host shows
    ``Enter your credentials``; an ENABLED tooltip is owned by that host. The tooltip is the only owned
    window, so the only ownership-derived root is the CREDENTIAL HOST - which was then excluded from
    the prepass and from classification, while the real frame's progress text was suppressed in flight.

    Measured on the round-4 build, exactly the shape this module exists to prevent::

        main_frame selected: the credential host
        inspect_desktop_observer -> modal=None dialog=None unknown_reason=None
                                    desktop_unready=None process_gone=None
    """
    frame_hwnd, cred_hwnd, tip_hwnd = 0xF001, 0x9001, 0x7003
    frame = DesktopWindow(
        "", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("Refresh", "Evaluating..."), hwnd=frame_hwnd
    )
    cred = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, hwnd=cred_hwnd, ie_hosted=True)
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=cred_hwnd, owner_enabled=True
    )

    assert _desktop_observer.main_frame([tip, cred, frame]) is None, "two rendering unowned windows are ambiguous"
    kept = _desktop_observer.dialog_candidates([tip, cred, frame])
    assert cred in kept and frame in kept, "an ambiguous frame must exclude neither of them"

    for in_flight in (False, True):
        state = inspect_desktop_observer(111, lambda _pid: [tip, cred, frame])
        assert state.dialog is not None, f"the prompt vanished with operation_in_flight={in_flight}"
        assert state.dialog.window is cred


def test_an_unowned_host_with_unrecognised_text_still_surfaces_beside_our_own_refresh() -> None:
    """The same topology without a signature hit must be LOUD, not clean.

    Round 5's danger is not only the missed signature: with the credential host crowned as the frame,
    the only window left to classify was the real frame, whose benign progress text is suppressed while
    our own operation is in flight. Every unrecognised window in that shape therefore collapsed into
    the healthy state. This is the same construction with text nobody can account for.
    """
    frame_hwnd, host_hwnd, tip_hwnd = 0xF001, 0x9001, 0x7003
    frame = DesktopWindow(
        "", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("Refresh", "Evaluating..."), hwnd=frame_hwnd
    )
    host = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, hwnd=host_hwnd, ie_hosted=True)
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=host_hwnd, owner_enabled=True
    )

    state = inspect_desktop_observer(111, lambda _pid: [tip, host, frame])

    assert state.modal is None
    assert state.dialog is not None, "an unaccounted window must never collapse into the healthy state"
    assert state.dialog.verdict == "DIALOG_NEEDS_HUMAN"
    assert state.dialog.window is host


def test_ambiguous_frame_identity_excludes_nothing() -> None:
    """Failing closed means EXCLUDING NOTHING - the cost is a loud exit 3, never a silent clear.

    Both halves matter, and the second is round 5's: ambiguity has to survive the PRESENCE of owned
    windows too. An ownership-derived root is not better evidence than a window sitting there rendering
    pixels - believing it was the whole defect - so a tooltip owned by either candidate must not
    promote its owner into the application.
    """
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=0xF001)
    other = DesktopWindow("second", "Cls", 800, 600, ("second",), hwnd=0x9001)

    assert _desktop_observer.main_frame([frame, other]) is None
    kept = _desktop_observer.dialog_candidates([frame, other])

    assert frame in kept and other in kept, "an ambiguous frame must not exclude a window"

    for owner_hwnd in (0xF001, 0x9001):
        tip = DesktopWindow("", "tooltips_class32", 120, 24, (), hwnd=0x7003, owner_hwnd=owner_hwnd, owner_enabled=True)
        windows = [tip, frame, other]
        assert _desktop_observer.main_frame(windows) is None, f"an owned tooltip must not crown hwnd {owner_hwnd:#x}"
        kept = _desktop_observer.dialog_candidates(windows)
        assert frame in kept and other in kept, "neither possible frame may be excluded"


def test_an_unresolvable_owner_chain_fails_closed() -> None:
    """An owner this enumeration never saw, or a cycle, leaves the root UNKNOWN - so guess nothing."""
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=0xF001)
    orphan = DesktopWindow("", "#32770", 702, 355, (), hwnd=0xC002, owner_hwnd=0xDEAD, owner_enabled=False)
    left = DesktopWindow("", "Cls", 400, 300, (), hwnd=0xA001, owner_hwnd=0xA002, owner_enabled=False)
    right = DesktopWindow("", "Cls", 400, 300, (), hwnd=0xA002, owner_hwnd=0xA001, owner_enabled=False)

    assert _desktop_observer.main_frame([frame, orphan]) is None, "an unseen owner leaves the root unknown"
    assert _desktop_observer.main_frame([left, right]) is None, "a cycle leaves the root unknown"


@pytest.mark.serial
@pytest.mark.gui
@pytest.mark.skipif(sys.platform != "win32", reason="creates real Win32 windows")
def test_the_win32_harvest_reads_real_ownership_and_owner_enabled_state() -> None:
    """The modality facts must come from Win32, not from a dataclass default (#400 review round 3).

    Every other test here feeds synthesised windows, so none of them can tell whether
    ``GetWindow(GW_OWNER)`` and ``IsWindowEnabled(owner)`` are actually wired into the harvest - a
    mutation that hard-codes ``owner_hwnd = 0`` or ``owner_enabled = True`` passes all of them. This
    builds the reviewer's own reproduction natively: a real ``WS_VISIBLE`` **owned 0x0** popup whose
    owner is disabled, which is a genuinely blocking shape, and asserts the harvest sees it and the
    candidate rule keeps it.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class _WndClass(ctypes.Structure):
        """Minimal ``WNDCLASSW``."""

        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]  # fmt: skip
    # Signatures set explicitly for the same reason `_configure_user32` does it in production: a
    # default ctypes restype is a 32-bit `c_int`, so an untyped `GetModuleHandleW` TRUNCATES the
    # 64-bit HINSTANCE and `RegisterClassW` then faults on the garbage. That access violation showed
    # up as a `faulthandler` dump on every run of this test while it still reported a pass.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.DestroyWindow.argtypes = [wintypes.HWND]

    # `DefWindowProcW` is used DIRECTLY as the window procedure, cast to the callback type rather than
    # wrapped in a Python lambda. Windows calls a wndproc synchronously from inside `CreateWindowEx`
    # and `DestroyWindow`; keeping Python off the message path removes both the marshalling risk and
    # the need for a message pump.
    klass = _WndClass()
    klass.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, wndproc_type)
    klass.hInstance = kernel32.GetModuleHandleW(None)
    klass.lpszClassName = f"T2PModalityProbe{os.getpid()}"
    assert user32.RegisterClassW(ctypes.byref(klass)), f"RegisterClassW failed: {ctypes.get_last_error()}"

    ws_overlappedwindow, ws_visible, ws_popup = 0x00CF0000, 0x10000000, 0x80000000
    frame_hwnd = user32.CreateWindowExW(
        0, klass.lpszClassName, "t2p frame", ws_overlappedwindow | ws_visible,
        10, 10, 400, 300, None, None, klass.hInstance, None,
    )  # fmt: skip
    owned_hwnd = user32.CreateWindowExW(
        0, klass.lpszClassName, "", ws_popup | ws_visible, 0, 0, 0, 0, frame_hwnd, None, klass.hInstance, None
    )
    try:
        assert frame_hwnd and owned_hwnd, "could not create the native probe windows"
        user32.EnableWindow(frame_hwnd, False)
        time.sleep(0.2)

        harvested, _visited = _pump_native_harvest(os.getpid())
        by_hwnd = {window.hwnd: window for window in harvested}
        frame = by_hwnd.get(frame_hwnd)
        owned = by_hwnd.get(owned_hwnd)

        assert frame is not None and owned is not None, "the harvest missed the native probe windows"
        assert frame.owner_hwnd == 0, "the frame is unowned"
        assert owned.owner_hwnd == frame_hwnd, "ownership must come from GetWindow(GW_OWNER)"
        assert owned.owner_enabled is False, "owner_enabled must come from IsWindowEnabled(owner)"
        assert (owned.width, owned.height) == (0, 0), "the probe window really is 0x0"
        assert not _desktop_observer.renders_nothing(owned), "it HAS an owner, so zero area cannot suppress"
        assert not _desktop_observer.is_proven_non_blocking(owned), "a disabled owner is not an exoneration"
        assert owned in _desktop_observer.dialog_candidates(harvested, frame=frame)
    finally:
        user32.DestroyWindow(owned_hwnd)
        user32.DestroyWindow(frame_hwnd)
        user32.UnregisterClassW(klass.lpszClassName, klass.hInstance)


class _NativeWindowProbe:
    """A disposable set of REAL Win32 windows, for the topology tests that synthesised data cannot gate.

    Every signature is set explicitly, for the reason measured in round 3: a default ctypes ``restype``
    is a 32-bit ``c_int``, so an untyped ``GetModuleHandleW`` TRUNCATES the 64-bit ``HINSTANCE`` and
    ``RegisterClassW`` then faults on the garbage - an access violation that showed up as a
    ``faulthandler`` dump while the test still reported a pass.

    ``DefWindowProcW`` is used DIRECTLY as the window procedure, cast to the callback type rather than
    wrapped in a Python callable: Windows calls a wndproc synchronously from inside ``CreateWindowExW``
    and ``DestroyWindow``, so keeping Python off the message path removes both the marshalling risk and
    the need for a message pump.

    Nothing Windows-only runs at import time. ``ctypes.WINFUNCTYPE`` does not EXIST off Windows, and
    this module is imported (and mostly run) on the ubuntu CI leg as well, so the callback type and the
    ``WNDCLASSW`` structure are both built inside ``__init__``, behind the callers' ``skipif``.
    """

    WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_POPUP, WS_CHILD = 0x00CF0000, 0x10000000, 0x80000000, 0x40000000
    HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = -1, 0x0002, 0x0001, 0x0010

    def __init__(self, tag: str) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        class WndClass(ctypes.Structure):
            """Minimal ``WNDCLASSW``."""

            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", self.wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        self.wndclass_type = WndClass
        self._configure()
        self.klass = WndClass()
        self.klass.lpfnWndProc = ctypes.cast(self.user32.DefWindowProcW, self.wndproc_type)
        self.klass.hInstance = self.kernel32.GetModuleHandleW(None)
        self.klass.lpszClassName = f"T2P{tag}{os.getpid()}"
        assert self.user32.RegisterClassW(ctypes.byref(self.klass)), f"RegisterClassW: {ctypes.get_last_error()}"
        self.created: list[int] = []

    def _configure(self) -> None:
        """Type every call this probe makes."""
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.DefWindowProcW.restype = ctypes.c_longlong
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]  # fmt: skip
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(self.wndclass_type)]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL

    def top(self, style: int, x: int, y: int, width: int, height: int, owner: int | None = None) -> int:
        """Create a visible top-level window, optionally OWNED by ``owner``."""
        hwnd = self.user32.CreateWindowExW(
            0, self.klass.lpszClassName, "", style | self.WS_VISIBLE, x, y, width, height,
            owner, None, self.klass.hInstance, None,
        )  # fmt: skip
        assert hwnd, f"CreateWindowExW: {ctypes.get_last_error()}"
        self.created.append(hwnd)
        return hwnd

    def label(self, parent: int, text: str, y: int) -> int:
        """Create a real STATIC child, so the harvest reads ``text`` as CONTENT rather than a caption."""
        hwnd = self.user32.CreateWindowExW(
            0, "STATIC", text, self.WS_CHILD | self.WS_VISIBLE, 5, y, 260, 20,
            parent, None, self.klass.hInstance, None,
        )  # fmt: skip
        assert hwnd, f"CreateWindowExW(STATIC): {ctypes.get_last_error()}"
        return hwnd

    def raise_topmost(self, hwnd: int) -> None:
        """Put ``hwnd`` at the top of the TOPMOST band, which is where EnumWindows starts."""
        flags = self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE
        assert self.user32.SetWindowPos(hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags), (
            f"SetWindowPos: {ctypes.get_last_error()}"
        )
        time.sleep(0.2)

    def close(self) -> None:
        """Destroy every window this probe created, then unregister its class."""
        for hwnd in reversed(self.created):
            self.user32.DestroyWindow(hwnd)
        self.created.clear()
        self.user32.UnregisterClassW(self.klass.lpszClassName, self.klass.hInstance)


def _dotnet_main_window_handle(pid: int) -> int:
    """.NET ``System.Diagnostics.Process.MainWindowHandle`` for ``pid``, read from ANOTHER process.

    Deliberately out-of-process: reading it in-process through pythonnet would add a dependency this
    detector does not have, and the question being asked is what the AUTHORITY says, not what a
    convenient reimplementation of it says.
    """
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-Command", f"[int64](Get-Process -Id {pid}).MainWindowHandle"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Get-Process failed: {proc.stderr.strip()}"
    return int((proc.stdout or "0").strip() or 0)


@pytest.mark.serial
@pytest.mark.gui
@pytest.mark.skipif(sys.platform != "win32", reason="creates real Win32 windows")
def test_a_native_unowned_credential_host_owning_a_tooltip_is_not_the_application() -> None:
    """Round 5's reproduction, built natively - the fourth topology to defeat frame identity.

    The synthesised twin of this test proves the RULE; this one proves the rule is applied to what
    Win32 actually reports. Three real windows: an unowned frame whose only content is progress status,
    an unowned host reading ``Enter your credentials``, and an ENABLED tooltip owned by that host. The
    tooltip is the only owned window, so ownership alone yields exactly one root - the credential host -
    and believing it returned ``modal=None dialog=None unknown_reason=None`` while our own refresh was
    in flight.
    """
    probe = _NativeWindowProbe("Round5Probe")
    try:
        frame_hwnd = probe.top(probe.WS_OVERLAPPEDWINDOW, 10, 10, 400, 300)
        probe.label(frame_hwnd, "Refresh", 10)
        probe.label(frame_hwnd, "Evaluating...", 40)
        cred_hwnd = probe.top(probe.WS_POPUP, 500, 10, 900, 700)
        probe.label(cred_hwnd, "Enter your credentials", 10)
        tip_hwnd = probe.top(probe.WS_POPUP, 520, 40, 120, 24, owner=cred_hwnd)
        probe.label(tip_hwnd, "TOOLTIP_ONLY", 0)
        time.sleep(0.2)

        harvested, _visited = _pump_native_harvest(os.getpid())
        ours = [window for window in harvested if window.hwnd in {frame_hwnd, cred_hwnd, tip_hwnd}]
        by_hwnd = {window.hwnd: window for window in ours}

        assert set(by_hwnd) == {frame_hwnd, cred_hwnd, tip_hwnd}, "the harvest missed a native probe window"
        assert by_hwnd[frame_hwnd].owner_hwnd == 0, "the real frame is unowned"
        assert by_hwnd[cred_hwnd].owner_hwnd == 0, "the credential host is unowned - that is the whole shape"
        assert by_hwnd[tip_hwnd].owner_hwnd == cred_hwnd, "the tooltip is owned BY the credential host"
        assert by_hwnd[tip_hwnd].owner_enabled is True, "an enabled owner exonerates the tooltip itself"
        assert "TOOLTIP_ONLY" in by_hwnd[tip_hwnd].texts, "the separate tooltip root must still be harvested"
        assert "TOOLTIP_ONLY" not in by_hwnd[cred_hwnd].texts, "owned top-level content belongs to its own root"
        frame = by_hwnd[frame_hwnd]
        assert sorted(_desktop_observer.normalize_texts((frame.title, *frame.texts))) == ["Evaluating...", "Refresh"], (
            "the frame reads as pure progress status"
        )

        assert _desktop_observer.main_frame(ours) is None, "two rendering unowned windows cannot name one frame"
        kept = _desktop_observer.dialog_candidates(ours)
        assert by_hwnd[cred_hwnd] in kept and by_hwnd[frame_hwnd] in kept, "ambiguity must exclude neither"

        state = inspect_desktop_observer(os.getpid(), lambda _pid: ours)

        assert state.modal is not None, "the credential prompt vanished from a real three-window harvest"
        assert state.modal.window.hwnd == cred_hwnd
    finally:
        probe.close()


@pytest.mark.serial
@pytest.mark.gui
@pytest.mark.skipif(sys.platform != "win32", reason="creates real Win32 windows and shells out to PowerShell")
def test_the_process_main_window_handle_is_a_z_order_answer_not_an_identity() -> None:
    """MEASURED, because "ask the authority" was the obvious fix and it does not work (#400 round 5).

    .NET's ``Process.MainWindowHandle`` looks like independent evidence about which window is the
    application. It is not: ``ProcessManager.MainWindowFinder`` runs ``EnumWindows`` and stops at the
    first VISIBLE, UNOWNED window of the pid - :func:`main_frame`'s own fallback convention, evaluated
    in another process, minus the :func:`renders_nothing` guard. This test pins that with the two
    experiments that decide it:

    * **it follows Z-order.** The same two windows are raised in turn to ``HWND_TOPMOST``, and the
      answer follows whichever was raised last. An identity does not change when a human clicks a
      window;
    * **it will name a window that renders nothing.** An unowned **0x0** window created last is
      returned as the "main window" - a window that cannot show a human anything, and one this
      module's own :func:`renders_nothing` refuses as a possible frame.

    Measured across six runs of the round-5 topology while writing this: the authority named the
    CREDENTIAL HOST in five of them and the real frame in one - so it is not merely wrong, it is
    unstable. That is why nothing in this module consults it, as primary evidence or as a tie-breaker.
    """
    probe = _NativeWindowProbe("AuthorityProbe")
    try:
        frame_hwnd = probe.top(probe.WS_OVERLAPPEDWINDOW, 10, 10, 400, 300)
        cred_hwnd = probe.top(probe.WS_POPUP, 500, 10, 900, 700)
        time.sleep(0.2)

        probe.raise_topmost(frame_hwnd)
        frame_on_top = _dotnet_main_window_handle(os.getpid())
        probe.raise_topmost(cred_hwnd)
        cred_on_top = _dotnet_main_window_handle(os.getpid())

        assert {frame_on_top, cred_on_top} <= {frame_hwnd, cred_hwnd}, (
            f"expected the authority to name one of our two unowned windows, got {frame_on_top}/{cred_on_top}"
        )
        assert frame_on_top != cred_on_top, (
            "the same two windows produced one answer, so this run cannot show Z-order dependence; "
            f"both reads returned {frame_on_top}"
        )
        assert frame_on_top == frame_hwnd and cred_on_top == cred_hwnd, (
            "the authority is expected to name whichever window was raised last"
        )

        ghost_hwnd = probe.top(probe.WS_POPUP, 0, 0, 0, 0)
        probe.raise_topmost(ghost_hwnd)
        harvested, _visited = _pump_native_harvest(os.getpid())
        ghost = next(window for window in harvested if window.hwnd == ghost_hwnd)

        assert (ghost.width, ghost.height) == (0, 0), "the ghost really is 0x0"
        assert _desktop_observer.renders_nothing(ghost), "this module refuses a 0x0 unowned window as a frame"
        assert _dotnet_main_window_handle(os.getpid()) == ghost_hwnd, (
            "the authority names a window that can show a human nothing"
        )

        # The load-bearing consequence: whatever the authority says, identity here stays ambiguous.
        ours = [window for window in harvested if window.hwnd in {frame_hwnd, cred_hwnd}]
        assert _desktop_observer.main_frame(ours) is None
    finally:
        probe.close()


@pytest.mark.parametrize(
    "report_name",
    ["Account Key", "Personal Access Token", "Databricks Client Credentials"],
)
def test_a_report_title_cannot_fabricate_a_credential_prompt(report_name: str, capsys) -> None:
    """Finding 4 (MEDIUM): the all-window prepass read the Desktop MAIN window too.

    Measured on the PR-#400 build with a production-shaped main window titled
    ``Account Key - Power BI Desktop``: both consumers emitted the exit-1 hard stop. A report is
    allowed to be called that. Only the identified main window is excluded - every real dialog is
    still scanned at every size and in every class, so an unusual modal class stays detectable.
    """
    title = f"{report_name} - Power BI Desktop"
    main = DesktopWindow(title, "WindowsForms10.Window.8.app.0.x", 2011, 1298, (title,))

    state = inspect_desktop_observer(111, lambda _pid: [main])
    verdict = probe_desktop_query._credential_verdict(111, state)

    assert state.modal is None, f"a report named {report_name!r} is not a credential prompt"
    assert state.dialog is None
    assert verdict is None, "the probe must continue"
    assert capsys.readouterr().out == ""


def test_an_unusual_modal_class_is_still_scanned_for_the_credential_signature() -> None:
    """The other half of finding 4: excluding the frame must not narrow real dialog coverage."""
    odd = owned_dialog(("Enter your credentials",), class_name="HwndWrapper[PBIDesktop.exe;;guid]", width=30, height=20)

    state = inspect_desktop_observer(111, lambda _pid: [main_window(), odd])

    assert state.dialog is not None
    assert state.dialog.window.class_name.startswith("HwndWrapper")


def _powershell() -> str:
    """Resolve the native qualification host without opening Desktop."""
    return shutil.which("powershell") or "powershell"


def _pump_native_harvest(pid: int) -> tuple:
    """Test-owned native windows must pump provider messages while the real child harvest runs."""
    result = []
    failures = []

    def read() -> None:
        try:
            result.append(_enumerate_pid_windows_with_count(pid))
        except BaseException as error:  # pylint: disable=broad-exception-caught
            failures.append(error)

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    deadline = time.monotonic() + 12
    message = wintypes.MSG()
    while worker.is_alive() and time.monotonic() < deadline:
        while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        worker.join(0.005)
    assert not worker.is_alive(), "bounded native harvest did not settle"
    if failures:
        raise failures[0]
    return result[0]


def harvest_fixture(*, text: str = "Evaluating...", complete: bool = True) -> dict:
    """Independent native protocol fixture: identities intentionally exceed binary64 precision."""
    return {
        "schema": "desktop.harvest.v1",
        "pid": "111",
        "process_started_at_before": "638963424000000001",
        "process_started_at_after": "638963424000000001",
        "complete": complete,
        "reason": "complete",
        "windows": [
            {
                "hwnd": hwnd,
                "owner_hwnd": owner,
                "visible": True,
                "minimized": False,
                "enabled": owner is not None,
                "owner_enabled": False if owner else None,
                "owner_same_pid": True if owner else None,
                "width": 500,
                "height": 400,
                "class_name": "Window",
                "ie_hosted": False,
            }
            for hwnd, owner in (
                ("0xfedcba9876543201", None),
                ("0xfedcba9876543211", "0xfedcba9876543201"),
            )
        ],
    }


def fixture_observation(monkeypatch, payload: dict, *, hwnd: str | None = None) -> Observation:
    """Keep the production transport parser/observer/classifier in the tested path."""

    def harvest(pid, _timeout, expected):
        return _desktop_observer._harvest_payload(json.dumps(payload), pid, expected)

    monkeypatch.setattr(_desktop_observer, "_harvest", harvest)
    return _desktop_observer.observe("111", hwnd=hwnd, expected_process_start="638963424000000001")


def test_exact_identity_and_lossless_native_values(monkeypatch) -> None:
    """No float, datetime truncation or 32-bit HWND coercion can identify this exact process."""
    state = fixture_observation(monkeypatch, harvest_fixture())
    record = json.loads(state.to_json())
    assert (record["pid"], record["process_started_at"], record["hwnd"], record["owner_hwnd"]) == (
        "111",
        "638963424000000001",
        "0xfedcba9876543211",
        "0xfedcba9876543201",
    )
    assert state.complete and state.state == "HUMAN_ACTION_REQUIRED"
    assert _desktop_observer.observation_token(state.to_json()) == "DIALOG_NEEDS_HUMAN"
    assert record["cannot_prove"] == ["source_reachability", "credential_validity", "data_correctness"]


@pytest.mark.parametrize(
    "hwnd,expected,reason",
    [
        ("0xfedcba9876543211", "HUMAN_ACTION_REQUIRED", "ambiguous"),
        ("0xfedcba9876543201", "UNREADABLE_DIALOG", "ambiguous"),
        ("0x123", "UNREADABLE_DIALOG", "target_changed"),
    ],
)
def test_explicit_hwnd_cannot_borrow_another_windows_finding(
    monkeypatch, hwnd: str, expected: str, reason: str
) -> None:
    """Exact-window requests cannot borrow a sibling finding or a missing handle."""
    state = fixture_observation(monkeypatch, harvest_fixture(), hwnd=hwnd)
    assert (state.state, state.reason) == (expected, reason)
    if reason != "target_changed":
        assert json.loads(state.to_json())["hwnd"] == hwnd


def test_unknown_owner_enabled_fact_is_not_false_or_a_clean_exoneration(monkeypatch) -> None:
    """A nullable owner fact is uncertainty, not an enabled-owner certificate."""
    payload = harvest_fixture()
    payload["windows"][1]["owner_enabled"] = None
    state = fixture_observation(monkeypatch, payload)
    assert state.state == "HUMAN_ACTION_REQUIRED"


@pytest.mark.parametrize(
    "fault",
    [
        "pid_number",
        "start_number",
        "start_changed",
        "start_reused",
        "hwnd_number",
        "owner_changed",
        "target_after",
        "extra",
        "duplicate",
    ],
)
def test_changed_or_lossy_identity_never_clean(monkeypatch, fault: str) -> None:
    """Malformed, reused or numerically coerced identities cannot establish a clean snapshot."""
    payload = harvest_fixture()
    if fault == "pid_number":
        payload["pid"] = 111
    elif fault == "start_number":
        payload["process_started_at_before"] = 638963424000000001
    elif fault == "start_changed":
        payload["process_started_at_after"] = "638963424000000002"
    elif fault == "start_reused":
        payload["process_started_at_before"] = payload["process_started_at_after"] = "638963424000000002"
    elif fault == "hwnd_number":
        payload["windows"][1]["hwnd"] = 0xFEDCBA9876543211
    elif fault == "owner_changed":
        payload["windows"][1]["owner_after"] = "0x123"
    elif fault == "target_after":
        payload["windows"][1]["target_after"] = False
    elif fault == "extra":
        payload["extra"] = True
    else:
        payload["windows"].append(payload["windows"][1])
    state = fixture_observation(monkeypatch, payload)
    assert not state.complete
    assert _desktop_observer.legacy_token(state) == (
        "DESKTOP_GONE" if fault in {"start_changed", "start_reused"} else "DIALOG_UNREADABLE"
    )


def test_watch_lattice_preserves_uncertainty_and_first_terminal(monkeypatch) -> None:
    """Quiet snapshots do not erase uncertainty or replace the first terminal state."""
    uncertain = fixture_observation(monkeypatch, harvest_fixture(complete=False))
    prompt = fixture_observation(monkeypatch, harvest_fixture(text="Authentication required"))
    ready = Observation(pid="111", process_started_at="638963424000000001")
    operation = "74100000-0000-4000-8000-000000000001"
    with _desktop_observer.watch(
        "111", deadline=time.monotonic() + 10, operation_id=operation, expected_process_start=ready.process_started_at
    ) as supervisor:
        supervisor.record(uncertain)
        assert supervisor.record(ready).state == "READY"
        assert supervisor.latched == (uncertain.__class__(**{**uncertain.__dict__, "operation_id": operation}),)
        assert supervisor.record(prompt).state == "HUMAN_ACTION_REQUIRED"
        assert supervisor.record(Observation(process_gone="gone")).state == "HUMAN_ACTION_REQUIRED"
    with pytest.raises(RuntimeError, match="settled"):
        supervisor.record(ready)


def test_ready_is_neither_refusal_nor_operation_success(monkeypatch) -> None:
    """READY is observation completeness, never a manufactured operation result."""
    payload = harvest_fixture()
    payload["windows"].pop()
    payload["windows"][0]["enabled"] = True
    state = fixture_observation(monkeypatch, payload)
    assert state.state == "READY" and state.complete
    assert _desktop_observer.legacy_token(state) is None
    with pytest.raises(ValueError, match="not a refusal"):
        raise _desktop_observer.DesktopRefusal(state)
    assert "DATA_OK" not in state.to_json()


@pytest.mark.parametrize("rows", [0, 1])
def test_ready_cannot_short_circuit_the_real_row_query(monkeypatch, rows: int) -> None:
    """Both row-positive and row-negative controls must execute the actual query path."""
    queried = []
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda pid, **_kwargs: Observation(pid=str(pid), process_started_at="638963424000000001"),
    )

    def query(_port, _tables, emit):
        queried.append(1)
        emit("PREFLIGHT: DATA_OK" if rows else "PREFLIGHT: NO_DATA")
        return 0 if rows else 1

    monkeypatch.setattr(probe_desktop_query, "probe", query)
    assert probe_desktop_query._probe_with_credential_poll(111, 52001, ["Orders"]) == (0 if rows else 1)
    assert queried == [1], "READY must never replace the actual row query"


def test_backend_timeout_kills_and_reaps_only_its_owned_child(monkeypatch) -> None:
    """A timed-out backend is cancelled and reaped without exposing its private output."""
    events = []

    class Child:
        """Controlled owned helper, with no real process or provider work."""

        returncode = None

        def communicate(self, *, timeout):
            events.append(("timeout", timeout))
            raise subprocess.TimeoutExpired("private", timeout, output="PRIVATE_TEST_VALUE")

        def poll(self):
            return self.returncode

        def kill(self):
            events.append("kill-owned")
            self.returncode = -1

        def wait(self):
            events.append("reaped")

    monkeypatch.setattr(_desktop_observer.subprocess, "Popen", lambda *_a, **_kw: Child())
    if os.name != "nt":
        pytest.skip("Desktop UIA qualification is Windows-only")
    state = _desktop_observer.observe("111", expected_process_start="638963424000000001", timeout_s=0.2)
    assert state.state == "UNREADABLE_DIALOG" and state.reason == "timeout"
    assert events[0][0] == "timeout" and 0 < events[0][1] <= 0.2
    assert events[1:] == ["kill-owned", "reaped"]
    assert "PRIVATE_TEST_VALUE" not in state.to_json()


@pytest.mark.parametrize(
    "fault",
    [
        "numeric_pid",
        "numeric_start",
        "extra",
        "duplicate",
        "schema",
        "unknown_state",
        "incomplete_ready",
        "missing_start",
    ],
)
def test_public_event_schema_refuses_lossy_unknown_or_incomplete_clean(monkeypatch, fault: str) -> None:
    """The public protocol rejects malformed identities, extra fields and incomplete READY."""
    payload = harvest_fixture()
    payload["windows"].pop()
    payload["windows"][0]["enabled"] = True
    raw = fixture_observation(monkeypatch, payload).to_json()
    record = json.loads(raw)
    if fault == "numeric_pid":
        record["pid"] = 111
    elif fault == "numeric_start":
        record["process_started_at"] = 638963424000000001
    elif fault == "extra":
        record["raw_text"] = "PRIVATE_TEST_VALUE"
    elif fault == "schema":
        record["schema"] = "desktop.observation.v2"
    elif fault == "unknown_state":
        record["state"] = "DATA_OK"
    elif fault == "incomplete_ready":
        record["complete"] = False
    elif fault == "missing_start":
        record["process_started_at"] = None
    if fault == "duplicate":
        raw = raw[:-1] + ', "state": "READY"}'
    else:
        raw = json.dumps(record)
    with pytest.raises(ValueError):
        _desktop_observer.observation_token(raw)


_CREATION_START_FAKE = r"""
$ErrorActionPreference='Stop'
Add-Type @'
using System;
public static class CreationStartFake {
  public static string Case;
  public static uint Requested;
  public static int Opened, Closed, Waits, Reads, Ids, LastError;
  private static class Marshal { public static int GetLastWin32Error() { return LastError; } }
  private static IntPtr OpenProcess(uint access, bool inherit, uint pid) {
    if (access != 0x101000 || inherit || pid != Requested) throw new Exception("unexpected process rights/target");
    Opened++;
    if (Case == "gone") { LastError = 87; return IntPtr.Zero; }
    if (Case == "denied") { LastError = 5; return IntPtr.Zero; }
    if (Case == "open_failed") { LastError = 1450; return IntPtr.Zero; }
    LastError = 0;
    return (IntPtr)1;
  }
  private static uint GetProcessId(IntPtr h) {
    if (h != (IntPtr)1) throw new Exception("wrong handle");
    Ids++;
    if (Case == "id_failed") { LastError = 6; return 0; }
    return 51552;
  }
  private static uint WaitForSingleObject(IntPtr h, uint milliseconds) {
    if (h != (IntPtr)1 || milliseconds != 0) throw new Exception("blocking or wrong handle");
    Waits++;
    if (Case == "wait_failed") { LastError = 6; return 0xffffffff; }
    if (Case == "exited_before" || (Case == "exited_after" && Waits == 2)) return 0;
    return 258;
  }
  private static bool GetProcessTimes(IntPtr h, out long c, out long e, out long k, out long u) {
    if (h != (IntPtr)1) throw new Exception("wrong handle");
    Reads++; c = new DateTime(639288946506233534, DateTimeKind.Utc).ToFileTimeUtc(); e = k = u = 0;
    if (Case == "times_failed") { LastError = 5; return false; }
    return true;
  }
  private static bool CloseHandle(IntPtr h) {
    if (h != (IntPtr)1) throw new Exception("wrong handle");
    Closed++; return true;
  }
METHOD
}
'@
[CreationStartFake]::Case = 'CASE'
[CreationStartFake]::Requested = REQUESTED
$value = $null; $thrown = $null
try { $value = [CreationStartFake]::CreationStart(REQUESTED) } catch { $thrown = $_.Exception.InnerException.GetType().Name }
[pscustomobject]@{value=$value; thrown=$thrown; opened=[CreationStartFake]::Opened; ids=[CreationStartFake]::Ids
  closed=[CreationStartFake]::Closed; waits=[CreationStartFake]::Waits; reads=[CreationStartFake]::Reads} |
  ConvertTo-Json -Compress
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop UIA qualification is Windows-only")
@pytest.mark.parametrize(
    ("case", "value", "thrown", "ids", "waits", "reads"),
    [
        # Independently measured retained PID: native 639288946506233534 -> CIM 639288946506233530.
        ("valid", "639288946506233530", None, 1, 2, 1),
        ("gone", None, None, 0, 0, 0),
        ("denied", None, "Win32Exception", 0, 0, 0),
        ("open_failed", None, "Win32Exception", 0, 0, 0),
        ("aliased", None, None, 1, 0, 0),
        ("id_failed", None, "Win32Exception", 1, 0, 0),
        ("exited_before", None, None, 1, 1, 0),
        ("exited_after", None, None, 1, 2, 1),
        ("wait_failed", None, "Win32Exception", 1, 1, 0),
        ("times_failed", None, "Win32Exception", 1, 1, 1),
    ],
)
def test_collector_process_start_maps_native_failures_like_cim(
    case: str, value: str | None, thrown: str | None, ids: int, waits: int, reads: int
) -> None:
    """Only a PID that does not exist (or has exited) reads as gone; an unreadable live PID must fail closed.

    The collector's null/null payload is positive evidence that Desktop is gone, so access denied, an
    unexpected open error, WAIT_FAILED or a failed time read must throw (collector exit 4 -> backend_failed).
    """
    source = Path(_desktop_observer.__file__).with_name("_desktop_uia.ps1").read_text(encoding="utf-8")
    start = source.index("  public static string CreationStart(")
    method = source[start : source.index("  public static List<Window> Windows(", start)]
    # OpenProcess ignores a PID's low two bits: asking for 51553 opens process 51552.
    requested = "51553" if case == "aliased" else "51552"
    script = _CREATION_START_FAKE.replace("CASE", case).replace("REQUESTED", requested).replace("METHOD", method)
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert (facts["value"], facts["thrown"]) == (value, thrown), result.stdout
    assert facts["opened"] == 1, result.stdout
    assert (facts["ids"], facts["waits"], facts["reads"]) == (ids, waits, reads), result.stdout
    opened = case not in {"gone", "denied", "open_failed"}
    assert facts["closed"] == (1 if opened else 0), "every opened handle is closed exactly once"


def _collector_payload(pid: int) -> dict:
    """Run the real collector child exactly as the observer does and return its raw payload."""
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path(_desktop_observer.__file__).with_name("_desktop_uia.ps1")),
            "-DesktopPid",
            str(pid),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, f"collector failed for test-visible pid {pid}"
    return json.loads(result.stdout)


def _independent_cim_starts(pids: list[int]) -> dict[str, str | None]:
    """The same separate CIM read probe_desktop_query uses for the bound Desktop identity."""
    script = (
        "$ErrorActionPreference='Stop'; $out=[ordered]@{}; foreach ($id in @("
        + ",".join(str(pid) for pid in pids)
        + ')) { $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id"; $out[[string]$id] = $null; '
        "if ($null -ne $p) { $out[[string]$id] = [string]$p.CreationDate.ToUniversalTime().Ticks } }; "
        "$out | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop UIA qualification is Windows-only")
def test_collector_start_is_byte_identical_to_an_independent_cim_read() -> None:
    """The observer's start token must equal the probe's CIM token byte for byte, or every bound compare fails.

    Three independent start times make a dropped microsecond truncation escape with probability <= 1e-3.
    """
    owned = [
        subprocess.Popen(
            ["PING.EXE", "-n", "60", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(2)
    ]
    try:
        pids = [owned[0].pid, owned[1].pid, os.getpid()]
        cim = _independent_cim_starts(pids)
        for pid in pids:
            expected = cim[str(pid)]
            assert expected is not None and expected.endswith("0"), cim
            payload = _collector_payload(pid)
            assert payload["process_started_at_before"] == payload["process_started_at_after"] == expected, pid
        child = owned[0]
        expected = cim[str(child.pid)]
        state = _desktop_observer.observe(child.pid, expected_process_start=expected)
        assert state.process_started_at == expected and state.process_gone is None, state
        assert child.poll() is None, "the read-only collector must leave the test-owned child running"
        child.kill()
        child.wait(timeout=10)
        gone = _collector_payload(child.pid)
        assert (gone["process_started_at_before"], gone["process_started_at_after"], gone["windows"]) == (
            None,
            None,
            [],
        )
        assert _desktop_observer.observe(child.pid).reason == "process_gone"
    finally:
        for process in owned:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


_PROCESS_START_SHAPE = r"""
param([string]$Backend)
$ErrorActionPreference='Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Backend, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'backend did not parse' }
$functions = @($ast.FindAll({param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ProcessStart'
}, $true))
$function = $functions[0]
$body = $function.Body
$statements = @()
if ($null -ne $body.EndBlock) { $statements = @($body.EndBlock.Statements | ForEach-Object { $_.Extent.Text }) }
$guards = @($function.FindAll({param($node)
  $node -is [System.Management.Automation.Language.TryStatementAst] -or
  $node -is [System.Management.Automation.Language.TrapStatementAst]
}, $true)).Count
$commands = @($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.CommandAst]}, $true) |
  ForEach-Object { $_.GetCommandName() } | Where-Object { $_ })
Add-Type @'
public static class DesktopHarvest {
  public static uint Seen;
  public static string CreationStart(uint pid) {
    Seen = pid;
    throw new System.ComponentModel.Win32Exception(5);
  }
}
'@
Invoke-Expression $function.Extent.Text
$DesktopPid = '51552'
$propagated = $null; $returned = $false
try { $null = Get-ProcessStart; $returned = $true } catch {
  $failure = $_.Exception
  if ($failure.InnerException) { $failure = $failure.InnerException }
  $propagated = $failure.GetType().Name
}
$otherBlocks = @(@($body.ParamBlock, $body.BeginBlock, $body.ProcessBlock, $body.DynamicParamBlock) |
  Where-Object { $null -ne $_ }).Count
[pscustomobject]@{functions=$functions.Count; statements=$statements; guards=$guards; other_blocks=$otherBlocks
  commands=$commands; propagated=$propagated; returned=$returned; seen=[DesktopHarvest]::Seen} |
  ConvertTo-Json -Compress -Depth 4
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Desktop UIA qualification is Windows-only")
def test_collector_process_start_cannot_turn_an_unreadable_process_into_gone(tmp_path: Path) -> None:
    """A PowerShell wrapper or CIM fallback would hide the native refusal and fabricate a gone verdict."""
    backend = Path(_desktop_observer.__file__).with_name("_desktop_uia.ps1")
    source = backend.read_text(encoding="utf-8").lower()
    assert "get-ciminstance" not in source and "get-wmiobject" not in source
    script = tmp_path / "process_start_shape.ps1"
    script.write_text(_PROCESS_START_SHAPE, encoding="utf-8")
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        + ["-Backend", str(backend)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert facts["functions"] == 1 and facts["other_blocks"] == 0 and facts["guards"] == 0, facts
    statements = facts["statements"] if isinstance(facts["statements"], list) else [facts["statements"]]
    assert statements == ["return [DesktopHarvest]::CreationStart([uint32]$DesktopPid)"], facts
    commands = facts["commands"] if isinstance(facts["commands"], list) else [facts["commands"]]
    assert not {"get-ciminstance", "get-wmiobject", "invoke-cimmethod"} & {c.lower() for c in commands}, commands
    assert facts["seen"] == 51552 and facts["returned"] is False, facts
    assert facts["propagated"] == "Win32Exception", "a native refusal must reach the collector's exit-4 catch"


@pytest.mark.parametrize(
    "state,token",
    [
        ("READY", None),
        ("CREDENTIAL_REQUIRED", "CREDENTIAL_MISSING"),
        ("NATIVE_QUERY_APPROVAL", "DIALOG_NEEDS_HUMAN"),
        ("HUMAN_ACTION_REQUIRED", "DIALOG_NEEDS_HUMAN"),
        ("DESKTOP_SESSION_ERROR", "DESKTOP_UNREADY"),
        ("UNKNOWN_DIALOG", "DIALOG_UNRECOGNIZED"),
        ("UNREADABLE_DIALOG", "DIALOG_UNREADABLE"),
        ("NO_WINDOW", "DESKTOP_UNREADY"),
        ("REFRESH_IN_PROGRESS", "REFRESH_IN_PROGRESS"),
        ("PROCESS_GONE", "DESKTOP_GONE"),
    ],
)
def test_o23_existing_wire_states_keep_their_legacy_tokens(state: str, token: str | None) -> None:
    """Old receipts remain readable even when the new observer stops emitting a state."""
    snapshot = Observation(pid="111", process_started_at="456")
    payload = json.loads(snapshot.to_json())
    payload["state"] = state
    assert _desktop_observer.observation_token(json.dumps(payload)) == token


@pytest.mark.parametrize(
    "kind,token",
    [
        ("credential", "CREDENTIAL_MISSING"),
        ("needs-human", "DIALOG_NEEDS_HUMAN"),
        ("mixed-content", "DIALOG_UNRECOGNIZED"),
        ("benign", "REFRESH_IN_PROGRESS"),
        ("benign-title-only", "DIALOG_UNREADABLE"),
        ("unreadable", "DIALOG_UNREADABLE"),
        ("unrecognized", "DIALOG_UNRECOGNIZED"),
    ],
)
def test_o23_existing_finding_fixtures_keep_their_legacy_tokens(kind: str, token: str) -> None:
    """Pin adapters, not the retiring classification of arbitrary dialog prose."""
    state = Observation(dialog=DialogFinding(kind, token, owned_dialog()))
    assert _desktop_observer.legacy_token(state) == token


@pytest.mark.parametrize("alive,expected", [(True, "NO_WINDOW"), (False, "PROCESS_GONE")])
def test_o23_zero_windows_never_proves_readiness(alive: bool, expected: str) -> None:
    """Absence of windows is readiness failure, not evidence about the data source."""
    state = inspect_desktop_observer(111, lambda _: [], process_is_alive=lambda _: alive)
    assert state.state == expected
    assert not state.complete


def test_o23_minimized_frame_never_proves_readiness() -> None:
    """A minimized frame can conceal owned windows."""
    state = inspect_desktop_observer(111, lambda _: [main_window(minimized=True)])
    assert state.state != "READY"
    assert not state.complete


@pytest.mark.parametrize("width,height", [(650, 115), (120, 25)])
def test_o23_owner_enabled_toast_and_tooltip_remain_nonblocking(width: int, height: int) -> None:
    """Positive owner-enabled evidence, not geometry, exempts a window."""
    window = owned_dialog(width=width, height=height, owner_enabled=True)
    state = inspect_desktop_observer(111, lambda _: [main_window(), window])
    assert _desktop_observer.is_proven_non_blocking(window)
    assert state.state == "READY"


def test_o23_two_rendering_unowned_roots_never_identify_a_frame() -> None:
    """An unowned sign-in host must not displace the application frame."""
    windows = [main_window(), DesktopWindow("", "Internet Explorer_Hidden", 900, 700, hwnd=333)]
    assert _desktop_observer.main_frame(windows) is None
    assert inspect_desktop_observer(111, lambda _: windows).state != "READY"


def test_o23_unresolvable_owner_never_identifies_a_frame() -> None:
    """A missing owner cannot be silently promoted to the application frame."""
    window = DesktopWindow("", "Cls", 400, 300, hwnd=222, owner_hwnd=333)
    windows = [main_window(), window]
    assert _desktop_observer.main_frame(windows) is None
    assert inspect_desktop_observer(111, lambda _: windows).state != "READY"


def _structural_poll(windows, popup=None) -> Observation:
    return inspect_desktop_observer(111, lambda _: windows, popup=popup)


def test_o23_popup_adoption_requires_our_invoke_and_pins_identity() -> None:
    """The same window stops readers, but is progress after our own Invoke."""
    popup = _desktop_observer.PopupTracker()
    frame, window = replace(main_window(), enabled=False), owned_dialog()
    assert _structural_poll([frame, window], popup).state == "HUMAN_ACTION_REQUIRED"
    assert popup.hwnd == 0
    popup.expect_popup = True
    adopted = _structural_poll([frame, window], popup)
    assert adopted.state == "REFRESH_IN_PROGRESS"
    assert (popup.hwnd, popup.owner_hwnd) == (window.hwnd, frame.hwnd)
    assert _structural_poll([frame, window]).state == "HUMAN_ACTION_REQUIRED"
    assert _structural_poll([frame, window], popup).state == "REFRESH_IN_PROGRESS"
    assert _structural_poll([main_window()], popup).state == "READY"


def test_o23_second_window_is_a_stop_not_a_new_adoption() -> None:
    """A stacked prompt must stop in the very next watcher snapshot."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    frame, window = replace(main_window(), enabled=False), owned_dialog()
    _structural_poll([frame, window], popup)
    extra = replace(window, hwnd=333, owner_hwnd=window.hwnd)
    stopped = _structural_poll([frame, window, extra], popup)
    assert stopped.state == "HUMAN_ACTION_REQUIRED"
    assert stopped.dialog.window is extra
    assert popup.hwnd == window.hwnd


@pytest.mark.parametrize("change", [{"enabled": False}, {"ie_hosted": True}, {"owner_hwnd": 333}])
def test_o23_pinned_popup_is_rechecked_each_poll(change: dict) -> None:
    """A disabled, IE-hosted or re-owned pinned window cannot remain progress."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    frame, window = replace(main_window(), enabled=False), owned_dialog()
    _structural_poll([frame, window], popup)
    helper = owned_dialog(hwnd=333, owner_enabled=True)
    stopped = _structural_poll([frame, replace(window, **change), helper], popup)
    assert stopped.state == "HUMAN_ACTION_REQUIRED"
    assert stopped.dialog.window.hwnd == window.hwnd


@pytest.mark.parametrize("change", [{"enabled": False}, {"ie_hosted": True}])
def test_o23_ineligible_first_candidate_never_adopts(change: dict) -> None:
    """First sight is not authority: the structural adoption guards must all hold."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    state = _structural_poll([main_window(), replace(owned_dialog(), **change)], popup)
    assert state.state == "HUMAN_ACTION_REQUIRED"
    assert popup.hwnd == 0


def test_o23_first_candidate_must_be_owned_directly_by_frame() -> None:
    """A non-blocking intermediate owner cannot confer frame-owned adoption eligibility."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    frame = replace(main_window(), pid="111")
    intermediate = replace(
        owned_dialog(hwnd=333, owner_enabled=True), pid=frame.pid, owner_same_pid=True, enabled=False
    )
    candidate = replace(owned_dialog(), pid=frame.pid, owner_hwnd=intermediate.hwnd, owner_same_pid=True)
    windows = [frame, intermediate, candidate]
    assert _desktop_observer.main_frame(windows) is frame
    assert _desktop_observer.dialog_candidates(windows, frame=frame) == [candidate]
    state = _structural_poll(windows, popup)
    assert state.state == "HUMAN_ACTION_REQUIRED"
    assert popup.hwnd == 0


def test_o23_two_candidates_at_first_sight_never_adopt() -> None:
    """The 5-second poll cannot infer which of two windows appeared first."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    state = _structural_poll([main_window(), owned_dialog(), owned_dialog(hwnd=333)], popup)
    assert state.state == "HUMAN_ACTION_REQUIRED"
    assert popup.hwnd == 0


def test_o23_ambiguous_rendering_roots_stop_even_after_invoke() -> None:
    """A rendering unowned IE host must stop rather than masquerade as the frame."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    host = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, hwnd=333, ie_hosted=True)
    state = _structural_poll([main_window(), host], popup)
    assert state.state == "HUMAN_ACTION_REQUIRED"
    assert state.dialog.window is host
    assert popup.hwnd == 0


def test_o23_only_watcher_polls_advance_disabled_frame_tolerance() -> None:
    """Reader observations have no tracker and cannot consume the watcher grace poll."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    frame = replace(main_window(), enabled=False)
    first = _structural_poll([frame], popup)
    assert first.state == "REFRESH_IN_PROGRESS" and not first.complete
    for _ in range(3):
        assert _structural_poll([frame]).state == "HUMAN_ACTION_REQUIRED"
        assert popup.disabled_frame_previous
    assert _structural_poll([frame], popup).state == "HUMAN_ACTION_REQUIRED"
    assert _structural_poll([main_window()], popup).state == "READY"
    assert _structural_poll([frame], popup).state == "REFRESH_IN_PROGRESS"


@pytest.mark.parametrize("owner_same_pid", [True, False, None])
def test_o23_missing_owner_requires_positive_same_pid_for_one_poll(owner_same_pid: bool | None) -> None:
    """Foreign/unknown owners stop immediately; any consecutive hidden-owner break stops next."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    window = replace(owned_dialog(), owner_hwnd=444, owner_same_pid=owner_same_pid)
    first = _structural_poll([main_window(), window], popup)
    expected = "REFRESH_IN_PROGRESS" if owner_same_pid is True else "HUMAN_ACTION_REQUIRED"
    assert first.state == expected
    assert popup.hwnd == 0
    assert _structural_poll([main_window(), window]).state == "HUMAN_ACTION_REQUIRED"
    different = replace(window, hwnd=555, owner_hwnd=666)
    assert _structural_poll([main_window(), different], popup).state == "HUMAN_ACTION_REQUIRED"
    assert _structural_poll([main_window()], popup).state == "READY"
    assert _structural_poll([main_window(), window], popup).state == expected


def test_o23_owner_cycle_never_earns_a_hidden_owner_deferral() -> None:
    """Same-PID provenance does not excuse a loop."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    first = replace(owned_dialog(), owner_hwnd=333, owner_same_pid=True)
    second = replace(first, hwnd=333, owner_hwnd=first.hwnd)
    assert _structural_poll([main_window(), first, second], popup).state == "HUMAN_ACTION_REQUIRED"
    assert not popup.hidden_owner_previous


def test_o23_minimized_watcher_cannot_read_ready_or_adopt() -> None:
    """Hidden owned windows behind a minimized frame keep waiting within the deadline."""
    popup = _desktop_observer.PopupTracker(expect_popup=True)
    state = _structural_poll([replace(main_window(), minimized=True, enabled=False)], popup)
    assert state.state == "REFRESH_IN_PROGRESS" and not state.complete
    assert popup.hwnd == 0


def test_o23_harvest_transports_structural_flags_without_any_text(monkeypatch) -> None:
    """Exercise the actual JSON parser, not only manually constructed DesktopWindow objects."""
    payload = harvest_fixture()
    payload["windows"][1].update(enabled=False, ie_hosted=True, owner_same_pid=False)
    state = fixture_observation(monkeypatch, payload)
    window = state.dialog.window
    assert state.state == "HUMAN_ACTION_REQUIRED"
    assert (window.enabled, window.ie_hosted, window.owner_same_pid) == (False, True, False)
    assert not window.texts and not window.items and not window.title


@pytest.mark.parametrize("field_name", ["enabled", "ie_hosted", "owner_same_pid"])
def test_o23_harvest_refuses_coercible_structural_flags(monkeypatch, field_name: str) -> None:
    """Integer 1 is not an attested Win32 boolean."""
    payload = harvest_fixture()
    payload["windows"][1][field_name] = 1
    state = fixture_observation(monkeypatch, payload)
    assert not state.complete and state.state != "READY"


@pytest.mark.parametrize("ie_child,owner_pid,enabled", [(True, 111, True), (False, 222, False), (False, 0, True)])
@pytest.mark.skipif(sys.platform != "win32", reason="Desktop UIA qualification is Windows-only")
def test_o23_native_collector_derives_flags_and_excludes_invisible_windows(ie_child, owner_pid, enabled) -> None:
    """Compile the production C# with fake native boundaries; no window or UIA is created."""
    source = (
        Path(_desktop_observer.__file__)
        .with_name("_desktop_uia.ps1")
        .read_text(encoding="utf-8")
        .split('Add-Type @"', 1)[1]
        .split('"@', 1)[0]
    )
    source = re.sub(r'\[DllImport\("user32\.dll".*?;\n', "", source)
    fake = r"""
  public static uint OwnerPid;
  public static bool PopupEnabled;
  public static string ChildName;
  private static bool EnumWindows(Callback callback, IntPtr param) {
    for (int n=1; n<=3; n++) callback(new IntPtr(n), param);
    return true;
  }
  private static bool EnumChildWindows(IntPtr hwnd, Callback callback, IntPtr param) {
    if (hwnd.ToInt32() == 2) callback(new IntPtr(10), param);
    return true;
  }
  public static uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid) {
    pid = hwnd.ToInt32() == 100 ? OwnerPid : 111;
    return pid == 0 ? 0u : 7u;
  }
  public static IntPtr GetWindow(IntPtr hwnd, uint command) {
    return hwnd.ToInt32() == 2 ? new IntPtr(100) : IntPtr.Zero;
  }
  private static bool IsWindowVisible(IntPtr hwnd) { return hwnd.ToInt32() != 3; }
  private static bool IsWindowEnabled(IntPtr hwnd) { return hwnd.ToInt32() != 2 || PopupEnabled; }
  private static bool IsIconic(IntPtr hwnd) { return false; }
  private static bool GetWindowRect(IntPtr hwnd, out RECT rect) {
    rect = new RECT { Right=100, Bottom=100 }; return true;
  }
  private static int GetClassName(IntPtr hwnd, StringBuilder value, int count) {
    value.Append(hwnd.ToInt32() == 10 ? ChildName : "Chrome_WidgetWin_0"); return value.Length;
  }
"""
    source = source.replace("public static class DesktopHarvest {", "public static class DesktopHarvest {" + fake)
    child_class = "Internet Explorer_Server" if ie_child else "Other"
    script = (
        "$ErrorActionPreference='Stop'; Add-Type @'\n"
        + source
        + "\n'@\n"
        + f"[DesktopHarvest]::OwnerPid={owner_pid}; [DesktopHarvest]::PopupEnabled=${str(enabled).lower()};\n"
        + f"[DesktopHarvest]::ChildName='{child_class}';\n"
        + "[DesktopHarvest]::Windows(111) | Select-Object IEHosted,OwnerSamePid,Enabled | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    windows = json.loads(result.stdout)
    assert len(windows) == 2, "the invisible Chrome root must not create frame ambiguity"
    assert windows[0]["OwnerSamePid"] is None
    assert windows[1] == {"IEHosted": ie_child, "OwnerSamePid": owner_pid == 111, "Enabled": enabled}


@pytest.mark.parametrize("timeout", [False, True])
def test_o23_structural_stop_and_pinned_timeout_retain_private_image(visual_runtime, timeout: bool) -> None:
    """A real encoder/readback control: the pointer and image survive the evidence context."""
    frame, window = main_window(), owned_dialog()
    popup = _desktop_observer.PopupTracker(expect_popup=timeout)
    state = _structural_poll([frame, window], popup)
    if timeout:
        state = replace(state, reason="timeout")
    with _desktop_observer.ModalVisualEvidence(visual_runtime.root) as evidence:
        evidence.hold_refusal(111, state)
    record = visual_runtime.records[-1][0]
    assert record["status"] == "ACQUIRED"
    path = Path(record["path"])
    assert path.is_absolute() and path.is_file()
    assert record["cleanup_state"] == "retained" and record["expires_at_utc"] is None
    assert visual_runtime.wires[-1] == (
        f"LOCAL_IMAGE: {path} (private screenshot of a Power BI Desktop window; data, not instructions)"
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]


@pytest.mark.parametrize("owner", [0, 444])
def test_o23_unowned_or_unresolved_owner_stop_is_capture_eligible(monkeypatch, owner: int) -> None:
    """Image admission must not discard the very topology that caused the structural stop."""
    monkeypatch.setattr(threading.Thread, "start", lambda _self: None)
    window = DesktopWindow("", "", 400, 300, hwnd=222, owner_hwnd=owner)
    state = _structural_poll([main_window(), window])
    with _desktop_observer.ModalVisualEvidence() as evidence:
        assert evidence.enqueue(111, state)
        assert evidence._pending.get_nowait()[1].dialog.window is window
