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
A **pass** here means a refresh that RAN and returned a row, not a probe variant that was built.
Those are different events, and this script keeps them apart:

  build  (`--out`)      -> receipt.status = BUILT_NOT_EXECUTED, proves []
  refresh (ran by you)  -> `--record DATA_OK|NO_DATA|TIMEOUT|CREDENTIAL_REQUIRED|ERROR`
                        -> receipt.status = EXECUTED, proves populated FROM THE OUTCOME

PROVES (only on a recorded `DATA_OK`): the connector resolves, every M parameter is defined, the
          credential is bound, the object is readable, and Power BI returned a row.
DOES NOT: prove the full load succeeds. Type drift on row 500,000 is invisible at row 1.

⚠️ This split exists because of a real defect in this file. The receipt used to assert
"credential is bound in Power BI" and "a row can be returned" the moment the files were rewritten -
having opened nothing, bound nothing and read nothing. It was a false green emitted by the script
written to prevent false greens, and it would certify an unreachable source. Building a probe is
preparation; only an executed refresh is evidence.
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

RECEIPT_NAME = "probe-receipt.json"

# Receipt lifecycle. A probe variant is BUILT by rewriting files, and only becomes EXECUTED when a
# refresh has actually run against it. Keeping these distinct is the whole point of the receipt:
# a consumer can tell "prepared" from "proven" without reading the code that wrote it.
STATUS_BUILT = "BUILT_NOT_EXECUTED"
STATUS_EXECUTED = "EXECUTED"

OUTCOME_DATA_OK = "DATA_OK"
OUTCOME_PARTIAL = "PARTIAL"
OUTCOME_NO_DATA = "NO_DATA"
OUTCOME_TIMEOUT = "TIMEOUT"
OUTCOME_CREDENTIAL_REQUIRED = "CREDENTIAL_REQUIRED"
OUTCOME_ERROR = "ERROR"
REFRESH_OUTCOMES = frozenset(
    {
        OUTCOME_DATA_OK,
        OUTCOME_PARTIAL,
        OUTCOME_NO_DATA,
        OUTCOME_TIMEOUT,
        OUTCOME_CREDENTIAL_REQUIRED,
        OUTCOME_ERROR,
    }
)

# A wrapped expression: `Table.FirstN(<expr>, <n>) /*PROBE*/`
_WRAPPED = re.compile(r"Table\.FirstN\((?P<expr>.+),\s*\d+\)\s*" + re.escape(PROBE_MARKER))

# The line that OPENS a calculated object: `measure 'X' = ...` / `column 'X' = ...`.
# A SOURCE column has no `=`, so it never matches and is never stripped.
_DAX_OBJECT_HEAD = re.compile(r"^(?P<indent>[ \t]*)(?P<kind>measure|column)\s+(?P<name>'[^']+'|[^\s=]+)\s*=")

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


def _indent_width(line: str) -> int:
    """Visual indent of a line, tabs expanded, so tab- and space-indented TMDL compare correctly."""
    return len(line[: len(line) - len(line.lstrip())].expandtabs(4))


def _block_end(lines: list[str], start: int) -> int:
    """Index just past the block opened at `start` (every following deeper-indented line)."""
    own = _indent_width(lines[start])
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and _indent_width(line) <= own:
            break
        index += 1
    return index


def _repair_dangling_refs(lines: list[str], removed: set[str]) -> list[str]:
    """Drop hierarchy levels and sort-by references that point at a column we just removed.

    Without this the file is still well-formed TMDL - and still fails to open. Measured 2026-08-05,
    Desktop 2.157:

        Cannot resolve all the paths while de-serializing Database.
        Property Column of object "level Year in hierarchy Calendar in table Date" refers to an
        object which cannot be found

    A hierarchy that loses every level is removed too, since an empty hierarchy is not valid.
    """
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("level "):
            end = _block_end(lines, index)
            target = None
            for inner in lines[index:end]:
                match = re.match(r"\s*column:\s*'?([^'\r\n]+?)'?\s*$", inner)
                if match:
                    target = match.group(1)
            if target in removed:
                index = end
                continue
            out.extend(lines[index:end])
            index = end
            continue

        if stripped.startswith("hierarchy "):
            end = _block_end(lines, index)
            kept = _repair_dangling_refs(lines[index + 1 : end], removed)
            if any(part.strip().startswith("level ") for part in kept):
                out.append(line)
                out.extend(kept)
            index = end
            continue

        match = re.match(r"\s*sortByColumn:\s*'?([^'\r\n]+?)'?\s*$", line)
        if match and match.group(1) in removed:
            index += 1
            continue

        out.append(line)
        index += 1
    return out


def strip_dax_objects(tmdl: str) -> tuple[str, int]:
    """Remove measures, and calculated columns from M-BACKED tables only.

    Measured 2026-08-04: a single calculated column whose expression errors takes the whole model
    down - every query against it fails, not just the ones touching that column. A connectivity
    probe must isolate the connection, so DAX objects are dropped rather than risked.

    Two hard-won boundaries, both found only by opening the result in Power BI Desktop while every
    static check - ours and the deterministic tier's `tmdl_lint`/`openability_gate` - reported clean:

    1. **A CALCULATED table is left alone.** Its columns are not decoration, they ARE the table
       (`Date` is `CALENDAR(...)`, its Year/Quarter/Month columns derive from that), and it never
       touches the data source, so stripping it buys a connectivity probe nothing while destroying
       the table. Measures are still stripped everywhere.
    2. **Whatever is removed must not leave a dangling reference.** See `_repair_dangling_refs`.

    The earlier single-regex implementation also stopped at `^annotation`, orphaning a deleted
    object's own annotations onto its predecessor - Desktop then refused to open the project with
    "TMDL objects cannot be merged because both declare the same property". The rule is now
    STRUCTURAL: an object owns every following line indented deeper than its own declaration.
    """
    lines = tmdl.splitlines(keepends=True)
    table_is_calculated = any(re.match(r"\s*partition\s+.*=\s*calculated\s*$", ln) for ln in lines)

    kept: list[str] = []
    removed: set[str] = set()
    index = 0
    count = 0

    while index < len(lines):
        head = _DAX_OBJECT_HEAD.match(lines[index])
        if not head:
            kept.append(lines[index])
            index += 1
            continue

        if head.group("kind") == "column" and table_is_calculated:
            kept.append(lines[index])
            index += 1
            continue

        removed.add(head.group("name").strip("'"))
        index = _block_end(lines, index)
        count += 1

    if removed:
        kept = _repair_dangling_refs(kept, removed)

    return "".join(kept), count


def check_m_parameters(model_dir: Path) -> dict[str, list[str]]:
    """Compare `#"Name"` references across partitions against `expression Name` definitions.

    This is the `M_PARAM_UNDEFINED` defect class, and it is worth running on its own: it is a static
    check that needs no credential, no Desktop and no network, and neither
    `powerbi-report-author validate` nor the deterministic tier's own `openability_selfcheck`
    detects it (measured: both report clean on a bundle with two undefined parameters).

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
    "the sources are covered", and reporting that as OK is the false-green this whole script exists
    to prevent - the same defect the receipt lifecycle was written to kill.
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
        # ---------------------------------------------------------------------------------
        # NOTHING here is a connectivity claim, and that is deliberate. This function has
        # copied a directory and rewritten regexes; it has not opened Power BI, not bound a
        # credential, and not read a row. An earlier version of this receipt asserted
        # "credential is bound in Power BI" and "a row can be returned" at exactly this
        # point - a false green produced by the very script written to prevent false greens.
        # Connectivity claims are now written ONLY by record_refresh_result(), and only from
        # the outcome of a refresh that actually ran.
        # ---------------------------------------------------------------------------------
        "status": STATUS_BUILT,
        "refresh": None,
        "proves": [],
        "statically_checked": [
            "every M parameter referenced by a partition has a definition in the bundle",
            "every partition expression is wrapped to at most "
            f"{rows} row(s), so a refresh measures REACHABILITY, not data volume",
        ],
        "does_not_prove": [
            "the connector resolves",
            "the credential is bound in Power BI",
            "the object is readable",
            "a row can be returned",
            "the full load succeeds",
        ],
    }
    _write_receipt(out, receipt)
    return receipt


def _write_receipt(probe_dir: Path, receipt: dict) -> None:
    (probe_dir / RECEIPT_NAME).write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def read_receipt(probe_dir: Path) -> dict:
    """Read a probe receipt, or raise if the directory is not a probe variant."""
    path = probe_dir / RECEIPT_NAME
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - {probe_dir} is not a probe variant")
    return json.loads(path.read_text(encoding="utf-8"))


def record_refresh_result(
    probe_dir: Path,
    outcome: str,
    *,
    detail: str | None = None,
    elapsed_sec: float | None = None,
    table_rows: dict[str, int] | None = None,
) -> dict:
    """Update a probe receipt from a refresh that ACTUALLY RAN.

    This is the only function permitted to write a connectivity claim, and it derives that claim
    from `outcome` rather than from having been called. `DATA_OK` is the single outcome that
    proves anything; every other outcome proves nothing and says so, because the failure modes are
    not equivalent - a TIMEOUT is uninformative (it may be a slow source *or* an unreachable one),
    whereas CREDENTIAL_REQUIRED is a final answer that no retry can change.

    `table_rows` maps table name -> rows returned by the probe, and exists because a MODEL-level
    verdict is not safe for a mixed model. Measured 2026-08-04 on a Databricks migration: a 2.5-hour
    run produced a full model, a 62 KB `cache.abf`, and a green refresh - while the warehouse never
    left STOPPED with 0 sessions. The flat-file table had refreshed; both live tables were empty.
    A model-level `DATA_OK` would have certified a source that was never contacted. So when
    `table_rows` is supplied and ANY table returned 0 rows, a claimed `DATA_OK` is DOWNGRADED to
    `PARTIAL` and the receipt names the unproven tables, rather than a caller's optimistic verdict
    being taken at face value.
    """
    if outcome not in REFRESH_OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {sorted(REFRESH_OUTCOMES)}")

    # `read_receipt` first: "this is not a probe bundle" is the more fundamental complaint, and a
    # caller who got the directory wrong should hear that rather than a lecture about evidence.
    receipt = read_receipt(probe_dir)

    # A DATA_OK claim must ARRIVE WITH ITS EVIDENCE. Until 2026-08-06 the downgrade below could only
    # fire when `table_rows` was supplied, so `--record DATA_OK` with no measurements wrote the
    # strongest claim in the receipt backed by nothing at all - the caller's word. That is the same
    # false-green shape this module was written to kill (a receipt asserting "the credential is bound
    # in Power BI" having executed nothing), one level up: we stopped the BUILD step from claiming,
    # and left the RECORD step able to claim without measuring.
    #
    # An external caller still supplies the numbers, so this is not proof - it is the difference
    # between an unfalsifiable assertion and a checkable one. `tables_without_rows` names what failed;
    # a fabricated row count is now a specific, auditable lie rather than a vague optimistic verdict.
    if outcome == OUTCOME_DATA_OK and not table_rows:
        raise ValueError(
            "DATA_OK requires --table-rows: a connectivity claim must arrive with the per-table "
            "measurements that support it. Query the loaded model (probe_desktop_query.py exposes "
            "discover_port/table_names/probe) and pass what you measured. To record a refresh you "
            "could not measure per-table, use PARTIAL - it claims nothing."
        )

    receipt["status"] = STATUS_EXECUTED

    proven = sorted(t for t, n in (table_rows or {}).items() if n)
    unproven = sorted(t for t, n in (table_rows or {}).items() if not n)

    downgraded_from = None
    if outcome == OUTCOME_DATA_OK and unproven:
        downgraded_from = OUTCOME_DATA_OK
        outcome = OUTCOME_PARTIAL

    receipt["refresh"] = {
        "outcome": outcome,
        "downgraded_from": downgraded_from,
        "detail": detail,
        "elapsed_sec": round(elapsed_sec, 1) if elapsed_sec is not None else None,
        "table_rows": table_rows,
        "tables_with_rows": proven or None,
        "tables_without_rows": unproven or None,
        # Provenance, so a reader never has to guess how strong this is. `probe_bundle` does not run
        # the refresh, so every measurement here reached it from outside. Saying so in the receipt is
        # the difference between evidence and hearsay-presented-as-evidence.
        "evidence_source": "caller-supplied measurements (probe_bundle does not execute the refresh)",
    }

    if outcome == OUTCOME_DATA_OK:
        rows = receipt.get("rows_per_partition", 1)
        scope = f"all {len(proven)} table(s)" if proven else "the model"
        receipt["proves"] = [
            "the connector resolves",
            "every M parameter is defined",
            "the credential is bound in Power BI",
            "the object is readable",
            f"at least one row can be returned for {scope} (probe limit {rows}/partition)",
        ]
        receipt["does_not_prove"] = [
            "the full load succeeds (type drift beyond the probe limit is invisible)",
            "query folding actually occurred (confirm in the source system's query history)",
            "any DAX is correct (measures are stripped unless --keep-dax)",
        ]
    elif outcome == OUTCOME_PARTIAL:
        receipt["proves"] = [f"'{t}' returned at least one row" for t in proven]
        receipt["does_not_prove"] = [
            f"'{t}' is reachable - it returned NO rows, so its source was never proven" for t in unproven
        ] + [
            "the model as a whole is loadable; a per-table verdict is the only safe reading here",
        ]
    else:
        receipt["proves"] = []
        receipt["does_not_prove"] = [
            f"nothing - the probe refresh ended {outcome}, so no connectivity claim is supported",
        ]

    _write_receipt(probe_dir, receipt)
    return receipt


def _cmd_record(args: argparse.Namespace) -> int:
    """Record an executed refresh outcome into an existing probe receipt."""
    table_rows = json.loads(args.table_rows) if args.table_rows else None
    receipt = record_refresh_result(
        args.bundle,
        args.record,
        detail=args.detail,
        elapsed_sec=args.elapsed_sec,
        table_rows=table_rows,
    )
    refresh = receipt["refresh"]
    print(f"receipt: {args.bundle / RECEIPT_NAME}")
    print(f"  status  : {receipt['status']}")
    print(f"  outcome : {refresh['outcome']}")
    if refresh["downgraded_from"]:
        print(
            f"  DOWNGRADED from {refresh['downgraded_from']}: "
            f"{', '.join(refresh['tables_without_rows'])} returned no rows"
        )
    if receipt["proves"]:
        for claim in receipt["proves"]:
            print(f"  PROVES  : {claim}")
    else:
        print("  PROVES  : (nothing - a non-DATA_OK refresh supports no connectivity claim)")
    return 0 if refresh["outcome"] == OUTCOME_DATA_OK else 1


# The CLI is a small dispatcher over 5 mutually exclusive modes; splitting it would hide the flow.
# pylint: disable=too-many-return-statements,too-many-branches,too-many-statements
def run_check_only(bundle: Path, spec: Path | None) -> int:
    """Static, offline defect checks: undefined/empty M parameters, then endpoint coverage.

    Split out of ``main`` because it is the part that grows: each new static defect class lands
    here, and every one of them must be able to say UNKNOWN rather than being forced into a
    pass/fail it cannot honestly make.
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
        # must never print as OK - claiming coverage we did not check is the exact false green
        # the receipt lifecycle exists to prevent.
        print(f"SOURCE_COVERAGE: UNKNOWN - {coverage['reason']}")
    else:
        print(f"OK - all {len(coverage['declared'])} declared endpoint(s) are reachable in the model.")
    print("OK - every referenced M parameter is defined and non-empty.")
    return 0


def main() -> int:
    """Parse arguments and dispatch to the requested mode."""
    parser = argparse.ArgumentParser(description="Build a one-row probe variant of an emitted PBIP bundle.")
    parser.add_argument("bundle", type=Path, help="the emitted bundle (contains *.SemanticModel)")
    parser.add_argument("--out", type=Path, help="where to write the probe variant")
    parser.add_argument(
        "--spec",
        type=Path,
        help="migration-spec.json - enables the SOURCE_COLLAPSED endpoint-coverage check "
        "(without it, coverage reports UNKNOWN rather than passing silently)",
    )
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
    parser.add_argument(
        "--record",
        choices=sorted(REFRESH_OUTCOMES),
        help="record the outcome of a refresh that ACTUALLY RAN against this probe variant; "
        "only this writes a connectivity claim into the receipt",
    )
    parser.add_argument("--detail", help="free-text detail to store alongside --record")
    parser.add_argument("--elapsed-sec", type=float, help="wall-clock seconds the refresh took")
    parser.add_argument(
        "--table-rows",
        help='per-table rows the probe returned, as JSON: \'{"Sales": 1, "Orders": 0}\'. '
        "A claimed DATA_OK with any 0-row table is downgraded to PARTIAL.",
    )
    args = parser.parse_args()

    if not args.bundle.exists():
        print(f"ERROR: {args.bundle} does not exist", file=sys.stderr)
        return 2

    if args.record:
        return _cmd_record(args)

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
        return run_check_only(args.bundle, args.spec)

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
    empty = receipt["m_parameters"]["empty"]
    if undefined:
        print(f"  M_PARAM_UNDEFINED: {', '.join(undefined)} - this bundle CANNOT refresh as emitted.")
        return 1
    if empty:
        print(
            f"  M_PARAM_EMPTY: {', '.join(empty)} - defined but blank. A partition that navigates"
            " with an empty parameter CANNOT refresh; supply a value before probing."
        )
        return 1
    print("  M parameters: all referenced parameters are defined and non-empty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
