"""Ad-hoc inspection of migration-spec.json for the electricity-per-capita workbook.
Run: .venv\\Scripts\\python.exe fabric\\_validation\\_inspect.py [section]
"""
import json, os, sys, textwrap

HERE = os.path.dirname(__file__)
SPEC = os.path.abspath(os.path.join(HERE, "..", "..", "migration-spec.json"))
d = json.load(open(SPEC, encoding="utf-8"))

def sep(t): print("\n" + "=" * 100 + "\n" + t + "\n" + "=" * 100)

# global id -> caption map for resolving referenced_fields
ID2CAP = {}
for ds in d["data_sources"]:
    for fld in ds.get("fields", []):
        ID2CAP[fld.get("id")] = fld.get("caption")
def rc(ids):
    return [ID2CAP.get(i, i) for i in (ids or [])]

section = sys.argv[1] if len(sys.argv) > 1 else "all"

if section in ("all", "src"):
    sep("SOURCE")
    print(json.dumps(d.get("source"), indent=2)[:2000])

if section in ("all", "params"):
    sep("PARAMETERS")
    for p in d.get("parameters", []):
        print(json.dumps(p, indent=2))

if section in ("all", "ds"):
    for ds in d["data_sources"]:
        sep(f"DATA SOURCE: caption={ds.get('caption')!r}  id={ds.get('id')}")
        print("connection:", json.dumps(ds.get("connection"), indent=2))
        print("\nTABLES:")
        for t in ds.get("tables", []):
            print("  ", json.dumps(t))
        print("\nJOINS:", json.dumps(ds.get("joins", [])))
        print("\nFIELDS (%d):" % len(ds.get("fields", [])))
        for fld in ds.get("fields", []):
            kind = fld.get("kind")
            marks = []
            if fld.get("is_lod"): marks.append("LOD")
            if fld.get("is_table_calc"): marks.append("TABLECALC")
            if fld.get("reshape_hint"): marks.append("reshape=" + str(fld.get("reshape_hint")))
            if fld.get("hidden"): marks.append("HIDDEN")
            print(f"  - id={fld.get('id')} | kind={kind} | caption={fld.get('caption')!r} | dtype={fld.get('data_type')} | role={fld.get('role')} | table_id={fld.get('table_id')} {'['+','.join(marks)+']' if marks else ''}")
            print(f"      internal_name={fld.get('internal_name')!r}  default_agg={fld.get('default_aggregation')}")
            if fld.get("aliases"):
                print("      aliases:", json.dumps(fld.get("aliases")))
            if fld.get("formatting"):
                print("      formatting:", json.dumps(fld.get("formatting")))
            if kind == "calculated":
                formula = (fld.get("tableau_formula") or "").strip()
                print("      FORMULA: " + formula.replace("\r\n", "\n").replace("\n", "\n               "))
                if fld.get("referenced_fields"):
                    print("      referenced_fields:", rc(fld.get("referenced_fields")))

if section in ("all", "ws"):
    for w in d["worksheets"]:
        sep(f"WORKSHEET: {w.get('name')!r}")
        print("mark:", w.get("mark_type"), "| datasources:", w.get("data_source_ids") or w.get("data_sources"))
        print(json.dumps(w, indent=2)[:3500])

if section == "limits":
    sep("LIMITATIONS")
    for l in d.get("limitations_encountered", []):
        print(json.dumps(l))

# field id -> (caption, ds_short)
FLD2DS = {}
for ds in d["data_sources"]:
    short = ds.get("id", "").replace("ds.", "")
    for fld in ds.get("fields", []):
        FLD2DS[fld.get("id")] = short
def fcap(fid):
    if not fid:
        return "None"
    return f"{ID2CAP.get(fid, fid)} <{FLD2DS.get(fid,'?')}>"

if section in ("all", "wsum"):
    sep("WORKSHEET SUMMARY")
    for w in d["worksheets"]:
        enc = w.get("encodings", {}) or {}
        # data sources actually referenced by fields
        used = set()
        def collect(items):
            for it in items or []:
                fid = it.get("field_id") if isinstance(it, dict) else None
                if fid:
                    used.add(FLD2DS.get(fid, "?"))
        for k in ("rows", "columns", "label", "detail", "tooltip"):
            collect(enc.get(k))
        for k in ("color", "size", "shape"):
            v = enc.get(k)
            if isinstance(v, dict):
                collect([v])
        print(f"\n### {w.get('name')!r}  mark={w.get('mark_type')}  ds_used={sorted(used)}")
        for shelf in ("rows", "columns", "color", "size", "shape", "label", "detail", "tooltip"):
            v = enc.get(shelf)
            if not v:
                continue
            if isinstance(v, dict):
                v = [v]
            parts = []
            for it in v:
                if not isinstance(it, dict):
                    continue
                fid = it.get("field_id")
                der = it.get("derivation")
                agg = it.get("aggregation")
                tag = fcap(fid)
                extra = ",".join(x for x in [f"der={der}" if der and der != "none" else "", f"agg={agg}" if agg else ""] if x)
                parts.append(tag + (f" ({extra})" if extra else ""))
            if parts:
                print(f"    {shelf:8}: " + " | ".join(parts))
        for flt in w.get("filters", []) or []:
            print(f"    FILTER  : {fcap(flt.get('field_id'))}  note={flt.get('note')!r}  kind={flt.get('kind')}")
        for rl in w.get("reference_lines", []) or []:
            print(f"    REFLINE : {json.dumps(rl)}")
        mnv = w.get("measure_names_values_pivot")
        if mnv:
            print(f"    MNV PIVOT: {json.dumps(mnv)}")
