"""
purpose: Parse a Tableau workbook (.twb / .twbx) into migration-spec.json, the normalized
         intermediate representation consumed by the pbi-semantic-builder and pbi-report-builder
         subagents. See docs/migration-spec.md for the schema and design rationale.
usage:   python scripts/parse_tableau.py <workbook.twb|workbook.twbx> -o <migration-spec.json>
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lxml import etree

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("parse_tableau")

SPEC_VERSION = "1.0"

_LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)
_TABLE_CALC_RE = re.compile(r"\b(WINDOW_\w+|RUNNING_\w+|INDEX|RANK\w*|LOOKUP|TOTAL|PREVIOUS_VALUE)\s*\(", re.IGNORECASE)
_PARAM_EQUALITY_RE = re.compile(r"if\s*\[Parameters\]\.\[[^\]]+\]\s*=\s*\[[^\]]+\]\s*then", re.IGNORECASE)
_BRACKET_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")
_SHELF_FIELD_RE = re.compile(r"\[([^\[\]]+)\]\.\[([^\[\]]+)\]")


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


def load_twb_root(path: Path) -> tuple[etree._Element, dict[str, str]]:
    """Return the parsed .twb XML root, plus a map of hyper-file relative paths found in the archive
    (empty if the input is a plain .twb with no packaged extracts)."""
    hyper_files: dict[str, str] = {}
    if path.suffix.lower() == ".twbx":
        with zipfile.ZipFile(path) as zf:
            twb_names = [n for n in zf.namelist() if n.lower().endswith(".twb")]
            if not twb_names:
                raise ValueError(f"No .twb found inside {path}")
            xml_bytes = zf.read(twb_names[0])
            hyper_files = {Path(n).name: n for n in zf.namelist() if n.lower().endswith(".hyper")}
    else:
        xml_bytes = path.read_bytes()
    root = etree.fromstring(xml_bytes)
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


def _parse_connection(ds_el: etree._Element, hyper_files: dict[str, str]) -> dict[str, Any]:
    """Resolve both the *original* source connection (e.g. excel-direct, sqlserver) and whether the
    datasource runs off a packaged .hyper extract (Tableau Public workbooks always do)."""
    outer_conn = ds_el.find("connection")
    connection: dict[str, Any] = {"class": "unknown", "mode": "live", "server": None, "database": None, "note": None}
    if outer_conn is None:
        return connection

    named_conn = outer_conn.find(".//named-connections/named-connection/connection")
    if named_conn is not None:
        connection["class"] = named_conn.get("class", "unknown")
        connection["server"] = named_conn.get("server")
        connection["database"] = named_conn.get("dbname")
    elif outer_conn.get("class") not in (None, "federated"):
        connection["class"] = outer_conn.get("class", "unknown")
        connection["server"] = outer_conn.get("server")
        connection["database"] = outer_conn.get("dbname")

    extract_conn = ds_el.find(".//extract/connection")
    if extract_conn is not None:
        connection["mode"] = "extract"
        hyper_name = Path(extract_conn.get("dbname", "")).name
        connection["hyper_file"] = hyper_files.get(hyper_name, extract_conn.get("dbname"))
        connection["note"] = (
            f"extract-based - original logical source was '{connection['class']}'; "
            "actual rows come from the packaged .hyper file, not a live connection"
        )
    return connection


_CONTAINER_RELATION_TYPES = {"collection", "join", "union"}


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
        "is_lod": bool(_LOD_RE.search(formula)),
        "is_table_calc": bool(_TABLE_CALC_RE.search(formula)),
        "reshape_hint": reshape_hint,
    }


def _build_field_entry(col: etree._Element, ds_id: str, table_id: str | None, ids: IdRegistry) -> dict[str, Any]:
    """Build one field entry dict from a <column> element (base column or calculated field)."""
    internal_name = col.get("name", "")
    caption = col.get("caption") or internal_name.strip("[]")
    calc_el = col.find("calculation")
    formula = calc_el.get("formula") if calc_el is not None else None
    aliases = {a.get("key", "").strip('"'): a.get("value", "") for a in col.findall("aliases/alias")}

    entry: dict[str, Any] = {
        "id": ids.make("fld", ds_id, caption),
        "internal_name": internal_name,
        "caption": caption,
        "table_id": table_id,
        "kind": "calculated" if formula is not None else "column",
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

    data_source = {
        "id": ds_id,
        "caption": caption,
        "internal_name": internal_name,
        "connection": _parse_connection(ds_el, hyper_files),
        "tables": tables,
        "joins": _parse_joins(ds_el),
        "fields": fields,
    }
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
    return "".join(run.text or "" for run in container.findall("run"))


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
    that must resolve to the referenced parameter's field_id, not be treated as a direction."""
    raw_type = zone_el.get("type")
    has_name = bool(zone_el.get("name"))
    type_aliases = {"paramctrl": "parameter", "bitmap": "image"}
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
        "worksheet_id": None,
        "field_id": None,
        "text_html": None,
        "background_color": None,
        "children": [],
    }
    param_attr = zone_el.get("param", "")
    if zone_type == "layout-flow":
        zone["direction"] = {"horz": "horizontal", "vert": "vertical"}.get(param_attr)
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
                "worksheet_id": None,
                "field_id": None,
                "text_html": None,
                "background_color": None,
                "children": [_parse_zone(z, worksheet_ids_by_name, param_ids_by_name) for z in top_zones],
            }
        else:
            zones = {}
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


def collect_limitations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan the parsed spec for known risk areas (extract-based sources, LOD/table calcs, unresolved
    shelf references) and emit limitations_encountered entries for the honest capabilities writeup."""
    limitations = []
    for ds in spec["data_sources"]:
        if ds["connection"]["mode"] == "extract":
            limitations.append(
                {
                    "item": ds["id"],
                    "issue": "extract-based (.hyper) data source - row data requires a separate "
                    "extraction step (tableauhyperapi -> Parquet) or repointing to the true upstream "
                    "system",
                    "severity": "medium",
                    "stage": "parse",
                }
            )
        for f in ds["fields"]:
            limitations.extend(_field_limitations(f))
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
    parser.add_argument("workbook", type=Path, help="Path to a .twb or .twbx file")
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


if __name__ == "__main__":
    main()
