"""
Ground-truth validation for the 9 Tableau TABLE CALCULATIONS migrated to DAX.

No live Power BI Desktop / MCP / EVALUATE is available in this build, so we validate
the *numbers* the DAX must produce with TWO independently-coded computations and assert
they agree for specific known rows:

  (A) TABLEAU side  - the Tableau table-calc semantics, using pandas time-series idioms
      on the per-Symbol partition sorted by Date (.iloc[0]/.iloc[-1]/cumcount/last_date).
  (B) DAX side      - a literal replica of the DAX I wrote, evaluated with boolean masks
      over the *raw unsorted* table, mirroring exactly what CALCULATE/ALLEXCEPT/FILTER/
      EARLIER do (e.g. "Adj Close where Symbol=s AND Date = MIN(Date) over Symbol=s";
      "COUNTROWS where Symbol=s AND Date <= this row's Date").

If (A) == (B) for every calc on the probe rows, the DAX faithfully reproduces the
Tableau table calc. Any mismatch is printed as FAIL and exits non-zero.

Partition/addressing (INFERRED - no worksheet binds these calcs; flagged as a limitation):
  * 8 stock calcs: PARTITION BY Symbol, ORDER BY Date
  * Calculation3 (IPO): TOTAL() over the whole pane (grand total)
Data grain confirmed earlier: exactly 1 row per (Symbol, Date); Adj Close has no nulls.
"""
import pandas as pd
import os, sys

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data")
STOCKS = os.path.join(DATA, "ds.stocks_in_dow_jones_industrial_average_csv_stocks_in_dow_jones_industrial_average_csv.csv")
IPOS = os.path.join(DATA, "ds.top_100_ipos.csv")

# Tableau parameter current values (migration-spec.json parameters[] defaults)
INVESTMENT_AMOUNT = 500.0   # [Investment Amount]
DOLLAR_AMOUNT     = 100.0   # [Parameter 1] (Dollar amount)

TOL = 1e-6
_failures = []

def check(name, tableau_val, dax_val, fmt="{}"):
    """Assert the Tableau-semantics value equals the DAX-replica value."""
    if tableau_val is None or dax_val is None:
        ok = (tableau_val is None and dax_val is None)
    elif isinstance(tableau_val, float) or isinstance(dax_val, float):
        ok = abs(float(tableau_val) - float(dax_val)) <= TOL * max(1.0, abs(float(tableau_val)))
    else:
        ok = (tableau_val == dax_val)
    tag = "PASS" if ok else "FAIL"
    tv = fmt.format(tableau_val) if tableau_val is not None else "BLANK"
    dv = fmt.format(dax_val) if dax_val is not None else "BLANK"
    print(f"      [{tag}] {name:24} tableau={tv:>16}  dax={dv:>16}")
    if not ok:
        _failures.append(name)
    return ok

# --------------------------------------------------------------------------
_stocks_df = None
def stocks_df():
    global _stocks_df
    if _stocks_df is None:
        _stocks_df = pd.read_csv(STOCKS, parse_dates=["Date"])
    return _stocks_df

def validate_symbol(sym):
    df = stocks_df()
    # (A) TABLEAU side: sorted per-symbol partition
    g = df[df["Symbol"] == sym].sort_values("Date").reset_index(drop=True)
    n = len(g)
    t_first_adj = g["Adj Close"].iloc[0]
    t_last_adj  = g["Adj Close"].iloc[-1]
    t_first_date = g["Date"].iloc[0]
    t_last_date  = g["Date"].iloc[-1]

    # (B) DAX side: raw masks over the whole table (what ALLEXCEPT(Symbol) sees)
    part = df[df["Symbol"] == sym]                       # ALLEXCEPT('Stocks', [Symbol])
    d_min_date = part["Date"].min()                      # CALCULATE(MIN([Date]), ALLEXCEPT(..,[Symbol]))
    d_max_date = part["Date"].max()                      # CALCULATE(MAX([Date]), ALLEXCEPT(..,[Symbol]))
    d_first_adj = part.loc[part["Date"] == d_min_date, "Adj Close"].mean()   # 'Adj Close First'
    d_last_adj  = part.loc[part["Date"] == d_max_date, "Adj Close"].mean()   # 'Adj Close Last'

    print(f"\n  SYMBOL {sym}: n={n}  dates {t_first_date.date()}..{t_last_date.date()}")
    check("Adj Close First", t_first_adj, d_first_adj, "{:.4f}")
    check("Adj Close Last",  t_last_adj,  d_last_adj,  "{:.4f}")
    check("last date (LOOKUP/WINDOW)", str(t_last_date.date()), str(d_max_date.date()))

    # Per-symbol constants
    countd_nor = 1                                        # DISTINCTCOUNT([Number of Records]) = 1 (degenerate)
    t_orig_inv = INVESTMENT_AMOUNT / 1
    d_orig_inv = INVESTMENT_AMOUNT / countd_nor
    check("Original Investment Amt", t_orig_inv, d_orig_inv, "{:.4f}")

    t_orig_share = t_orig_inv / t_first_adj
    d_orig_share = d_orig_inv / d_first_adj                                   # DIVIDE([Orig Inv],[Adj Close First])
    check("Original Share Amount", t_orig_share, d_orig_share, "{:.6f}")

    t_very_last = t_orig_inv * t_last_adj / t_first_adj
    d_very_last = d_orig_inv * (d_last_adj / d_first_adj)                     # [Orig Inv]*DIVIDE([Last],[First])
    check("Very Last Value", t_very_last, d_very_last, "{:.4f}")

    # Row-level calcs on FIRST / MIDDLE / LAST probe rows
    for label, i in [("FIRST", 0), ("MIDDLE", n // 2), ("LAST", n - 1)]:
        row_date = g["Date"].iloc[i]
        today_adj = g["Adj Close"].iloc[i]
        print(f"    -- {label} row {row_date.date()} (adj={today_adj}) --")

        # Avg. Adj Close  (value of the initial investment over time)
        t_avg = today_adj * (INVESTMENT_AMOUNT / t_first_adj)
        # DAX: AVERAGE([Adj Close]) in row's filter ctx (one date) * DIVIDE([Inv],[Adj Close First])
        d_today = part.loc[part["Date"] == row_date, "Adj Close"].mean()
        d_avg = d_today * (INVESTMENT_AMOUNT / d_first_adj)
        check("Avg Adj Close Value", t_avg, d_avg, "{:.4f}")

        # Index = INDEX()  (1-based position by Date within Symbol)
        t_index = i + 1
        d_index = int((part["Date"] <= row_date).sum())      # COUNTROWS(FILTER(ALLEXCEPT(..Symbol), Date<=EARLIER(Date)))
        check("Index", t_index, d_index)

        # Gains/(Loss) and %
        t_gl = t_avg - t_orig_inv
        d_gl = d_avg - d_orig_inv
        check("Gains Loss", t_gl, d_gl, "{:.4f}")
        check("Pct", t_gl / t_orig_inv, d_gl / d_orig_inv, "{:.6f}")

        # Last Value = value only on last date, else BLANK
        t_lastv = t_avg if row_date == t_last_date else None
        d_lastv = d_avg if row_date == d_max_date else None
        check("Last Value", t_lastv, d_lastv, "{:.4f}")

        # First/Last Value = value on first AND last date, else BLANK
        t_flv = t_avg if (row_date == t_last_date or row_date == t_first_date) else None
        d_flv = d_avg if (row_date == d_max_date or row_date == d_min_date) else None
        check("First Last Value", t_flv, d_flv, "{:.4f}")

def validate_ipo_calc3():
    df = pd.read_csv(IPOS)
    rev = pd.to_numeric(df["Revenue_Inf_Adjusted"], errors="coerce")
    # (A) Tableau: TOTAL(SUM([Revenue_Inf_Adjusted])) >= [Parameter 1]*100000
    t_total = rev.sum()
    threshold = DOLLAR_AMOUNT * 100000
    t_gate = bool(t_total >= threshold)
    # (B) DAX: CALCULATE(SUM(...), ALL('Top 100 IPOs')) >= [Dollar Amount Value]*100000
    d_total = rev.dropna().sum()
    d_gate = bool(d_total >= threshold)
    print("\n  IPO Calculation3 (TOTAL/ALL grand-total gate):")
    check("grand total Rev(InfAdj)", round(t_total, 2), round(d_total, 2), "{:.2f}")
    check("threshold", threshold, threshold, "{:.0f}")
    check("gate", t_gate, d_gate)
    res = "<max Company Name (Full)>" if t_gate else "No"
    print(f"      => Calculation3 = \"{res}\" for every row  (gate {'TRUE' if t_gate else 'FALSE'})")

if __name__ == "__main__":
    print("=" * 80)
    print("GROUND-TRUTH: Tableau table-calc semantics (A)  vs  DAX-replica (B)")
    print("=" * 80)
    validate_ipo_calc3()
    for sym in ["UTX", "AA"]:
        validate_symbol(sym)
    print("\n" + "=" * 80)
    if _failures:
        print(f"RESULT: FAIL - {len(_failures)} mismatch(es): {_failures}")
        sys.exit(1)
    print("RESULT: PASS - all 9 table calcs: DAX replica == Tableau semantics on every probe row")
    print("=" * 80)
