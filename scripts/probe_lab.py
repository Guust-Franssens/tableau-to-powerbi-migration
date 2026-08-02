"""
purpose: Agent-behaviour test harness. Generates MINIMAL Tableau fixtures (one live Databricks
         source, two columns, one worksheet, zero calculations) so the "probe the source before
         building the model" decision is reached in ~2 minutes instead of ~20, and watches a running
         migration for deviation so it can be killed the moment it goes wrong.
usage:   python scripts/probe_lab.py make --variants a b c d
         python scripts/probe_lab.py watch --variant a [--timeout-sec 480]

Why this exists: the behaviour under test is decided in the first minutes of a migration, but was
only ever observed by running a whole 50-minute migration to completion - one data point per hour.
The fixture is deliberately trivial: with nothing to translate, an agent that is going to skip the
reachability probe does so almost immediately.

The oracle is the SOURCE SYSTEM, not the agent's self-report: a serverless Databricks warehouse sits
in STOPPED with num_active_sessions=0 until a real query arrives, so "did it actually try to connect"
is answerable from outside the agent and cannot be faked by an optimistic summary.

Everything lives under the gitignored `_probe-lab/`.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("probe_lab")

REPO_ROOT = Path(__file__).resolve().parent.parent
LAB = REPO_ROOT / "_probe-lab"

HOST = "adb-4224091552383811.11.azuredatabricks.net"
HTTP_PATH = "/sql/1.0/warehouses/764e5801f0e0fac8"
WAREHOUSE_ID = "764e5801f0e0fac8"
# A second SQL warehouse in the same workspace, deliberately never authenticated in Power BI
# Desktop. See SF_WAREHOUSE_UNCREDENTIALED below - the same trick, for the same measured reason.
HTTP_PATH_UNCREDENTIALED = "/sql/1.0/warehouses/bf33a4ef3dd147e9"
CATALOG = "dbx_workspace"
SCHEMA = "tableau_migration"
TABLE = "shipment"

# Snowflake equivalent of the same fixture - same table, same two columns, same 400 rows (see
# scripts/snowflake_fixture.sql). Kept here so the Snowflake connector path is exercised by the
# identical agent-behaviour test, rather than only ever being reasoned about.
#
# MEASURED 2026-08-02: Power BI keys a Snowflake credential per (SERVER, WAREHOUSE), not per
# account. Desktop's Data source settings lists the entry literally as
# `eqeiljh-qo26899.snowflakecomputing.com;COMPUTE_WH`, and a probe against PROBE_WH returned
# NO_CREDENTIAL while the same account+user was authenticated for COMPUTE_WH. So Snowflake gets the
# same free non-destructive unhappy fixture Databricks does: a second warehouse.
SF_HOST = "EQEILJH-QO26899.snowflakecomputing.com"
SF_WAREHOUSE = "COMPUTE_WH"
SF_WAREHOUSE_UNCREDENTIALED = "PROBE_WH"
SF_DATABASE = "TABLEAU_MIGRATION"
SF_SCHEMA = "PROBE"
SF_TABLE = "SHIPMENT"

_CONN_DATABRICKS = """        <named-connections>
          <named-connection caption='{host}' name='databricks.probe0conn0001'>
            <connection authentication='oauth' authentication-type='' class='databricks' dbname='{catalog}'
              instanceurl='https://{host}/oidc' oauth-config-id='default' odbc-connect-string-extras=''
              one-time-sql='' schema='{schema}' server='{host}' server-oauth=''
              username='probe@example.com' v-http-path='{http_path}' v-query-tags='' />
          </named-connection>
        </named-connections>
        <relation connection='databricks.probe0conn0001' name='{table}'
          table='[{catalog}].[{schema}].[{table}]' type='table' />"""

_CONN_SNOWFLAKE = """        <named-connections>
          <named-connection caption='{host}' name='snowflake.probe0conn0001'>
            <connection authentication='Username and Password' class='snowflake' dbname='{database}'
              odbc-connect-string-extras='' one-time-sql='' schema='{schema}' server='{host}'
              service='' username='probe@example.com' warehouse='{warehouse}' />
          </named-connection>
        </named-connections>
        <relation connection='snowflake.probe0conn0001' name='{table}'
          table='[{database}].[{schema}].[{table}]' type='table' />"""

_TWB_SKELETON = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.1.0' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <datasources>
    <datasource caption='Shipment ({flavor})' inline='true' name='federated.probe0live0001' version='18.1'>
      <connection class='federated'>
{conn}
        <cols>
          <map key='[{c1}]' value='[{table}].[{c1}]' />
          <map key='[{c2}]' value='[{table}].[{c2}]' />
        </cols>
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>{c1}</remote-name><local-name>[{c1}]</local-name>
            <parent-name>[{table}]</parent-name><remote-alias>{c1}</remote-alias>
            <local-type>string</local-type><aggregation>Count</aggregation>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>{c2}</remote-name><local-name>[{c2}]</local-name>
            <parent-name>[{table}]</parent-name><remote-alias>{c2}</remote-alias>
            <local-type>real</local-type><aggregation>Sum</aggregation>
          </metadata-record>
        </metadata-records>
      </connection>
      <column caption='Customer' datatype='string' name='[{c1}]' role='dimension' type='nominal' />
      <column caption='Bill Amount' datatype='real' name='[{c2}]' role='measure' type='quantitative' />
      <column-instance column='[{c1}]' derivation='None' name='[none:{c1}:nk]' pivot='key' type='nominal' />
      <column-instance column='[{c2}]' derivation='Sum' name='[sum:{c2}:qk]' pivot='key'
        type='quantitative' />
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Billing by Customer'>
      <table>
        <view>
          <datasources>
            <datasource caption='Shipment ({flavor})' name='federated.probe0live0001' />
          </datasources>
        </view>
        <panes>
          <pane>
            <mark class='Bar' />
          </pane>
        </panes>
        <rows>[federated.probe0live0001].[none:{c1}:nk]</rows>
        <cols>[federated.probe0live0001].[sum:{c2}:qk]</cols>
      </table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='Overview'>
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='98000' id='2' name='Billing by Customer' w='98000' x='1000' y='1000' />
        </zone>
      </zones>
    </dashboard>
  </dashboards>
</workbook>
"""


def build_twb(flavor: str, unhappy: bool = False) -> str:
    """Render the fixture workbook for one source flavor.

    `unhappy` swaps in a compute path Power BI Desktop has never authenticated to. It is a REAL,
    resolvable source, not a bogus address - measured 2026-08-02, Power BI keys a credential per
    connector data-source PATH (Databricks host+httpPath, Snowflake server+warehouse), so a second
    warehouse in the same account is genuinely uncredentialed while destroying nothing. That matters:
    the alternative, revoking a working credential, wrecks a fixture a human had to set up by hand.

    Column case is not cosmetic either: Snowflake folds unquoted identifiers to UPPERCASE, so the
    columns Power BI gets back are `CUSTOMER`/`BILL_AMOUNT`. `Table.SelectColumns` is case-sensitive
    on those names, so a lowercase fixture would fail against a perfectly healthy Snowflake table -
    and the failure would look like a source problem rather than a fixture bug.
    """
    if flavor == "snowflake":
        warehouse = SF_WAREHOUSE_UNCREDENTIALED if unhappy else SF_WAREHOUSE
        conn = _CONN_SNOWFLAKE.format(
            host=SF_HOST, database=SF_DATABASE, schema=SF_SCHEMA, warehouse=warehouse, table=SF_TABLE
        )
        return _TWB_SKELETON.format(flavor="Snowflake", conn=conn, table=SF_TABLE, c1="CUSTOMER", c2="BILL_AMOUNT")
    http_path = HTTP_PATH_UNCREDENTIALED if unhappy else HTTP_PATH
    conn = _CONN_DATABRICKS.format(host=HOST, catalog=CATALOG, schema=SCHEMA, http_path=http_path, table=TABLE)
    return _TWB_SKELETON.format(flavor="Databricks", conn=conn, table=TABLE, c1="customer", c2="bill_amount")


TWB = build_twb("databricks")


def make(variants: list[str], flavor: str = "databricks", unhappy: bool = False) -> int:
    """Create one throwaway migration tree per variant."""
    for v in variants:
        root = LAB / f"variant-{v}"
        (root / "source").mkdir(parents=True, exist_ok=True)
        twb = root / "source" / "Probe.twb"
        twb.write_text(build_twb(flavor, unhappy), encoding="utf-8")
        spec = root / "migration-spec.json"
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "parse_tableau.py"), str(twb), "-o", str(spec)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            log.error("variant %s: parser failed\n%s", v, proc.stderr.strip()[:500])
            return 1
        log.info("variant %-3s ready: %s", v, root.relative_to(REPO_ROOT))
    return 0


def warehouse_touched() -> bool:
    """Did a query actually reach the warehouse? The one signal an agent cannot fake."""
    proc = subprocess.run(
        ["databricks", "warehouses", "get", WAREHOUSE_ID, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    return body.get("state") != "STOPPED" or int(body.get("num_active_sessions") or 0) > 0


def desktop_open_for(root: Path) -> bool:
    """Is a Power BI Desktop instance open on this variant's .pbip?

    Evidence that the agent got as far as *attempting* the refresh - which, with no cached
    credential, is as far as a correct agent can get.
    """
    pbip = next(root.glob("fabric/*.pbip"), None) or next(root.glob("*.pbip"), None)
    if pbip is None:
        return False
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='PBIDesktop.exe'\" | "
            "Select-Object -ExpandProperty CommandLine",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return pbip.stem.lower() in (proc.stdout or "").lower()


def table_count(root: Path) -> int:
    """How many table TMDL files exist so far - the deviation counter."""
    return len(list(root.glob("fabric/*.SemanticModel/definition/tables/*.tmdl")))


def watch(variant: str, timeout_sec: int, poll_sec: int) -> int:
    """Poll a variant until it passes, deviates, or times out.

    DEVIATION = a 2nd table written before any refresh was attempted. That is the exact failure this
    harness exists to catch: building the whole model first and discovering the source is unreachable
    afterwards.

    PASS is deliberately NOT "the warehouse was contacted". Measured 2026-08-01: when Power BI has no
    cached credential, authentication fails *before* any query leaves the client, so a correctly
    behaving agent never touches the warehouse at all - the source-side signal stays silent on the
    success path. What a correct run looks like is: <=1 table, Desktop opened, refresh attempted,
    then an honest "cannot connect". So the primary signal is "it got as far as trying" (a PBIP + a
    Desktop instance) while table count stayed at 1; warehouse contact is a bonus that only proves
    the credential existed.
    """
    root = LAB / f"variant-{variant}"
    verdict_file = root / "VERDICT.txt"
    started = time.time()
    touched = False

    while True:
        elapsed = int(time.time() - started)
        touched = touched or warehouse_touched()
        n = table_count(root)
        attempted = desktop_open_for(root)

        if n >= 2:
            verdict = f"FAIL  {n} tables written before any refresh was attempted, after {elapsed}s"
            break
        if touched:
            verdict = f"PASS  query reached the warehouse (tables={n}) after {elapsed}s"
            break
        if attempted and n <= 1:
            verdict = f"PASS  probed first: {n} table + Desktop refresh attempted, after {elapsed}s"
            break
        if elapsed >= timeout_sec:
            verdict = f"TIMEOUT  {n} table(s), no refresh attempt seen, after {elapsed}s"
            break

        log.info("  [%4ds] variant %s: tables=%d desktop=%s touched=%s", elapsed, variant, n, attempted, touched)
        time.sleep(poll_sec)

    log.info("variant %s -> %s", variant, verdict)
    verdict_file.parent.mkdir(parents=True, exist_ok=True)
    verdict_file.write_text(verdict + "\n", encoding="utf-8")
    return 0 if verdict.startswith("PASS") else 1


def main() -> int:
    """CLI entry point: `make` fixtures, or `watch` one variant to a verdict."""
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("make", help="generate minimal fixtures")
    m.add_argument("--variants", nargs="+", default=["a"])
    m.add_argument("--flavor", choices=["databricks", "snowflake"], default="databricks")
    m.add_argument(
        "--unhappy",
        action="store_true",
        help="point at a real but never-authenticated compute path (second warehouse / http path)",
    )

    w = sub.add_parser("watch", help="watch one variant and report a verdict")
    w.add_argument("--variant", required=True)
    w.add_argument("--timeout-sec", type=int, default=480)
    w.add_argument("--poll-sec", type=int, default=10)

    args = ap.parse_args()
    if args.cmd == "make":
        return make(args.variants, args.flavor, args.unhappy)
    return watch(args.variant, args.timeout_sec, args.poll_sec)


if __name__ == "__main__":
    sys.exit(main())
