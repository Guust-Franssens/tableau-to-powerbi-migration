"""
purpose: read the error Power BI Desktop is showing on screen - as TEXT - and hand it to an agent.
         Desktop has the best diagnostics in the whole toolchain; every CLI around it throws them away.

Why this exists (measured 2026-07-31, Desktop 2.157.480.0)
---------------------------------------------------------
Open a PBIP whose TMDL has a duplicated property. Ask the tooling what happened:

  * `powerbi-desktop open`   -> `"status":"launched"`, `"bridgeStatus":"connected"`, **exit 0**
  * `powerbi-desktop status` -> `"Host is not ready to accept operations"`, `BRIDGE_ERROR`,
                                **`retryable: true`**, `retryAfterMs: 2000`, `details: {}`
  * `powerbi-report-author validate` -> n/a, it checks the Report item, not the model
  * `check_m_syntax.py` / `check_tmdl.py` -> catch some classes, by construction not all

So the one tool that can see Desktop reports a PERMANENT failure as a RETRYABLE one with empty
details - the exact shape of the incident this repo documents (129 minutes / 298 tool calls of an
agent retrying something that could never succeed). Meanwhile Desktop is displaying:

    TMDL Format Error:
        Parsing error type - DuplicatedProperty
        Detailed error - Duplicated property - formatString appears more then once...
        Document - './tables/Shipments'
        Line Number - 8
        Line - '        formatString: 0.00%'

...naming the file, the line and the offending text. This script retrieves that, as text.

How (the part that is not obvious)
----------------------------------
The dialog body is NOT in the UI Automation tree. Measured, in order:
  * `TreeScope::Children` of the desktop root does not find the dialog at all - it is an OWNED
    window, so `Descendants` is required;
  * once found, UIA's default CONTROL view exposes nothing but a stray "Report Zoomed To 89%";
  * the RAW `TreeWalker` exposes 5 elements - the window and four panes - and no message text;
  * `SetFocus()` on the dialog is refused ("Target element cannot receive focus"), so bringing it
    to the foreground does not materialise the content either.

The reason: the body is hosted in a legacy **`Internet Explorer_Server`** (MSHTML) control, whose DOM
never projects into UIA. It IS reachable the classic way - `WM_HTML_GETOBJECT` + `ObjectFromLresult`
-> `IHTMLDocument2`. From there `body.innerText` gives the message, and the "Copy details to
clipboard" element (a `<SPAN>` - not a link, not a button, which is why control-type searches return
nothing) can be `.click()`ed for the full payload: product version, session id, error message AND the
.NET stack trace.

Read-only by design. This does NOT type credentials into Desktop and must not be extended to do so:
a secret belongs to the human, and handing one to an agent is a worse problem than the one being
solved here.

usage:   python scripts/dump_desktop_error.py                      # all Desktop instances
         python scripts/dump_desktop_error.py --pid 31976
         python scripts/dump_desktop_error.py --watch 120          # poll until a dialog appears
         python scripts/dump_desktop_error.py --no-copy            # skip the clipboard payload
         python scripts/dump_desktop_error.py --shot-dir out/      # also save a PNG
         python scripts/dump_desktop_error.py --json

Exit 0 = no error dialog found. Exit 3 = an error dialog was found (its text is on stdout).
Windows-only by necessity, like scripts/probe_desktop_credential.ps1.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("dump_desktop_error")

# Titles that mean "Desktop is blocked, telling the user something". Power BI funnels the whole
# PBIP-load failure family through one dialog ("Issues were found"); the rest are defensive.
ERROR_TITLES = ("issues were found", "error", "problem", "unable", "cannot", "couldn't", "failed")

_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PbiNative {
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern uint RegisterWindowMessage(string s);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern IntPtr SendMessageTimeout(
      IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out IntPtr res);
  [DllImport("oleacc.dll", PreserveSig=false)] public static extern void ObjectFromLresult(
      IntPtr lResult, ref Guid riid, IntPtr wParam, [MarshalAs(UnmanagedType.IUnknown)] out object ppv);
}
"@
$auto = [System.Windows.Automation.AutomationElement]
$root = $auto::RootElement
$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
$out = @()

function Read-Prop($o, $name) { return $o.GetType().InvokeMember($name, 'GetProperty', $null, $o, $null) }

foreach ($procId in @(__PIDS__)) {
  $pcond = New-Object System.Windows.Automation.PropertyCondition($auto::ProcessIdProperty, $procId)
  $wcond = New-Object System.Windows.Automation.PropertyCondition($auto::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
  $and   = New-Object System.Windows.Automation.AndCondition($pcond, $wcond)
  foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $and)) {
    $title = $w.Current.Name
    if (-not $title) { continue }

    $ieh = [IntPtr]::Zero
    $stack = New-Object System.Collections.Stack
    $stack.Push($w)
    while ($stack.Count -gt 0) {
      $el = $stack.Pop()
      if ($el.Current.ClassName -eq 'Internet Explorer_Server') { $ieh = [IntPtr]$el.Current.NativeWindowHandle }
      $c = $walker.GetFirstChild($el)
      while ($c -ne $null) { $stack.Push($c); $c = $walker.GetNextSibling($c) }
    }

    $bodyText = ''
    $copied = ''
    if ($ieh -ne [IntPtr]::Zero) {
      $msg = [PbiNative]::RegisterWindowMessage('WM_HTML_GETOBJECT')
      $res = [IntPtr]::Zero
      [void][PbiNative]::SendMessageTimeout($ieh, $msg, [IntPtr]::Zero, [IntPtr]::Zero, 2, 3000, [ref]$res)
      if ($res -ne [IntPtr]::Zero) {
        $iid = [Guid]'626FC520-A41E-11CF-A731-00A0C9082637'
        $doc = $null
        [PbiNative]::ObjectFromLresult($res, [ref]$iid, [IntPtr]::Zero, [ref]$doc)
        if ($doc) {
          try { $bodyText = Read-Prop (Read-Prop $doc 'body') 'innerText' } catch { }
          if (__DOCOPY__) {
            try {
              # Iterate with foreach, NOT document.all.item(i): PowerShell marshals the COM
              # collection to an Object[] in some contexts, and `.length` then fails on it.
              foreach ($e in (Read-Prop $doc 'all')) {
                $t = ''
                try { $t = Read-Prop $e 'innerText' } catch { }
                # It is a <SPAN> - not an <a>, not a <button> - so match on text, not tag/control type.
                if ($t -and $t.Trim() -match '(?i)^copy details') {
                  [System.Windows.Forms.Clipboard]::Clear()
                  $e.GetType().InvokeMember('click', 'InvokeMethod', $null, $e, $null) | Out-Null
                  Start-Sleep -Milliseconds 1200
                  $copied = [System.Windows.Forms.Clipboard]::GetText()
                  break
                }
              }
            } catch { }
          }
        }
      }
    }

    $shot = ''
    if ('__SHOTDIR__' -ne '') {
      try {
        $r = $w.Current.BoundingRectangle
        if ($r.Width -gt 0 -and $r.Height -gt 0) {
          $bmp = New-Object System.Drawing.Bitmap([int]$r.Width, [int]$r.Height)
          $g = [System.Drawing.Graphics]::FromImage($bmp)
          $g.CopyFromScreen([int]$r.X, [int]$r.Y, 0, 0, $bmp.Size)
          $shot = Join-Path '__SHOTDIR__' ("desktop-dialog-$procId-" + ($title -replace '[^A-Za-z0-9]', '_') + ".png")
          $bmp.Save($shot, [System.Drawing.Imaging.ImageFormat]::Png)
          $g.Dispose(); $bmp.Dispose()
        }
      } catch { $shot = '' }
    }

    $out += [pscustomobject]@{
      pid = $procId; window = $title; hasHtmlHost = ($ieh -ne [IntPtr]::Zero)
      text = $bodyText; details = $copied; screenshot = $shot
    }
  }
}
$out | ConvertTo-Json -Depth 5 -Compress
"""


def desktop_pids() -> list[int]:
    """Every running Power BI Desktop process id."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "(Get-Process PBIDesktop -ErrorAction SilentlyContinue).Id"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(x) for x in proc.stdout.split() if x.strip().isdigit()]


def dump(pids: list[int], copy_details: bool = True, shot_dir: str = "") -> list[dict]:
    """Return one record per Desktop window, with the dialog text read out of the MSHTML host."""
    if not pids:
        return []
    script = (
        _PS.replace("__PIDS__", ",".join(str(p) for p in pids))
        .replace("__DOCOPY__", "$true" if copy_details else "$false")
        .replace("__SHOTDIR__", shot_dir or "")
    )
    tmp = Path(tempfile.gettempdir()) / f"pbi_error_dump_{os.getpid()}_{int(time.time() * 1000)}.ps1"
    tmp.write_text(script, encoding="utf-8")
    try:
        # -STA is required: both the clipboard and the MSHTML COM object need a single-threaded apartment.
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        tmp.unlink(missing_ok=True)
    out = proc.stdout.strip()
    if not out:
        return []
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        log.warning("could not parse the dump: %s", out[:300])
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def looks_like_error(record: dict) -> bool:
    """True when this window is Desktop reporting a problem."""
    title = (record.get("window") or "").lower()
    if any(t in title for t in ERROR_TITLES):
        return True
    # A non-main window hosting MSHTML is, in practice, an error/frown dialog.
    return bool(record.get("hasHtmlHost")) and "power bi desktop" not in title


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pid", type=int, action="append", help="Desktop process id (repeatable; default: all)")
    ap.add_argument("--json", action="store_true", help="emit the raw structured dump")
    ap.add_argument("--all-windows", action="store_true", help="include windows that do not look like errors")
    ap.add_argument("--no-copy", action="store_true", help="do not click 'Copy details' / touch the clipboard")
    ap.add_argument("--shot-dir", default="", help="also save a PNG of each matched dialog here")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="poll until an error appears")
    args = ap.parse_args(argv)

    deadline = time.time() + args.watch
    records: list[dict] = []
    errors: list[dict] = []
    while True:
        pids = args.pid or desktop_pids()
        if not pids:
            log.info("No Power BI Desktop process is running.")
            return 0
        records = dump(pids, copy_details=not args.no_copy, shot_dir=args.shot_dir)
        errors = [r for r in records if looks_like_error(r)]
        if errors or time.time() >= deadline:
            break
        time.sleep(3)

    shown = records if args.all_windows else errors
    if args.json:
        print(json.dumps(shown, indent=2))
        return 3 if errors else 0

    if not shown:
        log.info("No error dialog found across %d Desktop window(s).", len(records))
        return 0

    for rec in shown:
        log.info("=" * 78)
        log.info("pid %s   %s", rec.get("pid"), rec.get("window") or "<unnamed>")
        log.info("=" * 78)
        text = (rec.get("text") or "").strip()
        if text:
            for line in text.splitlines():
                if line.strip():
                    log.info("  %s", line.rstrip())
        elif not rec.get("hasHtmlHost"):
            log.info("  (no MSHTML host in this window - nothing to read)")
        details = (rec.get("details") or "").strip()
        if details:
            log.info("")
            log.info("  ---- COPY DETAILS payload (version, session id, message, STACK TRACE) ----")
            for line in details.splitlines():
                log.info("  %s", line.rstrip())
        if rec.get("screenshot"):
            log.info("  screenshot: %s", rec["screenshot"])
    log.info("")
    log.info(
        "An open Desktop error dialog means the bridge's `retryable: true` / 'Host is not ready to "
        "accept operations' is a PERMANENT condition. Do NOT retry - fix what the dialog says."
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
