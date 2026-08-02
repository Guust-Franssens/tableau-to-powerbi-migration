"""
purpose: provision and verify the Snowflake test fixture for the credential-gate probe - a warehouse,
         database, schema, and a 400-row SHIPMENT table mirroring the Databricks fixture, so both
         connector paths are exercised by the same test.
usage:   python scripts/provision_snowflake_fixture.py [--verify-only] [--warehouse PROBE_WH]

Auth is KEY-PAIR, not the PAT, for two concrete reasons: key-pair authentication does not require a
Snowflake network policy (a PAT is refused with `390432: Network policy is required` until one is
ATTACHED to the user), and it does not break when the workstation's public IP rotates.

Run scripts/setup_snowflake_keypair.py once to generate the key and register it.

⚠️ This credential provisions INFRASTRUCTURE only. It is never a Power BI credential, and that
distinction is the entire premise of the credential gate: the probe must exercise Power BI Desktop's
own per-user credential store, so a key held by this script proves nothing about whether Power BI can
reach the source. Provisioning is ordinary setup; authenticating *as the user* to Power BI is not.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("provision_snowflake")

REPO = Path(__file__).resolve().parent.parent
KEY_PATH = REPO / "secrets" / "snowflake_key.p8"

WAREHOUSE = "PROBE_WH"
DATABASE = "TABLEAU_MIGRATION"
SCHEMA = "PROBE"
TABLE = "SHIPMENT"

# Mirrors the Databricks fixture exactly - same table, same two columns, same 400 deterministic rows -
# so a probe result from one connector is directly comparable with the other, and the totals double
# as a fidelity oracle later.
STATEMENTS = [
    f"CREATE WAREHOUSE IF NOT EXISTS {WAREHOUSE} WAREHOUSE_SIZE = 'XSMALL' "
    "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE",
    f"CREATE DATABASE IF NOT EXISTS {DATABASE}",
    f"CREATE SCHEMA IF NOT EXISTS {DATABASE}.{SCHEMA}",
    f"CREATE OR REPLACE TABLE {DATABASE}.{SCHEMA}.{TABLE} (CUSTOMER STRING, BILL_AMOUNT NUMBER(12,2))",
    f"INSERT INTO {DATABASE}.{SCHEMA}.{TABLE} (CUSTOMER, BILL_AMOUNT) "
    "SELECT 'Customer ' || TO_CHAR(MOD(SEQ4(), 8) + 1), "
    "ROUND(100 + (MOD(SEQ4() * 37, 900)) + (MOD(SEQ4(), 100) / 100.0), 2) "
    "FROM TABLE(GENERATOR(ROWCOUNT => 400))",
]


def read_env() -> dict[str, str]:
    """Parse the gitignored `.env`. Values are used, never logged."""
    path = REPO / ".env"
    if not path.is_file():
        raise SystemExit(f"no {path} - expected SNOWFLAKE_URL (and SNOWFLAKE_USER)")
    env = dict(os.environ)
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def account_and_user(env: dict[str, str]) -> tuple[str, str]:
    """Derive the Snowflake account identifier and user.

    The account is the host label before `.snowflakecomputing.com` - Snowflake rejects the full
    hostname, and the resulting error is a generic auth failure that names nothing, so deriving it
    beats guessing.
    """
    url = env.get("SNOWFLAKE_URL", "")
    host = url.split("://", 1)[-1].split("/", 1)[0].rstrip(".")
    account = host.split(".", 1)[0]
    user = env.get("SNOWFLAKE_USER", "")
    if not account or not user:
        raise SystemExit("need SNOWFLAKE_URL and SNOWFLAKE_USER in .env")
    return account, user


def connect(env: dict[str, str], warehouse: str | None):
    """Open a key-pair authenticated connection."""
    try:
        # Lazy: the connector is an optional extra, so importing at module level would break every
        # other use of this file on a machine that never touches Snowflake.
        import snowflake.connector as sc  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    except ModuleNotFoundError as exc:
        raise SystemExit("needs the connector:  uv pip install snowflake-connector-python") from exc
    if not KEY_PATH.is_file():
        raise SystemExit(f"no private key at {KEY_PATH} - run scripts/setup_snowflake_keypair.py first")
    account, user = account_and_user(env)
    return sc.connect(
        account=account,
        user=user,
        private_key_file=str(KEY_PATH),
        role=env.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        **({"warehouse": warehouse} if warehouse else {}),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify-only", action="store_true", help="just count what is already there")
    parser.add_argument("--warehouse", default=WAREHOUSE, help="warehouse to run in (and to create)")
    args = parser.parse_args(argv)

    env = read_env()
    account, user = account_and_user(env)
    log.info("Snowflake %s as %s (key-pair)", account, user)

    # The warehouse may not exist yet, so the first connection must not request one.
    with connect(env, None) as conn, conn.cursor() as cur:
        if not args.verify_only:
            for i, statement in enumerate(STATEMENTS, 1):
                log.info("[%d/%d] %s...", i, len(STATEMENTS), statement.split("(")[0].strip()[:68])
                cur.execute(statement.replace(WAREHOUSE, args.warehouse, 1))
        cur.execute(f"USE WAREHOUSE {args.warehouse}")
        cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT CUSTOMER), ROUND(SUM(BILL_AMOUNT), 2) FROM {DATABASE}.{SCHEMA}.{TABLE}"
        )
        count, customers, total = cur.fetchone()

    log.info("\n%s.%s.%s: %s rows, %s customers, total %s", DATABASE, SCHEMA, TABLE, count, customers, total)
    if int(count) != 400:
        log.error("expected 400 rows, got %s", count)
        return 1
    log.info("\nFixture ready. Next: sign in to Snowflake ONCE in Power BI Desktop, then the happy")
    log.info("path is testable. The probe cannot use this key - it must use Desktop's own credential.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
