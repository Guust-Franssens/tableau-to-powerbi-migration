"""Introspect the packaged .hyper extracts: list schemas, tables, columns, row counts.
Run: .venv\\Scripts\\python.exe fabric\\_validation\\_hyper_probe.py
"""
import os, glob
from tableauhyperapi import HyperProcess, Telemetry, Connection, CreateMode

HERE = os.path.dirname(__file__)
RAW = os.path.abspath(os.path.join(HERE, "..", "..", "data", "_hyper_raw"))

files = sorted(glob.glob(os.path.join(RAW, "*.hyper")))
with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
    for f in files:
        print("\n" + "#" * 90)
        print("HYPER:", os.path.basename(f))
        print("#" * 90)
        with Connection(endpoint=hp.endpoint, database=f, create_mode=CreateMode.NONE) as conn:
            schemas = conn.catalog.get_schema_names()
            for sch in schemas:
                tables = conn.catalog.get_table_names(schema=sch)
                for tn in tables:
                    tdef = conn.catalog.get_table_definition(tn)
                    n = conn.execute_scalar_query(f"SELECT COUNT(*) FROM {tn}")
                    print(f"\n  TABLE {tn}  rows={n}")
                    for c in tdef.columns:
                        print(f"      {str(c.name):45} {c.type}")
