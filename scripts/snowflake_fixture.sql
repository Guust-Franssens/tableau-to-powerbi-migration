-- purpose: create the Snowflake test fixture for the credential-gate probe, mirroring the
--          Databricks fixture (same table, same columns, same 400 rows) so the two connectors
--          produce directly comparable probe results.
-- usage:   paste into a Snowsight worksheet and Run All. Takes a few seconds.
--
-- Why you run this by hand rather than the agent running it:
--   Snowflake PAT authentication refuses with "390432: Network policy is required" until a network
--   policy is attached to the USER (not merely the account). Pasting this script is faster than
--   granting that, and it keeps the PAT unused - which matters, because the probe deliberately
--   CANNOT use a PAT anyway (it must exercise Power BI Desktop's own credential store, not the
--   agent's).
--
--   If you DO want the PAT to work for automation, run as SECURITYADMIN/ACCOUNTADMIN:
--       CREATE NETWORK POLICY IF NOT EXISTS PAT_POLICY
--           ALLOWED_IP_LIST = ('<your.public.ip>');
--       SET me = CURRENT_USER();
--       ALTER USER IDENTIFIER($me) SET NETWORK_POLICY = PAT_POLICY;
--       SHOW PARAMETERS LIKE 'NETWORK_POLICY' FOR USER IDENTIFIER($me);   -- verify: must be set
--   Note IDENTIFIER() takes a literal or a session variable - `IDENTIFIER(CURRENT_USER())` fails,
--   which is easy to miss because CREATE NETWORK POLICY succeeds first and the PAT then keeps
--   returning the same 390432 as if nothing had been done. The policy existing is not the same as
--   the policy being ATTACHED, and only the attach silences that error.
--   Scope it to the USER, never the account: an account-wide policy also governs Power BI Desktop's
--   own connection and can lock you out of the interactive sign-in the happy path depends on. The
--   IP is typically dynamic (ISP rotation, VPN), so a PAT that suddenly fails with 390432 again
--   usually means the address moved, not that the token expired.
--
-- After running this, sign in to Snowflake ONCE in Power BI Desktop. Note the ordering:
--   1. BEFORE signing in  -> probe should return NO_CREDENTIAL  (the unhappy path, free to test)
--   2. AFTER signing in   -> probe should return DATA_OK        (the happy path)
--
-- ⚠️ UNVERIFIED: whether Power BI keys a Snowflake credential per ACCOUNT HOST or per
-- (HOST, WAREHOUSE) is NOT yet measured. For Databricks it is measured to be per (host, HTTP path),
-- which is what makes a second warehouse a free uncredentialed fixture there. Snowflake's connector
-- takes `warehouse` as a REQUIRED parameter of Snowflake.Databases(), so it may well be part of the
-- credential's data-source path too - do not assume it is not. PROBE_WH_2 exists to settle this by
-- experiment: after signing in with PROBE_WH, probe PROBE_WH_2. DATA_OK means per-account;
-- NO_CREDENTIAL means per-warehouse (and then Snowflake gets a permanent non-destructive unhappy
-- fixture, exactly like Databricks).

CREATE WAREHOUSE IF NOT EXISTS PROBE_WH
    WAREHOUSE_SIZE = 'XSMALL'
    AUTO_SUSPEND = 60          -- seconds; costs nothing while idle
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE;

CREATE DATABASE IF NOT EXISTS TABLEAU_MIGRATION;
CREATE SCHEMA   IF NOT EXISTS TABLEAU_MIGRATION.PROBE;

USE WAREHOUSE PROBE_WH;
USE SCHEMA TABLEAU_MIGRATION.PROBE;

CREATE OR REPLACE TABLE SHIPMENT (
    CUSTOMER    STRING,
    BILL_AMOUNT NUMBER(12, 2)
);

-- 400 rows across 8 customers, deterministic so the probe and any later fidelity check agree.
INSERT INTO SHIPMENT (CUSTOMER, BILL_AMOUNT)
SELECT
    'Customer ' || TO_CHAR(MOD(SEQ4(), 8) + 1),
    ROUND(100 + (MOD(SEQ4() * 37, 900)) + (MOD(SEQ4(), 100) / 100.0), 2)
FROM TABLE(GENERATOR(ROWCOUNT => 400));

-- Verify: expect 400 rows and 8 distinct customers.
SELECT COUNT(*) AS ROW_COUNT,
       COUNT(DISTINCT CUSTOMER) AS CUSTOMERS,
       ROUND(SUM(BILL_AMOUNT), 2) AS TOTAL_BILL
FROM SHIPMENT;
