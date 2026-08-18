import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
BASE = str(_REPO / "migrations" / "wind-energy-utilization")
spec = json.load(open(BASE + r"\migration-spec.json", encoding="utf-8"))
fld = {}
for ds in spec["data_sources"]:
    for f in ds["fields"]:
        fld[f["id"]] = f.get("caption") or f.get("name")

def cap(fid):
    if fid is None: return None
    return fld.get(fid, fid)

want = {"spiral","wind_vs_output","total_output_bars","turbine_heatmap","map",
        "highest_performers","lowest_performers","page_1","capacity_owner",
        "turbine_baseline","months_hl","months","selected_turbine","co2_saved",
        "cm_total_output","max_total_output","min_total_output","t_performance_ratio",
        "no_of_turbines","today","selected_month","all_year"}

for w in spec["worksheets"]:
    nm = (w.get("name") or "").strip()
    key = nm.lower().replace(" ","_").replace(".","").replace("(","").replace(")","").replace("/","_")
    # match loosely
    if not any(t in key for t in want):
        continue
    print("="*80)
    print(f'WS: "{nm}"  id={w.get("id")}  mark={w.get("mark_type")}')
    enc = w.get("encodings") or {}
    for shelf, items in enc.items():
        if not items: continue
        vals = []
        for it in (items if isinstance(items,list) else [items]):
            if isinstance(it, dict):
                vals.append(f'{cap(it.get("field_id"))}|{it.get("aggregation") or ""}|{it.get("field_type") or ""}')
            else:
                vals.append(str(it))
        print(f'   {shelf}: {vals}')
    rl = w.get("reference_lines")
    if rl: print(f'   reference_lines: {json.dumps(rl)[:300]}')
    piv = w.get("measure_names_values_pivot")
    if piv: print(f'   PIVOT: {json.dumps(piv)[:300]}')
    flt = w.get("filters")
    if flt:
        for f in flt:
            note = f.get("note")
            print(f'   filter: {cap(f.get("field_id"))}  note={note}')
    tt = w.get("customized_tooltip_text")
    if tt: print(f'   tooltip: {tt[:120]}')
