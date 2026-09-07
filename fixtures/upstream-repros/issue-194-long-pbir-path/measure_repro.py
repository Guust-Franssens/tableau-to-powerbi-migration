"""
purpose: run the canonical engine on the issue-194 A/B pair and census EVERY emitted path, so the
         longest required file and directory are MEASURED rather than projected, and so an
         unmeasurable run can never look like a clean result.
usage:   python fixtures/upstream-repros/issue-194-long-pbir-path/measure_repro.py \
             --engine <engine-plugin-root> [--runs-parent C:\\tfmig\\i194] [--json out.json]

Exit 0 measured AND the documented A/B held · 2 INVALID/UNMEASURED · 3 measured but the documented
A/B did not hold · 64 usage. A non-zero exit never prints a clean verdict.

⚠️ This script NEVER deletes anything. It allocates a fresh four-digit run directory under
`--runs-parent` with an atomic exclusive `mkdir`, retrying on collision, and prints what it
allocated. An earlier revision used fixed ids plus an unconditional `shutil.rmtree`; that is the
exact shape that destroyed a colleague's run directory, and it is not coming back.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Power BI Desktop's observed limits, in UTF-16 code units. Kept local so this file can be handed
#: to an upstream maintainer with no repository around it.
FILE_CEILING = 259
DIR_CEILING = 247

#: This fixture's OWN parent. Deliberately NOT `C:\tfmig\runs`, which is a shared, populated
#: location: allocating into it by pattern is what caused the incident recorded in the report.
DEFAULT_RUNS_PARENT = Path(r"C:\tfmig\i194")

#: The single required child this repro is about, as a POSIX-style relative-tail fragment.
OFFENDER_FRAGMENT = ".SemanticModel/definition/tables/"
OFFENDER_SUFFIX = ".tmdl"

CASES = {
    "long": "Regional Sales Performance and Inventory Turnover Review FY2026 Q3 Final.twbx",
    "short": "Regional Sales FY26Q3.twbx",
}

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_UNEXPECTED = 3
EXIT_USAGE = 64

MAX_ALLOCATION_ATTEMPTS = 200


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units - what .NET's String.Length counts, and what Desktop enforces."""
    return len(text.encode("utf-16-le")) // 2


def allocate_run(parent: Path) -> Path:
    """Atomically claim the lowest unused four-digit run directory under `parent`.

    `os.mkdir` is the claim: it fails if the directory already exists, so two concurrent callers
    cannot be handed the same id, and an id already on disk is never reused or removed. Nothing here
    deletes, renames or writes into a directory it did not create.
    """
    parent.mkdir(parents=True, exist_ok=True)
    for candidate in range(1, MAX_ALLOCATION_ATTEMPTS + 1):
        run = parent / f"{candidate:04d}"
        try:
            os.mkdir(run)
        except FileExistsError:
            continue
        return run
    raise SystemExit(
        f"REFUSED: no unused run id under {parent} after {MAX_ALLOCATION_ATTEMPTS} attempts. "
        "Point --runs-parent at a fresh directory; this script will not delete or reuse one."
    )


def run_engine(engine: Path, source: Path, out: Path) -> tuple[int, str, str]:
    """Invoke the canonical engine DIRECTLY - no repository wrapper and no preflight."""
    cmd = [
        sys.executable,
        str(engine / "skills" / "tableau-migration" / "scripts" / "migrate_estate.py"),
        "-i",
        str(source),
        "-o",
        str(out),
    ]
    done = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1800, check=False)
    return done.returncode, " ".join(cmd), (done.stdout or "") + (done.stderr or "")


def census(out: Path, root_len: int | None = None) -> dict:
    """Every emitted path as a RELATIVE tail, with offenders judged at a given root length.

    Relative tails are what makes this portable and what makes one implementation serve both the
    public script and its tests: the caller supplies the root length it cares about, so a host that
    cannot create `C:\\tfmig` can still exercise the identical code.
    """
    root_len = utf16_len(str(out)) if root_len is None else root_len
    files: list[str] = []
    dirs: list[str] = []
    unknown: list[str] = []
    for dirpath, dirnames, filenames in os.walk(out, onerror=lambda e: unknown.append(str(e))):
        for name in list(dirnames) + list(filenames):
            full = Path(dirpath) / name
            try:
                rel = str(full.relative_to(out)).replace("\\", "/")
            except ValueError:  # pragma: no cover - defensive
                unknown.append(f"not-under-root {full!r}")
                continue
            if any("\ud800" <= ch <= "\udfff" for ch in rel):
                unknown.append(f"undecodable-name {rel!r}")
                continue
            (dirs if name in dirnames else files).append(rel)

    def at_root(rel: str) -> int:
        return root_len + 1 + utf16_len(rel)

    offenders = [
        {"kind": "file", "tail": rel, "length": at_root(rel), "ceiling": FILE_CEILING}
        for rel in files
        if at_root(rel) > FILE_CEILING
    ] + [
        {"kind": "directory", "tail": rel, "length": at_root(rel), "ceiling": DIR_CEILING}
        for rel in dirs
        if at_root(rel) > DIR_CEILING
    ]
    pbip = sorted(rel for rel in files if rel.endswith(".pbip"))
    return {
        "root_len": root_len,
        "entries": len(files) + len(dirs),
        "files": len(files),
        "directories": len(dirs),
        "unknown": unknown,
        "longest_file_tail": max(files, key=utf16_len, default=""),
        "longest_file_len": at_root(max(files, key=utf16_len)) if files else 0,
        "longest_dir_tail": max(dirs, key=utf16_len, default=""),
        "longest_dir_len": at_root(max(dirs, key=utf16_len)) if dirs else 0,
        "pbip_tails": pbip,
        "pbip_len": at_root(pbip[0]) if len(pbip) == 1 else 0,
        "offenders": offenders,
    }


def invalid_reasons(case: str, measured: dict, engine_exit: int) -> list[str]:
    """Why this measurement may NOT be reported as a result. Empty means it may."""
    reasons: list[str] = []
    if engine_exit != 0:
        reasons.append(f"engine exited {engine_exit}")
    if measured["unknown"]:
        reasons.append(f"{len(measured['unknown'])} unknown/unreadable path(s): {measured['unknown'][:3]}")
    if measured["entries"] == 0:
        reasons.append("engine produced no entries at all")
    if len(measured["pbip_tails"]) != 1:
        reasons.append(
            f"expected exactly one .pbip pointer, found {len(measured['pbip_tails'])}: {measured['pbip_tails']}"
        )
    if not measured["longest_file_tail"]:
        reasons.append("no files were emitted")
    if case not in CASES:
        reasons.append(f"unknown case {case!r}")
    return reasons


def unexpected_reasons(case: str, measured: dict) -> list[str]:
    """Why the measurement, though valid, is not the documented A/B result."""
    reasons: list[str] = []
    offenders = measured["offenders"]
    if case == "long":
        required = [
            o
            for o in offenders
            if o["kind"] == "file" and OFFENDER_FRAGMENT in o["tail"] and o["tail"].endswith(OFFENDER_SUFFIX)
        ]
        if len(required) != 1:
            reasons.append(
                f"expected exactly ONE overlong required {OFFENDER_FRAGMENT}*{OFFENDER_SUFFIX} file, "
                f"found {len(required)}: {[o['tail'] for o in required]}"
            )
        if len(offenders) != len(required):
            others = [(o["kind"], o["tail"]) for o in offenders if o not in required]
            reasons.append(f"unexpected additional offender(s): {others}")
        if measured["pbip_len"] > FILE_CEILING:
            reasons.append(
                f".pbip pointer is itself over the ceiling ({measured['pbip_len']}); the repro needs a LEGAL entry file"
            )
    if case == "short" and offenders:
        reasons.append(
            f"the short control has {len(offenders)} offender(s): {[(o['kind'], o['tail']) for o in offenders]}"
        )
    return reasons


def main(argv: list[str]) -> int:  # pylint: disable=too-many-locals
    """Run both cases, print the path census, and refuse to report an unmeasurable run as clean."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True, help="canonical engine plugin root")
    parser.add_argument("--runs-parent", type=Path, default=DEFAULT_RUNS_PARENT)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    entry = args.engine / "skills" / "tableau-migration" / "scripts" / "migrate_estate.py"
    if not entry.is_file():
        print(f"INVALID: no engine at {entry} - nothing was measured.")
        return EXIT_INVALID

    version_file = args.engine / "skills" / "tableau-migration" / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else "unknown"

    report: dict = {
        "file_ceiling": FILE_CEILING,
        "dir_ceiling": DIR_CEILING,
        "engine_version": version,
        "runs_parent": str(args.runs_parent),
        "cases": {},
    }
    invalid = unexpected = 0
    for case, archive in CASES.items():
        run_root = allocate_run(args.runs_parent)
        source = run_root / "in"
        source.mkdir()
        shutil.copy2(HERE / archive, source / archive)
        out = run_root / "out"
        print(f"== {case.upper()}  {archive}")
        print(f"   allocated run       : {run_root}")

        code, command, output = run_engine(args.engine, source, out)
        measured = (
            census(out)
            if out.is_dir()
            else {
                "entries": 0,
                "unknown": ["no output directory"],
                "files": 0,
                "directories": 0,
                "offenders": [],
                "pbip_tails": [],
                "longest_file_tail": "",
                "longest_file_len": 0,
                "longest_dir_tail": "",
                "longest_dir_len": 0,
                "pbip_len": 0,
                "root_len": utf16_len(str(out)),
            }
        )
        bad = invalid_reasons(case, measured, code)
        odd = [] if bad else unexpected_reasons(case, measured)
        measured.update(
            {
                "archive": archive,
                "archive_stem_len": utf16_len(Path(archive).stem),
                "engine_exit": code,
                "engine_command": command,
                "engine_emitted_max_path_warning": "MAX_PATH" in output,
                "invalid_reasons": bad,
                "unexpected_reasons": odd,
                "verdict": "INVALID"
                if bad
                else ("UNEXPECTED" if odd else ("OVER CEILING" if measured["offenders"] else "within ceilings")),
            }
        )
        report["cases"][case] = measured
        invalid += bool(bad)
        unexpected += bool(odd)

        print(f"   output root         : {out}  ({measured['root_len']} units)")
        warned = measured["engine_emitted_max_path_warning"]
        print(f"   engine exit         : {code}   version {version}   MAX_PATH warning: {warned}")
        print(
            f"   entries             : {measured['entries']} "
            f"({measured['files']} files, {measured['directories']} dirs)"
        )
        print(f"   unknown/unreadable  : {len(measured['unknown'])}")
        print(
            f"   longest FILE        : {measured['longest_file_len']:4d} "
            f"(ceiling {FILE_CEILING})  {measured['longest_file_tail']}"
        )
        print(
            f"   longest DIRECTORY   : {measured['longest_dir_len']:4d} "
            f"(ceiling {DIR_CEILING})  {measured['longest_dir_tail']}"
        )
        print(f"   .pbip pointer       : {measured['pbip_len']:4d}  {measured['pbip_tails']}")
        for reason in bad:
            print(f"   INVALID             : {reason}")
        for reason in odd:
            print(f"   UNEXPECTED          : {reason}")
        print(f"   VERDICT             : {measured['verdict']}  ({len(measured['offenders'])} offender(s))")
        print()

    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if invalid:
        print(f"RESULT: INVALID - {invalid} case(s) could not be measured; no verdict is claimed.")
        return EXIT_INVALID
    if unexpected:
        print(f"RESULT: UNEXPECTED - {unexpected} case(s) measured cleanly but did not match the documented A/B.")
        return EXIT_UNEXPECTED
    print("RESULT: the documented A/B held - long crosses on the required table file, short does not.")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:  # noqa: TRY302  # pylint: disable=try-except-raise
        raise
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # A crash must be reported as INVALID, never as an absent result that a reader mistakes for
        # a clean one - that is finding 2 of the review this file was corrected for.
        print(f"INVALID: measurement crashed ({type(exc).__name__}: {exc}) - nothing is claimed.")
        sys.exit(EXIT_INVALID)
