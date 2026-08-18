"""Numeric ground-truth: replicate each translated measure's DAX semantics in pandas
against the extracted CSVs, and compare to the stored target. Proves the measures
return the RIGHT number (not just 'a number'), including a parameter-driven measure
evaluated at its Tableau default (Month Parameter Value = 6 = June)."""
import csv, os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

D = str(_REPO / "migrations" / "wind-energy-utilization" / "data")


def load(name):
    with open(os.path.join(D, name), encoding="utf-8") as f:
        return list(csv.DictReader(f))


daily = load("daily_performance_2024.csv")
co2 = load("co2_savings_2024.csv")
turb = load("turbine_master_data.csv")


def fnum(rows, col):
    return [float(r[col]) for r in rows if r[col] not in ("", None)]


def month_of(r):
    return int(r["date"][5:7])  # 'YYYY-MM-DD'


# Default parameter selections (SELECTEDVALUE defaults baked into the param tables):
P1 = 6                         # Month Parameter Value default = June
TURB_NAME = "GRWF Turbine 18"  # Upd Turbine Name Parameter Value default
# selected turbine's id:
sel_id = next(r["turbine_id"] for r in turb if r["upd_turbine_name"] == TURB_NAME)

results = []


def check(label, computed, target, tol=0.01):
    ok = abs(computed - target) <= tol
    results.append((label, computed, target, ok))
    print(("  [%s] %-42s computed=%s target=%s" %
           ("PASS" if ok else "FAIL", label, f"{computed:,.3f}", f"{target:,.3f}")))
    return ok


print("=== NON-PARAMETER measures ===")
# Total Actual Output (2024) = SUM(energy_actual_mwh)
check("Total Actual Output (2024)", sum(fnum(daily, "energy_actual_mwh")), 453167.284)
# Total Forecast Output (2024)
check("Total Forecast Output (2024)", sum(fnum(daily, "energy_forecast_mwh")), 455969.875)
# Total Co2 Saved = SUM(co2_saved_tonnes)
check("Total Co2 Saved", sum(fnum(co2, "co2_saved_tonnes")), 169031.37, tol=0.05)
# Fleet counts / capacity
check("No of Turbines", float(len(set(r["turbine_id"] for r in turb))), 30, tol=0)
check("Active Turbines", float(sum(1 for r in turb if r["operational_status"] == "Operational")), 28, tol=0)
check("Onshore Turbines", float(sum(1 for r in turb if r["onshore_offshore"] == "Onshore")), 24, tol=0)
check("CM Total Capacity (nameplate)", sum(fnum(turb, "capacity_mw")), 130.2, tol=0.01)

print("\n=== PARAMETER-DRIVEN measures at Tableau DEFAULT (Month=6/June, Turbine=GRWF Turbine 18) ===")
june = [r for r in daily if month_of(r) == P1]
may = [r for r in daily if month_of(r) == P1 - 1]
# CM Total Output (June) = SUM June energy_actual
check("CM Total Output @June", sum(float(r["energy_actual_mwh"]) for r in june), 28240.792)
# PM Total Output (May)
check("PM Total Output @May", sum(float(r["energy_actual_mwh"]) for r in may), 36292.689)
# CM Capacity Factor (June) = AVG(cap_factor_actual)/100
jcf = [float(r["capacity_factor_actual"]) for r in june]
check("CM Capacity Factor @June", (sum(jcf) / len(jcf)) / 100, 0.300671, tol=1e-4)
# CM Performance Ratio (June)
jpr = [float(r["performance_ratio"]) for r in june]
check("CM Performance Ratio @June", (sum(jpr) / len(jpr)) / 100, 0.991943, tol=1e-4)
# CM Availability (June)
jav = [float(r["availability_percent"]) for r in june]
check("CM Availability @June", (sum(jav) / len(jav)) / 100, 0.940656, tol=1e-4)
# CM CO2 Saved (Tn) default = AVERAGE(co2 over 30) [month-invariant]
c = fnum(co2, "co2_saved_tonnes")
check("CM CO2 Saved (Tn) @default", sum(c) / len(c), 5634.379, tol=0.01)

print("\n=== SELECTED-TURBINE (T) measures at default ===")
june_t = [r for r in june if r["turbine_id"] == sel_id]
# T CM Total Output (June, GRWF T18)
check("T CM Total Output @June/T18", sum(float(r["energy_actual_mwh"]) for r in june_t), 1117.319)
# T CM Performance Ratio (Abs) (June, GRWF T18) = AVG(performance_ratio) no /100
tpr = [float(r["performance_ratio"]) for r in june_t]
abs_pr = sum(tpr) / len(tpr)
check("T CM Perf Ratio (Abs) @June/T18", abs_pr, 104.676, tol=0.01)
# Spiral Length = INT(abs_pr)
check("Spiral Length @June/T18", float(int(abs_pr)), 104, tol=0)

print("\n=== source-bug FAITHFULNESS checks (documented, not 'fixed') ===")
# T PM Capacity Factor (May, T18) has NO /100 -> raw ~30 (vs T CM /100 ~0.30) => T MoM CF ~ -0.99
may_t = [r for r in may if r["turbine_id"] == sel_id]
t_cm_cf = (sum(float(r["capacity_factor_actual"]) for r in june_t) / len(june_t)) / 100
t_pm_cf_raw = sum(float(r["capacity_factor_actual"]) for r in may_t) / len(may_t)  # no /100
mom_cf = (t_cm_cf / t_pm_cf_raw) - 1
print(f"  T CM CapFac(/100)={t_cm_cf:.4f}  T PM CapFac(raw,no /100)={t_pm_cf_raw:.2f}  =>  T Neg MoM CapFac={mom_cf:.4f}  (100x mismatch, faithful)")
# T CO2 MoM = 0 (annual co2, CM==PM)
t_co2 = float(next(r["co2_saved_tonnes"] for r in co2 if r["turbine_id"] == sel_id))
print(f"  T CM CO2 = T PM CO2 = {t_co2:,.2f}  =>  T Neut MoM CO2 = (co2/co2)-1 = 0.0  (annual grain, faithful)")

npass = sum(1 for _, _, _, ok in results if ok)
print(f"\n{npass}/{len(results)} ground-truth checks PASSED")
if npass != len(results):
    raise SystemExit("GROUND-TRUTH MISMATCH")
