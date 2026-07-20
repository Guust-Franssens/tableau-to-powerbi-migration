"""Extract Tableau GROUP (e.g. 'Category (group)') and BIN (e.g. 'Song Stat
Value (bin)') definitions + value aliases from the source .twb."""
import zipfile, os
import xml.etree.ElementTree as ET

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "source",
                   "StagetoScreenIronVizBroadwayMusicalsTurnedintoMovies.twbx")
with zipfile.ZipFile(SRC) as z:
    twb = [n for n in z.namelist() if n.lower().endswith(".twb")][0]
    root = ET.fromstring(z.read(twb))

def local(t): return t.split("}")[-1]

for ds in root.iter():
    if local(ds.tag) != "datasource":
        continue
    dsname = ds.get("caption") or ds.get("name")
    for col in ds.iter():
        if local(col.tag) != "column":
            continue
        cap = col.get("caption") or col.get("name")
        # GROUP fields: <column ...><calculation class='categorical-bin' ...> OR <groupfilter>
        has_group = any(local(c.tag) in ("groupfilter", "group", "members") for c in col.iter())
        # BIN fields carry attributes 'bin-size' or a <calculation class='bin'>
        binsize = col.get("bin-size") or col.get("size")
        calc = None
        for c in col:
            if local(c.tag) == "calculation":
                calc = c
        if calc is not None and calc.get("class") in ("bin", "categorical-bin"):
            print(f"[BIN/CATBIN] {dsname} :: {cap!r} class={calc.get('class')} "
                  f"decimal-formula={calc.get('decimal-formula')} formula={calc.get('formula')!r} "
                  f"peano={calc.get('peano')} size={calc.get('size')} attrs={dict(col.attrib)}")
        if has_group:
            print(f"\n[GROUP] {dsname} :: {cap!r} (name={col.get('name')})")
            for gf in col.iter():
                if local(gf.tag) == "groupfilter":
                    fn = gf.get("function"); mem = gf.get("member"); lvl = gf.get("level")
                    print(f"    groupfilter function={fn} member={mem} level={lvl}")

print("\n\n=== ALL <column> with 'bin' in name/caption ===")
for ds in root.iter():
    if local(ds.tag) != "datasource": continue
    for col in ds.iter():
        if local(col.tag) != "column": continue
        cap = (col.get("caption") or col.get("name") or "")
        if "bin" in cap.lower():
            print(f"  {cap!r}: attribs={dict(col.attrib)}")
            for c in col:
                print(f"      child <{local(c.tag)}> {dict(c.attrib)}")

print("\n\n=== value ALIASES (<aliases>/<alias>) per datasource ===")
for ds in root.iter():
    if local(ds.tag) != "datasource": continue
    dsname = ds.get("caption") or ds.get("name")
    for col in ds.iter():
        if local(col.tag) != "column": continue
        aliases = [(a.get("key"), a.get("value")) for a in col.iter() if local(a.tag) == "alias"]
        if aliases:
            print(f"  {dsname} :: {(col.get('caption') or col.get('name'))!r}: {aliases}")
