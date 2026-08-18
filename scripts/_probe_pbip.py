"""PBIP scaffold writers for the throwaway one-table probe model.

Extracted from ``probe_live_source`` following that module's own documented module-size strategy
(pylint ``max-module-lines = 1200``): SPLIT, not waive. ``_verdict_lines.py`` was the first
extraction; this is the second and last one it named, taken when the custom-SQL probe path needed
room. These are pure, Desktop-independent string/JSON builders with no network and no subprocess:
given a table, a column and an M query they return the complete set of relative paths and file
bodies that Power BI Desktop needs to open a minimal PBIP.

``probe_live_source`` imports these names, so they stay reachable as
``probe_live_source._tmdl_ident`` etc. The seam tests in ``tests/test_probe_live_tmdl_quoting.py``
reach them that way, which makes the re-export load-bearing - do not "tidy" it into a private alias.
"""

from __future__ import annotations

import json
import uuid

_SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
)
# `definition.pbir` sits one level ABOVE `definition/`, so it does not share `_SCHEMA_BASE`, and it
# is NOT optional: omitting it is `PBIR_JSON_FILE_NO_SCHEMA` ("Fabric rejects PBIR definition JSON
# files without $schema"). Value copied from the committed deliverable
# `examples/shipping-kpis/fabric/ShippingKPIs.Report/definition.pbir`.
_PBIR_PROPERTIES_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"
)
# `.pbip` / `.pbism` are project-level, not PBIR, so they have their own schema roots again. The
# `.pbip` one must end in a LITERAL numeric version, never the placeholder `1.x.x`
# (`.github/skills/powerbi-semantic-model-gotchas/SKILL.md`).
_PBIP_PROPERTIES_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json"
_PBISM_PROPERTIES_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json"
)


# TMDL reserves the single quote as its identifier delimiter: an identifier containing a space (or
# most punctuation) MUST be quoted or Power BI Desktop refuses the model with InvalidObjectHeader.
# The deterministic engine names unresolved custom-SQL relations `Custom SQL Query`, so the un-quoted
# probe crashed on most real estates. Quote UNCONDITIONALLY - a quoted identifier is always valid
# TMDL even where a bare one would be legal, which kills the "which character forces quoting?" guess.
# Ground truth (Power BI's own serializer): examples/wind-energy-utilization/.../CO2 Savings.tmdl
# `table 'CO2 Savings'` and model.tmdl `ref table 'CO2 Savings'`; an embedded quote is doubled, per
# examples/broadway-stage-to-screen/.../1 Films.tmdl `column 'Sondheim''s Work'`.
def _tmdl_ident(name: str) -> str:
    """Quote `name` as a TMDL identifier: single quotes, doubling any embedded quote."""
    return "'" + name.replace("'", "''") + "'"


# A table's TMDL identifier and its FILENAME are different strings. Spaces are legal in a Windows
# filename but `< > : " / \ | ? *` and control chars are not - a source table like `dbo:staging`
# would yield an unwritable path (a separate failure mode). Sanitise only the filename; the `table`
# header keeps the real quoted name, and TOM matches tables by header content, not by filename.
def _tmdl_filename_stem(name: str) -> str:
    """A non-empty, Windows-safe filename stem for `name` (its identifier stays separate)."""
    stem = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
    return stem.rstrip(" .") or "probe_table"


def _pbip_files(name: str, m_query: str, table: str, column: str) -> dict[str, str]:
    """The minimum PBIP that Power BI Desktop will open: one table, one column, one partition."""
    indented = "\n".join("\t\t\t\t" + line for line in m_query.split("\n"))
    table_ident = _tmdl_ident(table)
    column_ident = _tmdl_ident(column)
    table_stem = _tmdl_filename_stem(table)
    return {
        f"{name}.pbip": json.dumps(
            {
                "$schema": _PBIP_PROPERTIES_SCHEMA,
                "version": "1.0",
                "artifacts": [{"report": {"path": f"{name}.Report"}}],
            },
            indent=2,
        ),
        f"{name}.SemanticModel/.platform": json.dumps(
            {
                "$schema": _PLATFORM_SCHEMA,
                "metadata": {"type": "SemanticModel", "displayName": name},
                "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
            },
            indent=2,
        ),
        # 4.2, matching all 16 shipped examples under `examples/*/fabric/*.SemanticModel/`. 4.0 is
        # an older project version this repo no longer produces anywhere else.
        f"{name}.SemanticModel/definition.pbism": json.dumps(
            {"$schema": _PBISM_PROPERTIES_SCHEMA, "version": "4.2", "settings": {}},
            indent=2,
        ),
        f"{name}.SemanticModel/definition/database.tmdl": (
            # 1702, never lower. Measured 2026-08-03 (a real Power BI Desktop crash, "Frown"
            # feedback): TOM refuses to load a model that requests a LOWER compatibilityLevel than
            # whatever Desktop's current AS instance already has cached ("Tabular databases do not
            # support CompatibilityLevel downgrade"). This template used 1567 - a value that does
            # not appear ANYWHERE else in this repo's real migrations, and is lower even than the
            # 1606 that triggered the crash. This repo's own documented convention (superstore-
            # sales-performance/migration-spec.json) is 1702+ for newly created models; matching it
            # here means the probe's throwaway model can only ever be requesting an UPGRADE
            # relative to whatever baseline Desktop already initialized, never a downgrade.
            "database\n\tcompatibilityLevel: 1702\n"
        ),
        f"{name}.SemanticModel/definition/model.tmdl": (
            "model Model\n"
            "\tculture: en-US\n"
            "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
            "\tsourceQueryCulture: en-US\n\n"
            f"ref table {table_ident}\n"
        ),
        f"{name}.SemanticModel/definition/tables/{table_stem}.tmdl": (
            f"table {table_ident}\n\n"
            f"\tcolumn {column_ident}\n"
            "\t\tdataType: string\n"
            f"\t\tlineageTag: {uuid.uuid4()}\n"
            "\t\tsummarizeBy: none\n"
            f"\t\tsourceColumn: {column}\n\n"
            f"\tpartition {table_ident} = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            f"{indented}\n"
        ),
        f"{name}.Report/.platform": json.dumps(
            {
                "$schema": _PLATFORM_SCHEMA,
                "metadata": {"type": "Report", "displayName": name},
                "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
            },
            indent=2,
        ),
        f"{name}.Report/definition.pbir": json.dumps(
            {
                "$schema": _PBIR_PROPERTIES_SCHEMA,
                "version": "4.0",
                "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}},
            },
            indent=2,
        ),
        f"{name}.Report/definition/version.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "versionMetadata/1.0.0/schema.json",
                # Three-part and MUST match `^[1-9][0-9]*\.(0|[1-9][0-9]*)\.0$`, so the two-part
                # "4.0" this used to carry is a schema error, not a variant spelling. This is the
                # PBIR *definition* version (2.0.0 in every shipped example), which is a different
                # number from `definition.pbir`'s project `version` above - do not sync them.
                "version": "2.0.0",
            },
            indent=2,
        ),
        f"{name}.Report/definition/report.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "report/1.0.0/schema.json",
                # `reportVersionAtImport` is LOCATION-DEPENDENT, and this scaffold is the place the
                # confusion already cost us once. It is FORBIDDEN here at the top level (`/ must NOT
                # have additional properties`) and REQUIRED inside each `themeCollection` entry
                # (`PBIR_THEME_VERSION_AT_IMPORT_MISSING`). The probe registers no theme at all, so
                # there is no entry to carry it and the correct scaffold has it nowhere. If you ever
                # add a `baseTheme`/`customTheme` here, that entry must carry its own
                # `reportVersionAtImport` - see `examples/shipping-kpis/.../definition/report.json`.
                "themeCollection": {},
                "layoutOptimization": "None",
                "resourcePackages": [],
            },
            indent=2,
        ),
        f"{name}.Report/definition/pages/pages.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "pagesMetadata/1.0.0/schema.json",
                "pageOrder": ["p"],
                "activePageName": "p",
            },
            indent=2,
        ),
        f"{name}.Report/definition/pages/p/page.json": json.dumps(
            {
                "$schema": _SCHEMA_BASE + "page/1.0.0/schema.json",
                "name": "p",
                "displayName": "Probe",
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280,
            },
            indent=2,
        ),
    }
