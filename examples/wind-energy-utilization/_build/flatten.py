import json, sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

BASE = str(_REPO / "migrations" / "wind-energy-utilization")
spec = json.load(open(BASE + r"\migration-spec.json", encoding="utf-8"))

W, H = 1400.0, 960.0
SX, SY = W/100000.0, H/100000.0

# worksheet_id -> (name, mark_type)
ws = {}
for w in spec["worksheets"]:
    ws[w["id"]] = (w.get("name"), w.get("mark_type"))

# field_id -> caption
fld = {}
for ds in spec["data_sources"]:
    for f in ds["fields"]:
        fld[f["id"]] = f.get("caption") or f.get("name")

leaves = []
def walk(z, depth=0):
    kids = z.get("children")
    # normalize children (spec sometimes has string junk)
    if isinstance(kids, list):
        childlist = [c for c in kids if isinstance(c, dict)]
    else:
        childlist = []
    is_leaf = bool(z.get("worksheet_id") or z.get("field_id") or (z.get("text_html")))
    if is_leaf:
        px = z["x"]*SX; py = z["y"]*SY; pw = z["w"]*SX; ph = z["h"]*SY
        kind = "worksheet" if z.get("worksheet_id") else ("param" if z.get("field_id") else "text")
        label = ""
        if z.get("worksheet_id"):
            nm = ws.get(z["worksheet_id"], (z["worksheet_id"], "?"))
            label = f"{nm[0]} [{nm[1]}]"
        elif z.get("field_id"):
            label = "PARAM:" + str(fld.get(z["field_id"], z["field_id"]))
        else:
            t = (z.get("text_html") or "").strip().replace("\n"," ")
            label = "TEXT:" + (t[:60])
        leaves.append({
            "id": z.get("id"), "kind": kind, "type": z.get("type"),
            "x": round(px,1), "y": round(py,1), "w": round(pw,1), "h": round(ph,1),
            "x2": round(px+pw,1), "y2": round(py+ph,1),
            "bg": z.get("background_color"),
            "label": label
        })
    for c in childlist:
        walk(c, depth+1)

walk(spec["dashboards"][0]["zones"])

# sort by y then x
leaves.sort(key=lambda l: (l["y"], l["x"]))
print(f"TOTAL LEAVES: {len(leaves)}\n")
for l in leaves:
    print(f'{l["id"]:>4} {l["kind"]:<9} x={l["x"]:>6.0f} y={l["y"]:>6.0f} w={l["w"]:>5.0f} h={l["h"]:>5.0f}  (x2={l["x2"]:>6.0f} y2={l["y2"]:>6.0f})  {l["label"]}')

json.dump(leaves, open(BASE + r"\_build\leaves.json","w",encoding="utf-8"), indent=2)
print("\nwrote _build/leaves.json")
