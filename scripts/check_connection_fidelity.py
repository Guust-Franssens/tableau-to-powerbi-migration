"""
purpose: fail loudly when a Tableau data source that must connect to a LIVE upstream system ships as
         a semantic model that instead reads a FLAT FILE - a connection silently downgraded so the
         model can never refresh and freezes the customer's data at export time.
usage:   python scripts/check_connection_fidelity.py <bundle-or-unit-dir> [...]
                                              [--model <dir>] [--json <file>] [--quiet]
                                              [--verbose] [--warn-only]
         Accepts BOTH migration tiers: a parser `migration-spec.json` unit, and an engine bundle
         (`report.json` / `handover/*.json`) built by `run_estate.py`.

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

Two migration tiers, and ONE of them can produce a pass (issue #366)
--------------------------------------------------------------------
A unit reaches Power BI by one of two paths, and only one of them has a spec:

  * the PARSER path - `parse_tableau.py` emits `migration-spec.json`, which names each data source's
    TABLES. Evidence is scoped per source, table by table (`SCOPE_TABLE`).
  * the ENGINE path - `migrate_estate.py` (via `run_estate.py`) emits a bundle with NO spec. Nine of
    fourteen units in a real 2026-08-28 field run were therefore reported SKIPPED "no spec with
    data_sources" - the gate built for a WORKBOOK incident silently covering no workbook at all.

The engine bundle is not evidence-free, but its two unit kinds carry DIFFERENT evidence, and the
difference decides what each can conclude. Measured against real canonical 2.339.0 output:

  * a DATASOURCE unit (`report.json` -> `datasources[]`) carries `connector` (the Tableau class),
    `pbip_folder`, and - the load-bearing part - **`tables`, the real emitted table names** (measured:
    `["FACT_ORDERS", "DIM_CUSTOMER", "DIM_DATE", "Date"]`, matching the emitted TMDL filenames
    exactly). That is the parser contract in all but name, so it is judged at `SCOPE_TABLE`, at full
    strength: a real CONNECTED pass, a real DOWNGRADED finding, the same escape hatch.
  * a WORKBOOK unit carries `embedded_datasources` telemetry with `connection_class` per datasource
    and per federated leg, plus `pbip_folder` - but only a `table_count`, **never table names**.

A model-scope PASS IS NOT REACHABLE, and that is the design
-----------------------------------------------------------
A pass says THIS source's rows still arrive over its connection. At `SCOPE_MODEL` nothing attributes
a partition to a source, so every pass there was an inference, and three adversarial review rounds
each broke a different one: a sibling's live partition certifying an empty scaffold; a connector token
matched inside a STRING LITERAL or an unused lazy binding (connector detection is a regex over
partition text - it does not trace the final `in` expression, and doing so is writing an M
interpreter); one connected table certifying two same-connector sources; an unresolved source hidden
behind a resolved one. The third round's reproduction came from freshly generated engine output, not a
fixture: a two-source SQL Server workbook with one table deleted still reported BOTH sources
connected, exit 0.

Each remedy was another heuristic. The honest reading is that this is a CEILING, not a bug list, so
`_model_scope_verdict` emits a FINDING or a REFUSAL and names no pass constant - the pass-surface
closure test proves that mechanically. Closing the gap needs per-table provenance the payload does not
carry (filed upstream as #182), and until it lands a workbook unit reports "not evaluated" rather than
a guess. A FINDING still stands there, because it rests on direct evidence rather than attribution:
the connector this class requires appears NOWHERE in the model, and rows in that model come off disk.

Measured on a real 52-unit estate (45 workbooks + 7 datasources, canonical 2.339.0, 2026-08-29):
7 datasource units examined and CONNECTED; 30 workbooks `nothing_to_check` (every declared class is a
flat file - a complete answer, zero false ones); 15 `not_evaluated`, of which 9 are published-datasource
pointers whose real connection IS checked in those 7 datasource units. 0 findings, 0 false findings.

Absent is not empty
-------------------
A SKIPPED unit carries a machine-readable `reason`: `nothing_to_check` (no live source in this unit -
a real, complete answer), `not_evaluated` (this unit could not be examined) or `partial_coverage`.
Reading the second as the first is exactly how nine unexamined workbooks looked like a clean bill of
health. The estate summary reports coverage (n checked / n total) for the same reason, and
`report.json` - not the handover slices copied from it - is the CENSUS that denominator counts. A unit
whose slice is missing or unreadable, and an unreadable census itself, both appear as not-evaluated
units rather than shrinking the denominator in silence. A source that cannot be resolved to a live
system or a file gets its own NOT_CHECKED verdict and is never dropped, because a resolved sibling
must not carry a unit past an unresolved one.

What this gate does NOT tell you
--------------------------------
* Whether a live source that DID stay connected points at the right server - that is fidelity, and
  needs a credentialed probe (`probe_live_source.py`).
* A PARTIAL downgrade inside a source that still has one live partition, or an exotic connection
  class this module cannot map to an M connector: both are reported as NOT_CHECKED for that source,
  never as PASS. Anything this module cannot prove is a downgrade is not called one.
* On the engine WORKBOOK path, whether ONE table of a live source was materialised while its
  siblings stayed connected. That payload does not name tables, so the shape reports NOT_CHECKED,
  never PASS. The engine DATASOURCE path does name them and does not have this limitation.
"""

from __future__ import annotations

# One gate, two evidence tiers. The length is the recorded knowledge - twenty-odd blind-review
# defects, each kept beside the guard that closes it - not sprawl. The real fix it defers is lifting
# the engine-telemetry READER (`EngineUnit`, `engine_datasource_telemetry`, `engine_sources`,
# `engine_model_dirs`) beside `migration_bundle.py`, which is where a second consumer would want it;
# that seam saves ~170 lines and leaves only ~12 of headroom, so it is worth doing when there IS a
# second consumer and not merely to duck this cap.
# pylint: disable=too-many-lines

import argparse
import collections
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import check_empty_model
import read_handover
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

# Why a SKIPPED unit was skipped. `check_unit.py` keys off STATUS_SKIPPED/exit 3, so the umbrella
# status is unchanged; this says which of the two very different things it means. Acceptance
# criterion 2 of issue #366: "nothing to compare here" and "this unit could not be examined" printed
# identically, and nine unexamined workbooks read as a clean bill of health.
REASON_NOTHING_TO_CHECK = "nothing_to_check"
REASON_PARTIAL_COVERAGE = "partial_coverage"
REASON_NOT_EVALUATED = "not_evaluated"

# How much of the model a source's verdict is allowed to look at.
SCOPE_TABLE = "table"  # parser path: the spec names this source's tables, so evidence is per table
SCOPE_MODEL = "model"  # engine path: no table names exist, so evidence is the whole emitted model

# The engine's `embedded_datasources` telemetry, with absence kept distinct from emptiness - the same
# MISSING/INVALID/NONE/PRESENT ladder `read_handover.partitions_needs_review_status` uses, and for the
# same reason: this repo has shipped a false "0" from that conflation three times (#276, #299, #309).
TELEMETRY_MISSING = "missing"
TELEMETRY_INVALID = "invalid"
TELEMETRY_NONE = "none"
TELEMETRY_PRESENT = "present"

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
    "azuresqldb": "Sql",  # Tableau's `azure_sqldb`; found unmapped on a real 52-unit estate, 2026-08-29
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

# Every connector token this module can look for, derived from the class map so the two cannot drift.
CONNECTOR_TOKENS = frozenset(CLASS_TO_CONNECTOR.values())

# Tableau connection classes that are a PROXY to another migration unit rather than an upstream
# system. Measured on a real 2.339.0 bundle: a workbook binding a published datasource reports
# `sqlproxy`, and its real connection (Snowflake) is not in the workbook payload at all. These are
# NOT_CHECKED-with-a-pointer, never an unmapped class - and never a `CLASS_TO_CONNECTOR` entry, since
# no Power Query connector corresponds to them.
PROXY_CLASSES = frozenset({"sqlproxy"})


@dataclass(frozen=True)
class Model:
    """One emitted semantic model, classified for connectivity."""

    name: str
    path: str
    partitions: tuple[dict[str, Any], ...]
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

        MODEL-WIDE, and therefore never sufficient for a PASS - see `_partition_connects`. Blind
        review found that "some partition carries rows" AND "the token appears somewhere in the
        model" need not be the SAME partition: a live Sql partition certified a Snowflake source
        whose only partition was an empty `#table` stub, and the unit exited 0. This is kept for the
        model-wide "is the connector present at all" question, which only ever produces NOT_CHECKED.

        Comments are stripped first. Blind review demonstrated that a connector name sitting in an M
        comment - `// prior source was Snowflake.Databases("s","d")` - made a fully file-backed source
        report CONNECTED, which is a false PASS in the exact direction this gate exists to prevent.
        """
        return bool(re.search(rf"\b{re.escape(token)}\.[A-Za-z]", _strip_m_noise(self.m_text)))

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


def _strip_m_noise(text: str) -> str:
    """Remove M comments AND string literals, preserving length so structure survives.

    String literals matter as much as comments now. Blind review round 18: a partition carrying
    `Note = "Snowflake.Databases(""fake"")"` was reported as a connected Snowflake source, because
    only comments were stripped. Doubled quotes are M's escape, and blanking the whole literal handles
    them without needing to parse the escape.
    """
    out = []
    i, n, in_string = 0, len(text), False
    while i < n:
        char = text[i]
        if in_string:
            if char == '"':
                if i + 1 < n and text[i + 1] == '"':
                    out.append("  ")
                    i += 2
                    continue
                in_string = False
                out.append(" ")
            else:
                out.append(" " if char != "\n" else "\n")
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(" ")
            i += 1
            continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if text.startswith("/*", i):
            while i < n and not text.startswith("*/", i):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


_LET_BLOCK = re.compile(r"\blet\b(?P<body>.*?)\bin\b\s*(?P<final>#?\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_.]*)", re.DOTALL)
_BINDING = re.compile(r"^\s*(?P<name>#?\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<expr>.*)$", re.DOTALL)
_CONNECTOR_CALL = re.compile(r"^\s*([A-Z][A-Za-z0-9]*)\.[A-Za-z]+\s*\(")
_NAVIGATION_STEP = re.compile(r"^\s*(#?\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_]*)\s*\{.*\}\s*\[[^\]]*\]\s*$", re.DOTALL)
# `Value.NativeQuery(<handle>, "SELECT ...")` is the engine's custom-SQL step, and its FIRST argument
# is the connection handle - so rows provably derive from whatever that handle traces back to. It is
# admitted as a chain step because it is exactly how a Tableau custom-SQL relation lands, which is the
# shape of the incident this gate was built for (#328). Only the first-argument position counts: a
# connector named anywhere else in the call does not make the chain.
_NATIVE_QUERY_STEP = re.compile(r"^\s*Value\.NativeQuery\s*\(\s*(#?\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_]*)\s*,", re.DOTALL)


def _split_bindings(body: str) -> list[str]:
    """Split a `let` body on TOP-LEVEL commas only - brackets, braces and parens all nest."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(body):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    return [p for p in parts if p.strip()]


def partition_provenance(body: str) -> frozenset[str]:
    """The connector token that PROVABLY supplies this partition's rows, or nothing.

    ATTRIBUTION IS NOT PROVENANCE. Round 18's finding, and the reason this exists: knowing which
    TABLE belongs to a source (which `ds_details.tables` does answer) never established which
    EXPRESSION supplies that table's rows. A lexical token match said "connected" for a partition
    whose rows came from an inline `#table` literal beside an unused string, and for one whose rows
    came from `Sql.Database` beside an unused lazy `Snowflake.Databases` binding.

    This is reachability, NOT an M interpreter. It walks backwards from the `in` expression through
    `name = expr` bindings and requires the chain to be the engine's own canonical shape:

        let  Source = <Token>.<Func>(...),        <- the root, and the only connector call allowed
             Db     = Source{[...]}[Data],        <- zero or more pure navigation steps, or
             Data   = Value.NativeQuery(Db, ...)  <- a custom-SQL step over the same handle
        in   Data

    Measured across BOTH real bundles (280 partitions, canonical engine 2.339.0): every one of the 49
    live-connector partitions matches it - 41 Snowflake and 3 Databricks as 4-step chains, 3 Sql and 2
    PostgreSQL as 2-step. So this recognises a known generator's output rather than inferring, which
    is the distinction that makes a pass defensible here. `Value.NativeQuery` does not appear in
    either bundle (neither migrated a custom-SQL relation live) but is admitted on the same reasoning:
    its first argument IS the connection handle, and it is how a Tableau custom-SQL table lands - the
    exact shape of the incident this gate was built for.

    Anything else - a hand-edited partition, an extra binding in the chain, a branch, a missing link -
    returns nothing, and the caller reports NOT_CHECKED. Unreachable bindings are ignored by
    construction: they are never visited, so an unused connector call cannot vote.
    """
    match = _LET_BLOCK.search(_strip_m_noise(body))
    if not match:
        return frozenset()
    bindings: dict[str, str] = {}
    for chunk in _split_bindings(match.group("body")):
        found = _BINDING.match(chunk)
        if found:
            bindings[found.group("name").strip('#"')] = found.group("expr")
    current = match.group("final").strip('#"')
    for _step in range(len(bindings) + 1):
        expr = bindings.get(current)
        if expr is None:
            return frozenset()
        root = _CONNECTOR_CALL.match(expr)
        if root and root.group(1) in CONNECTOR_TOKENS:
            return frozenset({root.group(1)})
        # A call that is NOT a known connector falls through to the step patterns rather than ending
        # the walk: `Value.NativeQuery(...)` matches the root shape too, and returning here made every
        # custom-SQL partition unprovable - including the committed `mixed-live-and-flat-file` fixture,
        # which is the one artifact in this repo that shows the incident's own shape.
        nav = _NAVIGATION_STEP.match(expr) or _NATIVE_QUERY_STEP.match(expr)
        if not nav:
            return frozenset()
        current = nav.group(1).strip('#"')
    return frozenset()


def partition_connectors(body: str) -> frozenset[str]:
    """Connector tokens this partition's M MENTIONS, comments and string literals removed.

    LEXICAL and deliberately over-detecting: it feeds only `connector_present`, which can move a
    verdict to NOT_CHECKED and never to a pass. The pass side uses `partition_provenance`.
    """
    text = _strip_m_noise(body)
    return frozenset(token for token in CONNECTOR_TOKENS if re.search(rf"\b{re.escape(token)}\.[A-Za-z]", text))


def _partition_connects(part: dict[str, Any], token: str) -> bool:
    """Whether THIS partition carries rows and PROVABLY draws them through THIS connector.

    Keyed on proven provenance, not on a token appearing in the text. A partition with no recorded
    provenance fails closed; the only producer is `load_model`, which always records one.
    """
    return part["category"] in CONNECTED_CATEGORIES and token in (part.get("provenance") or frozenset())


def load_model(model_dir: Path) -> Model:
    """Classify every partition of one `.SemanticModel` folder via check_empty_model."""
    params = model_parameters(model_dir)
    partitions: list[dict[str, Any]] = []
    m_chunks: list[str] = []
    for tmdl in sorted((model_dir / "definition" / "tables").glob("*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        m_chunks.append(text)
        for block in _partition_blocks(text):
            verdict = classify_partition(block, model_dir, params)
            body = str(block.get("body") or "")
            partitions.append(
                {
                    "table": tmdl.stem,
                    "category": str(verdict.get("category", "unrecognized")),
                    "connectors": partition_connectors(body),
                    "provenance": partition_provenance(body)
                    if str(block.get("kind", "")).lower() == "m"
                    else frozenset(),
                }
            )
    return Model(name=model_dir.name, path=str(model_dir), partitions=tuple(partitions), m_text="\n".join(m_chunks))


def _declaration_scope(
    item: str, source_id: str, table_names: list[str], sibling_ids: frozenset[str] = frozenset()
) -> str | None:
    """What a limitation item DECLARES: "source", "table", or None for "nothing precise".

    `SOURCE_DECLARED` is a PASS, so every predicate that can produce one must be precise. Blind review
    rounds 9, 10 and 11 walked through three imprecise variants in turn: the source-wide SCOPE test,
    the ASSOCIATION test, and then the source-qualified-table syntax below.

    Precise forms only:
      * `item == source_id`                       -> "source" (attests to every declared table)
      * `item == <a declared table name>`         -> "table"
      * `item == <source_id>.<table>` or `__`     -> "table"

    ⚠️ `_` is NOT a qualifier delimiter. Round 11: `ds.sf_ORDERS` parsed as "source `ds.sf`, table
    `ORDERS`" while being a perfectly good id for a SIBLING source, so a decision recorded for that
    sibling declared a downgrade for `ds.sf`. Real specs use `.` and `__`; measured, none rely on `_`.

    ⚠️ And an item that EXACTLY equals another known source id is never reinterpreted as a qualified
    table, whatever the delimiter. Dropping `_` alone would leave the same ambiguity available to a
    sibling literally named `ds.sf.ORDERS`, so the id set is the general guard and the delimiter rule
    is the specific one.

    In a committed example the airline workbook ships two near-duplicate sources whose ids differ only
    by a `_1` suffix, so this family of collisions is real, not theoretical.
    """
    item = item.strip()
    if not item:
        return None
    folded = item.casefold()
    own = source_id.strip().casefold()
    if folded == own:
        return "source"
    if any(folded == other.strip().casefold() for other in sibling_ids if other.strip().casefold() != own):
        return None
    tables = {(name or "").strip().casefold() for name in table_names if name}
    if folded in tables:
        return "table"
    for delimiter in (".", "__"):
        prefix = f"{own}{delimiter}"
        if folded.startswith(prefix) and folded[len(prefix) :] in tables:
            return "table"
    return None


def _item_names_source(item: str, source_id: str, table_names: list[str]) -> bool:
    """Whether a `limitations_encountered` item string references this data source or a table.

    ⚠️ DELIBERATELY LOOSE, and no longer used to decide anything that can produce a pass - see
    `_declaration_scope`. Kept because "is this record about this source at all" is a fair question
    for reporting, but substring containment must never certify a downgrade as decided.
    """
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


def _declared_by_limitation(
    limitations: list[dict[str, Any]],
    source_id: str,
    table_names: list[str],
    sibling_ids: frozenset[str] = frozenset(),
) -> tuple[str, bool] | None:
    """`(issue text, names_the_source)` for a decision-stage entry covering this source.

    The boolean is the SCOPE: True when the item names the data source itself - an attestation about
    the whole source - and False when it only matched one of its table names. A source-wide record
    still stands when some declared table is unmatched; a table-scoped one must not vouch for a table
    nobody examined. A source-wide match wins over a table-only one, so the strongest record applies.
    """
    fallback: tuple[str, bool] | None = None
    for entry in limitations:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("stage")) not in DECISION_STAGES:
            continue
        item = str(entry.get("item") or "")
        text = str(entry.get("issue") or "recorded downgrade")
        scope = _declaration_scope(item, source_id, table_names, sibling_ids)
        if scope == "source":
            return text, True
        if scope == "table" and fallback is None:
            fallback = (text, False)
    return fallback


def _owning_model_names(models: tuple[Model, ...], file_tables: set[str]) -> set[str]:
    """Names of the emitted models that actually hold these file-backed tables."""
    owners: set[str] = set()
    for model in models:
        if any(part["table"] in file_tables and part["category"] in FILE_CATEGORIES for part in model.partitions):
            owners.add(model.name.casefold())
    return owners


def _declared_by_edits(declarations: list[dict[str, Any]], file_tables: set[str], owners: set[str]) -> set[str]:
    """EVERY file-backed table of this source that a generated-edit declaration provably touches.

    Returns the SET, not the first hit. Blind review, HIGH 4: returning one declaration and treating
    it as sufficient let a single recorded edit certify a second, undeclared file-backed table -
    exactly the "a table-scoped record attests only to its table" rule this module already claimed.

    Round 11: matching on `Path(target).stem` alone discarded every other path component, so a target
    inside `OtherSource.SemanticModel/definition/tables/ORDERS.tmdl` declared a downgrade for a
    same-named table in a DIFFERENT model - DECLARED / OK / exit 0 on someone else's edit.

    `DECLARED` is a pass, so ownership has to be proven rather than assumed: some segment of the
    target path must name a model that actually holds the file-backed table. If ownership cannot be
    established the declaration does not apply, and the downgrade stands as a finding.
    """
    covered: set[str] = set()
    for declaration in declarations:
        target = str(declaration.get("target") or "")
        if not target:
            continue
        path = Path(target)
        if path.stem not in file_tables:
            continue
        if owners and not owners & {segment.casefold() for segment in path.parts}:
            continue
        covered.add(path.stem)
    return covered


@dataclass(frozen=True)
class UnitContext:
    """The emitted models and recorded decisions a source verdict is judged against."""

    models: tuple[Model, ...]
    limitations: tuple[dict[str, Any], ...]
    declarations: tuple[dict[str, Any], ...]
    source_ids: frozenset[str] = frozenset()
    scope: str = SCOPE_TABLE
    flat_file_capacity: int | None = 0


@dataclass(frozen=True)
class Coverage:
    """What the model actually tells us about ONE source, and how completely.

    `unmatched` is the load-bearing field and the reason this is a type rather than a 4-tuple: the
    verdict rules differ for a finding and for a pass. A FINDING may rest on partial coverage - the
    matched tables carry real evidence. A PASS may not, because a declared table we could not find in
    the model could be exactly the one that was downgraded.

    `scope` records WHICH evidence produced it. `SCOPE_TABLE` is the parser path, where the spec names
    this source's tables. `SCOPE_MODEL` is the engine path, where no table names exist anywhere in the
    bundle (issue #366) and the strongest honest scope is the one model the workbook built. Under
    model scope `unmatched` is always empty - there is nothing declared to be unmatched - so a
    "partial coverage" note there would be a lie of a different kind; the model-scope refusals say
    what they actually mean instead.

    `connector_present` is kept separately from `connected` because they answer different questions.
    `connected` means "rows demonstrably arrive through this connector"; `connector_present` means
    only "the connector is named in live M somewhere". Under model scope the second is not enough to
    call a downgrade, and keeping them apart is what stops that.
    """

    connected: bool
    file_backed: bool
    file_tables: list[str]
    attributable: bool
    unmatched: list[str]
    connector_present: bool = False
    scope: str = SCOPE_TABLE

    @property
    def complete(self) -> bool:
        """True when every declared table was found in the emitted model."""
        return not self.unmatched


def _normalise_table(name: str) -> str:
    """Comparable form of a table name, qualifiers PRESERVED and case folded.

    Deliberately does NOT drop qualifiers. An earlier version returned only the last dot-segment, and
    blind review showed that collapses genuinely different tables: `SALES.ORDERS` and `HR.ORDERS` both
    became `orders`, so a downgraded HR source matched the preserved SALES partition and was certified
    CONNECTED with the unit exiting 0. Unqualified matching still happens, but only as an explicit,
    ambiguity-checked fallback in `_attribute` - never as the primary key.
    """
    return name.strip("[]\"'").casefold()


def _attribute(models: tuple[Model, ...], tables: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    """Partitions belonging to ONE source, plus the declared tables that matched nothing.

    EXACT match only, qualifiers preserved, case-folded. No unqualified fallback - an earlier version
    matched on the last dot-segment and blind review showed it collapses distinct tables: `SALES.ORDERS`
    and `HR.ORDERS` both became `orders`, so a downgraded HR source matched the preserved SALES
    partition and was certified CONNECTED with the unit exiting 0.

    The fallback was there to absorb a spec saying `DB.PUBLIC.FLIGHTS` against an emitted `FLIGHTS`.
    ⚠️ That justification was asserted, not measured, and the data contradicts it: across every spec in
    this repo, 58 of 65 declared table names are unqualified, and all 7 containing a dot are CSV
    FILENAMES (`tree.csv`) - for which a leaf match is actively wrong, since every one would collapse to
    `csv`. So the fallback solved nothing real while creating two ways to mis-attribute.

    Returns `(matched, unmatched)`. `matched` is EVERY partition whose table is declared by this
    source; `unmatched` is the declared names with no emitted counterpart. It is deliberately NOT
    all-or-nothing any more: an earlier version returned None the moment ONE declared table failed to
    match, and that discarded real evidence about the tables that DID match. Measured - a source
    declaring `ORDERS` + `CUSTOMERS` against a model emitting only a file-backed `ORDERS` reported
    NOT_CHECKED, while the identical model with a single-table source reported DOWNGRADED. Adding a
    second, unmatched table to the spec silenced a positively-evidenced downgrade, and the detail line
    claimed "none of this source's declared tables match" when one of them matched exactly.

    The caller applies the asymmetry that makes this safe: a FINDING may be reported on partial
    evidence, a PASS may not. See `_judge_source`.

    EVERY matching partition is returned, not one per name. An earlier version keyed a dict by
    normalised table name, so duplicates silently collapsed last-write-wins, and blind review showed
    that makes the verdict ORDER-DEPENDENT: one table emitted as a file-backed partition in one model
    and a live partition in another reported CONNECTED (exit 0) when the file partition was visited
    first, and DOWNGRADED when it was visited second. The same held for two partitions of a single
    table. Both shapes are real - a source table can appear in more than one semantic model, and a
    table can have several partitions - so the aggregate must see all of them.
    """
    if not tables:
        return [], []
    by_exact: dict[str, list[dict[str, str]]] = {}
    for model in models:
        for part in model.partitions:
            by_exact.setdefault(_normalise_table(part["table"]), []).append(part)
    matched: list[dict[str, str]] = []
    unmatched: list[str] = []
    for name in sorted(tables):
        key = _normalise_table(name)
        if key in by_exact:
            matched.extend(by_exact[key])
        else:
            unmatched.append(name)
    return matched, unmatched


def _connectivity(models: tuple[Model, ...], token: str, tables: set[str]) -> Coverage:
    """Connectivity evidence for ONE source, scoped to the tables it declares.

    BOTH sides are scoped. Scoping only the file side is its own false PASS: with two live sources of
    the same class, source A's preserved connector vouched for source B, and if B's declared table did
    not match the emitted one, B had no file evidence either - so a downgraded source reported
    CONNECTED and the unit exited 0.

    `connected` is now PER PARTITION - the same partition must carry rows AND call this connector.
    Model-wide agreement was blind review's HIGH 2 and it applies at table scope too: a source's own
    table classified `remote_import` through `Sql.Database` used to be certified by a Snowflake token
    emitted for an unrelated table.
    """
    matched, unmatched = _attribute(models, tables)
    connector_present = any(token in (part.get("connectors") or frozenset()) for part in matched)
    if not matched:
        return Coverage(False, False, [], False, unmatched, connector_present, SCOPE_TABLE)
    connected = any(_partition_connects(part, token) for part in matched)
    file_tables = sorted({part["table"] for part in matched if part["category"] in FILE_CATEGORIES})
    return Coverage(connected, bool(file_tables), file_tables, True, unmatched, connector_present, SCOPE_TABLE)


def _model_coverage(models: tuple[Model, ...], token: str) -> Coverage:
    """The two DIRECT facts a model-scope verdict is allowed to rest on, and nothing more.

    Is this connector named anywhere in the model, and do any rows come off disk? Both are observable
    without attributing a partition to a source, which is exactly why they are the only two kept.

    Everything else this used to compute - counting connected partitions against a declared
    `table_count`, tallying unclassifiable federated legs - existed to gate a model-scope PASS. There
    is no model-scope pass any more (see `_model_scope_verdict`), so those were heuristics standing in
    for attribution the payload cannot provide. `declared_table_count` survives ONLY on flat-file
    sources, where it bounds the capacity test that stops a legitimate CSV being read as a downgrade.
    """
    partitions = [part for model in models for part in model.partitions]
    connector_present = any(token in (part.get("connectors") or frozenset()) for part in partitions)
    file_tables = sorted({part["table"] for part in partitions if part["category"] in FILE_CATEGORIES})
    return Coverage(False, bool(file_tables), file_tables, bool(partitions), [], connector_present, SCOPE_MODEL)


def _coverage_note(cov: "Coverage") -> str:
    """Say plainly when a verdict rests on incomplete coverage."""
    if not cov.unmatched:
        return ""
    return (
        f". NOTE partial coverage: {len(cov.unmatched)} declared table(s) match no emitted partition "
        f"({', '.join(cov.unmatched)}), so this source's other tables could not be examined"
    )


def _connected_verdict(token: str, cov: "Coverage") -> tuple[str, str]:
    """Verdict for a source whose connector IS present: connected, or a downgrade we cannot rule out.

    A PARTIAL downgrade is NOT_CHECKED, never PASS. The module contract says so in its header, but the
    connected-branch used to return before file-backed partitions were considered, so it passed. Blind
    review caught it, and it is the shape of the incident that motivated this gate: a model where SOME
    tables of a live source kept their connector while others were materialised to CSV would have
    reported CONNECTED and hidden every downgraded table.

    INCOMPLETE COVERAGE is the same refusal for the same reason. A clean bill of health is a statement
    about the WHOLE source, so it requires evidence about every table the source declares; a table we
    could not find could be the downgraded one. This is the asymmetry the module runs on - a finding
    may rest on partial evidence, a pass may not.
    """
    if cov.file_backed:
        scope_note = (
            " (evidence is MODEL-scoped: the engine bundle names no tables, so this source's "
            "partitions cannot be told apart from a sibling's)"
            if cov.scope == SCOPE_MODEL
            else ""
        )
        return (
            SOURCE_NOT_CHECKED,
            f"PARTIAL: a `{token}.*` connector is present AND {len(cov.file_tables)} table(s) of this "
            f"source are file-backed ({', '.join(cov.file_tables)}). Per-partition attribution to a "
            f"source is not reliable enough to call this either way - inspect it by hand{scope_note}",
        )
    if cov.unmatched:
        return (
            SOURCE_NOT_CHECKED,
            f"a `{token}.*` connector is present and no matched table is file-backed, but "
            f"{len(cov.unmatched)} declared table(s) match no emitted partition "
            f"({', '.join(cov.unmatched)}). A pass must cover every declared table - one of these "
            "could be the downgraded one. Check the spec-to-TMDL table naming by hand",
        )
    return SOURCE_CONNECTED, f"live source is connected in the model (a `{token}.*` connector is present)"


def _limitation_table_coverage(
    limitations: list[dict[str, Any]],
    source_id: str,
    table_names: list[str],
    sibling_ids: frozenset[str] = frozenset(),
) -> set[str]:
    """Table names precisely declared by decision-stage limitation entries.

    The companion to `_declared_by_limitation`'s table-scoped answer. That function reports THAT a
    table-scoped record exists; this reports WHICH tables it covers, so a pass can require every
    file-backed table to be covered rather than just one (blind review, HIGH 4).

    ⚠️ Every delimiter decision is delegated to `_declaration_scope`, one table at a time, rather than
    re-implementing the prefix rules here. The first draft re-implemented them and immediately went
    out of step: the round-12 `_` mutation was neutralised at this second site, so invariant I8 read
    VACUOUS. Two copies of a matching rule is how a mutation stops being observable.
    """
    covered: set[str] = set()
    for entry in limitations:
        if not isinstance(entry, dict) or str(entry.get("stage")) not in DECISION_STAGES:
            continue
        item = str(entry.get("item") or "")
        for name in table_names:
            if name and _declaration_scope(item, source_id, [name], sibling_ids) == "table":
                covered.add(name)
    return covered


def _declared_reason(
    ctx: "UnitContext", source_id: str, data_source: dict[str, Any], file_tables: list[str]
) -> tuple[str, str] | None:
    """`(reason, scope)` for why this downgrade counts as DECIDED, or None if nobody recorded it.

    `scope` is "source", "table" or "partial", and it decides whether the record can certify tables
    the model never showed us. A record naming the SOURCE attests to the whole source, so it still
    stands when some declared table is unmatched. A record naming ONE TABLE - a generated-edit target,
    or a limitation whose item is a table name - attests only to that table.

    ⚠️ "Attests only to that table" is now ENFORCED, not merely stated. Blind review, HIGH 4: a single
    generated-edit declaration cleared a source with TWO file-backed tables, because the first
    matching declaration was accepted and nobody checked whether the rest were covered. Table-scoped
    records must now cover EVERY file-backed table of the source before they can produce a pass;
    covering only some yields "partial", which is a refusal that names what is missing.

    Measured across every spec in this repo: 4 decision-stage limitations name a data-source id and
    **0** name a table. So the dangerous form does not occur in practice while the safe form does,
    which is why this distinguishes them rather than simply refusing every declaration on incomplete
    coverage - that would add noise to the only shape real specs actually use.
    """
    table_names = _table_names(data_source)
    limitation = _declared_by_limitation(ctx.limitations, source_id, table_names, ctx.source_ids)
    if limitation and limitation[1]:
        return f"downgrade recorded in limitations_encountered: {limitation[0]}", "source"

    wanted = set(file_tables)
    covered = _limitation_table_coverage(ctx.limitations, source_id, table_names, ctx.source_ids)
    covered |= _declared_by_edits(ctx.declarations, wanted, _owning_model_names(ctx.models, wanted))
    if not covered:
        return None
    named = ", ".join(sorted(covered))
    if wanted <= covered:
        return f"downgrade recorded for every file-backed table of this source ({named})", "table"
    return (
        f"a table-scoped decision covers {named}, but {len(wanted - covered)} file-backed table(s) of "
        f"this source are recorded by nobody ({', '.join(sorted(wanted - covered))}). A table-scoped "
        "record attests only to its own table; record the decision against the data source, or add a "
        "declaration for each materialised table",
        "partial",
    )


def _declared_verdict(
    ctx: "UnitContext", source_id: str, data_source: dict[str, Any], cov: "Coverage"
) -> tuple[str, str] | None:
    """Verdict for a downgrade somebody recorded, or None when nobody did.

    A PASS requires complete evidence, and DECLARED is a pass. Blind review round 8 found that rule
    enforced only on the CONNECTED path, so an explicit decision about ORDERS silently certified an
    unexamined CUSTOMERS and the unit exited 0. A SOURCE-wide record does cover the whole source and
    still stands; a TABLE-scoped one cannot vouch for a table nobody examined.

    Three refusals, not one, because they fail for different reasons: "partial" means some MATERIALISED
    table was recorded by nobody (HIGH 4), while incomplete coverage means some DECLARED table was
    never located at all. Both are refusals; conflating their messages sends an operator to the wrong
    place.
    """
    declared = _declared_reason(ctx, source_id, data_source, cov.file_tables)
    if not declared:
        return None
    reason, scope = declared
    if scope == "partial":
        return SOURCE_NOT_CHECKED, reason
    if scope == "source" or cov.complete:
        return SOURCE_DECLARED, reason
    return (
        SOURCE_NOT_CHECKED,
        f"{reason} - but that record is scoped to a single table, and "
        f"{len(cov.unmatched)} declared table(s) could not be accounted for "
        f"({', '.join(cov.unmatched)}). A table-scoped decision cannot certify a table nobody "
        "examined; record the decision against the data source, or fix the spec-to-TMDL naming",
    )


def _name_sample(names: list[str], limit: int = 6) -> str:
    """A bounded, sorted rendering of a name list for a diagnostic line."""
    ordered = sorted(names)
    if not ordered:
        return "(none)"
    if len(ordered) <= limit:
        return ", ".join(ordered)
    return ", ".join(ordered[:limit]) + f", ... (+{len(ordered) - limit} more)"


def _unattributable_detail(cov: "Coverage", data_source: dict[str, Any], models: tuple[Model, ...]) -> str:
    """Why nothing could be attributed - and, for a spec unit, WHICH kind of gap it is.

    Issue #366 acceptance criterion 4 asks whether two unattributed sources in a field run were a
    name-matching gap or a genuinely unemitted source. This gate cannot answer that from a message
    that prints neither side, so it prints both: the names the spec declared and the names the model
    actually emitted. A reader can then tell a rename (`FLIGHTS` vs `SF_FLIGHTS`) from a source that
    never landed at all (nothing plausibly corresponding emitted), which is the difference between a
    naming fix and a re-migration.
    """
    if cov.scope == SCOPE_MODEL:
        return (
            "the model this workbook built has no partitions at all, so there is nothing to compare "
            "the workbook's declared live connection against - inspect the emitted model by hand"
        )
    declared = _table_names(data_source)
    emitted = sorted({part["table"] for model in models for part in model.partitions})
    if not declared:
        return (
            "this source declares NO tables in the spec, so no evidence can be attributed to it - it "
            "must not borrow another source's verdict. The emitted model holds: "
            f"{_name_sample(emitted)}"
        )
    return (
        "none of this source's declared tables match an emitted model partition, so no evidence "
        "can be attributed to it - it must not borrow another source's verdict. Declared: "
        f"{_name_sample(declared)}; emitted: {_name_sample(emitted)}. Matching names on both sides "
        "means a spec-to-TMDL NAMING gap; nothing corresponding on the emitted side means the "
        "source did not land at all"
    )


def _judge_source(  # pylint: disable=too-many-return-statements
    data_source: dict[str, Any],
    target: str,
    connection_class: str,
    mode: str,
    ctx: UnitContext,
) -> SourceVerdict:
    """Decide the connection-fidelity verdict for one live data source.

    The return count IS the decision ladder, and each rung is a distinct refusal a blind-review round
    put there. Collapsing them into a lookup would make the ladder unreadable and every guard harder
    to argue with, which is the opposite of what this module needs.
    """
    source_id = str(data_source.get("id") or "<unnamed>")
    caption = str(data_source.get("caption") or data_source.get("internal_name") or source_id)
    if _norm_class(connection_class) in PROXY_CLASSES:
        target_unit = str(data_source.get("published_datasource_name") or "").strip()
        where = f"the migrated unit for published datasource '{target_unit}'" if target_unit else "that unit"
        return SourceVerdict(
            source_id,
            caption,
            connection_class,
            mode,
            target,
            SOURCE_NOT_CHECKED,
            f"'{connection_class}' is Tableau's PROXY to a published datasource, not an upstream "
            f"system - this workbook's payload does not carry the real connection at all. Check "
            f"{where}; `check_sqlproxy_connections.py` owns the binding question. This is a POINTER, "
            "not an unmapped class: do not add it to CLASS_TO_CONNECTOR, no Power Query connector "
            "corresponds to it",
            [],
        )
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
    cov = (
        _model_coverage(ctx.models, token)
        if ctx.scope == SCOPE_MODEL
        else _connectivity(ctx.models, token, set(_table_names(data_source)))
    )

    def make(verdict: str, detail: str) -> SourceVerdict:
        """Build the verdict for this source, carrying the shared identity fields."""
        return SourceVerdict(source_id, caption, connection_class, mode, target, verdict, detail, list(cov.file_tables))

    if ctx.scope == SCOPE_MODEL:
        return make(*_model_scope_verdict(token, connection_class, cov, ctx))
    if not cov.attributable:
        return make(SOURCE_NOT_CHECKED, _unattributable_detail(cov, data_source, ctx.models))
    if cov.connected:
        return make(*_connected_verdict(token, cov))
    if not cov.file_backed:
        return make(
            SOURCE_NOT_CHECKED,
            "no live connection found, but no file-backed partition either - the source's rows were "
            "not clearly materialised to a file (another gate owns an empty/stub model)",
        )
    declared = _declared_verdict(ctx, source_id, data_source, cov)
    if declared:
        return make(*declared)
    # A FINDING is reported even on partial coverage. The tables that matched carry positive evidence
    # of a downgrade, and refusing to report it because a SIBLING table did not match is how adding a
    # second table to a spec used to silence a real finding.
    return make(
        SOURCE_DOWNGRADED,
        f"a '{connection_class}' live source, but the model has no `{token}.*` connector and its rows "
        "land in a flat file - the connection was silently downgraded and the model can never refresh"
        + _coverage_note(cov),
    )


def _model_scope_verdict(token: str, connection_class: str, cov: "Coverage", ctx: "UnitContext") -> tuple[str, str]:
    """A FINDING or a REFUSAL. **A PASS IS NOT REACHABLE FROM HERE, BY CONSTRUCTION.**

    This function deliberately names no pass constant, so the pass-surface closure gate in the test
    suite proves the claim mechanically rather than by reading the prose.

    WHY. A pass is a statement that THIS source's rows still arrive over its connection, and at model
    scope nothing attributes a partition to a source: the engine's workbook payload records a
    `table_count` and no table names. Two adversarial review rounds each found a different way that
    inference produced a clean exit 0 - a sibling's live partition certifying an empty scaffold, a
    connector token matched inside a string literal, one connected table certifying two same-connector
    sources, an unresolved source hidden behind a resolved one. The remedy for each was another
    heuristic, and the third round's reproduction came from freshly generated canonical 2.339.0
    output, not a fixture: a two-source SQL Server workbook with one table deleted still reported BOTH
    sources connected. The honest reading is that this is a CEILING, not a bug list. Closing it needs
    per-table provenance the payload does not carry - filed upstream as #182 - or an M interpreter,
    which does not belong in a fidelity gate.

    A FINDING still stands, because it rests on direct evidence rather than attribution: the connector
    this class requires appears NOWHERE in the emitted model, and rows in that model come off disk.
    Neither statement needs to know which partition belongs to which source. The asymmetry is the
    module's own - a finding may rest on partial evidence, a pass may not.

    Note the failure direction of the connector regex is safe here. It OVER-detects (a token inside a
    string literal, or an unused lazy binding), and over-detection lands on the refusal branch; a
    finding requires the token to be absent entirely.
    """
    if not cov.attributable:
        return (
            SOURCE_NOT_CHECKED,
            "the model this workbook built has no partitions at all, so there is nothing to compare "
            "the workbook's declared live connection against - inspect the emitted model by hand",
        )
    if cov.connector_present:
        return (
            SOURCE_NOT_CHECKED,
            f"a `{token}.*` connector appears in this model, but the engine's workbook payload names "
            "no tables, so it cannot be attributed to THIS source - and a lexical connector match is "
            "not proof that rows arrive through it. Not evaluated; check this model by hand",
        )
    if not cov.file_backed:
        return (
            SOURCE_NOT_CHECKED,
            "no live connection found, but no file-backed partition either - the source's rows were "
            "not clearly materialised to a file (another gate owns an empty/stub model)",
        )
    unexplained = _unexplained_file_note(ctx, cov)
    if unexplained:
        return SOURCE_NOT_CHECKED, unexplained
    if _any_recorded_decision(ctx, cov):
        # A recorded decision cannot CLEAR this source at model scope (nothing attributes the
        # materialised table to it), but it must not be ACCUSED either. Reporting a downgrade over
        # work somebody deliberately recorded is how a gate gets muted, and being unable to tell the
        # two apart is precisely what "not evaluated" is for.
        return (
            SOURCE_NOT_CHECKED,
            f"NO `{token}.*` connector appears in this model and rows come off disk, but a downgrade "
            "decision IS recorded for a file-backed table here. At model scope nothing attributes that "
            "table to this source, so the record can neither clear it nor be dismissed - not evaluated",
        )
    return (
        SOURCE_DOWNGRADED,
        f"a '{connection_class}' live source, but NO `{token}.*` connector appears anywhere in the "
        f"emitted model and {len(cov.file_tables)} of its table(s) read a flat file "
        f"({', '.join(cov.file_tables)}) - the connection was silently downgraded and the model can "
        "never refresh",
    )


def _any_recorded_decision(ctx: "UnitContext", cov: "Coverage") -> bool:
    """Whether ANY recorded decision touches a file-backed table of this model.

    Deliberately coarse - it asks "did somebody record something about these tables", not "does the
    record cover this source", because the second question is the attribution model scope cannot do.
    Coarse is the right shape here: it can only move a FINDING to a REFUSAL, never to a pass.
    """
    wanted = set(cov.file_tables)
    if _declared_by_edits(ctx.declarations, wanted, _owning_model_names(ctx.models, wanted)):
        return True
    return any(isinstance(entry, dict) and str(entry.get("stage")) in DECISION_STAGES for entry in ctx.limitations)


def _unexplained_file_note(ctx: "UnitContext", cov: "Coverage") -> str | None:
    """Refuse a model-scope FINDING when a declared flat-file sibling could own every file partition.

    Blind review, MEDIUM 5 - the one false FINDING rather than a false pass. At model scope a
    file-backed partition was attributed to EVERY live source whose connector is absent, so a unit
    declaring one Snowflake source and one legitimate Excel source, emitting only the Excel CSV,
    claimed the Snowflake rows had landed in that file. Nothing supports that attribution, and this
    inverts the discrimination the whole gate turns on: flagging a legitimately-flat table is how a
    gate gets muted and then misses the real case.

    The capacity test keeps detection alive on genuinely mixed units: file-backed partitions beyond
    what every declared flat-file source could account for are unexplained, and a finding may rest on
    those. A flat-file sibling with NO recorded table_count has unknown capacity, so nothing is
    unexplained and the verdict is a refusal.
    """
    if cov.scope != SCOPE_MODEL:
        return None
    capacity = ctx.flat_file_capacity
    if capacity == 0:
        return None  # no flat-file sibling exists that could own these partitions
    if capacity is not None and len(cov.file_tables) > capacity:
        return None  # more file-backed tables than every flat-file sibling could account for
    limit = "an unrecorded number of" if capacity is None else str(capacity)
    return (
        f"{len(cov.file_tables)} file-backed table(s) are in this model, but the unit also declares "
        f"FLAT-FILE source(s) accounting for {limit} table(s) - at model scope those partitions "
        "cannot be told apart, so this source's rows may never have been materialised at all. "
        "Inspect by hand; a legitimately-flat sibling must not be reported as this source's downgrade"
    )


def _flat_file_capacity(data_sources: list[dict[str, Any]]) -> int | None:
    """How many file-backed tables the unit's declared FLAT-FILE sources could legitimately own.

    `None` means unknown - at least one flat-file source has no recorded table count, so no number of
    file partitions is "more than they can explain". Used only to refuse a model-scope FINDING
    (blind review, MEDIUM 5); it can never turn a refusal into a pass.
    """
    total = 0
    for source in data_sources:
        if _expected_target(source.get("connection") or {})[0] != FLAT_FILE:
            continue
        count = source.get("declared_table_count")
        if not isinstance(count, int):
            return None
        total += count
    return total


def _live_verdicts(
    data_sources: list[dict[str, Any]],
    models: list[Model],
    limitations: list[dict[str, Any]],
    declarations: list[dict[str, Any]],
    scope: str = SCOPE_TABLE,
) -> list[SourceVerdict]:
    """Judge every live-source data source; flat_file/unknown sources are not this gate's concern."""
    ctx = UnitContext(
        tuple(models),
        tuple(limitations),
        tuple(declarations),
        frozenset(str(ds.get("id") or "") for ds in data_sources),
        scope,
        _flat_file_capacity(data_sources),
    )
    verdicts: list[SourceVerdict] = []
    for data_source in data_sources:
        target, connection_class, mode = _expected_target(data_source.get("connection") or {})
        if target == FLAT_FILE:
            continue
        if target != LIVE_SOURCE:
            # UNKNOWN, and it must SURVIVE as a verdict. Blind review, round 17 finding 4: an
            # unresolved source was dropped here, so a unit with one connected source and one
            # unclassifiable one reported OK - the resolved source HID the unresolved one. Dropping is
            # the same absent-is-not-empty conflation this whole gate exists to refuse.
            verdicts.append(
                SourceVerdict(
                    str(data_source.get("id") or "<unnamed>"),
                    str(data_source.get("caption") or data_source.get("id") or "<unnamed>"),
                    connection_class,
                    mode,
                    target,
                    SOURCE_NOT_CHECKED,
                    f"connection class '{connection_class or '(none recorded)'}' could not be resolved "
                    "to a live system or a file, so whether it should connect is UNKNOWN - not "
                    "'nothing to check'. Determine the source class before trusting this unit",
                    [],
                )
            )
            continue
        verdicts.append(_judge_source(data_source, target, connection_class, mode, ctx))
    return verdicts


def scan_unit(spec_path: Path) -> dict[str, Any]:
    """Judge one PARSER-path migration unit: a migration-spec.json plus the models beside it."""
    unit_dir = spec_path.parent
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _unit_result(
            spec_path, STATUS_SKIPPED, [], detail=f"spec unreadable: {exc}", reason=REASON_NOT_EVALUATED
        )
    if not isinstance(spec, dict):
        return _unit_result(
            spec_path, STATUS_SKIPPED, [], detail="spec is not a JSON object", reason=REASON_NOT_EVALUATED
        )

    data_sources = [ds for ds in (spec.get("data_sources") or []) if isinstance(ds, dict)]
    if not data_sources:
        # NOT `nothing_to_check`: a spec with no data_sources does not say the workbook had none, it
        # says nobody recorded any. Absence is not emptiness.
        return _unit_result(
            spec_path, STATUS_SKIPPED, [], detail="spec declares no data_sources", reason=REASON_NOT_EVALUATED
        )

    models = [load_model(model_dir) for model_dir in shipping_models(unit_dir, include_standalone=True)]
    if not models:
        return _unit_result(
            spec_path,
            STATUS_SKIPPED,
            [],
            detail="no semantic model ships beside this spec",
            reason=REASON_NOT_EVALUATED,
        )

    limitations = [e for e in (spec.get("limitations_encountered") or []) if isinstance(e, dict)]
    declarations = load_generated_edit_declarations(unit_dir)
    verdicts = _live_verdicts(data_sources, models, limitations, declarations)
    return _finalize_unit(spec_path, verdicts, live_declared=_count_judgeable(data_sources))


def _count_judgeable(data_sources: list[dict[str, Any]]) -> int:
    """How many declared sources could carry a live connection this gate is responsible for.

    LIVE_SOURCE **and** UNKNOWN both count. A source whose class could not be resolved is not
    evidence that nothing live exists - it is the absence of evidence, and counting it as zero is the
    same conflation issue #366 is about, one level down. Only a resolved `flat_file` is a real zero.
    """
    return sum(1 for ds in data_sources if _expected_target(ds.get("connection") or {})[0] != FLAT_FILE)


def _finalize_unit(
    spec_path: Path,
    verdicts: list[SourceVerdict],
    *,
    unit_name: str | None = None,
    live_declared: int | None = None,
) -> dict[str, Any]:
    """Fold per-source verdicts into one unit result.

    An UNCHECKED source keeps the unit off a clean OK. Blind review round 3 surfaced a unit with one
    connected and one unattributable source reporting OK/exit 0 - the gate announcing a pass over a
    source it could not examine, which is the false-green shape this repo has shipped three times
    (#276, #299, #309). `check_stub_measures.py:68-69` states the rule: "no stubs" and "no model" must
    never print or exit the same way. A finding still outranks it: a real downgrade is worth more than
    a partial-coverage note.

    `live_declared` separates the two SKIPPED reasons that used to print identically (issue #366).
    ZERO live sources declared is a complete answer - `nothing_to_check`. One or more declared and
    none examinable is `not_evaluated`, which is the answer that must never be read as clean.

    It is also a HEADCOUNT: fewer verdicts than declared judgeable sources means one was dropped
    somewhere between declaration and judgment, and that unit cannot be OK. Round 17 finding 4 was
    exactly that shape - an UNKNOWN source silently discarded, so a connected sibling carried the unit
    to exit 0. The drop is fixed at source in `_live_verdicts`; this is the belt that catches the next
    one, because "a source vanished" must never be spelled the same as "every source passed".
    """
    downgraded = [v for v in verdicts if v.verdict == SOURCE_DOWNGRADED]
    unchecked = [v for v in verdicts if v.verdict == SOURCE_NOT_CHECKED]
    checked = [v for v in verdicts if v.verdict in {SOURCE_CONNECTED, SOURCE_DECLARED, SOURCE_DOWNGRADED}]
    declared_live = len(verdicts) if live_declared is None else live_declared
    missing = max(0, declared_live - len(verdicts))
    if downgraded:
        status = STATUS_DOWNGRADED
        detail = None
        reason = None
    elif missing:
        status = STATUS_SKIPPED
        detail = (
            f"{declared_live} data source(s) that could carry a live connection were declared but only "
            f"{len(verdicts)} produced a verdict - {missing} was dropped before judgment. A unit "
            "cannot pass while a declared source is unaccounted for"
        )
        reason = REASON_NOT_EVALUATED
    elif not checked and not declared_live:
        status = STATUS_SKIPPED
        detail = "no live source was declared here, so there is nothing this gate can compare"
        reason = REASON_NOTHING_TO_CHECK
    elif not checked:
        status = STATUS_SKIPPED
        detail = (
            f"{declared_live} data source(s) that could carry a live connection were declared, but "
            "NONE could be examined. " + _unchecked_reasons(unchecked)
        )
        reason = REASON_NOT_EVALUATED
    elif unchecked:
        status = STATUS_SKIPPED
        detail = (
            f"{len(checked)} live source(s) checked, but {len(unchecked)} could NOT be attributed to an "
            f"emitted table ({', '.join(v.source_id for v in unchecked)}) - partial coverage, not a pass"
        )
        reason = REASON_PARTIAL_COVERAGE
    else:
        status = STATUS_OK
        detail = None
        reason = None
    return _unit_result(spec_path, status, verdicts, detail=detail, reason=reason, unit_name=unit_name)


def _unchecked_reasons(unchecked: list[SourceVerdict], limit: int = 3) -> str:
    """The per-source reasons behind a wholly-unexamined unit, surfaced where an operator reads them.

    Measured against the real cold-run bundle: the workbook unit's ONLY actionable sentence - that
    `sqlproxy` is a pointer to the published datasource's own unit - lived on `sources[0].detail` and
    never reached the rendered line, which said the generic "unresolved or unmappable connection
    class". An operator was told to go looking for an unmapped class when the answer was "check the
    other unit". A reason nobody sees is not a reason.
    """
    if not unchecked:
        return "No source-level reason was recorded."
    shown = unchecked[:limit]
    parts = [f"{verdict.source_id}: {verdict.detail}" for verdict in shown]
    more = f" (+{len(unchecked) - len(shown)} more source(s))" if len(unchecked) > len(shown) else ""
    return " || ".join(parts) + more


def _unit_result(  # pylint: disable=too-many-arguments
    spec_path: Path,
    status: str,
    verdicts: list[SourceVerdict],
    detail: str | None,
    *,
    reason: str | None = None,
    unit_name: str | None = None,
) -> dict[str, Any]:
    """Shape one unit's result for JSON and rendering.

    Six parameters because a unit result now carries WHY it was skipped and, on the engine path, a
    name that is not its parent folder. They are flat rather than bundled into a record because this
    function is pinned by `test_unit_result_only_counts_and_never_decides` as a pure shaper: adding a
    type it could read a decision out of is the thing that pin exists to prevent.
    """
    return {
        "spec": str(spec_path),
        "unit": unit_name or spec_path.parent.name,
        "status": status,
        "detail": detail,
        "reason": reason,
        "live_sources_checked": sum(1 for v in verdicts if v.verdict != SOURCE_NOT_CHECKED),
        "downgraded": sum(1 for v in verdicts if v.verdict == SOURCE_DOWNGRADED),
        "declared": sum(1 for v in verdicts if v.verdict == SOURCE_DECLARED),
        "connected": sum(1 for v in verdicts if v.verdict == SOURCE_CONNECTED),
        "not_checked": sum(1 for v in verdicts if v.verdict == SOURCE_NOT_CHECKED),
        "sources": [v.__dict__ for v in verdicts],
    }


@dataclass(frozen=True)
class EngineUnit:
    """One unit of an ENGINE bundle: its telemetry payload, where the bundle lives, and its scope.

    `scope` is carried per unit, not per bundle, because the engine's two unit kinds genuinely carry
    different evidence. A datasource detail names its tables; a workbook detail does not. Deciding
    that once, at discovery, is what keeps the datasource half at full strength.
    """

    name: str
    source: Path
    bundle: Path
    workbook: dict[str, Any]
    scope: str = SCOPE_MODEL
    error: str | None = None


def engine_datasource_sources(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """An engine DATASOURCE detail as this gate's data-source records - TABLE NAMES INCLUDED.

    `report.json` -> `datasources[]` is the parser contract in all but name. Measured on a real
    canonical 2.339.0 bundle, one migrated Snowflake datasource carried::

        connector      = "snowflake"                # the Tableau connection class
        m_connector    = "Snowflake.Databases"      # the emitted Power Query connector
        storage_mode   = "DirectQuery"
        tables         = ["FACT_ORDERS", "DIM_CUSTOMER", "DIM_DATE", "Date"]
        pbip_folder    = "pbip/<name>/<name>.pbip"

    and those four table names matched the emitted TMDL filenames exactly. So this half of the engine
    path needs no weakening at all: it is judged at `SCOPE_TABLE`, with a real CONNECTED pass and a
    real DOWNGRADED finding, exactly like a spec unit.

    `storage_mode` is recorded as `mode` for the operator-facing line only. It is NOT the
    discriminator - see this module's header on why `mode` never is.
    """
    name = str(detail.get("name") or "<unnamed datasource>")
    connection_class = str(detail.get("connector") or detail.get("connection_class") or "")
    target, _reason = powerbi_target(connection_class or UNKNOWN, "unknown")
    tables = [str(t) for t in (detail.get("tables") or []) if isinstance(t, str)]
    count = detail.get("table_count")
    declared = len(tables) if tables else (count if isinstance(count, int) else None)
    return [
        {
            "id": name,
            "caption": name,
            "connection": {
                "class": connection_class,
                "mode": str(detail.get("storage_mode") or "unknown"),
                "powerbi_target": target,
            },
            "tables": tables,
            "declared_table_count": declared,
        }
    ]


def engine_datasource_units(bundle: Path) -> list[EngineUnit]:
    """Every DATASOURCE unit recorded in a bundle's `report.json`.

    A thin view over `_engine_census`, kept because "what datasource units does this bundle declare?"
    is a question worth asking on its own.
    """
    return [unit for unit in _engine_census(bundle)[0] if unit.scope == SCOPE_TABLE]


def engine_datasource_telemetry(workbook: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """`(status, rows)` for a workbook's `embedded_datasources`, absence kept distinct from empty.

    Mirrors `read_handover.partitions_needs_review_status` deliberately. An older engine build, or a
    workbook detail assembled before the telemetry existed, has NO key - and answering that with an
    empty list would say "this workbook touches no live system", which is the one wrong answer a
    connection gate must never give.
    """
    if "embedded_datasources" not in workbook:
        return TELEMETRY_MISSING, []
    rows = workbook.get("embedded_datasources")
    if not isinstance(rows, list):
        return TELEMETRY_INVALID, []
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return TELEMETRY_NONE, []
    return TELEMETRY_PRESENT, rows


def engine_sources(rows: list[dict[str, Any]], published_name: str = "") -> list[dict[str, Any]]:
    """Engine `embedded_datasources` telemetry as this gate's data-source records.

    ONE RECORD PER DISTINCT CONNECTION CLASS, not per datasource. A Tableau datasource can federate
    several upstream systems at once (the engine's own `_connection_facts` says Azure SQL + Snowflake
    + Databricks in one datasource is a real shipped shape), and the connector token - the thing this
    gate looks for in the emitted M - is per CLASS. Collapsing the legs into one `federated` record,
    as `migration_bundle._engine_embedded_source` does for its own purpose, maps to no connector at
    all and would report every federated source NOT_CHECKED.

    `published_name` rides along from the workbook's `binding_signal` so a `sqlproxy` record can name
    the unit that actually holds its connection instead of reporting an unmapped class.

    ⚠️ `table_count` and `named_connection_count` are CARRIED, not discarded. Blind review, HIGH 3:
    dropping them threw away the only completeness evidence this payload has - a source declaring
    three tables against one emitted partition passed clean, and a federated leg whose
    `connection_class` is null was silently FILTERED OUT and passed clean too. Both are now
    incompleteness that `_model_scope_unmatched` turns into a refusal. An unclassifiable leg also
    becomes its own UNKNOWN-target record, so it is visible rather than merely counted.

    `tables` is deliberately left empty rather than invented: the WORKBOOK emitter records a
    `table_count` and no names (measured against canonical engine 2.339.0). The DATASOURCE emitter
    does name them - see `engine_datasource_sources`. `mode` is `unknown` because this payload does
    not record extract-vs-live, and `powerbi_target` does not need it.
    """
    sources: list[dict[str, Any]] = []
    for row in rows:
        sources.extend(_engine_row_sources(row, published_name))
    return sources


def _engine_leg_classes(row: dict[str, Any]) -> tuple[list[str], int]:
    """`(distinct classes, unclassifiable leg count)` for one embedded-datasource row.

    An unclassifiable leg is COUNTED rather than dropped. Blind review, HIGH 3: filtering legs whose
    `connection_class` is null made a datasource binding two systems look like one fully-understood
    system, and it passed clean.
    """
    legs = [leg for leg in (row.get("connections") or []) if isinstance(leg, dict)]
    named = row.get("named_connection_count")
    declared_legs = named if isinstance(named, int) else len(legs)
    if legs:
        classified = [str(leg.get("connection_class") or "") for leg in legs]
        classified = [cls for cls in classified if cls]
    else:
        classified = [str(row.get("connection_class"))] if row.get("connection_class") else []
        declared_legs = max(declared_legs, 1)
    ordered: list[str] = []
    for cls in classified:
        if cls not in ordered:
            ordered.append(cls)
    return ordered, max(0, declared_legs - len(classified))


def _engine_row_sources(row: dict[str, Any], published_name: str) -> list[dict[str, Any]]:
    """The data-source records for ONE embedded-datasource row: one per class, plus unknown legs."""
    label = str(row.get("label") or row.get("caption") or "embedded datasource")
    caption = str(row.get("caption") or label)
    table_count = row.get("table_count")
    declared_tables = table_count if isinstance(table_count, int) else None
    classes, unclassified = _engine_leg_classes(row)
    shared = {
        "tables": [],
        "declared_table_count": declared_tables,
        "unclassified_legs": unclassified,
        "published_datasource_name": published_name,
    }
    out: list[dict[str, Any]] = []
    for connection_class in classes or [""]:
        target, _reason = powerbi_target(connection_class or UNKNOWN, "unknown")
        multi = len(classes) > 1
        out.append(
            {
                "id": f"{label}#{connection_class or UNKNOWN}" if multi else label,
                "caption": f"{caption} [{connection_class or UNKNOWN}]" if multi else caption,
                "connection": {"class": connection_class, "mode": "unknown", "powerbi_target": target},
                **shared,
            }
        )
    for index in range(unclassified):
        # Preserved as UNKNOWN rather than filtered out: an unrecorded leg is a system nobody
        # classified, and dropping it made the datasource look like one fully-understood system.
        out.append(
            {
                "id": f"{label}#unclassified-leg-{index + 1}",
                "caption": f"{caption} [connection leg with no recorded class]",
                "connection": {"class": "", "mode": "unknown", "powerbi_target": UNKNOWN},
                **{**shared, "declared_table_count": None},
            }
        )
    return out


def engine_model_dirs(bundle: Path, workbook: dict[str, Any]) -> tuple[list[Path], str | None]:
    """`(model dirs, why-not)` for the model ONE unit built, from the engine's own pointer.

    Three pointers are tried IN ORDER, and every one of them is a value the engine recorded - nothing
    here matches on name. Guessing the model by name is how a unit would silently borrow a sibling's
    model and, with it, a sibling's verdict.

      1. `pbip_folder` - run-relative `pbip/<Name>/<Name>.pbip`; the working copy agents edit.
      2. `bound_model` - the model a workbook bound, when no project path was recorded.
      3. `output_folder` - `semantic_models/<Name>.SemanticModel`, the engine's PRISTINE baseline.
         LAST on purpose: judging the baseline answers what the engine emitted, not what ships.

    Falling through rather than giving up on the first miss is deliberate: a unit whose project path
    is stale but whose `bound_model` still resolves is checkable, and refusing it would be an
    unnecessary NOT_EVALUATED. Every failed pointer is still reported, so an operator sees which.

    ⚠️ The model inside a workbook's project is NOT named after the workbook. Measured on the real
    cold-run bundle: `pbip/Meridian Revenue by Region/Meridian Sales (Live Snowflake).SemanticModel`
    - the project is the workbook, the model is the DATASOURCE it bound. So the project directory is
    scanned for whatever `.SemanticModel` it holds.
    """
    reasons: list[str] = []

    folder = workbook.get("pbip_folder")
    if isinstance(folder, str) and folder.strip():
        project = (bundle / Path(folder.strip())).parent
        models = shipping_models(project, include_standalone=True) if project.is_dir() else []
        if models:
            return models, None
        missing = "does not exist" if not project.is_dir() else "holds no .SemanticModel"
        reasons.append(f"pbip_folder '{folder}' points at {project}, which {missing}")

    bound = workbook.get("bound_model")
    if isinstance(bound, str) and bound.strip():
        wanted = bound.strip().casefold()
        models = [m for m in shipping_models(bundle, include_standalone=True) if m.stem.casefold() == wanted]
        if models:
            return models, None
        reasons.append(f"bound_model '{bound}' matches no .SemanticModel shipping in {bundle}")

    output = workbook.get("output_folder")
    if isinstance(output, str) and output.strip():
        baseline = bundle / Path(output.strip())
        models = shipping_models(baseline, include_standalone=True) if baseline.is_dir() else []
        if models:
            return models, None
        missing = "does not exist" if not baseline.is_dir() else "holds no .SemanticModel"
        reasons.append(f"output_folder '{output}' points at {baseline}, which {missing}")

    if not reasons:
        return [], (
            "the engine recorded no pbip_folder, bound_model or output_folder for this unit, so no "
            "emitted model can be attributed to it (it produced no bound project)"
        )
    return [], "no emitted model could be attributed to this unit: " + "; ".join(reasons)


def scan_engine_unit(unit: EngineUnit) -> dict[str, Any]:
    """Judge one ENGINE-path unit. Never silently passes.

    Dispatches on the unit's own scope: a DATASOURCE unit names its tables and gets the full-strength
    table-scoped rules; a WORKBOOK unit does not and gets the weaker model-scoped ones.
    """
    if unit.error:
        return _engine_skip(unit, unit.error, REASON_NOT_EVALUATED)
    if unit.scope == SCOPE_TABLE:
        return _scan_engine_datasource(unit)
    status, rows = engine_datasource_telemetry(unit.workbook)
    binding = unit.workbook.get("binding_signal")
    published = binding.get("published_ds_name") if isinstance(binding, dict) else None
    unusable = _telemetry_refusal(unit, status, str(published or ""))
    if unusable:
        return unusable

    model_dirs, why_not = engine_model_dirs(unit.bundle, unit.workbook)
    if not model_dirs:
        return _engine_skip(unit, str(why_not), REASON_NOT_EVALUATED)

    sources = engine_sources(rows, str(published or ""))
    return _judge_engine_models(unit, sources, model_dirs, SCOPE_MODEL)


def _telemetry_refusal(unit: EngineUnit, status: str, published: str) -> dict[str, Any] | None:
    """The skip result for a workbook whose `embedded_datasources` telemetry cannot be judged.

    MISSING and INVALID are `not_evaluated` - the key is absent or malformed, so what the workbook
    connected to is UNRECORDED. NONE is `nothing_to_check`, the one complete answer of the three.
    """
    if status == TELEMETRY_MISSING:
        return _engine_skip(
            unit,
            "this workbook's engine telemetry carries NO `embedded_datasources` key, so what it "
            "connected to is UNRECORDED - not absent. Re-run the engine, or check this unit by hand",
            REASON_NOT_EVALUATED,
        )
    if status == TELEMETRY_INVALID:
        return _engine_skip(
            unit,
            "this workbook's `embedded_datasources` telemetry is present but is not a list - "
            "inspect the raw handover; nothing can be concluded from it",
            REASON_NOT_EVALUATED,
        )
    if status == TELEMETRY_NONE:
        note = (
            f" - it binds PUBLISHED datasource '{published}', whose connection is checked in that "
            "datasource's own unit, so co-migrate it and check that unit"
            if published
            else ""
        )
        return _engine_skip(
            unit,
            f"the engine recorded ZERO embedded datasources for this workbook{note}",
            REASON_NOTHING_TO_CHECK,
        )
    return None


def _scan_engine_datasource(unit: EngineUnit) -> dict[str, Any]:
    """Judge one engine DATASOURCE unit at TABLE scope - it names its tables, so nothing is weakened."""
    detail = unit.workbook
    if not (detail.get("connector") or detail.get("connection_class")):
        return _engine_skip(
            unit,
            "this datasource detail records no `connector`, so what it connected to is UNRECORDED - "
            f"not absent (status: {detail.get('status') or 'unknown'})",
            REASON_NOT_EVALUATED,
        )
    model_dirs, why_not = engine_model_dirs(unit.bundle, detail)
    if not model_dirs:
        return _engine_skip(unit, str(why_not), REASON_NOT_EVALUATED)
    return _judge_engine_models(unit, engine_datasource_sources(detail), model_dirs, SCOPE_TABLE)


def _judge_engine_models(
    unit: EngineUnit, sources: list[dict[str, Any]], model_dirs: list[Path], scope: str
) -> dict[str, Any]:
    """Load the unit's models and fold its source verdicts, at whichever scope its payload supports.

    An engine bundle has no `limitations_encountered` - that field is a parser-spec artifact. The
    sanctioned escape hatch on this path is therefore the generated-edit declaration, which is our
    own tier's artifact and works on either path.
    """
    models = [load_model(model_dir) for model_dir in model_dirs]
    declarations = load_generated_edit_declarations(unit.bundle)
    verdicts = _live_verdicts(sources, models, [], declarations, scope)
    return _finalize_unit(unit.source, verdicts, unit_name=unit.name, live_declared=_count_judgeable(sources))


def _engine_skip(unit: EngineUnit, detail: str, reason: str) -> dict[str, Any]:
    """A SKIPPED engine unit that says which kind of skip it is."""
    return _unit_result(unit.source, STATUS_SKIPPED, [], detail=detail, reason=reason, unit_name=unit.name)


def _find_specs(root: Path) -> list[Path]:
    """Every migration-spec.json under a target, root-level first."""
    if root.is_file() and root.name == "migration-spec.json":
        return [root]
    direct = root / "migration-spec.json"
    if direct.is_file():
        return [direct]
    return sorted(root.rglob("migration-spec.json"), key=str)


def _engine_bundle_roots(root: Path) -> list[Path]:
    """Directories under a target that could hold engine telemetry.

    The root itself, plus the parent of any `handover/` folder beneath it, so pointing at an estate
    of bundles works as well as pointing at one bundle. Deliberately NOT an rglob for `report.json`:
    every PBIR report definition in this repo is a file called `report.json`, and a recursive search
    would sweep hundreds of them in. `read_handover.load_workbooks` then rejects any payload without a
    `workbook`/`workbooks` key, which is the content-level half of the same guard.
    """
    roots = [root]
    roots.extend(path.parent for path in root.rglob("handover") if path.is_dir())
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in roots:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        ordered.append(candidate)
    return ordered


def _census_defect(payload: dict[str, Any]) -> str | None:
    """Why this census cannot be trusted to enumerate the bundle's units, or None if it can.

    Round 18: a census was accepted whenever EITHER collection was a list, so `workbooks: "malformed"`
    beside a valid `datasources: []` reported `COVERAGE: 1 of 1`, exit 0 - a wrong-typed collection
    silently contributing zero units. Once a file is identified as an engine census, BOTH collections
    must be present and list-typed, and every entry must be an object. Anything else is a census we
    cannot count from, which is `not_evaluated` - never a smaller denominator.
    """
    for key in ("workbooks", "datasources"):
        if key not in payload:
            return f"the census has no `{key}` collection, so the units it declares cannot be enumerated"
        if not isinstance(payload[key], list):
            return f"the census's `{key}` is {type(payload[key]).__name__}, not a list - it cannot be enumerated"
        bad = [i for i, entry in enumerate(payload[key]) if not isinstance(entry, dict)]
        if bad:
            return f"the census's `{key}` has {len(bad)} non-object entry/entries at index {bad[:5]} - it is malformed"
    return None


def _engine_census(bundle: Path) -> tuple[list[EngineUnit], bool]:
    """`(units, is_census)` from a bundle's `report.json` - workbooks AND datasources.

    `report.json` is the AUTHORITY, and handover slices are copies of it (`run_estate.slice_handovers`
    writes one per `report["workbooks"]` entry). Blind review: resolving units through slices let a
    missing, stale or malformed slice delete a declared unit from the numerator AND the denominator.
    A unit that vanishes must never be indistinguishable from a unit that was checked.

    An UNREADABLE or STRUCTURALLY MALFORMED census is its own unit, not silence. Guarded by CONTENT,
    not by filename - every PBIR report definition in this repo is also called `report.json`, and one
    carries NEITHER collection, so it is silently not-a-census. Carrying EITHER makes it a census, and
    then `_census_defect` requires it to be a well-formed one.
    """
    report = bundle / "report.json"
    if not report.is_file():
        return [], False
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [_census_failure(report, bundle, f"it is unreadable ({exc})")], True
    if not isinstance(payload, dict):
        return [], False
    if "workbooks" not in payload and "datasources" not in payload:
        return [], False
    defect = _census_defect(payload)
    if defect:
        return [_census_failure(report, bundle, defect)], True

    units: list[EngineUnit] = []
    for scope, key in ((SCOPE_MODEL, "workbooks"), (SCOPE_TABLE, "datasources")):
        for detail in payload[key]:
            units.append(EngineUnit(str(detail.get("name") or report.stem), report, bundle, detail, scope))
    return _flag_duplicate_names(units), True


def _census_failure(report: Path, bundle: Path, why: str) -> EngineUnit:
    """One unit standing for a census that cannot be enumerated."""
    return EngineUnit(
        report.stem,
        report,
        bundle,
        {},
        SCOPE_MODEL,
        f"this bundle's report.json is the unit CENSUS and {why}, so how many units this bundle "
        "declares is UNKNOWN - every datasource unit lives only in that file",
    )


def _flag_duplicate_names(units: list[EngineUnit]) -> list[EngineUnit]:
    """Replace every member of a name collision with one `not_evaluated` unit.

    Round 18: two records sharing a name were silently deduplicated, so a flat-only entry could hide a
    live one behind the same identity. Which of the two is real is not knowable here, so neither is
    judged and the collision is reported.
    """
    counts = collections.Counter(unit.name for unit in units)
    out, emitted = [], set()
    for unit in units:
        if counts[unit.name] == 1:
            out.append(unit)
            continue
        if unit.name in emitted:
            continue
        emitted.add(unit.name)
        out.append(
            EngineUnit(
                unit.name,
                unit.source,
                unit.bundle,
                {},
                unit.scope,
                f"{counts[unit.name]} units share the name {unit.name!r} and they do not agree - which "
                "one this is cannot be established, so neither is judged. De-duplicate the bundle",
            )
        )
    return out


def _slice_units(bundle: Path, known: set[str]) -> list[EngineUnit]:
    """Workbook units from `handover/*.json` that the census did not already declare.

    When there IS a census these only add units the report never mentioned. Either way a slice that is
    not VALID JSON becomes an explicit `not_evaluated` unit: it was written as a unit record and can
    no longer be read, which is different from a stray file that happens to live in the folder. That
    distinction is made on parse failure vs missing keys, so a `notes.json` is ignored while a
    truncated slice is surfaced.

    Two slices claiming one name go through the same collision rule as the census: reported, never
    silently deduplicated.
    """
    handover = bundle / "handover" if (bundle / "handover").is_dir() else bundle
    if not handover.is_dir():
        return []
    units: list[EngineUnit] = []
    for path in sorted(handover.glob("*.json")):
        if path.name == "migration-spec.json":
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if handover.name == "handover":
                units.append(
                    EngineUnit(path.stem, path, bundle, {}, SCOPE_MODEL, f"this handover slice is unreadable: {exc}")
                )
            continue
        try:
            found = read_handover.load_workbooks(path)
        except read_handover.HandoverError:
            continue  # valid JSON without a workbook key: a stray file, not a unit
        for name, workbook, source in found:
            if name in known:
                continue
            units.append(EngineUnit(name, source, bundle, workbook, SCOPE_MODEL))
    return _flag_duplicate_names(units)


def _find_engine_units(root: Path) -> list[EngineUnit]:
    """Every engine-bundle unit under a target - workbooks AND datasources, census-first.

    Both halves are needed. Measured on the real cold-run bundle: `handover/` held one workbook slice
    and `report.json` held the only genuinely live Snowflake source in the estate, as a DATASOURCE.
    Discovering workbooks alone found 1 of 2 units and skipped the checkable one.

    Skipped entirely when the target IS a parser unit (a root-level `migration-spec.json`): that
    contract governs the unit and gives the same table-scoped verdict, so scanning twice adds noise.
    """
    if root.is_file():
        if root.name == "migration-spec.json":
            return []
        bundle = root.parent.parent if root.parent.name == "handover" else root.parent
        # A single file is still a view of ONE bundle, so it answers with that bundle's census when
        # there is one. Round 18: pointing at a slice reported `0 of 1` for a bundle that reported
        # `1 of 2` from its directory - three entry points, three different coverage numbers for the
        # same estate. A file argument narrows what you point AT, never what the estate contains.
        census, is_census = _engine_census(bundle)
        if is_census:
            return census + _slice_units(bundle, {unit.name for unit in census})
        try:
            found = read_handover.load_workbooks(root)
        except read_handover.HandoverError:
            return []
        return [EngineUnit(name, src, bundle, wb, SCOPE_MODEL) for name, wb, src in found]
    if (root / "migration-spec.json").is_file():
        return []
    units: list[EngineUnit] = []
    for bundle in _engine_bundle_roots(root):
        census, _is_census = _engine_census(bundle)
        units.extend(census)
        units.extend(_slice_units(bundle, {unit.name for unit in census}))
    return units


def scan(root: Path) -> dict[str, Any]:
    """Scan every migration unit under one path, on EITHER migration tier."""
    units = [scan_unit(spec) for spec in _find_specs(root)]
    units.extend(scan_engine_unit(unit) for unit in _find_engine_units(root))
    return merge(units)


def merge(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-unit results into one verdict.

    Precedence is DOWNGRADED > SKIPPED > OK, and the middle rung is load-bearing. An earlier version
    checked `ok` before `skipped`, so a root holding one clean unit and one unit whose live source
    could not be attributed reported OK and exited 0 - re-opening at the root exactly the false green
    that `_finalize_unit` had just closed at the unit. A clean OK must mean every scanned unit was
    checked and passed, never "at least one passed and the rest were unexaminable".
    """
    downgraded = [u for u in units if u["status"] == STATUS_DOWNGRADED]
    skipped = [u for u in units if u["status"] == STATUS_SKIPPED]
    if not units:
        status = STATUS_SKIPPED
    elif downgraded:
        status = STATUS_DOWNGRADED
    elif skipped:
        status = STATUS_SKIPPED
    else:
        status = STATUS_OK
    return {
        "status": status,
        "units_scanned": len(units),
        "units_checked": len(units) - len(skipped),
        "units_with_downgrade": len(downgraded),
        "units_unchecked": len(skipped),
        # Coverage, split by WHY - acceptance criterion 2 of issue #366. A unit nobody could examine
        # and a unit with genuinely nothing to examine both printed "SKIPPED" and were both read as
        # fine; nine workbooks went unchecked behind that word.
        "units_not_evaluated": sum(1 for u in skipped if u.get("reason") == REASON_NOT_EVALUATED),
        "units_nothing_to_check": sum(1 for u in skipped if u.get("reason") == REASON_NOTHING_TO_CHECK),
        "units_partial_coverage": sum(1 for u in skipped if u.get("reason") == REASON_PARTIAL_COVERAGE),
        "downgraded_sources": sum(u["downgraded"] for u in units),
        "declared_sources": sum(u["declared"] for u in units),
        "connected_sources": sum(u["connected"] for u in units),
        "units": units,
    }


def _coverage_line(report: dict[str, Any]) -> str:
    """One line of coverage, printed on EVERY verdict.

    Acceptance criterion 3. "No findings" is not "all clear" until you know how many units the gate
    actually looked at, and the field run that produced this issue read nine unexamined workbooks as
    a pass precisely because that number was never printed.
    """
    scanned = report.get("units_scanned", 0)
    checked = report.get("units_checked", scanned - report.get("units_unchecked", 0))
    parts = [f"  COVERAGE: {checked} of {scanned} unit(s) examined"]
    not_evaluated = report.get("units_not_evaluated", 0)
    nothing = report.get("units_nothing_to_check", 0)
    partial = report.get("units_partial_coverage", 0)
    if not_evaluated:
        parts.append(f"{not_evaluated} COULD NOT BE evaluated")
    if partial:
        parts.append(f"{partial} partially covered")
    if nothing:
        parts.append(f"{nothing} with nothing to check")
    return "; ".join(parts) + "."


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Human-readable verdict, matching the sibling offline gates."""
    if report["status"] == STATUS_SKIPPED:
        unchecked = report.get("units_unchecked", 0)
        checked = report["units_scanned"] - unchecked
        if not checked:
            return "\n".join(
                [
                    "CONNECTION FIDELITY CHECK: SKIPPED - nothing measured "
                    "(no spec with data_sources, no shipped model, or no live source to check)",
                    _coverage_line(report),
                    *_skipped_unit_lines(report),
                ]
            )
        # PARTIAL COVERAGE is not "nothing measured", and saying so told the operator the opposite of
        # what happened: some units WERE checked and passed, others could not be attributed at all.
        lines = [
            f"CONNECTION FIDELITY CHECK: SKIPPED - partial coverage: {checked} of "
            f"{report['units_scanned']} unit(s) checked, {unchecked} could not be checked.",
            _coverage_line(report),
            *_skipped_unit_lines(report),
        ]
        lines.append(
            "  Read the labels: [NOT EVALUATED] means a downgrade CANNOT be ruled out here - the unit "
            "was not examined (no telemetry, no model, or nothing attributable; on the parser path "
            "usually a spec table name matching no emitted table). [nothing to check] means there was "
            "no live source to compare, which IS a complete answer."
        )
        return "\n".join(lines)
    if report["status"] == STATUS_OK:
        return "\n".join(
            [
                f"CONNECTION FIDELITY CHECK: OK - every live source stays connected across "
                f"{report['units_scanned']} unit(s) ({report['connected_sources']} connected, "
                f"{report['declared_sources']} declared downgrade(s)).",
                _coverage_line(report),
            ]
        )
    lines = [
        f"CONNECTION FIDELITY CHECK: DOWNGRADED - {report['downgraded_sources']} live source(s) "
        f"silently shipped as flat files in {report['units_with_downgrade']} of "
        f"{report['units_scanned']} unit(s).",
        _coverage_line(report),
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
    lines.extend(_skipped_unit_lines(report))
    lines.append(
        "  DOWNGRADED = connect the semantic model to the live upstream system (the packaged extract "
        "is only Tableau's cache), or - if materialising IS correct - record the decision. On the "
        "parser path that is a build-stage limitations_encountered entry (stage semantic_build) or a "
        "generated-edit declaration; an engine bundle has no limitations_encountered, so there it is "
        "the generated-edit declaration."
    )
    return "\n".join(lines)


_REASON_LABELS = {
    REASON_NOT_EVALUATED: "NOT EVALUATED",
    REASON_PARTIAL_COVERAGE: "PARTIAL",
    REASON_NOTHING_TO_CHECK: "nothing to check",
}


def _skipped_unit_lines(report: dict[str, Any]) -> list[str]:
    """One line per skipped unit, LABELLED with which kind of skip it is.

    The label is the whole point. "SKIPPED" alone reads as fine; "NOT EVALUATED" does not, and those
    are the units where a downgrade is still possible and simply unproved.
    """
    lines = []
    for unit in report.get("units", []):
        if unit.get("status") != STATUS_SKIPPED:
            continue
        label = _REASON_LABELS.get(unit.get("reason"), "SKIPPED")
        lines.append(f"  [{label}] {unit.get('unit')}: {unit.get('detail')}")
    return lines


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="bundle/unit folder(s), a migration-spec.json, or an engine handover slice / report.json",
    )
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
        if not (path.is_dir() or (path.is_file() and path.suffix.lower() == ".json")):
            parser.error(f"{path} is not a directory or a JSON unit/telemetry file")

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
