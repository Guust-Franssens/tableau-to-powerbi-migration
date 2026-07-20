"""Structured dump of the broadway migration-spec.json (scratch inspection tool)."""
import json, os, sys

SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "migration-spec.json")
with open(SPEC, encoding="utf-8") as f:
    d = json.load(f)

def sep(t): print("\n" + "=" * 90 + f"\n{t}\n" + "=" * 90)

print("TOP-LEVEL KEYS:", list(d.keys()))

sep("PARAMETERS")
for p in d.get("parameters", []):
    print(f"  - id={p.get('id')} caption={p.get('caption')!r} internal={p.get('internal_name')!r} "
          f"dtype={p.get('data_type')} current={p.get('current_value')!r}")
    if p.get("members"):
        print(f"        members={p.get('members')}")

sep("DATA SOURCES")
for ds in d.get("data_sources", []):
    print(f"\n### DS id={ds.get('id')} name={ds.get('name')!r} caption={ds.get('caption')!r}")
    conn = ds.get("connection", {})
    print(f"    connection: mode={conn.get('mode')} hyper={conn.get('hyper_file')!r} class={conn.get('class')}")
    print(f"    tables: {[t.get('name') for t in ds.get('tables', [])]}")
    for t in ds.get("tables", []):
        print(f"        table id={t.get('id')} name={t.get('name')!r} caption={t.get('caption')!r}")
    joins = ds.get("joins", [])
    print(f"    joins ({len(joins)}):")
    for j in joins:
        print(f"        {j}")
    fields = ds.get("fields", [])
    cols = [f for f in fields if f.get("kind") == "column"]
    calcs = [f for f in fields if f.get("kind") == "calculated"]
    print(f"    fields: {len(fields)} total ({len(cols)} column, {len(calcs)} calculated)")
    print("    --- COLUMNS ---")
    for f in cols:
        print(f"        [{f.get('id')}] cap={f.get('caption')!r} internal={f.get('internal_name')!r} "
              f"dtype={f.get('data_type')} role={f.get('role')} agg={f.get('default_aggregation')} hidden={f.get('hidden')}")
    print("    --- CALCULATED ---")
    for f in calcs:
        print(f"        [{f.get('id')}] cap={f.get('caption')!r} internal={f.get('internal_name')!r} "
              f"dtype={f.get('data_type')} role={f.get('role')} lod={f.get('is_lod')} tc={f.get('is_table_calc')} "
              f"reshape={f.get('reshape_hint')} hidden={f.get('hidden')}")
        print(f"             formula: {f.get('formula')!r}")
        if f.get("referenced_fields"):
            print(f"             refs: {f.get('referenced_fields')}")

sep("WORKSHEETS")
for w in d.get("worksheets", []):
    print(f"\n### WS id={w.get('id')} name={w.get('name')!r} mark={w.get('mark_type')} ds={w.get('data_source_ids')}")
    if w.get("measure_names_values_pivot"):
        print(f"    MEASURE-NAMES PIVOT: {w.get('measure_names_values_pivot')}")
    for enc in w.get("encodings", []):
        print(f"    enc: {enc}")
    for fl in w.get("filters", []):
        print(f"    filter: {fl}")
    for rl in w.get("reference_lines", []):
        print(f"    refline: {rl}")

sep("DASHBOARDS (names only)")
for db in d.get("dashboards", []):
    print(f"  - {db.get('name')!r} zones_root_type={db.get('zones',{}).get('type') if isinstance(db.get('zones'),dict) else 'n/a'}")
    print(f"      actions: {len(db.get('actions', []))}")

sep("LIMITATIONS (parse-stage)")
for lim in d.get("limitations_encountered", []):
    print(f"  - stage={lim.get('stage')} sev={lim.get('severity')} item={lim.get('item')}")
    print(f"      {lim.get('issue')}")
