"""
purpose: generate a .twbx whose single federated datasource spans THREE different live systems
         (Databricks + Snowflake + Azure SQL), for testing the multi-source path end to end:
         parser -> connections[] -> credential gate -> deterministic tier -> probe -> Desktop.
         Real Tableau workbooks federate across systems routinely, and that path has its own
         failure modes (per-connector M parameters, the Privacy Levels modal) that a single-source
         fixture cannot reach.
usage:   python scripts/make_live_source_fixture.py [-o tests/fixtures/live/multi-source-live.twbx]

Endpoints come from the environment so this script carries no site-specific hostnames and stays
committable; the GENERATED workbook does contain them, which is why it is gitignored. Set:

    PROBE_DBX_SERVER      PROBE_DBX_HTTP_PATH   PROBE_DBX_CATALOG   PROBE_DBX_SCHEMA   PROBE_DBX_TABLE
    PROBE_SF_SERVER       PROBE_SF_WAREHOUSE    PROBE_SF_DATABASE   PROBE_SF_SCHEMA    PROBE_SF_TABLE
    PROBE_SQL_SERVER      PROBE_SQL_DATABASE                        PROBE_SQL_SCHEMA   PROBE_SQL_TABLE

Any group whose *_SERVER is unset is omitted, so this also produces 2-source or 1-source fixtures.
"""

from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path

DEFAULT_OUT = Path("tests/fixtures/live/multi-source-live.twbx")
TEMPLATE_DIR = Path("tests/fixtures/connection-templates")


def _leg(prefix: str, cls: str, extra: dict[str, str]) -> dict | None:
    """Describe one federated leg from its PROBE_* variables, or None when unconfigured."""
    server = os.environ.get(f"PROBE_{prefix}_SERVER")
    if not server:
        return None
    return {
        "prefix": prefix.lower(),
        "class": cls,
        "server": server,
        "database": os.environ.get(f"PROBE_{prefix}_DATABASE", ""),
        "schema": os.environ.get(f"PROBE_{prefix}_SCHEMA", ""),
        "table": os.environ.get(f"PROBE_{prefix}_TABLE", "SHIPMENT"),
        "extra": {k: os.environ.get(v, "") for k, v in extra.items()},
    }


def collect_legs() -> list[dict]:
    """Every leg that has an endpoint configured, in a stable order."""
    legs = [
        _leg("DBX", "databricks", {"http_path": "PROBE_DBX_HTTP_PATH"}),
        _leg("SF", "snowflake", {"warehouse": "PROBE_SF_WAREHOUSE"}),
        _leg("SQL", "azure_sqldb", {}),
    ]
    return [leg for leg in legs if leg]


def connection_xml(leg: dict) -> str:
    """Render one <named-connection> from the REAL attribute shape for that connector.

    The inner element is a template derived from an actual Tableau export
    (`scripts/derive_connection_templates.py`), with only endpoint values substituted. It is not
    synthesised, and that distinction is the point of this fixture: a hand-written element encodes
    what we THINK Tableau writes, and that guess has been wrong in a way that mattered - the
    Databricks HTTP path is spelled `_.fcp.DatabricksCatalog.true...v-http-path`, which both this
    repo's parser and the deterministic tier's silently read as None.
    """
    template_path = TEMPLATE_DIR / f"{leg['class']}.xml"
    if not template_path.exists():
        raise SystemExit(
            f"ERROR: no connection template for '{leg['class']}' at {template_path}.\n"
            f"       Derive one from a real Tableau export:\n"
            f"       python scripts/derive_connection_templates.py <export.tds|.tdsx|.twb|.twbx>"
        )

    element = template_path.read_text(encoding="utf-8").strip()
    server = leg["server"]
    for token, value in (
        ("{{SERVER}}", server),
        ("{{DATABASE}}", leg["database"]),
        ("{{SCHEMA}}", leg["schema"]),
        ("{{WAREHOUSE}}", leg["extra"].get("warehouse", "")),
        ("{{HTTP_PATH}}", leg["extra"].get("http_path", "")),
        ("{{USERNAME}}", "probeuser"),
        ("{{INSTANCE_URL}}", f"https://{server}/oidc"),
    ):
        element = element.replace(token, value)

    name = f"{leg['class']}.probe{leg['prefix']}"
    closing = "" if element.rstrip().endswith("/>") else "\n        </connection>"
    return f"""      <named-connection caption='{server}' name='{name}'>
        {element}{closing}
      </named-connection>"""


def relation_xml(leg: dict) -> str:
    """Render the <relation> that binds a leg to its physical table."""
    name = f"{leg['class']}.probe{leg['prefix']}"
    parts = [p for p in (leg["database"], leg["schema"], leg["table"]) if p]
    ref = ".".join(f"[{p}]" for p in parts)
    return f"      <relation connection='{name}' name='{leg['prefix']}_{leg['table']}' table='{ref}' type='table' />"


def build_twb(legs: list[dict]) -> str:
    """Render a complete .twb whose single federated datasource spans every configured leg."""
    connections = "\n".join(connection_xml(leg) for leg in legs)
    relations = "\n".join(relation_xml(leg) for leg in legs)
    manifest = (
        "    <_.fcp.DatabricksCatalog.true...DatabricksCatalog />\n"
        if any(leg["class"] == "databricks" for leg in legs)
        else ""
    )
    cols, records = [], []
    for leg in legs:
        tbl = f"{leg['prefix']}_{leg['table']}"
        for col, ctype in (("CUSTOMER", "string"), ("BILL_AMOUNT", "real")):
            cols.append(f"      <map key='[{tbl}_{col}]' value='[{tbl}].[{col}]' />")
            records.append(
                f"""      <metadata-record class='column'>
        <remote-name>{col}</remote-name>
        <local-name>[{tbl}_{col}]</local-name>
        <parent-name>[{tbl}]</parent-name>
        <remote-alias>{col}</remote-alias>
        <local-type>{ctype}</local-type>
        <contains-null>true</contains-null>
      </metadata-record>"""
            )
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2021.3' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <document-format-change-manifest>
{manifest}  </document-format-change-manifest>
  <datasources>
    <datasource caption='Multi Source Probe' inline='true' name='federated.probe' version='18.1'>
      <connection class='federated'>
        <named-connections>
{connections}
        </named-connections>
{relations}
        <cols>
{chr(10).join(cols)}
        </cols>
        <metadata-records>
{chr(10).join(records)}
        </metadata-records>
      </connection>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Probe Sheet'>
      <table>
        <view>
          <datasources>
            <datasource caption='Multi Source Probe' name='federated.probe' />
          </datasources>
        </view>
      </table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='Probe Dashboard'>
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='98000' id='2' name='Probe Sheet' w='98000' x='1000' y='1000' />
        </zone>
      </zones>
    </dashboard>
  </dashboards>
</workbook>
"""


def main() -> int:
    """Generate the fixture from whichever PROBE_* endpoint groups are configured."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    legs = collect_legs()
    if not legs:
        print("ERROR: no PROBE_*_SERVER environment variables set; nothing to generate.")
        print("       See this file's docstring for the variable names.")
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("multi-source-live.twb", build_twb(legs))

    print(f"wrote {args.out}  ({args.out.stat().st_size} bytes)")
    for leg in legs:
        print(f"  {leg['class']:<14}{leg['server']}  ->  {leg['database']}.{leg['schema']}.{leg['table']}")
    print("\nThis file embeds real endpoint names - it is gitignored on purpose.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
