#!/usr/bin/env python3
"""Independent field-reference cross-check: every visual.json binding vs the
frozen AirlineAllianceActivity.SemanticModel TMDL display names.
Re-runnable. Exit 0 = all bindings resolve; exit 1 = at least one bad ref.
"""
import json, os, re, glob, sys

ROOT = r"C:\Users\gfranssens\vscode-projects\tableau-to-pbi-migration\migrations\airline-alliance-activity\fabric"
MODEL = os.path.join(ROOT, "AirlineAllianceActivity.SemanticModel", "definition", "tables")
PAGES = os.path.join(ROOT, "AirlineAllianceActivity.Report", "definition", "pages")

# ---- 1. Parse TMDL: table -> columns / measures (display names) ----
cols, meas, tables = {}, {}, set()
for tf in glob.glob(os.path.join(MODEL, "*.tmdl")):
    cur = None
    for raw in open(tf, encoding="utf-8"):
        line = raw.rstrip("\n")
        m = re.match(r"^table\s+(?:'([^']+)'|(\S+))", line)
        if m:
            cur = m.group(1) or m.group(2)
            tables.add(cur); cols.setdefault(cur, set()); meas.setdefault(cur, set())
            continue
        m = re.match(r"^\t(column|measure)\s+(?:'([^']+)'|([^\s=]+))", line)
        if m and cur:
            name = m.group(2) or m.group(3)
            (cols if m.group(1) == "column" else meas)[cur].add(name)

def status(entity, prop):
    if entity not in tables:
        return "BAD_TABLE"
    if prop in cols.get(entity, ()) or prop in meas.get(entity, ()):
        return "OK"
    return "BAD_FIELD"

# ---- 2. Recursively collect Column/Measure refs from a visual.json ----
def collect(node, aliases):
    if isinstance(node, dict):
        local = dict(aliases)
        fr = node.get("From")
        if isinstance(fr, list):
            for e in fr:
                if isinstance(e, dict) and "Name" in e and "Entity" in e:
                    local[e["Name"]] = e["Entity"]
        for key in ("Column", "Measure"):
            sub = node.get(key)
            if isinstance(sub, dict) and "Property" in sub:
                sr = (sub.get("Expression") or {}).get("SourceRef") or {}
                ent = sr.get("Entity") or local.get(sr.get("Source"))
                if ent is not None:
                    yield (ent, sub["Property"], key)
        for v in node.values():
            yield from collect(v, local)
    elif isinstance(node, list):
        for it in node:
            yield from collect(it, aliases)

# ---- 3. Walk every visual on every page ----
pj = json.load(open(os.path.join(PAGES, "pages.json"), encoding="utf-8"))
pagename = {0: "Alliance", 1: "Airlines", 2: "Fleet", 3: "Flight"}
total = 0
bad = []
pair_status = {}           # (entity,prop,kind) -> status
per_table = {}             # entity -> ref count
for idx, pg in enumerate(pj["pageOrder"]):
    for vf in glob.glob(os.path.join(PAGES, pg, "visuals", "*", "visual.json")):
        vj = json.load(open(vf, encoding="utf-8"))
        vtype = vj.get("visual", {}).get("visualType", "?")
        for ent, prop, kind in collect(vj, {}):
            total += 1
            st = status(ent, prop)
            pair_status[(ent, prop, kind)] = st
            per_table[ent] = per_table.get(ent, 0) + 1
            if st != "OK":
                bad.append((idx, pagename.get(idx), vtype,
                            os.path.basename(os.path.dirname(vf)), ent, prop, kind, st))

# ---- 4. Report ----
print(f"Model tables ({len(tables)}): " + ", ".join(sorted(tables)))
print(f"  columns total: {sum(len(v) for v in cols.values())} | measures total: {sum(len(v) for v in meas.values())}")
print(f"\nField references scanned across all visuals: {total}")
print(f"Distinct (table, field, kind) bindings: {len(pair_status)}")
print("\nReferences per table:")
for t, c in sorted(per_table.items(), key=lambda x: -x[1]):
    print(f"   {c:5d}  {t}")
print(f"\nDistinct bindings by status:")
by = {}
for st in pair_status.values():
    by[st] = by.get(st, 0) + 1
for st, c in sorted(by.items()):
    print(f"   {st}: {c}")

if bad:
    print(f"\n!!! {len(bad)} BAD REFERENCE(S) !!!")
    for b in bad:
        print(f"   page{b[0]} {b[1]} [{b[2]}] visual {b[3]}: {b[6]} '{b[4]}'[{b[5]}] -> {b[7]}")
    sys.exit(1)
else:
    print("\nALL BINDINGS RESOLVE against the semantic model TMDL display names. (0 bad references)")
    # also list the distinct bindings for the record
    print("\n--- distinct bindings (for the record) ---")
    for (ent, prop, kind), st in sorted(pair_status.items()):
        print(f"   {kind:8s} {ent} [{prop}]")
    sys.exit(0)
