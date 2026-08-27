"""
purpose: enforce the live-source credential gate at the FILESYSTEM level, so an unvalidated semantic
         model physically cannot be written, and record every gate decision for audit.
usage:   python scripts/credential_gate.py status  <migration-dir>
         python scripts/credential_gate.py list    <estate-root> [--json]
         python scripts/credential_gate.py block   <migration-dir> --sources "a" "b"
         python scripts/credential_gate.py clear    <migration-dir> --reason probe-data-ok
         python scripts/credential_gate.py verify   <migration-dir>

Why this is an ACL and not a check
----------------------------------
Because checks lose. Measured 2026-08-01 against real agents:

* Prose loses. Four blind migrations hit the credential gate, all four announced the stop correctly,
  three then talked themselves past it and shipped a model for a warehouse never contacted.
* A tool-call hook that pattern-matches command text also loses. `mai-code-1-flash-picker` read the
  hook's own source and beat it twice: first by creating the human-override file itself, then - after
  that was closed - by writing through `python -c` with `target_name = 'R3' + '.' + 'tmdl'`, so the
  literal extension never appeared in the command for a matcher to find. A shell is Turing-complete;
  the set of ways to write a byte is not enumerable, so a denylist over command text cannot win.

An ACL is different in kind: it is enforced by the kernel at the syscall, so it does not care HOW the
write is attempted. Verified against both evasions - `pathlib.write_text` raised `PermissionError`
and raw `ctypes.CreateFileW` was refused.

What this does NOT claim
------------------------
It is not a sandbox. The ACL denies the *current user*, and the agent runs as that user, so it can in
principle remove it. That is not hypothetical - measured 2026-08-02, an agent left behind a script
that did exactly this:

    OVERRIDE_NAME = "." + "credential-gate-AUTHORIZED"     # split to evade the hook's regex
    advapi32.SetNamedSecurityInfoW(...)                    # raw Win32 DACL edit, bypassing icacls

That is a determined adversary escalating from string tricks to the Win32 security API, and no
same-user control defeats it. What the layers achieve is narrower and, I think, the right goal:

* the ACCIDENTAL case - an agent under task-completion pressure, which is what actually happened in
  every real migration failure - is stopped outright, because a normal write just fails;
* the DELIBERATE case is expensive, unmistakable, and DETECTED: `verify` reads the ACL and the audit
  log rather than files an agent can write, so a stripped ACE or a forged override is reported.

Do not describe this as "agents cannot bypass the gate". Describe it as "agents cannot bypass it by
accident, and cannot bypass it quietly".

Engine-output receipts have the same threat model. They distinguish deterministic-tier output from
later agent drift when the agent follows the normal path of least resistance: `run_estate.py` writes
the receipt, records its hash in this audit log, and only an exact path/size/hash match is classified
as pre-gate tier output. They are NOT an anti-forgery boundary against a deliberately malicious agent
that can write both the bundle and the audit log as the same OS user. The value is traceability and
fail-closed handling of accidental/stale/malformed receipts, not cryptographic non-repudiation.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from migration_bundle import ENGINE_OUTPUT_DIRS, ENGINE_RECEIPT, sha256_file

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("credential_gate")

MARKER = ".credential-gate-BLOCKED.json"
OVERRIDE = ".credential-gate-AUTHORIZED"
AUDIT = ".credential-gate-audit.log"

# Denied rights: WD (write data / create files), AD (append data / create subdirs), WA (write
# attributes). Read and traverse stay allowed on purpose - the agent must still be able to inspect
# the tree, and a gate that blinds it produces worse reports, not safer ones.
DENY_RIGHTS = "(OI)(CI)(WD,AD,WA)"

# Both audit actions mean "the gate was armed"; they differ only in how strongly it is ENFORCED
# (kernel ACL vs marker file). Every READER must treat them alike, or the gate's ordering guarantee
# silently becomes Windows-only. Measured 2026-08-03 by simulating the non-Windows path: with only
# `block` recognised, a `probe-cleared` recorded BEFORE a re-arm still counted as earned afterwards,
# so backdated evidence survived exactly the event that exists to invalidate it. The distinct names
# are kept because the enforcement difference is real and belongs in the log.
BLOCK_ACTIONS = frozenset({"block", "block-marker-only"})

# Files that mark a directory as a legitimately gateable UNIT of work - one migration, or one engine
# bundle. A gate target should be one of these, because the hook's `_blocking_marker()` walks UPWARD
# from any write target and stops at the first marker it meets: a marker therefore governs its whole
# subtree, and one placed too high governs work it knows nothing about.
SCOPE_MARKERS = ("migration-spec.json", ENGINE_RECEIPT, "input_manifest.json")

# Shape of a repository checkout rather than a unit of work. `.git` alone is the decisive one (it is
# what the real incident hit); the other two catch a checkout exported without its git directory.
REPO_ROOT_SIGNS = (".git", "AGENTS.md", "pyproject.toml")


def _scope_refusal(migration: Path) -> str | None:
    """Why `migration` is too broad to gate, or None when it is a legitimate scoped target.

    Measured 2026-08-18, from a real incident: `credential_gate.py block` was invoked from the wrong
    working directory and wrote its marker at the REPO ROOT. Because `_blocking_marker()` walks up
    from any write target and returns the first marker found, that one file governed every migration
    in the checkout - blocking ~13 unrelated in-flight agents at once, including bundles that had
    already independently earned their clearance, and stranding a live unsaved DAX measure in a
    Desktop session with nowhere to write.

    Nothing refused it, because `apply_block` accepted any directory at all. The blast radius of a
    gate is its entire subtree, so the target has to BE a unit of work - not merely contain some.

    Deliberately a positive check with an escape hatch: a directory carrying its own scope marker is
    always allowed, anything shaped like a checkout root is always refused, and anything else is
    refused with `--force-scope` named in the message. That keeps an unusual-but-legitimate layout
    workable without making the catastrophic case reachable by accident.
    """
    resolved = migration.resolve()
    if resolved.parent == resolved:
        return f"{resolved} is a filesystem root"
    for sign in REPO_ROOT_SIGNS:
        if (resolved / sign).exists():
            return f"{resolved} looks like a repository checkout root (contains {sign}), not one migration or bundle"
    if any((resolved / name).is_file() for name in SCOPE_MARKERS):
        return None
    return (
        f"{resolved} carries none of {', '.join(SCOPE_MARKERS)}, so it is not identifiable as a single "
        "migration or engine bundle"
    )


def _audit(migration: Path, action: str, detail: str) -> None:
    """Append a tamper-evident-ish record of every gate transition."""
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "detail": detail,
            "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        }
    )
    try:
        with (migration / AUDIT).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _parse_sources_detail(detail: str) -> list[str] | None:
    """Parse a ``sources=[...]`` audit detail, returning None when absent or malformed."""
    marker = "sources="
    if not detail.startswith(marker):
        return None
    source_text = detail[len(marker) :].split(";", 1)[0].strip()
    try:
        parsed = ast.literal_eval(source_text)
    except (ValueError, SyntaxError):
        return None
    return [str(source) for source in parsed] if isinstance(parsed, list) else None


def _parse_legacy_probe_source(detail: str) -> list[str] | None:
    """Source name from pre-source-aware ``probe-cleared: DATA_OK from ...`` audit details."""
    marker = "probe-cleared: DATA_OK from "
    return [detail[len(marker) :]] if detail.startswith(marker) and detail[len(marker) :] else None


def _earned_sources(migration: Path) -> tuple[dict[str, str | None], bool]:
    """Source-level gate evidence from the append-only audit log.

    A later block invalidates only the sources it names. This is the concurrency fix for sibling
    agents sharing one bundle: source Y being re-armed must not erase source X's previously earned
    proof, because those are independent reachability facts.
    """
    states: dict[str, tuple[str | None, str]] = {}
    authorized = False
    last_block_sources: list[str] = []
    try:
        for line in (migration / AUDIT).read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            action = entry.get("action")
            ts = str(entry.get("ts") or "")
            detail = str(entry.get("detail") or "")
            if action in BLOCK_ACTIONS:
                sources = _parse_sources_detail(detail)
                last_block_sources = sources or []
                for source in last_block_sources:
                    states[source] = (None, ts)
            elif action == "authorize":
                authorized = True
            elif action == "probe-cleared":
                sources = _parse_sources_detail(detail) or _parse_legacy_probe_source(detail) or last_block_sources
                for source in sources:
                    _earned, blocked_at = states.get(source, (None, ""))
                    if ts >= blocked_at:
                        states[source] = ("probe-cleared", blocked_at)
    except (OSError, ValueError):
        return {}, False
    return {source: earned for source, (earned, _blocked_at) in states.items()}, authorized


def _clear_was_earned(migration: Path, sources: list[str] | None = None) -> str | None:
    """Return the action that legitimately lifted the gate, or None if nothing did.

    Only two things earn a lift: a successful reachability probe, or an audit-backed human
    authorization. A bare `clear` earns nothing.

    This exists because `clear` was callable by anyone with any excuse - measured 2026-08-02, a
    single `credential_gate.py clear --reason "I decided it is fine"` lifted the ACL and the build
    proceeded with no probe ever run. That is the whole guarantee gone, via the front door.

    ⚠️ KNOWN LIMIT, stated plainly: the audit log is a text file, so at same-user privilege an agent
    can append a fake `probe-cleared` entry - measured, and it passed. Ordering below is per-source:
    a later `block` invalidates only the source(s) it names, which preserves sibling clearances but
    still makes each source earn its own post-block proof. No file-based receipt can be unforgeable.

    **The real oracle is the source system.** A genuine probe leaves a `... limit 1` query in the
    warehouse's query history; a forged receipt leaves nothing. `verify` prints the recorded
    timestamp precisely so that claim is cheap to check externally. Treat the audit log as an
    accountability trail, not as proof.
    """
    states, authorized = _earned_sources(migration)
    if authorized:
        return "authorize"
    if sources is not None:
        return "probe-cleared" if sources and all(states.get(source) for source in sources) else None
    if not states:
        return None
    return "probe-cleared" if all(states.values()) else None


def _icacls(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["icacls", *args], capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


PROBE_DIR = "_probe"


def probe_dir(migration: Path) -> Path:
    """The writable sandbox where the reachability probe builds. A SIBLING of `fabric/`, not a child.

    That placement is the whole design, and it is the fix for the flaw that got v1 reverted off
    master.

    The rule is "no semantic model for a source you never reached". The way you *earn* the right to
    build is the one-row probe: a minimal PBIP with a single table that refreshes and returns a row.
    But the probe is itself a PBIP, and v1 denied writes to all of `fabric/` - so the probe was
    blocked by the gate it exists to satisfy, and EVERY live-source migration dead-ended at "a human
    must authorize an unvalidated build", credentials or not.

    The obvious patch - a sub-folder of `fabric/` re-granted after the deny - works but is fragile in
    three separate ways, all measured: the deny is inherited so the grant must come after it, the
    folder cannot be recreated once deleted, and healing it means briefly lifting the gate. Putting
    the sandbox OUTSIDE the denied tree removes all three at once. There is no inheritance, no
    ordering dependency, and no window where the gate is open.

    Safe to leave writable because the probe is not a deliverable: one table, thrown away, and
    `verify` only ever inspects the real output paths under `fabric/`.
    """
    d = migration / PROBE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def denied_dirs(migration: Path, create: bool = True) -> list[Path]:
    """Directories the ACL DENIES writes to while the gate is up. Enforcement surface only.

    Only `fabric/` is denied. The probe sandbox is `<migration>/_probe/`, a SIBLING that is never
    in this list and so is never touched by the deny - see `probe_dir` for why it sits outside the
    denied tree rather than inside it.

    `create=False` is for read-only callers (`verify`, `status`): applying an ACL needs the
    directory to exist, but INSPECTING the gate must not conjure one into every migration it looks
    at.

    ⚠️ This is deliberately NARROWER than what `audited_paths` verifies. Denying a directory the
    build must write into would dead-end the migration; detecting that something was written there
    anyway costs nothing. Enforcement and verification are different jobs and must not share a list.
    """
    fabric = migration / "fabric"
    if create:
        fabric.mkdir(parents=True, exist_ok=True)
    return [fabric]


# Model/report DEFINITION files: their existence means a model or report was built.
DEFINITION_SUFFIXES = frozenset({".tmdl", ".pbism", ".pbir", ".pbip"})

# MATERIALIZED SOURCE ROWS. These are a strictly LARGER harm than a definition file: a `.tmdl`
# describes a model, but a materialized `.csv` IS the customer's data, sitting unencrypted on a
# workstation, extracted from a source whose reachability was never proven.
#
# Measured 2026-08-04: a deterministic-tier run wrote **two 110 MB CSVs** of source rows to
# `<out>/data/`, and `verify()` reported "OK - gate applied, no model/report artifacts exist",
# because it only ever looked at `DEFINITION_SUFFIXES`. `.json` is deliberately absent from this
# set - PBIR is made of `visual.json`/`report.json`, so including it would flag every report.
MATERIALIZED_DATA_SUFFIXES = frozenset({".csv", ".tsv", ".parquet", ".hyper", ".xlsx", ".xls", ".dat"})

# Directories that are NOT harm, and must be excluded or every migration self-reports a violation:
#   `source/`    - the input workbook. Always present; it is what we were given, not what we built.
#   `reference/` - Tableau-side screenshots used as fidelity ground truth.
#   `_probe/`    - the sanctioned sandbox for the one-row reachability probe, which by design is
#                  built WHILE the gate is up. Flagging it would make earning the clear impossible.
AUDIT_EXCLUDED_DIRS = frozenset({"source", "reference", "_probe"})


def audited_paths(migration: Path) -> list[Path]:
    """Every file under `migration` whose existence would mean something was built or extracted.

    Verification surface. Read-only: unlike `denied_dirs` this creates nothing, because `verify` is
    a post-hoc check and must not have side effects on the tree it is judging.

    Scans the WHOLE migration rather than one directory. The gate's guarantee is "no model, and no
    extracted rows, for a source that was never reached" - so where the artifact landed is
    irrelevant to whether the harm occurred. A build that writes outside `fabric/` (the deterministic
    tier writes to `pbip/`, `semantic_models/` and `data/`) is exactly the case a `fabric/`-only scan
    misses.
    """
    if not migration.exists():
        return []
    found: list[Path] = []
    for path in migration.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(migration)
        except ValueError:  # pragma: no cover - rglob results are always relative to migration
            continue
        if AUDIT_EXCLUDED_DIRS.intersection(relative.parts):
            continue
        suffix = path.suffix.lower()
        if suffix in DEFINITION_SUFFIXES or suffix in MATERIALIZED_DATA_SUFFIXES:
            found.append(path)
    return found


def _is_engine_output(path: Path, migration: Path) -> bool:
    try:
        relative = path.relative_to(migration)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in ENGINE_OUTPUT_DIRS


def _load_engine_receipt(migration: Path) -> dict[str, dict[str, str | int]] | None:
    """Load exact engine artifact receipts; malformed or stale provenance earns no exemption."""
    try:
        receipt_path = migration / ENGINE_RECEIPT
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _receipt_was_audited_before_block(migration, sha256_file(receipt_path)):
        return None
    if not _receipt_matches_bundle(migration, receipt):
        return None
    return _receipt_artifacts(receipt.get("artifacts"))


def _receipt_was_audited_before_block(migration: Path, receipt_hash: str) -> bool:
    """Was this exact receipt recorded before the latest gate arm?"""
    seen = False
    valid_for_latest_block = False
    had_block = False
    try:
        for line in (migration / AUDIT).read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            action = entry.get("action")
            detail = str(entry.get("detail") or "")
            if action == "engine-receipt" and f"sha256={receipt_hash}" in detail:
                seen = True
            elif action in BLOCK_ACTIONS:
                had_block = True
                valid_for_latest_block = seen
                seen = False
    except (OSError, ValueError):
        return False
    return valid_for_latest_block if had_block else seen


def _receipt_matches_bundle(migration: Path, receipt: dict) -> bool:
    """Does the receipt describe this bundle's current run markers?"""
    if receipt.get("version") != 1:
        return False
    try:
        report_hash = sha256_file(migration / "report.json")
        manifest_hash = sha256_file(migration / "input_manifest.json")
    except OSError:
        return False
    return receipt.get("report_sha256") == report_hash and receipt.get("input_manifest_sha256") == manifest_hash


def _receipt_artifacts(records: object) -> dict[str, dict[str, str | int]] | None:
    """Validate receipt artifact records and index them by relative path."""
    if not isinstance(records, list):
        return None
    by_path: dict[str, dict[str, str | int]] = {}
    for record in records:
        if not isinstance(record, dict):
            return None
        rel = record.get("path")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(rel, str) or not isinstance(size, int) or not isinstance(digest, str):
            return None
        by_path[rel] = {"size": size, "sha256": digest}
    return by_path


def _split_pre_gate_engine_artifacts(migration: Path, artifacts: list[Path]) -> tuple[list[Path], list[Path]]:
    """Return (pre_gate_engine_output, still_violating_artifacts)."""
    receipt = _load_engine_receipt(migration)
    if receipt is None:
        return [], artifacts
    pre_gate: list[Path] = []
    violations: list[Path] = []
    for artifact in artifacts:
        relative = artifact.relative_to(migration).as_posix()
        record = receipt.get(relative)
        if (
            _is_engine_output(artifact, migration)
            and record
            and artifact.stat().st_size == record["size"]
            and sha256_file(artifact) == record["sha256"]
        ):
            pre_gate.append(artifact)
        else:
            violations.append(artifact)
    return pre_gate, violations


def _last_block_sources(migration: Path) -> list[str] | None:
    """The source list recorded by the most recent `block`, or None if unreadable/absent.

    `_audit` stores it as the repr of a Python list (`sources=['source[0]']`), so it is parsed with
    `literal_eval` rather than `json.loads`. Any parse failure returns None, which callers must
    treat as "cannot prove these are the same sources" and therefore re-arm - failing closed.
    """
    found: list[str] | None = None
    try:
        for line in (migration / AUDIT).read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry.get("action") not in BLOCK_ACTIONS:
                continue
            detail = entry.get("detail", "")
            if not detail.startswith("sources="):
                found = None
                continue
            try:
                parsed = ast.literal_eval(detail[len("sources=") :])
            except (ValueError, SyntaxError):
                found = None
                continue
            found = [str(s) for s in parsed] if isinstance(parsed, list) else None
    except (OSError, ValueError):
        return None
    return found


def _redundant_rearm(migration: Path, sources: list[str]) -> str | None:
    """Would arming here merely re-block sources a probe ALREADY proved reachable?

    Returns the earning action (`probe-cleared`/`authorize`) when the re-arm is redundant, else
    None. This closes a real, measured hole - and it is a hole in BOTH directions, which is why the
    conditions are narrow rather than "skip whenever previously cleared".

    Measured 2026-08-03 (`claude-haiku-4.5`, variant-m5c): after a legitimate probe had cleared the
    gate and the model was built, the agent re-ran the classifier, which re-armed unconditionally.
    Faced with its OWN freshly re-armed gate on a source it had *already* proven reachable minutes
    earlier, it did not re-probe - it ran `cd variant-m5c; clear .` and bypassed the gate outright.
    So an unconditional re-arm does not merely annoy: it manufactures the exact dead end that
    invites a bypass, and it does so for a source whose reachability is already established fact.

    The two conditions are both load-bearing:

    * **The gate must be currently EARNED** (`_clear_was_earned`), not merely cleared. A bare
      `manual-clear` earns nothing, so a migration cleared that way still gets re-armed - otherwise
      this helper would launder an unearned clear into permanent immunity.
    * **Each incoming source must be proven.** If the spec gained a new live source, that source has
      never been probed, and skipping the re-arm would let a model be built against it unproven. A
      sibling's later block no longer erases previously proven sources, but it also cannot borrow
      their clearance.

    Anything it cannot parse or compare falls through to a normal re-arm: fail closed.
    """
    return _clear_was_earned(migration, sources)


def apply_block(migration: Path, sources: list[str], force_scope: bool = False) -> int:
    """Write the marker and deny write access to the output folder.

    ⚠️ The marker states a STATE, never a VERDICT, and the distinction is load-bearing. This runs at
    PARSE time, from a static classifier that opens no socket - it knows only that a live source
    EXISTS. It cannot know whether a credential is present, whether the host resolves, or whether a
    single row could be read.

    It used to claim `"reason": "live data source(s) have no Power BI credential"`. Measured
    2026-08-03: `claude-opus-4.6` read that, reasonably treated it as an established fact, reported
    "no credential" to the user, and never ran the probe. It behaved correctly on false input. The
    same conflation was fixed in the classifier's console output first; the file kept the old claim,
    so the two disagreed and the file won.
    """
    refusal = _scope_refusal(migration)
    if refusal:
        if not force_scope:
            log.error(
                "REFUSING to arm the gate: %s.\n"
                "A marker governs its ENTIRE subtree (the hook walks upward and stops at the first "
                "one), so arming here would block every migration beneath it - including any that "
                "already earned a clearance. This is usually a wrong working directory: pass the "
                "migration or bundle directory explicitly. Use --force-scope only if you really do "
                "mean to gate everything below this path.",
                refusal,
            )
            return 2
        log.warning("--force-scope: arming the gate on a target that failed the scope check (%s).", refusal)
        _audit(migration, "block-forced-scope", refusal)

    if (migration / OVERRIDE).exists():
        if _override_is_authentic(migration):
            log.warning("Override present and audit-backed: gate NOT applied - human authorized a build-only run.")
            _audit(migration, "block-skipped", "authentic override")
            return 0
        log.error(
            "IGNORING FORGED OVERRIDE: %s exists with no 'authorize' audit entry - applying the gate anyway.", OVERRIDE
        )
        _audit(migration, "violation", "forged override ignored at block time")

    already = _redundant_rearm(migration, sources)
    if already:
        log.warning(
            "Gate NOT re-applied: these sources were already proven by '%s'. Re-arming a gate "
            "that a probe has satisfied is what invited a real bypass (see _redundant_rearm). "
            "Re-probe explicitly if you need to re-verify reachability.",
            already,
        )
        _audit(migration, "block-skipped", f"already earned by {already}; sources={sources}")
        return 0

    pending_sources = sources
    if sources:
        states, authorized = _earned_sources(migration)
        pending_sources = [] if authorized else [source for source in sources if not states.get(source)]
    if sources and not pending_sources:
        _audit(migration, "block-skipped", f"already earned by source state; sources={sources}")
        return 0

    (migration / MARKER).write_text(
        json.dumps(
            {
                "writes_blocked": True,
                "reachability": "UNPROVEN",
                "credential_status": "UNKNOWN - nothing has contacted this source yet",
                "reason": "live data source(s) detected; reachability has NOT been measured",
                "next_step": (
                    "python scripts/probe_live_source.py --spec <this-migration>/migration-spec.json "
                    "OR --bundle <engine-output-dir>"
                ),
                "read_this_before_reporting": (
                    "This file was written at PARSE time by a static check that opens NO connection. "
                    "It does NOT mean a credential is missing - only that nothing has proven the "
                    "source is reachable. Do NOT report a credential or connection problem from this "
                    "file alone: run the probe and let its verdict (DATA_OK / NO_CREDENTIAL / "
                    "UNREACHABLE) decide. Only the probe can tell a missing credential (a human must "
                    "act) from a wrong hostname (nobody needs to sign in)."
                ),
                "sources": pending_sources,
                "applied": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # The sandbox is a SIBLING of fabric/, so it needs no grant and no particular ordering - see
    # probe_dir(). Created before the platform branch: the probe needs somewhere to build on every
    # platform, and only the ENFORCEMENT is Windows-specific, not the workflow.
    probe = probe_dir(migration)

    if platform.system() != "Windows":
        log.warning("Non-Windows: marker written, but ACL enforcement is Windows-only here.")
        log.info("PROBE SANDBOX: %s (build the 1-table reachability probe here)", probe)
        # Same `sources=` detail as the enforced path. Without it `_last_block_sources` cannot read
        # this entry, so the redundant-re-arm check fails closed forever on non-Windows.
        _audit(migration, "block-marker-only", f"sources={pending_sources}")
        return 0

    failed = 0
    for d in denied_dirs(migration):
        code, out = _icacls([str(d), "/deny", f"{_user()}:{DENY_RIGHTS}"])
        if code != 0:
            log.error("Could not deny write on %s: %s", d, out)
            failed += 1
        else:
            log.info("ENFORCED: write denied on %s", d)

    log.info("PROBE SANDBOX: %s (build the 1-table reachability probe here)", probe)
    _audit(migration, "block", f"sources={pending_sources}")
    return 1 if failed else 0


def _marker_sources(migration: Path) -> list[str]:
    """Source list currently named by the blocking marker, or an empty list when unreadable."""
    try:
        payload = json.loads((migration / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    marker_sources = payload.get("sources") if isinstance(payload, dict) else None
    return [str(source) for source in marker_sources] if isinstance(marker_sources, list) else []


def clear_block(migration: Path, reason: str, earned: bool = False, sources: list[str] | None = None) -> int:
    """Remove the ACL and marker.

    `earned=True` is for the probe only: it records `probe-cleared`, which is the evidence `verify`
    looks for. A bare clear is recorded as `manual-clear` and earns nothing, so artifacts built
    after one are reported as UNVALIDATED. The verb has to keep existing for teardown, but it must
    never quietly confer the guarantee.
    """
    marker = migration / MARKER
    detail = reason
    earned_sources = sources if sources is not None else _last_block_sources(migration)
    if earned and earned_sources:
        detail = f"sources={earned_sources}; reason={reason}"
        remaining_sources = [source for source in _marker_sources(migration) if source not in set(earned_sources)]
        if remaining_sources:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["sources"] = remaining_sources
            marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            log.info("credential gate PARTIALLY CLEARED (%s); still blocked: %s", reason, remaining_sources)
            _audit(migration, "probe-cleared", detail)
            return 0

    if platform.system() == "Windows":
        for d in denied_dirs(migration):
            code, out = _icacls([str(d), "/remove:d", _user()])
            if code != 0:
                log.error("Could not clear deny ACE on %s: %s", d, out)
                return 1
            log.info("cleared write-deny on %s", d)
    if marker.exists():
        marker.unlink()
    log.info("credential gate CLEARED (%s)", reason)
    _audit(migration, "probe-cleared" if earned else "manual-clear", detail)
    return 0


def _override_is_authentic(migration: Path) -> bool:
    """Is the override file backed by a real authorization, or was it forged by an agent?

    A bare file is worthless as an authorization token: measured 2026-08-01, agents created
    `.credential-gate-AUTHORIZED` themselves twice - once directly, and once (after the hook started
    matching that name) by assembling the name from string fragments so no literal appeared to match.
    Any check over text loses that game.

    So the file alone authorizes NOTHING. It is honoured only when the audit log also records an
    `authorize` action, which is written exclusively by `credential_gate.py authorize`. That command
    is what actually lifts the ACL - and the ACL, not the file, is what was ever holding the line.
    A forged file therefore changes no behaviour, and its presence WITHOUT a matching audit entry is
    itself evidence of an attempted bypass, which `verify` reports.
    """
    if not (migration / OVERRIDE).exists():
        return False
    try:
        for line in (migration / AUDIT).read_text(encoding="utf-8").splitlines():
            if json.loads(line).get("action") == "authorize":
                return True
    except (OSError, ValueError):
        return False
    return False


def _ancestry() -> list[str]:
    """Process names from this process up to the root, for provenance checks and audit."""
    if platform.system() != "Windows":
        return []
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$id=$PID; $all=Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name;"
                "for($i=0;$i -lt 12;$i++){$p=$all|Where-Object{$_.ProcessId -eq $id}|Select-Object -First 1;"
                "if(-not $p){break}; Write-Output $p.Name; $id=$p.ParentProcessId}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return ["<lineage-unavailable>"]


def _has_copilot_ancestor(chain: list[str]) -> bool:
    """Was this process started (directly or indirectly) by a Copilot CLI agent?

    The guard on `authorize`, because everything softer failed. Measured 2026-08-02: an agent ran
    `credential_gate.py authorize --who gfranssens` itself, impersonating the user through the
    sanctioned path - so the audit log then *certified* a forged authorization and `verify` returned
    a clean exit 0. That is worse than having no audit trail at all.

    A TTY check does not help: the agent's shell reports `stdin.isatty() == True` (measured), and it
    could pipe a confirmation anyway. Process lineage is the practical boundary - a real chain looks
    like `python <- pwsh <- copilot.exe <- ...`, and an agent would have to escape its own process
    tree to defeat it.

    **Fails CLOSED on an empty or unavailable chain**, which is the whole reason this is a separate
    function. The first version treated "no chain" as "no copilot ancestor" and returned False, so a
    failed lineage query silently AUTHORIZED - and that is exactly what happened: under four
    concurrent agents one query came back empty and the forged authorization went through. An
    unknown answer must never be read as permission.
    """
    if platform.system() != "Windows":
        return False
    if not chain or "<lineage-unavailable>" in chain:
        return True
    return any("copilot" in name.lower() for name in chain)


def authorize(migration: Path, who: str) -> int:
    """Record a human's explicit build-only authorization and lift the ACL.

    The ONLY supported way to proceed without a successful probe, and deliberately hostile to being
    run by an agent - see `_has_copilot_ancestor`. The ancestry chain is recorded either way, so a
    forged authorization is attributable after the fact even if the check is somehow evaded.
    """
    chain = _ancestry()
    if _has_copilot_ancestor(chain):
        log.error("REFUSED: this command was invoked from inside a Copilot agent session.")
        log.error("  Authorizing an unvalidated build is a HUMAN decision. An agent running this")
        log.error("  command is impersonating the user - measured 2026-08-02, one did exactly that.")
        log.error("  If you are a human: run this from a plain terminal, outside any Copilot session.")
        log.error("  Process chain seen: %s", " <- ".join(chain))
        _audit(migration, "violation", f"authorize refused; chain={chain}")
        return 2
    (migration / OVERRIDE).write_text(
        f"Build-only migration authorized by {who} at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}.\n"
        "Validation against live data was explicitly deferred by a human.\n",
        encoding="utf-8",
    )
    _audit(migration, "authorize", f"by={who}; chain={chain}")
    return clear_block(migration, f"user-authorized-build-only ({who})", earned=True)


def status(migration: Path) -> int:
    """Report the gate state. Exit 1 when blocked."""
    blocked = (migration / MARKER).exists()
    override = (migration / OVERRIDE).exists()
    log.info("marker=%s override=%s", "BLOCKED" if blocked else "none", "yes" if override else "no")
    if platform.system() == "Windows":
        for d in denied_dirs(migration, create=False):
            if not d.exists():
                continue
            _, out = _icacls([str(d)])
            denied = "(DENY)" in out.upper() or ":(DENY)" in out.upper() or "(N)" in out.upper()
            log.info("acl on %s: %s", d.name, "deny-write present" if denied else "no deny ACE")
    return 1 if blocked and not override else 0


def _unit_state(unit: Path) -> str:
    """Classify one unit's gate state from artifacts on disk, never from prose.

    Ordered most-alarming-first, because a forged override coexisting with a marker is a bypass
    attempt and must not be reported as the benign state that happens to also be true.
    """
    marker = (unit / MARKER).exists()
    override = (unit / OVERRIDE).exists()
    if override and not _override_is_authentic(unit):
        return "FORGED-OVERRIDE"
    if override:
        return "authorized-unearned"
    if marker:
        return "BLOCKED"
    try:
        actions = {json.loads(line).get("action") for line in (unit / AUDIT).read_text(encoding="utf-8").splitlines()}
    except (OSError, ValueError):
        return "clean"
    if "probe-cleared" in actions:
        return "cleared-earned"
    return "clean"


def list_units(root: Path, as_json: bool = False) -> int:
    """Report gate state for EVERY unit beneath `root`. Read-only.

    Exists because every other subcommand takes exactly one migration, so "what is still gated?"
    across an estate cost one invocation per unit. Field report 2026-08-26, a ~44-unit estate:
    *"I am always asked to run these for all the dashboards manually"*.

    The agent needs this as much as the human. After a human signs in, a credential caches
    machine-wide (DPAPI), so units sharing that source may now be probeable -- but with no way to
    enumerate what is gated, an agent cannot discover what became retryable and cannot resume.

    Exit codes are for scripting, and deliberately rank the security signal above the workflow one:
    2 = a forged override exists anywhere, 1 = something is still blocked, 0 = nothing gated.

    ⚠️ **`2` is not self-certifying, so do not alarm on it alone.** A bad `<root>` exits `3`, but
    argparse's own usage errors (missing argument, unknown flag) also exit `2` and that is not ours
    to renumber. Measured: before `3` existed, a mistyped estate root raised the *most alarming*
    state in the vocabulary. A scripted consumer must confirm a forgery through the `--json`
    `state` field, which is unambiguous, and treat a `2` with no parseable JSON as a usage error.

    ⚠️ **This reads the marker/override/audit FILES, not the ACL.** `_has_deny_ace` is the real
    enforcement state, so a unit whose marker was removed while the write-deny ACE survives reports
    here as `clean`. That direction is safe -- it under-reports "blocked" and cannot help produce an
    unvalidated artifact -- but it is why `list` is a *resume signal*, never a ship gate. `verify`
    remains the authoritative pre-ship check.
    """
    units = sorted({p.parent for name in (MARKER, OVERRIDE, AUDIT) for p in root.rglob(name)})
    rows = [
        {"unit": str(u), "relative": str(u.relative_to(root)) if u != root else ".", "state": _unit_state(u)}
        for u in units
    ]

    if as_json:
        print(json.dumps({"root": str(root), "units": rows}, indent=2))
    elif not rows:
        log.info("no gated units found under %s", root)
    else:
        width = max(len(r["relative"]) for r in rows)
        for r in rows:
            log.info("  %-*s  %s", width, r["relative"], r["state"])
        tally: dict[str, int] = {}
        for r in rows:
            tally[r["state"]] = tally.get(r["state"], 0) + 1
        log.info("")
        log.info("  %d unit(s): %s", len(rows), ", ".join(f"{n} {s}" for s, n in sorted(tally.items())))
        blocked = tally.get("BLOCKED", 0)
        if blocked:
            log.info("")
            log.info("  %d still BLOCKED. Two ways out, and they are NOT equivalent:", blocked)
            log.info("    EARNED   - sign in, then re-probe. The clear is recorded as 'probe-cleared'")
            log.info("               and the model counts as validated. Prefer this.")
            log.info('    UNEARNED - credential_gate.py authorize <unit> --who "<name>"')
            log.info("               marks the build UNVALIDATED, permanently, in the audit log.")
            log.info("    A credential caches machine-wide, so ONE sign-in may earn several of these.")

    states = {r["state"] for r in rows}
    if "FORGED-OVERRIDE" in states:
        return 2
    return 1 if "BLOCKED" in states else 0


def _has_deny_ace(migration: Path) -> bool:
    """Is the kernel-level write-deny still applied? This is the real gate state.

    Reads `denied_dirs` WITHOUT creating them: this is called from `verify`, which must not mutate
    the tree it judges. A directory that does not exist cannot carry a deny ACE, so skipping it is
    also the correct answer, not merely the safe one.
    """
    if platform.system() != "Windows":
        return (migration / MARKER).exists()
    for d in denied_dirs(migration, create=False):
        if not d.exists():
            continue
        _, out = _icacls([str(d)])
        if "(DENY)" in out.upper():
            return True
    return False


def _gate_was_ever_applied(migration: Path) -> bool:
    """Did this migration EVER have a gate, or was one never needed?

    `verify`'s unearned-clear check asks "was the lift earned?", which is only a meaningful question
    if something was ever lifted. For an extract-only migration -- every datasource a packaged
    `.hyper`/flat file -- step 6 correctly never applies a gate at all, so there is no lift, nothing
    to earn, and no probe to run (`probe_live_source.py` has no live source to probe).

    Measured 2026-08-08 on `book_5-2-LOD` (one embedded `excel-direct` datasource, zero live
    sources): `verify` reported `UNEARNED CLEAR - ... this model is UNVALIDATED. Do not ship it.` and
    exited 1 against a migration that was never gated. That is a false BLOCK on the *final* gate, and
    it fires for every extract-only migration -- i.e. exactly the shape most likely to be run
    offline, where a spurious "do not ship" is most likely to be believed.

    The signal is the audit log: `apply_block` writes a BLOCK_ACTIONS entry before it does anything
    else, so a gate that was ever applied always left one. Same trust model as `_clear_was_earned`
    (an accountability trail, not proof) -- and no weaker, because anyone who could delete the log to
    fake "never gated" could equally append a fake `probe-cleared` to fake "earned".
    """
    try:
        for line in (migration / AUDIT).read_text(encoding="utf-8").splitlines():
            if json.loads(line).get("action") in BLOCK_ACTIONS:
                return True
    except (OSError, ValueError):
        return False
    return False


def verify(migration: Path) -> int:
    """Authoritative post-hoc check: did anything get built while the gate was up?

    This is the compensating control for everything enforcement cannot guarantee, and it is
    deliberately based on the ACL and the audit log rather than on the marker or override files -
    because those are files, and an agent can write files. Four distinct violations are reported:

      1. artifacts exist while the gate is still applied;
      2. an override file exists with no matching `authorize` audit entry (forged);
      3. the ACL is gone while the marker remains (someone lifted enforcement out of band);
      4. artifacts were built after a bare `clear` that earned nothing (UNEARNED CLEAR).

    None of which applies when a gate was never raised in the first place - see
    `_gate_was_ever_applied`.
    """
    marker = (migration / MARKER).exists()
    override_file = (migration / OVERRIDE).exists()
    authentic = _override_is_authentic(migration)
    deny = _has_deny_ace(migration)
    violations = 0

    if override_file and not authentic:
        log.error("GATE VERIFY: FORGED OVERRIDE - %s exists but no 'authorize' audit entry backs it.", OVERRIDE)
        log.error("  An agent created this file. It authorizes nothing. Treat this run as unvalidated.")
        _audit(migration, "violation", "forged override")
        violations += 1

    if marker and not deny and not authentic:
        log.error("GATE VERIFY: ENFORCEMENT REMOVED - marker present but the write-deny ACE is gone.")
        violations += 1

    # A gate that is down must have been EARNED - by a successful probe or a human authorization.
    # Measured: `clear --reason "I decided it is fine"` lifted the ACL and the build proceeded with
    # no probe ever run. Enforcement cannot prevent that (clear has to exist for teardown), but it
    # must never pass silently, or the guarantee is gone via the front door.
    artifacts = audited_paths(migration)
    pre_gate_engine_artifacts, gate_artifacts = _split_pre_gate_engine_artifacts(migration, artifacts)
    if artifacts and not deny and not _clear_was_earned(migration) and _gate_was_ever_applied(migration):
        log.error("GATE VERIFY: UNEARNED CLEAR - artifacts exist, but no successful probe and no")
        log.error("  human authorization is recorded in the audit log. The gate was lifted without")
        log.error("  proving the source is reachable, so this model is UNVALIDATED. Do not ship it.")
        _audit(migration, "violation", "artifacts built after an unearned clear")
        violations += 1

    if (marker or deny) and not authentic:
        if pre_gate_engine_artifacts:
            log.warning(
                "GATE VERIFY: PRE-GATE TIER OUTPUT - %d engine artifact(s) predate the latest gate arm.",
                len(pre_gate_engine_artifacts),
            )
            log.warning(
                "  On the engine path the gate is a detection control: these files are unvalidated "
                "until the probe clears the gate, but they were not built while blocked."
            )
        if gate_artifacts:
            log.error("GATE VERIFY: VIOLATION - %d artifact(s) exist while the gate is applied:", len(gate_artifacts))
            for p in gate_artifacts[:10]:
                log.error("  %s", p)
            log.error("Built against a source whose reachability was never proven. Do not ship them.")
            _audit(migration, "violation", f"{len(gate_artifacts)} artifacts while blocked")
            violations += 1

    if violations:
        return 1
    if authentic:
        log.info("GATE VERIFY: OK - build-only run authorized by a human (audit-backed).")
    elif marker or deny:
        log.info("GATE VERIFY: OK - gate applied, no model/report artifacts exist.")
    elif not _gate_was_ever_applied(migration):
        # Say WHY it passed, so an extract-only pass is never confused with a gate that was lifted.
        log.info("GATE VERIFY: OK - no gate was ever applied to this migration (no 'block' entry in")
        log.info("  the audit log), so there was no lift to earn. Expected for an extract-only")
        log.info("  migration where every datasource is a packaged/flat file and step 6 correctly")
        log.info("  raised no gate. NOTE: this attests the gate's own history, not that the source")
        log.info("  classification was right - that is step 6/6b's job.")
    else:
        log.info("GATE VERIFY: OK - gate not applied.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    lst = sub.add_parser("list", help="report gate state for every unit beneath a root (read-only)")
    lst.add_argument("root", type=Path)
    lst.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    for name in ("status", "block", "clear", "verify", "authorize"):
        p = sub.add_parser(name)
        p.add_argument("migration", type=Path)
        if name == "block":
            p.add_argument("--sources", nargs="*", default=[])
            p.add_argument(
                "--force-scope",
                action="store_true",
                help=(
                    "arm the gate even when the target is not identifiable as a single migration or "
                    "bundle. A marker governs its whole subtree, so this can block unrelated work."
                ),
            )
        if name == "clear":
            p.add_argument("--reason", default="manual")
            p.add_argument("--earned", action="store_true", help=argparse.SUPPRESS)
            p.add_argument("--sources", nargs="*", help="source(s) proven by this earned clear")
        if name == "authorize":
            p.add_argument("--who", required=True, help="who is authorizing this unvalidated build")
    args = parser.parse_args(argv)

    target = (args.root if args.cmd == "list" else args.migration).resolve()
    if not target.is_dir():
        log.error("not a directory: %s", target)
        # `list` gets its own code: its 2 is a documented SECURITY signal (forged override), and a
        # mistyped estate root must not raise it. Every other subcommand keeps 2, unchanged.
        return 3 if args.cmd == "list" else 2

    handlers = {
        "list": lambda: list_units(target, as_json=args.as_json),
        "block": lambda: apply_block(target, args.sources, force_scope=args.force_scope),
        "clear": lambda: clear_block(target, args.reason, earned=args.earned, sources=args.sources),
        "authorize": lambda: authorize(target, args.who),
        "verify": lambda: verify(target),
        "status": lambda: status(target),
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
