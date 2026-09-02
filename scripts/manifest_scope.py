"""
purpose: the ONE allowlist mechanism every shipped handover manifest is projected through.
usage:   library only - imported by scripts/package_unit.py; there is no CLI.

Extracted from `package_unit.py` so that "one mechanism" is structurally true rather than merely
asserted, and so the declarative surface of a handover package - what a customer deliverable may
contain, at every level - can be read in one place. The reasoning that produced it is below.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------------------------
# ONE allowlist mechanism, applied to EVERY shipped manifest at EVERY level
#
# Round-1 review found a denylist leaking the estate out of `report.json` and it was replaced with an
# allowlist - but only there, and only at the top level. Round-2 review found the SAME CLASS still
# open in three more places: the retained `workbooks[]` / `definition_of_done.workbooks[]` ROWS were
# copied wholesale (so unknown nested fields shipped automatically), and `oracle-manifest.json` and
# `engine-output-receipt.json` were still denylists in their own right. That is the failure this
# repo has measured before - `docs/review-throughput-postmortem.md`: **66% of round-2+ findings were
# a known class rediscovered one site at a time.**
#
# So there is now exactly one projector and one spec vocabulary, and every shipped manifest declares
# its surface through it. A field nobody enumerated is DROPPED at whatever depth it appears, and its
# full path is recorded, so the omission is discoverable instead of silent.
# --------------------------------------------------------------------------------------------


class Rows:  # pylint: disable=too-few-public-methods
    """Spec marker: a list whose object entries are each projected onto ``spec``."""

    __slots__ = ("spec",)

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec


#: Spec leaf: carry this value verbatim, whatever it is. Only ever used on values that cannot name
#: another unit - scalars, or per-file facts about a file inside THIS package.
KEEP = "keep-verbatim"


def _fields(*names: str) -> dict[str, Any]:
    """An allowlist level carrying the named fields verbatim and dropping everything else."""
    return {name: KEEP for name in names}


def project(payload: Any, spec: Any, *, prefix: str = "") -> tuple[Any, list[str]]:
    """`(projected value, dropped paths)` - carry ONLY what ``spec`` names, at EVERY level.

    The recursion is the point. `{"workbooks": Rows({"name": KEEP})}` drops an unenumerated
    `workbooks[].future_nested` and reports it under that path, where a top-level-only allowlist
    would have carried the whole row and reported nothing.

    Dropped paths are de-duplicated and use `[]` for a row position, so 48 rows carrying the same
    unknown field report one `workbooks[].future_nested` rather than 48 indexed near-duplicates.

    A spec/value type mismatch drops the value rather than guessing - a `Rows` spec meeting a dict,
    or a mapping spec meeting a string, means the manifest is not the shape we enumerated, and
    carrying it anyway is how the estate got out the first time.
    """
    if spec is KEEP:
        return payload, []
    if isinstance(spec, Rows):
        if not isinstance(payload, list):
            return [], [prefix or "."]
        kept_rows: list[Any] = []
        dropped: list[str] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                dropped.append(f"{prefix}[{index}]")
                continue
            value, lost = project(entry, spec.spec, prefix=f"{prefix}[]")
            kept_rows.append(value)
            dropped.extend(lost)
        return kept_rows, sorted(set(dropped))
    if isinstance(spec, dict):
        if not isinstance(payload, dict):
            return {}, [prefix or "."]
        kept: dict[str, Any] = {}
        dropped = []
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in spec:
                dropped.append(path)
                continue
            projected, lost = project(value, spec[key], prefix=path)
            kept[key] = projected
            dropped.extend(lost)
        return kept, sorted(set(dropped))
    raise TypeError(f"unusable allowlist spec at {prefix or '.'}: {spec!r}")


#: A `report.json` workbook row, measured on `_runs/407-dryrun-gates/bundle` (N=29). Every one was
#: checked for foreign content before being approved: none carries another unit's name as an exact
#: scalar, and none carries an absolute host path.
REPORT_WORKBOOK_ROW = _fields(
    "binding_signal",
    "bound_datasource",
    "bound_model",
    "colour_twins_retired",
    "column_prune",
    "column_rebind",
    "date_rebind",
    "embedded_datasources",
    "field_rebind",
    "flatfile_data",
    "measure_filters_needs_review",
    "measure_rebind",
    "model_facts",
    "model_translation_handoff",
    "name",
    "note",
    "openability_selfcheck",
    "output_folder",
    "pbip_folder",
    "pbip_page_count",
    "pbip_ref_drops",
    "pbip_status",
    "pbip_warnings",
    "remediation_worklist",
    "source_id",
    "visual_calculations",
    "viz_fidelity",
    "viz_implicit_row_count",
    "viz_status",
)
#: A `report.json` datasource row, measured the same way (N=29).
REPORT_DATASOURCE_ROW = _fields(
    "calc_columns",
    "calc_columns_stubbed",
    "calc_columns_translated",
    "column_count",
    "column_prune",
    "connector",
    "dim_calcs",
    "flatfile_data",
    "flatfile_landed",
    "fully_supported",
    "m_connector",
    "manual_followups",
    "measures",
    "measures_stubbed",
    "measures_translated",
    "name",
    "output_folder",
    "partitions_needs_review",
    "partitions_stubbed",
    "pbip_folder",
    "skipped_calcs",
    "skipped_tables",
    "source_id",
    "status",
    "storage_decision",
    "storage_mode",
    "table_count",
    "tables",
    "translation_handoff",
)
#: A `definition_of_done.workbooks[]` row (N=6).
REPORT_DOD_ROW = _fields("workbook", "status", "reason", "report_bound", "bound_model", "pbip_folder")

#: `report.json`. `workbooks`/`datasources` ARE the gate surface: both
#: `check_reference_readiness._engine_report` and `check_unit._is_engine_report` reject the file
#: unless `workbooks` is a list, and `._unit_names` reads the names of both. `definition_of_done`
#: keeps only `applicable` and this unit's own row - the estate's `status`/`reports_*` counters are
#: dropped because `status: "failed"` is the ESTATE's verdict (18 of 48 reports failed) and in a
#: one-unit package it reads as this unit's.
REPORT_ALLOW: dict[str, Any] = {
    "tool": KEEP,
    "generated_at": KEEP,
    "workbooks": Rows(REPORT_WORKBOOK_ROW),
    "datasources": Rows(REPORT_DATASOURCE_ROW),
    "definition_of_done": {"applicable": KEEP, "workbooks": Rows(REPORT_DOD_ROW)},
}
#: The two collections filtered to this unit before projection. Always emitted, always lists.
REPORT_UNIT_LISTS = ("workbooks", "datasources")

#: `engine-output-receipt.json`. `engine.version` is the only field any consumer reads
#: (`check_engine_receipts.py:33-35`). `engine.root`/`engine.plugin_root` are dropped because they
#: are absolute `C:\Users\<user>\...` installation paths - engine provenance is a VERSION, not a
#: location on the machine that happened to build it. `report_sha256`/`input_manifest_sha256` are
#: dropped because they hash ESTATE files that are not in the package, so they are a false claim
#: about what sits beside them; `credential_gate._receipt_matches_bundle` still fails closed, and
#: earlier than before - it raises OSError on the absent `input_manifest.json` before reading either.
RECEIPT_ALLOW: dict[str, Any] = {
    "version": KEEP,
    "created_at": KEEP,
    "engine": _fields("version", "source", "canonical"),
    "artifacts": Rows(_fields("path", "sha256", "size")),
}

#: One view inside `oracle-manifest.json` (N=13, plus the stem this packager adds). The four leg
#: objects are carried verbatim by design: `_copy_leg` builds each from the capture's own record for
#: THIS file and `reference_evidence.render_facts` verifies the `sha256`/`bytes`/dimensions it finds
#: there, so projecting them risks breaking verification to close a channel that carries per-file
#: facts about a file already inside this package. Named as a residual rather than left unsaid.
ORACLE_VIEW_ALLOW: dict[str, Any] = {
    **_fields(
        "view_luid",
        "view_name",
        "view_url_name",
        "view_type",
        "content_url",
        "project",
        "workbook_luid",
        "workbook_name",
        "captured_at",
        "updated_at",
        "packaged_object_stem",
    ),
    **{leg: KEEP for leg in ("image", "svg", "pdf", "data")},
}

#: `oracle-manifest.json`. Everything counting the ESTATE RUN is dropped and RECOMPUTED from the
#: packaged views (see `_scope_oracle_manifest`); everything identifying another unit is dropped
#: outright. `render_capability.probe_view_luid`/`probe_view_name`/`probe_view_luids` name the view
#: the capability ladder was probed against, which on the reference estate is `'Overview'` in the
#: FOREIGN `Superstore` workbook; `warnings` is free text that can quote that same view, so it goes
#: too. What survives is what tells a consumer which GRADE of evidence it got.
ORACLE_MANIFEST_ALLOW: dict[str, Any] = {
    **_fields("schema", "captured_at", "server", "site", "rest_api_version", "requested_renders"),
    "render_capability": _fields(
        "configured_api_version",
        "advertised_api_version",
        "selected_api_version",
        "selected_tier",
        "provisional",
        "capability_complete",
        "server",
        "tiers",
    ),
    "views": Rows(ORACLE_VIEW_ALLOW),
}
