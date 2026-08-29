"""
purpose: run the deterministic tier over an ESTATE and turn its output into something a downstream
         agent tier can consume safely - a real exit code, collision-checked approvals, per-workbook
         handover slices, and a phase-timing record.
usage:   python scripts/run_estate.py --input <folder-of-.twb/.twbx> --output <bundle-dir>
                                      [--approved-dax <file.json>] [--dry-run]
                                      [--accept-bundle-rewrite] [--accept-engine-version-change]
         python scripts/run_estate.py --slice-only --output <existing-bundle-dir>

The engine is NOT a parameter you normally pass. It resolves to the installed
`tableau-fabric-skills` plugin - the single canonical source (issue #107) - and the resolved path
and VERSION land in `engine-output-receipt.json` so the bundle can answer "what built me?" on its
own. `--engine` still exists for a deliberate override and requires `--allow-noncanonical-engine`.

Why this exists, and why it is a SCRIPT rather than an agent step
----------------------------------------------------------------
The deterministic tier already migrates a whole folder in one run, and it is already failure-isolated
(a malformed asset lands as an `error` rather than aborting the bundle). Rebuilding that would be
pure duplication. What it does NOT do is make its result safe to consume, and three of those gaps are
things a conversation cannot be trusted to remember every time:

1. **`definition_of_done: "failed"` still exits 0.** `migrate_estate.py` ends with
   `# ASCII markers only ... Soft-but-loud: exit stays 0` and an unconditional `return 0`. That is a
   defensible choice for a batch tool - one bad workbook should not fail the estate - but a consumer
   that gates on the exit code silently accepts a failed migration. An agent *may* read the JSON
   field; a script *must*. This is the single reason the coordinator is code.

2. **`--approved-dax` is an estate-GLOBAL, name-keyed map.** `_load_approved_dax` returns a flat
   `{calc name: DAX}` dict with no model scoping, and the seam carries it into *every* model build.
   Two workbooks with a same-named calc and different formulas therefore collide - and the names
   really are generic: a real 6-workbook run produced `Calculation2` (Tableau's auto-generated
   default), `Rank`, `Size`, `Running Sum`. Measured 0 collisions in that sample, so this is a
   LATENT hazard, not an observed one - which is exactly when a cheap check is worth having.

3. **`report.json` is ~14 KB per workbook** (83.4 KB measured for 6). At estate scale that is
   hundreds of KB of mostly-irrelevant context if handed whole to a per-workbook agent.

4. **A model can pass every check above and still contain ZERO ROWS.** An Import partition over a
   flat file that was never landed opens, validates, binds its report and reports success; the
   engine notes it in a `pbip_warnings` string and moves on. Measured on a 38-workbook estate, one
   such workbook came back `definition_of_done: warn` - it would have passed this coordinator's own
   gate. `check_empty_model.py` is the offline artifact scan that catches it, wired in below as
   `EXIT_EMPTY_MODEL`.

5. **A report can pass every check above and be STRUCTURALLY INVALID.** When a Tableau calc falls
   back to a stub, the engine drops its projection instead of binding it; if that projection was the
   sole occupant of a REQUIRED visual role, `powerbi-report-author validate` rejects the report
   (`PBIR_ROLE_REQUIRED_MISSING`) while the engine grades the same bytes `definition_of_done: warn`,
   `0 error`, `Viz=built`. The engine's own always-on linter has no required-role rule and its real
   validate pre-gate is default-off *and* non-binding (filed upstream as #220 / #221), so nothing in
   the default conversion path can see it. `check_pbir_valid.py` delegates to the first-party
   validator and makes its verdict bind, wired in below as `EXIT_INVALID_PBIR`.

6. **A report can consume a BLANK() placeholder the engine safely emitted.** The handover names the
   calc and why translation failed, and the TMDL carries a BLANK()-only column or measure. If PBIR
   filters or visual field bindings reference it, the page can render empty while every structural
   gate passes. `check_blank_placeholders.py` correlates handover + TMDL + PBIR and blocks only the
   report-referenced cases, wired in below as `EXIT_BLANK_PLACEHOLDER`. It reads the handover half
   from `report.json`, NOT from `<bundle>/handover/`: those slices are written by `slice_handovers`
   in phase 3, one phase AFTER this gate runs, so globbing them made the check a no-op on every
   fresh run and, on a re-used `--output` folder, correlated the previous estate's entries.

7. **`--slice-only` skipped the generated-artifact baseline entirely (issue #230).** It never runs
   `record_engine_output`, so `input_manifest.json` never carried a `generated_artifacts` key for a
   bundle built this way - `check_migration_progress.py --tamper` returned `NO_BASELINE` (exit 2) for
   every such bundle's whole life, even though nothing was tampered with. A field-measured SES estate
   had to caveat its own first engine-gap distribution as "usable signal, not cryptographically
   attested signal" as a direct result. `backfill_slice_only_baseline`, wired in below, records a
   best-effort baseline scoped to whatever is on disk at that moment (never overwriting a real one a
   prior full run already wrote), and `check_migration_progress.py` now tells the two cases apart -
   see its `NO_BASELINE_BY_DESIGN` state.

Deliberately NOT here
---------------------
No migration logic. This never writes TMDL, never writes PBIR, never opens Power BI Desktop. It runs
his engine, reads his report, and writes derived artifacts alongside it. If this file ever starts
emitting model content, the split has been violated.

The barrier
-----------
A `--approved-dax` re-run is **delete-and-recreate**, not merge: `migrate_estate.py` `rmtree`s the
`.SemanticModel` folder (:879), the whole `.pbip` project dir (:3035) and `<name>.Report` (:3284)
before rewriting them. Nothing a downstream agent wrote into that bundle survives. Worse, the
stale-output guard (:5040) *exempts* the landing re-run, so the most destructive path is the one that
needs no `--force`. Hence: all DAX lands in ONE run, and per-workbook work starts only afterwards.

That sentence used to end "this script owns that ordering so no agent has to remember it", which was
false: it DOCUMENTED the ordering and checked nothing (issue #250). Every gate below runs in phase 2,
reading the report the engine has already written - including the collision check, the one gate that
is specifically about `--approved-dax`. A wrong landing was reported *after* it had landed, and a
landing into a bundle full of hand-authored fix work was not reported at all.

`assess_bundle_rewrite` now runs BEFORE the engine and makes it true, refusing with
`EXIT_BUNDLE_REWRITE` in two cases:

* **downstream work would be destroyed.** Re-hash the bundle against the two baselines the previous
  run already wrote - `engine-output-receipt.json` (written by every run, read back by nothing until
  now) and `input_manifest.json`'s `generated_artifacts` - and any file that no longer matches is
  somebody's work. `--accept-bundle-rewrite` proceeds and the acknowledgement is recorded in the
  bundle.
* **the bundle was built by a DIFFERENT engine version.** Two engine versions are not equivalent:
  2.113.0 emitted deprecated Bing `shapeMap` visuals and dropped a density-map worksheet entirely
  where 2.126.0 emitted `azureMap` with a heat layer, and nothing in the output said which ran
  (#107). Re-running into that bundle silently mixes both. `--accept-engine-version-change` proceeds.

The two acknowledgements are deliberately SEPARATE flags. One flag would mean accepting a known
engine bump also silently waives the destruction guard for work you did not know was there.

A bundle with neither baseline (pre-receipt, or third-party) warns and proceeds - a missing receipt
must not brick every older bundle - and `--slice-only` never runs the guard at all, because it never
runs the engine and therefore destroys nothing.

(Line numbers are against engine HEAD `81e6164`. They drift on every upstream release - the four
`rmtree`/guard sites moved ~30 lines between 2.72.0 and 2.78.0 with no behaviour change - so re-derive
them by symbol, not by number, if they do not match.)
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from check_empty_model import REPORT_NAME as EMPTY_MODEL_REPORT
from check_empty_model import STATUS_EMPTY_MODELS
from check_empty_model import render as render_empty_model
from check_empty_model import scan as scan_for_empty_models
from check_blank_placeholders import REPORT_NAME as BLANK_PLACEHOLDER_REPORT
from check_blank_placeholders import STATUS_REFERENCED as BLANK_PLACEHOLDER_REFERENCED
from check_blank_placeholders import render as render_blank_placeholders
from check_blank_placeholders import scan as scan_blank_placeholders
from check_pbir_valid import REPORT_NAME as PBIR_VALID_REPORT
from check_pbir_valid import render as render_pbir_valid
from check_pbir_valid import scan as scan_pbir_validity
from engine_source import EngineNotFoundError, NonCanonicalEngineError, engine_provenance, resolve_engine
from migration_bundle import ENGINE_RECEIPT, engine_artifact_records, sha256_file, write_engine_receipt

log = logging.getLogger("run_estate")

# `definition_of_done.status` values that mean the estate is NOT safe to hand downstream. "warn" is
# deliberately allowed through: it is the normal state of a real migration (deferred visuals, stubbed
# calcs) and blocking on it would make the coordinator useless on every workbook that has any gap.
DOD_BLOCKING = {"failed"}

EXIT_OK = 0
EXIT_ENGINE_FAILED = 1
EXIT_USAGE = 2
EXIT_DOD_FAILED = 3
EXIT_COLLISION = 4
EXIT_ENGINE_SOURCE = 5
EXIT_EMPTY_MODEL = 6
EXIT_INVALID_PBIR = 7
EXIT_BLANK_PLACEHOLDER = 8
EXIT_BUNDLE_REWRITE = 9
GENERATED_ARTIFACTS_KEY = "generated_artifacts"
VOLATILE_GENERATED_DIRS = {".pbi"}
SCRATCH_DIRS = frozenset({"scratch", "_work", "_build", "_probe", "tmp", "temp", "_shots"})
SCRATCH_INTENTS = frozenset(part.lstrip("._") for part in SCRATCH_DIRS)


def run_engine(engine: Path, src: Path, out: Path, approved_dax: Path | None) -> tuple[int, str]:
    """Invoke the deterministic tier. Returns (exit code, combined output).

    No timeout: an estate run is legitimately long and offline, and it needs no credentials, so a
    hang here is not the credential-modal shape that the live-source probe has to defend against.
    """
    script = engine / "skills" / "tableau-migration" / "scripts" / "migrate_estate.py"
    if not script.is_file():
        raise FileNotFoundError(f"engine not found: {script}")

    cmd = [sys.executable, str(script), "-i", str(src), "-o", str(out)]
    if approved_dax:
        cmd += ["--approved-dax", str(approved_dax)]
    log.info("ENGINE: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr)


def _is_scratch_path(relative: Path) -> bool:
    """Whether a relative path is migration scratch rather than a generated deliverable."""
    return any(part.lower().lstrip("._") in SCRATCH_INTENTS for part in relative.parts)


def _is_generated_artifact(path: Path, bundle: Path, earliest_mtime: float | None = None) -> bool:
    """Stable deterministic-tier output that should stay explainable after agent work.

    Power BI refreshes and Desktop autosaves write under ``.pbi``; those sidecars are deliberately
    outside the hash set so a normal refresh does not look like tampering.
    """
    if not path.is_file():
        return False
    if earliest_mtime is not None and path.stat().st_mtime < earliest_mtime:
        return False
    relative = path.relative_to(bundle)
    lower_parts = [part.lower() for part in relative.parts]
    if _is_scratch_path(relative) or any(part in VOLATILE_GENERATED_DIRS for part in lower_parts):
        return False
    if path.suffix.lower() == ".pbip":
        return True
    return any(part.endswith((".semanticmodel", ".report")) for part in lower_parts)


def generated_artifact_hashes(bundle: Path, earliest_mtime: float | None = None) -> dict[str, str]:
    """All stable generated artifacts in a bundle, keyed by POSIX relative path."""
    files = {}
    for path in sorted(bundle.rglob("*")):
        if _is_generated_artifact(path, bundle, earliest_mtime):
            files[path.relative_to(bundle).as_posix()] = sha256_file(path)
    return files


def write_generated_artifact_manifest(
    bundle: Path,
    report: dict | None = None,
    earliest_mtime: float | None = None,
    coverage: str | None = None,
) -> Path:
    """Upsert generated-file hashes into ``input_manifest.json`` after the engine run.

    The deterministic engine already owns this manifest for source inputs. Adding a separate key keeps
    that contract intact while giving downstream checks a baseline for generated TMDL/PBIR drift.

    ``coverage`` records how the baseline was captured when it is NOT a normal full-engine run - e.g.
    ``"slice_only_backfill"`` (issue #230) for a ``--slice-only`` invocation that has no engine-run
    boundary to hash from. ``check_migration_progress.py --tamper`` surfaces this so a bundle that
    passes still says its coverage is partial instead of silently claiming full attestation.
    """
    manifest_path = bundle / "input_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = {"engine_input_manifest": manifest}
    else:
        manifest = {}
    generated = {
        "version": 1,
        "run_id": uuid.uuid4().hex,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_generated_at": (report or {}).get("generated_at"),
        "report_sha256": sha256_file(bundle / "report.json") if (bundle / "report.json").is_file() else None,
        "files": generated_artifact_hashes(bundle, earliest_mtime),
    }
    if coverage:
        generated["coverage"] = coverage
    manifest[GENERATED_ARTIFACTS_KEY] = generated
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def backfill_slice_only_baseline(bundle: Path, report: dict | None, phases: list[dict]) -> Path | None:
    """Record a best-effort generated-artifact baseline for a ``--slice-only`` bundle (issue #230).

    ``--slice-only`` skips the engine phase entirely (see ``resolve_run_engine``), so there is no run
    boundary to hash FROM - the bundle's TMDL/PBIR may have been produced by ``migrate_estate.py`` run
    directly, or by an earlier ``run_estate.py`` invocation this process never saw. Before this fix,
    that meant ``input_manifest.json`` never carried a ``generated_artifacts`` key at all, and every
    declaration path built on ``check_migration_progress.py``'s baseline became unusable for the
    bundle's whole life - a field-measured SES estate had to caveat its own output as "usable signal,
    not cryptographically attested signal" as a result.

    Rather than leave that permanent, record a baseline now, scoped to whatever generated artifacts
    already exist on disk at THIS moment. It cannot attest to the bundle's state before this moment -
    that coverage gap is real, so it is recorded in the baseline itself (``coverage:
    "slice_only_backfill"``) rather than silently claimed away.

    Never overwrites an EXISTING ``generated_artifacts`` key, valid or not: a prior full engine run
    through ``run_estate.py`` already recorded a real baseline, and a manifest whose key fails
    validation is potential tamper evidence in its own right - either way, clobbering it here would
    destroy exactly the evidence a tamper check depends on.
    """
    manifest_path = bundle / "input_manifest.json"
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if isinstance(existing, dict) and GENERATED_ARTIFACTS_KEY in existing:
            return None
    started = time.monotonic()
    written = write_generated_artifact_manifest(bundle, report, earliest_mtime=None, coverage="slice_only_backfill")
    phases.append({"phase": "slice_only_baseline_backfill", "elapsed_sec": round(time.monotonic() - started, 1)})
    log.info("GENERATED_ARTIFACTS: backfilled slice-only baseline -> %s", written)
    return written


def check_definition_of_done(report: dict) -> tuple[bool, str]:
    """Turn `definition_of_done` into a pass/fail the caller can act on.

    THE reason this script exists. The engine prints `[FAIL] Definition of done: failed` and then
    returns 0 anyway, so a consumer gating on the exit code accepts a failed migration silently.
    """
    dod = report.get("definition_of_done") or {}
    if not dod.get("applicable"):
        return True, "definition_of_done not applicable to this run"
    status = dod.get("status")
    detail = (
        f"status={status} "
        f"bound={dod.get('reports_bound', 0)}/{dod.get('workbooks_total', 0)} "
        f"failed={dod.get('reports_failed', 0)} warned={dod.get('reports_warned', 0)}"
    )
    return status not in DOD_BLOCKING, detail


def find_approval_collisions(report: dict) -> dict[str, list[dict]]:
    """Group stubbed-calc requests by name, keeping only names claimed by more than one model.

    A collision is (same name, different owning model). The formula is carried so a caller can see
    whether the two are actually the same calc - identical formulas under one name are harmless,
    differing formulas mean one approval would land the WRONG DAX in the other model.
    """
    by_name: dict[str, list[dict]] = defaultdict(list)
    for wb in report.get("workbooks") or []:
        handoff = wb.get("model_translation_handoff") or {}
        for req in handoff.get("requests") or []:
            name = (req.get("name") or "").strip().lower()
            if not name:
                continue
            by_name[name].append(
                {
                    "workbook": wb.get("name"),
                    "model": wb.get("bound_model"),
                    "formula": (req.get("formula") or "").strip(),
                    "target_table": req.get("target_table"),
                }
            )

    collisions = {}
    for name, claims in by_name.items():
        if len({c["model"] for c in claims}) > 1:
            collisions[name] = claims
    return collisions


def slice_handovers(report: dict, out_dir: Path) -> list[Path]:
    """Write one handover file per workbook, so the whole estate report never enters an agent context.

    Each slice carries that workbook's own entry plus the estate-level facts it genuinely needs
    (which gates were offered, what produced it). Everything else is dropped on purpose.
    """
    handover_dir = out_dir / "handover"
    handover_dir.mkdir(parents=True, exist_ok=True)

    estate_context = {
        "tool": report.get("tool"),
        "generated_at": report.get("generated_at"),
        "source": report.get("source"),
        "pending_gates": report.get("pending_gates") or [],
        "definition_of_done_status": (report.get("definition_of_done") or {}).get("status"),
    }

    written = []
    for wb in report.get("workbooks") or []:
        name = wb.get("name") or "unnamed"
        safe = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip() or "unnamed"
        path = handover_dir / f"{safe}.json"
        path.write_text(
            json.dumps({"estate": estate_context, "workbook": wb}, indent=2),
            encoding="utf-8",
        )
        written.append(path)
    return written


def stamp_inputs(input_dir: Path, out_dir: Path) -> str | None:
    """Record where each input workbook came from, into ``<out>/source-provenance.json``.

    Best-effort by design: a migration must never fail because a Tableau site was unreachable, so a
    lookup failure degrades to fingerprints and anything unexpected degrades to no file at all.
    Imported lazily so `run_estate` still works in an environment where the stamper is absent.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import stamp_tableau_provenance as prov  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        result = prov.build(input_dir, prov.resolve_env(Path(".env")))
        if not result["input_count"]:
            return None
        path = out_dir / "source-provenance.json"
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        matched = sum(1 for r in result["inputs"] if (r.get("origin") or {}).get("match") == "sha256")
        return f"{result['input_count']} input(s) stamped, {matched} confirmed against the site -> {path}"
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        log.warning("provenance stamp skipped (%s: %s)", type(exc).__name__, str(exc)[:120])
        return None


def write_phase_record(out_dir: Path, phases: list[dict]) -> Path:
    """Persist the phase timings.

    Not a telemetry system - the session store already records model, tokens and duration per turn.
    What it cannot know is WHICH MIGRATION PHASE a turn belonged to, so that is all this supplies.
    It exists so the retrospective can say "where did the time actually go" instead of "what did we
    learn", which is prose this repo has repeatedly had to retract.
    """
    path = out_dir / "phase-timings.json"
    total = sum(p["elapsed_sec"] for p in phases)
    path.write_text(
        json.dumps({"phases": phases, "total_elapsed_sec": round(total, 1)}, indent=2),
        encoding="utf-8",
    )
    return path


def read_report(out: Path) -> dict:
    """Read the engine's report.json, failing loudly if it is absent or malformed."""
    path = out / "report.json"
    if not path.is_file():
        raise FileNotFoundError(f"no report.json at {path} - did the engine run?")
    return json.loads(path.read_text(encoding="utf-8"))


def print_summary(report: dict, out_dir: Path, slices: list[Path], timings: Path, dod_detail: str) -> None:
    """Print what a caller needs to decide the next step, and nothing more."""
    summary = report.get("summary") or {}
    workbooks = report.get("workbooks") or []
    print(f"ESTATE: {len(workbooks)} workbook(s) | {out_dir}")
    print(f"  definition_of_done : {dod_detail}")
    print(f"  handover slices    : {len(slices)} -> {out_dir / 'handover'}")
    print(f"  phase timings      : {timings}")
    print(
        f"  gates pending      : "
        f"{', '.join(g.get('gate', '?') for g in (report.get('pending_gates') or [])) or '(none)'}"
    )
    print(
        f"  stubbed calcs      : {summary.get('workbook_calcs_stubbed', 0)}"
        f" | visuals warned: {summary.get('visuals_warned', 0)}"
    )


def print_collisions(collisions: dict[str, list[dict]]) -> None:
    """Explain a collision in terms of what it will DO, not what it is.

    The failure mode is the reason this is loud: a colliding approval does not error and does not
    conflict - it lands the wrong DAX in a model that happened to reuse a calc name, and every
    downstream signal then says the migration succeeded.
    """
    print(f"\nAPPROVED_DAX_COLLISION: {len(collisions)} calc name(s) claimed by >1 model")
    for name, claims in sorted(collisions.items()):
        same = len({c["formula"] for c in claims}) == 1
        print(f"  '{name}' - {len(claims)} models, formulas {'IDENTICAL' if same else 'DIFFER'}")
        for claim in claims:
            print(f"      {claim['model']}  ({claim['workbook']})")
    print(
        "  --approved-dax is an estate-GLOBAL, name-keyed map, so ONE approval for this name\n"
        "  lands in EVERY model that has a calc called it. Where the formulas DIFFER that is a\n"
        "  wrong-DAX landing, not a merge conflict - it will not error, it will just be wrong.\n"
        "  Land these per-workbook instead, or rename before approving."
    )


def write_receipt_phase(out_dir: Path, phases: list[dict], engine: Path | None = None) -> None:
    """Persist the engine-output receipt and record the phase."""
    started = time.monotonic()
    receipt = write_engine_receipt(out_dir, engine)
    phases.append({"phase": "engine_receipt", "elapsed_sec": round(time.monotonic() - started, 1)})
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from credential_gate import _audit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    _audit(out_dir, "engine-receipt", f"sha256={sha256_file(receipt)}")
    log.info("ENGINE RECEIPT: %s", receipt)


def record_engine_output(out_dir: Path, report: dict | None, phases: list[dict], engine: Path | None = None) -> None:
    """Baseline the engine's output: generated-artifact hashes, then the receipt over them.

    The order is load-bearing and lives HERE rather than in ``main`` so it cannot be separated by an
    unrelated edit: the manifest UPSERTS into ``input_manifest.json`` and the receipt HASHES that
    same file, so receipt-first leaves ``input_manifest_sha256`` stale and the credential gate then
    rejects the bundle the engine just produced - while the run still reports success.
    """
    log.info(
        "GENERATED_ARTIFACTS: hashes -> %s",
        write_generated_artifact_manifest(out_dir, report, phases[0]["started_wall"] - 1),
    )
    write_receipt_phase(out_dir, phases, engine)


# ---------------------------------------------------------------------------
# The destructive-re-run barrier (issue #250) - the ONLY pre-engine gate
# ---------------------------------------------------------------------------

BUNDLE_REWRITE_RECORD = "bundle-rewrite-acknowledgement.json"
REWRITE_LIST_LIMIT = 12


class BundleDrift(NamedTuple):
    """Files in a bundle that no longer match the baselines its previous run wrote."""

    modified: list[str]
    added: list[str]
    missing: list[str]

    @property
    def total(self) -> int:
        """How many files are no longer what the engine produced."""
        return len(self.modified) + len(self.added) + len(self.missing)


class BundleRewriteFindings(NamedTuple):
    """Everything the pre-engine barrier knows about the `--output` folder it is about to rewrite.

    ``applicable`` is False when the question does not arise at all - ``--slice-only`` (no engine, so
    nothing is destroyed), a `--output` that does not exist yet, or a bundle carrying neither
    baseline. It is deliberately distinct from "checked and found nothing".
    """

    bundle: Path
    applicable: bool
    drift: BundleDrift
    recorded_version: str | None
    running_version: str | None
    warnings: list[str]
    accepted_rewrite: bool
    accepted_version: bool

    @property
    def version_changed(self) -> bool:
        """Whether this run's engine differs from the one that built the bundle.

        Both versions have to be known: an unversioned engine tree or a pre-``engine`` receipt is an
        absence of evidence, and blocking on it would fail every older bundle for no finding.
        """
        return bool(
            self.applicable
            and self.recorded_version
            and self.running_version
            and self.recorded_version != self.running_version
        )

    @property
    def blocks_on_downstream(self) -> bool:
        """Downstream work exists in the bundle and the caller has not accepted losing it."""
        return bool(self.applicable and self.drift.total and not self.accepted_rewrite)

    @property
    def blocks_on_engine_version(self) -> bool:
        """The bundle would be rewritten by a different engine than the one that built it."""
        return bool(self.version_changed and not self.accepted_version)

    @property
    def blocking(self) -> bool:
        """Whether this run must be refused."""
        return self.blocks_on_downstream or self.blocks_on_engine_version

    @property
    def acknowledged(self) -> bool:
        """Whether a real finding was waived by an explicit flag, and so must be recorded."""
        return bool(
            (self.applicable and self.drift.total and self.accepted_rewrite)
            or (self.version_changed and self.accepted_version)
        )


def _looks_like_bundle(bundle: Path) -> bool:
    """Whether `--output` already holds engine output, rather than being a fresh folder."""
    return (bundle / "report.json").is_file() or (bundle / "pbip").is_dir()


def read_engine_receipt(bundle: Path) -> tuple[dict | None, str | None]:
    """Read the receipt the bundle's last run wrote. Returns ``(receipt, warning)``.

    ``write_receipt_phase`` writes this on EVERY run and, until this guard existed, nothing ever read
    it back. It is the only record of which files the engine itself produced, which is what makes
    "everything else in here is somebody's downstream work" a decidable question rather than a guess.

    A malformed receipt degrades to ``(None, warning)`` instead of raising: the caller's job is to
    protect a bundle, and aborting the run because the protection metadata is corrupt would turn a
    safety feature into an outage.
    """
    path = bundle / ENGINE_RECEIPT
    if not path.is_file():
        return None, None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{ENGINE_RECEIPT} is unreadable ({type(exc).__name__})"
    if not isinstance(receipt, dict):
        return None, f"{ENGINE_RECEIPT} is not a JSON object"
    return receipt, None


def _is_guarded_path(relative: Path) -> bool:
    """Whether a bundle-relative path is accounted for by the barrier at all.

    ``.pbi`` sidecars and scratch folders are excluded on BOTH sides of every comparison - current
    disk state and recorded baseline alike - so a Power BI Desktop refresh can never read as
    downstream work, and a pristine bundle still reconciles exactly.
    """
    lower = [part.lower() for part in relative.parts]
    return not _is_scratch_path(relative) and not any(part in VOLATILE_GENERATED_DIRS for part in lower)


def _receipt_artifact_hashes(receipt: dict | None) -> dict[str, str]:
    """The receipt's ``artifacts`` list as a path -> sha256 map, ignoring malformed entries."""
    hashes: dict[str, str] = {}
    for record in (receipt or {}).get("artifacts") or []:
        if not isinstance(record, dict):
            continue
        path, digest = record.get("path"), record.get("sha256")
        if isinstance(path, str) and isinstance(digest, str) and _is_guarded_path(Path(path)):
            hashes[path] = digest
    return hashes


def _generated_baseline_hashes(bundle: Path) -> dict[str, str]:
    """The WIDER baseline: every file under a `*.SemanticModel`/`*.Report` folder, PBIR JSON included.

    The receipt only records `ARTIFACT_SUFFIXES`, so a hand-edited PBIR visual definition - the
    report builder's entire output - is invisible to it. `write_generated_artifact_manifest` hashes
    the whole generated tree, so the two baselines together cover both halves of what an agent
    actually edits: model measures and report visuals.
    """
    manifest_path = bundle / "input_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    generated = manifest.get(GENERATED_ARTIFACTS_KEY) if isinstance(manifest, dict) else None
    files = generated.get("files") if isinstance(generated, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        path: digest
        for path, digest in files.items()
        if isinstance(path, str) and isinstance(digest, str) and _is_guarded_path(Path(path))
    }


def detect_downstream_work(bundle: Path, receipt_hashes: dict[str, str], baseline: dict[str, str]) -> BundleDrift:
    """Re-hash the bundle and name every file that is no longer what the engine produced.

    ``added`` is decided from the RECEIPT alone, via ``engine_artifact_records`` - the very function
    that wrote it - so a pristine bundle reconciles exactly and cannot raise a false alarm. The
    generated-artifact baseline is mtime-filtered when written, so it is used only for files it
    already knows about (modified/missing) and never to decide that a file is new.
    """
    modified: list[str] = []
    added: list[str] = []
    missing: list[str] = []

    if receipt_hashes:
        current = {
            record["path"]: record["sha256"]
            for record in engine_artifact_records(bundle)
            if _is_guarded_path(Path(record["path"]))
        }
        for path, digest in receipt_hashes.items():
            if path not in current:
                missing.append(path)
            elif current[path] != digest:
                modified.append(path)
        added = [path for path in current if path not in receipt_hashes]

    for path, digest in baseline.items():
        if path in receipt_hashes:
            continue
        candidate = bundle / path
        if not candidate.is_file():
            missing.append(path)
        elif sha256_file(candidate) != digest:
            modified.append(path)

    return BundleDrift(sorted(set(modified)), sorted(set(added)), sorted(set(missing)))


def _engine_versions(receipt: dict | None, engine: Path | None) -> tuple[str | None, str | None]:
    """(version that built the bundle, version about to rewrite it) - either may be unknown."""
    block = (receipt or {}).get("engine")
    recorded = block.get("version") if isinstance(block, dict) else None
    running = engine_provenance(engine)["version"] if engine is not None else None
    return (recorded if isinstance(recorded, str) else None, running if isinstance(running, str) else None)


def assess_bundle_rewrite(args: argparse.Namespace, engine: Path | None) -> BundleRewriteFindings:
    """Decide, BEFORE the engine runs, whether this run may rewrite `--output`.

    ``--slice-only`` is exempt by construction, not by exception: it never invokes the engine (see
    ``resolve_run_engine``), so there is no delete-and-recreate to guard against, and it legitimately
    points at an existing bundle every single time.
    """
    bundle = Path(args.output)
    nothing = BundleDrift([], [], [])
    accepted_rewrite = bool(args.accept_bundle_rewrite)
    accepted_version = bool(args.accept_engine_version_change)
    if args.slice_only or not bundle.is_dir():
        return BundleRewriteFindings(bundle, False, nothing, None, None, [], accepted_rewrite, accepted_version)

    receipt, warning = read_engine_receipt(bundle)
    warnings = [warning] if warning else []
    receipt_hashes = _receipt_artifact_hashes(receipt)
    if receipt is not None and not receipt_hashes:
        warnings.append(f"{ENGINE_RECEIPT} lists no artifacts - it cannot attest to what the engine produced")
    baseline = _generated_baseline_hashes(bundle)
    recorded_version, running_version = _engine_versions(receipt, engine)
    if not receipt_hashes and not baseline:
        if _looks_like_bundle(bundle):
            warnings.append(
                f"no usable {ENGINE_RECEIPT} and no generated-artifact baseline in {bundle} - this run "
                "cannot tell engine output from downstream work, so it is not trying to; anything an "
                "agent wrote here will be destroyed without being named"
            )
        if not recorded_version:
            return BundleRewriteFindings(
                bundle, False, nothing, None, None, warnings, accepted_rewrite, accepted_version
            )

    return BundleRewriteFindings(
        bundle,
        True,
        detect_downstream_work(bundle, receipt_hashes, baseline),
        recorded_version,
        running_version,
        warnings,
        accepted_rewrite,
        accepted_version,
    )


def _sample(paths: list[str]) -> str:
    """The first few paths, plus an honest count of what was elided."""
    shown = paths[:REWRITE_LIST_LIMIT]
    lines = [f"      {path}" for path in shown]
    if len(paths) > len(shown):
        lines.append(f"      ... and {len(paths) - len(shown)} more")
    return "\n".join(lines)


def print_bundle_rewrite(findings: BundleRewriteFindings) -> None:
    """Name what a re-run into this `--output` would destroy, and how to proceed deliberately."""
    for warning in findings.warnings:
        print(f"BUNDLE_REWRITE: WARN - {warning}")
    if findings.drift.total:
        print(
            f"\nESTATE: BUNDLE_REWRITE {'ACCEPTED' if findings.accepted_rewrite else 'REFUSED'} - "
            f"{findings.drift.total} file(s) in {findings.bundle} no longer match what the engine produced"
        )
        for label, paths in (
            ("modified", findings.drift.modified),
            ("added", findings.drift.added),
            ("missing", findings.drift.missing),
        ):
            if paths:
                print(f"  {label} ({len(paths)}):")
                print(_sample(paths))
        print(
            "  An engine re-run is DELETE-AND-RECREATE, not merge: it rmtree()s the .SemanticModel\n"
            "  folder, the .pbip project dir and <name>.Report before rewriting them, and the\n"
            "  engine's own stale-output guard EXEMPTS the --approved-dax landing path. Every file\n"
            "  listed above would be gone, and no other gate here runs until after that has happened."
        )
        if not findings.accepted_rewrite:
            print("  -> land into a FRESH --output, or pass --accept-bundle-rewrite to proceed knowingly.")
    if findings.version_changed:
        print(
            f"\nESTATE: BUNDLE_REWRITE {'ACCEPTED' if findings.accepted_version else 'REFUSED'} - this "
            f"bundle was built by engine {findings.recorded_version}, this run would rewrite it with "
            f"{findings.running_version}"
        )
        print(
            "  Two engine versions are not equivalent: 2.113.0 emitted deprecated Bing shapeMap\n"
            "  visuals and dropped a density-map worksheet entirely where 2.126.0 emitted azureMap\n"
            "  with a heat layer, and nothing in the output said which one ran (#107). Rewriting in\n"
            "  place mixes both into one bundle."
        )
        if not findings.accepted_version:
            print("  -> use a FRESH --output, or pass --accept-engine-version-change to proceed knowingly.")


def record_bundle_rewrite_acknowledgement(findings: BundleRewriteFindings) -> Path | None:
    """Append the acknowledgement to the bundle, so the ARTIFACT says the loss was deliberate.

    Written before the engine runs and at the bundle root, which the engine's `rmtree` sites do not
    touch, so it survives the rewrite it is describing. Append rather than overwrite: a bundle that
    has been knowingly rewritten twice should say so twice.
    """
    if not findings.acknowledged:
        return None
    path = findings.bundle / BUNDLE_REWRITE_RECORD
    records = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and isinstance(existing.get("records"), list):
            records = existing["records"]
    records.append(
        {
            "acknowledged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "accepted_bundle_rewrite": findings.accepted_rewrite,
            "accepted_engine_version_change": findings.accepted_version,
            "engine_version_recorded": findings.recorded_version,
            "engine_version_running": findings.running_version,
            "destroyed": {
                "modified": findings.drift.modified,
                "added": findings.drift.added,
                "missing": findings.drift.missing,
            },
        }
    )
    path.write_text(json.dumps({"version": 1, "records": records}, indent=2) + "\n", encoding="utf-8")
    log.info("BUNDLE_REWRITE: acknowledgement recorded -> %s", path)
    return path


def check_empty_models(out_dir: Path) -> dict:
    """Scan the emitted models for the one failure the engine reports but nothing gates on.

    THE SECOND reason this script exists, and the quieter one. `check_definition_of_done` catches a
    migration that failed *visibly*. This catches one that succeeded visibly and produced nothing:
    an Import partition over a flat file that was never landed opens fine, validates fine, deploys
    fine, and shows a customer an empty report. Measured on a 38-workbook estate: one such model was
    `definition_of_done: warn`, i.e. it would have passed every gate this coordinator had.

    Offline by construction - no Fabric, no Desktop, no credential - so it runs on every estate, not
    only the ones where a tenant happens to be reachable. The verdict is also written to
    ``<bundle>/empty-model-check.json`` so a later deploy step can re-read it without re-deriving it.
    """
    report = scan_for_empty_models(out_dir)
    (out_dir / EMPTY_MODEL_REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def check_pbir_validity(out_dir: Path) -> dict:
    """Run the FIRST-PARTY PBIR validator over the reports that ship, and let its verdict bind.

    THE THIRD reason this script exists. `check_empty_models` catches a model that opens with no
    rows; this catches a report that does not validate at all - the engine emits it and grades it a
    pass. Measured 2026-08-18 (engine 2.151.0): a stubbed Tableau calc had its projection dropped
    rather than bound, leaving a `clusteredColumnChart` with no `Y` role;
    `powerbi-report-author validate` returned `PBIR_ROLE_REQUIRED_MISSING` and exit 1 while the
    engine reported `definition_of_done: warn`, `0 error`, `Viz=built` on the same bytes.

    The engine is not missing the tool - it has a `--validate` pre-gate - it is missing the DEFAULT:
    that gate is opt-in and explicitly "never changes the structural aggregate". Its always-on
    linter (`pbir_lint.py`) is hand-rolled and has no required-role rule. Filed as #220 / #221.

    Delegated, not reimplemented: the role-requirement catalog belongs to Microsoft's CLI and is
    versioned with it. Degrades to SKIPPED when that CLI is absent, so a machine without Node still
    completes a run. The verdict is written to ``<bundle>/pbir-validity-check.json``.
    """
    report = scan_pbir_validity(out_dir)
    (out_dir / PBIR_VALID_REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def check_blank_placeholders(out_dir: Path) -> dict:
    """Correlate engine fallback handover entries with BLANK()-only TMDL objects.

    THE FOURTH reason this script exists. The deterministic tier can safely refuse a Tableau calc by
    preserving its formula in handover and emitting a BLANK() placeholder in TMDL. That is a good
    engine fallback, but if the PBIR report consumes the placeholder in a filter or visual field
    binding, the report can render empty while TMDL deserialization, PBIR validation and model
    refresh all pass. The verdict is written to ``<bundle>/blank-placeholder-check.json``.

    Severity is intentionally split: unreferenced placeholders are a visible migration gap, but not
    an estate-level refusal; report-referenced placeholders block because they affect rendered pages.

    Runs HERE, in phase 2, and therefore reads `report.json` rather than `<bundle>/handover/` -
    `slice_handovers` does not write those slices until phase 3. Moving this call after the slicing
    would work too and is the wrong fix: it reorders the coordinator's phases for one gate's
    convenience, and the slices left behind by a previous run into the same ``--output`` folder are
    stale evidence about THIS one.
    """
    report = scan_blank_placeholders(out_dir)
    (out_dir / BLANK_PLACEHOLDER_REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, kept out of ``main`` so the run logic stays readable."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="folder of .twb/.twbx to migrate")
    parser.add_argument("--output", type=Path, required=True, help="bundle output folder")
    parser.add_argument(
        "--engine",
        type=Path,
        help=(
            "DELIBERATE OVERRIDE ONLY. Defaults to the installed tableau-fabric-skills plugin, which "
            "is the single canonical engine (#107); a different path needs --allow-noncanonical-engine"
        ),
    )
    parser.add_argument(
        "--allow-noncanonical-engine",
        action="store_true",
        help="acknowledge a non-plugin --engine; the bundle receipt records the run as non-canonical",
    )
    parser.add_argument("--approved-dax", type=Path, help="landing re-run: {calc name: DAX} JSON")
    parser.add_argument(
        "--accept-bundle-rewrite",
        action="store_true",
        help=(
            "acknowledge that this run DESTROYS downstream work already in --output (an engine re-run "
            "is delete-and-recreate, not merge); the acknowledgement is recorded in the bundle"
        ),
    )
    parser.add_argument(
        "--accept-engine-version-change",
        action="store_true",
        help=(
            "acknowledge rewriting a bundle that a DIFFERENT engine version built; two versions are "
            "not equivalent (#107), so the default is to refuse rather than mix them"
        ),
    )
    parser.add_argument(
        "--slice-only",
        action="store_true",
        help="skip the engine; re-derive handovers/checks from an existing bundle",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would run, then stop")
    return parser


def resolve_run_engine(args: argparse.Namespace) -> tuple[Path | None, int]:
    """Resolve the engine ONCE, up front, and fail loudly. Returns (engine, exit code).

    ``--slice-only`` re-derives artifacts from a bundle the engine already produced, so it needs no
    engine and must keep working on a machine where the plugin is not installed.
    """
    if args.slice_only:
        return None, EXIT_OK
    try:
        engine = resolve_engine(args.engine, args.allow_noncanonical_engine)
    except (EngineNotFoundError, NonCanonicalEngineError) as exc:
        print(f"ESTATE: ENGINE_SOURCE - {exc}", file=sys.stderr)
        return None, EXIT_ENGINE_SOURCE
    provenance = engine_provenance(engine)
    log.info(
        "ENGINE SOURCE: %s VERSION=%s (%s)",
        provenance["root"],
        provenance["version"] or "unknown",
        "canonical plugin" if provenance["canonical"] else "NON-CANONICAL OVERRIDE",
    )
    return engine, EXIT_OK


def print_dry_run(args: argparse.Namespace, engine: Path | None) -> None:
    """Say exactly what would run, including WHICH engine and at what version."""
    version = engine_provenance(engine)["version"] if engine else None
    print(f"DRY RUN: engine={engine} version={version or '(n/a)'}")
    print(f"         input={args.input}  output={args.output}")
    print(f"         approved-dax={args.approved_dax or '(none)'}")


def run_engine_phase(args: argparse.Namespace, engine: Path | None, phases: list[dict]) -> int:
    """Run the deterministic engine and record its timing. Returns an exit code; 0 means proceed."""
    started = time.monotonic()
    phases.append({"phase": "engine_run", "started_wall": time.time()})
    code, output = run_engine(engine, args.input, args.output, args.approved_dax)
    elapsed = time.monotonic() - started
    phases[-1].update({"elapsed_sec": round(elapsed, 1), "exit_code": code})
    log.info("ENGINE: exit %d in %.0fs", code, elapsed)
    if code != 0:
        print(output[-2000:], file=sys.stderr)
        print(f"ESTATE: ENGINE_FAILED (exit {code})")
        return EXIT_ENGINE_FAILED
    return EXIT_OK


class GateResults(NamedTuple):
    """Every independent verdict one estate run produces, in precedence order.

    Grouped rather than passed loose because the list grows: it was three gates, is now four, and
    each addition otherwise pushes `final_verdict` and `main` past pylint's argument and local
    limits. One named bundle also makes the precedence order below readable at the call site.
    """

    collisions: dict
    dod_ok: bool
    dod_detail: str
    pbir_valid: dict
    blank_placeholders: dict
    empty_models: dict


def final_verdict(gates: GateResults, out_dir: Path) -> int:
    """The verdict the engine's own exit code cannot give us.

    Precedence is collision > definition of done > invalid PBIR > empty model. All four refuse the
    bundle and the earlier ones are the broader signal, so they are what a reader should act on
    first. Invalid PBIR outranks an empty model because it is the harder failure: a report that will
    not open correctly cannot even be assessed for whether its data landed. Only the exit code is
    exclusive: both quieter defects are PRINTED by the caller before this runs, so neither is ever
    hidden behind a louder one.
    """
    if gates.collisions:
        print_collisions(gates.collisions)
        return EXIT_COLLISION
    if not gates.dod_ok:
        print(
            f"\nESTATE: DOD_FAILED - {gates.dod_detail}\n"
            "  The engine exits 0 even on a failed definition of done (deliberate: one bad workbook\n"
            "  should not fail a batch). This is the exit code it cannot give you. Do not hand this\n"
            "  bundle downstream until the failing workbook(s) are resolved or explicitly accepted."
        )
        return EXIT_DOD_FAILED
    if gates.pbir_valid.get("status") == "INVALID":
        print(
            f"\nESTATE: INVALID_PBIR - {gates.pbir_valid['reports_invalid']} of "
            f"{gates.pbir_valid['reports_scanned']} report(s) FAIL first-party structural validation\n"
            "  These passed the engine's definition of done, which never runs the Microsoft\n"
            "  validator over its own output. A required role left unbound is usually a STUBBED\n"
            f"  measure whose projection was dropped. Details: {out_dir / PBIR_VALID_REPORT}"
        )
        return EXIT_INVALID_PBIR
    if gates.blank_placeholders.get("status") == BLANK_PLACEHOLDER_REFERENCED:
        print(
            f"\nESTATE: BLANK_PLACEHOLDER - {gates.blank_placeholders['placeholders_referenced']} "
            f"handover-backed BLANK() placeholder(s) are used by report filters or visual fields\n"
            "  The engine safely refused to translate these calcs and recorded why in handover;\n"
            "  the blocking problem is that the shipping PBIR consumes the placeholders, so a page\n"
            "  or visual can render empty while structural validation still passes. "
            f"Details: {out_dir / BLANK_PLACEHOLDER_REPORT}"
        )
        return EXIT_BLANK_PLACEHOLDER
    if gates.empty_models["status"] == STATUS_EMPTY_MODELS:
        print(
            f"\nESTATE: EMPTY_MODEL - {gates.empty_models['models_empty']} of "
            f"{gates.empty_models['models_scanned']} model(s) would open and load NO ROWS\n"
            "  These passed the definition of done: they built, they bound, and (per the check\n"
            "  above) they validate. They have no data. Nothing else in this pipeline can tell\n"
            f"  'migrated' from 'migrated and empty'. Details: {out_dir / EMPTY_MODEL_REPORT}"
        )
        return EXIT_EMPTY_MODEL
    print(
        "\nESTATE: READY - definition of done is not failed, no approval collisions, "
        "no invalid reports, no report-referenced BLANK() placeholders, no empty models."
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    phases: list[dict] = []

    if not args.slice_only and not args.input:
        print("ERROR: --input is required unless --slice-only is given", file=sys.stderr)
        return EXIT_USAGE

    engine, engine_code = resolve_run_engine(args)
    if engine_code != EXIT_OK:
        return engine_code

    # --- phase 0: the barrier ------------------------------------------------------------------
    # BEFORE the engine, because every other gate in this file reads output the engine has already
    # written - which for a landing re-run means reading it out of the crater (issue #250).
    rewrite = assess_bundle_rewrite(args, engine)
    print_bundle_rewrite(rewrite)
    if rewrite.blocking:
        return EXIT_BUNDLE_REWRITE

    if args.dry_run:
        print_dry_run(args, engine)
        return EXIT_OK

    record_bundle_rewrite_acknowledgement(rewrite)

    # --- phase 1: the engine ------------------------------------------------------------------
    if not args.slice_only:
        code = run_engine_phase(args, engine, phases)
        if code != EXIT_OK:
            return code

    report = read_report(args.output)
    if not args.slice_only:
        record_engine_output(args.output, report, phases, engine)
    else:
        backfill_slice_only_baseline(args.output, report, phases)

    # --- phase 1b: stamp where the inputs came from -------------------------------------------
    # The engine records the LOCAL half in input_manifest.json (name, size, sha256, staged path)
    # and nothing about the upstream: which Tableau site, workbook LUID, project, or product
    # version. Measured cost of that gap: filing three upstream defects required reconstructing all
    # of it by hand, and it mattered - Tableau's samples differ between releases, so figures cited
    # against "Superstore" do not reproduce against a different build and the reader cannot tell.
    # Best-effort and never fatal: a migration must not fail because a site was unreachable.
    if args.input:
        started = time.monotonic()
        stamped = stamp_inputs(args.input, args.output)
        phases.append({"phase": "provenance", "elapsed_sec": round(time.monotonic() - started, 1)})
        if stamped:
            log.info("PROVENANCE: %s", stamped)

    # --- phase 2: the check the engine's exit code cannot give us -----------------------------
    started = time.monotonic()
    dod_ok, dod_detail = check_definition_of_done(report)
    gates = GateResults(
        collisions=find_approval_collisions(report),
        dod_ok=dod_ok,
        dod_detail=dod_detail,
        pbir_valid=check_pbir_validity(args.output),
        blank_placeholders=check_blank_placeholders(args.output),
        empty_models=check_empty_models(args.output),
    )
    phases.append({"phase": "adjudicate", "elapsed_sec": round(time.monotonic() - started, 1)})

    # --- phase 3: slice -----------------------------------------------------------------------
    started = time.monotonic()
    slices = slice_handovers(report, args.output)
    phases.append(
        {"phase": "slice_handovers", "elapsed_sec": round(time.monotonic() - started, 1), "count": len(slices)}
    )

    timings = write_phase_record(args.output, phases)

    # --- report -------------------------------------------------------------------------------
    print_summary(report, args.output, slices, timings, gates.dod_detail)

    # Printed BEFORE the verdict, and on a pass as well as a fail: a quiet defect that ships
    # alongside a `failed` definition of done is the one most likely to be missed, because the reader
    # stops at the first blocking verdict.
    print("\n" + render_empty_model(gates.empty_models))
    print("\n" + render_blank_placeholders(gates.blank_placeholders))
    print("\n" + render_pbir_valid(gates.pbir_valid))

    return final_verdict(gates, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
