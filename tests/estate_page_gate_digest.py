"""
purpose: reproduce the page-parity + oracle estate measurement quoted on PR #433, and emit a
         SHA-256 digest of the result so the claim is checkable rather than narrated.
usage:   python tests/estate_page_gate_digest.py --bundle <engine-bundle> --specs <dir> [--json <out>]
         exit 0 when the digest matches tests/estate_page_gate_expected.json, 1 when it differs,
         3 when the measurement could not be made at all.

Why this exists: round 7 could not verify a "byte-identical to baseline" claim because no tracked
estate baseline existed in the branch - the number was narrated, not checkable. This script IS the
baseline. It stages each engine `pbip/<unit>` beside its parsed `migration-spec.json` and its
handover slice, runs BOTH halves of the page gate through the documented RELATIVE CLI path, and
hashes a canonical summary.

⚠️ Three things it deliberately does, each because getting them wrong produced a wrong number:

* **Relative invocation.** `_unit_dir` resolves while the CLI target keeps the caller's spelling; an
  absolute-only measurement hid a defect for a whole round.
* **Handover lookup by lossy key.** `run_estate` sanitises handover FILENAMES (parentheses become
  underscores), so an exact-name lookup silently skips real evidence - measured, that alone moved the
  split from 19/2 to 18/3.
* **Exit 3 when nothing could be staged.** A measurement that could not be made must not report a
  clean digest; that is the failure mode this whole PR exists to remove.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_unit as cu  # noqa: E402  # pylint: disable=wrong-import-position

EXPECTED = Path(__file__).with_name("estate_page_gate_expected.json")

EXIT_MATCH = 0
EXIT_DIFFERS = 1
EXIT_UNMEASURABLE = 3


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def stage(bundle: Path, specs: Path, work: Path) -> tuple[list[Path], list[str]]:
    """Copy each `pbip/<unit>` beside its spec and handover slice. Returns (units, unstaged)."""
    handovers: dict[str, list[Path]] = {}
    for path in (bundle / "handover").glob("*.json"):
        handovers.setdefault(_key(path.stem), []).append(path)

    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    units: list[Path] = []
    unstaged: list[str] = []
    for pbip in sorted((bundle / "pbip").iterdir()):
        if not pbip.is_dir():
            continue
        spec = specs / f"{pbip.name}.json"
        if not spec.is_file():
            unstaged.append(f"{pbip.name} (no parsed spec)")
            continue
        unit = work / pbip.name
        (unit / "fabric").mkdir(parents=True)
        for item in pbip.iterdir():
            if item.is_dir():
                shutil.copytree(item, unit / "fabric" / item.name)
        shutil.copy2(spec, unit / "migration-spec.json")
        matches = handovers.get(_key(pbip.name), [])
        if len(matches) == 1:
            (unit / "handover").mkdir(exist_ok=True)
            shutil.copy2(matches[0], unit / "handover" / matches[0].name)
        units.append(unit)
    return units, unstaged


def measure(units: list[Path]) -> dict[str, object]:
    """Both halves of the page gate per unit, plus the parity/oracle denominator cross-check.

    The cross-check compares the resulting DENOMINATOR, not only the reported exclusion list: round 5
    measured an exclusion set that removed more candidates than it listed, and a list-only comparison
    was structurally blind to it.
    """
    verdicts: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    disagreements: list[str] = []
    blank = extra = contested = 0
    for unit in units:
        rel = Path(unit.as_posix())
        parity = cu.check_page_parity(rel, cu.load_exemptions(rel))
        oracle = cu.check_oracle_coverage(rel, None, None)
        verdicts[parity["status"]] += 1
        for row in parity.get("omissions", []):
            dispositions[row["disposition"]] += 1
        blank += len(parity.get("blank_pages", []))
        extra += len(parity.get("unaccounted_extra_pages", []))
        contested += len(parity.get("contested_names", []))

        expectation = cu.page_expectation(rel)
        if not expectation["assessable"]:
            continue
        owing_nothing = {
            cu._page_key(row)  # pylint: disable=protected-access
            for row in parity.get("omissions", [])
            if row["disposition"] in cu.OMISSIONS_OWING_NOTHING
        }
        excluded = {cu._page_key(row) for row in oracle.get("excluded_omissions", [])}  # pylint: disable=protected-access
        candidates = {cu._page_key(page) for page in expectation["candidates"]}  # pylint: disable=protected-access
        if owing_nothing != excluded:
            disagreements.append(f"{unit.name}: exclusion lists differ")
        elif oracle.get("pages") is not None and oracle["pages"] != len(candidates - excluded):
            disagreements.append(f"{unit.name}: denominator {oracle['pages']} != {len(candidates - excluded)} implied")
    return {
        "units": len(units),
        "verdicts": dict(sorted(verdicts.items())),
        "dispositions": dict(sorted(dispositions.items())),
        "blank_pages": blank,
        "unaccounted_extra_pages": extra,
        "contested_names": contested,
        "disagreements": sorted(disagreements),
    }


def digest(summary: dict[str, object]) -> str:
    """SHA-256 over the canonical JSON form, so the claim is one comparable string."""
    return hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="engine bundle with pbip/ and handover/")
    parser.add_argument("--specs", required=True, type=Path, help="directory of <unit>.json migration specs")
    parser.add_argument("--work", type=Path, default=Path("_estate_page_gate"), help="staging directory")
    parser.add_argument("--json", type=Path, help="write the summary + digest here")
    parser.add_argument("--update", action="store_true", help="rewrite the committed expectation")
    args = parser.parse_args(argv)

    if not (args.bundle / "pbip").is_dir():
        print(f"cannot measure: {args.bundle / 'pbip'} is not a directory", file=sys.stderr)
        return EXIT_UNMEASURABLE
    os.chdir(REPO_ROOT)
    units, unstaged = stage(args.bundle, args.specs, args.work)
    if not units:
        print("cannot measure: no unit could be staged (missing parsed specs?)", file=sys.stderr)
        return EXIT_UNMEASURABLE

    summary = measure(units)
    result = {"summary": summary, "sha256": digest(summary), "unstaged": unstaged}
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.update:
        EXPECTED.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nwrote {EXPECTED}")
        return EXIT_MATCH

    if not EXPECTED.is_file():
        print(f"\ncannot compare: {EXPECTED} does not exist; run with --update", file=sys.stderr)
        return EXIT_UNMEASURABLE
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    if expected.get("sha256") == result["sha256"]:
        print(f"\nMATCHES committed expectation: {result['sha256']}")
        return EXIT_MATCH
    print(f"\nDIFFERS from committed expectation\n  expected {expected.get('sha256')}\n  measured {result['sha256']}")
    return EXIT_DIFFERS


if __name__ == "__main__":
    raise SystemExit(main())
