"""
purpose: run the canonical engine on the issue-194 A/B pair and census EVERY emitted path, so the
         longest required file and directory are measured rather than projected.
usage:   python fixtures/upstream-repros/issue-194-long-pbir-path/measure_repro.py \
             --engine <engine-root> --runs-parent C:\\tfmig\\runs [--json out.json]

Exit 0 always: this is a measurement tool, not a gate. The numbers it prints are the repro.
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

#: Power BI Desktop's observed limits, in UTF-16 code units. Kept local so this script can be
#: handed to an upstream maintainer with no repository around it.
FILE_CEILING = 259
DIR_CEILING = 247

CASES = {
    "long": "Regional Sales Performance and Inventory Turnover Review FY2026 Q3 Final.twbx",
    "short": "Regional Sales FY26Q3.twbx",
}
RUN_IDS = {"long": "9194", "short": "9195"}


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units - what .NET's String.Length counts, and what Desktop enforces."""
    return len(text.encode("utf-16-le")) // 2


def run_engine(engine: Path, source: Path, out: Path) -> tuple[int, str]:
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
    return done.returncode, " ".join(cmd)


def census(root: Path) -> dict:
    """Every file and directory under `root`, with UTF-16 lengths and unreadable entries counted."""
    files = dirs = 0
    unknown: list[str] = []
    longest_file = (0, "")
    longest_dir = (0, "")
    offenders: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: unknown.append(str(e))):
        for name in list(dirnames) + list(filenames):
            full = str(Path(dirpath) / name)
            if any("\ud800" <= ch <= "\udfff" for ch in full):
                unknown.append(f"undecodable-name {full!r}")
                continue
            length = utf16_len(full)
            if name in dirnames:
                dirs += 1
                longest_dir = max(longest_dir, (length, full))
                if length > DIR_CEILING:
                    offenders.append({"kind": "directory", "length": length, "path": full})
            else:
                files += 1
                longest_file = max(longest_file, (length, full))
                if length > FILE_CEILING:
                    offenders.append({"kind": "file", "length": length, "path": full})
    return {
        "root": str(root),
        "root_len": utf16_len(str(root)),
        "entries": files + dirs,
        "files": files,
        "directories": dirs,
        "unknown": unknown,
        "longest_file_len": longest_file[0],
        "longest_file": longest_file[1],
        "longest_dir_len": longest_dir[0],
        "longest_dir": longest_dir[1],
        "offenders": offenders,
        "verdict": "OVER CEILING" if offenders else "within ceilings",
    }


def entry_pbip(root: Path) -> dict:
    """The `.pbip` POINTER - deliberately short, and a positive control for requirement 9."""
    found = sorted(root.rglob("*.pbip"))
    return {
        "count": len(found),
        "paths": [{"path": str(p), "length": utf16_len(str(p))} for p in found],
    }


def main(argv: list[str]) -> int:
    """Run both cases, print the path census, and optionally write it as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True, help="canonical engine plugin root")
    parser.add_argument("--runs-parent", type=Path, default=Path(r"C:\tfmig\runs"))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    report = {"file_ceiling": FILE_CEILING, "dir_ceiling": DIR_CEILING, "cases": {}}
    for case, archive in CASES.items():
        run_root = args.runs_parent / RUN_IDS[case]
        source = run_root / "in"
        out = run_root / "out"
        if run_root.exists():
            shutil.rmtree(run_root)
        source.mkdir(parents=True)
        shutil.copy2(HERE / archive, source / archive)

        code, command = run_engine(args.engine, source, out)
        measured = census(out)
        measured.update(
            {
                "archive": archive,
                "archive_stem_len": utf16_len(Path(archive).stem),
                "engine_exit": code,
                "engine_command": command,
                "pbip_entry": entry_pbip(out),
                "unit_folders": sorted(p.name for p in (out / "pbip").iterdir() if p.is_dir())
                if (out / "pbip").is_dir()
                else [],
            }
        )
        report["cases"][case] = measured

        print(f"== {case.upper()}  {archive}")
        print(f"   workbook stem        : {measured['archive_stem_len']} UTF-16 units")
        print(f"   engine exit          : {code}")
        print(f"   output root          : {measured['root']}  ({measured['root_len']} units)")
        print(f"   emitted unit folder  : {measured['unit_folders']}")
        print(
            f"   entries              : {measured['entries']} "
            f"({measured['files']} files, {measured['directories']} dirs)"
        )
        print(f"   unknown/unreadable   : {len(measured['unknown'])}")
        print(f"   longest FILE         : {measured['longest_file_len']:4d} (ceiling {FILE_CEILING})")
        print(f"      {measured['longest_file']}")
        print(f"   longest DIRECTORY    : {measured['longest_dir_len']:4d} (ceiling {DIR_CEILING})")
        print(f"      {measured['longest_dir']}")
        for entry in measured["pbip_entry"]["paths"]:
            print(f"   .pbip pointer        : {entry['length']:4d}  {entry['path']}")
        print(f"   VERDICT              : {measured['verdict']}  ({len(measured['offenders'])} offender(s))")
        print()

    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
