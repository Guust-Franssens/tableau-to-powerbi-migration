"""
Ground-truth validation for the Telecommunications semantic model.

No live Power BI / DAX engine is available in this environment, so this script
replicates each non-trivial measure and the 'Regional Group (group)' calc-column
logic in pure Python (stdlib only) directly against the materialized extract CSV
(data/ds.cell_links.csv), and prints EXPECTED (hand-computed) vs ACTUAL so the
numbers a live model would produce can be checked.

Rounding note: DAX ROUND and Tableau ROUND are HALF-AWAY-FROM-ZERO. Python's
built-in round() is banker's rounding, so we use decimal.Decimal + ROUND_HALF_UP
to faithfully mirror the model's calc column.
"""
import csv
import os
from decimal import Decimal, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.normpath(os.path.join(HERE, "..", "..", "data", "ds.cell_links.csv"))

# Region map matches definition/tables/Cell Links.tmdl SWITCH and the
# migration-spec group aliases (str(ROUND(Lat,1)) + str(ROUND(Long,1))).
REGION_MAP = {
    (474, -1221): "South",
    (475, -1221): "South West",
    (476, -1219): "Central East",
    (476, -1223): "Central West",
    (477, -1220): "North",
    (477, -1224): "North East",
    (478, -1222): "North West",
}


def half_up_times10(x: float) -> int:
    """ROUND(x * 10, 0) with half-away-from-zero == ROUND(x,1)*10 (integer form)."""
    return int((Decimal(repr(x)) * 10).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def region_of(lat: float, lon: float) -> str:
    la, lo = half_up_times10(lat), half_up_times10(lon)
    if (la, lo) in REGION_MAP:
        return REGION_MAP[(la, lo)]
    # Ungrouped fallback: FORMAT(_la/10,"0.0") & FORMAT(_lo/10,"0.0")
    return f"{la/10:.1f}{lo/10:.1f}"


def approx(a: float, b: float, tol: float = 1e-6) -> str:
    return "MATCH" if abs(a - b) <= tol else f"*** MISMATCH (diff={a-b:g}) ***"


def main() -> None:
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    n = len(rows)
    cap = [int(r["Capacity (Mb/s)"]) for r in rows if r["Capacity (Mb/s)"] not in ("", None)]
    avail = [float(r["Availability (%)"]) for r in rows if r["Availability (%)"] not in ("", None)]
    freq = [float(r["Freq Band (GHz)"]) for r in rows if r["Freq Band (GHz)"] not in ("", None)]
    dist = [float(r["Distance (miles)"]) for r in rows if r["Distance (miles)"] not in ("", None)]

    total_cap = sum(cap)
    avg_cap = total_cap / len(cap)
    avg_avail = sum(avail) / len(avail)
    avg_freq = sum(freq) / len(freq)
    total_dist = sum(dist)

    print("=" * 72)
    print("MEASURE GROUND-TRUTH (Python replication vs model DAX)")
    print("=" * 72)
    checks = [
        ("COUNTROWS('Cell Links')            [Number of Records]", n, 808, "%d"),
        ("SUM([Capacity (Mb/s)])             [Total Capacity]", total_cap, 115212, "%d"),
        ("AVERAGE([Capacity (Mb/s)])         [Avg Capacity]", avg_cap, 142.589109, "%.6f"),
        ("AVERAGE([Availability (%)])        [Avg Availability]", avg_avail, 99.131871, "%.6f"),
        ("AVERAGE([Freq Band (GHz)])         [Avg Freq Band]", avg_freq, 13.028960, "%.6f"),
        ("SUM([Distance (miles)])            [Total Distance]", total_dist, 2416.906000, "%.6f"),
    ]
    for label, actual, expected, fmt in checks:
        a_s, e_s = fmt % actual, fmt % expected
        print(f"{label:52s} expected={e_s:>14s}  actual={a_s:>14s}  {approx(float(a_s), float(e_s), 5e-6)}")

    print()
    print("=" * 72)
    print("REGIONAL GROUP (group) CALC COLUMN + % OF TOTAL CAPACITY")
    print("=" * 72)
    by_region = {}
    for r in rows:
        reg = region_of(float(r["Lat"]), float(r["Long"]))
        c = int(r["Capacity (Mb/s)"])
        agg = by_region.setdefault(reg, [0, 0])
        agg[0] += 1
        agg[1] += c

    named = [k for k in by_region if k in REGION_MAP.values()]
    ungrouped_keys = [k for k in by_region if k not in REGION_MAP.values()]
    named_rows = sum(by_region[k][0] for k in named)
    ungrouped_rows = sum(by_region[k][0] for k in ungrouped_keys)

    print(f"Named-region rows : {named_rows}   (expected 240)   {approx(named_rows, 240, 0)}")
    print(f"Ungrouped rows    : {ungrouped_rows}   (expected 568)   {approx(ungrouped_rows, 568, 0)}")
    print(f"Distinct ungrouped coordinate labels: {len(ungrouped_keys)}")
    print()
    print(f"{'Region':16s} {'rows':>5s} {'sum(Capacity)':>14s} {'% of grand total':>18s}")
    order = sorted(named, key=lambda k: -by_region[k][1])
    for k in order:
        cnt, s = by_region[k]
        print(f"{k:16s} {cnt:5d} {s:14d} {100*s/total_cap:17.2f}%")
    ug_rows = sum(by_region[k][0] for k in ungrouped_keys)
    ug_cap = sum(by_region[k][1] for k in ungrouped_keys)
    print(f"{'(ungrouped)':16s} {ug_rows:5d} {ug_cap:14d} {100*ug_cap/total_cap:17.2f}%")
    print(f"{'GRAND TOTAL':16s} {n:5d} {total_cap:14d} {100.0:17.2f}%")

    grand = sum(v[1] for v in by_region.values())
    print()
    print(f"Sum of per-region capacity == Total Capacity : {approx(grand, total_cap, 0)} "
          f"({grand} vs {total_cap})  -> validates the ALLSELECTED denominator of [% of Total Capacity]")


if __name__ == "__main__":
    main()
