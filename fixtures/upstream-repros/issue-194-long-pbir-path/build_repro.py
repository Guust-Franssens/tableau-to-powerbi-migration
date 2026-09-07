"""
purpose: deterministically build the issue-194 long-path repro pair (.twbx A/B) from ONE template,
         so a maintainer can reproduce the committed archives byte for byte.
usage:   python fixtures/upstream-repros/issue-194-long-pbir-path/build_repro.py [--check]

`--check` rebuilds into memory and compares SHA-256 against the committed archives instead of
writing, so CI can prove the archives are reproducible without touching the working tree.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
TEMPLATE = SRC / "workbook-template.twb"
CSV = SRC / "regional_sales.csv"

#: The template's placeholder for the flat file's own name. Substituted per case, because the
#: emitted `definition/tables/<table>.tmdl` filename is derived from it and is NOT covered by the
#: engine's `_MAX_FS_BASE` folder cap - that asymmetry is the whole repro.
CSV_TOKEN = "@@CSVFILE@@"

#: Where Tableau stores a packaged workbook's flat file inside the archive.
CSV_DIR = "Data/regional-sales"

#: A fixed timestamp so the ZIP is byte-reproducible on any machine and in any year.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

#: The ONLY dimension that differs between the two cases: the identity names. Long side is a
#: plausible enterprise report title and export filename, never repeated characters.
CASES = {
    "long": {
        "stem": "Regional Sales Performance and Inventory Turnover Review FY2026 Q3 Final",
        "datasource": "Regional Sales Performance and Inventory Turnover Consolidated Source",
        "dashboard": "Regional Sales Performance and Inventory Turnover Review Dashboard",
        "worksheet": "Regional Net Revenue by Sales Region and Fiscal Period Detail",
        "csv": "Regional Sales Performance and Inventory Turnover FY2026 Q3 Detail Extract.csv",
    },
    "short": {
        "stem": "Regional Sales FY26Q3",
        "datasource": "Regional Sales",
        "dashboard": "Regional Sales Review",
        "worksheet": "Net Revenue by Region",
        "csv": "regional_sales.csv",
    },
}

PLACEHOLDERS = {
    "@@DATASOURCE@@": "datasource",
    "@@DASHBOARD@@": "dashboard",
    "@@WORKSHEET@@": "worksheet",
    CSV_TOKEN: "csv",
}


def workbook_xml(case: dict[str, str]) -> bytes:
    """The template with only the identity placeholders substituted - nothing else differs."""
    text = TEMPLATE.read_text(encoding="utf-8")
    for token in PLACEHOLDERS:
        assert token in text, f"template lost its {token} placeholder"
    for token, key in PLACEHOLDERS.items():
        text = text.replace(token, case[key])
    return text.encode("utf-8")


def build(case_name: str) -> tuple[str, bytes]:
    """Return (archive filename, archive bytes) for one case, deterministically."""
    case = CASES[case_name]
    name = f"{case['stem']}.twbx"
    buffer = io.BytesIO()
    # Sorted, fixed-date, fixed-compression entries: same bytes on every machine and every run.
    entries = [
        (f"{case['stem']}.twb", workbook_xml(case)),
        (f"{CSV_DIR}/{case['csv']}", CSV.read_bytes()),
    ]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for arcname, payload in sorted(entries):
            info = zipfile.ZipInfo(arcname, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            info.create_system = 0
            archive.writestr(info, payload)
    return name, buffer.getvalue()


def main(argv: list[str]) -> int:
    """Build both archives, or verify the committed ones. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify committed archives instead of writing")
    args = parser.parse_args(argv)

    failures = 0
    for case_name in sorted(CASES):
        name, payload = build(case_name)
        digest = hashlib.sha256(payload).hexdigest()
        target = HERE / name
        if args.check:
            if not target.is_file():
                print(f"MISSING  {name}")
                failures += 1
                continue
            committed = hashlib.sha256(target.read_bytes()).hexdigest()
            status = "OK   " if committed == digest else "DIFFER"
            failures += committed != digest
            print(f"{status}   {name}\n          rebuilt {digest}\n          committed {committed}")
        else:
            target.write_bytes(payload)
            print(f"WROTE    {name}  sha256 {digest}  {len(payload)} bytes")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
