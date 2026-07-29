"""Quick data facts from the extracted CSVs (stdlib csv only - no pandas)."""
import csv, os, statistics
from collections import Counter

DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))

def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def col(rows, c):
    return [r[c] for r in rows]

# 1. per_capita_fnr
fnr = load("ds.per_capita_electricity_fossil_nuclear_renewables.csv")
ents = set(col(fnr, "Entity"))
print("FNR rows:", len(fnr), "| distinct Entity:", len(ents), "| 'World' present:", "World" in ents)
yrs = sorted(set(int(y) for y in col(fnr, "Year")))
print("FNR years:", yrs[0], "..", yrs[-1])
# World 2022 FF% ground truth
ff_col = [c for c in fnr[0].keys() if c.startswith("Fossil")][0]
nu_col = [c for c in fnr[0].keys() if c.startswith("Nuclear")][0]
re_col = [c for c in fnr[0].keys() if c.startswith("Renewable")][0]
for e in ("World",):
    for yr in (2022, 2021):
        rr = [r for r in fnr if r["Entity"] == e and int(r["Year"]) == yr]
        if rr:
            r = rr[0]
            ff, nu, re_ = float(r[ff_col]), float(r[nu_col]), float(r[re_col])
            tot = ff + nu + re_
            print(f"  {e} {yr}: FF={ff:.3f} Nuc={nu:.3f} Ren={re_:.3f} tot={tot:.3f} -> FF%={ff/tot:.5f} Nuc%={nu/tot:.5f} Ren%={re_/tot:.5f}")

# 2. pivoted
piv = load("ds.pivoted_per_capita_electricity_generation_by_source.csv")
print("\nPIVOTED rows:", len(piv), "| Fuel Source:", sorted(set(col(piv, "Fuel Source"))))
pyrs = sorted(set(int(y) for y in col(piv, "Year")))
print("PIVOTED years:", pyrs[0], "..", pyrs[-1])
# FIXED [Year],[Code] LOD ground truth for USA 2022
for code, yr in [("USA", 2022), ("FRA", 2022)]:
    sub = [r for r in piv if r["Code"] == code and int(r["Year"]) == yr]
    by = {r["Fuel Source"]: float(r["Electricity generation per capita"]) for r in sub}
    ff = by.get("Fossil Fuel", 0.0); nu = by.get("Nuclear", 0.0); rn = by.get("Renewables", 0.0)
    tot = ff + nu + rn
    if tot:
        print(f"  {code} {yr}: FF={ff:.3f} Nuc={nu:.3f} Ren={rn:.3f} -> FF%={ff/tot:.5f} Nuc%={nu/tot:.5f} Ren%={rn/tot:.5f}")

# 3. elec generation
eg = load("ds.elec_generation_per_capita_regions.csv")
egents = set(col(eg, "Entity"))
egregs = set(col(eg, "Region"))
print("\nELEC GEN rows:", len(eg), "| distinct Entity:", len(egents), "| distinct Region:", len(egregs))
eyrs = sorted(set(int(y) for y in col(eg, "Year")))
print("ELEC GEN years:", eyrs[0], "..", eyrs[-1], "| World present:", "World" in egents)
print("ELEC GEN regions:", sorted(egregs))
# Avg per-capita by region in 2022 (for 'Average per region' worksheet)
print("  Avg Per capita by Region in 2022:")
for reg in sorted(egregs):
    vals = [float(r["Per capita electricity - kWh"]) for r in eg if r["Region"] == reg and int(r["Year"]) == 2022]
    if vals:
        print(f"    {reg:35} n={len(vals):3} avg={statistics.mean(vals):.3f}")
# Global avg (all entities) 2022
allv = [float(r["Per capita electricity - kWh"]) for r in eg if int(r["Year"]) == 2022]
print(f"  GLOBAL avg per-capita 2022 (all rows): {statistics.mean(allv):.3f}  (n={len(allv)})  [Tableau constant 'Global avg in 2022'=3616.7]")

# 4. tree.csv child vs Entity
tree = load("ds.tree.csv")
egmax = max(int(y) for y in col(eg, "Year"))
print("\nTREE rows:", len(tree), "| type:", dict(Counter(col(tree, "type"))))
children = set(c for c in col(tree, "child") if c)
print("TREE distinct child:", len(children), "| ElecGen max year (CY):", egmax)
overlap_ent = children & egents
overlap_reg = children & egregs
print(f"  child in ElecGen.Entity = {len(overlap_ent)} ; child in ElecGen.Region = {len(overlap_reg)}")
print("  sample child NOT in Entity:", sorted(list(children - egents))[:12])
# CY Consumption ground-truth: FIXED [child]: sum(IF Year=CY THEN percapita) for a country/region child
for ch in ["Norway", "Iceland", "East Asia & Pacific", "World"]:
    v = [float(r["Per capita electricity - kWh"]) for r in eg if r["Entity"] == ch and int(r["Year"]) == egmax]
    print(f"  CY Consumption[child={ch!r}] (Entity match, {egmax} sum) = {sum(v):.3f}  (n={len(v)}, inChildren={ch in children})")
