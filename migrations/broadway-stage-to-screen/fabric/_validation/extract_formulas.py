"""Extract calculated-field formulas, parameter members, and blend/relationship
info directly from the source .twb inside the .twbx (works around the parser's
formula=None gap for this workbook)."""
import zipfile, os, re, sys
import xml.etree.ElementTree as ET

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "source",
                   "StagetoScreenIronVizBroadwayMusicalsTurnedintoMovies.twbx")

def find_twb_bytes(path):
    with zipfile.ZipFile(path) as z:
        twbs = [n for n in z.namelist() if n.lower().endswith(".twb")]
        print(f"[twbx members: {len(z.namelist())}] .twb files: {twbs}")
        with z.open(twbs[0]) as f:
            return f.read()

raw = find_twb_bytes(SRC)
root = ET.fromstring(raw)

def local(tag): return tag.split("}")[-1]

print("\n" + "=" * 100)
print("PARAMETERS (with members)")
print("=" * 100)
for ds in root.iter():
    if local(ds.tag) != "datasource":
        continue
    if ds.get("name") == "Parameters":
        for col in ds.iter():
            if local(col.tag) == "column":
                cap = col.get("caption") or col.get("name")
                members = []
                for m in col.iter():
                    if local(m.tag) == "member":
                        members.append(m.get("value"))
                print(f"  PARAM caption={cap!r} name={col.get('name')!r} datatype={col.get('datatype')} "
                      f"value={col.get('value')!r}")
                if members:
                    print(f"        members={members}")

print("\n" + "=" * 100)
print("CALCULATED FIELDS per datasource (caption | internal name | formula)")
print("=" * 100)
for ds in root.iter():
    if local(ds.tag) != "datasource":
        continue
    dsname = ds.get("caption") or ds.get("name")
    if ds.get("name") == "Parameters":
        continue
    printed_header = False
    for col in ds.iter():
        if local(col.tag) != "column":
            continue
        calc = None
        for child in col:
            if local(child.tag) == "calculation":
                calc = child
                break
        if calc is None:
            continue
        formula = calc.get("formula")
        if formula is None:
            continue
        if not printed_header:
            print(f"\n### DATASOURCE: {dsname!r} (name={ds.get('name')!r})")
            printed_header = True
        cap = col.get("caption") or col.get("name")
        print(f"\n  [{cap}]  (name={col.get('name')})  datatype={col.get('datatype')} role={col.get('role')} type={col.get('type')}")
        print(f"    formula: {formula}")

print("\n" + "=" * 100)
print("CROSS-DATASOURCE / BLEND relationships (<relationships> / <mapping>)")
print("=" * 100)
for el in root.iter():
    lt = local(el.tag)
    if lt in ("relationships", "relationship", "mapping", "expression"):
        # print relationship maps if present
        pass
# Tableau blend links live under a top-level <mapsources> or per-worksheet
# <datasource-dependencies>; also as <cols> alias maps. Dump any explicit
# relationship/mapping elements found.
for el in root.iter():
    if local(el.tag) == "relation" and el.get("type") in ("join", "union"):
        print(f"  RELATION type={el.get('type')} name={el.get('name')!r}")
        for c in el:
            if local(c.tag) == "clause":
                print(f"    clause: {ET.tostring(c, encoding='unicode')[:300]}")

# Explicit blend link fields: look for <_.fcp.ObjectModelEncapsulateLegacy...> or
# <column-instance> - simplest signal is the <datasource-dependencies> in worksheets
# referencing multiple datasources. Instead, dump the workbook-level <mapsource>.
print("\n-- worksheets referencing >1 datasource (blend candidates) --")
for ws in root.iter():
    if local(ws.tag) != "worksheet":
        continue
    dsdeps = set()
    for dep in ws.iter():
        if local(dep.tag) == "datasource-dependencies":
            dsdeps.add(dep.get("datasource"))
    if len(dsdeps) > 1:
        print(f"  worksheet {ws.get('name')!r}: datasources={sorted(dsdeps)}")
