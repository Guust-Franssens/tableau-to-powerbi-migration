"""
purpose: harvest the engine-gap evidence that already sits in a bundle - compare the engine's
         pristine `reports/`+`semantic_models/` baseline against the shipped `pbip/` working copy,
         ATTRIBUTE every difference to whoever wrote it, classify its shape, and report frequencies
         with denominators.
usage:   python scripts/harvest_engine_gaps.py <bundle> [...] [--json <file>] [--markdown <file>]
                                               [--quiet] [--warn-only] [--top N]

Full methodology, every measurement behind it, and the estate-scale results:
`docs/engine-gap-harvest.md`. The essentials, because they change how the output must be read:

**A difference between `reports/` and `pbip/` is NOT evidence the engine was wrong.** Measured on
`_runs/estate-2.339.0-20260829/` (2026-08-30): 37 of 44 report pairs differ, 500 files - and **100%
of those bytes were written by the engine itself**. All 2481 recorded artifacts still hash-match
(0 mismatched, 0 missing); no `_build/` directory and no edit declaration exists anywhere. Nobody
edited that bundle. The raw delta answers *"what does the engine change between its own reference and
its own bound emission?"* - by design. Only the ATTRIBUTED subset answers issue #274's question, and
this module reports the two separately and never merges them.

Axis 1 - PROVENANCE, arbitrated by hash against `generated_artifacts.files` (which covers BOTH sides:
862 `reports/` entries as well as 1556 `pbip/` ones):

    baseline matches + working matches  -> engine_internal   the engine wrote both; NOT a tier fix
    baseline matches + working drifted  -> tier_edit         changed after the engine - THE EVIDENCE
    baseline DRIFTED                    -> baseline_tampered refuse; exit 1 rather than report a lie
    unrecorded, or no baseline at all   -> unattributed      honest ignorance, never laundered

Axis 2 - SHAPE, from a structural JSON-pointer diff (and a line diff for TMDL). Buckets were chosen
by measuring the corpus first; `UNCLASSIFIED` is retained and reported (0 on this corpus - a
measurement, not a guarantee). `BINDING_RESOLUTION` is the refinement that stops it crying wolf: the
baseline is a reference-only emission whose `datasetReference.byPath` resolves in 0 of 45 cases, so
an entity rename is usually the binding being resolved. It is claimed only when EVERY new entity
resolves in the bound model and NO removed one does - 265 of 292 files; the other 27 stay
`MODEL_OBJECT_NAMES` rather than being excused.

This module does NOT use git, and that is a correctness fix. Measured: of 44 pairs, the mandated
`git diff --no-index --stat` produced NO stat line for 3 (worst path 261/285/287 vs 259 for the 41 it
could read), while agreeing with this module on 41 of 41 that it could. A Python content comparison
reads all three, so UNASSESSABLE falls from 3 to 0 - and the blind spot is still reported, because it
is evidence about the mandated command.

⚠️ `UNASSESSABLE` remains real: a file that cannot be read is counted, listed, withdrawn from BOTH
sides (so it can never masquerade as an addition), and forces a non-zero exit.

Standalone rather than a `run_estate.py` phase, deliberately: a phase inside the run can only observe
a bundle the tier has not touched yet - the degenerate case above, where `tier_edit` is 0 by
construction. Wiring `check_unit.py` is a follow-up, not done here.

What this does NOT tell you: **effort** (counts are not hours), **why** (that lives in the handover
and `limitations_encountered`), whether the **engine is wrong** (`engine_internal` is by construction
not defect evidence), and anything about a NATURALLY OCCURRING tier edit - no bundle on the machine
this was built on contained one. The `tier_edit` / `baseline_tampered` paths are proven on real
engine artifacts with an INJECTED change (a copied unit reported exactly 1 tier edit, then
`untrustworthy`/exit 1 when its baseline was touched); the field case is still unconfirmed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from check_migration_progress import load_generated_artifact_baseline, load_generated_edit_declarations
from migration_bundle import ENGINE_RECEIPT, sha256_file

REPORT_VERSION = 1

EXIT_OK = 0
EXIT_UNTRUSTWORTHY = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_UNTRUSTWORTHY = "untrustworthy"

PROV_ENGINE = "engine_internal"
PROV_TIER = "tier_edit"
PROV_TAMPERED = "baseline_tampered"
PROV_UNATTRIBUTED = "unattributed"
PROVENANCES = (PROV_ENGINE, PROV_TIER, PROV_TAMPERED, PROV_UNATTRIBUTED)

PAIR_IDENTICAL = "identical"
PAIR_DIFFERS = "differs"
PAIR_NO_BASELINE = "unpaired_no_baseline"
PAIR_NO_WORKING = "unpaired_no_working"
PAIR_UNASSESSABLE = "unassessable"

LAYER_REPORT = "report"
LAYER_MODEL = "model"

# Longest full path git read successfully on the estate corpus; 261/285/287 all failed with no stat
# line. Kept local rather than imported from `check_path_ceiling` because that module's ceilings are
# a Power BI Desktop measurement and this one is a git measurement - two different instruments that
# happen to agree, and collapsing them would hide the day one of them moves.
GIT_READABLE_PATH_MAX = 259

DEFAULT_TOP = 12

_TABLE_DECL = re.compile(r"^\s*table\s+(?:'([^']+)'|(\S+))", re.MULTILINE)

# Leaves whose value IS a model object name. Everything else under a query is query SHAPE.
_NAME_LEAVES = frozenset({"Entity", "Property", "queryRef", "nativeQueryRef"})

SHAPE_MODEL_NAMES = "MODEL_OBJECT_NAMES"
SHAPE_BINDING = "BINDING_RESOLUTION"
SHAPE_QUERY = "QUERY_SHAPE"
SHAPE_REBIND = "REBIND_TARGET"
SHAPE_FILTER = "FILTER"
SHAPE_LAYOUT = "LAYOUT"
SHAPE_FORMATTING = "FORMATTING"
SHAPE_VISUAL_TYPE = "VISUAL_TYPE"
SHAPE_PAGE_ORDER = "PAGE_ORDER"
SHAPE_RESOURCES = "RESOURCES"
SHAPE_UNCLASSIFIED = "UNCLASSIFIED"
SHAPE_BINARY = "BINARY_CHANGED"
SHAPE_UNREADABLE_JSON = "UNPARSEABLE_JSON"
SHAPE_UNREADABLE_TEXT = "UNREADABLE_TEXT"

# `.pbism` and `.pbip` are JSON documents despite their suffixes, so they get the structural diff
# rather than being written off as binary.
_JSON_SUFFIXES = frozenset({".json", ".pbir", ".pbip", ".pbism", ".platform"})
_TEXT_SUFFIXES = frozenset({".tmdl", ".md", ".txt", ".dax", ".m"})

# TMDL line kinds, most specific first. Measured on the estate corpus, the ONLY model-layer
# differences were whole `measure` blocks appended to a shared published-datasource model by the
# consuming workbook - a signal that reads as `BINARY_CHANGED` if TMDL is not parsed at all.
_TMDL_LINE_KINDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*lineageTag:"), "TMDL_LINEAGE_TAG"),
    (re.compile(r"^\s*annotation\s"), "TMDL_ANNOTATION"),
    (re.compile(r"^\s*measure\s"), "TMDL_MEASURE"),
    (re.compile(r"^\s*column\s"), "TMDL_COLUMN"),
    (re.compile(r"^\s*table\s"), "TMDL_TABLE"),
    (re.compile(r"^\s*relationship\s"), "TMDL_RELATIONSHIP"),
    (re.compile(r"^\s*(partition|source|mode)\b"), "TMDL_PARTITION"),
)
SHAPE_TMDL_OTHER = "TMDL_OTHER"

# Ordered (prefixes, substrings, shape). First match wins, so the more specific families sit first.
_POINTER_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("/position",), (), SHAPE_LAYOUT),
    ((), ("filter", "Filter"), SHAPE_FILTER),
    ((), ("/visualType",), SHAPE_VISUAL_TYPE),
    (("/datasetReference",), (), SHAPE_REBIND),
    (("/pageOrder", "/activePageName"), (), SHAPE_PAGE_ORDER),
    (("/resourcePackages",), (), SHAPE_RESOURCES),
)


class Pair(NamedTuple):
    """One baseline/working pair for one layer.

    `artifact` is the folder name without its suffix and is what the two sides are matched on;
    `unit` is the owning `pbip/<unit>/` directory. They are NOT interchangeable: measured on the
    estate corpus, every one of the 51 units holds exactly one `.SemanticModel` and only 7 of them
    are named after their unit - `pbip/HR Dashboard/` holds `HumanResources.SemanticModel`. Pairing
    the model layer by unit name reported 21 units as having no engine baseline when 20 of them do.
    """

    artifact: str
    unit: str
    layer: str
    baseline: Path | None
    working: Path | None


class TreeDelta(NamedTuple):
    """The raw content comparison of two trees, with unreadable entries kept apart."""

    added: list[str]
    removed: list[str]
    changed: list[str]
    unassessable: list[dict[str, str]]
    baseline_files: int
    working_files: int
    longest_path: int


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe(text: str) -> str:
    """A console/JSON-safe rendering of a path that may carry undecodable bytes."""
    return text.encode("utf-8", "backslashreplace").decode("ascii", "replace")


def hash_tree(root: Path) -> tuple[dict[str, str], list[dict[str, str]], int]:
    """Hash every file under `root`, returning (by-relative-path, unreadable, longest full path).

    Unreadable entries are returned SEPARATELY and are never given a digest, because a file that
    cannot be read is not a file that is the same - the single defect shape this repo keeps
    re-introducing. Each carries `relative` when one could be computed, so the caller can withdraw
    that path from BOTH sides of a comparison rather than letting it masquerade as an addition.
    """
    digests: dict[str, str] = {}
    unreadable: list[dict[str, str]] = []
    longest = 0
    root_str = str(root)

    def on_error(exc: OSError) -> None:
        failed = str(getattr(exc, "filename", "") or root_str)
        record = {"path": _safe(failed), "reason": f"{type(exc).__name__}: {exc.strerror or exc}"}
        try:
            record["relative"] = Path(failed).relative_to(root).as_posix()
        except ValueError:
            pass
        unreadable.append(record)

    for dirpath, dirnames, filenames in os.walk(root_str, onerror=on_error):
        for name in list(dirnames) + list(filenames):
            longest = max(longest, len(os.path.join(dirpath, name)))
        for name in filenames:
            full = Path(dirpath) / name
            relative = None
            try:
                relative = full.relative_to(root).as_posix()
                digests[relative] = sha256_file(full)
            except (OSError, ValueError) as exc:
                record = {"path": _safe(str(full)), "reason": f"{type(exc).__name__}: {exc}"}
                if relative is not None:
                    record["relative"] = relative
                unreadable.append(record)
    return digests, unreadable, longest


def compare_trees(baseline: Path, working: Path) -> TreeDelta:
    """Content-compare two trees without git, so a long path is assessed rather than skipped."""
    a, a_bad, a_longest = hash_tree(baseline)
    b, b_bad, b_longest = hash_tree(working)
    unassessable = a_bad + b_bad
    # A path that could not be read on EITHER side is withdrawn from BOTH key sets, so it can never
    # masquerade as an addition or a removal. Matching is done on the POSIX relative path, not the
    # rendered absolute one: `Path(root) / "a/b"` stringifies to `root\a/b` on Windows and would
    # never match the `root\a\b` that `os.walk` produced.
    blocked = {record["relative"] for record in unassessable if "relative" in record}
    a_keys = set(a) - blocked
    b_keys = set(b) - blocked
    return TreeDelta(
        added=sorted(b_keys - a_keys),
        removed=sorted(a_keys - b_keys),
        changed=sorted(k for k in a_keys & b_keys if a[k] != b[k]),
        unassessable=unassessable,
        baseline_files=len(a),
        working_files=len(b),
        longest_path=max(a_longest, b_longest),
    )


def _json_pointer_diff(a: Any, b: Any, pointer: str = "", out: list[tuple[str, str]] | None = None):
    """Every difference between two JSON documents, as (pointer, kind) pairs."""
    if out is None:
        out = []
    if type(a) is not type(b):
        out.append((pointer, "type"))
        return out
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            child = f"{pointer}/{key}"
            if key not in a:
                out.append((child, "added"))
            elif key not in b:
                out.append((child, "removed"))
            else:
                _json_pointer_diff(a[key], b[key], child, out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append((f"{pointer}[]", "length"))
        for item_a, item_b in zip(a, b):
            _json_pointer_diff(item_a, item_b, f"{pointer}[]", out)
    elif a != b:
        out.append((pointer, "value"))
    return out


def _pointer_shape(pointer: str) -> str:
    """Map one JSON pointer to a shape. Buckets derived by measuring the estate corpus.

    Ordered, first-match: `filter` is tested before the query/objects families because a filter
    lives under both and is the more specific answer.
    """
    for prefixes, substrings, shape in _POINTER_RULES:
        if any(pointer.startswith(p) for p in prefixes) or any(s in pointer for s in substrings):
            return shape
    leaf = pointer.rstrip("[]").rsplit("/", 1)[-1].rstrip("[]")
    if "/query" in pointer or "/sortDefinition" in pointer:
        return SHAPE_MODEL_NAMES if leaf in _NAME_LEAVES else SHAPE_QUERY
    if "/objects" in pointer:
        return SHAPE_MODEL_NAMES if leaf in _NAME_LEAVES else SHAPE_FORMATTING
    return SHAPE_UNCLASSIFIED


def _entities(node: Any, out: set[str]) -> set[str]:
    """Every `Entity` value anywhere in a PBIR document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Entity" and isinstance(value, str):
                out.add(value)
            else:
                _entities(value, out)
    elif isinstance(node, list):
        for value in node:
            _entities(value, out)
    return out


def bound_model_tables(report_dir: Path) -> set[str] | None:
    """Table names of the semantic model a report actually binds, or None when unresolvable.

    None is a real answer and must stay distinguishable from the empty set: it means the refinement
    below cannot run, not that the model has no tables.
    """
    pbir = report_dir / "definition.pbir"
    try:
        reference = json.loads(pbir.read_text(encoding="utf-8")).get("datasetReference") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    relative = (reference.get("byPath") or {}).get("path")
    if not isinstance(relative, str) or not relative:
        return None
    tables_dir = (pbir.parent / relative).resolve() / "definition" / "tables"
    if not tables_dir.is_dir():
        return None
    names: set[str] = set()
    for tmdl in tables_dir.glob("*.tmdl"):
        try:
            match = _TABLE_DECL.search(tmdl.read_text(encoding="utf-8"))
        except OSError:
            continue
        if match:
            names.add(match.group(1) or match.group(2))
    return names


def _is_binding_resolution(before: Any, after: Any, tables: set[str] | None) -> bool:
    """Whether an entity rename is the unbound reference copy being resolved against a real model.

    True only when the model is known, at least one entity actually changed, EVERY newly referenced
    entity exists in the bound model, and NO removed entity does. Anything else - a substitution
    between two valid tables, a new name the model does not have - stays an unexplained
    MODEL_OBJECT_NAMES change rather than being quietly excused.
    """
    if tables is None:
        return False
    old = _entities(before, set())
    new = _entities(after, set())
    gained = new - old
    lost = old - new
    if not gained and not lost:
        return False
    return bool(gained) and gained <= tables and not lost & tables


def _tmdl_line_shape(line: str) -> str:
    for pattern, shape in _TMDL_LINE_KINDS:
        if pattern.match(line):
            return shape
    return SHAPE_TMDL_OTHER


def _text_shapes(baseline_file: Path, working_file: Path) -> tuple[list[str], int]:
    """Shapes for a changed TMDL/plain-text file, from the lines that differ.

    A blank line carries no meaning, so it is counted as a difference but never given a shape - it
    would otherwise dominate `TMDL_OTHER` and make the residue bucket look alarming.
    """
    try:
        before = baseline_file.read_text(encoding="utf-8").splitlines()
        after = working_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [SHAPE_UNREADABLE_TEXT], 1
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    shapes: set[str] = set()
    count = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for line in before[i1:i2] + after[j1:j2]:
            count += 1
            if line.strip():
                shapes.add(_tmdl_line_shape(line))
    return sorted(shapes) or [SHAPE_TMDL_OTHER], count


def shapes_for_change(
    baseline_file: Path,
    working_file: Path,
    tables: set[str] | None,
) -> tuple[list[str], int]:
    """Shapes describing one changed file, plus the number of structural differences found."""
    suffix = baseline_file.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return _text_shapes(baseline_file, working_file)
    if suffix not in _JSON_SUFFIXES:
        return [SHAPE_BINARY], 1
    try:
        before = json.loads(baseline_file.read_text(encoding="utf-8"))
        after = json.loads(working_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return [SHAPE_UNREADABLE_JSON], 1
    differences = _json_pointer_diff(before, after)
    shapes = {_pointer_shape(pointer) for pointer, _kind in differences}
    if SHAPE_MODEL_NAMES in shapes and _is_binding_resolution(before, after, tables):
        shapes.discard(SHAPE_MODEL_NAMES)
        shapes.add(SHAPE_BINDING)
    return sorted(shapes), len(differences)


def _added_removed_shape(relative: str, added: bool) -> str:
    name = relative.rsplit("/", 1)[-1].lower()
    if name == "page.json":
        return "PAGE_ADDED" if added else "PAGE_REMOVED"
    if name == "visual.json":
        return "VISUAL_ADDED" if added else "VISUAL_REMOVED"
    return "FILE_ADDED" if added else "FILE_REMOVED"


class Attribution:
    """Decides who wrote a byte, from the run's recorded sha256 baseline.

    Deliberately a small class rather than a closure: `usable` has to be inspectable by the caller so
    an unattributable run can be reported as such instead of silently reporting everything as
    `unattributed` with no explanation.
    """

    def __init__(self, bundle: Path, recorded: dict[str, str] | None, notes: list[str]) -> None:
        self.bundle = bundle
        self.recorded = recorded
        self.notes = notes
        self.usable = recorded is not None
        self._cache: dict[str, str] = {}

    def state(self, relative: str) -> str:
        """`match`, `drift`, `absent` or `unrecorded` for one bundle-relative path."""
        if self.recorded is None:
            return "unrecorded"
        if relative in self._cache:
            return self._cache[relative]
        expected = self.recorded.get(relative)
        path = self.bundle / Path(relative)
        if expected is None:
            state = "unrecorded"
        elif not path.is_file():
            state = "absent"
        else:
            try:
                state = "match" if sha256_file(path) == expected else "drift"
            except OSError:
                state = "unrecorded"
        self._cache[relative] = state
        return state

    def verdict(self, baseline_rel: str | None, working_rel: str | None) -> str:
        """Provenance of one difference. `None` means that side has no file."""
        base = self.state(baseline_rel) if baseline_rel else None
        work = self.state(working_rel) if working_rel else None
        if not self.usable:
            return PROV_UNATTRIBUTED
        if base == "drift":
            return PROV_TAMPERED
        if work == "drift":
            return PROV_TIER
        states = {s for s in (base, work) if s is not None}
        if states == {"match"}:
            return PROV_ENGINE
        return PROV_UNATTRIBUTED


def _load_attribution(bundle: Path) -> Attribution:
    baseline = load_generated_artifact_baseline(bundle)
    if baseline is None:
        return Attribution(
            bundle,
            None,
            [
                "no usable generated_artifacts baseline in input_manifest.json - every difference is "
                "reported as unattributed. The delta below is real; the claim about WHO caused it is "
                "withheld, not guessed (issue #230).",
            ],
        )
    notes = []
    if baseline.get("coverage") == "slice_only_backfill":
        notes.append(
            "baseline was backfilled by `run_estate.py --slice-only`, not recorded at the engine's "
            "own run boundary: it proves nothing changed SINCE the backfill, not since the engine ran"
        )
    return Attribution(bundle, dict(baseline["files"]), notes)


def _unit_names(directory: Path, suffix: str) -> dict[str, Path]:
    if not directory.is_dir():
        return {}
    return {p.name[: -len(suffix)]: p for p in directory.iterdir() if p.is_dir() and p.name.endswith(suffix)}


def _working_artifacts(bundle: Path, suffix: str) -> list[tuple[str, str, Path]]:
    """Every shipping artifact of one kind under `pbip/`, as (artifact, unit, path).

    Returned as a LIST, not a dict: a shared published datasource is copied into every consuming
    unit, so one artifact name legitimately has several working copies (measured: `Meridian Calc
    Gauntlet (Live Snowflake).SemanticModel` exists in 3 units). Each copy is compared separately,
    because each is an independent emission that can diverge on its own.
    """
    pbip = bundle / "pbip"
    if not pbip.is_dir():
        return []
    found = []
    for unit_dir in sorted(p for p in pbip.iterdir() if p.is_dir()):
        for artifact_dir in sorted(p for p in unit_dir.iterdir() if p.is_dir() and p.name.endswith(suffix)):
            found.append((artifact_dir.name[: -len(suffix)], unit_dir.name, artifact_dir))
    return found


def discover_pairs(bundle: Path) -> list[Pair]:
    """Every baseline/working pair in the bundle, for both layers.

    A side that is missing is still returned, as None: an artifact with no engine baseline is a
    FINDING (issue #179), not an artifact to drop from the denominator.
    """
    pairs: list[Pair] = []
    for layer, baseline_dir, suffix in (
        (LAYER_REPORT, "reports", ".Report"),
        (LAYER_MODEL, "semantic_models", ".SemanticModel"),
    ):
        baselines = _unit_names(bundle / baseline_dir, suffix)
        matched: set[str] = set()
        for artifact, unit, working in _working_artifacts(bundle, suffix):
            matched.add(artifact)
            pairs.append(
                Pair(artifact=artifact, unit=unit, layer=layer, baseline=baselines.get(artifact), working=working)
            )
        for artifact in sorted(set(baselines) - matched):
            pairs.append(Pair(artifact=artifact, unit="", layer=layer, baseline=baselines[artifact], working=None))
    return pairs


def _declaring_scripts(bundle: Path) -> dict[str, str]:
    """Map bundle-relative target -> declaring script, from the declared-edit records."""
    out: dict[str, str] = {}
    for declaration in load_generated_edit_declarations(bundle):
        target = str(declaration.get("target", "")).replace("\\", "/")
        identity = declaration.get("script_identity")
        if target and identity:
            out[target] = str(identity)
    return out


def _base_record(pair: Pair, relative: str, kind: str, shapes: list[str], differences: int) -> dict[str, Any]:
    return {
        "artifact": pair.artifact,
        "unit": pair.unit,
        "layer": pair.layer,
        "path": relative,
        "kind": kind,
        "shapes": shapes,
        "differences": differences,
    }


def _changed_records(
    pair: Pair,
    delta: TreeDelta,
    roots: tuple[str, str],
    attribution: Attribution,
    declared: dict[str, str],
) -> list[dict[str, Any]]:
    """One record per file present on both sides with different content."""
    tables = bound_model_tables(pair.working) if pair.layer == LAYER_REPORT else None
    base_root, work_root = roots
    records = []
    for relative in delta.changed:
        shapes, count = shapes_for_change(pair.baseline / relative, pair.working / relative, tables)
        work_rel = f"{work_root}/{relative}"
        records.append(
            _base_record(pair, relative, "changed", shapes, count)
            | {
                "provenance": attribution.verdict(f"{base_root}/{relative}", work_rel),
                "declared_by": declared.get(work_rel),
            }
        )
    return records


def _added_removed_records(
    pair: Pair,
    delta: TreeDelta,
    roots: tuple[str, str],
    attribution: Attribution,
    declared: dict[str, str],
) -> list[dict[str, Any]]:
    """One record per file present on only one side."""
    base_root, work_root = roots
    records = []
    for relative, added in [(r, True) for r in delta.added] + [(r, False) for r in delta.removed]:
        side = f"{work_root}/{relative}" if added else f"{base_root}/{relative}"
        records.append(
            _base_record(pair, relative, "added" if added else "removed", [_added_removed_shape(relative, added)], 1)
            | {
                "provenance": attribution.verdict(None, side) if added else attribution.verdict(side, None),
                "declared_by": declared.get(side) if added else None,
            }
        )
    return records


def _difference_records(
    bundle: Path,
    pair: Pair,
    delta: TreeDelta,
    attribution: Attribution,
    declared: dict[str, str],
) -> list[dict[str, Any]]:
    """One record per differing file: its shape, its provenance, and who declared it (if anyone)."""
    if pair.baseline is None or pair.working is None:
        return []
    roots = (pair.baseline.relative_to(bundle).as_posix(), pair.working.relative_to(bundle).as_posix())
    return _changed_records(pair, delta, roots, attribution, declared) + _added_removed_records(
        pair, delta, roots, attribution, declared
    )


def _pair_status(pair: Pair, delta: TreeDelta | None) -> str:
    if pair.baseline is None:
        return PAIR_NO_BASELINE
    if pair.working is None or delta is None:
        return PAIR_NO_WORKING
    if delta.unassessable:
        return PAIR_UNASSESSABLE
    if delta.added or delta.removed or delta.changed:
        return PAIR_DIFFERS
    return PAIR_IDENTICAL


def _reference_resolves(pair: Pair) -> bool | None:
    """Whether the BASELINE report's dataset reference resolves. None when there is nothing to ask."""
    if pair.layer != LAYER_REPORT or pair.baseline is None:
        return None
    return bound_model_tables(pair.baseline) is not None


def _engine_metadata(bundle: Path) -> dict[str, Any]:
    receipt = bundle / ENGINE_RECEIPT
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    engine = payload.get("engine") if isinstance(payload, dict) else None
    if not isinstance(engine, dict):
        return {"available": False}
    return {
        "available": True,
        "version": engine.get("version"),
        "root": engine.get("root"),
        "canonical": engine.get("canonical"),
    }


def _layer_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(entry["status"] for entry in entries)
    assessed = [e for e in entries if e["status"] in {PAIR_IDENTICAL, PAIR_DIFFERS}]
    resolved = [e["baseline_reference_resolves"] for e in entries if e["baseline_reference_resolves"] is not None]
    return {
        "artifacts": len(entries),
        "pairs_assessed": len(assessed),
        "identical": statuses[PAIR_IDENTICAL],
        "differs": statuses[PAIR_DIFFERS],
        "unpaired_no_baseline": statuses[PAIR_NO_BASELINE],
        "unpaired_no_working": statuses[PAIR_NO_WORKING],
        "unassessable": statuses[PAIR_UNASSESSABLE],
        "files_changed": sum(e["files"]["changed"] for e in entries),
        "files_added": sum(e["files"]["added"] for e in entries),
        "files_removed": sum(e["files"]["removed"] for e in entries),
        "baseline_reference_resolves": sum(1 for r in resolved if r),
        "baseline_reference_checked": len(resolved),
    }


def _shape_rows(records: list[dict[str, Any]], denominator: int) -> list[dict[str, Any]]:
    files: Counter[str] = Counter()
    artifacts: dict[str, set[str]] = {}
    for record in records:
        for shape in record["shapes"]:
            files[shape] += 1
            artifacts.setdefault(shape, set()).add(f"{record['unit']}/{record['artifact']}")
    return [
        {
            "shape": shape,
            "files": count,
            "artifacts": len(artifacts[shape]),
            "share_of_differing_files": round(count / denominator, 4) if denominator else None,
        }
        for shape, count in files.most_common()
    ]


def _pair_entry(pair: Pair, delta: TreeDelta | None, status: str, pair_records: list[dict[str, Any]]) -> dict[str, Any]:
    """The per-pair row of the report."""
    return {
        "artifact": pair.artifact,
        "unit": pair.unit,
        "layer": pair.layer,
        "status": status,
        "files": {
            "changed": len(delta.changed) if delta else 0,
            "added": len(delta.added) if delta else 0,
            "removed": len(delta.removed) if delta else 0,
            "baseline": delta.baseline_files if delta else 0,
            "working": delta.working_files if delta else 0,
        },
        "longest_path": delta.longest_path if delta else None,
        "unassessable": len(delta.unassessable) if delta else 0,
        "provenance": dict(Counter(r["provenance"] for r in pair_records)),
        "shapes": sorted({s for r in pair_records for s in r["shapes"]}),
        "baseline_reference_resolves": _reference_resolves(pair),
    }


def _overall_status(
    entries: list[dict[str, Any]],
    tampered: list[dict[str, Any]],
    unassessable: list[dict[str, str]],
    attributable: bool,
) -> str:
    """`untrustworthy` beats `incomplete` beats `complete`.

    A tampered baseline outranks everything: the delta is not merely partial, it is wrong. An
    incomplete run is one whose numbers are real but do not cover the estate - unpaired artifacts,
    unreadable paths, or an unattributable bundle. Neither ever reports as clean.
    """
    if tampered:
        return STATUS_UNTRUSTWORTHY
    partial = any(e["status"] in {PAIR_NO_BASELINE, PAIR_NO_WORKING, PAIR_UNASSESSABLE} for e in entries)
    if unassessable or not attributable or partial or not entries:
        return STATUS_INCOMPLETE
    return STATUS_COMPLETE


def harvest(bundle: Path) -> dict[str, Any]:
    """Compare, attribute and classify one bundle. Returns the machine-readable report."""
    attribution = _load_attribution(bundle)
    declared = _declaring_scripts(bundle)

    entries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    unassessable: list[dict[str, str]] = []
    git_blind: list[dict[str, Any]] = []

    for pair in discover_pairs(bundle):
        delta = compare_trees(pair.baseline, pair.working) if pair.baseline and pair.working else None
        status = _pair_status(pair, delta)
        if delta is not None:
            unassessable.extend(delta.unassessable)
            if delta.longest_path > GIT_READABLE_PATH_MAX:
                git_blind.append(
                    {
                        "artifact": pair.artifact,
                        "unit": pair.unit,
                        "layer": pair.layer,
                        "longest_path": delta.longest_path,
                    }
                )
        pair_records = (
            _difference_records(bundle, pair, delta, attribution, declared)
            if delta is not None and status in {PAIR_DIFFERS, PAIR_UNASSESSABLE}
            else []
        )
        records.extend(pair_records)
        entries.append(_pair_entry(pair, delta, status, pair_records))

    provenance = Counter(record["provenance"] for record in records)
    tampered = [r for r in records if r["provenance"] == PROV_TAMPERED]
    status = _overall_status(entries, tampered, unassessable, attribution.usable)

    return {
        "version": REPORT_VERSION,
        "generated_at": _utcnow(),
        "bundle": str(bundle),
        "status": status,
        "engine": _engine_metadata(bundle),
        "attribution": {
            "usable": attribution.usable,
            "files_recorded": len(attribution.recorded) if attribution.recorded else 0,
            "notes": attribution.notes,
        },
        "layers": {
            layer: _layer_summary([e for e in entries if e["layer"] == layer]) for layer in (LAYER_REPORT, LAYER_MODEL)
        },
        "provenance": {name: provenance.get(name, 0) for name in PROVENANCES} | {"differing_files": len(records)},
        "shapes": _shape_rows(records, len(records)),
        "tier_edits": [r for r in records if r["provenance"] == PROV_TIER],
        "baseline_tampered": tampered,
        "pairs": entries,
        "unassessable": unassessable,
        "git_blind_spot": {
            "count": len(git_blind),
            "path_max": GIT_READABLE_PATH_MAX,
            "pairs": git_blind,
            "note": (
                "these pairs exceed the longest path git read on the measured corpus; the command"
                " AGENTS.md mandates returns exit 1 with NO stat line for them. They ARE assessed"
                " here - this module compares content in Python, which reads them."
            ),
        },
    }


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({round(100 * part / whole):d}%)" if whole else f"{part}/0 (n/a)"


def _layer_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    for layer, summary in report["layers"].items():
        if not summary["artifacts"]:
            continue
        lines.append(
            f"  {layer + ' layer':<14}: {_pct(summary['pairs_assessed'], summary['artifacts'])} assessed"
            f" | identical {summary['identical']}, differs {summary['differs']}"
            f" | no baseline {summary['unpaired_no_baseline']},"
            f" no working copy {summary['unpaired_no_working']},"
            f" unassessable {summary['unassessable']}"
        )
        lines.append(
            f"  {'':<14}  files: {summary['files_changed']} changed,"
            f" {summary['files_added']} added, {summary['files_removed']} removed"
        )
        if summary["baseline_reference_checked"]:
            lines.append(
                f"  {'':<14}  baseline dataset reference resolves:"
                f" {_pct(summary['baseline_reference_resolves'], summary['baseline_reference_checked'])}"
            )
    return lines


def render(report: dict[str, Any], top: int = DEFAULT_TOP) -> str:
    """Human-readable console report."""
    provenance = report["provenance"]
    total = provenance["differing_files"]
    lines = [
        f"{report['status'].upper()}: {report['bundle']}",
        f"  engine                : {report['engine'].get('version') or 'unknown'}"
        f" (canonical={report['engine'].get('canonical')})",
        f"  attribution           : {'hash-attributed' if report['attribution']['usable'] else 'NOT AVAILABLE'}"
        f" from {report['attribution']['files_recorded']} recorded artifacts",
    ]
    for note in report["attribution"]["notes"]:
        lines.append(f"      note              : {note}")
    lines.extend(_layer_lines(report))
    lines.append(f"  differing files       : {total}")
    for name in PROVENANCES:
        lines.append(f"      {name:<18}: {_pct(provenance[name], total)}")
    lines.append(
        "      -> only `tier_edit` answers 'what did the engine get wrong?'."
        " `engine_internal` is the engine's own reference-vs-bound difference."
    )
    if report["shapes"]:
        lines.append(f"  shapes (top {top})       :")
        for row in report["shapes"][:top]:
            share = f"{100 * row['share_of_differing_files']:.0f}%" if row["share_of_differing_files"] else "n/a"
            lines.append(f"      {row['files']:>5} files / {row['artifacts']:>3} artifacts  {share:>5}  {row['shape']}")
    if report["tier_edits"]:
        lines.append(f"  TIER EDITS            : {len(report['tier_edits'])} file(s) changed after the engine ran")
        for record in report["tier_edits"][:top]:
            declared = record["declared_by"] or "UNDECLARED"
            lines.append(
                f"      [{record['unit'] or record['artifact']}] {record['path']} {record['shapes']} <- {declared}"
            )
    if report["baseline_tampered"]:
        lines.append(
            f"  BASELINE TAMPERED     : {len(report['baseline_tampered'])} file(s) - the engine baseline"
            " itself drifted, so this delta cannot be read as engine behaviour"
        )
        for record in report["baseline_tampered"][:top]:
            lines.append(f"      [{record['unit'] or record['artifact']}] {record['path']}")
    if report["unassessable"]:
        lines.append(f"  UNASSESSABLE (not passed): {len(report['unassessable'])} path(s) could not be read")
        for record in report["unassessable"][:top]:
            lines.append(f"      {record['reason']}  {record['path']}")
    blind = report["git_blind_spot"]
    if blind["count"]:
        lines.append(
            f"  git blind spot        : {blind['count']} pair(s) exceed {blind['path_max']} characters -"
            " the AGENTS.md `git diff --no-index` form returns exit 1 with NO stat line for these."
            " Assessed here anyway."
        )
        for record in blind["pairs"][:top]:
            lines.append(f"      {record['longest_path']:>4}  [{record['layer']}] {record['unit']}")
    return "\n".join(lines)


def _markdown_shape_table(report: dict[str, Any], top: int) -> list[str]:
    lines = ["| shape | files | artifacts | share of differing files |", "|---|---:|---:|---:|"]
    for row in report["shapes"][:top]:
        share = f"{100 * row['share_of_differing_files']:.0f}%" if row["share_of_differing_files"] else "n/a"
        lines.append(f"| `{row['shape']}` | {row['files']} | {row['artifacts']} | {share} |")
    return lines


def render_markdown(report: dict[str, Any], top: int = DEFAULT_TOP) -> str:
    """An upstream-fileable summary: frequencies with denominators, and explicit non-claims."""
    provenance = report["provenance"]
    total = provenance["differing_files"]
    lines = [
        "# Engine-gap harvest",
        "",
        f"- bundle: `{report['bundle']}`",
        f"- engine: **{report['engine'].get('version') or 'unknown'}**"
        f" (canonical: {report['engine'].get('canonical')})",
        f"- harvested: {report['generated_at']}",
        f"- status: **{report['status']}**",
        f"- attribution: {'hash-attributed' if report['attribution']['usable'] else '**unavailable**'}"
        f" from {report['attribution']['files_recorded']} recorded artifacts",
        "",
        "## Coverage, per layer",
        "",
        "| layer | artifacts | assessed | identical | differs | no baseline | no working copy | unassessable |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for layer, summary in report["layers"].items():
        lines.append(
            f"| {layer} | {summary['artifacts']} | {summary['pairs_assessed']} | {summary['identical']} |"
            f" {summary['differs']} | {summary['unpaired_no_baseline']} |"
            f" {summary['unpaired_no_working']} | {summary['unassessable']} |"
        )
    lines += [
        "",
        "## Who wrote the difference",
        "",
        "| provenance | files | share |",
        "|---|---:|---:|",
    ]
    for name in PROVENANCES:
        share = f"{round(100 * provenance[name] / total)}%" if total else "n/a"
        lines.append(f"| `{name}` | {provenance[name]} | {share} |")
    lines += [
        "",
        "> `engine_internal` means the engine wrote **both** sides - its reference-only emission and"
        " its bound working copy. That is a by-design difference and is **not** evidence of an engine"
        ' defect. Only `tier_edit` answers *"what did a human or agent have to change?"*.',
        "",
        "## What changed",
        "",
    ]
    lines += _markdown_shape_table(report, top)
    if report["tier_edits"]:
        lines += ["", "## Tier edits (the engine-gap evidence)", ""]
        lines += ["| unit | layer | file | shapes | declared by |", "|---|---|---|---|---|"]
        for record in report["tier_edits"][:top]:
            lines.append(
                f"| {record['unit']} | {record['layer']} | `{record['path']}` |"
                f" {', '.join(record['shapes'])} | {record['declared_by'] or '**undeclared**'} |"
            )
    else:
        lines += [
            "",
            "## Tier edits (the engine-gap evidence)",
            "",
            "**None.** Every differing byte in this bundle is still hash-identical to what the engine"
            " itself recorded, so nothing here shows work a human or agent had to do. A bundle with no"
            " fix pass cannot answer issue #274's question, and this report does not pretend it can.",
        ]
    lines += ["", "## What this does not say", ""]
    lines += [
        "- **Not effort.** File and line counts are not hours; a reformat and a fidelity fix count the same.",
        "- **Not why.** Provenance says who, shape says what; the reason lives in the handover and"
        " `limitations_encountered`.",
        "- **Not a defect list.** `engine_internal` differences are by construction not defect evidence.",
    ]
    if report["unassessable"]:
        lines.append(
            f"- **{len(report['unassessable'])} path(s) could not be read** and are excluded from every count above."
        )
    return "\n".join(lines) + "\n"


def _emit(text: str, stream) -> None:
    """Print one line, degrading only characters this stream cannot encode."""
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None)
        if isinstance(encoding, str):
            try:
                print(text.encode(encoding, "backslashreplace").decode(encoding, "replace"), file=stream)
                return
            except LookupError:  # pragma: no cover - an encoding name Python does not know
                pass
        print(text.encode("ascii", "backslashreplace").decode("ascii"), file=stream)


def _exit_code(reports: list[dict[str, Any]]) -> int:
    if any(r["status"] == STATUS_UNTRUSTWORTHY for r in reports):
        return EXIT_UNTRUSTWORTHY
    if any(r["status"] == STATUS_INCOMPLETE for r in reports):
        return EXIT_INCOMPLETE
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Harvest the engine-gap evidence in a migration bundle's baseline/working delta.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("bundles", nargs="+", type=Path, help="migration bundle director(ies)")
    parser.add_argument("--json", type=Path, help="also write the machine-readable report here")
    parser.add_argument("--markdown", type=Path, help="also write an upstream-fileable markdown summary here")
    parser.add_argument("--quiet", action="store_true", help="print only the verdict line")
    parser.add_argument("--warn-only", action="store_true", help="report findings but always exit 0")
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP, help=f"rows to name per section (default {DEFAULT_TOP})"
    )
    args = parser.parse_args(argv)

    missing = [str(b) for b in args.bundles if not b.is_dir()]
    if missing:
        _emit(f"ERROR: not a directory: {', '.join(missing)}", sys.stderr)
        return EXIT_USAGE
    if args.top < 1:
        _emit("ERROR: --top must be >= 1", sys.stderr)
        return EXIT_USAGE

    reports = [harvest(bundle.resolve()) for bundle in args.bundles]

    # The machine-readable artifacts are written BEFORE anything is printed: console rendering can
    # fail on a path this terminal cannot encode, and an ordering that printed first would destroy
    # the very output an automated consumer asked for. `--json` is a contract.
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = reports[0] if len(reports) == 1 else {"version": REPORT_VERSION, "bundles": reports}
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text("\n".join(render_markdown(r, args.top) for r in reports), encoding="utf-8")

    for report in reports:
        _emit(f"{report['status']}: {report['bundle']}" if args.quiet else render(report, args.top), sys.stdout)

    return EXIT_OK if args.warn_only else _exit_code(reports)


if __name__ == "__main__":
    raise SystemExit(main())
