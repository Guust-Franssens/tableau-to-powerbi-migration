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

The mechanism — measured from the assembly, not inferred from the error text
---------------------------------------------------------------------------
⚠️ An earlier revision of this module claimed Desktop refuses 260/248 through
`PBIProjectUtils.EnsureNotLong`. **That was wrong**, and the correction is worth keeping because it
is the same mistake in miniature that produced issue #235: reading intent off an error message
instead of measuring the thing.

`Microsoft.PowerBI.Packaging.dll` 2.157.828.0 was loaded and the method invoked directly:

    PBIProjectUtils.EnsureNotLong(string path, bool isFolder)

    FILE 258 ALLOWED   259 ALLOWED   260 ALLOWED   261 THREW PathTooLongException
    DIR  246 ALLOWED   247 ALLOWED   248 ALLOWED   249 THREW PathTooLongException

**It compares with `>`, so 260 and 248 are ALLOWED.** Its message - *"must be less than 260 ... less
than 248"* - describes intent, not the comparison it performs. Two consequences:

* The end-to-end refusal observed at file 260 / dir 248 (below) did **not** come from this guard.
  ⚠️ INFERRED, not measured: the most consistent explanation is the .NET/Win32 `MAX_PATH` limit
  applying because `PBIDesktop.exe` carries no `longPathAware` manifest entry (see the manifest
  evidence below) - which is the ORIGINAL framing, before `EnsureNotLong` was over-read into it.
* `EnsureNotLong` is nonetheless real and does fire: the crash report that first named it came from
  a 268-character path, and 268 > 260.

So Desktop has at least two independent length guards and the effective limit is the STRICTER of
them. The ceilings below are the conservative, end-to-end-validated pair - deliberately one character
tighter than the assembly's own inclusive limits.

Why `LongPathsEnabled` still cannot rescue it
---------------------------------------------
    HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem  ->  LongPathsEnabled = 1   (default is 0)

Every measurement below was taken on a machine with that value set to 1, and Desktop refused anyway.
It is not "customers on stock Windows are at risk" - it is **every consumer on every machine,
including ours**, regardless of registry configuration. The registry setting only ever governed
whether our *generator* could WRITE these paths (Python 3.6+ declares `longPathAware`, so here it
can), which is precisely why the defect was invisible: we could produce artifacts we could never
open.

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

Hence the two ceilings this module enforces - conservative by one character, and validated
end-to-end rather than derived from any single guard:

    FILE_CEILING = 259    a PBIP whose deepest file is 259 OPENED; at 260 it was REFUSED
    DIR_CEILING  = 247    the same pair's deepest directory was 247 / 248

⚠️ These are ONE TIGHTER than `EnsureNotLong`'s own inclusive limits (260 / 248, measured above).
That is deliberate: the observed end-to-end refusal at 260/248 comes from a different guard, so the
gate follows the OBSERVED boundary, not any one implementation's comparison.

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

Three consumers, three different answers, one machine — which is why "it works here" was never
evidence
--------------------------------------------------------------------------------------------------
Measured 2026-08-29/30 on this machine, `LongPathsEnabled = 1` and `core.longpaths` unset throughout:

| consumer                    | tolerates a 287-char path? | evidence                                    |
|-----------------------------|----------------------------|---------------------------------------------|
| Python 3.6+ (our generator) | **yes**                    | writes the whole estate bundle in silence    |
| Power BI Desktop            | **NO, and no setting helps**| `EnsureNotLong`; refused at file 260/dir 248 |
| git (`core.longpaths` unset)| **NO, but a setting fixes**| 0 of 179 files staged, exit 128              |

The git half is not theoretical, and it is the reason `core.longpaths` is reported next to the
registry value. A faithful copy of the three offending estate units at the same 90-character root:

    core.longpaths unset (default)   git add -A -> 74 warnings + fatal, exit 128, 0/179 staged
    core.longpaths=true              git add -A -> 0 warnings,          exit   0, 179/179 staged

⚠️ **And git's failure is not always loud.** The two failure modes differ by WHICH path is overlong:

    file  over the limit, parent directory readable -> `error: unable to index file` -> FATAL, exit 128
    DIRECTORY over the limit                        -> `warning: could not open directory` -> SKIPPED

In the second case git never sees the files inside, so it reports nothing missing. Measured on a
synthetic tree with one 265-char directory: `git add -A` exited **0**, `git commit` exited **0**,
`git status --porcelain` printed **nothing**, and **1 of 2 files was committed**. A green, silent,
content-missing commit whose only trace is a `warning:` on stderr that any script redirecting stderr
throws away. That is the strongest independent argument for gating on the DIRECTORY ceiling and not
only on file paths.

Why this cannot silently inherit LongPathsEnabled=1
---------------------------------------------------
The verdict is computed ARITHMETICALLY from path strings. This module never asks the operating
system whether a path can be opened, so there is no code path by which the host's registry setting,
its git configuration, or the host's OS can soften the answer. Both settings ARE read and printed -
because issue #235 exists entirely because nobody printed them, and because one being set while the
other was not is exactly how the defect stayed invisible - but neither is ever an input to the
verdict. Running on Linux CI produces the same numbers for the same tree.

Two questions, and the check answers both
-----------------------------------------
* *"Can this bundle be used WHERE IT IS?"* - the absolute ceiling. This is the blocking gate, because
  it is not hypothetical: at its current location the estate bundle breaks git today and Desktop
  refuses it.
* *"Can this bundle be SHIPPED?"* - the longest TAIL and the `259 - tail` root budget, which survive
  relocation. A bundle sitting at a short root can pass the absolute check and still be unshippable,
  so a tight root budget is called out on every run (`TIGHT ROOT BUDGET`) and `--min-root-budget N`
  turns it into a gate.

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
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

# Conservative by one character, and validated END-TO-END rather than taken from any single guard.
# A PBIP whose deepest file was 259 / deepest directory 247 opened and answered the Desktop Bridge;
# one character longer on each was refused. Measured separately, Desktop's own
# `PBIProjectUtils.EnsureNotLong` compares with `>` and ALLOWS 260 / 248 - so the observed refusal
# comes from a different guard, and the gate follows the observation, not that method.
FILE_CEILING = 259
DIR_CEILING = 247

# Advisory only. Suggested on issue #235 by a customer whose bundle measured 258 of 260 on a stock
# machine: the margin is consumed by where the bundle lands, which the generator cannot see.
DEFAULT_WARN_AT = 240

# Advisory only, and deliberately derived rather than invented: `C:\\Users\\<name>\\Documents\\` is
# already ~28 characters before a customer creates a single folder, so a bundle that cannot tolerate
# a 40-character install root is a shipping hazard even when every absolute path measures clean where
# it was built. For contrast, the root this repo's own estate run occupies is 90 characters
# (`...\\tableau-to-pbi-migration\\_runs\\estate-2.339.0-20260829`) - measured, not assumed.
SHIPPING_ROOT_BUDGET_ADVISORY = 40

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


def read_git_long_paths(cwd: Path | None = None) -> bool | None:
    """Return the effective `git config core.longpaths`, or None when it cannot be determined.

    Reported for context alongside the registry value, and for the same reason: issue #235 stayed
    invisible because one opt-in was set and the other was not, so "it works here" was never
    evidence. Measured 2026-08-29 on this machine, with `core.longpaths` unset (its default):
    `git add -A` on a real 3-unit slice of the estate bundle staged **0 of 179** files and exited
    128 (`fatal: adding files failed`); with `core.longpaths=true` the same tree staged 179 of 179.

    Like the registry value, this NEVER feeds the verdict - git and Power BI Desktop disagree about
    long paths, and Desktop's answer is the one a shipped bundle has to survive.
    """
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "core.longpaths"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = proc.stdout.strip().lower()
    if proc.returncode != 0 or not value:
        return False  # unset is git's documented default: long paths NOT enabled
    return value in {"true", "yes", "on", "1"}


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


def _utf16_len(value: str) -> int:
    """Length in UTF-16 code units - the unit Power BI Desktop actually counts.

    Python's `len()` counts CODE POINTS; .NET's `String.Length` counts UTF-16 CODE UNITS, so every
    non-BMP character (emoji, astral planes) is 1 here and 2 there. Measured: a real path whose
    `len()` is 259 but whose UTF-16 length is 261 passed this check and is refused by Desktop.

    Raises UnicodeEncodeError for a string that cannot be represented in UTF-16 at all - notably a
    lone surrogate, which is exactly what `os.walk` hands back for an undecodable POSIX filename
    under `surrogateescape`. Callers must treat that as UNKNOWN, never as clean.
    """
    return len(value.encode("utf-16-le", "strict")) // 2


def _safe_repr(value: str) -> str:
    """A JSON/console-safe rendering of a path that may carry lone surrogates."""
    return value.encode("utf-8", "backslashreplace").decode("ascii", "replace")


def collect(root: Path) -> tuple[list[dict], list[dict]]:
    """Walk `root`, returning (measured, unknown).

    `measured` carries one record per file and per directory. `unknown` carries every entry the walk
    could not measure; those are never folded into `measured`.
    """
    root_str = str(root)
    measured: list[dict] = []
    unknown: list[dict] = []

    try:
        root_units = _utf16_len(root_str)
    except UnicodeEncodeError as exc:
        return [], [{"path": _safe_repr(root_str), "reason": f"root is not representable in UTF-16: {exc}"}]

    def on_error(exc: OSError) -> None:
        unknown.append(
            {
                "path": _safe_repr(str(getattr(exc, "filename", "") or root_str)),
                "reason": f"{type(exc).__name__}: {exc.strerror or exc}",
            }
        )

    for dirpath, dirnames, filenames in os.walk(root_str, onerror=on_error):
        for name, kind in [(d, KIND_DIR) for d in dirnames] + [(f, KIND_FILE) for f in filenames]:
            full = os.path.join(dirpath, name)
            try:
                length = _utf16_len(full)
            except (UnicodeEncodeError, ValueError) as exc:
                # An undecodable POSIX filename, or any name UTF-16 cannot represent. Unmeasurable
                # is NOT clean - this is the failure shape this repo keeps re-introducing.
                unknown.append({"path": _safe_repr(full), "reason": f"{type(exc).__name__}: {exc}"})
                continue
            measured.append({"path": full, "kind": kind, "length": length, "tail": length - root_units})
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
    # The portable number: how long an install root this tree can still tolerate. It is the MINIMUM
    # remaining budget across every path, judged against each path's OWN ceiling - not
    # `file_ceiling - longest_tail`. A short filename (`page.json`, `.platform`) makes the stricter
    # DIRECTORY rule decisive, which is the ordinary PBIR shape, not a contrived one: a blank page
    # directory can be the binding constraint while the longest tail belongs to a file.
    root_budget = min((_ceiling_for(r, limits) - r["tail"] for r in measured), default=None)
    binding = min(measured, key=lambda r: _ceiling_for(r, limits) - r["tail"], default=None) if measured else None

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
        "host_git_long_paths": read_git_long_paths(root),
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
        "root_budget_binding": (
            None if binding is None else {k: binding[k] for k in ("path", "kind", "length", "tail")}
        ),
        "shipping_root_budget_advisory": SHIPPING_ROOT_BUDGET_ADVISORY,
        "root_budget_is_tight": root_budget is not None and root_budget < SHIPPING_ROOT_BUDGET_ADVISORY,
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


def _git_line(report: dict) -> str:
    """The second opt-in. Reported because ONE being set and the other not is how #235 hid."""
    value = report["host_git_long_paths"]
    if value is None:
        return "  host git core.longpaths: could not be determined - unknown, do not read as enabled"
    if value:
        return "  host git core.longpaths: true - git tolerates these paths; Power BI Desktop still will not"
    return (
        "  host git core.longpaths: false (git default) - `git add` on an over-ceiling tree either"
        " aborts (exit 128) or, when the OVERLONG path is a DIRECTORY, silently drops its contents"
        " and exits 0"
    )


def render(report: dict) -> str:
    """Human-readable report for one target."""
    counted = report["counted"]
    lines = [
        f"{report['status'].upper()}: {report['root']}",
        _registry_line(report),
        _git_line(report),
        f"  ceilings              : file <= {report['file_ceiling']} (observed refusal at"
        f" {report['file_ceiling'] + 1}), directory <= {report['dir_ceiling']} (refuses"
        f" {report['dir_ceiling'] + 1}) - UTF-16 code units, see module docstring",
        f"  measured              : {counted['measured']} paths"
        f" ({counted['files']} files, {counted['directories']} directories)",
    ]
    longest = report["longest"]
    if longest:
        lines.append(f"  longest path          : {longest['length']} chars - {longest['path']}")
    if report["root_budget"] is not None:
        binding = report["root_budget_binding"] or {}
        lines.append(
            f"  longest tail          : {report['longest_tail']} units"
            f"  ->  root budget {report['root_budget']} units"
            " (longest install root this bundle tolerates)"
        )
        lines.append(
            f"      binding path      : [{binding.get('kind')}] tail {binding.get('tail')}"
            f" vs its own ceiling - {binding.get('path')}"
        )
    if report["root_budget_is_tight"]:
        lines.append(
            f"  TIGHT ROOT BUDGET     : {report['root_budget']} <"
            f" {report['shipping_root_budget_advisory']} - this bundle is a shipping hazard wherever"
            " it currently sits; advisory, use --min-root-budget to gate on it"
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
