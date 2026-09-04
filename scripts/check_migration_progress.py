"""
purpose: tell an orchestrator whether a delegated migration is PROGRESSING or SPINNING, from the
         artifacts on disk rather than from the subagent's own narrative.
usage:   python scripts/check_migration_progress.py --bundle <dir> [--since-minutes 20] [--json]
         python scripts/check_migration_progress.py --bundle <dir> --baseline 2026-08-09T20:00:00+02:00
         python scripts/check_migration_progress.py --bundle <dir> --tamper

Why this exists
---------------
Measured 2026-08-07, four migrations running in parallel. Two of them passed 100 minutes on their
first turn. Elapsed time could not tell them apart:

* one had written **27 model files and 148 report files** and was still going - it had 30 stubbed
  calculations to author, which is genuinely the hardest work in the corpus;
* the other had written **zero report files in 105 minutes**, and its whole report tree still carried
  a single emission timestamp, while it accumulated screenshots and DAX probes in a scratch folder.

Both looked identical to a stopwatch and to a "still running" status. What separated them is
**deliverable output over time**: edits to the semantic model and the report, as opposed to activity
in scratch. That file signal is still incomplete during read-heavy triage, so callers can add the
runtime signal with `--liveness active` when the tool-call count is climbing.

This is deliberately NOT a kill switch. Its output is a prompt to ASK - the orchestrator's job when
this reports STALLED is to make the subagent report what it is blocked on, which is the thing that
did not happen for 105 minutes.

The tamper mode is an audit record, not a security boundary. A declaration is written by the same
script that changed the artifact, so it proves the edit was made visible and replayable; it does not
authorize the edit or protect against someone hand-editing the declaration JSON.

⚠️ **BOTH modes look at the phase-2 packages, not only at the bundle.** Since #460 was settled - the
package's `fabric/` is the canonical place an agent edits, and `promote_unit.py` ships FROM it - a
gate pointed only at `<bundle>/pbip/` is looking in the tree the work is NOT in. Measured before this
was fixed, on a package whose model had just been edited: `--tamper` reported
`CLEAN  "3 generated artifact(s) are pristine against their engine-run hashes"` at exit 0, and
progress reported `SILENT  deliverable_count_seen=0` for an agent that had been producing
deliverables the whole time - the precise signal an orchestrator reads as "this subagent is dead".
Packages are found beside the bundle by default (`_runs/<NNN>-<slug>/{bundle,packages}`);
`--packages` names a different root.

⚠️ **A stall is not a failure, and a fast run is not a success.** A migration can legitimately go
quiet while Power BI Desktop loads a model (~60-90s) or an XMLA refresh runs (measured 93s), and a
correct early STOP - an unreachable source, a missing credential - produces almost no artifacts at
all and is the RIGHT outcome. Read the verdict together with what the run was asked to do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

try:
    from generated_edit_declarations import load_generated_edit_declarations as _load_generated_edit_declarations
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from generated_edit_declarations import load_generated_edit_declarations as _load_generated_edit_declarations

# Windows defaults stdout/stderr to the legacy cp1252 codec, which cannot encode the non-ASCII
# characters (e.g. the warning glyph above) in this module's own docstring -- argparse's --help
# crashes with UnicodeEncodeError before printing anything. Force UTF-8 so --help and any print()
# of the same characters work the same on every platform.
for _stream in (sys.stdout, sys.stderr):
    # pylint: disable-next=no-member  # astroid mis-infers TextIOWrapper.encoding as a class here
    if _stream is not None and _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

LOG = logging.getLogger("check_migration_progress")

# Buckets, in the order a migration produces them. `deliverable` is what the user gets; `scratch` is
# how the agent got there. The distinction IS the measurement - see the module docstring.
# Matched against path COMPONENTS relative to the bundle, never against the absolute path.
DELIVERABLE_SUFFIXES = (".semanticmodel", ".report", ".pbip")
SCRATCH_DIRS = frozenset({"scratch", "_work", "_build", "_probe", "tmp", "temp", "_shots"})
SCRATCH_INTENTS = frozenset(part.lstrip("._") for part in SCRATCH_DIRS)
GENERATED_ARTIFACTS_KEY = "generated_artifacts"
GENERATED_EDIT_DECLARATIONS = Path("_build") / "generated-edit-declarations.json"
GENERATED_EDIT_DECLARATIONS_DIR = Path("_build") / "generated-edit-declarations"
VOLATILE_GENERATED_DIRS = {".pbi"}

#: The phase-2 handover packages `package_unit.py` writes, and the manifest that makes one
#: self-describing. Since #460 was settled the package's `fabric/` is the CANONICAL place an agent
#: edits, and `promote_unit.py` ships FROM it - so a gate that only ever looked in the bundle was
#: looking in the wrong tree. Measured before this: edit a canonical package, and `--tamper` reports
#: `CLEAN  "3 generated artifact(s) are pristine against their engine-run hashes"` at exit 0 while
#: progress reports `SILENT  deliverable_count_seen=0` for an agent that had been working all along.
PACKAGES_DIRNAME = "packages"
PACKAGE_MANIFEST_NAME = "package-manifest.json"
#: How deep under a `packages/` root a `<Unit>/package-manifest.json` may sit. The documented layout
#: is `packages/<batch>/<Unit>/`, so 2 is the real depth; 3 leaves room for one nesting level a
#: future batch convention might add without turning this into an unbounded tree walk.
PACKAGE_SEARCH_DEPTH = 3

# A Desktop model load is 60-90s and a refresh + ImageSave was measured at 93s, so a window shorter
# than this cannot distinguish "loading" from "stuck" and must not try.
SHORT_WINDOW_MINUTES = 10
BURST_GRACE_MULTIPLIER = 2
LIVENESS_ACTIVE = "active"
LIVENESS_UNKNOWN = "unknown"


def parse_baseline(value: str) -> datetime:
    """Parse the delegation baseline supplied by the orchestrator."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 baseline: {value}") from ex
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def classify(path: Path) -> str:
    """Bucket one path: deliverable, scratch, or other.

    ⚠️ **Pass a path RELATIVE to the bundle, and match on path COMPONENTS.** The first version
    lower-cased the absolute path and substring-matched it, which meant any bundle living under
    `...\\AppData\\Local\\Temp\\` had every file classified as scratch - and so would a customer
    working in `C:\\work\\migrations\\`, which would report STALLED forever regardless of output.
    Component matching also stops `temp` matching `template` and `tmp` matching `tmpl`.
    """
    parts = [part.lower() for part in path.parts]
    if any(part.lstrip("._") in SCRATCH_INTENTS for part in parts):
        return "scratch"
    if any(part.endswith(DELIVERABLE_SUFFIXES) for part in parts):
        return "deliverable"
    return "other"


def sha256_file(path: Path) -> str:
    """Hash a generated artifact from disk."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_generated_artifact(path: Path, bundle: Path) -> bool:
    """Stable generated output whose drift should be declared.

    Excludes ``.pbi`` sidecars because refreshes and Desktop autosaves legitimately rewrite them.
    """
    if not path.is_file():
        return False
    relative = path.relative_to(bundle)
    lower_parts = [part.lower() for part in relative.parts]
    if any(part in VOLATILE_GENERATED_DIRS for part in lower_parts) or classify(relative) == "scratch":
        return False
    if path.suffix.lower() == ".pbip":
        return True
    return any(part.endswith((".semanticmodel", ".report")) for part in lower_parts)


def current_generated_artifacts(bundle: Path) -> set[str]:
    """Current stable generated files, keyed by POSIX relative path."""
    return {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if _is_generated_artifact(path, bundle)}


def _baseline_binds_current_report(bundle: Path, generated: dict[str, Any]) -> bool:
    """Whether the manifest's run evidence still matches this bundle's report.json."""
    report_path = bundle / "report.json"
    if not report_path.is_file() or sha256_file(report_path) != generated.get("report_sha256"):
        return False
    if generated.get("report_generated_at") is None:
        return True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report.get("generated_at") == generated.get("report_generated_at")


def _valid_generated_baseline(bundle: Path, generated: Any) -> bool:
    """Validate the generated-artifact baseline before trusting it."""
    if not isinstance(generated, dict) or generated.get("version") != 1:
        return False
    if not generated.get("run_id") or not generated.get("report_sha256"):
        return False
    try:
        datetime.fromisoformat(str(generated.get("recorded_at")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return _baseline_binds_current_report(bundle, generated) and isinstance(generated.get("files"), dict)


def load_generated_artifact_baseline(bundle: Path) -> dict[str, Any] | None:
    """Generated-file baseline recorded by ``run_estate.py``."""
    manifest_path = bundle / "input_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    generated = manifest.get(GENERATED_ARTIFACTS_KEY) if isinstance(manifest, dict) else None
    if not _valid_generated_baseline(bundle, generated):
        return None
    files = generated["files"]
    generated["files"] = {str(path).replace("\\", "/"): str(digest) for path, digest in files.items()}
    return generated


def _no_baseline_verdict(bundle: Path) -> tuple[str, list[str]]:
    """Distinguish 'no baseline was ever attempted' (by design) from 'the baseline is unusable'.

    Doctrine (``check_stub_measures.py``): "'no stubs' and 'no model' must never print or exit the
    same way." Applied here (issue #230): a bundle shape that structurally never records a baseline -
    one produced by ``run_estate.py --slice-only`` before it started backfilling one, or by running
    the engine outside ``run_estate.py`` entirely - is EXPECTED ABSENCE, not tampering: the manifest
    is fine, it simply never carried this key. A missing/corrupt ``input_manifest.json``, or a
    ``generated_artifacts`` entry that IS present but does not validate (wrong version, unparsable
    timestamp, bound to a different report), stays the ORIGINAL, more suspicious ``NO_BASELINE``:
    something that should be there is broken, absent, or was tampered with.
    """
    manifest_path = bundle / "input_manifest.json"
    if not manifest_path.is_file():
        return "NO_BASELINE", ["no input_manifest.json in bundle - cannot check for a baseline at all"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return "NO_BASELINE", [f"input_manifest.json is not valid JSON ({exc}) - cannot check for a baseline"]
    if not isinstance(manifest, dict):
        return "NO_BASELINE", ["input_manifest.json is not a JSON object - cannot check for a baseline"]
    if GENERATED_ARTIFACTS_KEY not in manifest:
        return "NO_BASELINE_BY_DESIGN", [
            "no generated_artifacts baseline in input_manifest.json, and none was ever recorded - this "
            "bundle shape (e.g. built with `run_estate.py --slice-only` before it backfilled one, or an "
            "engine run outside run_estate.py) never had one. This is EXPECTED ABSENCE, not tampering: "
            "tamper detection was never armed for this bundle. Re-run `run_estate.py --slice-only "
            f"--output {bundle}` to backfill a best-effort baseline, or treat any signal drawn from "
            "this bundle as usable but NOT cryptographically attested (issue #230)."
        ]
    return "NO_BASELINE", [
        "generated_artifacts baseline in input_manifest.json is present but invalid, or does not match "
        "this bundle's report.json"
    ]


def load_generated_edit_declarations(bundle: Path) -> list[dict[str, Any]]:
    """Structured declarations written by refresh/fix tooling."""
    return _load_generated_edit_declarations(bundle)


class GeneratedArtifactSnapshot(NamedTuple):
    """ONE read of the generated-artifact inventory, so two consumers can share one filesystem state.

    ⚠️ Exists because reading the disk twice is not the same as reading it once. Measured (blind
    review round 6 of PR #399): ``harvest_engine_gaps.py`` adjudicated provenance here and only THEN
    walked the bundle's trees, so an edit landing between the two operations was described by both at
    once - the comparison saw the NEW bytes while the stale adjudication still called that path
    pristine. One injected working-visual edit produced ``status=complete``, ``differing_files=1`` and
    ``engine_internal=1`` (a TIER edit attributed to the ENGINE) while :func:`tamper_check` returned
    ``DRIFT`` on the same bundle a second later.

    ``hashes`` covers the recorded inventory UNION whatever is on disk now, so a newly added artifact
    carries a digest too; the value is ``None`` for a path that is not a file. ``current`` is kept
    rather than derived from ``hashes`` because "is this a generated artifact" is a classification
    question (:func:`_is_generated_artifact`), not a "does it hash" one.

    ⚠️ Round 6 also compared two of these end to end and called the difference a race. Round 7 killed
    that: an ABA edit restores the original bytes, both endpoints match, and the consumer's own read
    of the changed bytes goes unnoticed. A snapshot is an AUTHORITY to check evidence against - never
    evidence in its own right that nothing happened.
    """

    hashes: dict[str, str | None]
    current: frozenset[str]


def observe_generated_artifacts(bundle: Path, baseline: dict[str, str]) -> GeneratedArtifactSnapshot:
    """Read the whole generated-artifact inventory once, for a caller that must not read it twice."""
    current = frozenset(current_generated_artifacts(bundle))
    hashes: dict[str, str | None] = {}
    for relative in sorted(set(baseline) | current):
        path = bundle / Path(relative)
        hashes[relative] = sha256_file(path) if path.is_file() else None
    return GeneratedArtifactSnapshot(hashes, current)


def _artifact_drift(
    bundle: Path,
    baseline: dict[str, str],
    snapshot: GeneratedArtifactSnapshot | None = None,
) -> list[tuple[str, str]]:
    """Changed, missing, or newly-added generated artifacts.

    ``snapshot`` lets a caller that has ALREADY read the inventory adjudicate against that exact
    read instead of taking a second, later one. Omit it and the behaviour is unchanged: this reads
    the disk itself.
    """
    seen = observe_generated_artifacts(bundle, baseline) if snapshot is None else snapshot
    drift = []
    for relative, expected in sorted(baseline.items()):
        actual = seen.hashes.get(relative)
        if actual is None:
            drift.append((relative, "missing"))
        elif actual != expected:
            drift.append((relative, "changed"))
    for relative in sorted(seen.current - set(baseline)):
        drift.append((relative, "added"))
    return drift


def _current_hash(bundle: Path, relative: str, snapshot: GeneratedArtifactSnapshot | None = None) -> str | None:
    """Hash the current target, or ``None`` when the declared outcome is deletion."""
    if snapshot is not None and relative in snapshot.hashes:
        return snapshot.hashes[relative]
    path = bundle / Path(relative)
    return sha256_file(path) if path.is_file() else None


def _declaration_matches(
    declaration: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    """Whether one declaration proves one current drift item."""
    actual = {key: declaration.get(key) for key in expected}
    return (
        declaration.get("version", 1) == 1
        and actual == expected
        and bool(declaration.get("script_identity"))
        and bool(declaration.get("script_sha256"))
    )


def _matching_declaration(
    declarations: list[dict[str, Any]],
    generated: dict[str, Any],
    relative: str,
    kind: str,
    current_hash: str | None,
) -> dict[str, Any] | None:
    """A declaration is evidence only when it is tied to this run and this exact outcome."""
    expected = {
        "run_id": str(generated["run_id"]),
        "kind": kind,
        "target": relative,
        "baseline_sha256": generated["files"].get(relative),
        "expected_sha256": current_hash,
    }
    for declaration in declarations:
        declaration = declaration | {"target": str(declaration.get("target", "")).replace("\\", "/")}
        if _declaration_matches(declaration, expected):
            return declaration
    return None


class UnreadableDeclarations(RuntimeError):
    """The declaration ledger exists but cannot be read, so drift cannot be adjudicated.

    A DISTINCT outcome on purpose. Reporting unreadable evidence as ``DRIFT`` would give it the exit
    code of a positively detected undeclared edit, and those are different situations: one says "an
    artifact moved and nobody declared it", the other says "an artifact moved and the ledger that
    might exonerate it is corrupt".
    """


def adjudicate_generated_drift(
    bundle: Path,
    generated: dict[str, Any],
    snapshot: GeneratedArtifactSnapshot | None = None,
) -> list[dict[str, Any]]:
    """Every generated artifact that moved since the engine ran, with its declaration verdict.

    The structured core of :func:`tamper_check`, exposed as a PUBLIC contract so a second consumer
    can adjudicate provenance with THIS machinery instead of re-deriving weaker rules of its own
    (issue #274). ``harvest_engine_gaps.py`` is that consumer: a blind review found its own
    hand-rolled attribution missed a baseline rewrite, a post-engine file addition, a post-engine
    deletion and a stale declaration - all four of which this function already reported as drift.

    Returns one record per moved artifact: ``target`` (bundle-relative POSIX path), ``kind``
    (``changed`` / ``missing`` / ``added``), and ``declared_by`` - the declaring script identity,
    populated ONLY when a declaration ties this run id, this baseline hash, this operation and this
    exact resulting hash together. Anything weaker is undeclared, which is the point.

    ⚠️ **Drift is computed FIRST, and declarations are loaded only if there is drift to adjudicate.**
    The order is load-bearing, not stylistic. An earlier version of this extraction loaded the ledger
    up front, and a bundle whose generated artifacts were entirely PRISTINE then depended on data
    that could not affect its verdict: measured (blind review of PR #399), invalid UTF-8 in either
    supported declaration location turned a `CLEAN` verdict into an uncaught ``UnicodeDecodeError``
    and a CLI traceback exiting 1 - the same numeric code as a real ``DRIFT``. Declarations are
    evidence ABOUT drift; with no drift there is nothing for them to be evidence about.

    ``snapshot`` is an already-taken :class:`GeneratedArtifactSnapshot`. Pass one when the verdict
    must describe the SAME filesystem state as some other read the caller made; omit it and this
    reads the inventory itself, exactly as before.
    """
    drift = _artifact_drift(bundle, generated["files"], snapshot)
    if not drift:
        return []
    try:
        declarations = load_generated_edit_declarations(bundle)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise UnreadableDeclarations(
            f"declaration ledger under {GENERATED_EDIT_DECLARATIONS.parent.as_posix()} could not be read "
            f"({type(exc).__name__}: {exc}), so {len(drift)} drifted artifact(s) cannot be adjudicated. "
            "This is NOT the same as undeclared drift - repair or remove the ledger and re-run."
        ) from exc
    adjudicated: list[dict[str, Any]] = []
    for relative, kind in drift:
        declaration = _matching_declaration(
            declarations,
            generated,
            relative,
            kind,
            _current_hash(bundle, relative, snapshot),
        )
        adjudicated.append(
            {
                "target": relative,
                "kind": kind,
                "declared_by": declaration["script_identity"] if declaration else None,
            }
        )
    return adjudicated


def discover_package_roots(bundle: Path, explicit: Path | None = None) -> list[Path]:
    """Every phase-2 package directory this bundle is accountable for.

    A package is identified by carrying its own `package-manifest.json`, never by its position, so
    the discovery cannot be fooled by a batch directory named something unexpected.

    ⚠️ **Auto-discovery is the point, not a convenience.** The reproduction that made this necessary
    is the DOCUMENTED command - `check_migration_progress.py --bundle <b> --tamper`, no new flag -
    reporting CLEAN while the canonical package carried an edit. An opt-in flag would leave that
    command exactly as wrong as it was. `--packages` exists for a layout the canonical one does not
    cover, and it overrides the search rather than adding to it.

    The canonical layout is `_runs/<NNN>-<slug>/{bundle,packages}` (`work_dirs.RunPaths`), so the
    default search is the bundle's SIBLING `packages/`, plus a `packages/` nested inside the bundle
    for the ad-hoc layout an operator gets by passing `--out <bundle>/packages`.
    """
    roots = [explicit] if explicit is not None else [bundle.parent / PACKAGES_DIRNAME, bundle / PACKAGES_DIRNAME]
    found: set[Path] = set()
    for root in roots:
        if root is None or not root.is_dir():
            continue
        if (root / PACKAGE_MANIFEST_NAME).is_file():
            found.add(root)
        for depth in range(1, PACKAGE_SEARCH_DEPTH + 1):
            pattern = "/".join(["*"] * depth) + f"/{PACKAGE_MANIFEST_NAME}"
            found.update(manifest.parent for manifest in root.glob(pattern) if manifest.is_file())
    return sorted(found)


def package_edit_state(package: Path) -> tuple[list[str], str | None]:
    """`(files that differ from what packaging recorded, reason it could not be established)`.

    Delegates to `package_unit.package_edits` rather than re-deriving the comparison: two
    implementations of "has this package been edited" would answer differently the first time either
    changed, and this gate exists to make that answer visible.
    """
    from package_unit import package_edits  # pylint: disable=import-outside-toplevel

    return package_edits(package)


def package_tamper_check(roots: list[Path]) -> tuple[str, list[str]]:
    """Have the CANONICAL packages been edited since packaging wrote them?

    ⚠️ **"No packages" and "packages all pristine" must not print the same**, which is the same
    doctrine `_no_baseline_verdict` applies one layer up: a silent absence reads as a clean bill of
    health for a tree nobody looked at. A run that has not reached phase 2 yet is a legitimate,
    NAMED state here.

    A package edit is reported, not condemned. `<package>/fabric/` is where an agent is *supposed*
    to work (#460, settled by `promote_unit.py`, which ships from it) - so the finding is "the
    canonical tree carries work that the engine-run hashes cannot account for", which is exactly
    what an orchestrator needs to know before promoting, and exactly what a CLEAN verdict used to
    hide.
    """
    if not roots:
        return "CLEAN", ["no phase-2 packages found beside this bundle - nothing canonical to check"]
    edited: list[str] = []
    unassessable: list[str] = []
    notes: list[str] = []
    for package in roots:
        changed, reason = package_edit_state(package)
        if reason is not None:
            unassessable.append(package.name)
            notes.append(f"PACKAGE UNASSESSABLE: {package.name} - {reason}")
            continue
        if changed:
            edited.append(package.name)
            notes.append(
                f"PACKAGE EDITED: {package.name} - {len(changed)} file(s) differ from what packaging "
                f"wrote: {', '.join(changed[:5])}" + (" ..." if len(changed) > 5 else "")
            )
    if not edited and not unassessable:
        return "CLEAN", [f"{len(roots)} phase-2 package(s) still match their own package-manifest.json digest"]
    if edited:
        return "PACKAGE_DRIFT", notes
    return "PACKAGE_UNASSESSABLE", notes


def tamper_check(bundle: Path, package_roots: list[Path] | None = None) -> tuple[str, list[str]]:
    """Detect generated artifacts that changed without structured declaration evidence.

    Two trees, because since #460 there are two: the engine bundle, adjudicated against the
    engine-run baseline in `input_manifest.json`, and the phase-2 packages, adjudicated against each
    package's own `package-manifest.json` digest. A bundle-level verdict wins when it is already
    non-clean - it is about the baseline everything else is measured from - but a package finding
    REPLACES a would-be clean one, so `--tamper` can no longer report CLEAN for a tree it never
    looked at.
    """
    state, notes = _bundle_tamper_check(bundle)
    package_state, package_notes = package_tamper_check(
        package_roots if package_roots is not None else discover_package_roots(bundle)
    )
    notes = [*notes, *package_notes]
    if package_state != "CLEAN" and state in {"CLEAN", "DECLARED_DRIFT"}:
        return package_state, notes
    return state, notes


def _bundle_tamper_check(bundle: Path) -> tuple[str, list[str]]:
    """The bundle half: generated artifacts against the engine-run hashes in `input_manifest.json`."""
    generated = load_generated_artifact_baseline(bundle)
    if generated is None:
        return _no_baseline_verdict(bundle)

    # A baseline `run_estate.py --slice-only` backfills has no engine-run boundary behind it - it can
    # prove nothing changed SINCE it was recorded, but not before. Say so on every verdict this
    # baseline produces, pass or fail, rather than only on the ones that would otherwise look silent.
    coverage_notes = (
        [
            "PARTIAL COVERAGE: this baseline was backfilled by `run_estate.py --slice-only` rather "
            "than recorded at the engine's own run boundary, so it can prove nothing changed SINCE the "
            "backfill but cannot see drift from before it. Treat as usable signal, not full attestation."
        ]
        if generated.get("coverage") == "slice_only_backfill"
        else []
    )

    baseline = generated["files"]
    try:
        adjudicated = adjudicate_generated_drift(bundle, generated)
    except UnreadableDeclarations as exc:
        return "UNREADABLE_DECLARATIONS", [str(exc), *coverage_notes]
    if not adjudicated:
        return "CLEAN", [
            f"{len(baseline)} generated artifact(s) are pristine against their engine-run hashes",
            *coverage_notes,
        ]

    notes = []
    undeclared = []
    for item in adjudicated:
        relative, kind = item["target"], item["kind"]
        if item["declared_by"]:
            notes.append(f"DECLARED {kind}: {relative} via {item['declared_by']}")
        else:
            undeclared.append((relative, kind))
            notes.append(
                f"UNDECLARED {kind}: {relative} - record target, baseline hash and expected post-fix hash in "
                f"{GENERATED_EDIT_DECLARATIONS.as_posix()}"
            )
    return ("DRIFT" if undeclared else "DECLARED_DRIFT"), [*notes, *coverage_notes]


def _record_write(buckets: dict[str, dict[str, Any]], name: str, written: datetime, relative: Path) -> None:
    """Fold one file's write into a bucket, keeping the newest example."""
    bucket = buckets[name]
    bucket["count"] += 1
    if bucket["newest"] is None or written > bucket["newest"]:
        bucket["newest"] = written
        bucket["example"] = str(relative)


def scan(
    bundle: Path,
    since: datetime,
    baseline: datetime | None = None,
    package_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Count files by bucket within the window, and record the newest write in each.

    `newest_overall` is tracked separately and ignores the window, because "nothing in the last 30
    minutes" has two very different meanings: a run that never started (the delegation failed) and a
    run that went quiet an hour ago (finished, blocked on a human, or dead). Without it the tool
    reports "has this run started?" for a bundle that has been working all evening.

    ⚠️ **The phase-2 packages are scanned TOO, and that is the whole of a measured false SILENT.**
    Since #460 was settled the canonical place an agent edits is `<package>/fabric/`, which lives
    beside the bundle rather than inside it - so a gate that scanned only the bundle reported
    `SILENT  deliverable_count_seen=0` for an agent that had been producing deliverables the whole
    time, which is the exact signal an orchestrator reads as "this subagent is dead". Package files
    are classified by the same :func:`classify` on a path relative to the package's PARENT, so
    `<Unit>/fabric/<Model>.SemanticModel/...` lands in `deliverable` on its own merits.
    """
    buckets: dict[str, dict[str, Any]] = {
        b: {"count": 0, "newest": None, "example": None} for b in ("deliverable", "scratch", "other")
    }
    overall_buckets: dict[str, dict[str, Any]] = {
        b: {"count": 0, "newest": None, "example": None} for b in ("deliverable", "scratch", "other")
    }
    newest_overall: datetime | None = None
    cutoff = max(since, baseline) if baseline else since
    observed_minutes = max(0.0, (datetime.now() - cutoff).total_seconds() / 60)
    for root, anchor in [(bundle, bundle), *[(package, package.parent) for package in package_roots or []]]:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                written = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if baseline and written < baseline:
                continue
            relative = path.relative_to(anchor)
            bucket_name = classify(relative)
            if newest_overall is None or written > newest_overall:
                newest_overall = written
            _record_write(overall_buckets, bucket_name, written, relative)
            if written >= cutoff:
                _record_write(buckets, bucket_name, written, relative)
    return {
        "buckets": buckets,
        "overall_buckets": overall_buckets,
        "files_total": sum(bucket["count"] for bucket in overall_buckets.values()),
        "newest_overall": newest_overall,
        "baseline": baseline,
        "observed_minutes": observed_minutes,
        "packages_scanned": [str(package) for package in package_roots or []],
    }


def _age_minutes(written: datetime) -> float:
    """Minutes since a filesystem write."""
    return (datetime.now() - written).total_seconds() / 60


def _window_text(scanned: dict[str, Any], window_minutes: int) -> str:
    """Human label for the actual observed window after a baseline is applied."""
    observed = scanned.get("observed_minutes", window_minutes)
    if abs(observed - window_minutes) < 0.5:
        return f"{window_minutes}m"
    return f"{observed:.0f}m observed after baseline"


def _quiet_verdict(scanned: dict[str, Any], window_minutes: int, liveness: str, window: str) -> tuple[str, str]:
    """Verdict for a window with no current writes in any bucket."""
    if liveness == LIVENESS_ACTIVE:
        newest_overall = scanned.get("newest_overall")
        file_signal = (
            "no file writes" if not newest_overall else f"last file write {_age_minutes(newest_overall):.0f}m ago"
        )
        return (
            "THINKING",
            (
                f"{file_signal}, but the external runtime liveness signal is active (tool-call count is climbing). "
                "Treat this as a read-heavy phase and re-check; file mtimes alone cannot prove a stall."
            ),
        )

    prior_deliverable = scanned.get("overall_buckets", {}).get("deliverable", {})
    if prior_deliverable.get("newest"):
        age = _age_minutes(prior_deliverable["newest"])
        burst_grace = window_minutes * BURST_GRACE_MULTIPLIER
        if age <= burst_grace:
            return (
                "THINKING",
                (
                    f"no deliverables in the last {window}, but this bundle has recent deliverable output "
                    f"(last {age:.0f}m ago: {prior_deliverable.get('example')}). Agents write in bursts; "
                    "use this age and runtime liveness before interrupting it."
                ),
            )
        return (
            "SILENT",
            (
                f"nothing written in the last {window}; last deliverable was {age:.0f}m ago "
                f"({prior_deliverable.get('example')}). Finished, blocked on a human, or dead - "
                "check whether it is waiting on a credential before assuming it is stuck."
            ),
        )

    newest_overall = scanned.get("newest_overall")
    if newest_overall is None:
        baseline_note = " after baseline" if scanned.get("baseline") else " at all"
        return "SILENT", f"no files written{baseline_note} - has this run started?"
    quiet = (datetime.now() - newest_overall).total_seconds()
    return (
        "SILENT",
        (
            f"nothing written for {quiet / 60:.0f}m. Finished, blocked on a human, or dead - "
            "check whether it is waiting on a credential before assuming it is stuck."
        ),
    )


def verdict(scanned: dict[str, Any], window_minutes: int, liveness: str = LIVENESS_UNKNOWN) -> tuple[str, str]:
    """Turn the counts into a verdict and the sentence an orchestrator should act on.

    Four outcomes, and the two middle ones are the point:

    * PROGRESSING  - deliverables are being written; leave it alone.
    * THINKING     - only scratch activity, but the window is too SHORT to judge on yet.
    * STALLED      - scratch activity across a full window and NO deliverable. This is the shape that
                     burned 105 minutes: busy, and producing nothing the user asked for.
    * SILENT       - nothing at all. Either finished, blocked on a human, or dead.

    ⚠️ **Recency must NOT rescue a window with zero deliverables.** The first version tested "last
    write < 180s ago -> THINKING", which meant an agent touching a scratch file every 30 seconds for
    105 minutes read THINKING forever - the exact run this tool was written for. Recency only
    separates STALLED from SILENT; the WINDOW decides whether the absence of output is meaningful.
    """
    deliverable = scanned["buckets"]["deliverable"]
    scratch = scanned["buckets"]["scratch"]
    other = scanned["buckets"]["other"]
    observed_minutes = scanned.get("observed_minutes", window_minutes)
    window = _window_text(scanned, window_minutes)

    if deliverable["count"]:
        state, detail = "PROGRESSING", f"{deliverable['count']} deliverable file(s) in the last {window}."
    elif not scratch["count"] and not other["count"]:
        state, detail = _quiet_verdict(scanned, window_minutes, liveness, window)
    elif observed_minutes < SHORT_WINDOW_MINUTES:
        state, detail = (
            "THINKING",
            (
                f"{scratch['count']} scratch file(s), nothing deliverable yet, but a {window} "
                f"window is too short to judge (a Desktop load is ~90s and a refresh ~93s). Re-check "
                f"over >= {SHORT_WINDOW_MINUTES}m."
            ),
        )
    else:
        state, detail = (
            "STALLED",
            (
                f"{scratch['count']} scratch file(s) but ZERO deliverables in {window}. "
                "Busy and producing nothing the user asked for - ASK IT WHAT IT IS BLOCKED ON."
            ),
        )
    return state, detail


def render(bundle: Path, scanned: dict[str, Any], state: str, detail: str, window: int) -> str:
    """The human-readable check-in."""
    lines = [f"PROGRESS [{state}] {bundle.name} - {detail}", ""]
    window_label = _window_text(scanned, window)
    for name in ("deliverable", "scratch", "other"):
        bucket = scanned["buckets"][name]
        when = bucket["newest"].strftime("%H:%M:%S") if bucket["newest"] else "-"
        lines.append(
            f"  {name:12} {bucket['count']:>4} file(s) in last {window_label}   newest {when}  "
            f"{bucket['example'] or ''}"
        )
    if state == "STALLED":
        lines += [
            "",
            "  The file signal says this run is busy but not producing deliverables.",
            "  Do NOT kill it - make it report what it is blocked on,",
            "  which is the thing that does not happen on its own.",
        ]
    return "\n".join(lines)


def newest_write(root: Path, suffixes: tuple[str, ...]) -> tuple[datetime | None, Path | None]:
    """The most recent write among files with these suffixes, and which file it was."""
    newest, which = None, None
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            try:
                written = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if newest is None or written > newest:
                newest, which = written, path
    return newest, which


def handoff_ready(bundle: Path) -> tuple[str, list[str]]:
    """Is every semantic model in this bundle SAFE to hand to the report builder?

    Two failures, both measured on a real parallel run, and both invisible to the report builder:

    1. **No cache at all.** Desktop opens an empty model, and the agent's natural next move is to
       trigger its own refresh - minutes of cost, and on a live source a modal credential prompt that
       stops the run outright.
    2. **A cache OLDER than the TMDL.** The model was edited after it was persisted, so the warm data
       no longer matches the schema on disk. This is the more dangerous one: something loads, so
       nothing looks wrong.

    There is a third, measured 2026-08-07 at 22:19 against a cache written at 22:22: the report side
    opened Desktop **two minutes before the cache existed**. The cache was correct and the handoff
    gate had run; the ORDER was wrong. That is precisely what this check exists to make impossible -
    the orchestrator runs it before assigning the report phase, not after.
    """
    problems: list[str] = []
    models = sorted(p for p in bundle.rglob("*.SemanticModel") if p.is_dir())
    if not models:
        return "NO_MODEL", ["no *.SemanticModel directory in this bundle - nothing to hand over"]

    for model in models:
        rel = model.relative_to(bundle)
        cache = model / ".pbi" / "cache.abf"
        tmdl_at, tmdl_file = newest_write(model, (".tmdl",))
        if not cache.is_file():
            problems.append(
                f"{rel}: NO cache.abf - the report builder will open an EMPTY model and is likely "
                "to trigger its own refresh (minutes, and a credential prompt on a live source)"
            )
            continue
        cache_at = datetime.fromtimestamp(cache.stat().st_mtime)
        if tmdl_at and cache_at < tmdl_at:
            problems.append(
                f"{rel}: cache.abf is STALE - persisted {cache_at:%H:%M:%S} but "
                f"{tmdl_file.name if tmdl_file else 'TMDL'} was edited {tmdl_at:%H:%M:%S}. "
                "Something will load, so nothing will look wrong. Re-refresh and re-save."
            )

    if problems:
        return "NOT_READY", problems
    return "READY", [f"{len(models)} model(s) carry a cache that post-dates their TMDL"]


def _emit_notes(label: str, bundle: Path, state: str, notes: list[str], as_json: bool) -> None:
    """Render a note-list mode as JSON or log lines."""
    if as_json:
        sys.stdout.write(json.dumps({"bundle": str(bundle), "state": state, "notes": notes}, indent=2) + "\n")
    else:
        LOG.info("%s [%s] %s", label, state, bundle.name)
        for note in notes:
            LOG.info("  %s", note)


def run_handoff_mode(bundle: Path, as_json: bool) -> int:
    """Run the handoff gate and return its process exit code."""
    state, notes = handoff_ready(bundle)
    _emit_notes("HANDOFF", bundle, state, notes, as_json)
    return {"READY": 0, "NOT_READY": 1, "NO_MODEL": 2}[state]


def run_tamper_mode(bundle: Path, as_json: bool, package_roots: list[Path] | None = None) -> int:
    """Run the generated-artifact drift gate and return its process exit code.

    ``UNREADABLE_DECLARATIONS`` -> 4 is a NEW code, and it can only replace a situation that
    previously died with an uncaught traceback (also exiting 1, indistinguishably from a real
    ``DRIFT``). No bundle that ever produced one of the five existing verdicts produces a different
    one now.

    ``PACKAGE_DRIFT`` -> 1 joins ``DRIFT``: a generated artifact changed and nothing accounts for it,
    which is the same finding wherever the artifact lives. ``PACKAGE_UNASSESSABLE`` -> 5 is its own
    code because "this package carries no digest, so whether it was edited cannot be established" is
    not the same answer as "it was edited" - the distinction this whole review round is about. Both
    are reachable only for a bundle that has phase-2 packages beside it, which no bundle had when
    the five existing verdicts were specified.
    """
    state, notes = tamper_check(bundle, package_roots)
    _emit_notes("TAMPER", bundle, state, notes, as_json)
    return {
        "CLEAN": 0,
        "DECLARED_DRIFT": 0,
        "DRIFT": 1,
        "PACKAGE_DRIFT": 1,
        "NO_BASELINE": 2,
        "NO_BASELINE_BY_DESIGN": 3,
        "UNREADABLE_DECLARATIONS": 4,
        "PACKAGE_UNASSESSABLE": 5,
    }[state]


def main(argv: list[str] | None = None) -> int:
    """Exit 0 PROGRESSING/THINKING/READY, 1 STALLED/NOT_READY, 2 SILENT/NO_MODEL.

    ``--tamper`` has its own exit map: 0 CLEAN/DECLARED_DRIFT, 1 DRIFT and PACKAGE_DRIFT,
    2 NO_BASELINE (a baseline that should exist is missing, corrupt, or invalid),
    3 NO_BASELINE_BY_DESIGN (this bundle shape never records one - expected absence, not tampering;
    see ``_no_baseline_verdict``), 4 UNREADABLE_DECLARATIONS (artifacts drifted and the ledger that
    might exonerate them cannot be read - deliberately NOT 1, which means "drifted and undeclared"),
    5 PACKAGE_UNASSESSABLE (a phase-2 package carries no digest, so whether it was edited cannot be
    established - deliberately NOT 1 for the same reason).

    Both modes look at the phase-2 packages as well as the bundle, because since #460 was settled
    the canonical place an agent edits is ``<package>/fabric/`` and that lives beside the bundle.
    ``--packages`` overrides the search for a non-canonical layout.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="the migration's output directory")
    parser.add_argument("--since-minutes", type=int, default=20, help="observation window (default 20)")
    parser.add_argument(
        "--baseline",
        type=parse_baseline,
        help="delegation timestamp (ISO-8601); ignore files older than this so setup artifacts are not agent progress",
    )
    parser.add_argument(
        "--liveness",
        choices=(LIVENESS_UNKNOWN, LIVENESS_ACTIVE, "idle"),
        default=LIVENESS_UNKNOWN,
        help="external runtime signal; use active when tool-call count rose since the last poll",
    )
    parser.add_argument(
        "--packages",
        type=Path,
        help="phase-2 package root; defaults to the bundle's sibling packages/ (the canonical layout)",
    )
    parser.add_argument(
        "--handoff",
        action="store_true",
        help="instead of progress: is this bundle safe to hand to the report builder?",
    )
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="instead of progress: did generated artifacts drift without matching hash declarations?",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.bundle.is_dir():
        LOG.error("PROGRESS: ERROR no such bundle: %s", args.bundle)
        return 2
    if sum(bool(mode) for mode in (args.handoff, args.tamper)) > 1:
        LOG.error("PROGRESS: ERROR choose only one mode: progress, --handoff, or --tamper")
        return 2

    if args.handoff:
        return run_handoff_mode(args.bundle, args.json)

    packages = discover_package_roots(args.bundle, args.packages)
    if args.tamper:
        return run_tamper_mode(args.bundle, args.json, packages)

    if args.baseline is None:
        LOG.error(
            "PROGRESS: ERROR --baseline <iso8601> is required in progress mode so dispatcher setup files "
            "cannot be credited as agent progress."
        )
        return 2

    since = datetime.now() - timedelta(minutes=args.since_minutes)
    scanned = scan(args.bundle, since, args.baseline, packages)
    state, detail = verdict(scanned, args.since_minutes, args.liveness)

    if args.json:
        payload = {
            "bundle": str(args.bundle),
            "state": state,
            "detail": detail,
            "window_minutes": args.since_minutes,
            "observed_minutes": round(scanned["observed_minutes"], 2),
            "baseline": args.baseline.isoformat(timespec="seconds") if args.baseline else None,
            "liveness": args.liveness,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "packages_scanned": scanned.get("packages_scanned", []),
            "buckets": {
                name: {
                    "count": b["count"],
                    "newest": b["newest"].isoformat(timespec="seconds") if b["newest"] else None,
                    "example": b["example"],
                }
                for name, b in scanned["buckets"].items()
            },
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        LOG.info("%s", render(args.bundle, scanned, state, detail, args.since_minutes))

    return {"PROGRESSING": 0, "THINKING": 0, "STALLED": 1, "SILENT": 2}[state]


if __name__ == "__main__":
    sys.exit(main())
