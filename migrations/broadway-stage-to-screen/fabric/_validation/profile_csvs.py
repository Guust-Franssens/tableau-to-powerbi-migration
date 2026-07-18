"""Profile the 4 extracted CSVs with the stdlib csv module (no pandas):
headers, row counts, and candidate join-key distinct-value overlaps."""
import csv, os

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data")
FILES = {
    "1_films":     "ds.1_films.csv",
    "2_chronology":"ds.2_chronology.csv",
    "3_accolades": "ds.3_accolades.csv",
    "4_song_stats":"ds.4_song_stats.csv",
}

def load(fn):
    with open(os.path.join(DATA, fn), encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        return r.fieldnames, rows

tables = {}
for k, fn in FILES.items():
    cols, rows = load(fn)
    tables[k] = (cols, rows)
    print("=" * 100)
    print(f"{k}: {len(rows)} rows x {len(cols)} cols  ({fn})")
    print("-" * 100)
    for c in cols:
        vals = [row[c] for row in rows if row[c] not in (None, "")]
        distinct = sorted(set(vals))
        print(f"    {c!r:44} nonnull={len(vals):5} distinct={len(distinct):5} eg={distinct[:3]}")

def keyset(k, col):
    cols, rows = tables[k]
    return set((row[col] or "").strip() for row in rows if (row.get(col) or "").strip())

def overlap(a_lbl, a, b_lbl, b):
    inter = a & b
    print(f"\n  {a_lbl} ({len(a)})  vs  {b_lbl} ({len(b)}): intersect={len(inter)}")
    only_a = sorted(a - b); only_b = sorted(b - a)
    if only_a: print(f"     only in {a_lbl}: {only_a}")
    if only_b: print(f"     only in {b_lbl}: {only_b}")

print("\n" + "#" * 100 + "\nCANDIDATE JOIN KEYS\n" + "#" * 100)
K = {}
for lbl, (k, col) in {
    "1_films.Title": ("1_films", "Title"),
    "4_song.Film": ("4_song_stats", "Film"),
    "4_song.Original": ("4_song_stats", "Original"),
    "3_acc.Film": ("3_accolades", "Film"),
    "3_acc.Original": ("3_accolades", "Original"),
    "2_chron.Movie Name": ("2_chronology", "Movie Name"),
    "2_chron.Broadway Show": ("2_chronology", "Broadway Show"),
}.items():
    try:
        K[lbl] = keyset(k, col)
        print(f"\n{lbl}: {len(K[lbl])} distinct -> {sorted(K[lbl])}")
    except Exception as e:
        print(f"\n{lbl}: ERR {e}")

print("\n" + "=" * 100 + "\nOVERLAP vs 1_films.Title (movie hub)\n" + "=" * 100)
overlap("1_films.Title", K["1_films.Title"], "4_song.Film", K["4_song.Film"])
overlap("1_films.Title", K["1_films.Title"], "3_acc.Film", K["3_acc.Film"])
overlap("1_films.Title", K["1_films.Title"], "2_chron.Movie Name", K["2_chron.Movie Name"])

print("\n" + "=" * 100 + "\nOVERLAP on ORIGINAL (Broadway) title\n" + "=" * 100)
overlap("4_song.Original", K["4_song.Original"], "3_acc.Original", K["3_acc.Original"])
overlap("4_song.Original", K["4_song.Original"], "2_chron.Broadway Show", K["2_chron.Broadway Show"])
overlap("1_films.Title", K["1_films.Title"], "4_song.Original", K["4_song.Original"])

print("\n" + "=" * 100 + "\nGRAIN: is key unique per table?\n" + "=" * 100)
for k, (cols, rows) in tables.items():
    for col in cols:
        if col.lower() in ("title", "film", "original", "broadway show", "movie name"):
            vals = [(r[col] or "").strip() for r in rows if (r.get(col) or "").strip()]
            print(f"  {k}.{col!r}: rows_with_val={len(vals)} distinct={len(set(vals))} unique={len(vals)==len(set(vals))}")
