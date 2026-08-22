"""
purpose: fail loudly when a migration produces a PBIR report that is STRUCTURALLY INVALID -
         a required visual role left unbound, which the engine's own definition of done grades
         as a PASS.
usage:   python scripts/check_pbir_valid.py <bundle-or-report-dir> [...]
                                            [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
The engine can emit a report that `powerbi-report-author validate` rejects, and grade it
`definition_of_done: warn` / `0 error` / `Viz=built`. Measured 2026-08-18 on engine 2.151.0, a
cold run of one datasource + one downstream workbook off a live Tableau site:

    v-RegionalSharea1c2c884 | clusteredColumnChart | roles: ['Category']        <-- no Y role
    v-RevenuebyRegio864a62f6| clusteredColumnChart | roles: ['Category', 'Y']
    v-RevenueTrend1626f5ae  | lineChart            | roles: ['Category', 'Y']

    $ powerbi-report-author validate <...>.Report --format text
    ERROR [PBIR_ROLE_REQUIRED_MISSING] x1
    1 error(s), 0 warning(s); result=failed            exit 1

    engine verdict on the same bytes: ESTATE: READY, definition_of_done: warn, 0 error, Viz=built

The cause is a Tableau calc that fell back to a stub: the engine dropped the projection instead of
binding it, and that projection was the sole occupant of a REQUIRED role. Filed upstream as #220
(the defect) and #221 (why the engine's own gate cannot see it - `pbir_lint.py` has no required-role
rule, and the real `validate` pre-gate in `fidelity_oracle.py` is default-off AND explicitly
"never changes the structural aggregate" when enabled).

Why here and not in the engine
------------------------------
It belongs in both, which is why #221 is filed. But this repo is the critic tier, and the
`run_estate.py` ladder already exists for exactly this shape: "the exit code the engine cannot give
you". `check_empty_model.py` is the sibling that catches a model which opens but loads no rows; this
catches a report which does not validate at all. Until #221 lands upstream, this is the only thing
between a stubbed calc and a customer-facing report that will not open correctly.

The check is DELEGATED, not reimplemented
-----------------------------------------
This module shells out to the first-party `powerbi-report-author validate`. It deliberately does not
parse PBIR itself: the authoritative role-requirement catalog lives in that CLI, is versioned with
it, and reimplementing it here would drift. What this module adds is that the CLI actually RUNS, on
every report that ships, and that its verdict BINDS.

Two measured traps this module encodes
--------------------------------------
1. **`--format json` is a false negative.** Piping it to a JSON parser returns an empty `errorCount`
   even on a failing report. Text output plus the exit code is the reliable read, so that is what
   this module uses. The exit code is honest: 1 = failed, 0 = passed.
2. **The path is POSITIONAL.** `validate --path <p>` fails with `INVALID_USAGE`.

Scope: `pbip/` only
-------------------
A bundle carries the working copy at `<bundle>/pbip/<WB>/<WB>.Report` and the engine's pristine
baseline at `<bundle>/reports/<WB>.Report`. Only `pbip/` is scanned, because only `pbip/` ships:
`reports/` is reference-only (no semantic model beside it), so validating it reports unresolvable
references that say nothing about the deliverable. Point this script directly at a `.Report` folder
to override that.

What it will NOT tell you
-------------------------
That the report is CORRECT. A clean result means "no structural defect the first-party validator
recognises" - the same "necessary, not sufficient" limit every other gate in this repo carries. A
report can validate perfectly and still render the wrong number, bind to an empty model, or drop a
visual the source had. It also cannot run without Node and the CLI on PATH; that case degrades to
SKIPPED and never blocks, so an estate run on a machine without the toolchain still completes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from bundle_corpus import shipping_reports

REPORT_NAME = "pbir-validity-check.json"
CLI = "powerbi-report-author"
CLI_PKG = "@microsoft/powerbi-report-authoring-cli"

# Per-report wall clock. Measured 1.0s clean / 5.9s failing (first call pays Node startup), so this
# is ~20x headroom - generous enough never to false-positive, tight enough that a hung CLI on a
# 38-workbook estate cannot park the run indefinitely.
TIMEOUT_SEC = 120

_CODE_RE = re.compile(r"\[([A-Z][A-Z0-9_]{3,})\]")
_SUMMARY_RE = re.compile(r"(\d+)\s+error\(s\).*?(\d+)\s+warning\(s\)", re.IGNORECASE)


def find_cli(explicit: str | None = None) -> str | None:
    """Locate the validator, tolerating Windows' .cmd/.ps1 shims."""
    if explicit:
        return shutil.which(explicit) or (explicit if Path(explicit).exists() else None)
    found = shutil.which(CLI)
    if found:
        return found
    for ext in (".cmd", ".exe", ".ps1"):
        found = shutil.which(CLI + ext)
        if found:
            return found
    return None


find_reports = shipping_reports


def validate_one(report: Path, cli: str, timeout: int = TIMEOUT_SEC) -> dict:
    """Run the first-party validator over ONE report folder.

    The exit code is the verdict; the text is parsed only to name what failed. Any execution
    problem (timeout, crash) is reported as `error` rather than raised, so one bad report cannot
    abort the sweep over the rest.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [cli, "validate", str(report), "--format", "text"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"report": str(report), "status": "error", "detail": f"validator exceeded {timeout}s"}
    except OSError as exc:
        return {"report": str(report), "status": "error", "detail": f"could not run validator: {exc}"}

    out = (proc.stdout or "") + (proc.stderr or "")
    codes = sorted(set(_CODE_RE.findall(out)))
    counts = _SUMMARY_RE.search(out)

    # A non-zero exit with NOTHING recognisable in it is the CHECKER failing, not the report. Keep
    # the two apart: "error" means this gate could not form an opinion (a usage mistake, a missing
    # subcommand, a crashed CLI); "invalid" means the validator looked and refused. Measured during
    # development - dropping the `validate` subcommand produced exit 1, empty output, and a
    # confident "2 of 2 reports FAIL", which is a gate that always says no and so trains its reader
    # to ignore it. Strictly worse than having no gate at all.
    if proc.returncode != 0 and not codes and not counts:
        return {
            "report": str(report),
            "status": "error",
            "exit_code": proc.returncode,
            "detail": (out.strip() or "validator exited non-zero with no diagnostic output")[:800],
        }

    entry = {
        "report": str(report),
        "status": "invalid" if proc.returncode != 0 else "valid",
        "exit_code": proc.returncode,
        "codes": codes,
        "errors": int(counts.group(1)) if counts else None,
        "warnings": int(counts.group(2)) if counts else None,
    }
    if proc.returncode != 0:
        # Keep the validator's own words - a paraphrase of a schema error is a liability.
        entry["detail"] = "; ".join(ln.strip() for ln in out.splitlines() if "ERROR" in ln)[:800]
    return entry


def scan(root: Path, cli_path: str | None = None) -> dict:
    """Validate every shipping report under `root`.

    Degrades to `SKIPPED` (never `INVALID`) when the CLI is absent, so an estate run on a machine
    without Node still completes rather than failing for a reason that is not the migration's fault.
    """
    cli = find_cli(cli_path)
    reports = find_reports(root)
    if cli is None:
        return {
            "status": "SKIPPED",
            "reason": f"{CLI} not found on PATH (npm i -g {CLI_PKG})",
            "reports_scanned": 0,
            "reports_invalid": 0,
            "findings": [],
        }
    findings = [validate_one(r, cli) for r in reports]
    invalid = [f for f in findings if f["status"] == "invalid"]
    errored = [f for f in findings if f["status"] == "error"]
    # Only a real refusal BLOCKS. A checker that could not form an opinion is reported loudly but
    # does not refuse the bundle - the bundle may be perfectly fine, and halting every migration on
    # a tooling fault is the more expensive mistake.
    status = "INVALID" if invalid else ("ERROR" if errored else "OK")
    return {
        "status": status,
        "cli": cli,
        "reports_scanned": len(findings),
        "reports_invalid": len(invalid),
        "reports_errored": len(errored),
        "findings": findings,
    }


def render(report: dict) -> str:
    """Human-readable verdict, in the shape `render_empty_model` uses."""
    status = report.get("status")
    if status == "SKIPPED":
        return f"PBIR VALIDITY CHECK: SKIPPED - {report.get('reason', 'validator unavailable')}"
    scanned = report.get("reports_scanned", 0)
    if status == "OK":
        return f"PBIR VALIDITY CHECK: OK - {scanned} report(s) pass first-party structural validation."
    if status == "ERROR":
        lines = [
            f"PBIR VALIDITY CHECK: ERROR - the validator could not form an opinion on "
            f"{report.get('reports_errored', 0)} of {scanned} report(s)",
            "  This is a TOOLING fault, not a finding: the bundle is neither cleared nor refused.",
        ]
        lines += [
            f"  - {Path(f['report']).name}: {f.get('detail', '')}"
            for f in report.get("findings", [])
            if f.get("status") == "error"
        ]
        return "\n".join(lines)
    lines = [
        f"PBIR VALIDITY CHECK: INVALID - {report.get('reports_invalid', 0)} of {scanned} "
        "report(s) FAIL first-party structural validation",
    ]
    for finding in report.get("findings", []):
        if finding.get("status") == "valid":
            continue
        codes = ", ".join(finding.get("codes") or []) or finding.get("detail", "") or "see JSON"
        lines.append(f"  - {Path(finding['report']).name}: {codes}")
    lines.append(
        "  A required role left unbound usually means a STUBBED measure whose projection was "
        "dropped.\n"
        "  Bind the stub (it exists in the model as BLANK()); do NOT delete the visual - that "
        "destroys\n"
        "  the only in-report evidence the source had a chart there."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path, help="bundle folder(s) or .Report folder(s)")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0")
    parser.add_argument("--cli", help=f"explicit path to {CLI}")
    args = parser.parse_args(argv)

    merged: dict = {
        "status": "OK",
        "reports_scanned": 0,
        "reports_invalid": 0,
        "reports_errored": 0,
        "findings": [],
    }
    for path in args.paths:
        one = scan(path, args.cli)
        if one["status"] == "SKIPPED":
            merged = one
            break
        merged["reports_scanned"] += one["reports_scanned"]
        merged["reports_invalid"] += one["reports_invalid"]
        merged["reports_errored"] += one.get("reports_errored", 0)
        merged["findings"].extend(one["findings"])
        if one["status"] == "INVALID" or merged["reports_invalid"]:
            merged["status"] = "INVALID"
        elif one["status"] == "ERROR" and merged["status"] == "OK":
            merged["status"] = "ERROR"

    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged))
    if args.warn_only or merged["status"] != "INVALID":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
