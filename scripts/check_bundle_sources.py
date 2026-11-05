"""
purpose: static, offline source checks on an emitted PBIP bundle before anyone opens it in Desktop:
         undefined or empty M parameters (`M_PARAM_UNDEFINED` / `M_PARAM_EMPTY`) and declared live
         endpoints the model cannot reach (`SOURCE_COLLAPSED`).
usage:   python scripts/check_bundle_sources.py <emitted-bundle-dir> [--spec <migration-spec.json>]

Exit codes: 0 no finding (`SOURCE_COVERAGE: UNKNOWN` is printed, not passed off as OK), 1 a finding
or no `*.SemanticModel/definition` in the bundle, 2 the bundle does not exist.

It needs no credential, no Desktop and no network, so it runs before the live refresh: a model that
references an undefined parameter, or points two declared servers at one, cannot be proven by
refreshing it. Neither `powerbi-report-author validate` nor the deterministic tier's own
`openability_selfcheck` detects the parameter defect (measured: both report clean on a bundle with
two undefined parameters). Extracted unchanged from the retired probe builder's `--check-only` mode.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_M_PARAM_REF = re.compile(r'#"([^"]+)"')
# Connector functions whose FIRST argument is the upstream host. Used by `check_source_coverage` to
# resolve which endpoints a model can actually reach. Deliberately a fixed list: an unrecognised
# connector yields UNKNOWN (an honest "cannot tell"), never a silent pass.
_M_CONNECTOR_CALL = re.compile(
    r"\b(?:Sql\.Database|Snowflake\.Databases|Databricks\.Catalogs|Oracle\.Database"
    r"|MySQL\.Database|PostgreSQL\.Database|AmazonRedshift\.Database|Odbc\.DataSource"
    r"|GoogleBigQuery\.Database|Teradata\.Database)\s*\((?P<args>[^)]*)\)"
)
_M_PARAM_DEF = re.compile(r"(?m)^\s*expression\s+(?:'([^']+)'|([^\s=]+))\s*=\s*(?P<value>[^\r\n]*)")
# `... meta [IsParameterQuery=true, ...]` trails the value and is not part of it.
_M_PARAM_META = re.compile(r"\s+meta\s*\[.*$")


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


def check_m_parameters(model_dir: Path) -> dict[str, list[str]]:
    """Compare `#"Name"` references across partitions against `expression Name` definitions.

    This is the `M_PARAM_UNDEFINED` defect class.

    It also reports `empty` - a parameter that IS defined but holds `""`. Measured 2026-08-05 on a
    real Snowflake `.tdsx` whose `<connection warehouse=''>` was blank: the emitter correctly wrote
    `expression Warehouse = ""` with a TODO comment, and this function - checking only for the
    NAME's existence - reported "all referenced parameters are defined" and exited 0. A partition
    calling `Snowflake.Databases(#"Server", #"Warehouse")` with an empty warehouse cannot refresh,
    so an empty required parameter is exactly as fatal as a missing one and must not pass.
    """
    defined: dict[str, str] = {}
    referenced: set[str] = set()
    for tmdl in model_dir.rglob("*.tmdl"):
        text = tmdl.read_text(encoding="utf-8", errors="replace")
        for match in _M_PARAM_DEF.finditer(text):
            name = match.group(1) or match.group(2)
            value = _M_PARAM_META.sub("", match.group("value")).strip()
            defined[name] = value
        referenced.update(_M_PARAM_REF.findall(text))

    empty = sorted(n for n in referenced if n in defined and defined[n] in ("", '""', "''"))
    return {
        "defined": sorted(defined),
        "referenced": sorted(referenced),
        "undefined": sorted(referenced - set(defined)),
        "empty": empty,
    }


def _declared_endpoints(doc: dict) -> set[str]:
    """Every distinct live-source host the spec declares, lowercased.

    A single-connection datasource predates (and does not need) the plural ``connections[]`` array,
    so the scalar ``server`` is used as a one-leg fallback - otherwise an ordinary one-source
    workbook would report UNKNOWN forever and the check would be dead weight.
    """
    hosts: set[str] = set()
    for source in doc.get("data_sources") or []:
        conn = source.get("connection") or {}
        legs = conn.get("connections")
        if legs is None:
            legs = [conn]
        for leg in legs:
            if (leg.get("powerbi_target") or conn.get("powerbi_target")) != "live_source":
                continue  # extracts / flat files are materialised and reference no Server parameter
            server = (leg.get("server") or "").strip().lower()
            if server:
                hosts.add(server)
    return hosts


def _reached_endpoints(model_dir: Path) -> set[str]:
    """Every distinct host the emitted model can actually resolve, lowercased.

    A ``#"Name"`` first argument is resolved through the model's own ``expression`` definitions, so
    a parameterised and a hard-coded connection are compared on equal terms.
    """
    defined: dict[str, str] = {}
    for tmdl in model_dir.rglob("*.tmdl"):
        text = tmdl.read_text(encoding="utf-8", errors="replace")
        for match in _M_PARAM_DEF.finditer(text):
            name = match.group(1) or match.group(2)
            defined[name] = _M_PARAM_META.sub("", match.group("value")).strip().strip(chr(34))

    hosts: set[str] = set()
    for tmdl in model_dir.rglob("*.tmdl"):
        text = tmdl.read_text(encoding="utf-8", errors="replace")
        for match in _M_CONNECTOR_CALL.finditer(text):
            args = [a for a in match.group("args").split(",") if a.strip()]
            if not args:
                continue
            arg = args[0].strip()
            ref = _M_PARAM_REF.fullmatch(arg)
            host = defined.get(ref.group(1), "") if ref else arg.strip(chr(34))
            if host.strip():
                hosts.add(host.strip().lower())
    return hosts


def _load_spec(spec: Path | None) -> tuple[dict | None, str]:
    """Read the migration spec, or explain why it could not be read."""
    if spec is None:
        return None, "no --spec given; endpoint coverage was not checked"
    if not spec.is_file():
        return None, f"spec not found: {spec}"
    try:
        return json.loads(spec.read_text(encoding="utf-8")), ""
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"spec unreadable: {type(exc).__name__}"


def check_source_coverage(model_dir: Path, spec: Path | None) -> dict:
    """Compare the DISTINCT upstream endpoints the spec declares against those the model can reach.

    This is the `SOURCE_COLLAPSED` defect class, and it is the *silent* sibling of
    `M_PARAM_UNDEFINED`. Measured 2026-08-05 against the deterministic tier at 2.59.0
    (upstream issue #91): a Tableau cross-database join over two Azure SQL servers emitted

        expression Server   = "sales-sql.database.windows.net"   <- the FIRST connection only
        expression Database = "salesdb"
        Orders     -> Sql.Database(#"Server", #"Database")   correct
        Employees  -> Sql.Database(#"Server", #"Database")   WRONG - belongs to hr-sql / hrdb

    Nothing caught this when the check was written. Every parameter IS defined, so
    `check_m_parameters` returns clean; the TMDL is well-formed, so `tmdl_lint` exits 0; the model
    refreshes without error. It then either fails with a confusing "invalid object name" or - if a
    same-named table exists on the first server - **returns the wrong data and looks completely
    healthy**.

    ⚠️ **The engine now catches this natively too, and that is a good thing - this check is a
    deliberate SECOND opinion, not the only defense.** Since upstream 2.75.0 (issue #93) its
    `openability_gate.endpoints_distinct` compares the emitted endpoint parameter groups against an
    `expected_endpoints` count derived from its own descriptor, and the verdict lands in
    `report.json` at `workbooks[].openability_selfcheck.checks.endpoints_distinct`.
    The two are not redundant, because they count from **different sources**: the engine compares its
    build against its own parse, so a mis-parse is invisible to it; we compare against the endpoints
    declared in `migration-spec.json`, parsed independently. Ours therefore still catches a bad
    *parse*, and it stays silent where the engine's does (its check is skipped entirely when it
    cannot derive `expected_endpoints`, e.g. flat-file islands with no parameterised endpoints).

    The invariant: a datasource declaring N distinct upstream endpoints must produce a model that
    resolves N distinct endpoints. Collapsing N -> 1 means every table after the first is pointed at
    the wrong server.

    Returns `status` of `OK`, `SOURCE_COLLAPSED`, or `UNKNOWN`. **`UNKNOWN` is not a pass.** A spec
    that is missing, unreadable, or predates the `connections[]` array cannot support the claim
    "the sources are covered", and reporting that as OK is a false green.
    """
    doc, reason = _load_spec(spec)
    if doc is None:
        return {"status": "UNKNOWN", "reason": reason}

    declared = _declared_endpoints(doc)
    if not declared:
        return {
            "status": "UNKNOWN",
            "reason": "the spec declares no live-source endpoints (extract/flat-file only, or a "
            "spec predating the connections[] array) - nothing to compare",
        }

    reached = _reached_endpoints(model_dir)
    if not reached:
        return {
            "status": "UNKNOWN",
            "reason": "no recognised connector call found in the model - cannot compare endpoints",
        }

    if len(reached) < len(declared):
        return {
            "status": "SOURCE_COLLAPSED",
            "declared": sorted(declared),
            "reached": sorted(reached),
            "missing": sorted(declared - reached),
        }
    return {"status": "OK", "declared": sorted(declared), "reached": sorted(reached)}


def run_check_only(bundle: Path, spec: Path | None) -> int:
    """Static, offline defect checks: undefined/empty M parameters, then endpoint coverage.

    Each new static defect class lands here, and every one of them must be able to say UNKNOWN
    rather than being forced into a pass/fail it cannot honestly make.
    """
    model_dir = find_model_dir(bundle)
    params = check_m_parameters(model_dir)
    print(f"M parameters defined   : {', '.join(params['defined']) or '(none)'}")
    print(f"M parameters referenced: {', '.join(params['referenced']) or '(none)'}")
    if params["undefined"]:
        print(f"M_PARAM_UNDEFINED: {', '.join(params['undefined'])}")
        print("The model references M parameters that are never defined; it cannot refresh.")
        return 1
    if params["empty"]:
        print(f"M_PARAM_EMPTY: {', '.join(params['empty'])}")
        print("These are defined but blank. Navigating with an empty parameter cannot refresh.")
        return 1

    coverage = check_source_coverage(model_dir, spec)
    if coverage["status"] == "SOURCE_COLLAPSED":
        print(
            f"SOURCE_COLLAPSED: declared {len(coverage['declared'])} endpoint(s), "
            f"model reaches {len(coverage['reached'])}"
        )
        print(f"  declared: {', '.join(coverage['declared'])}")
        print(f"  reached : {', '.join(coverage['reached'])}")
        print(f"  MISSING : {', '.join(coverage['missing'])}")
        print(
            "  Every table bound to a missing endpoint is silently pointed at a DIFFERENT\n"
            "  server. This does NOT fail: it refreshes clean and returns the wrong data if a\n"
            "  same-named table exists there. See upstream issue #91."
        )
        return 1
    if coverage["status"] == "UNKNOWN":
        # Deliberately not an exit-1: it is not a defect, it is an absence of evidence. But it
        # must never print as OK - claiming coverage we did not check is a false green.
        print(f"SOURCE_COVERAGE: UNKNOWN - {coverage['reason']}")
    else:
        print(f"OK - all {len(coverage['declared'])} declared endpoint(s) are reachable in the model.")
    print("OK - every referenced M parameter is defined and non-empty.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the static checks."""
    parser = argparse.ArgumentParser(description="Static, offline source checks on an emitted PBIP bundle.")
    parser.add_argument("bundle", type=Path, help="the emitted bundle (contains *.SemanticModel)")
    parser.add_argument(
        "--spec",
        type=Path,
        help="migration-spec.json - enables the SOURCE_COLLAPSED endpoint-coverage check "
        "(without it, coverage reports UNKNOWN rather than passing silently)",
    )
    args = parser.parse_args(argv)

    if not args.bundle.exists():
        print(f"ERROR: {args.bundle} does not exist", file=sys.stderr)
        return 2
    return run_check_only(args.bundle, args.spec)


if __name__ == "__main__":
    raise SystemExit(main())
