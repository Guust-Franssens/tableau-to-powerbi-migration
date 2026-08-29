"""
purpose: build a minimal, self-contained .twbx seed workbook (one CSV, one worksheet) so that an
         otherwise-empty Tableau project has something a migration can actually pick up
usage:   python scripts/make_seed_workbook.py --name "Depth Probe L11" --out seed.twbx

Why a seed workbook is not busywork
-----------------------------------
A structural fixture with no content tests nothing. The trial site carries an 11-level project chain
(`ZZ Deep/L1/.../L11`) and a set of deliberately hostile project names (`R/D`, `R+D`, `Trailing dot.`,
`Ventes francaises`) - and every one of them is EMPTY. Nothing ever migrates out of them, so the path
handling they were built to break is never exercised. Dropping one tiny workbook into each turns an
inert folder into a real test case.

The workbook is deliberately trivial - a three-row CSV and a single bar - because the fixture's whole
value is its LOCATION, not its content. Keeping it small means seeding a dozen projects costs a few
KB rather than several MB of duplicated extracts.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

CSV_ROWS = "region,amount\nNorth,120\nSouth,95\nEast,143\n"

# The inline datasource's name doubles as the MARKER that identifies a workbook as one this tool
# built: `provision_tableau_estate.py` refuses to Overwrite a workbook it cannot positively confirm
# is a seed, and a name prefix is not evidence of authorship. Keep this constant and the template in
# step - `tests/test_make_seed_workbook.py` pins that they agree.
SEED_DATASOURCE_NAME = "federated.seed01"

# Tableau caps a sheet name at 60 characters and requires it unique ACROSS worksheets and dashboards.
# Composing first and truncating afterwards (`f"{name} Sheet"[:60]`) broke both rules at once: for a
# name of 54 characters or more the worksheet and the dashboard both truncate to `name[:60]` - the
# same string - and slicing an already-escaped string can cut an entity in half (`&amp;` straddling
# index 60 leaves a bare `&a`). Truncate the RAW base, then append the suffix, then escape.
MAX_SHEET_NAME = 60
SHEET_SUFFIX = " Sheet"
DASHBOARD_SUFFIX = " Dashboard"

# Tableau accepts a federated textscan datasource with an inline column list. The relation name and
# the `directory`/`filename` pair must agree with the packaged path, or the workbook opens broken.
TWB_TEMPLATE = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.1' source-platform='win' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <datasources>
    <datasource caption='{caption}' inline='true' name='federated.seed01' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='{csv_name}' name='textscan.seed01'>
            <connection class='textscan' directory='Data/seed' filename='{csv_name}' password='' server='' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.seed01' name='{csv_name}' table='[{csv_name}]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
            <column datatype='string' name='region' ordinal='0' />
            <column datatype='integer' name='amount' ordinal='1' />
          </columns>
        </relation>
        <metadata-records>
          <metadata-record class='column'>
            <remote-name>region</remote-name>
            <remote-type>129</remote-type>
            <local-name>[region]</local-name>
            <parent-name>[{csv_name}]</parent-name>
            <remote-alias>region</remote-alias>
            <ordinal>0</ordinal>
            <local-type>string</local-type>
            <aggregation>Count</aggregation>
            <contains-null>true</contains-null>
          </metadata-record>
          <metadata-record class='column'>
            <remote-name>amount</remote-name>
            <remote-type>20</remote-type>
            <local-name>[amount]</local-name>
            <parent-name>[{csv_name}]</parent-name>
            <remote-alias>amount</remote-alias>
            <ordinal>1</ordinal>
            <local-type>integer</local-type>
            <aggregation>Sum</aggregation>
            <contains-null>true</contains-null>
          </metadata-record>
        </metadata-records>
      </connection>
      <column caption='Region' datatype='string' name='[region]' role='dimension' type='nominal' />
      <column caption='Amount' datatype='integer' name='[amount]' role='measure' type='quantitative' />
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='{sheet}'>
      <table>
        <view>
          <datasources>
            <datasource caption='{caption}' name='federated.seed01' />
          </datasources>
          <datasource-dependencies datasource='federated.seed01'>
            <column caption='Region' datatype='string' name='[region]' role='dimension' type='nominal' />
            <column caption='Amount' datatype='integer' name='[amount]' role='measure' type='quantitative' />
            <column-instance column='[amount]' derivation='Sum' name='[sum:amount:qk]' pivot='key' type='quantitative' />
            <column-instance column='[region]' derivation='None' name='[none:region:nk]' pivot='key' type='nominal' />
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <rows>[federated.seed01].[sum:amount:qk]</rows>
        <cols>[federated.seed01].[none:region:nk]</cols>
      </table>
    </worksheet>
  </worksheets>
  <dashboards>
    <dashboard name='{dashboard}'>
      <style />
      <size maxheight='800' maxwidth='1000' minheight='800' minwidth='1000' />
      <zones>
        <zone h='98000' id='3' type-v2='layout-basic' w='98000' x='1000' y='1000'>
          <zone h='98000' id='2' name='{sheet}' w='98000' x='1000' y='1000' />
        </zone>
      </zones>
    </dashboard>
  </dashboards>
  <windows source-height='30'>
    <window class='worksheet' maximized='true' name='{sheet}'>
      <cards>
        <edge name='left'>
          <strip size='160'>
            <card type='pages' />
            <card type='filters' />
            <card type='marks' />
          </strip>
        </edge>
        <edge name='top'>
          <strip size='2147483647'>
            <card type='columns' />
          </strip>
          <strip size='2147483647'>
            <card type='rows' />
          </strip>
        </edge>
      </cards>
    </window>
    <window class='dashboard' name='{dashboard}'>
      <viewpoints>
        <viewpoint name='{sheet}' />
      </viewpoints>
    </window>
  </windows>
</workbook>
"""


def _attr(value: str) -> str:
    """Escape `value` for one of TWB_TEMPLATE's SINGLE-quoted attributes.

    `saxutils.escape` covers `&`, `<` and `>` only - not quotes. Every attribute in the template is
    single-quoted, so an apostrophe closes it early and the file stops being XML. Seed names are
    `"Seed - " + <live project name>`, i.e. the apostrophe is the customer's, not ours: measured, a
    project named `L'Equipe` produced `caption='Seed - L'Equipe'` and
    `not well-formed (invalid token), line 4, column 34`. `"` is escaped too, so the template can
    switch to double quotes without silently re-opening this.
    """
    return escape(value, {"'": "&apos;", '"': "&quot;"})


def sheet_names(name: str) -> tuple[str, str]:
    """Return the `(worksheet, dashboard)` names for a seed, truncated so they cannot collide.

    Both are derived from a base truncated to leave room for the LONGEST suffix, so the two always
    differ by their suffix no matter how long the workbook name is.
    """
    base = name[: MAX_SHEET_NAME - max(len(SHEET_SUFFIX), len(DASHBOARD_SUFFIX))]
    return f"{base}{SHEET_SUFFIX}", f"{base}{DASHBOARD_SUFFIX}"


def build_twbx(name: str, out: Path) -> Path:
    """Write a minimal packaged workbook whose only job is to exist inside a given project.

    The `.twb` entry inside the archive is given a SANITISED name, deliberately decoupled from the
    workbook's display name. A name like `Seed - R/D` would otherwise create a *directory* inside the
    zip, and Tableau rejects the result with a bare "unexpected error occurred opening the packaged
    workbook". The published name comes from the REST item, not from this filename, so nothing is
    lost by sanitising it.
    """
    csv_name = "seed_data.csv"
    entry = "".join(c if (c.isalnum() or c in " -_()") else "_" for c in name).strip() or "seed"
    sheet, dashboard = sheet_names(name)
    twb = TWB_TEMPLATE.format(
        caption=_attr(name),
        csv_name=csv_name,
        sheet=_attr(sheet),
        dashboard=_attr(dashboard),
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{entry}.twb", twb)
        z.writestr(f"Data/seed/{csv_name}", CSV_ROWS)
    return out


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="workbook name, also used for the sheet caption")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    path = build_twbx(args.name, args.out)
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
