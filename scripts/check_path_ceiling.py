"""
purpose: answer "would this bundle survive on a STOCK Windows machine?" - report every produced path
         against the ceiling Power BI Desktop itself enforces, independently of the host's registry.
usage:   python scripts/check_path_ceiling.py <bundle-or-any-dir> [...]
                                              [--json <file>] [--quiet] [--warn-only]
                                              [--ceiling N] [--dir-ceiling N] [--warn-at N]
                                              [--min-root-budget N]

Why this exists
---------------
Every bundle this toolkit produces is a SHIPPED artifact, and Power BI Desktop refuses to open one
whose deepest path crosses a limit it enforces ITSELF. Issue #235 measured 93 files already over 260
characters in bundles on this machine; the 52-unit estate run of 2026-08-29 has 183.

The mechanism, and why `LongPathsEnabled` is a red herring for Desktop
----------------------------------------------------------------------
Desktop's failure is NOT an OS error it inherited. It is its own managed guard, named in the crash
report a failing open produced:

    Microsoft.PowerBI.Packaging.Project.PBIProjectUtils.EnsureNotLong(String path, Boolean isFolder)
      at Microsoft.PowerBI.Client.Windows.Services.DiskProjectFilesReader.<GetAsync>d__2.MoveNext()

surfaced as `FilePathTooLongError`, wrapped in `Error Reading StorageSection: ReportDocument`:

    The specified path, file name, or both are too long. The fully qualified file name must be
    less than 260 characters, and the directory name must be less than 248 characters.

`EnsureNotLong` is a length comparison in Desktop's own code, executed before any filesystem call.
So the Windows opt-in

    HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem  ->  LongPathsEnabled = 1   (default is 0)

**cannot help, and does not**: every measurement below was taken on a machine with that value set to
1, and Desktop refused anyway. This is materially worse than the issue's original worst case. It is
not "customers on stock Windows are at risk" - it is **every consumer on every machine, including
ours**, regardless of registry configuration. The registry setting only ever governed whether our
*generator* could WRITE these paths (Python 3.6+ declares `longPathAware`, so here it can), which is
precisely why the defect was invisible: we could produce artifacts we could never open.

The measurements (2026-08-29, this worktree, LongPathsEnabled = 1 throughout)
-----------------------------------------------------------------------------
1. STATIC - the embedded Win32 application manifest (RT_MANIFEST, id 1) of `PBIDesktop.exe`
   2.157.828.0 contains NO `longPathAware` element. Neither does `msmdsrv.exe` (the Analysis
   Services engine that loads the model) nor `Microsoft.Mashup.Container.NetFX45.exe` (the M engine
   that reads source files). Controls for the extraction harness: `python.exe` -> True (element
   present and found), `explorer.exe` -> False.

   Static evidence alone is NOT conclusive, and this module does not pretend otherwise: a program can
   also opt in per-call by prefixing `\\\\?\\`. Measured here, `node.exe` and `pwsh.exe` both report
   manifest `longPathAware=False` yet read a 262-character path successfully, because libuv and
   .NET Core prepend the prefix themselves. So the manifest was a hypothesis; `EnsureNotLong` above
   is the actual mechanism, and the A/B below is the proof.

2. LIVE A/B, BOUNDARY-PINNED - byte-identical copies of `examples/shipping-kpis/fabric` (27 files)
   differing ONLY in the length of their root, each opened in Power BI Desktop and then probed with
   the Desktop Bridge (a second, independent instrument - a window title is a loading state before
   it is a verdict, the bridge is not):

   | copy    | deepest file | deepest dir | window title at t+200s | bridge `status --pid`        |
   |---------|-------------:|------------:|------------------------|------------------------------|
   | control |          200 |         188 | `ShippingKPIs`         | (not probed)                 |
   | F259    |      **259** |     **247** | `ShippingKPIs`         | `ready` / `connected`, pages |
   | S260    |      **260** |     **248** | `Untitled - Power BI D.`| `error`: "Host is not ready" |

   Entry-point `.pbip` paths were 119 / 178 / 179 characters - far below any limit - so the failure
   is attributable to a deep child file and to nothing else. S260 raised a modal "Issues were found"
   dialog naming the exact `visual.json`.

   This pins BOTH ceilings at their exact boundary in one pair, because the PBIR layout places the
   deepest directory exactly 12 characters below the deepest file, and 260 - 248 is also 12. F259
   proves 259/247 are legal; S260 proves 260/248 are not.

Hence the two ceilings this module enforces - measured, not inferred:

    FILE_CEILING = 259    legal at 259, refused at 260  ("must be less than 260 characters")
    DIR_CEILING  = 247    legal at 247, refused at 248  ("directory name must be less than 248")

⚠️ Because the S260 fixture crosses BOTH boundaries at once (file 260 AND directory 248), that single
A/B cannot attribute which of the two guards fired. F259 opening proves both 259 and 247 are legal,
which is what a gate needs; separating the two guards would need a fixture the PBIR layout cannot
produce (see "What it will NOT tell you").

A note on why both, and why including the directory rule adds no false-alarm surface: in a real PBIR
tree the deepest file is `visual.json`, whose `\\visual.json` tail is exactly 12 characters, and
260 - 248 is also exactly 12. The two rules therefore bite at the same point for the most numerous
and deepest file we emit - which is also the shape that actually fails in the wild:

    <unit>\\<unit>.Report\\definition\\pages\\<page-id>\\visuals\\<visual-id>\\visual.json

The directory rule only becomes the stricter of the two for shorter names (`page.json`, `.platform`),
which is precisely the case a file-only check would miss.

Why this cannot silently inherit LongPathsEnabled=1
---------------------------------------------------
The verdict is computed ARITHMETICALLY from path strings. This module never asks the operating
system whether a path can be opened, so there is no code path by which the host's registry setting,
or the host's OS, can soften the answer. The registry value IS read and printed - because issue #235
exists entirely because nobody printed it - but it is reported as context, is never an input to the
verdict, and is labelled in the output as not affecting Desktop at all. Running on Linux CI produces
the same numbers for the same tree.

The portable number, and why it matters more than the absolute one
------------------------------------------------------------------
"93 files over 260" is a fact about where the bundle happens to sit on THIS disk. The customer will
put it somewhere else. So the headline number here is the longest TAIL - the path relative to the
bundle root - and the root budget it leaves:

    root_budget = FILE_CEILING - longest_tail

That is the longest install root the bundle can tolerate before Power BI Desktop refuses it. A
customer unpacking to `C:\\Users\\<name>\\Documents\\migrations\\` consumes about 40 characters
before the bundle contributes anything, so a root budget under ~40 is a shipping hazard even when
every absolute path on the build machine measures clean. `--min-root-budget N` turns that judgement
into a gate; it is opt-in because the reasonable value depends on where the customer unpacks.

What is NEVER silently passed
-----------------------------
A path this module cannot measure - an unreadable directory, a name that will not decode - is
counted and listed as `unknown` and forces a non-zero exit. Unassessable input never lands in the
clean bucket.

What it will NOT tell you
-------------------------
* Which of the two guards fired. The PBIR layout cannot produce a tree whose deepest directory
  breaks 247 while every file stays within 259 (the `\\visual.json` tail is 12, and so is the gap
  between the rules), so the two were pinned together rather than separated.
* Whether the classic per-machine Power BI Desktop installer behaves identically. Everything above
  was measured against **2.157.828.0, the MSIX / Microsoft Store package** (`...\\Power BI Desktop
  Store App\\...`). `EnsureNotLong` is managed code shared by both, so a difference would be
  surprising - but surprising is not measured.
* Whether the archive step (zip/7z) has its own, lower budget. 282 remains an unreproduced anecdote
  and this module does not encode it; pass `--ceiling 282` explicitly if you want to test it.
* Whether a path that fits will open. Length is one failure mode among many.
* Anything about a name a customer has not chosen yet. The engine emits the workbook name twice in
  the PBIP path, so a longer workbook name moves every number here; the root budget is the figure
  that survives that change, which is why it is reported.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

# Both pinned by live A/B against Power BI Desktop 2.157.828.0 (see the module docstring): a PBIP
# whose deepest file was 259 chars / deepest directory 247 OPENED and answered the Desktop Bridge;
# one character longer on each (260 / 248) was REFUSED. Desktop's own `EnsureNotLong` guard states
# the rule as "less than 260" / "less than 248", so these are the longest legal values.
FILE_CEILING = 259
DIR_CEILING = 247

# Advisory only. Suggested on issue #235 by a customer whose bundle measured 258 of 260 on a stock
# machine: the margin is consumed by where the bundle lands, which the generator cannot see.
DEFAULT_WARN_AT = 240

REPORT_VERSION = 1

# How many offenders to name in the report. Naming the file is the whole point (issue #235 asked for
# it explicitly), but a bundle over the ceiling is usually over it in bulk.
WORST_N = 5

STATUS_OK = "ok"
STATUS_OVER_CEILING = "over_ceiling"
STATUS_UNKNOWN_PATHS = "unknown_paths"
STATUS_NO_PATHS = "no_paths"

EXIT_OK = 0
EXIT_OVER_CEILING = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 3

KIND_FILE = "file"
KIND_DIR = "directory"


class Limits(NamedTuple):
    """The thresholds one scan is judged against.

    Grouped rather than passed loose so the defaults are stated in exactly one place and a caller
    cannot silently supply four positional numbers in the wrong order.
    """

    file_ceiling: int = FILE_CEILING
    dir_ceiling: int = DIR_CEILING
    warn_at: int = DEFAULT_WARN_AT
    min_root_budget: int | None = None


DEFAULT_LIMITS = Limits()


def read_long_paths_enabled() -> int | None:
    """Return the host's LongPathsEnabled registry value, or None when it cannot be read.

    Reported for context only - see the module docstring. It is deliberately NOT an input to any
    verdict, so a None here (Linux CI, a locked-down registry) costs nothing.
    """
    if sys.platform != "win32":
        return None
    try:
        # `winreg` is Windows-only, so pylint on the Linux CI runner cannot resolve it. Measured
        # 2026-08-29 with a controlled experiment (a Unix-only `import posix` linted on Windows):
        # pylint emits E0401 for a stdlib module absent from the linting platform, and only the
        # explicit suppression silences it. `useless-suppression` is not enabled in this repo, so
        # the suppression costs nothing on Windows where the import does resolve.
        import winreg  # pylint: disable=import-outside-toplevel,import-error

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        return int(value)
    except (OSError, ValueError, TypeError):
        return None


def _tail_length(full: str, root: str) -> int:
    """Length the path contributes on top of its root, including the leading separator."""
    return len(full) - len(root)


def collect(root: Path) -> tuple[list[dict], list[dict]]:
    """Walk `root`, returning (measured, unknown).

    `measured` carries one record per file and per directory. `unknown` carries every entry the walk
    could not measure; those are never folded into `measured`.
    """
    root_str = str(root)
    measured: list[dict] = []
    unknown: list[dict] = []

    def on_error(exc: OSError) -> None:
        unknown.append(
            {
                "path": str(getattr(exc, "filename", "") or root_str),
                "reason": f"{type(exc).__name__}: {exc.strerror or exc}",
            }
        )

    for dirpath, dirnames, filenames in os.walk(root_str, onerror=on_error):
        for name, kind in [(d, KIND_DIR) for d in dirnames] + [(f, KIND_FILE) for f in filenames]:
            try:
                full = os.path.join(dirpath, name)
                length = len(full)
            except (ValueError, UnicodeError) as exc:  # pragma: no cover - defensive
                unknown.append({"path": f"{dirpath}<undecodable>", "reason": f"{type(exc).__name__}: {exc}"})
                continue
            measured.append(
                {
                    "path": full,
                    "kind": kind,
                    "length": length,
                    "tail": _tail_length(full, root_str),
                }
            )
    return measured, unknown


def _ceiling_for(record: dict, limits: Limits) -> int:
    return limits.dir_ceiling if record["kind"] == KIND_DIR else limits.file_ceiling


def scan(root: Path, limits: Limits = DEFAULT_LIMITS) -> dict:
    """Measure one target tree and return its machine-readable report."""
    measured, unknown = collect(root)

    over = [
        dict(record, ceiling=_ceiling_for(record, limits))
        for record in measured
        if record["length"] > _ceiling_for(record, limits)
    ]
    over.sort(key=lambda r: r["length"], reverse=True)

    near = [r for r in measured if limits.warn_at < r["length"] <= _ceiling_for(r, limits)]

    longest = max(measured, key=lambda r: r["length"], default=None)
    longest_tail = max((r["tail"] for r in measured), default=0)
    # The portable number: how long an install root this tree can still tolerate. Uses the FILE
    # ceiling because the longest tail in a PBIP tree is always a file.
    root_budget = limits.file_ceiling - longest_tail if measured else None

    if over:
        status = STATUS_OVER_CEILING
    elif unknown:
        status = STATUS_UNKNOWN_PATHS
    elif not measured:
        status = STATUS_NO_PATHS
    elif limits.min_root_budget is not None and root_budget is not None and root_budget < limits.min_root_budget:
        status = STATUS_OVER_CEILING
    else:
        status = STATUS_OK

    return {
        "version": REPORT_VERSION,
        "root": str(root),
        "root_length": len(str(root)),
        "status": status,
        "host_long_paths_enabled": read_long_paths_enabled(),
        "host_platform": sys.platform,
        "file_ceiling": limits.file_ceiling,
        "dir_ceiling": limits.dir_ceiling,
        "warn_at": limits.warn_at,
        "min_root_budget": limits.min_root_budget,
        "counted": {
            "measured": len(measured),
            "files": sum(1 for r in measured if r["kind"] == KIND_FILE),
            "directories": sum(1 for r in measured if r["kind"] == KIND_DIR),
            "over_ceiling": len(over),
            "near_ceiling": len(near),
            "unknown": len(unknown),
        },
        "longest": None if longest is None else {k: longest[k] for k in ("path", "kind", "length", "tail")},
        "longest_tail": longest_tail if measured else None,
        "root_budget": root_budget,
        "worst_offenders": [{k: r[k] for k in ("path", "kind", "length", "ceiling")} for r in over[:WORST_N]],
        "near_ceiling_paths": [
            {k: r[k] for k in ("path", "kind", "length")} for r in sorted(near, key=lambda r: -r["length"])[:WORST_N]
        ],
        "unknown_paths": unknown[:WORST_N],
    }


def _registry_line(report: dict) -> str:
    value = report["host_long_paths_enabled"]
    suffix = "does NOT affect Power BI Desktop, which enforces its own limit in managed code"
    if value is None:
        detail = "unreadable" if report["host_platform"] == "win32" else f"not applicable ({report['host_platform']})"
        return f"  host LongPathsEnabled : {detail} - {suffix}"
    note = "Windows default" if value == 0 else "NON-DEFAULT"
    return f"  host LongPathsEnabled : {value} ({note}) - {suffix}"


def render(report: dict) -> str:
    """Human-readable report for one target."""
    counted = report["counted"]
    lines = [
        f"{report['status'].upper()}: {report['root']}",
        _registry_line(report),
        f"  ceilings              : file <= {report['file_ceiling']} (Desktop refuses"
        f" {report['file_ceiling'] + 1}), directory <= {report['dir_ceiling']} (refuses"
        f" {report['dir_ceiling'] + 1}) - measured, see module docstring",
        f"  measured              : {counted['measured']} paths"
        f" ({counted['files']} files, {counted['directories']} directories)",
    ]
    longest = report["longest"]
    if longest:
        lines.append(f"  longest path          : {longest['length']} chars - {longest['path']}")
    if report["root_budget"] is not None:
        lines.append(
            f"  longest tail          : {report['longest_tail']} chars"
            f"  ->  root budget {report['root_budget']} chars"
            " (longest install root this bundle tolerates)"
        )
    if counted["over_ceiling"]:
        lines.append(f"  OVER CEILING          : {counted['over_ceiling']} path(s)")
        for record in report["worst_offenders"]:
            lines.append(f"      {record['length']:>4} > {record['ceiling']}  [{record['kind']}] {record['path']}")
    if counted["near_ceiling"]:
        lines.append(
            f"  near ceiling (> {report['warn_at']})  : {counted['near_ceiling']} path(s) - advisory, not a finding"
        )
        for record in report["near_ceiling_paths"]:
            lines.append(f"      {record['length']:>4}        [{record['kind']}] {record['path']}")
    if counted["unknown"]:
        lines.append(f"  UNKNOWN (not passed)  : {counted['unknown']} path(s) could not be measured")
        for record in report["unknown_paths"]:
            lines.append(f"      {record['reason']}  {record['path']}")
    if report["status"] == STATUS_NO_PATHS:
        lines.append("  nothing to measure - an empty target cannot be judged clean")
    if report["min_root_budget"] is not None:
        lines.append(f"  required root budget  : {report['min_root_budget']} chars")
    return "\n".join(lines)


def _nonneg(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Report whether a produced bundle would survive on a stock Windows machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="+", type=Path, help="bundle dir(s) or any directory to measure")
    parser.add_argument("--json", type=Path, help="also write the machine-readable report here")
    parser.add_argument("--quiet", action="store_true", help="print only the verdict line")
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="report findings but always exit 0 (for an explicitly accepted snapshot)",
    )
    parser.add_argument(
        "--ceiling",
        type=_nonneg,
        default=FILE_CEILING,
        help=f"blocking full-path ceiling for files (default {FILE_CEILING}, measured against Power BI Desktop)",
    )
    parser.add_argument(
        "--dir-ceiling",
        type=_nonneg,
        default=DIR_CEILING,
        help=f"blocking full-path ceiling for directories (default {DIR_CEILING})",
    )
    parser.add_argument(
        "--warn-at",
        type=_nonneg,
        default=DEFAULT_WARN_AT,
        help=f"advisory threshold; reported, never blocking (default {DEFAULT_WARN_AT})",
    )
    parser.add_argument(
        "--min-root-budget",
        type=_nonneg,
        default=None,
        help="fail when the bundle cannot tolerate an install root at least this long (opt-in)",
    )
    args = parser.parse_args(argv)

    missing = [str(t) for t in args.targets if not t.is_dir()]
    if missing:
        print(f"ERROR: not a directory: {', '.join(missing)}", file=sys.stderr)
        return EXIT_USAGE

    reports = [
        scan(
            target.resolve(),
            Limits(
                file_ceiling=args.ceiling,
                dir_ceiling=args.dir_ceiling,
                warn_at=args.warn_at,
                min_root_budget=args.min_root_budget,
            ),
        )
        for target in args.targets
    ]
    for report in reports:
        print(render(report) if not args.quiet else f"{report['status']}: {report['root']}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = reports[0] if len(reports) == 1 else {"version": REPORT_VERSION, "roots": reports}
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.warn_only:
        return EXIT_OK
    if any(r["status"] == STATUS_OVER_CEILING for r in reports):
        return EXIT_OVER_CEILING
    if any(r["status"] in (STATUS_UNKNOWN_PATHS, STATUS_NO_PATHS) for r in reports):
        return EXIT_SKIPPED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
