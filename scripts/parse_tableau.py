"""
purpose: Parse a Tableau workbook (.twb / .twbx) or standalone data source (.tds / .tdsx) into
         migration-spec.json, the normalized
         intermediate representation consumed by the pbi-semantic-builder and pbi-report-builder
         subagents. See docs/migration-spec.md for the schema and design rationale.
usage:   python scripts/parse_tableau.py <workbook.twb|.twbx|datasource.tds|.tdsx> -o <migration-spec.json>
"""
# pylint: disable=too-many-lines
# This module sits marginally over the 1200-line cap. The cap is a proxy for "this module does too
# much", and the honest answer here is that a Tableau parser genuinely is large: it was already at
# 98% of the cap before the connection-detail capture needed by the reachability probe was added.
# The real fix is splitting the parser (workbook / datasource / worksheet concerns), which is a
# risky refactor of the most critical file in the repo and does not belong inside a credential-gate
# change. Trimming the explanatory docstrings to squeeze under would trade documented knowledge for
# a number, which is the wrong trade in this codebase. Suppressed deliberately, not accidentally.

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lxml import etree

from connection_target import powerbi_target

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("parse_tableau")

SPEC_VERSION = "1.0"

_LOD_KEYWORD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)
_KEYWORDLESS_LOD_RE = re.compile(
    r"\{[^{}]*\b(SUM|AVG|COUNT|COUNTD|MIN|MAX|ATTR|MEDIAN|STDEV|STDEVP|VAR|VARP|AGG)\s*\(",
    re.IGNORECASE,
)
_TABLE_CALC_RE = re.compile(r"\b(WINDOW_\w+|RUNNING_\w+|INDEX|RANK\w*|LOOKUP|TOTAL|PREVIOUS_VALUE)\s*\(", re.IGNORECASE)
_PARAM_EQUALITY_RE = re.compile(r"if\s*\[Parameters\]\.\[[^\]]+\]\s*=\s*\[[^\]]+\]\s*then", re.IGNORECASE)
_BRACKET_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")
# Tableau's feature-flagged attribute spelling: `_.fcp.<Feature>.<true|false>...<realAttrName>`
_FCP_ATTR = re.compile(r"^_\.fcp\.(?P<feature>[^.]+)\.(?P<state>true|false)\.\.\.(?P<attr>.+)$")
_SHELF_FIELD_RE = re.compile(r"\[([^\[\]]+)\]\.\[([^\[\]]+)\]")
_TABLEAU_LB_SENTINEL_RE = re.compile(r"^\s*[\u00c6\u00a0]+\s*$")


def slugify(text: str) -> str:
    """Lowercase, alnum + underscore slug used to build stable synthetic ids."""
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return re.sub(r"_+", "_", text).strip("_") or "field"


@dataclass
class IdRegistry:
    """Tracks assigned ids to keep them stable and unique within a run."""

    seen: dict[str, int] = field(default_factory=dict)

    def make(self, prefix: str, *parts: str) -> str:
        """Build a stable, unique id like 'fld.ds_name__field_name', disambiguating collisions."""
        base = f"{prefix}.{'__'.join(slugify(p) for p in parts if p)}"
        count = self.seen.get(base, 0)
        self.seen[base] = count + 1
        return base if count == 0 else f"{base}_{count}"


def _wrap_datasource_as_workbook(ds_el: etree._Element) -> etree._Element:
    """Wrap a standalone `<datasource>` root (a .tds/.tdsx) in a synthetic `<workbook><datasources>`
    shell so every downstream `datasources/datasource` lookup works unchanged. A .tds legitimately has
    no worksheets or dashboards, so those parse to empty lists."""
    workbook = etree.Element("workbook")
    datasources = etree.SubElement(workbook, "datasources")
    datasources.append(ds_el)
    return workbook


def load_twb_root(path: Path) -> tuple[etree._Element, dict[str, str]]:
    """Return the parsed Tableau XML root, plus a map of hyper-file relative paths found in the archive
    (empty for a plain .twb/.tds with no packaged extracts).

    Accepts a workbook (.twb/.twbx) or a standalone data source (.tds/.tdsx). The latter matters for
    PUBLISHED data sources: a workbook that points at one (connection class 'sqlproxy') does NOT carry
    that datasource's calculated fields, so the exported .tds must be parsed to see them."""
    suffix = path.suffix.lower()
    hyper_files: dict[str, str] = {}
    if suffix in (".twbx", ".tdsx"):
        inner_ext = ".twb" if suffix == ".twbx" else ".tds"
        with zipfile.ZipFile(path) as zf:
            inner_names = [n for n in zf.namelist() if n.lower().endswith(inner_ext)]
            if not inner_names:
                raise ValueError(f"No {inner_ext} found inside {path}")
            xml_bytes = zf.read(inner_names[0])
            hyper_files = {Path(n).name: n for n in zf.namelist() if n.lower().endswith(".hyper")}
    else:
        xml_bytes = path.read_bytes()
    root = etree.fromstring(xml_bytes)
    if root.tag == "datasource":
        root = _wrap_datasource_as_workbook(root)
    return root, hyper_files


def parse_parameters(root: etree._Element, ids: IdRegistry) -> list[dict[str, Any]]:
    """The 'Parameters' pseudo-datasource holds workbook-level user controls."""
    parameters = []
    param_ds = root.find("datasources/datasource[@name='Parameters']")
    if param_ds is None:
        return parameters
    for col in param_ds.findall("column"):
        caption = col.get("caption") or col.get("name", "").strip("[]")
        calc = col.find("calculation")
        current_value = calc.get("formula") if calc is not None else col.get("value")
        if isinstance(current_value, str):
            current_value = current_value.strip('"')
        members = [m.get("value", "").strip('"') for m in col.findall("members/member")]
        parameters.append(
            {
                "id": ids.make("param", caption),
                "internal_name": col.get("name", ""),
                "caption": caption,
                "data_type": col.get("datatype", "string"),
                "domain_type": col.get("param-domain-type", "list"),
                "allowed_values": members,
                "current_value": current_value,
            }
        )
    logger.info("Parsed %d parameter(s)", len(parameters))
    return parameters


def _conn_attr(conn_el: etree._Element, attr: str) -> str | None:
    """Read a connection attribute, tolerating Tableau's feature-flagged spelling.

    When a connector gains a capability behind a document-format flag, Tableau writes the attribute
    as `_.fcp.<Feature>.<true|false>...<attr>` and ships BOTH variants, whose meanings differ. For
    `DatabricksCatalog`: the `.true...` variant of `dbname` is the Unity catalog while the
    `.false...` variant is the LEGACY meaning (it held the HTTP path), and only `.true...` carries
    `v-http-path` at all.

    So the plain name must be tried first, then the variant the `<document-format-change-manifest>`
    declares live. A blind prefix-strip is actively harmful - it lets `.false...dbname` overwrite
    the catalog with a `/sql/1.0/warehouses/...` string, a wrong value that still looks like a value.

    Measured 2026-08-05: without this, a real Databricks `.twbx` yields `http_path: None` and
    `database: None`, so the emitted M cannot connect. The deterministic tier had the same gap then;
    re-checked 2026-08-07, it no longer does — `connection_to_m` now calls `_resolve_fcp_attributes`
    before any connection reader, so its bare-name lookups are correct by construction. Both sides
    are right, and neither is redundant: his feeds M generation, this feeds the live-vs-flat-file
    classification that arms the credential gate.
    """
    direct = conn_el.get(attr)
    if direct:
        return direct

    root = conn_el.getroottree().getroot()
    manifest = root.find(".//document-format-change-manifest")
    live = set()
    for child in manifest if manifest is not None else []:
        match = _FCP_ATTR.match(str(child.tag))
        if match and match.group("state") == "true":
            live.add(match.group("feature"))

    for name, value in conn_el.attrib.items():
        match = _FCP_ATTR.match(str(name))
        if not match or match.group("attr") != attr:
            continue
        if (match.group("feature") in live) == (match.group("state") == "true"):
            return value
    return None


def _capture_connect_details(connection: dict[str, Any], conn_el: etree._Element) -> None:
    """Carry the attributes needed to CONNECT, not just to describe.

    A Databricks M query needs the SQL-warehouse HTTP path and most warehouses need the schema;
    dropping these forced the reachability probe to re-read the raw `.twb`. Empty values are skipped
    because Tableau writes `attr=''` for unset options.

    `warehouse` and `role` are Snowflake's equivalents: a Snowflake query cannot run without a
    compute warehouse, and corporate accounts commonly require an explicit role because the user's
    default has no grants on the target database. Both are plain connection attributes, never
    secrets.
    """
    for spec_key, attr in (
        ("http_path", "v-http-path"),
        ("schema", "schema"),
        ("warehouse", "warehouse"),
        ("role", "role"),
    ):
        value = (_conn_attr(conn_el, attr) or "").strip()
        if value:
            connection[spec_key] = value


def _parse_connection(ds_el: etree._Element, hyper_files: dict[str, str]) -> dict[str, Any]:
    """Resolve both the *original* source connection (e.g. excel-direct, sqlserver) and whether the
    datasource runs off a packaged .hyper extract (Tableau Public workbooks always do).

    A `class='federated'` datasource can wrap SEVERAL named connections - a Tableau join across, say,
    Azure SQL + Snowflake + Databricks is one datasource with three. `connection` (singular) keeps
    describing the FIRST, because the schema, the 16 committed example specs and every consumer
    predate this; the full list is added as `connections` so nothing downstream has to guess.

    ⚠️ This is a SAFETY fix, not a completeness one. Measured 2026-08-04 on a tri-source workbook:
    the parser reported one connection, so `preflight_source_credentials` armed the credential gate
    for 1 of 3 live systems and reported the other two nowhere. Under-reporting live sources is the
    one direction the gate must never fail in.
    """
    outer_conn = ds_el.find("connection")
    connection: dict[str, Any] = {"class": "unknown", "mode": "live", "server": None, "database": None, "note": None}
    if outer_conn is None:
        connection["powerbi_target"], connection["powerbi_target_reason"] = powerbi_target("unknown", "live")
        return connection

    # Both branches want the same treatment; pick the element that actually describes the source.
    named_conns = outer_conn.findall(".//named-connections/named-connection/connection")
    source_el = named_conns[0] if named_conns else None
    if source_el is None and outer_conn.get("class") not in (None, "federated"):
        source_el = outer_conn
    if source_el is not None:
        connection["class"] = source_el.get("class", "unknown")
        connection["server"] = source_el.get("server")
        connection["database"] = source_el.get("dbname")
        _capture_connect_details(connection, source_el)

    extract_conn = ds_el.find(".//extract/connection")
    if extract_conn is not None:
        connection["mode"] = "extract"
        hyper_name = Path(extract_conn.get("dbname", "")).name
        connection["hyper_file"] = hyper_files.get(hyper_name, extract_conn.get("dbname"))
        connection["note"] = (
            f"extract-based - original logical source was '{connection['class']}'; "
            "the packaged .hyper holds Tableau's cached copy of those rows"
        )
    connection["powerbi_target"], connection["powerbi_target_reason"] = powerbi_target(
        connection["class"], connection["mode"]
    )

    # Every named connection, in document order, each independently classified. Emitted whenever a
    # federated wrapper is present - including the single-connection case, so consumers can read one
    # field unconditionally instead of branching on how many there are.
    if named_conns:
        connection["connections"] = [_describe_named_connection(el, connection["mode"]) for el in named_conns]
    return connection


def _describe_named_connection(conn_el: etree._Element, mode: str) -> dict[str, Any]:
    """One entry of `connection.connections[]`: class, server, database, and its own PBI target.

    `mode` is inherited from the parent datasource: an extract caches the whole federated join, not
    one leg of it, so every named connection under an extracted datasource is itself extract-backed.
    """
    described: dict[str, Any] = {
        "class": conn_el.get("class", "unknown"),
        "server": _conn_attr(conn_el, "server"),
        "database": _conn_attr(conn_el, "dbname"),
        "mode": mode,
    }
    _capture_connect_details(described, conn_el)
    described["powerbi_target"], described["powerbi_target_reason"] = powerbi_target(described["class"], mode)
    return described


_CONTAINER_RELATION_TYPES = {"collection", "join", "union"}


def _published_ds_name(repo: etree._Element, connection: dict[str, Any]) -> tuple[str | None, str]:
    """Resolve the published datasource's real name, and say which attribute it came from.

    Precedence is deliberate, and was derived from real published workbooks:
      1. the last path segment of `derived-from` (the authoritative publish URL);
      2. the **sqlproxy** connection's `dbname` -- and ONLY sqlproxy's, see below;
      3. the `repository-location@id` attribute -- LAST, because it can go stale.
    Real-world evidence (github.com/vimosh0812/ai-bi-assistant, `new-ds.twb`): a Tableau Cloud
    workbook published against datasource `dandan003` carried `id='new'` (a leftover from a rename)
    while `derived-from`, `dbname` and `caption` all agreed on `dandan003`. Keying on `id` would have
    given two workbooks that share ONE datasource two different keys -- defeating the de-duplication
    this key exists for.

    ⚠️ **Rule 2 is guarded on the connection CLASS, and must stay guarded.** For a `sqlproxy`
    connection, `dbname` IS the published datasource's name -- that is what makes the rule sound.
    For any other class it is the *physical database*, which is a completely different thing.
    Measured 2026-08-07 on a downloaded `.tds`: the connection is `snowflake` with
    `dbname='MERIDIAN'`, so an unguarded rule 2 keyed the datasource as
    `fabric-migration-lab/meridian` while the two workbooks binding it keyed as
    `fabric-migration-lab/meridiansaleslivesnowflake` (via `derived-from`). The keys could never
    join, so the datasource-to-workbook seam silently did not de-duplicate -- and the failure is
    invisible, because both keys look perfectly reasonable on their own.
    """
    derived = repo.get("derived-from")
    if derived:
        segment = derived.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if segment:
            # The publish URL percent-encodes the name, so a datasource called "Sales Master" appears
            # as "Sales%20Master". Decode it so the key matches the plain name the Tableau REST /
            # Metadata API returns for the same datasource.
            return unquote(segment), "derived-from"
    if (connection.get("class") or "").lower() == "sqlproxy":
        dbname = connection.get("database")
        if dbname:
            return dbname, "connection.dbname"
    return repo.get("id"), "repository-location.id"


def _parse_published_datasource(ds_el: etree._Element, connection: dict[str, Any]) -> dict[str, Any] | None:
    """Identify a Tableau *published* (server-side) datasource the workbook merely points at.

    A workbook that connects to a Published Data Source carries `connection class='sqlproxy'` plus a
    `<repository-location>` naming the server-side datasource. The datasource's own metadata -- its
    connection details, custom SQL and (critically) its calculated-field formulas -- live centrally in
    that published datasource, NOT in this .twb, so parsing the workbook alone silently under-reports
    them. The exported `.tds`/`.tdsx` is required for complete coverage.

    Returns None for ordinary embedded datasources.

    The `key` is a STABLE identity (site + datasource name, lowercased) deliberately excluding
    `revision` and the server host: it is what lets the orchestrator recognise that several workbooks
    share ONE published datasource and bind them all to a single Power BI semantic model instead of
    rebuilding an identical model per workbook.
    """
    conn_classes = {c.get("class") for c in ds_el.iter("connection")}
    repo = ds_el.find("repository-location")
    # A standalone .tds carries an EMPTY `<repository-location />` placeholder even when the datasource
    # is not published at all (verified against tableau/document-api-python's datasource_test.tds), so
    # an element alone proves nothing -- require real server identity, or a sqlproxy connection.
    has_repo_identity = repo is not None and bool(repo.get("id") or repo.get("derived-from"))
    if "sqlproxy" not in conn_classes and not has_repo_identity:
        return None
    if repo is None or not has_repo_identity:
        return {
            "id": None,
            "site": None,
            "path": None,
            "derived_from": None,
            "revision": None,
            "key": None,
            "name_source": None,
            "id_attribute": None,
        }

    ds_name, name_source = _published_ds_name(repo, connection)
    site = repo.get("site")
    key_parts = [p for p in (site, ds_name) if p]
    return {
        "id": ds_name,
        "site": site,
        "path": repo.get("path"),
        "derived_from": repo.get("derived-from"),
        "revision": repo.get("revision"),
        "key": "/".join(key_parts).lower() if key_parts else None,
        "name_source": name_source,
        # Kept for auditability: when this differs from `id`, the server-side datasource was renamed
        # after this workbook was published, and `id` is the stale value.
        "id_attribute": repo.get("id"),
    }


def _collect_leaf_relations(rel: etree._Element) -> list[etree._Element]:
    """Descend container relations (collection/join/union wrappers) to their leaf table/text relations,
    so a multi-file collection or a join surfaces each underlying physical table instead of one opaque
    wrapper. Falls back to the wrapper itself if it has no nested <relation> children."""
    if rel.get("type", "table") in _CONTAINER_RELATION_TYPES:
        leaves = [leaf for child in rel.findall("relation") for leaf in _collect_leaf_relations(child)]
        return leaves or [rel]
    return [rel]


def _parse_tables(ds_el: etree._Element, ids: IdRegistry) -> list[dict[str, Any]]:
    """Parse top-level <relation> entries (descending collection/join/union containers to leaf tables;
    skipping the nested extract/[Extract].[Extract] relation, which lives under <extract>)."""
    tables = []
    outer_conn = ds_el.find("connection")
    if outer_conn is None:
        return tables
    for top in outer_conn.findall("relation"):
        for rel in _collect_leaf_relations(top):
            rel_type = rel.get("type", "table")
            name = rel.get("name") or rel.get("table", "table")
            tables.append(
                {
                    "id": ids.make("tbl", name),
                    "name": name,
                    "source_relation": "custom-sql" if rel_type == "text" else rel_type,
                    "custom_sql": rel.text if rel_type == "text" else None,
                }
            )
    return tables


def _table_from_ref(ref: str | None) -> str | None:
    """Recover the participating table name from a Tableau '[Table].[Field]' join-condition
    reference. Used when a join operand is itself a nested <relation type='join'> (a chained
    star-schema join) and so carries no direct name/table attribute of its own."""
    match = re.match(r"\[([^\]]+)\]\.\[", ref or "")
    return match.group(1) if match else None


def _parse_joins(ds_el: etree._Element) -> list[dict[str, Any]]:
    """Extract every <relation type='join'> operand pair, join type, and on-clause into a join graph so
    pbi-semantic-builder can rebuild Power BI model relationships. Conditions carry the raw Tableau
    [Table].[Field] references from each equality expression in the join clause."""
    outer_conn = ds_el.find("connection")
    if outer_conn is None:
        return []
    joins = []
    for jrel in outer_conn.iter("relation"):
        if jrel.get("type") != "join":
            continue
        operands = jrel.findall("relation")
        conditions = []
        clause = jrel.find("clause")
        if clause is not None:
            for eq in clause.iter("expression"):
                sides = eq.findall("expression")
                if eq.get("op") == "=" and len(sides) == 2:
                    conditions.append({"left_field": sides[0].get("op", ""), "right_field": sides[1].get("op", "")})
        left = operands[0].get("name") or operands[0].get("table") if operands else None
        right = operands[1].get("name") or operands[1].get("table") if len(operands) > 1 else None
        # A join operand that is itself a nested join (chained star-schema) has no name/table;
        # recover the table from the on-clause's [Table].[Field] reference instead of emitting null.
        if left is None and conditions:
            left = _table_from_ref(conditions[0]["left_field"])
        if right is None and conditions:
            right = _table_from_ref(conditions[0]["right_field"])
        joins.append({"left": left, "right": right, "type": jrel.get("join", "inner"), "conditions": conditions})
    return joins


def _classify_calculation(formula: str) -> dict[str, bool | str | None]:
    reshape_hint = None
    if "Pivot Field Names" in formula or "Pivot Field Values" in formula:
        reshape_hint = "pivot_derived"
    return {
        "is_lod": bool(_LOD_KEYWORD_RE.search(formula) or _KEYWORDLESS_LOD_RE.search(formula)),
        "is_table_calc": bool(_TABLE_CALC_RE.search(formula)),
        "reshape_hint": reshape_hint,
    }


def _build_field_entry(col: etree._Element, ds_id: str, table_id: str | None, ids: IdRegistry) -> dict[str, Any]:
    """Build one field entry dict from a <column> element (base column or calculated field)."""
    internal_name = col.get("name", "")
    caption = col.get("caption") or internal_name.strip("[]")
    calc_el = col.find("calculation")
    calc_class = calc_el.get("class") if calc_el is not None else None
    formula = calc_el.get("formula") if calc_el is not None else None
    aliases = {a.get("key", "").strip('"'): a.get("value", "") for a in col.findall("aliases/alias")}

    entry: dict[str, Any] = {
        "id": ids.make("fld", ds_id, caption),
        "internal_name": internal_name,
        "caption": caption,
        "table_id": table_id,
        "kind": "bin" if calc_class == "bin" else "calculated" if formula is not None else "column",
        "data_type": col.get("datatype", "string"),
        "role": col.get("role", "dimension"),
        "default_aggregation": None,
        "hidden": col.get("hidden") == "true",
        "semantic_role": None,
        "formatting": {},
        "aliases": aliases,
    }
    if formula is not None:
        entry["tableau_formula"] = formula
        entry.update(_classify_calculation(formula))
    if calc_class == "bin":
        entry["bin_size"] = calc_el.get("bin-size")
        entry["bin_source_column"] = calc_el.get("column")
    return entry


_METADATA_MEASURE_TYPES = {"integer", "real"}


def _build_metadata_column_entry(
    rec: etree._Element, ds_id: str, table_id: str | None, ids: IdRegistry
) -> dict[str, Any] | None:
    """Build a field entry from a <metadata-record class='column'> that has no matching <column>
    element. Tableau lists every physical/extract column in metadata-records even when it was never
    dragged onto a shelf or given a <column> definition, so scanning them recovers physical columns
    that fields[] would otherwise silently omit (verified across two workbooks: extract-based sources
    dropped e.g. 'Billable Miles'/'Status' and 'Adj Close', some the basis of downstream calcs)."""
    local_name_el = rec.find("local-name")
    if local_name_el is None or not local_name_el.text:
        return None
    internal_name = local_name_el.text
    remote_el = rec.find("remote-name")
    caption = remote_el.text if remote_el is not None and remote_el.text else internal_name.strip("[]")
    local_type_el = rec.find("local-type")
    data_type = local_type_el.text if local_type_el is not None and local_type_el.text else "string"
    return {
        "id": ids.make("fld", ds_id, caption),
        "internal_name": internal_name,
        "caption": caption,
        "table_id": table_id,
        "kind": "column",
        "data_type": data_type,
        "role": "measure" if data_type in _METADATA_MEASURE_TYPES else "dimension",
        "default_aggregation": None,
        "hidden": False,
        "semantic_role": None,
        "formatting": {},
        "aliases": {},
        "from_metadata_record": True,
    }


def _resolve_field_dependencies(fields: list[dict[str, Any]], name_to_id: dict[str, str]) -> None:
    """Second pass: now that every internal name in this datasource is known, resolve each
    calculated field's raw [bracketed] formula references to field ids, in place."""
    for entry in fields:
        formula = entry.get("tableau_formula")
        if not formula:
            continue
        refs = {
            name_to_id[bracketed]
            for token in _BRACKET_TOKEN_RE.findall(formula)
            if (bracketed := f"[{token}]") in name_to_id and name_to_id[bracketed] != entry["id"]
        }
        entry["referenced_fields"] = sorted(refs)


def _parse_fields(
    ds_el: etree._Element, ds_id: str, table_id: str | None, ids: IdRegistry
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Parse <column> definitions (incl. calculated fields) directly under the datasource, then
    supplement with any physical columns that appear only in <metadata-records> (never surfaced as a
    <column>). Returns (fields, internal_name -> field_id map) for later cross-referencing."""
    fields = [_build_field_entry(col, ds_id, table_id, ids) for col in ds_el.findall("column")]
    known_internal_names = {f["internal_name"] for f in fields}
    for rec in ds_el.findall(".//metadata-record[@class='column']"):
        local_name_el = rec.find("local-name")
        if local_name_el is None or not local_name_el.text or local_name_el.text in known_internal_names:
            continue
        entry = _build_metadata_column_entry(rec, ds_id, table_id, ids)
        if entry is not None:
            fields.append(entry)
            known_internal_names.add(entry["internal_name"])
    name_to_id = {f["internal_name"]: f["id"] for f in fields}
    _resolve_field_dependencies(fields, name_to_id)
    return fields, name_to_id


def _parse_single_data_source(
    ds_el: etree._Element, hyper_files: dict[str, str], ids: IdRegistry
) -> tuple[dict[str, Any], str, dict[str, str], dict[str, str]]:
    """Parse one <datasource> element.
    Returns (data_source_dict, internal_name, instance_map, name_to_id_map). name_to_id_map (raw
    Tableau [bracketed] column name -> field id) is returned separately so worksheet-local
    column-instances (declared only inside a worksheet's <datasource-dependencies>, not centrally on
    the datasource) can still be resolved later."""
    internal_name = ds_el.get("name", "")
    caption = ds_el.get("caption") or internal_name
    ds_id = ids.make("ds", caption)

    tables = _parse_tables(ds_el, ids)
    table_id = tables[0]["id"] if len(tables) == 1 else None
    fields, name_to_id = _parse_fields(ds_el, ds_id, table_id, ids)

    instance_map = {
        ci.get("name", ""): name_to_id[ci.get("column", "")]
        for ci in ds_el.findall("column-instance")
        if ci.get("column", "") in name_to_id
    }

    connection = _parse_connection(ds_el, hyper_files)
    data_source = {
        "id": ds_id,
        "caption": caption,
        "internal_name": internal_name,
        "connection": connection,
        "tables": tables,
        "joins": _parse_joins(ds_el),
        "fields": fields,
    }
    published = _parse_published_datasource(ds_el, connection)
    if published is not None:
        data_source["published_datasource"] = published
    return data_source, internal_name, instance_map, name_to_id


def parse_data_sources(
    root: etree._Element, hyper_files: dict[str, str], ids: IdRegistry
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Returns (data_sources, instance_maps, name_to_id_maps), both keyed by datasource internal name.
    instance_maps resolves shelf/filter column-instance names to field ids directly; name_to_id_maps
    is the fallback used to resolve worksheet-local column-instances (see _parse_single_data_source)."""
    data_sources = []
    instance_maps: dict[str, dict[str, str]] = {}
    name_to_id_maps: dict[str, dict[str, str]] = {}

    for ds_el in root.findall("datasources/datasource"):
        if ds_el.get("name", "") == "Parameters":
            continue
        data_source, internal_name, instance_map, name_to_id = _parse_single_data_source(ds_el, hyper_files, ids)
        data_sources.append(data_source)
        instance_maps[internal_name] = instance_map
        name_to_id_maps[internal_name] = name_to_id

    logger.info("Parsed %d data source(s)", len(data_sources))
    return data_sources, instance_maps, name_to_id_maps


def _resolve_shelf(shelf_text: str | None, ds_instance_map: dict[str, str]) -> list[dict[str, Any]]:
    """Tokenize a Tableau shelf expression (e.g. '([ds].[a] / [ds].[b])') into resolved field refs.
    Falls back to a raw, unresolved note when a token can't be matched to a known field id."""
    if not shelf_text:
        return []
    results = []
    for _ds_ref, instance_name in _SHELF_FIELD_RE.findall(shelf_text):
        bracketed_instance = f"[{instance_name}]"
        field_id = ds_instance_map.get(bracketed_instance)
        derivation = instance_name.split(":")[0] if ":" in instance_name else None
        results.append(
            {
                "field_id": field_id or f"UNRESOLVED:{instance_name}",
                "aggregation": derivation.upper() if derivation in ("sum", "avg", "cnt", "min", "max") else None,
                "derivation": derivation,
                "nested_with": None,
            }
        )
    return results


def _text_from_runs(container: etree._Element | None) -> str | None:
    """Flatten a Tableau <formatted-text><run>...</run></formatted-text> block into plain text."""
    if container is None:
        return None
    parts: list[str] = []
    pending_line_break = False
    for run in container.findall("run"):
        text = run.text or ""
        if _TABLEAU_LB_SENTINEL_RE.match(text):
            pending_line_break = True
            continue
        if pending_line_break and parts and not parts[-1].endswith("\n"):
            parts.append("\n")
        parts.append(text)
        pending_line_break = False
    return "".join(parts)


def _build_worksheet_instance_map(
    view: etree._Element, primary_ds: str | None, base_map: dict[str, str], name_to_id: dict[str, str]
) -> dict[str, str]:
    """Merge datasource-level column-instances with worksheet-local ones. Some column-instances (e.g.
    a parameter-equality filter's derivation, or a gauge's scaled axis) are declared only inside a
    worksheet's own <datasource-dependencies>, not centrally on the datasource - resolve those against
    the datasource's global name_to_id map (raw column name -> field id) instead of dropping them.

    Known remaining gap (flagged in limitations_encountered, not silently dropped): Tableau ad-hoc
    "unnamed" calculations created directly on a shelf (marked user:unnamed=...) live *only* inside a
    worksheet's <datasource-dependencies> and are never registered on the datasource itself, so they
    can't be resolved here; likewise the built-in 'Measure Names'/'Measure Values' pseudo-fields and
    Tableau Groups. Phase 2: promote these to first-class datasource fields instead of just flagging."""
    instance_map = dict(base_map)
    for dep in view.findall(f"datasource-dependencies[@datasource='{primary_ds}']"):
        for ci in dep.findall("column-instance"):
            name_attr = ci.get("name", "")
            if name_attr in instance_map:
                continue
            field_id = name_to_id.get(ci.get("column", ""))
            if field_id:
                instance_map[name_attr] = field_id
            else:
                logger.debug("Unresolved worksheet-local column-instance: %s", name_attr)
    return instance_map


def _resolve_encoding_field(
    encodings_el: etree._Element | None, tag: str, instance_map: dict[str, str]
) -> dict[str, Any] | None:
    """Resolve a single-field encoding shelf (color/size/shape/text) to its field id."""
    el = encodings_el.find(tag) if encodings_el is not None else None
    if el is None:
        return None
    match = _SHELF_FIELD_RE.search(el.get("column", ""))
    if not match:
        return None
    instance_name = f"[{match.group(2)}]"
    return {"field_id": instance_map.get(instance_name, f"UNRESOLVED:{instance_name}")}


def _resolve_encoding_fields(
    encodings_el: etree._Element | None, tag: str, instance_map: dict[str, str]
) -> list[dict[str, Any]]:
    """Resolve a multi-field encoding shelf to a list of field ids, deduped in document order.
    Tableau's Detail shelf serializes as one <lod> element per field and the Tooltip shelf as one
    <tooltip> element per field (both carry a `column` attribute like color/size)."""
    if encodings_el is None:
        return []
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for el in encodings_el.findall(tag):
        match = _SHELF_FIELD_RE.search(el.get("column", ""))
        if not match:
            continue
        instance_name = f"[{match.group(2)}]"
        field_id = instance_map.get(instance_name, f"UNRESOLVED:{instance_name}")
        if field_id not in seen:
            seen.add(field_id)
            resolved.append({"field_id": field_id})
    return resolved


def _resolve_mark_type(pane: etree._Element | None) -> str:
    """Return the worksheet's Tableau mark class (Bar/Line/Circle/.../Automatic), defaulting safely
    when the pane or its <mark> child is absent."""
    mark_el = pane.find("mark") if pane is not None else None
    return mark_el.get("class", "Automatic") if mark_el is not None else "Automatic"


def _parse_reference_lines(pane: etree._Element | None) -> list[dict[str, Any]]:
    """Tableau's Min/Max/Average reference-line-on-a-fixed-axis trick - the source pattern for
    KPI-gauge-style worksheets (see docs/tableau-dax-translation-guide.md #5)."""
    if pane is None:
        return []
    return [
        {
            "id": rl.get("id", ""),
            "label": rl.get("label", ""),
            "formula": rl.get("formula", "constant"),
            "value": rl.get("value"),
            "scope": rl.get("scope", "per-table"),
        }
        for rl in pane.findall("reference-line")
    ]


def _shelf_has_marker(shelf: list[dict[str, Any]] | None, marker: str) -> bool:
    """True if a resolved shelf (rows/columns/label - always a list, even for single-field shelves)
    carries an UNRESOLVED field_id containing the given Tableau pseudo-field marker text."""
    return any(isinstance(f, dict) and marker in str(f.get("field_id", "")) for f in shelf or [])


def _detect_measure_values_pivot(
    encodings: dict[str, Any], filters: list[dict[str, Any]], instance_map: dict[str, str]
) -> dict[str, Any] | None:
    """Detect Tableau's built-in 'Measure Names/Measure Values' virtual pivot (dragging the Measure
    Names pseudo-dimension onto an axis so 'Measure Values' can carry N real measures at once). It has
    no backing datasource field, so _resolve_shelf/_resolve_encoding_field always emit it as an opaque
    UNRESOLVED:... reference - Power BI has no equivalent pseudo-dimension, so this idiom always needs
    a manual rebuild (e.g. one field per resolved measure bound directly to the visual). See
    docs/tableau-dax-translation-guide.md and pbi-report-builder.agent.md Gotchas.

    Returns None if the idiom isn't present on this worksheet. Otherwise returns the axis carrying the
    'Measure Names' marker plus the resolved list of real field ids 'Measure Values' would have shown,
    read off the accompanying 'Measure Names' filter's members (each member is a quoted, fully
    qualified '[datasource].[instance]' reference using the same grammar as shelf fields) - so
    downstream consumers get ready-to-use field ids instead of reverse-parsing the filter themselves.

    'Multiple Values' (seen on the *other* axis in the EEA sample - a display artifact of some
    unrelated multi-field shelf combination) is only used as a fallback signal: on its own it doesn't
    imply a real pivot the way the 'Measure Names' marker does, so 'Measure Names' is checked first on
    every candidate axis before falling back to 'Multiple Values' on any of them."""
    candidate_axes = ("rows", "columns", "label")
    axis = next((a for a in candidate_axes if _shelf_has_marker(encodings.get(a), "Measure Names")), None)
    if axis is None:
        axis = next((a for a in candidate_axes if _shelf_has_marker(encodings.get(a), "Multiple Values")), None)
    if axis is None:
        return None

    pivoted_field_ids: list[str] = []
    for filt in filters:
        if "Measure Names" not in str(filt.get("field_id", "")):
            continue
        for member in filt.get("members", []):
            match = _SHELF_FIELD_RE.search(member)
            if match is None:
                continue
            bracketed_instance = f"[{match.group(2)}]"
            pivoted_field_ids.append(instance_map.get(bracketed_instance, f"UNRESOLVED:{match.group(2)}"))

    resolution_note = (
        "Bind each resolved field in pivoted_field_ids directly as its own field on the target visual "
        "(e.g. one Y-axis field per measure on a clustered column chart), rather than trying to "
        "recreate a literal pivot column."
        if pivoted_field_ids
        else "No accompanying 'Measure Names' filter with a resolvable member list was found - inspect "
        "this worksheet's shelves and customized_tooltip_text by hand to recover which real measures "
        "were intended."
    )
    return {
        "axis": axis,
        "pivoted_field_ids": pivoted_field_ids,
        "note": f"Tableau 'Measure Names/Measure Values' virtual pivot - no direct Power BI equivalent. "
        f"{resolution_note}",
    }


def _parse_worksheet_filters(view: etree._Element, instance_map: dict[str, str]) -> list[dict[str, Any]]:
    filters = []
    for filt in view.findall("filter"):
        match = _SHELF_FIELD_RE.search(filt.get("column", ""))
        instance_name = f"[{match.group(2)}]" if match else filt.get("column", "")
        group = filt.find("groupfilter")
        exclude_nulls = group is not None and group.get("function") == "except"
        filters.append(
            {
                "field_id": instance_map.get(instance_name, f"UNRESOLVED:{instance_name}"),
                "type": filt.get("class", "categorical"),
                "direction": filt.get("direction"),
                "max": filt.get("max"),
                "exclude_nulls": exclude_nulls,
                "members": [g.get("member", "") for g in filt.findall(".//groupfilter[@function='member']")],
                "note": None,
            }
        )
    return filters


def _parse_single_worksheet(
    ws_el: etree._Element,
    instance_maps: dict[str, dict[str, str]],
    name_to_id_maps: dict[str, dict[str, str]],
    ids: IdRegistry,
) -> dict[str, Any] | None:
    """Parse one <worksheet> element into its migration-spec representation, or None if it has no
    view (shouldn't happen in practice, guards against malformed input)."""
    view = ws_el.find("table/view")
    if view is None:
        return None

    name = ws_el.get("name", "")
    ds_refs = [d.get("name") for d in view.findall("datasources/datasource") if d.get("name") != "Parameters"]
    primary_ds = ds_refs[0] if ds_refs else None
    instance_map = _build_worksheet_instance_map(
        view, primary_ds, instance_maps.get(primary_ds, {}), name_to_id_maps.get(primary_ds, {})
    )

    pane = ws_el.find("table/panes/pane")
    mark_type = _resolve_mark_type(pane)
    encodings_el = pane.find("encodings") if pane is not None else None
    label_field = _resolve_encoding_field(encodings_el, "text", instance_map)

    encodings = {
        "rows": _resolve_shelf(ws_el.findtext("table/rows"), instance_map),
        "columns": _resolve_shelf(ws_el.findtext("table/cols"), instance_map),
        "color": _resolve_encoding_field(encodings_el, "color", instance_map),
        "size": _resolve_encoding_field(encodings_el, "size", instance_map),
        "shape": _resolve_encoding_field(encodings_el, "shape", instance_map),
        "label": [label_field] if label_field else [],
        "detail": _resolve_encoding_fields(encodings_el, "lod", instance_map),
        "tooltip": _resolve_encoding_fields(encodings_el, "tooltip", instance_map),
    }
    filters = _parse_worksheet_filters(view, instance_map)

    return {
        "id": ids.make("ws", name),
        "name": name,
        "title_text": _text_from_runs(ws_el.find(".//layout-options/title/formatted-text")),
        "data_source_ids": ds_refs,
        "mark_type": mark_type,
        "encodings": encodings,
        "reference_lines": _parse_reference_lines(pane),
        "filters": filters,
        "measure_names_values_pivot": _detect_measure_values_pivot(encodings, filters, instance_map),
        "manual_sort": [],
        "customized_tooltip_text": _text_from_runs(ws_el.find(".//customized-tooltip/formatted-text")),
    }


def parse_worksheets(
    root: etree._Element,
    instance_maps: dict[str, dict[str, str]],
    name_to_id_maps: dict[str, dict[str, str]],
    ids: IdRegistry,
) -> list[dict[str, Any]]:
    """Parse every <worksheet> in the workbook into its migration-spec representation."""
    worksheets = [
        parsed
        for ws_el in root.findall("worksheets/worksheet")
        if (parsed := _parse_single_worksheet(ws_el, instance_maps, name_to_id_maps, ids)) is not None
    ]
    logger.info("Parsed %d worksheet(s)", len(worksheets))
    return worksheets


def _parse_zone(
    zone_el: etree._Element,
    worksheet_ids_by_name: dict[str, str],
    param_ids_by_name: dict[str, str],
) -> dict[str, Any]:
    """Recursively parse a Tableau dashboard <zone> (percentage-based layout tree) into the spec's
    zone shape, resolving worksheet-name zones back to their worksheet id.

    Tableau typically omits the type='...' attribute entirely for worksheet zones (a zone with a
    name attribute and no type is implicitly a worksheet reference); only container/text/etc. zones
    carry an explicit type. Infer 'worksheet' in that case rather than defaulting to layout-basic.

    Tableau's real XML uses 'paramctrl' and 'bitmap' as raw type strings (not 'parameter'/'image' -
    those are this spec's friendlier aliases), and overloads the zone's 'param' attribute for two
    unrelated purposes depending on context: on a layout-flow container it is the flow direction
    ('horz'/'vert'); on a parameter/filter/legend control it is a '[Parameters].[Name]' reference
    that must resolve to the referenced parameter's field_id, not be treated as a direction.

    Tableau also serializes STANDALONE LEGENDS as zones typed 'color'/'size'/'shape' (each carrying
    name=<owning worksheet>), Web Page objects as type='web' (param=<URL>), and navigation buttons
    as type='dashboard-object' with a <button> child. None of these are containers. Collapsing them
    into 'layout-basic' (the old else-branch) made them INVISIBLE to every downstream consumer while
    they still occupied real canvas - on book_8-1-Dashboards that silently discarded a Web Page
    object filling 62% x 46% of the page plus a 10%-wide rail of three legends and a nav button, so
    the dashboard's whole-page gestalt could not be reconstructed from the spec at all."""
    raw_type = zone_el.get("type")
    has_name = bool(zone_el.get("name"))
    legend_kinds = {"color", "size", "shape"}
    type_aliases = {"paramctrl": "parameter", "bitmap": "image"}
    legend_kind = raw_type if raw_type in legend_kinds else None
    if legend_kind:
        raw_type = "legend"
    elif raw_type == "dashboard-object":
        # A generic dashboard object; the child element says what it actually is.
        raw_type = "button" if zone_el.find("button") is not None else "blank"
    else:
        raw_type = type_aliases.get(raw_type, raw_type)
    if raw_type is None:
        zone_type = "worksheet" if has_name else "layout-basic"
    elif raw_type in (
        "layout-basic",
        "layout-flow",
        "worksheet",
        "text",
        "image",
        "title",
        "filter",
        "parameter",
        "legend",
        "web",
        "button",
        "blank",
    ):
        zone_type = raw_type
    else:
        zone_type = "layout-basic"
    zone: dict[str, Any] = {
        "id": zone_el.get("id", ""),
        "type": zone_type,
        "x": float(zone_el.get("x", 0)),
        "y": float(zone_el.get("y", 0)),
        "w": float(zone_el.get("w", 0)),
        "h": float(zone_el.get("h", 0)),
        "direction": None,
        "legend_kind": legend_kind,
        "url": None,
        "worksheet_id": None,
        "field_id": None,
        "text_html": None,
        "background_color": None,
        "children": [],
    }
    param_attr = zone_el.get("param", "")
    if zone_type == "layout-flow":
        zone["direction"] = {"horz": "horizontal", "vert": "vertical"}.get(param_attr)
    elif zone_type == "web":
        zone["url"] = param_attr or None
    elif zone_type in ("parameter", "filter", "legend") and param_attr:
        # param_attr is often dotted, e.g. '[Parameters].[Insight 1]' - split first to isolate the
        # final bracketed segment, THEN strip its brackets (stripping first would eat into the
        # dotted separator and leave a mangled 'Parameters].[Insight 1' key that never matches).
        zone["field_id"] = param_ids_by_name.get(param_attr.split("].[")[-1].strip("[]"))
    if has_name and zone_type not in ("layout-basic", "layout-flow"):
        zone["worksheet_id"] = worksheet_ids_by_name.get(zone_el.get("name", ""))
    text_el = zone_el.find("formatted-text")
    if text_el is not None:
        zone["text_html"] = "".join(run.text or "" for run in text_el.findall("run"))
    bg = zone_el.find("zone-style/format[@attr='background-color']")
    if bg is not None:
        zone["background_color"] = bg.get("value")
    zone["children"] = [
        _parse_zone(child, worksheet_ids_by_name, param_ids_by_name) for child in zone_el.findall("zone")
    ]
    return zone


_ACTION_TYPE_BY_COMMAND = {"tsc:brush": "highlight", "tsc:filter": "filter"}
_RUN_ON_BY_ACTIVATION = {"on-select": "select", "on-hover": "hover", "on-menu": "menu"}


def _action_type(action: etree._Element) -> str:
    """Classify a dashboard action: a <link> is a URL action; otherwise map the <command> (tsc:brush →
    highlight, tsc:filter → filter, anything mentioning 'parameter' → parameter), defaulting to filter."""
    if action.find("link") is not None:
        return "url"
    command = action.find("command")
    cmd = command.get("command", "") if command is not None else ""
    if "parameter" in cmd:
        return "parameter"
    return _ACTION_TYPE_BY_COMMAND.get(cmd, "filter")


def _parse_actions(root: etree._Element, worksheet_ids_by_name: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Parse workbook <actions>/<action> (filter/highlight/URL/parameter interactivity) and group them
    by their source dashboard name, so each dashboard carries the cross-sheet wiring it drives. Actions
    whose source is a datasource (not a dashboard) are skipped - they can't be attached to one dashboard.
    Precise target-worksheet and driving-field resolution is left to the LLM (best-effort empty here)."""
    by_dashboard: dict[str, list[dict[str, Any]]] = {}
    for action in root.findall(".//actions/action"):
        source = action.find("source")
        dash_name = source.get("dashboard") if source is not None else None
        if not dash_name:
            continue
        activation = action.find("activation")
        activation_type = activation.get("type", "") if activation is not None else ""
        source_ws = source.get("worksheet")
        by_dashboard.setdefault(dash_name, []).append(
            {
                "type": _action_type(action),
                "field_id": None,
                "source_worksheet_id": worksheet_ids_by_name.get(source_ws) if source_ws else None,
                "target_worksheet_ids": [],
                "run_on": _RUN_ON_BY_ACTIVATION.get(activation_type, "select"),
            }
        )
    return by_dashboard


def parse_dashboards(
    root: etree._Element,
    worksheets: list[dict[str, Any]],
    parameters: list[dict[str, Any]],
    ids: IdRegistry,
) -> list[dict[str, Any]]:
    """Parse every <dashboard> in the workbook into its migration-spec representation.

    Tableau dashboards built entirely with 'Floating' containers (every object independently
    absolute-positioned, no 'Tiled' auto-layout) serialize <zones> as N flat sibling <zone> elements
    with no wrapping root container at all - unlike the single-root-zone shape a Tiled-layout
    dashboard produces. Grabbing only the first <zone> (as a naive .find() would) silently drops
    every other object on the dashboard. Detect the flat-multi-child case and synthesize a
    'layout-floating' synthetic root so nothing is lost."""
    worksheet_ids_by_name = {ws["name"]: ws["id"] for ws in worksheets}
    param_ids_by_name = {p["internal_name"].strip("[]"): p["id"] for p in parameters}
    actions_by_dashboard = _parse_actions(root, worksheet_ids_by_name)
    dashboards = []
    for dash_el in root.findall("dashboards/dashboard"):
        name = dash_el.get("name", "")
        size_el = dash_el.find("size")
        top_zones = dash_el.findall("zones/zone")
        if len(top_zones) == 1:
            zones = _parse_zone(top_zones[0], worksheet_ids_by_name, param_ids_by_name)
        elif len(top_zones) > 1:
            zones = {
                "id": "",
                "type": "layout-floating",
                "x": 0.0,
                "y": 0.0,
                "w": 100000.0,
                "h": 100000.0,
                "direction": None,
                "legend_kind": None,
                "url": None,
                "worksheet_id": None,
                "field_id": None,
                "text_html": None,
                "background_color": None,
                "children": [_parse_zone(z, worksheet_ids_by_name, param_ids_by_name) for z in top_zones],
            }
        else:
            # An EMPTY dashboard (Tableau serializes a self-closing <zones/>) is a real, legal
            # authoring state -- a dashboard the author created and never populated. Emitting {}
            # here violates the spec's own zone schema (type/x/y/w/h are required), which failed
            # the WHOLE workbook parse over one empty dashboard. Synthesize a valid empty root so
            # the dashboard survives into the spec (collect_limitations flags it as empty).
            zones = {
                "id": "",
                "type": "layout-basic",
                "x": 0.0,
                "y": 0.0,
                "w": 100000.0,
                "h": 100000.0,
                "direction": None,
                "legend_kind": None,
                "url": None,
                "worksheet_id": None,
                "field_id": None,
                "text_html": None,
                "background_color": None,
                "children": [],
            }
        dashboards.append(
            {
                "id": ids.make("dash", name),
                "name": name,
                "size": {
                    "width": float(size_el.get("maxwidth", 1000)) if size_el is not None else 1000,
                    "height": float(size_el.get("maxheight", 800)) if size_el is not None else 800,
                    "sizing_mode": (size_el.get("sizing-mode", "fixed") if size_el is not None else "automatic"),
                },
                "zones": zones,
                "actions": actions_by_dashboard.get(name, []),
            }
        )
    logger.info("Parsed %d dashboard(s)", len(dashboards))
    return dashboards


def infer_theme(root: etree._Element) -> dict[str, Any]:
    """Best-effort aggregate palette/font from per-worksheet mark-color formats. Tableau has no
    single global theme file, so this is a starting point for design, not an authoritative source."""
    hexes = set()
    for fmt in root.findall(".//format[@attr='mark-color']"):
        value = fmt.get("value", "")
        if value.startswith("#"):
            hexes.add(value)
    fonts = {fmt.get("value") for fmt in root.findall(".//format[@attr='font-family']") if fmt.get("value")}
    return {
        "palette_hexes": sorted(hexes),
        "font_family": sorted(fonts)[0] if fonts else None,
        "background": None,
        "source_note": (
            "Tableau has no single global theme file - aggregated from per-worksheet mark-color "
            "formats; treat as a starting palette, not an authoritative theme to clone."
        ),
    }


def annotate_known_idioms(spec: dict[str, Any]) -> None:
    """Post-process pass: flag the parameter-equality filter idiom so pbi-semantic-builder simplifies
    it to a plain slicer instead of recreating the workaround as a DAX calculated column."""
    field_formulas = {
        f["id"]: f.get("tableau_formula", "")
        for ds in spec["data_sources"]
        for f in ds["fields"]
        if f.get("tableau_formula")
    }
    for ws in spec["worksheets"]:
        for filt in ws["filters"]:
            formula = field_formulas.get(filt["field_id"], "")
            if filt["exclude_nulls"] and _PARAM_EQUALITY_RE.search(formula):
                filt["note"] = (
                    "parameter-equality idiom: field = IF [Param]=[Dim] THEN [Dim] END, filtered to "
                    "exclude null -> collapses to a plain PBI slicer on the dimension, no calculated "
                    "column needed (see docs/tableau-dax-translation-guide.md #2)"
                )


_DATA_TYPE_LIMITATIONS = {
    "table": (
        "low",
        "data_type 'table' is Tableau's internal relationship-model table-anchor pseudo-column "
        "(not real data) - exclude from the semantic model entirely, do not create a column/measure "
        "for it",
    ),
    "spatial": (
        "high",
        "data_type 'spatial' (MAKEPOINT/MAKELINE-derived map geometry) has no native DAX/Power Query "
        "equivalent - requires a custom/ArcGIS visual or reducing to plain lat/long measure columns "
        "with reduced fidelity (e.g. no native origin-destination line rendering)",
    ),
}


def _field_limitations(f: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-field risk checks (LOD/table-calc translation risk, non-tabular data_type values) shared
    by every data source's field loop in collect_limitations."""
    found = []
    if f.get("is_lod") or f.get("is_table_calc"):
        found.append(
            {
                "item": f["id"],
                "issue": f"{'LOD expression' if f.get('is_lod') else 'table calculation'} - verify "
                "DAX translation grain/filter-context against a known Tableau value",
                "severity": "high",
                "stage": "parse",
            }
        )
    if f["data_type"] in _DATA_TYPE_LIMITATIONS:
        severity, issue = _DATA_TYPE_LIMITATIONS[f["data_type"]]
        found.append({"item": f["id"], "issue": issue, "severity": severity, "stage": "parse"})
    return found


def _live_source_limitation(ds: dict[str, Any]) -> dict[str, Any]:
    """The high-severity flag that stops a live source being migrated onto extracted rows.

    High, not medium: pointing the model at a cached copy of a live system produces numbers that
    match on day one and a model that can never refresh - a failure that is invisible exactly when
    someone would catch it.
    """
    conn = ds["connection"]
    server = conn.get("server") or "<server not recorded>"
    database = conn.get("database") or "<database not recorded>"
    cached = " Tableau also packages a .hyper CACHE of these rows." if conn["mode"] == "extract" else ""
    return {
        "item": ds["id"],
        "issue": (
            f"LIVE source ('{conn['class']}' @ {server} / {database}): the Power BI semantic model MUST "
            f"connect to this system directly.{cached} Do NOT migrate the model onto extracted rows/CSVs - "
            "that silently freezes the data at export time and produces a model that can never refresh, "
            "which is not a faithful migration. The .hyper is for SCHEMA DISCOVERY and VALIDATION BASELINES "
            "only (`python scripts/extract_hyper_data.py --schema ...`). ACTION: get the credential for this "
            "system from the user before building "
            "(`python scripts/preflight_source_credentials.py --spec <spec>`)."
        ),
        "severity": "high",
        "stage": "parse",
    }


def _flatten_zones(zone: dict[str, Any]) -> list[dict[str, Any]]:
    """Depth-first flatten of a dashboard zone tree into one list, parents before children.

    Module-level rather than a closure inside the per-dashboard loop: pylint's `cell-var-from-loop`
    is right in principle even when the closure is invoked in the same iteration, and a plain
    function is easier to test than a nested one.
    """
    out = [zone]
    for child in zone.get("children") or []:
        out.extend(_flatten_zones(child))
    return out


def collect_limitations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan the parsed spec for known risk areas (extract-based sources, LOD/table calcs, unresolved
    shelf references) and emit limitations_encountered entries for the honest capabilities writeup."""
    limitations = []
    for ds in spec["data_sources"]:
        published = ds.get("published_datasource")
        if published:
            name = published.get("id") or ds.get("caption") or ds["id"]
            site = published.get("site") or "?"
            key = published.get("key") or "unknown"
            limitations.append(
                {
                    "item": ds["id"],
                    "issue": (
                        f"PUBLISHED Tableau data source ('{name}' on site '{site}', dedup key "
                        f"'{key}'): this workbook only POINTS at a server-side datasource "
                        "(connection class 'sqlproxy'). Its connection details, custom SQL and "
                        "calculated-field formulas live in the published datasource, NOT in this "
                        "workbook, so parsing the .twb alone under-reports them (workbook-local "
                        "calcs built on top of it DO appear here, which makes the gap partial and "
                        "easy to miss). ACTION: export the published data source (.tds/.tdsx) from "
                        "Tableau Server/Cloud and parse it too. NOTE: several workbooks typically "
                        "share ONE published datasource - migrate it ONCE to a single Power BI "
                        "semantic model and bind every downstream report to that model; do not "
                        "rebuild an identical model per workbook. Match on the dedup key above."
                    ),
                    "severity": "high",
                    "stage": "parse",
                }
            )
        target = ds["connection"].get("powerbi_target")
        if target == "live_source":
            limitations.append(_live_source_limitation(ds))
        elif ds["connection"]["mode"] == "extract":
            limitations.append(
                {
                    "item": ds["id"],
                    "issue": "extract-based (.hyper) data source over a FILE original source - row data "
                    "requires a separate extraction step; pointing the model at the extracted rows is "
                    "faithful here because there is no upstream system to connect to",
                    "severity": "medium",
                    "stage": "parse",
                }
            )
        for f in ds["fields"]:
            limitations.extend(_field_limitations(f))
    limitations.extend(_worksheet_limitations(spec))
    limitations.extend(_dashboard_limitations(spec))
    return limitations


def _worksheet_limitations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Worksheet-level limitations: unresolved shelf/mark references, forecast shelves, and the
    Measure Names/Values pivot.

    Split out of `collect_limitations` for the same reason as `_dashboard_limitations`: three
    independent scans in one function pushed it past pylint's locals/branch thresholds.
    """
    limitations: list[dict[str, Any]] = []
    for ws in spec["worksheets"]:
        pivot = ws.get("measure_names_values_pivot")
        for enc_name in ("rows", "columns"):
            for shelf_field in ws["encodings"].get(enc_name) or []:
                if not isinstance(shelf_field, dict):
                    continue
                field_id = str(shelf_field.get("field_id", ""))
                if not field_id.startswith("UNRESOLVED:"):
                    continue
                if pivot is not None and ("Measure Names" in field_id or "Multiple Values" in field_id):
                    continue  # covered by the dedicated measure_names_values_pivot entry below instead
                limitations.append(
                    {
                        "item": ws["id"],
                        "issue": f"could not resolve shelf reference {field_id}",
                        "severity": "low",
                        "stage": "parse",
                    }
                )
        # Mark encodings (color/size/shape/detail/label/tooltip) were never scanned for unresolved
        # references, so a dropped MARK field - which changes what the chart actually shows - was
        # silently absent from limitations while a dropped ROW field was reported. Scan them too.
        for enc_name in ("color", "size", "shape", "label", "detail", "tooltip"):
            enc_val = ws["encodings"].get(enc_name)
            for enc in enc_val if isinstance(enc_val, list) else [enc_val]:
                if not isinstance(enc, dict):
                    continue
                field_id = str(enc.get("field_id", ""))
                if not field_id.startswith("UNRESOLVED:"):
                    continue
                limitations.append(
                    {
                        "item": ws["id"],
                        "issue": (
                            f"could not resolve the {enc_name.upper()} mark encoding {field_id} - the "
                            "chart will render WITHOUT this encoding, which changes what it shows"
                        ),
                        "severity": "medium",
                        "stage": "parse",
                    }
                )
        # Tableau's built-in Forecast (Analytics pane) synthesizes columns that exist in NO data
        # source: a 'fVal:' forecast value and a nominal 'Forecast Indicator' (Actual/Estimate).
        # Downstream tooling reports these as ordinary unresolved fields and advises "bind it to the
        # matching model column" - there IS no such column, and never will be, so that remediation
        # sends a builder hunting for something that cannot exist. Name the real capability gap.
        forecast_shelves = [
            shelf_field
            for enc_name in ("rows", "columns")
            for shelf_field in ws["encodings"].get(enc_name) or []
            if isinstance(shelf_field, dict) and str(shelf_field.get("derivation") or "").startswith("fVal")
        ]
        if forecast_shelves:
            limitations.append(
                {
                    "item": ws["id"],
                    "issue": (
                        f"worksheet '{ws['name']}' uses TABLEAU'S BUILT-IN FORECAST (Analytics pane): the "
                        "shelf carries a synthesized 'fVal' forecast value and the marks are coloured by "
                        "the generated 'Forecast Indicator' (Actual vs Estimate). Both are produced by "
                        "Tableau's exponential-smoothing model at render time and exist in NO data source, "
                        "so there is nothing to bind them to. Power BI's nearest native equivalent is the "
                        "analytics-pane Forecast line (line charts only, and it does not split marks into "
                        "Actual/Estimate). FAITHFUL options: (a) implement the forecast in DAX/Power Query "
                        "and materialise an Actual/Estimate flag, or (b) ship actuals only and log the gap. "
                        "Do NOT treat this as a broken field binding"
                    ),
                    "severity": "high",
                    "stage": "parse",
                }
            )
        if pivot is not None:
            limitations.append(
                {
                    "item": ws["id"],
                    "issue": f"{pivot['note']} (axis: {pivot['axis']}, resolved fields: "
                    f"{len(pivot['pivoted_field_ids'])})",
                    "severity": "medium" if pivot["pivoted_field_ids"] else "high",
                    "stage": "parse",
                }
            )
    limitations.extend(_dashboard_limitations(spec))
    return limitations


def _dashboard_limitations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Dashboard-layout limitations: empty dashboards, and zone types Power BI cannot re-create.

    Split out of `collect_limitations` so that function stays under pylint's locals/branch
    thresholds - the dashboard scan is independent of the data-source and worksheet scans.
    """
    limitations: list[dict[str, Any]] = []
    for dash in spec.get("dashboards", []):
        zones = dash.get("zones") or {}
        if not zones.get("children") and not zones.get("worksheet_id"):
            limitations.append(
                {
                    "item": dash["id"],
                    "issue": (
                        f"dashboard '{dash['name']}' is EMPTY in the Tableau source (<zones/> carries no "
                        "objects at all) - there is nothing to lay out, so no Power BI page is owed for "
                        "it. A downstream 'no supported visuals on this dashboard' warning about this "
                        "dashboard is FAITHFUL, not a migration defect"
                    ),
                    "severity": "low",
                    "stage": "parse",
                }
            )
        flat = _flatten_zones(zones)
        canvas = (zones.get("w") or 0) * (zones.get("h") or 0)
        for z in flat:
            if z["type"] == "web":
                share = ((z["w"] * z["h"]) / canvas * 100) if canvas else 0
                limitations.append(
                    {
                        "item": dash["id"],
                        "issue": (
                            f"dashboard '{dash['name']}' embeds a Tableau WEB PAGE object "
                            f"(url: {z.get('url') or 'unknown'}) occupying ~{share:.0f}% of the canvas. "
                            "Power BI has no native web-embed visual in a PBIR report (only an "
                            "AppSource custom visual), so this object cannot be re-created faithfully; "
                            "the canvas space it occupies must still be accounted for or the page's "
                            "proportions will not match the source"
                        ),
                        "severity": "medium",
                        "stage": "parse",
                    }
                )
            elif z["type"] == "legend":
                limitations.append(
                    {
                        "item": dash["id"],
                        "issue": (
                            f"dashboard '{dash['name']}' places a STANDALONE {z.get('legend_kind') or ''} "
                            f"legend (for worksheet id {z.get('worksheet_id')}) as its own dashboard object "
                            "occupying real canvas space. Power BI has no standalone legend object - a "
                            "legend is a property of its visual - so the faithful translation is to enable "
                            "that visual's own legend and reclaim/reserve the rail space deliberately"
                        ),
                        "severity": "low",
                        "stage": "parse",
                    }
                )
            elif z["type"] == "button":
                limitations.append(
                    {
                        "item": dash["id"],
                        "issue": (
                            f"dashboard '{dash['name']}' contains a Tableau navigation button "
                            "(goto-sheet). Power BI's equivalent is a button with a page-navigation "
                            "action; verify the target page exists in the migrated report"
                        ),
                        "severity": "low",
                        "stage": "parse",
                    }
                )
    return limitations


def _get_repository_location_id(root: etree._Element) -> str | None:
    """Return the workbook's Tableau Public repository-location id, if present."""
    repo_el = root.find("repository-location")
    return repo_el.get("id") if repo_el is not None else None


def parse_workbook(path: Path) -> dict[str, Any]:
    """Top-level entry point: parse a .twb/.twbx file into a complete migration-spec dict."""
    ids = IdRegistry()
    root, hyper_files = load_twb_root(path)

    parameters = parse_parameters(root, ids)
    data_sources, instance_maps, name_to_id_maps = parse_data_sources(root, hyper_files, ids)
    worksheets = parse_worksheets(root, instance_maps, name_to_id_maps, ids)
    dashboards = parse_dashboards(root, worksheets, parameters, ids)
    theme = infer_theme(root)

    spec: dict[str, Any] = {
        "migration_spec_version": SPEC_VERSION,
        "source": {
            "file_name": path.name,
            "tableau_version": root.get("version"),
            "source_build": root.get("source-build"),
            "repository_location_id": _get_repository_location_id(root),
            "parsed_at": datetime.now(timezone.utc).isoformat(),
        },
        "parameters": parameters,
        "data_sources": data_sources,
        "worksheets": worksheets,
        "dashboards": dashboards,
        "theme": theme,
        "limitations_encountered": [],
    }
    annotate_known_idioms(spec)
    spec["limitations_encountered"] = collect_limitations(spec)
    return spec


def validate_spec(spec: dict[str, Any], schema_path: Path) -> None:
    """Validate the spec against migration-spec.schema.json; skips gracefully if jsonschema isn't
    installed, since schema validation is a safety net, not a hard runtime dependency."""
    try:
        import jsonschema  # pylint: disable=import-outside-toplevel
    except ImportError:
        logger.warning("jsonschema not installed - skipping validation")
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(spec, schema)
    logger.info("migration-spec.json validated against schema")


def main() -> None:
    """CLI entry point: parse the workbook given on the command line and write migration-spec.json."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="Path to a .twb/.twbx workbook or .tds/.tdsx data source")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output migration-spec.json path")
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs" / "migration-spec.schema.json",
        help="Path to migration-spec.schema.json for validation",
    )
    args = parser.parse_args()

    logger.info("Parsing %s", args.workbook)
    spec = parse_workbook(args.workbook)
    validate_spec(spec, args.schema)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "Wrote %s (%d data sources, %d worksheets, %d dashboards, %d limitations flagged)",
        args.output,
        len(spec["data_sources"]),
        len(spec["worksheets"]),
        len(spec["dashboards"]),
        len(spec["limitations_encountered"]),
    )
    # Arm the live-source credential gate here, at the earliest moment a live source is known and
    # before any builder runs. Measured: an agent built the model 95s BEFORE invoking the gate when
    # arming was a later workflow step. Idempotent, no-op for extract-only workbooks, never fatal.
    try:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "preflight_source_credentials.py"),
                "--spec",
                str(args.output),
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not arm the credential gate: %s", exc)


if __name__ == "__main__":
    main()
