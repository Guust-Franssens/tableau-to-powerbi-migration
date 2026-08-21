"""
purpose: generate the deterministic, credential-free input for the `fixtures/large-refresh` PBIP
         semantic model - a seeded CSV of tunable size, plus the machine-local `expressions.tmdl`
         that points the model's `SourceFolder` parameter at it. Nothing it writes is committed:
         the CSV would bloat a repo that is already ~170 MB, and the path is machine-specific.
usage:   python scripts/make_refresh_fixture.py [--rows N] [--seed S] [--out DIR] [--no-bind]
                                                [--print-hash] [--force]

Why this exists (issue #262): every committed example model refreshes in about a second, so nothing
in this repo can observe refresh *behaviour over time* - the 300 s ceiling in
`refresh_pbip_model.py`, an unnecessary re-pull being measurably expensive, progress-event cadence,
or a refresh long enough to interrupt. This produces an input a local model can be genuinely slow
on, with no credentials and no network, so anyone can reproduce it.

Determinism is the whole point: same `--rows` and `--seed` give byte-identical output (proved by
`tests/test_make_refresh_fixture.py`), so a timing change means the *model* changed, not the data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "fixtures" / "large-refresh"
DEFAULT_OUT_DIR = FIXTURE_ROOT / "data"
MODEL_DEFINITION_DIR = FIXTURE_ROOT / "fabric" / "LargeRefresh.SemanticModel" / "definition"

CSV_NAME = "orders.csv"

# `--rows` default. Calibrated, not guessed: see `fixtures/large-refresh/README.md` for the measured
# seconds-per-row of each M shape. 2,000,000 rows is the size at which the shipped model's default
# table set crosses a minute, which is the "long enough to observe" bar issue #262 asks for.
DEFAULT_ROWS = 2_000_000
DEFAULT_SEED = 262  # the issue number, so the default is self-documenting

# The epoch every generated order date is an offset from. Fixed rather than "today" - a fixture whose
# bytes change daily cannot be hashed, and a hash is what makes determinism checkable.
EPOCH = date(2021, 1, 1)
DATE_SPAN_DAYS = 1_500

REGIONS = ("North America", "EMEA", "APAC", "LATAM")
COUNTRIES = (
    "United States",
    "Canada",
    "Mexico",
    "Brazil",
    "United Kingdom",
    "Germany",
    "France",
    "Netherlands",
    "Spain",
    "Italy",
    "Sweden",
    "Poland",
    "India",
    "Japan",
    "Australia",
    "Singapore",
    "South Korea",
    "China",
    "Chile",
    "Argentina",
)
CATEGORIES = (
    "Refrigerated",
    "Dry Goods",
    "Hazardous",
    "Oversize",
    "Electronics",
    "Apparel",
    "Automotive Parts",
    "Pharmaceutical",
)
SHIP_MODES = ("Ground", "Air", "Ocean", "Rail", "Expedited")
STATUSES = ("Delivered", "In Transit", "Delayed", "Returned", "Cancelled")

# Cardinality of the generated dimension keys. `CUSTOMER_COUNT` is the join key the `SelfJoin` and
# `Merged` shapes group on, so it directly controls how expensive those shapes are: a merge costs
# roughly rows x (rows / distinct keys), so fewer distinct customers means a bigger fan-out.
CUSTOMER_COUNT = 40_000
PRODUCT_COUNT = 6_000

# Free-text column. Real extracts carry wide, low-entropy strings, and they are a real cost in both
# the CSV read and the VertiPaq encode. Pre-rendered once and indexed into so generation stays fast
# while the bytes stay deterministic.
NOTE_FRAGMENTS = (
    "customer requested weekend delivery window",
    "carrier reported congestion at the origin terminal",
    "pallet re-wrapped after inspection at the cross-dock",
    "signature waived per standing account agreement",
    "temperature log attached to the bill of lading",
    "partial shipment released ahead of the balance",
    "address corrected by the dispatcher before pickup",
    "customs paperwork cleared without amendment",
    "appointment rescheduled at the consignee request",
    "no exceptions recorded against this movement",
)

COLUMNS = (
    "OrderID",
    "OrderDate",
    "CustomerID",
    "CustomerName",
    "Region",
    "Country",
    "Category",
    "ProductID",
    "ShipMode",
    "Status",
    "Quantity",
    "UnitPrice",
    "Discount",
    "ShipDays",
    "Notes",
)

# Rows handed to `csv.writer.writerows` at a time. Bounded so a 20,000,000-row run does not hold the
# whole extract in memory; large enough that the per-call overhead is irrelevant.
CHUNK_ROWS = 50_000

EXPRESSIONS_TMDL_TEMPLATE = """expression SourceFolder = "{folder}" meta \
[IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
\tlineageTag: 8f2b6a41-0d55-4d33-9c1e-2f6b7a90c262

\tannotation PBI_ResultType = Text

"""


def _note_pool(rng: random.Random) -> tuple[str, ...]:
    """Pre-render the free-text pool once, so per-row generation is an index lookup, not string work."""
    pool = []
    for i in range(512):
        first = NOTE_FRAGMENTS[i % len(NOTE_FRAGMENTS)]
        second = rng.choice(NOTE_FRAGMENTS)
        pool.append(f"{first}; {second} (ref {i:04d})")
    return tuple(pool)


def _rows(count: int, seed: int):
    """Yield `count` deterministic order rows for the given seed.

    A generator rather than a list: the row count is a parameter, and materialising 20,000,000 tuples
    to write them once is the kind of avoidable memory cliff that turns a tuning knob into a crash.
    """
    rng = random.Random(seed)
    notes = _note_pool(rng)
    note_count = len(notes)
    customer_names = tuple(f"Customer {i:06d}" for i in range(CUSTOMER_COUNT))

    for i in range(count):
        customer = rng.randrange(CUSTOMER_COUNT)
        order_date = EPOCH + timedelta(days=rng.randrange(DATE_SPAN_DAYS))
        quantity = rng.randint(1, 400)
        unit_price = round(rng.uniform(4.0, 2_500.0), 2)
        discount = round(rng.uniform(0.0, 0.35), 4)
        yield (
            f"ORD-{i:09d}",
            order_date.isoformat(),
            f"CUST-{customer:06d}",
            customer_names[customer],
            rng.choice(REGIONS),
            rng.choice(COUNTRIES),
            rng.choice(CATEGORIES),
            f"SKU-{rng.randrange(PRODUCT_COUNT):05d}",
            rng.choice(SHIP_MODES),
            rng.choice(STATUSES),
            quantity,
            unit_price,
            discount,
            rng.randint(0, 45),
            notes[rng.randrange(note_count)],
        )


def write_csv(path: Path, rows: int, seed: int) -> tuple[int, str]:
    """Write the fixture CSV and return `(bytes_written, sha256_hex)`.

    The hash is computed from the bytes actually on disk rather than from the in-memory rows, so it
    also pins the line ending and quoting - the two things that silently differ between platforms and
    would make a "deterministic" claim untrue in exactly the case nobody tests.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    # newline="" is required by csv; the explicit "\n" terminator keeps the bytes identical on
    # Windows and Linux so the hash is portable.
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COLUMNS)
        chunk: list[tuple] = []
        for row in _rows(rows, seed):
            chunk.append(row)
            if len(chunk) >= CHUNK_ROWS:
                writer.writerows(chunk)
                chunk.clear()
        if chunk:
            writer.writerows(chunk)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            written += len(block)
    return written, digest.hexdigest()


def write_binding(definition_dir: Path, data_dir: Path) -> Path:
    """Write the model's `SourceFolder` parameter, pointing at this machine's generated data folder.

    `definition/expressions.tmdl` is **gitignored on purpose**. It is the one file in the fixture that
    cannot be machine-independent - M has no environment-variable function, so the folder has to be a
    literal - and committing a literal path would either be wrong for every other clone or leave a
    permanently dirty working tree. Generating it is what keeps `git status` clean.
    """
    definition_dir.mkdir(parents=True, exist_ok=True)
    target = definition_dir / "expressions.tmdl"
    folder = str(data_dir.resolve())
    if not folder.endswith("\\"):
        folder += "\\"
    # M string literals escape a backslash as `""`-style quoting only for quotes; backslashes are
    # literal in M, so the Windows path goes in as-is. No BOM: Desktop's project reader hard-rejects
    # one (see the pbip-model-refresh skill).
    target.write_text(EXPRESSIONS_TMDL_TEMPLATE.format(folder=folder), encoding="utf-8")
    return target


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI surface: size, seed, destination, and whether to bind the model to this machine."""
    parser = argparse.ArgumentParser(
        description="Generate the deterministic local input for the large-refresh PBIP fixture.",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_ROWS,
        help=f"rows to generate (default {DEFAULT_ROWS:,}); underscores are accepted, e.g. 5_000_000",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"RNG seed (default {DEFAULT_SEED})")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"output folder for {CSV_NAME} (default {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--no-bind",
        action="store_true",
        help="do not write the model's expressions.tmdl (use when generating data for somewhere else)",
    )
    parser.add_argument(
        "--model-definition-dir",
        type=Path,
        default=MODEL_DEFINITION_DIR,
        help="TMDL definition folder whose expressions.tmdl gets the SourceFolder path",
    )
    parser.add_argument("--print-hash", action="store_true", help="print the SHA-256 of the written CSV")
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate even when a CSV of the requested size already exists",
    )
    return parser


def _existing_row_count(path: Path) -> int | None:
    """Rows in an existing CSV, or None when it is absent/unreadable - used only to skip needless work."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return max(sum(1 for _ in handle) - 1, 0)
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    """Generate the CSV, bind the model to it, and report size, rows and hash."""
    args = _build_arg_parser().parse_args(argv)
    if args.rows < 1:
        print("ERROR --rows must be >= 1")
        return 2

    csv_path = args.out / CSV_NAME
    existing = _existing_row_count(csv_path)
    if existing == args.rows and not args.force:
        size = csv_path.stat().st_size
        print(f"FIXTURE: SKIP {csv_path} already has {existing:,} rows ({size / 1e6:.1f} MB) - use --force")
    else:
        written, digest = write_csv(csv_path, args.rows, args.seed)
        print(f"FIXTURE: WROTE {csv_path}")
        print(f"  rows={args.rows:,} seed={args.seed} bytes={written:,} ({written / 1e6:.1f} MB)")
        if args.print_hash:
            print(f"  sha256={digest}")

    if not args.no_bind:
        bound = write_binding(args.model_definition_dir, args.out)
        print(f"FIXTURE: BOUND {bound} -> {args.out.resolve()}")
        print("  Open fixtures/large-refresh/fabric/LargeRefresh.pbip in Power BI Desktop to refresh it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
