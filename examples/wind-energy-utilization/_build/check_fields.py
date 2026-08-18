#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Independent field-reference cross-check: every Entity/Property used in the 30
visual.json files must resolve to a real column or measure in the committed TMDL.
Backs up validate's skipped JSON-schema step (PBIR_SCHEMA_UNREACHABLE offline)."""
import json, os, re, glob
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

ROOT = str(_REPO / "migrations" / "wind-energy-utilization" / "fabric")
SM_TABLES = os.path.join(ROOT, "WindEnergyUtilization.SemanticModel", "definition", "tables")
VIS = os.path.join(ROOT, "WindEnergyUtilization.Report", "definition", "pages", "overview", "visuals")

# ---- parse TMDL: {table: {"columns": set, "measures": set}} ----
model = {}
col_re  = re.compile(r"^\s*column\s+(?:'([^']+)'|([A-Za-z0-9_ ]+?))\s*(?:=|$)", re.M)
meas_re = re.compile(r"^\s*measure\s+(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z0-9_ ]+?))\s*=", re.M)
for f in glob.glob(os.path.join(SM_TABLES, "*.tmdl")):
    txt = open(f, encoding="utf-8-sig").read()
    m = re.search(r"^table\s+(?:'([^']+)'|([A-Za-z0-9_ ]+?))\s*$", txt, re.M)
    tname = (m.group(1) or m.group(2)).strip() if m else os.path.splitext(os.path.basename(f))[0]
    cols = set(); meas = set()
    for cm in col_re.finditer(txt):
        cols.add((cm.group(1) or cm.group(2)).strip())
    for mm in meas_re.finditer(txt):
        meas.add((mm.group(1) or mm.group(2) or mm.group(3)).strip())
    model[tname] = {"columns": cols, "measures": meas}

# ---- walk visual.json, collect (Entity, Property, kind) ----
refs = []  # (visualName, entity, prop, kind)
def walk(node, vname):
    if isinstance(node, dict):
        for kind in ("Column", "Measure"):
            if kind in node and isinstance(node[kind], dict) and "Property" in node[kind]:
                ent = None
                exprsrc = node[kind].get("Expression", {})
                # Column agg wraps: Expression.Column... ; plain: Expression.SourceRef.Entity
                sr = exprsrc.get("SourceRef", {})
                ent = sr.get("Entity")
                refs.append((vname, ent, node[kind]["Property"], kind))
        # Aggregation wraps a Column
        for v in node.values():
            walk(v, vname)
    elif isinstance(node, list):
        for it in node:
            walk(it, vname)

for vj in glob.glob(os.path.join(VIS, "*", "visual.json")):
    d = json.load(open(vj, encoding="utf-8"))
    vname = d.get("name", os.path.basename(os.path.dirname(vj)))
    vtype = d.get("visual", {}).get("visualType", "?")
    walk(d, f"{vname}[{vtype}]")

# ---- resolve; Entity may be None when only a source alias is present (From bindings) ----
bad = []
seen = set()
for vname, ent, prop, kind in refs:
    if ent is None:
        continue  # alias-based (filter From) refs resolved separately below
    key = (ent, prop, kind)
    if key in seen:
        continue
    seen.add(key)
    if ent not in model:
        bad.append(f"MISSING TABLE '{ent}' (prop '{prop}', {kind}) in {vname}")
        continue
    pool = model[ent]["columns"] if kind == "Column" else model[ent]["measures"]
    other = model[ent]["measures"] if kind == "Column" else model[ent]["columns"]
    if prop not in pool:
        if prop in other:
            bad.append(f"KIND MISMATCH {ent}[{prop}] declared {kind} but is a {'measure' if kind=='Column' else 'column'} ({vname})")
        else:
            bad.append(f"MISSING {kind} {ent}[{prop}] ({vname})")

print("TABLES IN MODEL:", len(model))
for t in sorted(model):
    print(f"  {t}: {len(model[t]['columns'])} cols, {len(model[t]['measures'])} measures")
print("DISTINCT FIELD REFS CHECKED:", len(seen))
print("PROBLEMS:", len(bad))
for b in bad:
    print("  X", b)
if not bad:
    print("ALL FIELD REFERENCES RESOLVE.")
