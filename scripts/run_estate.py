"""
purpose: run the deterministic tier over an ESTATE and turn its output into something a downstream
         agent tier can consume safely - a real exit code, collision-checked approvals, per-workbook
         handover slices, and a phase-timing record.
usage:   python scripts/run_estate.py --input <folder-of-.twb/.twbx> --output <bundle-dir>
                                      [--engine <path-to-tableau-fabric-skills>]
                                      [--approved-dax <file.json>] [--dry-run]
         python scripts/run_estate.py --slice-only --output <existing-bundle-dir>

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
This script owns that ordering so no agent has to remember it.

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

from migration_bundle import sha256_file, write_engine_receipt

log = logging.getLogger("run_estate")

# `definition_of_done.status` values that mean the estate is NOT safe to hand downstream. "warn" is
# deliberately allowed through: it is the normal state of a real migration (deferred visuals, stubbed
# calcs) and blocking on it would make the coordinator useless on every workbook that has any gap.
DOD_BLOCKING = {"failed"}

EXIT_OK = 0
EXIT_ENGINE_FAILED = 1
EXIT_DOD_FAILED = 3
EXIT_COLLISION = 4
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
    bundle: Path, report: dict | None = None, earliest_mtime: float | None = None
) -> Path:
    """Upsert generated-file hashes into ``input_manifest.json`` after the engine run.

    The deterministic engine already owns this manifest for source inputs. Adding a separate key keeps
    that contract intact while giving downstream checks a baseline for generated TMDL/PBIR drift.
    """
    manifest_path = bundle / "input_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = {"engine_input_manifest": manifest}
    else:
        manifest = {}
    manifest[GENERATED_ARTIFACTS_KEY] = {
        "version": 1,
        "run_id": uuid.uuid4().hex,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_generated_at": (report or {}).get("generated_at"),
        "report_sha256": sha256_file(bundle / "report.json") if (bundle / "report.json").is_file() else None,
        "files": generated_artifact_hashes(bundle, earliest_mtime),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


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

        result = prov.build(input_dir, prov.load_env(Path(".env")))
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


def write_receipt_phase(out_dir: Path, phases: list[dict]) -> None:
    """Persist the engine-output receipt and record the phase."""
    started = time.monotonic()
    receipt = write_engine_receipt(out_dir)
    phases.append({"phase": "engine_receipt", "elapsed_sec": round(time.monotonic() - started, 1)})
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from credential_gate import _audit  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    _audit(out_dir, "engine-receipt", f"sha256={sha256_file(receipt)}")
    log.info("ENGINE RECEIPT: %s", receipt)


def record_engine_output(out_dir: Path, report: dict | None, phases: list[dict]) -> None:
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
    write_receipt_phase(out_dir, phases)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, help="folder of .twb/.twbx to migrate")
    parser.add_argument("--output", type=Path, required=True, help="bundle output folder")
    parser.add_argument(
        "--engine",
        type=Path,
        default=Path.home() / "vscode-projects" / "tableau-fabric-skills",
        help="path to the tableau-fabric-skills clone",
    )
    parser.add_argument("--approved-dax", type=Path, help="landing re-run: {calc name: DAX} JSON")
    parser.add_argument(
        "--slice-only",
        action="store_true",
        help="skip the engine; re-derive handovers/checks from an existing bundle",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would run, then stop")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    phases: list[dict] = []

    if not args.slice_only and not args.input:
        print("ERROR: --input is required unless --slice-only is given", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"DRY RUN: engine={args.engine}")
        print(f"         input={args.input}  output={args.output}")
        print(f"         approved-dax={args.approved_dax or '(none)'}")
        return EXIT_OK

    # --- phase 1: the engine ------------------------------------------------------------------
    if not args.slice_only:
        started = time.monotonic()
        phases.append({"phase": "engine_run", "started_wall": time.time()})
        code, output = run_engine(args.engine, args.input, args.output, args.approved_dax)
        elapsed = time.monotonic() - started
        phases[-1].update({"elapsed_sec": round(elapsed, 1), "exit_code": code})
        log.info("ENGINE: exit %d in %.0fs", code, elapsed)
        if code != 0:
            print(output[-2000:], file=sys.stderr)
            print(f"ESTATE: ENGINE_FAILED (exit {code})")
            return EXIT_ENGINE_FAILED

    report = read_report(args.output)
    if not args.slice_only:
        record_engine_output(args.output, report, phases)

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
    collisions = find_approval_collisions(report)
    phases.append({"phase": "adjudicate", "elapsed_sec": round(time.monotonic() - started, 1)})

    # --- phase 3: slice -----------------------------------------------------------------------
    started = time.monotonic()
    slices = slice_handovers(report, args.output)
    phases.append(
        {"phase": "slice_handovers", "elapsed_sec": round(time.monotonic() - started, 1), "count": len(slices)}
    )

    timings = write_phase_record(args.output, phases)

    # --- report -------------------------------------------------------------------------------
    print_summary(report, args.output, slices, timings, dod_detail)

    if collisions:
        print_collisions(collisions)
        return EXIT_COLLISION

    if not dod_ok:
        print(
            f"\nESTATE: DOD_FAILED - {dod_detail}\n"
            "  The engine exits 0 even on a failed definition of done (deliberate: one bad workbook\n"
            "  should not fail a batch). This is the exit code it cannot give you. Do not hand this\n"
            "  bundle downstream until the failing workbook(s) are resolved or explicitly accepted."
        )
        return EXIT_DOD_FAILED

    print("\nESTATE: READY - definition of done is not failed, no approval collisions.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
