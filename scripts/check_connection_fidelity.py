"""
purpose: fail loudly when a Tableau data source that must connect to a LIVE upstream system ships as
         a semantic model that instead reads a FLAT FILE - a connection silently downgraded so the
         model can never refresh and freezes the customer's data at export time.
usage:   python scripts/check_connection_fidelity.py <bundle-or-unit-dir> [...]
                                              [--model <dir>] [--json <file>] [--quiet]
                                              [--verbose] [--warn-only]

Why this exists
---------------
A workbook whose Tableau source is a live database connection can ship as a flat file - a CSV or a
materialised extract - and every existing gate stays green. `check_empty_model.py` confirms rows
land, `check_datamodel.py` confirms the M is well-formed, `powerbi-report-author validate` confirms
the report opens. The model refreshes and returns rows. It is simply no longer connected to the
customer's database and goes stale the moment new rows land upstream.

Real incident, a live customer estate, 2026-08-24: three Snowflake custom-SQL tables in
`Regional_Scorecard_Dashboard` were materialised from the packaged `.hyper` extract to CSV
(1,957,003 + 350,781 rows) because the engine deferred the custom-SQL translation and nothing
surfaced it. It validated clean, refreshed clean, was stale-by-construction, and was found by a
human reading a migration brief.

The discrimination that makes this hard
---------------------------------------
In the SAME bundle, other CSV-backed tables were LEGITIMATELY CSV in the Tableau source. "The model
reads a CSV" is NOT evidence of a defect. Only the comparison against what the source actually was
can separate them. A gate that flags CSV is worse than no gate: it fires on correct work, gets muted,
and then misses the real case.

Why the discriminator is `powerbi_target`, NOT `connection.mode`
----------------------------------------------------------------
The obvious key is `connection.mode` (extract | live). It is WRONG, and that incident is the
proof: a Snowflake source WITH an extract has `mode: extract` - the extract is Tableau's cache - so
keying on `mode == "live"` would classify the exact incident as a legitimate extract and miss it.
`docs/migration-spec.schema.json` says as much on `powerbi_target`: "Do NOT infer this from
mode=extract: an extract looks identical whether it caches a CSV or a Snowflake warehouse."

So this gate keys off the stamped `connection.powerbi_target` (`live_source` | `flat_file` |
`unknown`), falling back to `connection_target.powerbi_target(class, mode)` - the SAME canonical
decision the parser stamps - when the field is absent. `mode` alone is never the discriminator.

Decided vs drifted (the sanctioned escape hatch)
------------------------------------------------
Materialising an extract is sometimes correct, and the repo documents "extract-baked custom-SQL ->
model one flat table" as a legitimate pattern. So the gate distinguishes a downgrade someone RECORDED
from one nobody did. It asks "was this recorded?", NOT "was this reasonable?" - the second needs
judgement and would make this a review, not a gate. A live source is treated as decided when either:
  * a `limitations_encountered` entry made at a BUILD stage (`semantic_build`/`deploy`/`validate`)
    names the data source or one of its tables - a parse-stage note is the parser observing an
    extract, not an agent deciding to downgrade, so parse-stage entries never excuse anything; or
  * a generated-edit declaration (see `generated_edit_declarations.py`) targets a file-backed table
    in the emitted model - an explicit recorded edit to the very partition in question.

What this gate does NOT tell you
--------------------------------
* Whether a live source that DID stay connected points at the right server - that is fidelity, and
  needs a credentialed probe (`probe_live_source.py`).
* A PARTIAL downgrade inside a source that still has one live partition, or an exotic connection
  class this module cannot map to an M connector: both are reported as NOT_CHECKED for that source,
  never as PASS. Anything this module cannot prove is a downgrade is not called one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import check_empty_model
from bundle_corpus import shipping_models

# `_partition_blocks` is a private helper reused as-is; check_empty_model.py must not be edited, so it
# is imported rather than promoted to public. `classify_partition`/`model_parameters` are public.
from check_empty_model import _partition_blocks, classify_partition, model_parameters
from connection_target import FLAT_FILE, LIVE_SOURCE, UNKNOWN, powerbi_target
from generated_edit_declarations import load_generated_edit_declarations

REPORT_NAME = "connection-fidelity-check.json"

STATUS_OK = "OK"
STATUS_DOWNGRADED = "DOWNGRADED"
STATUS_SKIPPED = "SKIPPED"

EXIT_OK = 0
EXIT_DOWNGRADED = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 3

# Emitted-M categories (from check_empty_model.classify_partition) that mean the rows come from a
# live connection at query time or an Import over a database connector - i.e. the connection was
# preserved. Reused by name so a new category upstream is a deliberate, visible decision here.
CONNECTED_CATEGORIES = frozenset({"live", "remote_import"})
# Categories that mean the rows come from a FLAT FILE on disk. A live source landing in one of these
# is the silent downgrade this gate exists to catch.
FILE_CATEGORIES = frozenset({check_empty_model.CATEGORY_FILE_OK, *check_empty_model.BLOCKING_CATEGORIES})

# Verdict per live data source.
SOURCE_CONNECTED = "connected"
SOURCE_DOWNGRADED = "downgraded"
SOURCE_DECLARED = "declared"
SOURCE_NOT_CHECKED = "not_checked"

# Build stages whose limitation entries count as a RECORDED downgrade decision. `parse` is excluded
# on purpose: the parser stamps a generic "extract-based (.hyper) data source" note on every extract,
# which would auto-excuse the exact live-extract case this gate targets.
DECISION_STAGES = frozenset({"semantic_build", "deploy", "validate"})

# Tableau connection class -> the Power Query connector base token the migrated model uses when the
# connection is preserved. Only classes we can map are attributable to a specific connector; an
# unmapped live class falls back to the model-level "no live connection at all" signal.
CLASS_TO_CONNECTOR: dict[str, str] = {
    "snowflake": "Snowflake",
    "sqlserver": "Sql",
    "mssql": "Sql",
    "microsoftsqlserver": "Sql",
    "azuresql": "Sql",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "oracle": "Oracle",
    "redshift": "AmazonRedshift",
    "amazonredshift": "AmazonRedshift",
    "awsredshift": "AmazonRedshift",
    "bigquery": "GoogleBigQuery",
    "googlebigquery": "GoogleBigQuery",
    "databricks": "Databricks",
    "sparksql": "Databricks",
    "spark": "Databricks",
    "teradata": "Teradata",
    "genericodbc": "Odbc",
    "odbc": "Odbc",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Model:
    """One emitted semantic model, classified for connectivity."""

    name: str
    path: str
    partitions: tuple[dict[str, str], ...]
    m_text: str

    @property
    def connected(self) -> int:
        """Count of partitions whose rows come from a live/remote connection."""
        return sum(1 for part in self.partitions if part["category"] in CONNECTED_CATEGORIES)

    @property
    def file_backed(self) -> int:
        """Count of partitions whose rows come from a flat file on disk."""
        return sum(1 for part in self.partitions if part["category"] in FILE_CATEGORIES)

    def has_connector(self, token: str) -> bool:
        """Whether the emitted M references a specific connector token (e.g. `Snowflake.Databases`).

        Comments are stripped first. Blind review demonstrated that a connector name sitting in an M
        comment - `// prior source was Snowflake.Databases("s","d")` - made a fully file-backed source
        report CONNECTED, which is a false PASS in the exact direction this gate exists to prevent.
        """
        return bool(re.search(rf"\b{re.escape(token)}\.[A-Za-z]", _strip_m_comments(self.m_text)))

    def file_tables(self, only: set[str] | None = None) -> list[str]:
        """Distinct table names whose partitions are file-backed, for finding evidence.

        `only` restricts the answer to one data source's declared tables, so a source is never judged
        by a partition belonging to a DIFFERENT source - the case that keeps a legitimately-flat
        source from tainting a live one that sits beside it in the same model.
        """
        return sorted(
            {
                part["table"]
                for part in self.partitions
                if part["category"] in FILE_CATEGORIES and (only is None or part["table"] in only)
            }
        )


@dataclass
class SourceVerdict:  # pylint: disable=too-many-instance-attributes
    """The connection-fidelity verdict for one Tableau data source."""

    source_id: str
    caption: str
    connection_class: str
    mode: str
    target: str
    verdict: str
    detail: str
    tables: list[str] = field(default_factory=list)


def _norm_class(connection_class: str | None) -> str:
    """Normalise a Tableau connection class for the connector lookup."""
    return _NON_ALNUM.sub("", (connection_class or "").lower())


def _expected_target(connection: dict[str, Any]) -> tuple[str, str, str]:
    """Return (target, class, mode) for one connection, preferring the stamped decision.

    The parser stamps `powerbi_target`; when present and valid we trust it, because it was computed
    from the class by the same `connection_target.powerbi_target` we would call. When absent we
    compute it, so an older spec is judged by the identical rule rather than skipped.
    """
    connection_class = str(connection.get("class") or "")
    mode = str(connection.get("mode") or "")
    stamped = connection.get("powerbi_target")
    if stamped in {LIVE_SOURCE, FLAT_FILE, UNKNOWN}:
        return str(stamped), connection_class, mode
    target, _reason = powerbi_target(connection_class or UNKNOWN, mode)
    return target, connection_class, mode


def _table_names(data_source: dict[str, Any]) -> list[str]:
    """Table names of one data source, tolerating both string and object table entries."""
    names: list[str] = []
    for table in data_source.get("tables") or []:
        if isinstance(table, str):
            names.append(table)
        elif isinstance(table, dict):
            name = table.get("name") or table.get("id")
            if name:
                names.append(str(name))
    return names


def load_model(model_dir: Path) -> Model:
    """Classify every partition of one `.SemanticModel` folder via check_empty_model."""
    params = model_parameters(model_dir)
    partitions: list[dict[str, str]] = []
    m_chunks: list[str] = []
    for tmdl in sorted((model_dir / "definition" / "tables").glob("*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        m_chunks.append(text)
        for block in _partition_blocks(text):
            verdict = classify_partition(block, model_dir, params)
            partitions.append({"table": tmdl.stem, "category": str(verdict.get("category", "unrecognized"))})
    return Model(name=model_dir.name, path=str(model_dir), partitions=tuple(partitions), m_text="\n".join(m_chunks))


def _item_names_source(item: str, source_id: str, table_names: list[str]) -> bool:
    """Whether a `limitations_encountered` item string references this data source or a table."""
    item = item.strip()
    if not item:
        return False
    if item == source_id or item.startswith(f"{source_id}.") or item.startswith(f"{source_id}__"):
        return True
    normalized = _NON_ALNUM.sub("", item.lower())
    source_norm = _NON_ALNUM.sub("", source_id.lower())
    if source_norm and source_norm in normalized:
        return True
    return any(table and _NON_ALNUM.sub("", table.lower()) in normalized for table in table_names)


def _declared_by_limitation(limitations: list[dict[str, Any]], source_id: str, table_names: list[str]) -> str | None:
    """Return a decision limitation's issue text when a build-stage entry names this source."""
    for entry in limitations:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("stage")) not in DECISION_STAGES:
            continue
        if _item_names_source(str(entry.get("item") or ""), source_id, table_names):
            return str(entry.get("issue") or "recorded downgrade")
    return None


def _declared_by_edit(declarations: list[dict[str, Any]], file_tables: set[str]) -> str | None:
    """Return a declaration target when a generated-edit declaration touches a file-backed table."""
    for declaration in declarations:
        target = str(declaration.get("target") or "")
        if not target:
            continue
        stem = Path(target).stem
        if stem in file_tables:
            return target
    return None


@dataclass(frozen=True)
class UnitContext:
    """The emitted models and recorded decisions a source verdict is judged against."""

    models: tuple[Model, ...]
    limitations: tuple[dict[str, Any], ...]
    declarations: tuple[dict[str, Any], ...]


def _strip_m_comments(text: str) -> str:
    """Remove M line and block comments so a connector NAME in prose cannot read as a connection."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def _normalise_table(name: str) -> str:
    """Comparable form of a table name: unqualified and case-folded.

    A spec says `DB.PUBLIC.FLIGHTS` where the emitted TMDL says `FLIGHTS`, and case varies freely
    between the two. Without normalisation those look like different tables and attribution fails on
    ordinary, correct input.
    """
    return name.rsplit(".", 1)[-1].strip("[]\"'").casefold()


def _connectivity(models: tuple[Model, ...], token: str, tables: set[str]) -> tuple[bool, bool, list[str], bool]:
    """Return (connected, file_backed, file_tables, attributable) for ONE source, scoped to its tables.

    BOTH sides are scoped. An earlier fix scoped only the file side while connector detection stayed
    model-wide, and blind review showed that is a false PASS in its own right: with two live sources of
    the same class, source A's preserved connector vouched for source B, and if B's declared table name
    did not match the emitted one (renamed, qualified, or merely different case) B's file evidence was
    empty - so a downgraded source reported CONNECTED and the unit exited 0.

    `attributable` is False when NO declared table of this source matches any emitted partition. That is
    reported rather than guessed: an unattributable source must never borrow another source's verdict.
    """
    wanted = {_normalise_table(name) for name in tables}
    mine = [
        part for model in models for part in model.partitions if not wanted or _normalise_table(part["table"]) in wanted
    ]
    if not mine:
        return False, False, [], False
    connected = any(part["category"] in CONNECTED_CATEGORIES for part in mine) and any(
        model.has_connector(token) for model in models
    )
    file_tables = sorted({part["table"] for part in mine if part["category"] in FILE_CATEGORIES})
    return connected, bool(file_tables), file_tables, True


def _connected_verdict(token: str, file_backed: bool, file_tables: list[str]) -> tuple[str, str]:
    """Verdict for a source whose connector IS present: connected, or a partial downgrade.

    A PARTIAL downgrade is NOT_CHECKED, never PASS. The module contract says so in its header, but the
    connected-branch used to return before file-backed partitions were considered, so it passed. Blind
    review caught it, and it is the shape of the incident that motivated this gate: a model where SOME
    tables of a live source kept their connector while others were materialised to CSV would have
    reported CONNECTED and hidden every downgraded table.
    """
    if file_backed:
        return (
            SOURCE_NOT_CHECKED,
            f"PARTIAL: a `{token}.*` connector is present AND {len(file_tables)} table(s) of this "
            f"source are file-backed ({', '.join(file_tables)}). Per-partition attribution to a "
            "source is not reliable enough to call this either way - inspect it by hand",
        )
    return SOURCE_CONNECTED, f"live source is connected in the model (a `{token}.*` connector is present)"


def _declared_reason(
    ctx: "UnitContext", source_id: str, data_source: dict[str, Any], file_tables: list[str]
) -> str | None:
    """Why this downgrade counts as DECIDED rather than drifted, or None if nobody recorded it.

    Two sanctioned records, checked in order of specificity: a `limitations_encountered` entry naming
    the source, then a generated-edit declaration naming one of its file-backed tables.
    """
    limitation = _declared_by_limitation(ctx.limitations, source_id, _table_names(data_source))
    if limitation:
        return f"downgrade recorded in limitations_encountered: {limitation}"
    edit = _declared_by_edit(ctx.declarations, set(file_tables))
    return f"downgrade recorded via generated-edit declaration: {edit}" if edit else None


def _judge_source(
    data_source: dict[str, Any],
    target: str,
    connection_class: str,
    mode: str,
    ctx: UnitContext,
) -> SourceVerdict:
    """Decide the connection-fidelity verdict for one live data source."""
    source_id = str(data_source.get("id") or "<unnamed>")
    caption = str(data_source.get("caption") or data_source.get("internal_name") or source_id)
    token = CLASS_TO_CONNECTOR.get(_norm_class(connection_class))
    if token is None:
        # An unmapped live class: we cannot name the connector it SHOULD emit, and check_empty_model
        # may not recognise it as live either, so a sibling flat_file table could make it look
        # downgraded when it was preserved. Refuse to guess - anything unprovable is not a downgrade.
        return SourceVerdict(
            source_id,
            caption,
            connection_class,
            mode,
            target,
            SOURCE_NOT_CHECKED,
            f"connection class '{connection_class}' has no known Power BI connector mapping - cannot "
            "attribute a downgrade offline; verify this source connects live by hand",
            [],
        )
    connected, file_backed, file_tables, attributable = _connectivity(ctx.models, token, set(_table_names(data_source)))

    def make(verdict: str, detail: str) -> SourceVerdict:
        """Build the verdict for this source, carrying the shared identity fields."""
        return SourceVerdict(source_id, caption, connection_class, mode, target, verdict, detail, list(file_tables))

    if not attributable:
        return make(
            SOURCE_NOT_CHECKED,
            "none of this source's declared tables match an emitted model partition, so no evidence "
            "can be attributed to it - it must not borrow another source's verdict. Check the "
            "spec-to-TMDL table naming by hand",
        )
    if connected:
        return make(*_connected_verdict(token, file_backed, file_tables))
    if not file_backed:
        return make(
            SOURCE_NOT_CHECKED,
            "no live connection found, but no file-backed partition either - the source's rows were "
            "not clearly materialised to a file (another gate owns an empty/stub model)",
        )
    declared = _declared_reason(ctx, source_id, data_source, file_tables)
    if declared:
        return make(SOURCE_DECLARED, declared)
    return make(
        SOURCE_DOWNGRADED,
        f"a '{connection_class}' live source, but the model has no `{token}.*` connector and its rows "
        "land in a flat file - the connection was silently downgraded and the model can never refresh",
    )


def _live_verdicts(
    data_sources: list[dict[str, Any]],
    models: list[Model],
    limitations: list[dict[str, Any]],
    declarations: list[dict[str, Any]],
) -> list[SourceVerdict]:
    """Judge every live-source data source; flat_file/unknown sources are not this gate's concern."""
    ctx = UnitContext(tuple(models), tuple(limitations), tuple(declarations))
    verdicts: list[SourceVerdict] = []
    for data_source in data_sources:
        target, connection_class, mode = _expected_target(data_source.get("connection") or {})
        if target != LIVE_SOURCE:
            continue
        verdicts.append(_judge_source(data_source, target, connection_class, mode, ctx))
    return verdicts


def scan_unit(spec_path: Path) -> dict[str, Any]:
    """Judge one migration unit: a migration-spec.json plus the models that ship beside it."""
    unit_dir = spec_path.parent
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _unit_result(spec_path, STATUS_SKIPPED, [], detail=f"spec unreadable: {exc}")
    if not isinstance(spec, dict):
        return _unit_result(spec_path, STATUS_SKIPPED, [], detail="spec is not a JSON object")

    data_sources = [ds for ds in (spec.get("data_sources") or []) if isinstance(ds, dict)]
    if not data_sources:
        return _unit_result(spec_path, STATUS_SKIPPED, [], detail="spec declares no data_sources")

    models = [load_model(model_dir) for model_dir in shipping_models(unit_dir, include_standalone=True)]
    if not models:
        return _unit_result(spec_path, STATUS_SKIPPED, [], detail="no semantic model ships beside this spec")

    limitations = [e for e in (spec.get("limitations_encountered") or []) if isinstance(e, dict)]
    declarations = load_generated_edit_declarations(unit_dir)
    return _finalize_unit(spec_path, _live_verdicts(data_sources, models, limitations, declarations))


def _finalize_unit(spec_path: Path, verdicts: list[SourceVerdict]) -> dict[str, Any]:
    """Fold per-source verdicts into one unit result."""
    downgraded = [v for v in verdicts if v.verdict == SOURCE_DOWNGRADED]
    checked = [v for v in verdicts if v.verdict in {SOURCE_CONNECTED, SOURCE_DECLARED, SOURCE_DOWNGRADED}]
    if downgraded:
        status = STATUS_DOWNGRADED
        detail = None
    elif not checked:
        status = STATUS_SKIPPED
        detail = "no live-source data source was checkable (all flat_file, unknown, or unattributable)"
    else:
        status = STATUS_OK
        detail = None
    return _unit_result(spec_path, status, verdicts, detail=detail)


def _unit_result(spec_path: Path, status: str, verdicts: list[SourceVerdict], detail: str | None) -> dict[str, Any]:
    """Shape one unit's result for JSON and rendering."""
    return {
        "spec": str(spec_path),
        "unit": spec_path.parent.name,
        "status": status,
        "detail": detail,
        "live_sources_checked": sum(1 for v in verdicts if v.verdict != SOURCE_NOT_CHECKED),
        "downgraded": sum(1 for v in verdicts if v.verdict == SOURCE_DOWNGRADED),
        "declared": sum(1 for v in verdicts if v.verdict == SOURCE_DECLARED),
        "connected": sum(1 for v in verdicts if v.verdict == SOURCE_CONNECTED),
        "not_checked": sum(1 for v in verdicts if v.verdict == SOURCE_NOT_CHECKED),
        "sources": [v.__dict__ for v in verdicts],
    }


def _find_specs(root: Path) -> list[Path]:
    """Every migration-spec.json under a target, root-level first."""
    if root.is_file() and root.name == "migration-spec.json":
        return [root]
    direct = root / "migration-spec.json"
    if direct.is_file():
        return [direct]
    return sorted(root.rglob("migration-spec.json"), key=str)


def scan(root: Path) -> dict[str, Any]:
    """Scan every migration unit under one path."""
    units = [scan_unit(spec) for spec in _find_specs(root)]
    return merge(units)


def merge(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-unit results into one verdict."""
    downgraded = [u for u in units if u["status"] == STATUS_DOWNGRADED]
    ok = [u for u in units if u["status"] == STATUS_OK]
    if not units:
        status = STATUS_SKIPPED
    elif downgraded:
        status = STATUS_DOWNGRADED
    elif ok:
        status = STATUS_OK
    else:
        status = STATUS_SKIPPED
    return {
        "status": status,
        "units_scanned": len(units),
        "units_with_downgrade": len(downgraded),
        "downgraded_sources": sum(u["downgraded"] for u in units),
        "declared_sources": sum(u["declared"] for u in units),
        "connected_sources": sum(u["connected"] for u in units),
        "units": units,
    }


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Human-readable verdict, matching the sibling offline gates."""
    if report["status"] == STATUS_SKIPPED:
        return (
            "CONNECTION FIDELITY CHECK: SKIPPED - nothing measured "
            "(no spec with data_sources, no shipped model, or no live source to check)"
        )
    if report["status"] == STATUS_OK:
        return (
            f"CONNECTION FIDELITY CHECK: OK - every live source stays connected across "
            f"{report['units_scanned']} unit(s) ({report['connected_sources']} connected, "
            f"{report['declared_sources']} declared downgrade(s))."
        )
    lines = [
        f"CONNECTION FIDELITY CHECK: DOWNGRADED - {report['downgraded_sources']} live source(s) "
        f"silently shipped as flat files in {report['units_with_downgrade']} of "
        f"{report['units_scanned']} unit(s)."
    ]
    for unit in report["units"]:
        if unit["status"] != STATUS_DOWNGRADED:
            continue
        lines.append(f"  {unit['unit']}")
        for source in unit["sources"]:
            if source["verdict"] != SOURCE_DOWNGRADED:
                continue
            lines.append(
                f"    - {source['caption']} [{source['connection_class']}/{source['mode']}]: {source['detail']}"
            )
            if verbose and source["tables"]:
                lines.append(f"        file-backed tables: {', '.join(source['tables'])}")
    lines.append(
        "  DOWNGRADED = connect the semantic model to the live upstream system (the packaged extract "
        "is only Tableau's cache), or - if materialising IS correct - record the decision in "
        "limitations_encountered (stage semantic_build) or a generated-edit declaration."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle/unit folder(s) or a migration-spec.json")
    parser.add_argument("--model", type=Path, help="unused override kept for CLI symmetry with sibling gates")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--verbose", action="store_true", help="also list the file-backed tables behind a downgrade")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0 after a successful scan")
    args = parser.parse_args(argv)

    targets = [*args.paths, *([args.model] if args.model else [])]
    if not targets:
        parser.error("give a bundle/unit path or a migration-spec.json")
    for path in targets:
        if not (path.is_dir() or (path.is_file() and path.name == "migration-spec.json")):
            parser.error(f"{path} is not a directory or migration-spec.json")

    merged = merge([unit for target in targets for unit in scan(target)["units"]])

    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged, verbose=args.verbose))
    if args.warn_only:
        return EXIT_OK
    if merged["status"] == STATUS_DOWNGRADED:
        return EXIT_DOWNGRADED
    if merged["status"] == STATUS_SKIPPED:
        return EXIT_SKIPPED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
