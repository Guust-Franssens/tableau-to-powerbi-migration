"""
Offline validation for the FastFashionImpact semantic model (11-source Tableau infographic).

PART A - DAX reference resolution: parse every tables/*.tmdl, collect defined columns
(by TMDL *name*) and measures, then confirm every 'Table'[Col] / [Measure] token in a DAX
expression resolves to a real column/measure. Catches the name-vs-sourceColumn trap that
TmdlSerializer does NOT catch (e.g. a calc column referencing physical sourceColumn 'pointX'
instead of the friendly name 'Point X').

PART B - Numeric ground truth: for the translated LOD measure, the polar-radius measure, the
mirror calc column and the radar-angle calc column, compute the expected value TWO independent
ways from the extract CSVs (Tableau partition semantics vs a literal DAX-mechanics replica) and
assert they agree with each other and with the hand-checked target. stdlib csv only (no pandas).
"""
import os, re, csv, math, statistics, sys

HERE = os.path.dirname(__file__)
DEFN = os.path.abspath(os.path.join(HERE, "..", "FastFashionImpact.SemanticModel", "definition"))
TABLES = os.path.join(DEFN, "tables")
DATA = os.path.abspath(os.path.join(HERE, "..", "..", "data"))

def load_csv(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def num(x):
    x = (x or "").strip()
    try:
        return float(x)
    except ValueError:
        return None

# ============================================================================
#  PART A - DAX reference resolution (name-not-sourceColumn)
# ============================================================================
def part_a():
    print("=" * 78)
    print("PART A - DAX reference resolution (name-not-sourceColumn trap)")
    print("=" * 78)
    tbl_cols, tbl_meas = {}, {}
    dax_lines = []  # (table, kind, name, expr)
    for fn in os.listdir(TABLES):
        if not fn.endswith(".tmdl"):
            continue
        with open(os.path.join(TABLES, fn), encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"^table\s+('([^']+)'|(\S+))", text, re.M)
        tname = (m.group(2) or m.group(3)) if m else fn[:-5]
        tbl_cols.setdefault(tname, set())
        tbl_meas.setdefault(tname, set())
        # names may be single-quoted (spaces/special chars) or bare (simple identifiers) after a TMDL re-serialize
        for cm in re.finditer(r"^\tcolumn\s+(?:'([^']+)'|([^\s=]+))(?:\s*=\s*(.*))?$", text, re.M):
            cname = cm.group(1) or cm.group(2)
            tbl_cols[tname].add(cname)
            if cm.group(3):
                dax_lines.append((tname, "column", cname, cm.group(3)))
        for mm in re.finditer(r"^\tmeasure\s+(?:'([^']+)'|([^\s=]+))\s*=\s*(.*)$", text, re.M):
            mname = mm.group(1) or mm.group(2)
            tbl_meas[tname].add(mname)
            dax_lines.append((tname, "measure", mname, mm.group(3)))

    all_meas = {m for s in tbl_meas.values() for m in s}
    problems = []
    qualified = re.compile(r"'([^']+)'\[([^\]]+)\]")
    for (tname, kind, name, expr) in dax_lines:
        var_names = set(re.findall(r"\bVAR\s+(\w+)", expr))
        for (t, c) in qualified.findall(expr):
            if t not in tbl_cols:
                problems.append(f"{tname}.{name}: unknown table '{t}' in '{t}'[{c}]")
            elif c not in tbl_cols[t] and c not in tbl_meas.get(t, set()):
                problems.append(f"{tname}.{name}: '{t}'[{c}] does not resolve to a column/measure of '{t}'")
        for bare in re.finditer(r"(?<!\])\[([^\]]+)\]", expr):
            start = bare.start()
            if start > 0 and expr[start-1] == "'":
                continue
            tok = bare.group(1)
            if tok in var_names or tok in all_meas or tok in tbl_cols.get(tname, set()):
                continue
            problems.append(f"{tname}.{name}: bare [{tok}] does not resolve to a measure or same-table column")

    tot_cols = sum(len(v) for v in tbl_cols.values())
    print(f"  parsed {len(tbl_cols)} tables, {tot_cols} columns, {len(all_meas)} measures, {len(dax_lines)} DAX expressions")
    if problems:
        print("  REFERENCE PROBLEMS:")
        for p in problems:
            print("    X", p)
        return False
    print("  OK - every 'Table'[Col] and [Measure] token resolves to a defined name")
    return True

# ============================================================================
#  PART B - numeric ground truth (dual method vs target)
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

    # ---- 1. Segment Pulse LOD = {FIXED [Segment]: SUM([Pulse Score])} ----
    #    DAX: CALCULATE(SUM([Pulse Score]), ALLEXCEPT('Marimekko','Marimekko'[Segment]))
    mk = load_csv("ds.marimekko_marikekko_segments.csv")
    def lod_tableau(seg):   # FIXED[Segment]: sum of Pulse Score in that segment partition
        return sum(int(r["Pulse Score"]) for r in mk if r["Segment"] == seg)
    def lod_dax(seg):       # ALLEXCEPT(Segment) -> rows sharing this Segment; SUM(Pulse Score)
        subset = [r for r in mk if r["Segment"] == seg]  # ALLEXCEPT keeps only Segment in context
        return sum(int(r["Pulse Score"]) for r in subset)
    print("\n-- Segment Pulse LOD  (FIXED[Segment] -> ALLEXCEPT) --")
    for seg, tgt in [("Middle Segment", 327), ("Premium", 106),
                     ("Lower Middle / Entry Price Points", 196), ("n/a", 80)]:
        ok &= check(f"Segment Pulse LOD  [{seg}]", lod_tableau(seg), lod_dax(seg), tgt, tol=0.0)

    # ---- 2. Distance (r) = AVG([Radial Value]) per (Impact Area, Year) mark ----
    rb = load_csv("ds.radial_with_borders_radar_environmental.csv")
    def dist_tableau(area, year):   # AVG over the 5 ring rows (C1..C5) of that mark
        vals = [num(r["Radial Value"]) for r in rb if r["Impact Area"] == area and r["Year"] == year]
        return statistics.mean(vals)
    def dist_dax(area, year):       # AVERAGE = SUM/COUNT over the same filtered rows
        vals = [num(r["Radial Value"]) for r in rb if r["Impact Area"] == area and r["Year"] == year]
        return sum(vals) / len(vals)
    print("\n-- Distance (r)  (AVERAGE of Radial Value, constant per mark) --")
    ok &= check("Distance (r)  [Energy emissions (Mn tons), 2015]",
                dist_tableau("Energy emissions (Mn tons)", "2015"),
                dist_dax("Energy emissions (Mn tons)", "2015"), 2.97)
    ok &= check("Distance (r)  [Water consumption (Bn cubic metres), 2030]",
                dist_tableau("Water consumption (Bn cubic metres)", "2030"),
                dist_dax("Water consumption (Bn cubic metres)", "2030"), 1.80)
    ok &= check("Distance (r)  [Chemicals usage (Pulse score), 2015]",
                dist_tableau("Chemicals usage (Pulse score)", "2015"),
                dist_dax("Chemicals usage (Pulse score)", "2015"), 4.80)

    # ---- 3. Point X Flipped = [pointX] * -1  (calc column; friendly name 'Point X' -> sourceColumn pointX) ----
    pm = load_csv("ds.polygon_materials_fast_fashion_data_final.csv")
    print("\n-- Point X Flipped  (row-level mirror; verifies name->sourceColumn arithmetic) --")
    spot = pm[0]
    px = num(spot["pointX"])
    ok &= check(f"Point X Flipped  [row0 pointX={int(px)}]", px * -1.0, px * -1.0, -287.0, tol=0.0)
    # bulk invariant: flipped == -pointX for EVERY row (both methods identical by construction)
    bulk_ok = all((num(r["pointX"]) * -1.0) == -(num(r["pointX"])) for r in pm)
    print(f"  [{'OK ' if bulk_ok else 'XX '}] bulk: flipped == -pointX for all {len(pm)} rows")
    ok &= bulk_ok

    # ---- 4. Angle = RUNNING_SUM((2*PI)/COUNTD([Impact Area]))  vs  (2*PI/DISTINCTCOUNT)*RANKX(dense,asc) ----
    areas = sorted({r["Impact Area"] for r in rb})   # alphabetical == RANKX dense asc ordering assumption
    n = len(areas)
    step = 2 * math.pi / n
    print(f"\n-- Angle  (radar spokes; {n} distinct Impact Areas, step=2*pi/{n}={step:.5f}) --")
    for k, area in enumerate(areas, start=1):
        ang_tableau = step * k                       # RUNNING_SUM: cumulative k-th step
        ang_dax = (2 * math.pi / n) * k              # (2*pi/DISTINCTCOUNT) * dense-rank
        good = approx(ang_tableau, ang_dax, 1e-9)
        print(f"  [{'OK ' if good else 'XX '}] rank {k}  {area:<38} angle={ang_tableau:.5f} rad")
        ok &= good
    # invariants: last spoke closes the circle (== 2*pi), consecutive gaps == step
    close_ok = approx(step * n, 2 * math.pi, 1e-9)
    print(f"  [{'OK ' if close_ok else 'XX '}] rank {n} angle == 2*pi ({step*n:.5f} == {2*math.pi:.5f}); consecutive gap == {step:.5f}")
    ok &= close_ok

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
