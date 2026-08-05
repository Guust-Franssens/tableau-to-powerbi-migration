"""
purpose: derive committable connection TEMPLATES from REAL Tableau exports, by replacing only the
         endpoint VALUES with placeholders and keeping the attribute shape byte-for-byte.
         Shape fidelity is the whole point: a synthesised connection element reflects what we THINK
         Tableau writes, and that guess has already been wrong once - the Databricks HTTP path is
         spelled `_.fcp.DatabricksCatalog.true...v-http-path`, not `v-http-path`, which is why both
         this repo's parser and the deterministic tier's read it as None.
usage:   python scripts/derive_connection_templates.py <real-export> [<real-export> ...]
         Accepts .tds / .tdsx / .twb / .twbx. Writes tests/fixtures/connection-templates/<class>.xml.

The templates carry NO hostnames, so they are safe to commit; the generator
(`scripts/make_live_source_fixture.py`) substitutes real endpoints at build time.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

OUT_DIR = Path("tests/fixtures/connection-templates")

# attribute -> placeholder token.
PLACEHOLDERS = {
    "server": "{{SERVER}}",
    "dbname": "{{DATABASE}}",
    "schema": "{{SCHEMA}}",
    "warehouse": "{{WAREHOUSE}}",
    "v-http-path": "{{HTTP_PATH}}",
    "username": "{{USERNAME}}",
    "instanceurl": "{{INSTANCE_URL}}",
}

# Fully-qualified overrides, checked BEFORE the base-name table above. Tableau ships both states of a
# feature-flagged attribute and their meanings differ, so they cannot share a placeholder: with
# DatabricksCatalog enabled, `.true...dbname` is the Unity catalog while `.false...dbname` is the
# LEGACY slot that held the SQL-warehouse HTTP path. Collapsing both to {{DATABASE}} would write a
# catalog name into a slot that really holds `/sql/1.0/warehouses/...`, quietly changing the shape
# this template exists to preserve.
QUALIFIED_PLACEHOLDERS = {
    "_.fcp.DatabricksCatalog.false...dbname": "{{HTTP_PATH}}",
    "_.fcp.DatabricksCatalog.true...dbname": "{{DATABASE}}",
}

_FCP = re.compile(r"^_\.fcp\.[^.]+\.(?:true|false)\.\.\.(?P<attr>.+)$")


def read_xml(path: Path) -> str:
    """Return the workbook/datasource XML, unwrapping the zip container when there is one."""
    if path.suffix.lower() in (".tdsx", ".twbx"):
        with zipfile.ZipFile(path) as zf:
            inner = next(n for n in zf.namelist() if n.endswith((".tds", ".twb")))
            return zf.read(inner).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def scrub(element: str) -> str:
    """Replace endpoint values with placeholders, leaving attribute names and layout untouched.

    Substitution is keyed on the ATTRIBUTE NAME, never on the value. Matching by value looks
    convenient and is wrong: in a real Databricks export `oauth-config-id` happened to hold the same
    string as `schema`, so a value-based pass rewrote it to `{{SCHEMA}}` and silently corrupted an
    unrelated attribute.
    """

    def replace(match: re.Match[str]) -> str:
        name, value = match.group(1), match.group(2)
        token = QUALIFIED_PLACEHOLDERS.get(name)
        if token is None:
            base = _FCP.match(name)
            token = PLACEHOLDERS.get(base.group("attr") if base else name)
        return f"{name}='{token}'" if token else f"{name}='{value}'"

    return re.sub(r"([\w.\-]+)='([^']*)'", replace, element)


def extract(xml: str) -> dict[str, str]:
    """Every distinct inner <connection class='...'> element in the document."""
    found: dict[str, str] = {}
    for match in re.finditer(r"<connection\b[^>]*?/?>", xml, re.S):
        element = match.group(0)
        cls = re.search(r"class='([^']+)'", element)
        if not cls or cls.group(1) == "federated":
            continue
        found.setdefault(cls.group(1), element)
    return found


def main() -> int:
    """Derive a template per connector class from the real exports given on the command line."""
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written = 0
    for raw in sys.argv[1:]:
        path = Path(raw)
        if not path.exists():
            print(f"  SKIP (not found): {path}")
            continue
        for cls, element in extract(read_xml(path)).items():
            target = OUT_DIR / f"{cls}.xml"
            scrubbed = scrub(element).strip() + "\n"
            if cls in seen:
                # FIRST source wins, deliberately. Tableau writes different attribute shapes for the
                # same connector depending on which document-format features the workbook enabled,
                # and letting a later file clobber an earlier one silently discarded the
                # `_.fcp.DatabricksCatalog.true...v-http-path` variant - the exact shape that broke
                # two parsers. Pass the richest export first; differences are reported, not merged.
                differs = target.read_text(encoding="utf-8") != scrubbed
                print(f"  {cls:<14} kept earlier template ({'DIFFERS' if differs else 'identical'} in {path.name})")
                continue
            target.write_text(scrubbed, encoding="utf-8")
            seen.add(cls)
            written += 1
            print(f"  {cls:<14} <- {path.name}  -> {target}")

    print(f"\n{written} template(s) written. Attribute SHAPE is verbatim from real exports;")
    print("only endpoint values are placeheld, so these carry no hostnames and are safe to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
