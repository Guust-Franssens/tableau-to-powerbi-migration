"""
purpose: Decide what a migrated Power BI semantic model should CONNECT TO, from a Tableau
         connection class. Shared by the parser (which stamps the decision onto migration-spec.json)
         and the Hyper extractor (which refuses to silently do the wrong thing).
usage:   from connection_target import powerbi_target, FLAT_FILE_CLASSES

Why this is its own module
--------------------------
This is the single most consequential mapping decision in a migration, and Tableau's metadata does
not answer it directly: a packaged `.hyper` extract looks *identical* whether it caches a CSV or a
Snowflake warehouse. Getting it wrong is invisible on day one - the numbers match, because they were
copied - and only surfaces when the customer's first refresh fails or the data is silently stale.
"""

from __future__ import annotations

# Tableau connection classes whose ORIGINAL source really is a file. For these, materialising the
# rows and pointing Power BI at them is a faithful migration - there is no upstream system to
# connect to. Everything NOT in this set is treated as a live system (database, warehouse, API,
# cloud app), because under-connecting is a far worse failure than over-asking for a credential.
FLAT_FILE_CLASSES = frozenset(
    {
        "excel-direct",
        "excel",
        "textscan",
        "csv",
        "json",
        "jsonfile",
        # Tableau's OGR/GDAL spatial-file connectors (e.g. packaged ESRI shapefiles inside a .twbx).
        "ogr",
        "ogrdirect",
        "parquet",
        "spatial",
        "statfile",
        "msaccess",
        "cubefile",
    }
)

LIVE_SOURCE = "live_source"
FLAT_FILE = "flat_file"
UNKNOWN = "unknown"


def powerbi_target(connection_class: str, mode: str) -> tuple[str, str]:
    """Return (target, reason) for one Tableau connection.

      * `flat_file`   - the source IS a file, so extracting the rows and pointing at them is faithful.
      * `live_source` - the `.hyper` is only Tableau's CACHE of an upstream system. Migrating the
        cache produces a model that can never refresh and silently freezes the data at export time.
        Power BI must connect to the SAME upstream (Snowflake, SQL Server, ...) that Tableau did.
      * `unknown`     - could not resolve the class; ask rather than guess.

    `mode == "extract"` deliberately does NOT change the answer for a live class - that is exactly
    the case that looks like a flat file and isn't.
    """
    if connection_class in FLAT_FILE_CLASSES:
        return (
            FLAT_FILE,
            f"'{connection_class}' is a FILE source. Extract the rows and point the semantic model at "
            "them - there is no upstream system to connect to.",
        )
    if connection_class in (UNKNOWN, "", None):
        return (
            UNKNOWN,
            "Could not resolve the original source class. Determine it before building: if it is a "
            "live system the model must connect to it, NOT to extracted rows.",
        )
    cache_warning = (
        "; the packaged .hyper is only Tableau's cache and is for schema discovery and validation "
        "baselines ONLY - do NOT migrate the model onto extracted rows, that yields a model that "
        "can never refresh."
        if mode == "extract"
        else "."
    )
    return (
        LIVE_SOURCE,
        f"'{connection_class}' is a LIVE system. The semantic model MUST connect to it directly{cache_warning}",
    )
