import re, glob, os

root = r"fabric\WindEnergyUtilization.SemanticModel\definition"
files = sorted(glob.glob(os.path.join(root, "tables", "*.tmdl")))
decl = re.compile(r"^(?P<indent>\t*)(?P<kind>table|column|measure) ")
missing = {"table": [], "column": [], "measure": []}
counts = {"table": 0, "column": 0, "measure": 0}

for f in files:
    lines = open(f, encoding="utf-8").read().splitlines()
    tname = None
    for i, ln in enumerate(lines):
        m = decl.match(ln)
        if not m:
            continue
        kind = m.group("kind")
        rest = ln.strip()[len(kind) + 1:].strip()
        name = rest.split(" = ")[0].strip().strip("'")
        if kind == "table":
            tname = name
        counts[kind] += 1
        j = i - 1
        has_doc = False
        while j >= 0:
            s = lines[j].strip()
            if s == "":
                j -= 1
                continue
            if s.startswith("///"):
                has_doc = True
            break
        if not has_doc:
            label = name if kind == "table" else f"{tname}[{name}]"
            missing[kind].append(label)

for kind in ("table", "column", "measure"):
    tot = counts[kind]
    miss = missing[kind]
    tail = "" if not miss else f"  MISSING: {miss}"
    print(f"{kind:8s}: {tot - len(miss):3d}/{tot:3d} described{tail}")
