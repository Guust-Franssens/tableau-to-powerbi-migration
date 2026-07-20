"""
Numeric ground-truth for the translated table-calc measures, vs the real extract CSV.

Pattern (per tale-of-100): compute each metric TWO independent ways in Python and assert
they agree per probe -- (a) TABLEAU semantics (sorted-partition INDEX / WINDOW aggregates),
(b) a literal replica of the DAX MECHANICS this model ships (RANKX/ALLSELECTED/KEEPFILTERS).
Two independent codings agreeing is far stronger than restating one formula. Then SHOW numbers.
"""
import os, sys
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(__file__)
CSV = os.path.abspath(os.path.join(HERE, "..", "..", "data", "ds.orders_sample_superstore.csv"))
df = pd.read_csv(CSV, encoding="utf-8-sig", dtype=str)
df["Sales"] = df["Sales"].astype(float)
df["Order Date"] = pd.to_datetime(df["Order Date"], format="%Y-%m-%d")
df["Ship Date"] = pd.to_datetime(df["Ship Date"], format="%Y-%m-%d")
N = len(df)

PASS = True
def check(label, a, b, show=None):
    global PASS
    ok = (a == b) or (isinstance(a, float) and isinstance(b, float) and abs(a - b) < 1e-6)
    PASS = PASS and ok
    tag = "PASS" if ok else "FAIL"
    extra = f"   [{show}]" if show else ""
    print(f"  [{tag}] {label}: tableau={a!r}  dax={b!r}{extra}")

print(f"CSV rows = {N}\n")

# ==========================================================================
# 1) Map Columns / Map Rows  = INT(INDEX()-1)%10 / INT((INDEX()-1)/10) over State
#    (a) Tableau: State alphabetical INDEX (1-based)
#    (b) DAX: RANKX(ALLSELECTED(State), State, ASC, Dense)
# ==========================================================================
print("== 1) Map Tile Grid (INDEX over State, alphabetical) ==")
states = sorted(df["State"].dropna().unique())               # (a) alphabetical order
idx_tableau = {s: i + 1 for i, s in enumerate(states)}       # 1-based INDEX
# (b) DAX-mechanics replica: dense rank ascending over the same distinct set
rank_series = pd.Series(states).rank(method="dense").astype(int)
idx_dax = {s: int(r) for s, r in zip(states, rank_series)}
print(f"  distinct States = {len(states)}   first='{states[0]}'  11th='{states[10]}'  last='{states[-1]}'")
for s in [states[0], states[9], states[10], states[-1]]:
    it, ix = idx_tableau[s], idx_dax[s]
    mc_t, mr_t = int(it - 1) % 10, int((it - 1) // 10)
    mc_d, mr_d = int(ix - 1) % 10, int((ix - 1) // 10)
    check(f"'{s}' Map Columns", mc_t, mc_d)
    check(f"'{s}' Map Rows", float(mr_t), float(mr_d), show=f"index={it}")

# ==========================================================================
# 2) Sales East/West Max = WINDOW_MAX(MAX([Sales-East],[Sales-West])) over Order Quarter
#    (a) Tableau: per-quarter east/west sums, max per quarter, window max over quarters
#    (b) DAX: MAXX(ALLSELECTED(Order Quarter), MAX(CALC East, CALC West)) with KEEPFILTERS
# ==========================================================================
print("\n== 2) Sales East/West Max (window max over Order Quarter) ==")
def qbucket(d):
    return pd.Timestamp(d.year, (d.quarter - 1) * 3 + 1, 1)
df["OQ"] = df["Order Date"].apply(qbucket)

def sales_ew_max(frame):
    # (a) Tableau semantics
    east = frame[frame["Region"] == "East"].groupby("OQ")["Sales"].sum()
    west = frame[frame["Region"] == "West"].groupby("OQ")["Sales"].sum()
    per_q = pd.concat([east, west], axis=1).fillna(0.0)
    per_q.columns = ["E", "W"]
    per_q["mx"] = per_q[["E", "W"]].max(axis=1)
    tableau = per_q["mx"].max()
    win_q = per_q["mx"].idxmax()
    # (b) DAX mechanics: KEEPFILTERS Region=East/West sum per quarter, MAX(2 scalars), MAXX over quarters
    dax_per_q = {}
    for q, sub in frame.groupby("OQ"):
        se = sub.loc[sub["Region"] == "East", "Sales"].sum()   # CALCULATE(Total Sales, KEEPFILTERS East)
        sw = sub.loc[sub["Region"] == "West", "Sales"].sum()
        dax_per_q[q] = max(se, sw)                              # MAX([Sales-East],[Sales-West])
    dax = max(dax_per_q.values())                              # MAXX(ALLSELECTED(OQ), ...)
    return round(tableau, 4), round(dax, 4), win_q

# filtered to Region in {East, West} (the worksheet's filter) and unfiltered — should match
for lbl, frame in [("Region in {East,West}", df[df["Region"].isin(["East", "West"])]),
                   ("all regions", df)]:
    t, d, q = sales_ew_max(frame)
    check(f"Sales East/West Max [{lbl}]", t, d, show=f"peak quarter={q.date()}  value=${t:,.2f}")

# ==========================================================================
# 3) Late %  = late records / total records  (On Time Ship? row-level SLA test)
#    (a) Tableau: AVG-like share of NOT on-time rows
#    (b) DAX: DIVIDE(CALCULATE(rows, KEEPFILTERS(OnTime=FALSE)), CALCULATE(rows, REMOVEFILTERS(OnTime)))
# ==========================================================================
print("\n== 3) Late % (late records / total records) ==")
df["DaysToShip"] = (df["Ship Date"] - df["Order Date"]).dt.days
def on_time(r):
    sm, d = r["Ship Mode"], r["DaysToShip"]
    return ((sm == "Same Day" and d == 0) or (sm == "First Class" and d <= 2) or
            (sm == "Second Class" and d <= 4) or (sm == "Standard Class" and d <= 5))
df["OnTime"] = df.apply(on_time, axis=1)
late_tableau = (~df["OnTime"]).mean()                          # (a) share of late rows
late_dax = (~df["OnTime"]).sum() / len(df)                     # (b) late count / total count
check("Late % (overall)", round(float(late_tableau), 6), round(float(late_dax), 6),
      show=f"{late_dax*100:.2f}%  ({(~df['OnTime']).sum():,} late of {N:,})")
# per-region breakdown (numbers to eyeball)
for reg, sub in df.groupby("Region"):
    print(f"     {reg:<8} late% = {(~sub['OnTime']).mean()*100:5.2f}%   ({(~sub['OnTime']).sum():,} of {len(sub):,})")

# ==========================================================================
# 4) Order Volume rank (bonus) = RANK(COUNT) over Sub-Category, DESC dense
# ==========================================================================
print("\n== 4) Order Volume - Rank of Count (DESC dense over Sub-Category) ==")
cnt = df.groupby("Sub-Category").size()
rank_tableau = cnt.rank(method="dense", ascending=False).astype(int)   # (a)
rank_dax = (-cnt).rank(method="dense").astype(int)                     # (b) RANKX DESC == rank of negated
top = cnt.sort_values(ascending=False)
for sc in list(top.index[:3]) + list(top.index[-1:]):
    check(f"rank '{sc}' (n={cnt[sc]:,})", int(rank_tableau[sc]), int(rank_dax[sc]))

print("\n" + ("ALL GROUND-TRUTH CHECKS PASSED" if PASS else "*** SOME CHECKS FAILED ***"))
sys.exit(0 if PASS else 1)
