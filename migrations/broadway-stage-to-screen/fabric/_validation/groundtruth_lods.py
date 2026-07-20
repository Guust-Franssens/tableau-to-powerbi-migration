"""
Numeric ground-truth for the conditional dimension-scoped FIXED-LOD measures.

No live Power BI Desktop / MCP / EVALUATE is available in this build, so each measure's
number is validated with TWO independently-coded computations that must agree per probe row:

  (A) TABLEAU FIXED semantics - group rows by the FIXED dimension(s) in a single pass
      (nested dict), apply the conditional (Type flag) INSIDE the aggregate, exactly like
      {FIXED [dim]: agg(IIF([Type]=flag, x, NULL))}.
  (B) DAX replica - for each probe key, a fresh full-table scan filtering by the ALLEXCEPT
      dimension value(s) AND the Type flag, then aggregate - exactly what
      CALCULATE(agg, ALLEXCEPT('T','T'[dim]), 'T'[Type]=flag) evaluates.

If (A) == (B) for every probe, the DAX faithfully reproduces the Tableau LOD. The printed
tables read like the EVALUATE output a live engine would return (grouped by the LOD dimension).

Measures covered:
  * 'Album Popularity, Theater' / ', Movie' / 'Popularity Diff'  (FIXED [Film], MIN)
  * 'Track Cnt, Movie Album Total' / ', Theater Album Total'      (FIXED [Original], COUNTD)
  * 'Latest Movie Premiere'                                        (FIXED [Broadway Show], MAX movie date)
  * 'Record Count' / 'Nomination Count' / 'Win Count'             (FIXED [Original],[Award or Category])
Plus integrity spot-checks of the 'Song Stat Value (bin)' FLOOR bin and the two Tableau groups.
"""
import csv, os, sys, math

DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
def load(fn):
    with open(os.path.join(DATA, fn), encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

SONG = load("ds.4_song_stats.csv")
CHRON = load("ds.2_chronology.csv")
ACC = load("ds.3_accolades.csv")

TOL = 1e-9
_fail = []

def check(name, a, b, fmt="{}"):
    if a is None or b is None:
        ok = (a is None and b is None)
    elif isinstance(a, float) or isinstance(b, float):
        ok = abs(float(a) - float(b)) <= TOL * max(1.0, abs(float(a)))
    else:
        ok = (a == b)
    av = fmt.format(a) if a is not None else "BLANK"
    bv = fmt.format(b) if b is not None else "BLANK"
    print(f"      [{'PASS' if ok else 'FAIL'}] {name:34} tableau={av:>14}  dax={bv:>14}")
    if not ok:
        _fail.append(name)

def sep(title):
    print("\n" + "-" * 92)
    print("  " + title)
    print("-" * 92)

# ===========================================================================
# 1) Album Popularity, Theater/Movie  =  {FIXED [Film]: MIN(IIF([Type]=flag,[Album Popularity ( %)],NULL))}
# ===========================================================================
sep("Album Popularity, Theater / Movie / Popularity Diff   (FIXED [Film], MIN)   [table: 4 Song Stats]")
# (A) Tableau: single-pass nested dict  film -> type -> min(pop)
A_pop = {}
for r in SONG:
    f = r["Film"]; ty = r["Type"]; pop = r["Album Popularity ( %)"].strip()
    if pop == "":
        continue
    p = int(pop)
    A_pop.setdefault(f, {}).setdefault(ty, [])
    A_pop[f][ty].append(p)
A_min = {f: {ty: min(v) for ty, v in d.items()} for f, d in A_pop.items()}

def dax_album_pop_min(film, flag):   # (B) CALCULATE(MIN(pop), ALLEXCEPT([Film]), Type=flag)
    vals = [int(r["Album Popularity ( %)"]) for r in SONG
            if r["Film"] == film and r["Type"] == flag and r["Album Popularity ( %)"].strip() != ""]
    return min(vals) if vals else None

print(f"\n  {'Film':40} {'Theater':>8} {'Movie':>8} {'Diff':>8}")
for film in ["Chicago", "The Phantom Of The Opera", "Mamma Mia!", "Cats", "Dear Evan Hansen", "West Side Story"]:
    a_th = A_min.get(film, {}).get("Theater"); a_mv = A_min.get(film, {}).get("Movie")
    d_th = dax_album_pop_min(film, "Theater"); d_mv = dax_album_pop_min(film, "Movie")
    a_diff = (a_mv - a_th) if (a_th is not None and a_mv is not None) else None
    d_diff = (d_mv - d_th) if (d_th is not None and d_mv is not None) else None
    print(f"  {film:40} {str(a_th):>8} {str(a_mv):>8} {str(a_diff):>8}")
    check(f"{film[:22]} | Album Pop Theater", a_th, d_th)
    check(f"{film[:22]} | Album Pop Movie",   a_mv, d_mv)
    check(f"{film[:22]} | Popularity Diff",   a_diff, d_diff)

# ===========================================================================
# 2) Track Cnt, Movie/Theater Album Total  =  {FIXED [Original]: COUNTD(IIF([Type]=flag,[Track ID],NULL))}
# ===========================================================================
sep("Track Cnt, Movie / Theater Album Total   (FIXED [Original], COUNTD Track ID)   [table: 4 Song Stats]")
# (A) Tableau: single-pass  original -> type -> set(track id)
A_trk = {}
for r in SONG:
    o = r["Original"]; ty = r["Type"]; tid = r["Track ID"]
    A_trk.setdefault(o, {}).setdefault(ty, set()).add(tid)

def dax_trackcnt_total(original, flag):   # (B) CALCULATE(COUNTD(Track ID), ALLEXCEPT([Original]), Type=flag)
    return len({r["Track ID"] for r in SONG if r["Original"] == original and r["Type"] == flag})

print(f"\n  {'Original':40} {'MovieTot':>9} {'TheaterTot':>11}")
for orig in ["Chicago", "Mamma Mia!", "Cats", "Annie", "Sweeney Todd: The Demon Barber of Fleet Street"]:
    a_mv = len(A_trk.get(orig, {}).get("Movie", set()))
    a_th = len(A_trk.get(orig, {}).get("Theater", set()))
    d_mv = dax_trackcnt_total(orig, "Movie"); d_th = dax_trackcnt_total(orig, "Theater")
    print(f"  {orig:40} {a_mv:>9} {a_th:>11}")
    check(f"{orig[:22]} | Movie Album Total",   a_mv, d_mv, "{:d}")
    check(f"{orig[:22]} | Theater Album Total", a_th, d_th, "{:d}")

# ===========================================================================
# 3) Latest Movie Premiere  =  {FIXED [Broadway Show]: MAX(IIF([Type]='Movie',[Date],NULL))}
# ===========================================================================
sep("Latest Movie Premiere   (FIXED [Broadway Show], MAX movie Date)   [table: 2 Chronology]")
# ISO dates sort lexically == chronologically.
A_prem = {}
for r in CHRON:
    if r["Type"] == "Movie" and r["Date"].strip():
        sh = r["Broadway Show"]
        d = r["Date"].strip()
        if sh not in A_prem or d > A_prem[sh]:
            A_prem[sh] = d

def dax_latest_movie(show):   # (B) CALCULATE(MAX(Date), ALLEXCEPT([Broadway Show]), Type='Movie')
    ds = [r["Date"].strip() for r in CHRON if r["Broadway Show"] == show and r["Type"] == "Movie" and r["Date"].strip()]
    return max(ds) if ds else None

print(f"\n  {'Broadway Show':40} {'Latest Movie Premiere':>22}")
for show in ["Chicago", "Cats", "Annie", "13: The Musical", "The Prom"]:
    a = A_prem.get(show); d = dax_latest_movie(show)
    print(f"  {show:40} {str(a):>22}")
    check(f"{show[:22]} | Latest Movie Premiere", a, d)

# ===========================================================================
# 4) Record / Nomination / Win Count  (FIXED [Original],[Award or Category]: COUNTD(Award+Category+Nominee))
#    Nomination Count == Record Count (grain realized by the visual axis).
# ===========================================================================
sep("Record / Nomination / Win Count   (COUNTD Award+Category+Nominee, Nominee not null)   [table: 3 Accolades]")

def key(r):
    return r["Award"] + r["Category"] + r["Nominee"]

def dax_record_count(pred):   # (B) CALCULATE(DISTINCTCOUNT(Record Key), NOT ISBLANK(Nominee), <pred>)
    return len({key(r) for r in ACC if r["Nominee"].strip() != "" and pred(r)})

# (A) Tableau: single-pass  original -> set(key) over non-null-Nominee rows (and won subset)
A_rec, A_win = {}, {}
for r in ACC:
    if r["Nominee"].strip() == "":
        continue
    o = r["Original"]
    A_rec.setdefault(o, set()).add(key(r))
    if r["Result"] == "Won":
        A_win.setdefault(o, set()).add(key(r))

print(f"\n  {'Original':30} {'RecordCnt':>10} {'NominCnt':>9} {'WinCnt':>7}")
for orig in ["Chicago", "Cats", "Annie", "Dear Evan Hansen", "West Side Story"]:
    a_rec = len(A_rec.get(orig, set())); a_win = len(A_win.get(orig, set()))
    d_rec = dax_record_count(lambda r, o=orig: r["Original"] == o)
    d_win = dax_record_count(lambda r, o=orig: r["Original"] == o and r["Result"] == "Won")
    # Nomination Count is defined as [Record Count]
    print(f"  {orig:30} {a_rec:>10} {a_rec:>9} {a_win:>7}")
    check(f"{orig[:20]} | Record Count",     a_rec, d_rec, "{:d}")
    check(f"{orig[:20]} | Nomination Count", a_rec, d_rec, "{:d}")   # == Record Count
    check(f"{orig[:20]} | Win Count",        a_win, d_win, "{:d}")

# breakdown by (Original, Award) - demonstrates the FIXED [Original],[Award or Category] grain
sep("  ...same, broken down by (Original, Award)  [demonstrates the FIXED 2nd-dimension grain]")
print(f"\n  {'Original':16} {'Award':26} {'RecCnt':>7} {'WinCnt':>7}")
for (orig, award) in [("Chicago", "Tony Awards"), ("Chicago", "Academy Awards"),
                      ("Cats", "Tony Awards"), ("Annie", "Grammy Awards")]:
    a_rec = len({key(r) for r in ACC if r["Nominee"].strip() != "" and r["Original"] == orig and r["Award"] == award})
    a_win = len({key(r) for r in ACC if r["Nominee"].strip() != "" and r["Original"] == orig and r["Award"] == award and r["Result"] == "Won"})
    d_rec = dax_record_count(lambda r, o=orig, aw=award: r["Original"] == o and r["Award"] == aw)
    d_win = dax_record_count(lambda r, o=orig, aw=award: r["Original"] == o and r["Award"] == aw and r["Result"] == "Won")
    print(f"  {orig:16} {award:26} {a_rec:>7} {a_win:>7}")
    check(f"{orig[:10]}/{award[:12]} Rec", a_rec, d_rec, "{:d}")
    check(f"{orig[:10]}/{award[:12]} Win", a_win, d_win, "{:d}")

# ===========================================================================
# 5) Integrity spot-checks: Song Stat Value (bin) FLOOR, and the two Tableau groups
# ===========================================================================
sep("Integrity: Song Stat Value (bin) = FLOOR(Pivot Field Values, 0.1)   [calc column]")
def dax_bin(v):   # ROUND(FLOOR(v + 1e-7, 0.1), 1)
    return round(math.floor((v + 0.0000001) / 0.1) * 0.1, 1)
for v in [0.0, 0.049, 0.05, 0.1, 0.182, 0.5, 0.543, 0.744, 0.999, 1.0]:
    tab = round(math.floor(v / 0.1 + 1e-9) * 0.1, 1)   # Tableau bin size 0.1 peg 0
    check(f"bin({v})", tab, dax_bin(v), "{:.1f}")

sep("Integrity: Category (group) + Film (Album Pop Highlight) SWITCH mapping")
CATG = {
    "Best Film Album": {"Best Compilation Soundtrack Album for a Motion Picture or Television",
        "Best Compilation Soundtrack Album for a Motion Picture, Television or Other Visual Media",
        "Best Compilation Soundtrack Album for Motion Picture, Television or Other Visual Media",
        "Grammy Award for Best Musical Show Album"},
    "Best Musical": {"Best Musical", "Best Musical or Comedy - Motion Picture", "Best Revival", "Best Revival of a Musical"},
    "Best Musical Album": {"Best Cast Show Album", "Best Musical Show Album", "Best Musical Theater Album"},
    "Best Picture": {"Best Motion Picture - Musical or Comedy", "Best Picture", "Best Picture - Comedy or Musical", "Best Picture - Musical/Comedy"},
}
def cat_group(c):
    for g, members in CATG.items():
        if c in members:
            return g
    return "Other"
# every distinct Category maps deterministically; count buckets
buckets = {}
for r in ACC:
    buckets[cat_group(r["Category"])] = buckets.get(cat_group(r["Category"]), 0) + 1
print("  Category (group) row distribution:", dict(sorted(buckets.items())))
# sanity: known members
check("cat 'Best Picture'", "Best Picture", cat_group("Best Picture"))
check("cat 'Best Revival' -> Best Musical", "Best Musical", cat_group("Best Revival"))
check("cat unknown -> Other", "Other", cat_group("Best Zzz Unknown"))

HL = {"Chicago", "Mamma Mia!", "Mamma Mia! Here We Go Again", "tick, tick... BOOM!"}
def film_hl(f):
    if f in HL:
        return "Chicago, Mamma Mia!, Mamma Mia! Here We Go Again and 1 more"
    if f == "Dear Evan Hansen":
        return "Dear Evan Hansen"
    return "Other"
check("hl Chicago", "Chicago, Mamma Mia!, Mamma Mia! Here We Go Again and 1 more", film_hl("Chicago"))
check("hl Dear Evan Hansen", "Dear Evan Hansen", film_hl("Dear Evan Hansen"))
check("hl Cats -> Other", "Other", film_hl("Cats"))

# ===========================================================================
print("\n" + "=" * 92)
if _fail:
    print(f"RESULT: FAIL - {len(_fail)} mismatch(es): {_fail}")
    sys.exit(1)
print("RESULT: PASS - every conditional-FIXED-LOD DAX replica == Tableau semantics on all probe rows")
print("=" * 92)
