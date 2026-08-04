"""
purpose: turn a deterministic-tier PBIP bundle into a ONE-ROW PROBE VARIANT, so a live source can be
         proven reachable FROM POWER BI using the SAME M the real model will use.
usage:   python scripts/probe_bundle.py <emitted-bundle-dir> --out <probe-dir> [--rows 1]
                                        [--keep-dax] [--check-only]

Why this exists
---------------
`probe_live_source.py` proves reachability by HAND-WRITING a one-table model per connector class
(`Sql.Database(...)`, `Snowflake.Databases(...)`, `Databricks.Catalogs(...)`). Its own docstring
concedes the fragility: *"The probe mirrors the builder on purpose ... If the builder's connector
shape changes, change this with it."*

That was safe while OUR agent was the builder. It is not safe now that the deterministic tier
(`tableau-fabric-skills`) emits the model on its own release schedule: a hand-maintained mirror
drifts silently, and a probe that drifts is worse than no probe - it returns a FALSE GREEN.

Measured on a real emitted bundle (`connection-test-workbook`, 3 connector classes in one model):

    partitions REFERENCE : #"Server"  #"Database"  #"HttpPath"  #"Warehouse"
    expressions DEFINE   : #"Server"  #"Database"

`#"HttpPath"` and `#"Warehouse"` are never defined, so the real model dies at refresh on M name
resolution. The hand-written probe substitutes LITERALS for those parameters, so it connects
happily and PASSES - certifying a model that cannot open. A probe derived from the emitted bundle
inherits the same undefined references and fails exactly where the real model fails.

Why `Table.FirstN` and not a native `SELECT 1`
----------------------------------------------
Two reasons, and the second is the one that matters.

1. `Table.FirstN(source, 1)` is a folding operation: against SQL Server / Snowflake / Databricks the
   mashup engine pushes it down as `TOP 1` / `LIMIT 1`, so it IS the select-1 at the source, without
   this script needing to know a single SQL dialect. It is also connector-agnostic - one transform
   covers every connector the deterministic tier supports today or adds tomorrow.
   WARNING: folding is documented behaviour, not something this script can verify offline. Confirm
   in the source system's query history (that trace is the real oracle - see `credential_gate.py`).

2. Running `SELECT 1` from a shell tests the WRONG CREDENTIAL. `databricks sql`, an ODBC call or an
   `az` token authenticate as the agent's shell identity; Power BI uses a credential cached
   per-Windows-user. Only a query issued BY Power BI, through the emitted M, proves the thing the
   gate cares about. See `probe_live_source.py` for the full argument.

What a pass proves, and what it does not
----------------------------------------
PROVES  : the connector resolves, every M parameter is defined, the credential is bound, the object
          is readable, and Power BI can return a row.
DOES NOT: prove the full load succeeds. Type drift on row 500,000 is invisible at row 1. Record that
          limitation in the credential receipt rather than implying full validation.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# A `partition <name> = m` block, up to the next sibling declaration. TMDL is indentation-scoped, so
# the lookahead terminates on the next line that starts a new object at any indent.
_PARTITION_M = re.compile(
    r"(?ms)^(?P<head>[ \t]*partition\s+.+?=\s*m\b)(?P<body>.*?)"
    r"(?=^[ \t]*(?:partition|column|measure|hierarchy|annotation|changedProperty)\b|\Z)"
)

# The final `in <expr>` of a let-expression. This is the ONLY line the probe transform touches.
_LET_IN = re.compile(r"(?ms)(?P<lead>\n(?P<indent>[ \t]*)in[ \t]*\n[ \t]*)(?P<expr>[^\r\n]+)")

# Marker written into the wrapped expression so the transform is EXACTLY reversible and auditable.
# Without it `Table.FirstN(...)` written by this script is indistinguishable from one the emitter or
# a human wrote, so `unwrap` could silently strip a legitimate row limit - and, worse, a shipping
# bundle could not be proven probe-free.
PROBE_MARKER = "/*PROBE*/"

# A wrapped expression: `Table.FirstN(<expr>, <n>) /*PROBE*/`
_WRAPPED = re.compile(r"Table\.FirstN\((?P<expr>.+),\s*\d+\)\s*" + re.escape(PROBE_MARKER))

# `measure 'X' = ...` / `column 'X' = ...` (calculated). A SOURCE column has no `=`.
_DAX_OBJECT = re.compile(
    r"(?ms)^(?P<indent>[ \t]*)(?P<kind>measure|column)\s+(?P<name>'[^']+'|[^\s=]+)\s*=.*?"
    r"(?=^[ \t]*(?:partition|column|measure|hierarchy|annotation|changedProperty)\b|\Z)"
)

_M_PARAM_REF = re.compile(r'#"([^"]+)"')
_M_PARAM_DEF = re.compile(r"(?m)^\s*expression\s+(?:'([^']+)'|([^\s=]+))\s*=")


def find_model_dir(bundle: Path) -> Path:
    """Locate the `*.SemanticModel/definition` folder inside an emitted bundle."""
    hits = sorted(bundle.rglob("*.SemanticModel/definition"))
    if not hits:
        raise SystemExit(f"ERROR: no *.SemanticModel/definition under {bundle}")
    # Prefer the copy under `pbip/` when the emitter writes the model twice.
    for h in hits:
        if "pbip" in h.parts:
            return h
    return hits[0]


def wrap_partitions(tmdl: str, rows: int) -> tuple[str, int]:
    """Wrap the final expression of every M partition in `Table.FirstN(..., rows)`."""
    count = 0

    def _fix(match: re.Match[str]) -> str:
        nonlocal count
        body = match.group("body")
        inner = _LET_IN.search(body)
        if not inner:
            return match.group(0)
        expr = inner.group("expr").strip()
        if PROBE_MARKER in expr:
            return match.group(0)
        wrapped = f"Table.FirstN({expr}, {rows}) {PROBE_MARKER}"
        new_body = body[: inner.start("expr")] + wrapped + body[inner.end("expr") :]
        count += 1
        return match.group("head") + new_body

    return _PARTITION_M.sub(_fix, tmdl), count


def unwrap_partitions(tmdl: str) -> tuple[str, int]:
    """Reverse `wrap_partitions`, restoring the emitter's original expression byte-for-byte.

    Only wrappers carrying `PROBE_MARKER` are removed, so a row limit written by the emitter or by a
    human is never touched.
    """
    return _WRAPPED.subn(r"\g<expr>", tmdl)


def find_probe_residue(root: Path) -> list[Path]:
    """Every TMDL under `root` still carrying a probe wrapper.

    A shipping bundle MUST be probe-free: a leftover wrapper yields a model that opens, refreshes
    and renders while containing exactly one row per table - a silent, plausible-looking corruption
    that no validator flags.
    """
    return [p for p in sorted(root.rglob("*.tmdl")) if PROBE_MARKER in p.read_text(encoding="utf-8", errors="replace")]


def force_import_mode(tmdl: str) -> tuple[str, int]:
    """Flip `mode: directQuery` to `mode: import`.

    A DirectQuery partition is not read at refresh at all - it is queried at render time - so a
    refresh against a DQ model proves nothing about the credential. Import mode is what forces the
    mashup engine to actually connect.
    """
    out, n = re.subn(r"(?m)^(\s*mode:\s*)directQuery\s*$", r"\1import", tmdl)
    return out, n


def strip_dax_objects(tmdl: str) -> tuple[str, int]:
    """Remove measures and CALCULATED columns, keeping source columns.

    Measured 2026-08-04: a single calculated column whose expression errors takes the whole model
    down - every query against it fails, not just the ones touching that column. A connectivity
    probe must isolate the connection, so DAX objects are dropped rather than risked.
    """
    count = 0

    def _drop(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return ""

    return _DAX_OBJECT.sub(_drop, tmdl), count


def check_m_parameters(model_dir: Path) -> dict[str, list[str]]:
    """Compare `#"Name"` references across partitions against `expression Name` definitions.

    This is the `M_PARAM_UNDEFINED` defect class, and it is worth running on its own: it is a static
    check that needs no credential, no Desktop and no network, and neither
    `powerbi-report-author validate` nor the deterministic tier's own `openability_selfcheck`
    detects it (measured: both report clean on a bundle with two undefined parameters).
    """
    defined: set[str] = set()
    referenced: set[str] = set()
    for tmdl in model_dir.rglob("*.tmdl"):
        text = tmdl.read_text(encoding="utf-8", errors="replace")
        for match in _M_PARAM_DEF.finditer(text):
            defined.add(match.group(1) or match.group(2))
        referenced.update(_M_PARAM_REF.findall(text))
    return {
        "defined": sorted(defined),
        "referenced": sorted(referenced),
        "undefined": sorted(referenced - defined),
    }


def build_probe(bundle: Path, out: Path, rows: int, keep_dax: bool) -> dict:
    """Copy `bundle` to `out` and rewrite it into a one-row probe variant."""
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(bundle, out)

    model_dir = find_model_dir(out)
    stats = {"tables": 0, "partitions_wrapped": 0, "dq_flipped": 0, "dax_stripped": 0}

    for tmdl in sorted((model_dir / "tables").glob("*.tmdl")):
        text = tmdl.read_text(encoding="utf-8")
        original = text
        text, wrapped = wrap_partitions(text, rows)
        text, flipped = force_import_mode(text)
        stripped = 0
        if not keep_dax:
            text, stripped = strip_dax_objects(text)
        if text != original:
            tmdl.write_text(text, encoding="utf-8")
        stats["tables"] += 1
        stats["partitions_wrapped"] += wrapped
        stats["dq_flipped"] += flipped
        stats["dax_stripped"] += stripped

    receipt = {
        "probe_variant_of": str(bundle),
        "probe_bundle": str(out),
        "rows_per_partition": rows,
        "stats": stats,
        "m_parameters": check_m_parameters(model_dir),
        "proves": [
            "connector resolves",
            "every M parameter is defined",
            "credential is bound in Power BI",
            "object is readable",
            "a row can be returned",
        ],
        "does_not_prove": [
            "the full load succeeds (type drift beyond row 1 is invisible)",
            "query folding actually occurred (confirm in the source system's query history)",
        ],
    }
    (out / "probe-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


# The CLI is a small dispatcher over 4 mutually exclusive modes; splitting it would hide the flow.
# pylint: disable=too-many-return-statements
def main() -> int:
    """Parse arguments and dispatch to the requested mode."""
    parser = argparse.ArgumentParser(description="Build a one-row probe variant of an emitted PBIP bundle.")
    parser.add_argument("bundle", type=Path, help="the emitted bundle (contains *.SemanticModel)")
    parser.add_argument("--out", type=Path, help="where to write the probe variant")
    parser.add_argument("--rows", type=int, default=1, help="rows per partition (default 1)")
    parser.add_argument("--keep-dax", action="store_true", help="keep measures and calculated columns")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="only run the M-parameter check on the bundle; write nothing",
    )
    parser.add_argument(
        "--unwrap",
        action="store_true",
        help="reverse the probe wrap IN PLACE, restoring the emitter's expressions",
    )
    parser.add_argument(
        "--assert-clean",
        action="store_true",
        help="exit non-zero if any probe wrapper remains (run before shipping a bundle)",
    )
    args = parser.parse_args()

    if not args.bundle.exists():
        print(f"ERROR: {args.bundle} does not exist", file=sys.stderr)
        return 2

    if args.assert_clean:
        residue = find_probe_residue(args.bundle)
        if residue:
            print(f"PROBE RESIDUE: {len(residue)} file(s) still carry {PROBE_MARKER}:", file=sys.stderr)
            for path in residue[:10]:
                print(f"  {path}", file=sys.stderr)
            print("This bundle would ship with ONE ROW per table. Run --unwrap.", file=sys.stderr)
            return 1
        print(f"OK - no probe wrappers under {args.bundle}.")
        return 0

    if args.unwrap:
        total = 0
        for tmdl in sorted(args.bundle.rglob("*.tmdl")):
            text = tmdl.read_text(encoding="utf-8")
            restored, n = unwrap_partitions(text)
            if n:
                tmdl.write_text(restored, encoding="utf-8")
                total += n
        print(f"unwrapped {total} partition(s) in {args.bundle}")
        return 0

    if args.check_only:
        params = check_m_parameters(find_model_dir(args.bundle))
        print(f"M parameters defined   : {', '.join(params['defined']) or '(none)'}")
        print(f"M parameters referenced: {', '.join(params['referenced']) or '(none)'}")
        if params["undefined"]:
            print(f"M_PARAM_UNDEFINED: {', '.join(params['undefined'])}")
            print("The model references M parameters that are never defined; it cannot refresh.")
            return 1
        print("OK - every referenced M parameter is defined.")
        return 0

    if not args.out:
        print("ERROR: --out is required unless --check-only is given", file=sys.stderr)
        return 2

    receipt = build_probe(args.bundle, args.out, args.rows, args.keep_dax)
    stats = receipt["stats"]
    print(f"probe bundle: {args.out}")
    print(
        f"  tables {stats['tables']} | partitions wrapped {stats['partitions_wrapped']}"
        f" | directQuery->import {stats['dq_flipped']} | DAX objects stripped {stats['dax_stripped']}"
    )
    undefined = receipt["m_parameters"]["undefined"]
    if undefined:
        print(f"  M_PARAM_UNDEFINED: {', '.join(undefined)} - this bundle CANNOT refresh as emitted.")
        return 1
    print("  M parameters: all referenced parameters are defined.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
