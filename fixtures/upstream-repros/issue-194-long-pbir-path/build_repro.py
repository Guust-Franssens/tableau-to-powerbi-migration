"""
purpose: deterministically build the issue-194 long-path repro pair (.twbx A/B) from ONE template,
         so a maintainer can reproduce the committed archives byte for byte.
usage:   python fixtures/upstream-repros/issue-194-long-pbir-path/build_repro.py [--check]

`--check` rebuilds into memory and compares SHA-256 against the committed archives instead of
writing, so CI can prove the archives are reproducible without touching the working tree.
"Reproducible" here means ACROSS operating systems and checkouts, not merely across runs on one
machine - the two mechanisms that guarantee it are `payload_bytes` (line-ending normalisation, the
proven cause of a past Linux-only mismatch) and `ZIP_COMPRESSION` (stored members, so no compressor
implementation can leak into the bytes).
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

#: ⚠️ **STORED, not DEFLATED.** A DEFLATE byte stream is a property of the *zlib implementation and
#: version* linked into the interpreter, not of the ZIP format, so a deflated archive is only
#: reproducible against an identically-linked interpreter. These fixtures are ~7 KB - compression
#: buys nothing and costs a reproducibility guarantee. ⚠️ This is a *hardening* measure whose
#: contribution was never isolated: the CI failure it was proposed for had a different, proven cause
#: (see `payload_bytes`). Both changes are kept because both remove a real class of host dependence.
ZIP_COMPRESSION = zipfile.ZIP_STORED

#: Fixed POSIX mode (0600) in the high half of ``external_attr``. Written explicitly because
#: ``ZipInfo`` otherwise derives it from the host filesystem.
ZIP_EXTERNAL_ATTR = 0o600 << 16

#: ``0`` = MS-DOS/FAT. ``ZipInfo`` otherwise picks 0 on Windows and 3 (Unix) elsewhere, which alone
#: would make the central directory differ between a Windows and a Linux rebuild.
ZIP_CREATE_SYSTEM = 0

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


def payload_bytes(path: Path) -> bytes:
    """Read a source file as UTF-8 text and re-encode it with LF endings.

    ⚠️ **This, not the compression method, is why a rebuild used to differ on Linux.** The archives
    are built from files in the git working tree, and this repository is checked out with
    ``core.autocrlf=true`` on Windows: ``src/regional_sales.csv`` is **156 bytes with CRLF** in a
    Windows working tree and **151 bytes with LF** in the stored blob that a Linux runner checks out.
    The old builder read it with ``read_bytes()``, so the archive literally contained a different
    member on each platform and no ZIP setting could have fixed that. (The ``.twb`` template was
    always immune, because ``read_text()`` applies universal newlines - this makes both sources use
    the same rule, explicitly, instead of one by accident.)

    Normalising in the BUILDER rather than via ``.gitattributes`` keeps the guarantee a property of
    the recipe: it holds for a maintainer who downloads the tree however their git is configured.
    """
    return path.read_text(encoding="utf-8").encode("utf-8")


def workbook_xml(case: dict[str, str]) -> bytes:
    """The template with only the identity placeholders substituted - nothing else differs."""
    text = payload_bytes(TEMPLATE).decode("utf-8")
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
    # Sorted, fixed-date, STORED entries with pinned host metadata: the same bytes on every machine,
    # every run and every operating system (see ZIP_COMPRESSION).
    entries = [
        (f"{case['stem']}.twb", workbook_xml(case)),
        (f"{CSV_DIR}/{case['csv']}", payload_bytes(CSV)),
    ]
    with zipfile.ZipFile(buffer, "w", ZIP_COMPRESSION) as archive:
        for arcname, payload in sorted(entries):
            info = zipfile.ZipInfo(arcname, date_time=ZIP_DATE)
            info.compress_type = ZIP_COMPRESSION
            info.external_attr = ZIP_EXTERNAL_ATTR
            info.create_system = ZIP_CREATE_SYSTEM
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
