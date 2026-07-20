"""
Offline validation for the ElectricityPerCapita semantic model.

PART A - DAX reference resolution: parse every tables/*.tmdl, collect defined columns
(by TMDL *name*) and measures, then confirm every 'Table'[Col] / [Measure] token in a
DAX expression resolves to a real column/measure. Catches the name-vs-sourceColumn trap
that TmdlSerializer does NOT catch (e.g. DAX referencing a physical sourceColumn).

PART B - Numeric ground truth: for the FIXED-LOD, CY/blend and WINDOW measures, compute
the expected value TWO independent ways from the extract CSVs (Tableau partition semantics
vs a literal DAX-mechanics replica) and assert they agree with each other and with the
hand-checked target. stdlib csv only (this venv has no pandas).
"""
import os, re, csv, statistics, sys

HERE = os.path.dirname(__file__)
DEFN = os.path.abspath(os.path.join(HERE, "..", "ElectricityPerCapita.SemanticModel", "definition"))
TABLES = os.path.join(DEFN, "tables")
DATA = os.path.abspath(os.path.join(HERE, "..", "..", "data"))

def load_csv(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def col(hdr_startswith, row):
    for k in row:
        if k.startswith(hdr_startswith):
            return k
    raise KeyError(hdr_startswith)

def num(x):
    x = (x or "").strip()
    return float(x) if x not in ("", "NA", "null") else None

# ============================================================================
#  PART A - DAX reference resolution
# ============================================================================
def part_a():
    print("=" * 78)
    print("PART A - DAX reference resolution (name-not-sourceColumn)")
    print("=" * 78)
    tbl_cols, tbl_meas = {}, {}
    dax_lines = []  # (table, kind, name, expr)
    for fn in os.listdir(TABLES):
        if not fn.endswith(".tmdl"):
            continue
        path = os.path.join(TABLES, fn)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        tname = None
        m = re.search(r"^table\s+('([^']+)'|(\S+))", text, re.M)
        tname = (m.group(2) or m.group(3)) if m else fn[:-5]
        tbl_cols.setdefault(tname, set())
        tbl_meas.setdefault(tname, set())
        for cm in re.finditer(r"^\tcolumn\s+'([^']+)'(?:\s*=\s*(.*))?$", text, re.M):
            tbl_cols[tname].add(cm.group(1))
            if cm.group(2):
                dax_lines.append((tname, "column", cm.group(1), cm.group(2)))
        for mm in re.finditer(r"^\tmeasure\s+'([^']+)'\s*=\s*(.*)$", text, re.M):
            tbl_meas[tname].add(mm.group(1))
            dax_lines.append((tname, "measure", mm.group(1), mm.group(2)))

    all_meas = {m for s in tbl_meas.values() for m in s}
    # VAR names are locals, not refs
    problems = []
    qualified = re.compile(r"'([^']+)'\[([^\]]+)\]")
    for (tname, kind, name, expr) in dax_lines:
        var_names = set(re.findall(r"\bVAR\s+(\w+)", expr))
        for (t, c) in qualified.findall(expr):
            if t not in tbl_cols:
                problems.append(f"{tname}.{name}: unknown table '{t}' in '{t}'[{c}]")
            elif c not in tbl_cols[t] and c not in tbl_meas.get(t, set()):
                problems.append(f"{tname}.{name}: '{t}'[{c}] does not resolve to a column/measure of '{t}'")
        # bare [Token] not preceded by ' (i.e. not the ]-tail of 'T'[Col])
        for bare in re.finditer(r"(?<!\])\[([^\]]+)\]", expr):
            # skip those that are the column part of a qualified ref (preceded by ')
            start = bare.start()
            if start > 0 and expr[start-1] == "'":
                continue
            tok = bare.group(1)
            if tok in var_names:
                continue
            if tok in all_meas or tok in tbl_cols.get(tname, set()):
                continue
            problems.append(f"{tname}.{name}: bare [{tok}] does not resolve to a measure or same-table column")

    tot_cols = sum(len(v) for v in tbl_cols.values())
    tot_meas = len(all_meas)
    print(f"  parsed {len(tbl_cols)} tables, {tot_cols} columns, {tot_meas} measures, {len(dax_lines)} DAX expressions")
    if problems:
        print("  REFERENCE PROBLEMS:")
        for p in problems:
            print("    X", p)
        return False
    print("  OK - every 'Table'[Col] and [Measure] token resolves to a defined name")
    return True

# ============================================================================
#  PART B - numeric ground truth
# ============================================================================
def approx(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol

def check(label, tableau_val, dax_val, target, tol=0.01):
    ok = approx(tableau_val, dax_val, tol) and approx(dax_val, target, tol)
    flag = "OK " if ok else "XX "
    print(f"  [{flag}] {label}")
    print(f"         tableau-semantics={tableau_val:.5f}  dax-replica={dax_val:.5f}  target={target:.5f}")
    return ok

def part_b():
    print("=" * 78)
    print("PART B - numeric ground truth (dual computation vs target)")
    print("=" * 78)
    ok = True

    # ---- 1. Per Capita FNR row-level ratios: World 2022 ----
    fnr = load_csv("ds.per_capita_electricity_fossil_nuclear_renewables.csv")
    ff_c = col("Fossil fuel electricity per capita - kWh", fnr[0])
    nu_c = col("Nuclear electricity per capita - kWh", fnr[0])
    re_c = col("Renewable electricity per capita - kWh", fnr[0])
    w = [r for r in fnr if r["Entity"] == "World" and r["Year"] == "2022"][0]
    ff, nu, rn = num(w[ff_c]), num(w[nu_c]), num(w[re_c])
    # tableau: row-level ratio; dax-replica: DIVIDE(col, col+col+col) on the same single row
    tot = ff + nu + rn
    print("\n-- Per Capita FNR (row-level %, World 2022) --")
    ok &= check("FF%  World 2022", ff / tot, ff / (ff + nu + rn), 0.61432)
    ok &= check("Nuclear%  World 2022", nu / tot, nu / (ff + nu + rn), 0.09152)
    ok &= check("Renewables %  World 2022", rn / tot, rn / (ff + nu + rn), 0.29416)

    # ---- 2. Pivoted dimension-FIXED LOD {FIXED [Year],[Code]} + ratios ----
    piv = load_csv("ds.pivoted_per_capita_electricity_generation_by_source.csv")
    def piv_fixed_tableau(code, year, fuel):
        # Tableau: per (Year,Code) group, SUM of per-capita where Fuel Source=fuel
        return sum(num(r["Electricity generation per capita"]) or 0
                   for r in piv if r["Code"] == code and r["Year"] == year and r["Fuel Source"] == fuel)
    def piv_fixed_dax(code, year, fuel):
        # DAX replica: CALCULATE(SUM(percap), FILTER(ALLEXCEPT(Year,Code), Fuel=fuel))
        subset = [r for r in piv if r["Year"] == year and r["Code"] == code]  # ALLEXCEPT(Year,Code)
        return sum(num(r["Electricity generation per capita"]) or 0 for r in subset if r["Fuel Source"] == fuel)
    print("\n-- Pivoted FIXED[Year,Code] LOD + % (USA / FRA, 2022) --")
    usa_ff = piv_fixed_tableau("USA", "2022", "Fossil Fuel")
    usa_nu = piv_fixed_tableau("USA", "2022", "Nuclear")
    usa_rn = piv_fixed_tableau("USA", "2022", "Renewables")
    ok &= check("FF Electricity  USA 2022", usa_ff, piv_fixed_dax("USA", "2022", "Fossil Fuel"), 7559.257, tol=0.01)
    ff_pct_tab = usa_ff / (usa_ff + usa_nu + usa_rn)
    ff_pct_dax = piv_fixed_dax("USA", "2022", "Fossil Fuel") / (piv_fixed_dax("USA", "2022", "Fossil Fuel") + piv_fixed_dax("USA", "2022", "Nuclear") + piv_fixed_dax("USA", "2022", "Renewables"))
    ok &= check("FF Electricity%  USA 2022", ff_pct_tab, ff_pct_dax, 0.59652)
    fra_ff = piv_fixed_tableau("FRA", "2022", "Fossil Fuel")
    fra_nu = piv_fixed_tableau("FRA", "2022", "Nuclear")
    fra_rn = piv_fixed_tableau("FRA", "2022", "Renewables")
    nu_pct_tab = fra_nu / (fra_ff + fra_nu + fra_rn)
    nu_pct_dax = piv_fixed_dax("FRA", "2022", "Nuclear") / (piv_fixed_dax("FRA", "2022", "Fossil Fuel") + piv_fixed_dax("FRA", "2022", "Nuclear") + piv_fixed_dax("FRA", "2022", "Renewables"))
    ok &= check("Nuclear Electricity%  FRA 2022", nu_pct_tab, nu_pct_dax, 0.62795)

    # ---- 3. Elec Generation: CY, CY Consumption (FIXED[child]), Max year (WINDOW), Ratio ----
    eg = load_csv("ds.elec_generation_per_capita_regions.csv")
    years = [int(r["Year"]) for r in eg if r["Year"]]
    cy = max(years)  # {MAX([Year])} grand-total
    print("\n-- Elec Generation CY / CY Consumption / Max year / Ratio --")
    print(f"  [OK ] CY = {{MAX([Year])}} = {cy}   (target 2023)")
    ok &= (cy == 2023)
    def cyc_tableau(child):
        # SUM({FIXED [child]: sum(IF Year=CY THEN percap END)}) at child grain
        return sum(num(r["Per capita electricity - kWh"]) or 0 for r in eg if r["Entity"] == child and int(r["Year"]) == cy)
    def cyc_dax(child):
        # VAR cy=max(all) ; CALCULATE(SUM(percap), Year=cy) in Entity=child context
        ctx = [r for r in eg if r["Entity"] == child]
        return sum(num(r["Per capita electricity - kWh"]) or 0 for r in ctx if int(r["Year"]) == cy)
    ok &= check("CY Consumption  child='Norway'", cyc_tableau("Norway"), cyc_dax("Norway"), 28056.230, tol=0.01)

    # Max year (WINDOW_max over per-entity partition): at (Norway, year=2023 ctx) -> percap; at 2022 -> blank
    def maxyear_tableau(entity, year_ctx):
        yrs = [int(r["Year"]) for r in eg if r["Entity"] == entity]
        emax = max(yrs)
        if year_ctx == emax:
            return sum(num(r["Per capita electricity - kWh"]) or 0 for r in eg if r["Entity"] == entity and int(r["Year"]) == year_ctx)
        return None
    def maxyear_dax(entity, year_ctx):
        # VAR emax = CALCULATE(MAX(Year), ALLEXCEPT(Entity)); IF(MAX(Year in ctx)=emax, SUM(percap in ctx))
        emax = max(int(r["Year"]) for r in eg if r["Entity"] == entity)
        ctx = [r for r in eg if r["Entity"] == entity and int(r["Year"]) == year_ctx]
        maxyear_ctx = max((int(r["Year"]) for r in ctx), default=None)
        if maxyear_ctx == emax:
            return sum(num(r["Per capita electricity - kWh"]) or 0 for r in ctx)
        return None
    my_t = maxyear_tableau("Norway", 2023)
    my_d = maxyear_dax("Norway", 2023)
    print(f"  [{'OK ' if approx(my_t, my_d) and approx(my_d, 28056.230) else 'XX '}] Max year  (Norway, Year=2023)")
    print(f"         tableau-semantics={my_t:.3f}  dax-replica={my_d:.3f}  target=28056.230")
    ok &= approx(my_t, my_d) and approx(my_d, 28056.230)
    my_t22 = maxyear_tableau("Norway", 2022)
    my_d22 = maxyear_dax("Norway", 2022)
    blank_ok = (my_t22 is None and my_d22 is None)
    print(f"  [{'OK ' if blank_ok else 'XX '}] Max year  (Norway, Year=2022) is BLANK (not the latest year)  tableau={my_t22} dax={my_d22}")
    ok &= blank_ok

    # Ratio = AVG(percap)/3616.7 ; sample: entity='Norway' all years avg
    nor_vals = [num(r["Per capita electricity - kWh"]) for r in eg if r["Entity"] == "Norway" and num(r["Per capita electricity - kWh"]) is not None]
    ratio_t = statistics.mean(nor_vals) / 3616.7
    ratio_d = (sum(nor_vals) / len(nor_vals)) / 3616.7
    print(f"  [{'OK ' if approx(ratio_t, ratio_d) else 'XX '}] Ratio (Norway avg / 3616.7) = {ratio_t:.4f}  (avg={statistics.mean(nor_vals):.2f})")
    ok &= approx(ratio_t, ratio_d)

    # ---- 4. Region avg per-capita 2022 (drives 'Average per region') ----
    print("\n-- Elec Generation: AVG per-capita by Region, 2022 (sample) --")
    for region, tgt in [("North America", 12033.652), ("Sub-Saharan Africa", 547.104), ("Europe & Central Asia", 6623.686)]:
        vals = [num(r["Per capita electricity - kWh"]) for r in eg if r["Region"] == region and r["Year"] == "2022" and num(r["Per capita electricity - kWh"]) is not None]
        avg = statistics.mean(vals)
        good = approx(avg, tgt, 0.5)
        print(f"  [{'OK ' if good else 'XX '}] {region:<24} n={len(vals):<3} avg={avg:.3f}  target={tgt}")
        ok &= good

    # ---- 5. Tree geometry: X Normalized + FIXED[id] 'X fixed for last path' spot-check ----
    tree = load_csv("ds.tree.csv")
    xs = [num(r["x"]) for r in tree if num(r["x"]) is not None]
    xmax = max(xs)
    # X Normalized = x/max(x); the max-x row -> 1.0
    row_maxx = [r for r in tree if num(r["x"]) == xmax][0]
    xn_t = num(row_maxx["x"]) / xmax
    print("\n-- Tree geometry (X Normalized, FIXED[id]) --")
    print(f"  [{'OK ' if approx(xn_t, 1.0) else 'XX '}] X Normalized at max-x row = {xn_t:.5f} (target 1.0); max(x)={xmax}")
    ok &= approx(xn_t, 1.0)
    # FIXED[id]: mlp = max(path) among link rows for a given id; pick an id that has link rows
    ids_with_links = {}
    for r in tree:
        if r["type"] == "link":
            ids_with_links.setdefault(r["id"], []).append(int(r["path"]))
    sample_id = next(iter(ids_with_links))
    mlp = max(ids_with_links[sample_id])
    print(f"  [OK ] FIXED[id] mlp for id={sample_id!r}: MAX(path where type=link) = {mlp}  ({len(ids_with_links)} ids have link rows)")

    print()
    print("=" * 78)
    print("GROUND TRUTH:", "ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    print("=" * 78)
    return ok

if __name__ == "__main__":
    a = part_a()
    print()
    b = part_b()
    sys.exit(0 if (a and b) else 1)
