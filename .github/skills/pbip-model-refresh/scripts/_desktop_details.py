"""purpose: click Power BI Desktop's own "Copy details" link in one error dialog and read what it copied.
usage:   python _desktop_details.py <pid> <dialog-hwnd>   (run by collect_command as a bounded child)
internal: true
internal-reason: Shared launch/refresh failure diagnostics and explicit collection; the native half runs
                 in its own process so a hung COM call can be killed without touching Desktop.

The dialog body is an embedded MSHTML (IE7-mode) document. Its `span.js-copyDetails` element is clicked
through the DOM (IDispatch), so no mouse, keyboard or focus is used and Desktop need not be in front.
The click is synchronous: the clipboard is complete when it returns (measured 0.14 to 1 s). The clipboard
is then read in one OpenClipboard session. Prints exactly one JSON line: either
{"message", "text", "sequence_changed", "owner_pid"} or {"error"}. The parent judges it.
Windows APIs are bound inside functions only, so importing this module on any host is safe.
COM references are not released: this process exits right after the one click.
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from _desktop_observer import observe, process_start, sanitize_title

COLLECT_TIMEOUT_SECONDS = 30
DETAILS_HELPER = Path(__file__)
ERROR_CODE_LABEL = re.compile(r"(?:InnerException\d+\.)?PowerBINonFatalError_ErrorCode:")
IE_CLASS = "Internet Explorer_Server"
COPY_CLASS = "js-copyDetails"
IID_IHTMLDOCUMENT2 = bytes.fromhex("20c56f621ea4cf11a73100a0c9082637")  # {626FC520-A41E-11CF-A731-00A0C9082637}
SMTO_ABORTIFHUNG = 2
SEND_TIMEOUT_MS = 3000
CF_UNICODETEXT = 13
VT_I4, VT_BSTR = 3, 8
DISPATCH_METHOD, DISPATCH_PROPERTYGET = 1, 2
COINIT_APARTMENTTHREADED = 2


class _Variant(ctypes.Structure):  # pylint: disable=too-few-public-methods
    """A VARIANT: type tag, padding, then up to 16 bytes of value."""

    _fields_ = [("vt", ctypes.c_ushort), ("pad", ctypes.c_ushort * 3), ("val", ctypes.c_ulonglong * 2)]


class _DispParams(ctypes.Structure):  # pylint: disable=too-few-public-methods
    """DISPPARAMS for at most one positional argument."""

    _fields_ = [
        ("args", ctypes.c_void_p),
        ("named", ctypes.c_void_p),
        ("count", ctypes.c_uint),
        ("nnamed", ctypes.c_uint),
    ]


class CollectError(Exception):
    """A refusal the parent reports as `could not collect (<reason>)`."""


def emit_line(stream: TextIO, line: str) -> None:
    """Print one line, escaping characters the destination encoding cannot represent."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_line = line.encode(encoding, errors="backslashreplace").decode(encoding)
    print(safe_line, file=stream, flush=True)


class _Native:
    """The Win32 and COM entry points this helper uses, bound on first use."""

    def __init__(self) -> None:
        from ctypes import wintypes  # pylint: disable=import-outside-toplevel

        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.oleacc = ctypes.WinDLL("oleacc")
        self.oleaut32 = ctypes.WinDLL("oleaut32")
        self.ole32 = ctypes.WinDLL("ole32")
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        self.user32.GetClipboardOwner.restype = wintypes.HWND
        self.user32.GetClipboardData.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        self.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        self.oleacc.ObjectFromLresult.argtypes = [
            ctypes.c_size_t,
            ctypes.c_char_p,
            wintypes.WPARAM,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.oleaut32.SysAllocString.restype = ctypes.c_void_p
        self.oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
        self.oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]

    def window_pid(self, hwnd: int) -> int:
        """The process that owns `hwnd`."""
        value = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(value))
        return value.value

    def ie_controls(self, dialog: int, pid: int) -> list[int]:
        """Every `Internet Explorer_Server` window under `dialog` that belongs to `pid`."""
        found = []

        def visit(hwnd, _lparam):
            name = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, name, 256)
            if name.value == IE_CLASS and self.window_pid(hwnd) == pid:
                found.append(hwnd)
            return True

        self.user32.EnumChildWindows(dialog, self.enum_proc(visit), 0)
        return found

    def document(self, control: int) -> ctypes.c_void_p:
        """The control's IHTMLDocument2; a hung UI thread gives up after SEND_TIMEOUT_MS."""
        message = self.user32.RegisterWindowMessageW("WM_HTML_GETOBJECT")
        result = ctypes.c_size_t()
        if not self.user32.SendMessageTimeoutW(
            control, message, 0, 0, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, ctypes.byref(result)
        ):
            raise CollectError("Desktop UI thread did not answer")
        document = ctypes.c_void_p()
        if self.oleacc.ObjectFromLresult(result.value, IID_IHTMLDOCUMENT2, 0, ctypes.byref(document)) or not document:
            raise CollectError("no HTML document")
        return document

    def call(self, target, name: str, flags: int = DISPATCH_PROPERTYGET, arg: str | int | None = None) -> _Variant:
        """IDispatch late binding: GetIDsOfNames (vtable 5), then Invoke (vtable 6); a failing HRESULT raises."""
        table = ctypes.cast(target, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        names = (ctypes.c_wchar_p * 1)(name)
        dispid = ctypes.c_long()
        null_iid = (ctypes.c_byte * 16)()
        ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
        )(table[5])(target, null_iid, names, 1, 0, ctypes.byref(dispid))
        bstr = self.oleaut32.SysAllocString(arg) if isinstance(arg, str) else None
        argument = _Variant(vt=VT_I4 if isinstance(arg, int) else VT_BSTR)
        argument.val[0] = (bstr or 0) if isinstance(arg, str) else (arg or 0)
        params = _DispParams(args=ctypes.addressof(argument), count=1) if arg is not None else _DispParams()
        result = _Variant()
        invoke = ctypes.WINFUNCTYPE(
            ctypes.HRESULT,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_ushort,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(table[6])
        try:
            invoke(target, dispid, null_iid, 0, flags, ctypes.byref(params), ctypes.byref(result), None, None)
        finally:
            if bstr:
                self.oleaut32.SysFreeString(bstr)
        return result

    def text(self, value: _Variant) -> str:
        """A BSTR result as str (empty for any other type), freeing the BSTR."""
        if value.vt != VT_BSTR or not value.val[0]:
            return ""
        try:
            return ctypes.wstring_at(value.val[0])
        finally:
            self.oleaut32.SysFreeString(value.val[0])

    def copy_link(self, document: ctypes.c_void_p) -> ctypes.c_void_p:
        """The Copy details span; the IE7-mode document has no querySelector, so spans are scanned."""
        spans = ctypes.c_void_p(self.call(document, "getElementsByTagName", DISPATCH_METHOD, "span").val[0])
        for index in range(self.call(spans, "length").val[0] & 0xFFFFFFFF):
            span = ctypes.c_void_p(self.call(spans, "item", DISPATCH_METHOD, index).val[0])
            if COPY_CLASS in self.text(self.call(span, "className")).split():
                return span
        raise CollectError("no Copy details link")

    def clipboard(self) -> tuple[int, int, str]:
        """(sequence number, owner PID, CF_UNICODETEXT) read in ONE OpenClipboard session."""
        for _ in range(40):
            if self.user32.OpenClipboard(None):
                break
            time.sleep(0.025)
        else:
            raise CollectError("clipboard busy")
        try:
            sequence = self.user32.GetClipboardSequenceNumber()
            owner = self.user32.GetClipboardOwner()
            handle = self.user32.GetClipboardData(CF_UNICODETEXT)
            pointer = self.kernel32.GlobalLock(handle) if handle else None
            if not pointer:
                raise CollectError("clipboard holds no text")
            try:
                text = ctypes.wstring_at(pointer)
            finally:
                self.kernel32.GlobalUnlock(handle)
            return sequence, self.window_pid(owner) if owner else 0, text
        finally:
            self.user32.CloseClipboard()


def collect(pid: int, dialog: int) -> dict:
    """Click Copy details in `dialog` of `pid` and return what the clipboard then holds."""
    native = _Native()
    native.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    controls = native.ie_controls(dialog, pid)
    if len(controls) != 1:
        raise CollectError("no IE control" if not controls else "several IE controls")
    document = native.document(controls[0])
    body = ctypes.c_void_p(native.call(document, "body").val[0])
    message = next(
        (line.strip() for line in native.text(native.call(body, "innerText")).splitlines() if line.strip()), ""
    )
    link = native.copy_link(document)
    before = native.user32.GetClipboardSequenceNumber()
    native.call(link, "click", DISPATCH_METHOD)
    after, owner_pid, text = native.clipboard()
    return {"message": message, "text": text, "sequence_changed": after != before, "owner_pid": owner_pid}


def error_code(text: str) -> str | None:
    """The first non-empty top-level or inner PowerBINonFatalError_ErrorCode labelled value."""
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if ERROR_CODE_LABEL.fullmatch(line):
            value = next((later for later in lines[index + 1 :] if later), "")
            if value and not value.endswith(":"):
                return value
    return None


def _read_details(pid: str, token: str, hwnd: int) -> dict:
    """Run the bounded native child and return only text attributed to the held Desktop."""
    command = [sys.executable, str(DETAILS_HELPER), pid, str(hwnd)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=COLLECT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CollectError("collector timed out; Desktop left as is") from exc
    except OSError as exc:
        raise CollectError(f"collector could not start ({type(exc).__name__}); Desktop left as is") from exc
    try:
        result = json.loads(done.stdout)
    except ValueError:
        result = None
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        reason = result.get("error") if isinstance(result, dict) else None
        raise CollectError(f"could not collect ({sanitize_title(reason) or 'collector failed'})")
    if result.get("sequence_changed") is not True or result.get("owner_pid") != int(pid):
        raise CollectError("clipboard was not written by this Desktop; nothing written")
    try:
        live_token = process_start(int(pid))
    except OSError as exc:
        raise CollectError("the Desktop identity could not be read after collection; nothing written") from exc
    if live_token != token:
        raise CollectError("the Desktop changed during collect; nothing written")
    if not result["text"].strip():
        raise CollectError("empty clipboard text; nothing written")
    return result


def collect_command(  # pylint: disable=too-many-return-statements
    pid: str, token: str | None, out: Path | None = None
) -> int:
    """Collect once: 0 collected, 3 no dialog, 2 refused. Without `out`, print details to stderr."""
    stream = sys.stderr if out is None else sys.stdout
    prefix = "DESKTOP_DETAILS" if out is None else "OPEN_DESKTOP"

    def emit(message: str) -> None:
        emit_line(stream, f"{prefix}: {message}")

    if out is not None and not out.parent.is_dir():
        emit("ERROR the --out folder does not exist; nothing collected")
        return 2
    if token is None:
        emit("ERROR process-start identity unavailable; nothing collected")
        return 2
    try:
        observation = observe(pid, expected_process_start=token)
    except (ValueError, OSError) as exc:
        emit(f"ERROR invalid or unreadable collection identity ({type(exc).__name__}); nothing collected")
        return 2
    state = observation.state
    if observation.process_gone == "identity_changed":
        emit("ERROR the PID is not the Desktop that token names; nothing collected")
        return 2
    if state == "UNKNOWN":
        hint = "; restore the Desktop window and retry" if observation.reason == "minimized" else ""
        emit(f"ERROR could not read the windows ({observation.reason}); nothing collected{hint}")
        return 2
    if state != "HUMAN_ACTION_REQUIRED":
        emit(f"no dialog to collect (state={state})")
        return 3
    try:
        result = _read_details(pid, token, observation.dialog.window.hwnd)
    except CollectError as exc:
        emit(f"ERROR {exc}")
        return 2
    code = error_code(result["text"])
    summary = (
        f'collect pid={pid} title="{observation.dialog.title}" error_code={sanitize_title(code) if code else "none"}'
    )
    if out is None:
        emit(summary)
        for line in result["text"].splitlines():
            emit(line)
    else:
        try:
            out.write_text(result["text"], encoding="utf-8", newline="")
        except OSError as exc:
            emit(f"ERROR cannot write --out ({type(exc).__name__})")
            return 2
        emit(f"{summary} out={out}")
        emit_line(stream, f"message: {sanitize_title(result.get('message'))}")
    return 0


def main(argv: list[str]) -> int:
    """Print one JSON line; the exit code carries nothing, the parent judges the line."""
    try:
        pid, dialog = (int(value) for value in argv)
        result = collect(pid, dialog)
    except CollectError as exc:
        result = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        result = {"error": type(exc).__name__}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
