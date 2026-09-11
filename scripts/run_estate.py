"""
purpose: run the deterministic tier over an ESTATE and turn its output into something a downstream
         agent tier can consume safely - a real exit code, collision-checked approvals, per-workbook
         handover slices, and a phase-timing record.
usage:   python scripts/run_estate.py --input <folder-of-.twb/.twbx/.tds/.tdsx> --output <bundle-dir>
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
   default), `Rank`, `Size`, `Running Sum`. That 6-workbook sample measured 0 collisions, which read
   as a LATENT hazard - but at estate scale it is OBSERVED: a 52-asset run (engine 2.339.0,
   2026-08-29) exited 4 `EXIT_COLLISION` on `calculation1`, claimed by two models with differing
   formulas. Sample size, not luck, is what made it look latent.

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

8. **The pre-engine path projection is fail-open, so the EMITTED tree is measured too (issue #564).**
   `preflight_estate_path_ceiling` projects the canonical PBIR visual tail onto names knowable before
   conversion; the path that actually breaches on the committed issue-194 repro is an uncapped
   SEMANTIC-MODEL table filename, which no pre-conversion projection here models. A bundle could
   therefore pass the projection and still be one Power BI Desktop refuses to open, with every
   signal green. `check_emitted_path_ceiling` measures what the engine really wrote - through
   `check_path_ceiling.scan`, the same walker and the same measured 259/247 ceilings every other
   consumer uses - immediately after the output is recorded and BEFORE provenance, handover slices,
   packaging, agents or Desktop. Over the ceiling, unmeasurable or unwalkable all return
   `EXIT_PATH_CEILING`; the output is preserved as evidence and never deleted or rewritten
   (permanent filename shortening is an upstream engine fix). The verdict is published ATOMICALLY
   (staging sibling + `os.replace`, so a failed write cannot destroy a previous report) and
   SHAREABLE: what lands in `path-ceiling.json` and on the console is bundle-relative, carrying the
   offending tail but never the run root, account or customer folder.

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
is specifically about `--approved-dax`. `assess_bundle_rewrite` now runs BEFORE the engine and
refuses with `EXIT_BUNDLE_REWRITE` on either finding:

* **downstream work would be destroyed** - the bundle is re-hashed against the baselines the
  previous run wrote: `engine_output_tree` in `input_manifest.json` (every file in every folder a
  re-run deletes, with NO format allowlist), plus the engine receipt and `generated_artifacts`.
  `--accept-bundle-rewrite` proceeds, and the acknowledgement is recorded in the bundle.
* **the bundle was built by a DIFFERENT engine version** - 2.113.0 emitted deprecated Bing
  `shapeMap` visuals and dropped a density-map worksheet entirely where 2.126.0 emitted `azureMap`
  with a heat layer, and nothing in the output said which ran (#107). Re-running in place silently
  mixes both. `--accept-engine-version-change` proceeds.

Two SEPARATE flags on purpose: one would mean accepting a known engine bump also silently waives the
destruction guard for work you did not know was there.

**The rule that governs the whole barrier: where it cannot assess, it BLOCKS.** A missing, empty,
truncated or `--slice-only`-backfilled baseline, or an unreadable engine version, is an explicit
indeterminate state - never a pass. The first cut of this file reported *clean* on all five of those
routes and a reviewer destroyed a sentinel through each of them at exit 0. Legacy and third-party
bundles stay usable through the acknowledgement flags, which is what they are for. `--slice-only`
alone is genuinely exempt: it never invokes the engine, so it destroys nothing.

(Line numbers are against engine HEAD `81e6164`. They drift on every upstream release - the four
`rmtree`/guard sites moved ~30 lines between 2.72.0 and 2.78.0 with no behaviour change - so re-derive
them by symbol, not by number, if they do not match.)
"""

from __future__ import annotations

# The coordinator and its pre-engine barrier are one procedure: the barrier's whole job is to run
# before `main`'s phase 1, and the docstring above is the measured knowledge that keeps it correct.
# DEFERRED FIX: extract the barrier (`assess_bundle_rewrite` and its helpers, ~390 lines) into
# `scripts/bundle_rewrite_guard.py`. Not done here because a new `scripts/*.py` must be classified in
# `docs/agent-capability-wiring.md` or carry an `internal: true` marker that no script in this repo
# uses yet - pioneering that mechanism belongs in its own change, not in a security fix.
# pylint: disable=too-many-lines

import argparse
import copy
import contextlib
import json
import logging
import math
import multiprocessing
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
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
from check_path_ceiling import (
    DIR_CEILING,
    FILE_CEILING,
    STATUS_NO_PATHS,
    STATUS_OVER_CEILING,
    STATUS_UNKNOWN_PATHS,
    WINDOWS_LIMITS,
    Limits,
    utf16_len,
)
from check_path_ceiling import scan as scan_path_ceiling
from engine_source import EngineNotFoundError, NonCanonicalEngineError, engine_provenance, resolve_engine
from manifest_scope import redact_host_paths
from migration_bundle import ENGINE_RECEIPT, sha256_file, write_engine_receipt

import stamp_tableau_provenance as prov  # isort: skip  # the provenance worker AND its result vocabulary

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
EXIT_PATH_CEILING = 10
EXIT_PROVENANCE_FAILED = 11
GENERATED_ARTIFACTS_KEY = "generated_artifacts"
SLICE_ONLY_COVERAGE = "slice_only_backfill"
SOURCE_PROVENANCE_REPORT = "source-provenance.json"

#: Where the post-engine path measurement lands inside the bundle, so a refusal is ATTRIBUTABLE to
#: named paths rather than to a console line nobody kept. `path-ceiling.json` is the name this repo
#: already uses for `check_path_ceiling.py --json` output (`check_unit.py` reads exactly that file),
#: so the bundle carries one convention rather than a second private one.
PATH_CEILING_REPORT = "path-ceiling.json"

#: The MEASURED Desktop pair (259 file / 247 directory, UTF-16 code units), applied unconditionally:
#: the question is "will the machine this bundle is shipped to open it", which is a Windows question
#: wherever the run happens. `min_root_budget` stays None, so the tight-root-budget number remains
#: ADVISORY here - it is reported, never a refusal.
PATH_CEILING_LIMITS = WINDOWS_LIMITS

#: How a measured path is spelled once it leaves this process. The measurement itself keeps the
#: absolute path (that is what Desktop counts), but a `path-ceiling.json` is shared upstream, pasted
#: into an issue and read by an agent, so what is PERSISTED and PRINTED is bundle-relative: the
#: refusal stays actionable (the offending tail is the whole point) while the run root - drive,
#: account name, customer folder - never leaves the machine that measured it.
SAFE_BUNDLE_ROOT = "<bundle>"

#: How the provenance artifact is NAMED once the name leaves this process. The same reasoning as
#: `SAFE_BUNDLE_ROOT`: the console line is pasted into an issue and read by an agent, so it names the
#: artifact bundle-relatively. The absolute path stays inside the process that wrote it.
SAFE_SOURCE_PROVENANCE_REPORT = f"{SAFE_BUNDLE_ROOT}/{SOURCE_PROVENANCE_REPORT}"

#: A path the transform could not PROVE lies inside the bundle. It is reported as an ordinal, never
#: echoed and never re-spelled as if it were relative: "I could not place this" is a different and
#: honest answer, and echoing it is exactly the disclosure this transform exists to prevent.
UNASSESSABLE_PATH = "<path-not-provably-inside-the-bundle-{index}>"

#: The same answer for a DIAGNOSTIC. Free-form text is not published or printed at all: a message
#: is written by whatever raised it, so proving one carries no path means parsing prose, and the
#: prefix-collision finding is what that costs (a root `…\bundle` "sanitised" a sibling
#: `…\bundle-foreign` into `<bundle>-foreign`, inventing containment that does not exist). What is
#: published instead is a stable code plus an ordinal, and - only for an exception this module
#: caught itself - its class name and numeric code, read STRUCTURALLY off the object.
SCAN_UNASSESSABLE_CODE = "scan-unassessable"
UNKNOWN_PATH_CODE = "unknown-path-{index}"

#: The operations this module may name in a log line. An allowlist, so a label is a constant of this
#: file rather than anything derived from data.
PUBLISH_REPORT_OPERATION = "publish-path-ceiling-report"
PUBLISH_PROVENANCE_OPERATION = "publish-source-provenance"
WRITE_PHASE_RECORD_OPERATION = "write-phase-record"
_ALLOWED_OPERATIONS = frozenset({PUBLISH_PROVENANCE_OPERATION, PUBLISH_REPORT_OPERATION, WRITE_PHASE_RECORD_OPERATION})

# --- the provenance deadline (issue #576) ----------------------------------------------------
#
# Measured: 66 harvested inputs cost 200 remote calls, and a single trickled response body outlasts
# `urlopen(timeout=180)`, because that timeout bounds each socket read and not the transfer. Neither
# a local `read_bytes`, a ZIP member scan, a recursive scrub nor a hung sign-out has any bound at
# all. A thread cannot be stopped, a cooperative check cannot interrupt a blocking call, and
# `Future.cancel()` returns False for a task already running - all three were measured. What
# preempts every one of them on Windows AND POSIX is a separate process the parent can terminate, so
# the whole phase runs in ONE spawned leaf worker under ONE monotonic deadline computed BEFORE the
# spawn: Windows spawn/import time is phase time and is charged as such.
#
# Deliberately OUTSIDE the deadline: the parent's own atomic publication. The artifact must be
# written AFTER expiry - recording the timeout is the whole point - so this is not a bound on total
# wall clock if the output filesystem hangs. That is a separate writer-process design, not this one.

#: The whole-phase budget. Generous on purpose: a backstop against an unbounded stall, not a
#: performance target.
PROVENANCE_TIMEOUT_DEFAULT_SEC = 120.0

#: The bounded joins after the deadline. Never an unbounded `join()`: waiting forever for the process
#: we just killed would reintroduce exactly the hang this phase exists to bound.
PROVENANCE_TERMINATE_JOIN_SEC = 0.5
PROVENANCE_KILL_JOIN_SEC = 1.0
PROVENANCE_RECEIVER_JOIN_SEC = 0.1

# Bounds apply before allocation/validation, not after accepting an alleged count. They are
# fail-closed protocol limits, not a limit on the engine or on the standalone stamper.
PROVENANCE_MAX_INPUTS = 4096
PROVENANCE_MAX_MEMBERS = 4096
PROVENANCE_MAX_FRAME_BYTES = 2 * 1024 * 1024
PROVENANCE_MAX_PHASE_BYTES = 16 * 1024 * 1024
PROVENANCE_MAX_MESSAGES = 8 * PROVENANCE_MAX_INPUTS + 32
PROVENANCE_MAX_COUNT = (1 << 63) - 1

#: The one prefix every machine-readable progress line carries.
PROVENANCE_PROGRESS_PREFIX = "PROVENANCE_PROGRESS"

PROVENANCE_PHASE_OPERATION = "phase"
PROVENANCE_PUBLISH_OPERATION = "publish"

#: Everything a progress line may NAME: the worker's operations plus the two the parent owns. An
#: operation label is therefore always a constant of this repository, never anything derived from a
#: filename, a site response or an exception.
PROVENANCE_PROGRESS_OPERATIONS = frozenset({PROVENANCE_PHASE_OPERATION, PROVENANCE_PUBLISH_OPERATION}) | frozenset(
    prov.WORKER_OPERATIONS
)
PROVENANCE_PROGRESS_EVENTS = frozenset({"phase-start", "operation-progress", "phase-finish"})
PROVENANCE_PROGRESS_STATUSES = frozenset(
    {"success", "local_only", "partial", "failed", "empty", "publication_failed", "unknown"}
)

#: The stable codes this supervisor records about work the worker never finished. Every one is a
#: NON-SUCCESS direction: a phase that was cut short, crashed, spoke nonsense or could not be reaped
#: has proved nothing about the inputs.
PROVENANCE_DEADLINE_CODE = prov.DEADLINE_CODE
PROVENANCE_CRASH_CODE = "worker-crashed"
PROVENANCE_PROTOCOL_CODE = "worker-protocol-invalid"
PROVENANCE_REAP_CODE = "worker-reap-failed"
PROVENANCE_START_CODE = "worker-start-failed"

#: The closed shape a checkpoint may have. The worker reduces every checkpoint to what it DERIVED
#: (sizes, digests, CRCs) because checkpoints are emitted BEFORE scrub has run; the parent refuses
#: anything else, so a message carrying a filename, a member name or an exception message is a
#: protocol violation rather than evidence.
_CHECKPOINT_KEYS = frozenset({"input", "fingerprint_error"})
_CHECKPOINT_INPUT_KEYS = frozenset({"size_bytes", "sha256", "revision_key", "members", "status"})
_CHECKPOINT_MEMBER_KEYS = frozenset({"size_bytes", "crc32"})
_CHECKPOINT_REVISION_KEYS = frozenset({"algo", "value"})
_CHECKPOINT_ERROR_KEYS = frozenset({"code", "operation", "exception_class", "errno", "winerror"})
_ERROR_KEYS = _CHECKPOINT_ERROR_KEYS | {"http_status"}
_WORKER_ERROR_CODES = frozenset(
    {
        "collect-inputs-failed",
        "empty-input",
        "local-fingerprint-failed",
        "live-lookup-refused",
        "live-lookup-failed",
        "content-unavailable",
        "scrub-failed",
        "sign-out-failed",
        "build-failed",
        prov.CANCELLED_CODE,
        prov.DEADLINE_CODE,
    }
)
_ERROR_OPERATIONS = prov.WORKER_OPERATIONS | {"lookup-origin", "download-workbook", "build", "scrub-local-fields"}
_REVISION_ALGORITHMS = frozenset({"twbx-content-v3", "tableau-xml-v1", "raw-sha256-v1"})
_OPERATION_ORDER = {
    prov.OP_COLLECT_INPUTS: 0,
    prov.OP_FINGERPRINT: 1,
    prov.OP_SIGN_IN: 2,
    prov.OP_INVENTORY: 3,
    prov.OP_CONTENT: 4,
    prov.OP_SCRUB: 5,
    prov.OP_SIGN_OUT: 6,
}
_ORIGIN_TEXT_KEYS = frozenset(
    {
        "server",
        "site",
        "workbook_luid",
        "workbook_name",
        "project",
        "owner_luid",
        "created_at",
        "updated_at",
        "tableau_product_version",
        "rest_api_version",
    }
)
_ORIGIN_KEYS = _ORIGIN_TEXT_KEYS | {
    "matched_by",
    "match",
    "content_unavailable",
    "revision_match",
    "remote_revision_key",
    "remote_sha256",
    "same_name_count",
}

#: Where `scan()` records a single measured path, and where it records a list of them.
_PATH_RECORD_KEYS = ("longest", "root_budget_binding")
_PATH_LIST_KEYS = ("worst_offenders", "near_ceiling_paths", "unknown_paths")
VOLATILE_GENERATED_DIRS = {".pbi"}
SCRATCH_DIRS = frozenset({"scratch", "_work", "_build", "_probe", "tmp", "temp", "_shots"})
SCRATCH_INTENTS = frozenset(part.lstrip("._") for part in SCRATCH_DIRS)
# Measured from 869 committed PBIR visual files (examples/ and migrations/): page directory
# identifiers are at most 20 UTF-16 units and visual identifiers at most 26. The engine's page-ID
# generator has a documented 24-unit upper bound, so that bound wins over the smaller corpus
# measurement. The two-unit margin is deliberately applied to the visual identifier so the
# envelope remains conservative for a future engine identifier.
_PBIR_MAX_PAGE_ID_UTF16 = 24
_PBIR_MAX_VISUAL_ID_UTF16 = 26
_PBIR_IDENTIFIER_SAFETY_MARGIN = 2
_PBIR_PAGE_ID = "p" * _PBIR_MAX_PAGE_ID_UTF16
_PBIR_VISUAL_ID = "v" * (_PBIR_MAX_VISUAL_ID_UTF16 + _PBIR_IDENTIFIER_SAFETY_MARGIN)
_PBIR_VISUAL_FILE = "visual" + ".json"
_PBIR_VISUAL_TAIL = f"definition/pages/{_PBIR_PAGE_ID}/visuals/{_PBIR_VISUAL_ID}/{_PBIR_VISUAL_FILE}"


_ENGINE_SOURCE_SUFFIXES = {".twb": ".twb", ".twbx": ".twb", ".tds": ".tds", ".tdsx": ".tds"}


def _readable_source(path: Path) -> bool:
    """Prove a source and its required Tableau document can be opened before conversion."""
    try:
        if path.suffix.lower() in {".twb", ".tds"}:
            path.read_bytes().decode("utf-8-sig")
            return True
        required = _ENGINE_SOURCE_SUFFIXES[path.suffix.lower()]
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir() and Path(info.filename).suffix.lower() == required:
                    with archive.open(info) as document:
                        document.read().decode("utf-8-sig")
                    return True
            return False
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return False


def _input_candidates(input_dir: Path) -> list[Path] | None:
    if input_dir.is_file():
        candidates = [input_dir]
    elif input_dir.is_dir():
        candidates = sorted(
            path
            for path in input_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".twb", ".twbx", ".tds", ".tdsx"}
        )
    else:
        return None
    if not candidates or any(not _readable_source(path) for path in candidates):
        return None
    return candidates


def _engine_unit_names(engine: Path, input_dir: Path) -> list[str] | None:
    """Ask the selected engine for its real datasource-then-workbook folder allocation."""
    scripts_dir = engine / "skills" / "tableau-migration" / "scripts"
    if not (scripts_dir / "migrate_estate.py").is_file():
        return None
    adapter = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from migrate_estate import LocalFilesSource, _safe_folder\n"
        "source = LocalFilesSource(Path(sys.argv[2]))\n"
        "used = set()\n"
        "names = []\n"
        "for asset_id in source.list_datasources():\n"
        "    names.append(_safe_folder(source.asset_name(asset_id), used))\n"
        "for asset_id in source.list_workbooks():\n"
        "    names.append(_safe_folder(source.asset_name(asset_id), used))\n"
        "print(json.dumps(names, ensure_ascii=False))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", adapter, str(scripts_dir), str(input_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        names = json.loads(result.stdout)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or not name for name in names)
        or len({name.casefold() for name in names}) != len(names)
    ):
        return None
    return names


def project_estate_path_ceiling(output_root: Path, unit_names: list[str] | None) -> dict:
    """Project the canonical PBIP visual path before the engine writes any output.

    The fixed page/visual identifiers define the minimum canonical PBIR safety envelope; the estate's
    longest source name and actual output root are the variable inputs available at this stage.
    """
    output_root = output_root.resolve()
    if not unit_names:
        return {
            "status": "cannot_establish",
            "reason": "no unit/workbook name was available before conversion",
            "output_root": str(output_root),
        }
    records = []
    projected_names = list(unit_names)
    if any(not isinstance(name, str) or not name for name in projected_names) or len(
        {name.casefold() for name in projected_names}
    ) != len(projected_names):
        return {
            "status": "cannot_establish",
            "reason": "the selected engine returned an invalid unit name",
            "output_root": str(output_root),
        }
    for unit in projected_names:
        report = f"{unit}.Report"
        report_root = output_root / "pbip" / unit / report
        directory = report_root / _PBIR_VISUAL_TAIL.rsplit("/", 1)[0]
        file_path = report_root / _PBIR_VISUAL_TAIL
        records.extend(
            (
                {
                    "kind": "directory",
                    "path": str(directory),
                    "length": utf16_len(str(directory)),
                    "ceiling": DIR_CEILING,
                },
                {
                    "kind": "file",
                    "path": str(file_path),
                    "length": utf16_len(str(file_path)),
                    "ceiling": FILE_CEILING,
                },
            )
        )
    offenders = [record for record in records if record["length"] > record["ceiling"]]
    return {
        "status": "over_ceiling" if offenders else "ok",
        "output_root": str(output_root),
        "longest_unit": max(projected_names, key=utf16_len),
        "projected_units": projected_names,
        "paths": records,
        "offenders": offenders,
    }


#: The actionable escape route for a projected path-ceiling refusal - issue #479's second reopen
#: measured that the earlier "use a shorter run/output root" text named no command an operator could
#: actually run, while the only documented allocator (`work_dirs.allocate_run`) *always* composes
#: `<repo>/_runs/<NNN>-<slug>/...`, i.e. the very path that just failed. `work_dirs.py --runs-parent`
#: allocates the identical canonical `_runs/<NNN>-<slug>/{...}` tree - same manifest, same `--verify`
#: contract - rooted under an external short parent instead, so the printed hint names it directly.
_SHORT_ROOT_HINT = (
    "Allocate a run under a short EXTERNAL parent instead, e.g. "
    "`python scripts/work_dirs.py <unit> --runs-parent C:\\short\\path --json` "
    "(same canonical _runs/<NNN>-<slug>/ tree, just rooted somewhere shorter), then point --output "
    "at that run's bundle/ and retry."
)


def preflight_estate_path_ceiling(input_dir: Path, output_root: Path, engine: Path | None = None) -> tuple[bool, str]:
    """Refuse an estate whose canonical downstream PBIP skeleton exceeds Desktop's ceilings."""
    try:
        candidates = _input_candidates(input_dir)
        names = _engine_unit_names(engine, input_dir) if engine and candidates else None
        projection = project_estate_path_ceiling(output_root, names)
    except (OSError, RuntimeError, UnicodeEncodeError, ValueError) as exc:
        return False, (f"CANNOT ASSESS downstream PBIP path length ({type(exc).__name__}: {exc}). {_SHORT_ROOT_HINT}")
    if projection["status"] == "cannot_establish":
        return False, (
            "CANNOT ASSESS downstream PBIP path length: the input estate has no usable unit/workbook "
            f"name. {_SHORT_ROOT_HINT}"
        )
    if projection["status"] == "over_ceiling":
        worst = max(projection["offenders"], key=lambda record: record["length"] - record["ceiling"])
        return False, (
            f"PATH CEILING: projected {worst['kind']} is {worst['length']} UTF-16 units "
            f"(ceiling {worst['ceiling']}) for unit {projection['longest_unit']!r}. "
            "LongPathsEnabled and \\\\?\\ prefixes do not make Power BI Desktop accept these paths. "
            f"{_SHORT_ROOT_HINT}"
        )
    return True, (
        f"PATH CEILING: projected canonical PBIP visual path fits ({projection['longest_unit']!r}); "
        "this is the pre-conversion safety envelope."
    )


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
    written = write_generated_artifact_manifest(bundle, report, earliest_mtime=None, coverage=SLICE_ONLY_COVERAGE)
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


class ProvenanceStampResult(NamedTuple):
    """Publication and verdict for one structured provenance result."""

    ok: bool
    status: str
    detail: str


def write_source_provenance(out_dir: Path, result: dict) -> Path | None:
    """Atomically publish one strict-JSON provenance result without harming a prior artifact."""
    final_path = out_dir / SOURCE_PROVENANCE_REPORT
    staging_path = final_path.with_name(f"{final_path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    swapped = False
    try:
        payload = json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        with open(staging_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging_path, final_path)
        swapped = True
    except (OSError, TypeError, ValueError) as exc:
        log.warning("PROVENANCE: artifact not published (%s)", _operation_failure(PUBLISH_PROVENANCE_OPERATION, exc))
        return None
    finally:
        if not swapped:
            try:
                staging_path.unlink()
            except OSError:  # pragma: no cover - best effort; never touch the prior final artifact
                log.warning("PROVENANCE: staging file left behind: %s", staging_path.name)
    return final_path


def _progress_payload(event: str, operation: str, completed: int, total: int | None) -> dict:
    """The four fields every progress event has, coerced onto the allowlist.

    Nothing derived from an input, a response or an exception can reach a progress line even by
    mistake: an unrecognised event or operation is REPLACED by its safe constant rather than echoed,
    because a progress line is exactly the thing that gets pasted into an issue.

    Deliberately absent: elapsed time, host, site, project, workbook or datasource name, LUID, path,
    owner, credential, exception message and response body. The checkpoint and result payloads the
    worker sends are never rendered at any level, including debug.
    """
    return {
        "event": event if event in PROVENANCE_PROGRESS_EVENTS else "operation-progress",
        "operation": operation if operation in PROVENANCE_PROGRESS_OPERATIONS else PROVENANCE_PHASE_OPERATION,
        "completed": max(int(completed), 0) if isinstance(completed, int) and not isinstance(completed, bool) else 0,
        "total": max(int(total), 0) if isinstance(total, int) and not isinstance(total, bool) else None,
    }


def _print_progress(payload: dict) -> dict:
    print(f"{PROVENANCE_PROGRESS_PREFIX} {json.dumps(payload)}")
    return payload


def emit_provenance_progress(
    event: str,
    operation: str,
    completed: int = 0,
    total: int | None = None,
    *,
    status: str | None = None,
) -> dict:
    """Print one machine-readable progress event, from allowlisted parts only."""
    payload = _progress_payload(event, operation, completed, total)
    if status is not None:
        payload["status"] = status if status in PROVENANCE_PROGRESS_STATUSES else "unknown"
    return _print_progress(payload)


def emit_provenance_phase_start(timeout_sec: float) -> dict:
    """The one event that announces the budget - and the only one allowed to carry it."""
    payload = _progress_payload("phase-start", PROVENANCE_PHASE_OPERATION, 0, None)
    if isinstance(timeout_sec, (int, float)) and math.isfinite(timeout_sec):
        payload["timeout_sec"] = float(timeout_sec)
    return _print_progress(payload)


class ProvenanceProtocolError(Exception):
    """The worker sent something outside the closed message protocol. Fail closed, never parse on."""


# Exact JSON builtin types are deliberate: bool is not a count, nor is an arbitrary coercible object.
# pylint: disable=unidiomatic-typecheck
def _is_count(value: object) -> bool:
    """A bounded whole count; bool, floats, negative and arbitrarily large integers are not counts."""
    return type(value) is int and 0 <= value <= PROVENANCE_MAX_COUNT


def _require(condition: bool) -> None:
    if not condition:
        raise ProvenanceProtocolError


def _enum(value: object, allowed) -> None:
    _require(type(value) is str and value in allowed)


def _text(value: object, maximum: int = 1024) -> None:
    _require(type(value) is str and len(value) <= maximum and all(ord(char) >= 32 for char in value))


def _digest(value: object, length: int = 64) -> None:
    _require(type(value) is str and len(value) == length and re.fullmatch("[0-9a-f]+", value) is not None)


def _validated_error(error: object) -> None:
    _require(type(error) is dict and {"code", "operation"} <= error.keys() <= _ERROR_KEYS)
    _enum(error["code"], _WORKER_ERROR_CODES)
    _enum(error["operation"], _ERROR_OPERATIONS)
    if "exception_class" in error:
        _enum(error["exception_class"], prov.ERROR_CLASSES)
    for key in ("errno", "winerror"):
        if key in error:
            _require(type(error[key]) is int and -(1 << 31) <= error[key] < (1 << 32))
    if "http_status" in error:
        _require(_is_count(error["http_status"]) and (error["http_status"] == 0 or 100 <= error["http_status"] <= 599))


def _validated_revision(revision: object) -> None:
    _require(type(revision) is dict and revision.keys() == _CHECKPOINT_REVISION_KEYS)
    _enum(revision["algo"], _REVISION_ALGORITHMS)
    _digest(revision["value"])


def _validated_checkpoint(record: object) -> dict:
    """Strict, bounded DERIVED evidence. In particular, a string in a digest field is not a digest."""
    _require(type(record) is dict and {"input"} <= record.keys() <= _CHECKPOINT_KEYS)
    local = record.get("input")
    _require(type(local) is dict and local.keys() <= _CHECKPOINT_INPUT_KEYS)
    if "status" in local:
        _require(local == {"status": "unavailable"} and "fingerprint_error" in record)
    else:
        _require({"size_bytes", "sha256"} <= local.keys() and _is_count(local["size_bytes"]))
        _digest(local["sha256"])
    if "members" in local:
        members = local["members"]
        _require(type(members) is list and len(members) <= PROVENANCE_MAX_MEMBERS)
        for member in members:
            _require(type(member) is dict and member.keys() == _CHECKPOINT_MEMBER_KEYS)
            _require(_is_count(member["size_bytes"]))
            _digest(member["crc32"], 8)
    if "revision_key" in local:
        _validated_revision(local["revision_key"])
    if "fingerprint_error" in record:
        _validated_error(record["fingerprint_error"])
        _require(record["fingerprint_error"]["operation"] == prov.OP_FINGERPRINT)
    return record


def _validated_origin(origin: object) -> None:
    if origin is None:
        return
    _require(type(origin) is dict and origin.keys() == _ORIGIN_KEYS)
    for key in _ORIGIN_TEXT_KEYS:
        if origin[key] is not None:
            _text(origin[key])
    _enum(origin["matched_by"], {"luid", "name", "sanitized_name"})
    _enum(origin["match"], {"sha256", "name_only", "unavailable"})
    _require(origin["revision_match"] is None or origin["revision_match"] in ("same", "differs"))
    if origin["remote_sha256"] is not None:
        _digest(origin["remote_sha256"])
    if origin["remote_revision_key"] is not None:
        _validated_revision(origin["remote_revision_key"])
    reason = origin["content_unavailable"]
    _require(reason is None or (type(reason) is str and re.fullmatch(r"HTTP [1-5][0-9]{2}", reason) is not None))
    _require(_is_count(origin["same_name_count"]))


def _validated_result_record(record: object) -> dict:
    """Validate a scrubbed record, returning its bounded derived projection for reconciliation."""
    allowed = {"input", "origin", "origin_note", "fingerprint_error", "lookup_error"}
    _require(type(record) is dict and {"input"} <= record.keys() <= allowed)
    local = record["input"]
    _require(type(local) is dict and local.keys() <= _CHECKPOINT_INPUT_KEYS | {"file"})
    if "file" in local:
        _text(local["file"], 255)
        _require(bool(local["file"]) and not any(char in local["file"] for char in "/\\:"))
    if "members" in local:
        _require(type(local["members"]) is list and len(local["members"]) <= PROVENANCE_MAX_MEMBERS)
        for member in local["members"]:
            _require(
                type(member) is dict and _CHECKPOINT_MEMBER_KEYS <= member.keys() <= _CHECKPOINT_MEMBER_KEYS | {"name"}
            )
            if "name" in member:
                _text(member["name"])
    reduced = prov.checkpoint_record(record)
    _validated_checkpoint(reduced)
    if "origin" in record:
        _validated_origin(record["origin"])
    if "origin_note" in record:
        _text(record["origin_note"])
        origin = record.get("origin")
        notes = {prov.WITHHELD_NOTE, "no workbook of this LUID or name on the site - local-only input"}
        if origin:
            notes.add(
                f"matched by {origin['matched_by']}, but the bytes DIFFER from the site copy - "
                "figures measured here will not reproduce against it"
            )
            reason = origin["content_unavailable"] or "the site refused the download"
            notes.add(
                f"matched by {origin['matched_by']}, but the site copy could NOT be read "
                f"({reason}) - no byte or revision comparison was made"
            )
        _require(record["origin_note"] in notes)
    if "lookup_error" in record:
        _validated_error(record["lookup_error"])
    return reduced


def _validated_result(result: object, total: int, checkpoints: dict[int, dict]) -> dict:
    _require(type(result) is dict and result.keys() == {"schema", "stamped_at", "input_count", "inputs", "phase"})
    _require(result["schema"] == prov.SCHEMA)
    stamp = result["stamped_at"]
    _require(type(stamp) is str and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp) is not None)
    _require(_is_count(result["input_count"]) and result["input_count"] == total)
    records = result["inputs"]
    _require(type(records) is list and len(records) == total)
    for index, record in enumerate(records):
        reduced = _validated_result_record(record)
        if index in checkpoints:
            _require(reduced == checkpoints[index])
        else:
            _require(reduced["input"] == {"status": "unavailable"})
    phase = result["phase"]
    _require(type(phase) is dict and phase.keys() == {"status", "errors"})
    _enum(phase["status"], prov.RESULT_STATUSES)
    errors = phase["errors"]
    _require(type(errors) is list and len(errors) <= 2 * PROVENANCE_MAX_INPUTS + 8)
    for error in errors:
        _validated_error(error)
    if phase["status"] in prov.SUCCESS_STATUSES:
        _require(total > 0 and not errors and len(checkpoints) == total)
        _require(all("status" not in record["input"] for record in records))
    if phase["status"] == "empty":
        _require(total == 0)
    return result


def _validated_message(message: object) -> dict:
    """One message against the closed protocol. Anything else raises rather than being interpreted."""
    _require(type(message) is dict and len(message) <= 4)
    kind, keys = message.get("kind"), set(message)
    if kind == prov.MSG_INPUTS_DISCOVERED:
        _require(keys == {"kind", "total"} and _is_count(message["total"]))
        _require(message["total"] <= PROVENANCE_MAX_INPUTS)
    elif kind == prov.MSG_OPERATION:
        _require(keys == {"kind", "operation", "completed", "total"})
        _enum(message["operation"], prov.WORKER_OPERATIONS)
        _require(_is_count(message["completed"]))
        _require(message["total"] is None or _is_count(message["total"]))
    elif kind == prov.MSG_CHECKPOINT:
        _require(keys == {"kind", "index", "record"} and _is_count(message["index"]))
        _validated_checkpoint(message["record"])
    elif kind in (prov.MSG_SAFE_SNAPSHOT, prov.MSG_TERMINAL):
        _require(keys == {"kind", "result"} and type(message["result"]) is dict)
    else:
        raise ProvenanceProtocolError
    return message


class _ProvenanceState:  # pylint: disable=too-many-instance-attributes
    """Everything the parent ACCEPTED before it stopped reading, and nothing it did not.

    The accept boundary is the whole safety property: a message read after the deadline latch is not
    recorded here, so a worker that finishes late cannot make a preempted phase look successful.
    """

    def __init__(self, emit=emit_provenance_progress) -> None:
        self._emit = emit
        self.total: int | None = None
        self.checkpoints: dict[int, dict] = {}
        self.snapshot: dict | None = None
        self.terminal: dict | None = None
        #: What the worker was last known to be DOING - the operation a deadline error names.
        self.operation: str = prov.OP_COLLECT_INPUTS
        self.counters: dict[str, int] = {}
        self.messages = 0

    def prepare(self, message: object) -> _ProvenanceState:
        """Validate into TEMPORARY state. Neither evidence nor progress is committed by this call."""
        message = _validated_message(message)
        _require(self.terminal is None and self.messages < PROVENANCE_MAX_MESSAGES)
        candidate = copy.copy(self)
        candidate.messages += 1
        kind = message["kind"]
        if kind == prov.MSG_INPUTS_DISCOVERED:
            _require(self.total is None and self.operation == prov.OP_COLLECT_INPUTS)
            candidate.total = message["total"]
        elif kind == prov.MSG_OPERATION:
            candidate.prepare_operation(message)
        elif kind == prov.MSG_CHECKPOINT:
            _require(self.total is not None and message["index"] == len(self.checkpoints) < self.total)
            _require(
                self.operation == prov.OP_FINGERPRINT and self.counters.get(prov.OP_FINGERPRINT) == message["index"] + 1
            )
            candidate.checkpoints = {**self.checkpoints, message["index"]: message["record"]}
        elif kind == prov.MSG_SAFE_SNAPSHOT:
            _require(self.total is not None and self.snapshot is None)
            _require(self.operation == prov.OP_SCRUB and self.counters.get(prov.OP_SCRUB) == 1)
            candidate.snapshot = _validated_result(message["result"], self.total, self.checkpoints)
        else:
            _require(self.total is not None)
            candidate.terminal = _validated_result(message["result"], self.total, self.checkpoints)
            if self.snapshot is not None:
                _require(candidate.terminal["inputs"] == self.snapshot["inputs"])
                prior = self.snapshot["phase"]
                _require(candidate.terminal["phase"]["errors"][: len(prior["errors"])] == prior["errors"])
                _require(
                    prior["status"] not in {"partial", "failed"}
                    or candidate.terminal["phase"]["status"] == prior["status"]
                )
        return candidate

    def prepare_operation(self, message: dict) -> None:
        """Check operation order and counters on the temporary candidate, not the accepted state."""
        operation, completed, total = message["operation"], message["completed"], message["total"]
        _require(_OPERATION_ORDER[operation] >= _OPERATION_ORDER[self.operation])
        _require(completed >= self.counters.get(operation, 0))
        if operation == prov.OP_COLLECT_INPUTS:
            _require(total == 1 and completed in (0, 1))
            _require(completed == 0 or self.total is not None)
            _require(operation not in self.counters or completed > self.counters[operation])
        else:
            _require(self.total is not None)
            if operation == prov.OP_FINGERPRINT:
                _require(
                    total == self.total and len(self.checkpoints) <= completed <= min(len(self.checkpoints) + 1, total)
                )
            else:
                _require(len(self.checkpoints) == self.total)
                if operation == prov.OP_CONTENT:
                    _require(total is None and completed <= self.total)
                    _require(self.counters.get(prov.OP_INVENTORY) == 1)
                else:
                    _require(total == 1 and completed in (0, 1))
                    _require(operation not in self.counters or completed > self.counters[operation])
                if operation == prov.OP_INVENTORY:
                    _require(self.counters.get(prov.OP_SIGN_IN) == 1)
                if operation == prov.OP_SIGN_OUT:
                    _require(self.snapshot is not None)
        self.operation = operation
        self.counters = {**self.counters, operation: completed}

    def commit(self, candidate: _ProvenanceState) -> None:
        """An O(1) state swap, performed ONLY by the supervising thread after its clock check."""
        previous = self.counters
        self.__dict__ = candidate.__dict__
        if self.counters is not previous:
            total = (
                self.total
                if self.operation == prov.OP_FINGERPRINT
                else (None if self.operation == prov.OP_CONTENT else 1)
            )
            self._emit("operation-progress", self.operation, self.counters[self.operation], total)

    def accept(self, message: object) -> None:
        """Synchronous protocol seam for direct tests; supervision uses prepare then clock then commit."""
        self.commit(self.prepare(message))

    def document(self, code: str, base: dict | None = None, **facts: int) -> dict:
        """The honest partial/failed document for a phase that did not finish.

        Everything ACCEPTED is kept and everything else is an EXPLICIT placeholder, so `input_count`
        still equals `len(inputs)` (the identity #594's normalisation refuses to let a result
        contradict) and an unfinished input reads as "we did not get to this one" rather than as
        absent or as fine. A placeholder names no file: it is addressed by ordinal, which is also why
        it cannot leak one.

        ``base`` overrides the accepted safe snapshot as the document to build on. It exists for the
        one case where a COMPLETE result was accepted and the phase still may not claim success -
        an unreapable worker - so the evidence survives while the verdict does not.
        """
        error = prov.phase_error(code, self.operation, **facts)
        source = base if base is not None else (self.terminal or self.snapshot)
        if source is not None:
            result = dict(source)
            phase = result.get("phase") if isinstance(result.get("phase"), dict) else {}
            result["phase"] = {"status": "partial", "errors": [*(phase.get("errors") or []), error]}
            return result
        if self.total is None and not self.checkpoints:
            return prov.phase_result([], "failed", [error])
        records = [self.checkpoints.get(index) or prov.unavailable_input(code) for index in range(self.total or 0)]
        return prov.phase_result(records, "partial" if self.checkpoints else "failed", [error])


class ProvenanceOutcome(NamedTuple):
    """One supervised provenance phase: the result to publish, and what the worker did."""

    result: dict
    completed: int
    total: int | None
    worker_pid: int | None
    worker_alive: bool | None
    worker_exitcode: int | None
    expired: bool


class _WorkerStop(NamedTuple):
    alive: bool | None
    exitcode: int | None
    killed: bool
    error: bool = False

    @property
    def reaped(self) -> bool:
        """Only affirmative, exception-free evidence can certify cleanup."""
        return self.alive is False and type(self.exitcode) is int and not self.error


def _stop_worker(process) -> _WorkerStop:
    """Terminate, then kill, then reap - each with a BOUNDED join.

    ⚠️ This is a DIRECT-process guarantee only. Measured on Windows: killing a worker did not kill
    its grandchild, which survived and had to be terminated by PID. The worker is therefore required
    to be a leaf (it is started as a daemon, which `multiprocessing` refuses to let have children),
    and no claim is made here about descendants.
    """
    failed = False
    killed = False
    alive, exitcode = None, None
    for action, budget in (("terminate", PROVENANCE_TERMINATE_JOIN_SEC), ("kill", PROVENANCE_KILL_JOIN_SEC)):
        if action == "kill" and alive is False and type(exitcode) is int and not failed:
            break
        killed = killed or action == "kill"
        try:
            getattr(process, action)()
        except Exception:  # pylint: disable=broad-exception-caught
            failed = True
        try:
            process.join(budget)
        except Exception:  # pylint: disable=broad-exception-caught
            failed = True
        try:
            alive, exitcode = process.is_alive(), process.exitcode
            if type(alive) is not bool or (exitcode is not None and type(exitcode) is not int):
                failed = True
                alive, exitcode = None, None
        except Exception:  # pylint: disable=broad-exception-caught
            failed = True
            alive, exitcode = None, None
    stopped = _WorkerStop(alive, exitcode, killed, failed)
    if not stopped.reaped:
        # CPython otherwise joins active children WITHOUT A TIMEOUT in its atexit handler. A
        # cannot-reap verdict must not turn back into an unbounded interpreter exit. This does not
        # assert that an unreapable child disappeared; the artifact explicitly reports the gap.
        multiprocessing.process._children.discard(process)  # pylint: disable=protected-access
    return stopped


def _json_integer(text: str) -> int:
    _require(len(text) <= 20)
    return int(text)


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    _require(len(pairs) <= 32)
    result = {}
    for key, value in pairs:
        _require(len(key) <= 64 and key not in result)
        result[key] = value
    return result


def _reject_json_number(_text: str) -> None:
    raise ProvenanceProtocolError


def _decode_message(payload: bytes) -> dict:
    """Strict JSON only; no pickle execution, duplicate keys, floating counts or nonfinite values."""
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_json_object,
        parse_int=_json_integer,
        parse_float=_reject_json_number,
        parse_constant=_reject_json_number,
    )


class _ProvenanceReceiver:
    """One daemon transport helper, never a second provenance worker or an artifact publisher.

    Receive, decode and validation run here, including hostile partial frames. The one-slot mailbox
    and acknowledgement bound buffering; only the supervisor commits candidates. Shutdown interrupts
    socket reads, and the bounded join is not extended for a stuck validator. A remaining helper is
    daemon-only and cannot hold interpreter exit or publish late evidence.
    """

    def __init__(self, channel: prov.ProvenanceChannel, state: _ProvenanceState) -> None:
        self.channel = channel
        self.state = state
        self.mailbox: queue.Queue = queue.Queue(maxsize=1)
        self.acknowledged = threading.Event()  # parent threads only; NEVER shared with the worker
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="provenance-transport", daemon=True)

    def start(self) -> None:
        """Start only the daemon transport helper; all provenance computation stays in the worker."""
        self.thread.start()

    def _deliver(self, value: object) -> bool:
        while not self.closed:
            try:
                self.mailbox.put(value, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        received = 0
        try:
            while not self.closed:
                payload = self.channel.recv_bytes(
                    min(PROVENANCE_MAX_FRAME_BYTES, PROVENANCE_MAX_PHASE_BYTES - received)
                )
                received += len(payload)
                candidate = self.state.prepare(_decode_message(payload))
                if not self._deliver(candidate):
                    return
                while not self.closed and not self.acknowledged.wait(0.05):
                    pass
                self.acknowledged.clear()
        except EOFError:
            self._deliver("eof")
        except Exception:  # pylint: disable=broad-exception-caught
            self._deliver(PROVENANCE_PROTOCOL_CODE)

    def close(self) -> None:
        """No peer-owned lock or unbounded thread join is used by the supervising parent."""
        self.closed = True
        self.acknowledged.set()
        with contextlib.suppress(OSError, ValueError):
            self.channel.shutdown()
        if self.thread.ident is not None:
            self.thread.join(PROVENANCE_RECEIVER_JOIN_SEC)
        self.channel.close()


def _drain_worker(receiver: _ProvenanceReceiver, deadline_at: float, state: _ProvenanceState) -> str | None:
    """Only a timed mailbox read and an O(1) commit run on the supervising thread."""
    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            return PROVENANCE_DEADLINE_CODE
        try:
            candidate = receiver.mailbox.get(timeout=remaining)
        except queue.Empty:
            return PROVENANCE_DEADLINE_CODE
        # Validation has finished into temporary state. It may have used the rest of the budget:
        # check AGAIN, immediately before committing any evidence or progress.
        if time.monotonic() >= deadline_at:
            return PROVENANCE_DEADLINE_CODE
        if candidate == "eof":
            return None if state.terminal is not None else PROVENANCE_CRASH_CODE
        if candidate == PROVENANCE_PROTOCOL_CODE:
            return PROVENANCE_PROTOCOL_CODE
        state.commit(candidate)
        receiver.acknowledged.set()


def _worker_document(state: _ProvenanceState, code: str | None, stopped: _WorkerStop) -> dict:
    """The result to publish: the worker's own when it finished, otherwise the honest partial one.

    The exit code is recorded only for a worker that died on its OWN - our terminate/kill code
    describes this parent's action and would say nothing about the phase.

    An UNREAPABLE worker is the one case where a complete terminal result was accepted and still may
    not be published as a pass. It is kept as the document - the evidence is real and was accepted
    before the latch - but the status is forced to `partial` beside a `worker-reap-failed` error,
    because a process we could not account for cannot certify anything.
    """
    if code is None:
        result = state.terminal or {}
    elif code == PROVENANCE_CRASH_CODE and type(stopped.exitcode) is int:
        result = state.document(code, exit_code=stopped.exitcode)
    else:
        result = state.document(code)
    if not stopped.reaped:
        if code is None:
            return state.document(PROVENANCE_REAP_CODE, base=state.terminal)
        result["phase"]["errors"].append(prov.phase_error(PROVENANCE_REAP_CODE, state.operation))
    return result


# pylint: enable=unidiomatic-typecheck


# Keep the process and both endpoint lifetimes explicit through setup failure and bounded cleanup.
# pylint: disable-next=too-many-locals
def collect_provenance(
    input_dir: Path,
    timeout_sec: float = PROVENANCE_TIMEOUT_DEFAULT_SEC,
    entry=None,
    env_path: Path | None = None,
) -> ProvenanceOutcome:
    """Run the whole provenance computation in one leaf worker under one absolute deadline.

    The deadline is computed BEFORE the spawn, so Windows' import-the-world startup is charged to the
    phase rather than granted for free. The worker resolves its own credentials from `env_path`, so
    no secret crosses the pipe; the parent sends it a path and receives numbers, derived digests and
    an already-scrubbed result.

    The parent publishes. Always, exactly once, whatever happened here - which is why publication is
    NOT in this function.
    """
    deadline_at = time.monotonic() + timeout_sec
    state = _ProvenanceState()
    process = receiver = recv = send = cancel = None
    worker_pid = None
    stopped = _WorkerStop(None, None, False, True)
    code = PROVENANCE_START_CODE
    try:
        context = multiprocessing.get_context("spawn")
        left, right = socket.socketpair()
        recv, send = prov.ProvenanceChannel(left), prov.ProvenanceChannel(right)
        # Single-writer shared byte: neither read nor write acquires a worker-owned lock. It is a
        # cooperative hint only; visibility of this byte is never the deadline enforcement.
        cancel = context.RawValue("b", 0)
        process = context.Process(
            target=entry or prov.provenance_worker,
            args=(send, cancel, {"input": str(input_dir), "env": str(env_path or Path(".env"))}),
            daemon=True,
        )
        # Reject an unpicklable injected entry BEFORE Windows creates a bootstrap child.
        multiprocessing.reduction.ForkingPickler.dumps(entry or prov.provenance_worker)
        if time.monotonic() >= deadline_at:
            code = PROVENANCE_DEADLINE_CODE
        else:
            process.start()
            worker_pid = process.pid
            send.close()
            receiver = _ProvenanceReceiver(recv, state)
            receiver.start()
            code = _drain_worker(receiver, deadline_at, state)
    except Exception:  # pylint: disable=broad-exception-caught
        code = PROVENANCE_START_CODE if worker_pid is None else PROVENANCE_PROTOCOL_CODE
    finally:
        if cancel is not None:
            cancel.value = 1
        if process is not None and process.pid is not None:
            stopped = _stop_worker(process)
            worker_pid = process.pid
            if stopped.reaped:
                with contextlib.suppress(OSError, ValueError):
                    process.close()
        if receiver is not None:
            receiver.close()
        elif recv is not None:
            recv.close()
        if send is not None:
            send.close()

    result = state.document(code) if worker_pid is None else _worker_document(state, code, stopped)
    completed = len(result["inputs"]) if isinstance(result.get("inputs"), list) else 0
    return ProvenanceOutcome(
        result=result,
        completed=completed,
        total=state.total if state.total is not None else completed,
        worker_pid=worker_pid,
        worker_alive=stopped.alive,
        worker_exitcode=stopped.exitcode,
        expired=code == PROVENANCE_DEADLINE_CODE,
    )


def stamp_inputs(
    input_dir: Path, out_dir: Path, timeout_sec: float = PROVENANCE_TIMEOUT_DEFAULT_SEC
) -> ProvenanceStampResult:
    """Record where each input workbook came from, into ``<out>/source-provenance.json``.

    Every structured result is published, including empty, partial, timed-out and failed ones. Remote
    lookup may honestly degrade to local-only evidence, but unassessable evidence or failed
    publication blocks later phases.

    A result is NORMALISED before it is published: a result that contradicts itself (a success or
    local_only status carrying no inputs, a count that is not a whole number, a count that disagrees
    with the list beside it) is published as a failure carrying the stable fault codes, so the
    artifact on disk is never success-shaped on evidence that cannot support it. The verdict is then
    taken from the same consistency check rather than from ``phase.status`` alone.

    The computation itself runs in a supervised leaf worker (#576). Publication stays HERE, in the
    parent, exactly once - a deadline that killed the worker is only useful if the artifact recording
    it still gets written.
    """
    emit_provenance_phase_start(timeout_sec)
    try:
        outcome = collect_provenance(input_dir, timeout_sec)
        result = prov.normalize_result(outcome.result)
    except Exception:  # pylint: disable=broad-exception-caught
        result = prov.phase_result(
            [], "failed", [prov.phase_error(PROVENANCE_PROTOCOL_CODE, PROVENANCE_PHASE_OPERATION)]
        )
        outcome = ProvenanceOutcome(result, 0, None, None, None, None, False)

    try:
        published = write_source_provenance(out_dir, result)
    except Exception:  # pylint: disable=broad-exception-caught
        published = None  # exactly one attempt even if the publisher itself unexpectedly raises
    status = result["phase"].get("status", "unknown")
    if published is None:
        stamped = ProvenanceStampResult(
            False,
            "publication_failed",
            f"{SAFE_SOURCE_PROVENANCE_REPORT} could not be published",
        )
    else:
        records = result.get("inputs") or []
        matched = sum(
            1
            for record in records
            if isinstance(record, dict)
            and isinstance(record.get("origin"), dict)
            and record["origin"].get("match") == "sha256"
        )
        count = result.get("input_count", 0)
        stamped = ProvenanceStampResult(
            prov.is_success(result),
            status,
            f"{count} input(s) stamped, {matched} confirmed against the site ({status}) "
            f"-> {SAFE_SOURCE_PROVENANCE_REPORT}",
        )
    emit_provenance_progress(
        "phase-finish",
        PROVENANCE_PHASE_OPERATION,
        outcome.completed,
        outcome.total,
        status=stamped.status,
    )
    return stamped


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
    """Baseline the engine's output: artifact hashes, the full output tree, then the receipt.

    The order is load-bearing and lives HERE rather than in ``main`` so it cannot be separated by an
    unrelated edit: both manifest writes UPSERT into ``input_manifest.json`` and the receipt HASHES
    that same file, so receipt-first leaves ``input_manifest_sha256`` stale and the credential gate
    then rejects the bundle the engine just produced - while the run still reports success.
    """
    log.info(
        "GENERATED_ARTIFACTS: hashes -> %s",
        write_generated_artifact_manifest(out_dir, report, phases[0]["started_wall"] - 1),
    )
    log.info("ENGINE_OUTPUT_TREE: hashes -> %s", write_engine_output_tree(out_dir))
    write_receipt_phase(out_dir, phases, engine)


# ---------------------------------------------------------------------------
# The post-engine path-ceiling gate (issue #564)
#
# The pre-engine projection above (`preflight_estate_path_ceiling`) is a PROJECTION: it composes the
# canonical PBIR visual tail onto unit names it can know BEFORE conversion. That is genuinely all it
# can see, and it is fail-open by construction - measured on the committed issue-194 repro, the path
# that actually breaches is a SEMANTIC-MODEL table file
# (`<unit>.SemanticModel/definition/tables/<uncapped table name>`), which no pre-conversion
# projection in this repo models. So an estate could pass the projection, emit a tree Power BI
# Desktop cannot open, and hand it to packaging, agents and Desktop with every signal green.
#
# This gate answers the different question - "what did the engine ACTUALLY write?" - by measuring the
# emitted tree with the SAME authority every other consumer uses (`check_path_ceiling.scan`, walker
# and ceilings included; nothing here re-implements either). Python can write these paths, so the
# tree exists; what must never happen is downstream work STARTING from it.
#
# Three rules, all deliberate:
#   * it runs AFTER the engine's output is recorded (receipt + baselines) and BEFORE provenance,
#     handover slices, packaging, agents and Desktop - so the refusal is early for consumers and
#     late enough that the bundle still explains what built it;
#   * where it cannot assess, it BLOCKS - an unreadable directory, an unmeasurable name, a walker
#     failure or a tree with nothing in it is an indeterminate state, never a pass. That is the same
#     rule the destructive-re-run barrier follows, for the same reason;
#   * it NEVER deletes, shortens or rewrites the output. Permanent filename shortening belongs
#     upstream in the engine; here the emitted tree is preserved as evidence for that report.
# ---------------------------------------------------------------------------


def _ascii_path(value: str) -> str:
    """A console-safe rendering of a path that may carry astral or undecodable characters.

    Output-only. The measurement itself is UTF-16 and belongs to `check_path_ceiling`; this exists
    because a CP1252 console raises `UnicodeEncodeError` on the very paths a refusal has to name,
    and a gate that crashes while printing its verdict has no verdict.
    """
    return value.encode("ascii", "backslashreplace").decode("ascii")


def _exception_facts(exc: BaseException) -> dict:
    """The only things this module reports about an exception: class name and numeric codes.

    Read STRUCTURALLY off the object - never `str(exc)`, never a parse of it. A message belongs to
    whoever raised it and routinely embeds the path it failed on; proving one carries no path means
    reasoning about prose, which is precisely what the prefix-collision finding showed cannot be
    done safely (a root `…\\bundle` rewrote a sibling `…\\bundle-foreign` into `<bundle>-foreign`).
    A class name and an errno are runtime facts with no location in them, and they are enough to act
    on: `PermissionError errno=13` says what to check.
    """
    facts: dict = {"class": type(exc).__name__}
    for attribute in ("errno", "winerror"):
        code = getattr(exc, attribute, None)
        if isinstance(code, int):
            facts[attribute] = code
    return facts


def _operation_failure(operation: str, exc: BaseException) -> str:
    """One log line for a failure this module caught itself: allowlisted label, class, codes."""
    label = operation if operation in _ALLOWED_OPERATIONS else "unlabelled-operation"
    facts = _exception_facts(exc)
    return " ".join([f"operation={label}"] + [f"{key}={value}" for key, value in facts.items()])


def _bundle_relative(value: object, root: Path, unplaced: list[str]) -> str:
    """One measured path as `<bundle>/<tail>`, or an ordinal when containment cannot be PROVEN.

    Purely lexical, and deliberately so: `Path.resolve()` on a UNC literal naming a host that does
    not exist blocks on SMB name resolution (measured in `manifest_scope._inside_any`), and a path
    that cannot be placed is unassessable regardless of what the filesystem would say.
    """
    text = value if isinstance(value, str) else str(value)
    try:
        candidate = Path(os.path.normpath(text))
        if candidate.is_relative_to(root):
            tail = candidate.relative_to(root).as_posix()
            return SAFE_BUNDLE_ROOT if tail in {"", "."} else f"{SAFE_BUNDLE_ROOT}/{tail}"
    except (OSError, ValueError):
        pass
    unplaced.append(text)
    return UNASSESSABLE_PATH.format(index=len(unplaced))


def shareable_path_report(report: dict, out_dir: Path) -> dict:
    """The measurement as it may leave this machine: same numbers, no host location.

    Two layers, and each closes what the other cannot:

    * every field `scan()` fills with a measured path is rewritten bundle-relative, so the refusal
      still names the offending tail (which IS the actionable part, and what an upstream report
      needs) while the run root never appears;
    * the whole document then goes through `manifest_scope.redact_host_paths`, the repo's
      value-shaped shipping redactor, so a string this function does not know about - an OS error
      message, an unknown-path reason, a future field - cannot carry a location past it either.

    The measurement itself is untouched: `check_path_ceiling.scan` still measures absolute paths,
    because absolute length is exactly what Power BI Desktop counts.
    """
    root = Path(os.path.normpath(str(out_dir)))
    unplaced: list[str] = []
    shareable = dict(report)
    shareable["root"] = SAFE_BUNDLE_ROOT
    for key in _PATH_RECORD_KEYS:
        record = shareable.get(key)
        if isinstance(record, dict):
            shareable[key] = dict(record, path=_bundle_relative(record.get("path"), root, unplaced))
    for key in _PATH_LIST_KEYS:
        rows = shareable.get(key)
        if isinstance(rows, list):
            shareable[key] = [
                dict(row, path=_bundle_relative(row.get("path"), root, unplaced)) if isinstance(row, dict) else row
                for row in rows
            ]
    # The walker's own `reason` is free-form text written by whatever raised it, so it is DROPPED
    # rather than sanitized: what survives is the ordinal that identifies the row.
    shareable["unknown_paths"] = [
        {"path": row.get("path"), "code": UNKNOWN_PATH_CODE.format(index=index)}
        if isinstance(row, dict)
        else {"code": UNKNOWN_PATH_CODE.format(index=index)}
        for index, row in enumerate(shareable.get("unknown_paths") or [], start=1)
    ]
    shareable["paths_not_placed"] = len(unplaced)
    cleaned, redacted = redact_host_paths(shareable, prefix=PATH_CEILING_REPORT)
    cleaned["redacted_fields"] = sorted(redacted)
    return cleaned


def scan_emitted_path_ceiling(out_dir: Path, limits: Limits | None = None) -> dict:
    """Measure the tree the engine ACTUALLY emitted. A failed measurement is never a clean one."""
    try:
        return scan_path_ceiling(out_dir, limits or PATH_CEILING_LIMITS)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        # `collect` already routes per-entry failures into `unknown_paths`; this is the walk itself
        # failing outright. Reported as a CODE plus the exception's structural facts - never its
        # message, which is written by whoever raised it and routinely quotes the path it failed on.
        return {
            "version": 1,
            "root": str(out_dir),
            "status": STATUS_UNKNOWN_PATHS,
            "scan_error_code": SCAN_UNASSESSABLE_CODE,
            "scan_error_facts": _exception_facts(exc),
            "counted": {"measured": 0, "files": 0, "directories": 0, "over_ceiling": 0, "unknown": 1},
            "worst_offenders": [],
            "unknown_paths": [{"path": str(out_dir)}],
        }


def write_path_ceiling_report(out_dir: Path, report: dict) -> Path | None:
    """Publish the measurement beside the bundle it judges, ATOMICALLY. None if it was not written.

    The order is the guarantee: the whole document is serialized to a string FIRST, then written to
    a per-process staging sibling, flushed and fsynced, and only then `os.replace`d over the final
    name. So a serialization error, a full disk or a torn write cannot leave a truncated
    `path-ceiling.json` behind, and an existing report from a previous run stays byte-identical
    rather than being destroyed by the very run that could not describe itself. On any failure only
    THIS call's exact staging file is removed, best effort - never the report, never a sibling.

    ⚠️ The pattern is deliberately the repo's existing one (`_abf._staged_image_write`,
    `generated_edit_declarations._append_record`): unique-per-process staging name, `os.replace`,
    staging removed when the swap did not happen. Neither is imported: `_append_record` is private
    to another module, generates its own filename and injects its own `version`/`recorded_at` keys,
    so its semantics do not fit publishing one named report - and `_abf` belongs to a skill bundle.
    """
    final_path = out_dir / PATH_CEILING_REPORT
    staging_path = final_path.with_name(f"{final_path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    swapped = False
    try:
        # Serialize BEFORE touching the filesystem: a TypeError here must never have opened a file.
        payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
        with open(staging_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging_path, final_path)
        swapped = True
    except (OSError, TypeError, ValueError) as exc:
        log.warning("PATH CEILING: report not published (%s)", _operation_failure(PUBLISH_REPORT_OPERATION, exc))
        return None
    finally:
        if not swapped:
            try:
                staging_path.unlink()
            except OSError:  # pragma: no cover - best effort by contract; the report is untouched
                log.warning("PATH CEILING: staging file left behind: %s", staging_path.name)
    return final_path


def path_ceiling_verdict(report: dict, written: Path | None) -> tuple[bool, str]:
    """Turn one measurement into (proceed, detail). Everything that is not clean refuses.

    ``report`` is the SHAREABLE view (:func:`shareable_path_report`), so every path this prints is
    already bundle-relative. The report is named relatively too - the operator supplied ``--output``
    and does not need it read back, while a console line is pasted into issues and chat.
    """
    counted = report.get("counted") or {}
    where = f" Report: {SAFE_BUNDLE_ROOT}/{PATH_CEILING_REPORT}." if written else ""
    preserved = " The emitted output is PRESERVED as evidence - nothing was deleted or rewritten."
    if written is None:
        return False, (
            "CANNOT ASSESS the emitted tree: its path-ceiling report could not be written into "
            f"{report.get('root')}, so the verdict would not be attributable." + preserved
        )
    if report.get("status") == STATUS_OVER_CEILING:
        offenders = report.get("worst_offenders") or []
        binding = max(offenders, key=lambda record: record["length"] - record["ceiling"], default=None)
        detail = (
            f"binding {binding['kind']} is {binding['length']} UTF-16 units (ceiling "
            f"{binding['ceiling']}): {_ascii_path(binding['path'])}"
            if binding
            else "no offender could be named"
        )
        return False, (
            f"PATH CEILING: {counted.get('over_ceiling')} EMITTED path(s) exceed what Power BI "
            f"Desktop will open - {detail}. LongPathsEnabled and \\\\?\\ prefixes do not make "
            f"Desktop accept these paths, so nothing downstream may start from this tree.{where}"
            f"{preserved} {_SHORT_ROOT_HINT}"
        )
    if report.get("status") == STATUS_UNKNOWN_PATHS:
        unknown = (report.get("unknown_paths") or [{}])[0]
        cause = report.get("scan_error_code") or unknown.get("code") or "no code was recorded"
        facts = report.get("scan_error_facts") or {}
        codes = "".join(f" {key}={value}" for key, value in facts.items())
        return False, (
            f"CANNOT ASSESS the emitted tree: {counted.get('unknown')} path(s) could not be "
            f"measured - {cause}{codes} ({_ascii_path(str(unknown.get('path')))}). Unmeasurable is "
            f"not clean, so nothing downstream may start from this tree.{where}{preserved}"
        )
    if report.get("status") == STATUS_NO_PATHS:
        return False, (
            f"CANNOT ASSESS the emitted tree: nothing was measured under {report.get('root')}. An "
            f"output folder with no measurable path cannot be judged clean.{where}{preserved}"
        )
    advisory = ""
    if report.get("root_budget_is_tight"):
        advisory = (
            f" ADVISORY: root budget {report.get('root_budget')} < "
            f"{report.get('shipping_root_budget_advisory')} - this bundle tolerates only a short "
            "installation root wherever it is shipped; not a refusal."
        )
    return True, (
        f"PATH CEILING: {counted.get('measured')} emitted path(s) measured, none over Desktop's "
        f"ceilings (file <= {report.get('file_ceiling')}, directory <= {report.get('dir_ceiling')})."
        f"{where}{advisory}"
    )


def check_emitted_path_ceiling(out_dir: Path, phases: list[dict], limits: Limits | None = None) -> tuple[bool, str]:
    """Gate the ACTUAL engine output against Desktop's ceilings before anything consumes it.

    The measurement is absolute (that is what Desktop counts); everything that LEAVES this call -
    the published report and the printed verdict - is the bundle-relative, host-location-free view
    built by :func:`shareable_path_report`.
    """
    started = time.monotonic()
    measured = scan_emitted_path_ceiling(out_dir, limits)
    report = shareable_path_report(measured, out_dir)
    written = write_path_ceiling_report(out_dir, report)
    proceed, detail = path_ceiling_verdict(report, written)
    phases.append(
        {
            "phase": "path_ceiling",
            "elapsed_sec": round(time.monotonic() - started, 1),
            "status": report.get("status"),
            "over_ceiling": (report.get("counted") or {}).get("over_ceiling"),
        }
    )
    return proceed, detail


# ---------------------------------------------------------------------------
# The destructive-re-run barrier (issue #250) - the ONLY pre-engine gate
#
# ONE rule governs everything below: where the barrier CANNOT ASSESS something, it must say so and
# BLOCK. Absence of evidence is not evidence of absence. A guard that reports clean when it cannot
# see is worse than no guard, because the operator trusts it - and five separate routes into
# "reported clean, destroyed real work at exit 0" were found in the first cut of this file.
# ---------------------------------------------------------------------------

BUNDLE_REWRITE_RECORD = "bundle-rewrite-acknowledgement.json"
ENGINE_TREE_KEY = "engine_output_tree"
REWRITE_LIST_LIMIT = 12

# Every top-level folder a re-run may delete and rewrite. `migration_bundle.ENGINE_OUTPUT_DIRS` is
# deliberately NOT reused: it omits `reports/`, and the receipt built from it filters by
# `ARTIFACT_SUFFIXES`, which is exactly the allowlist that let a hand-authored PBIR file and a
# `textscan` `.txt` extract go unbaselined and unprotected. The barrier allowlists LOCATIONS, never
# formats.
ENGINE_TREE_ROOTS = ("data", "pbip", "reports", "semantic_models")

# What makes a `--output` folder "already an engine bundle" rather than a fresh target. An empty or
# unrelated folder is NOT one, so a first run into a new folder is never touched by the barrier.
BUNDLE_MARKERS = ("report.json", "summary.md", "input_manifest.json", BUNDLE_REWRITE_RECORD)


class BundleDrift(NamedTuple):
    """Files in a bundle that no longer match the baselines its previous run wrote."""

    modified: list[str]
    added: list[str]
    missing: list[str]

    @property
    def total(self) -> int:
        """How many files are no longer what the engine produced."""
        return len(self.modified) + len(self.added) + len(self.missing)


class BundleCoverage(NamedTuple):
    """How much of the bundle the barrier can actually vouch for.

    ``complete`` means a full engine-output tree baseline exists, which is the ONLY thing that makes
    an ADDED file decidable - without it, a file the engine never wrote is indistinguishable from
    one it did. ``gaps`` is every reason the assessment is partial; a non-empty ``gaps`` is itself a
    blocking finding, because "I could not look" must never render as "I looked and it was fine".
    """

    known: dict[str, str]
    complete: bool
    gaps: list[str]


class BundleRewriteFindings(NamedTuple):
    """Everything the pre-engine barrier knows about the `--output` folder it is about to rewrite.

    ``applicable`` is False only when the question does not arise: ``--slice-only`` (no engine, so
    nothing is destroyed) or a `--output` that is not an engine bundle yet. It is never False merely
    because evidence is missing - that case is applicable AND indeterminate, which blocks.
    """

    bundle: Path
    applicable: bool
    drift: BundleDrift
    coverage: BundleCoverage
    recorded_version: str | None
    running_version: str | None
    accepted_rewrite: bool
    accepted_version: bool

    @property
    def version_changed(self) -> bool:
        """A different engine built this bundle than the one about to rewrite it."""
        return bool(
            self.applicable
            and self.recorded_version
            and self.running_version
            and self.recorded_version != self.running_version
        )

    @property
    def version_indeterminate(self) -> bool:
        """Either version is unknown, so "did the engine change?" has no answer.

        A truncated receipt used to make this return "no change" and wave the run through - one
        broken byte disabling the guard. Unknown is now its own blocking state.
        """
        return bool(self.applicable and not (self.recorded_version and self.running_version))

    @property
    def blocks_on_downstream(self) -> bool:
        """Work would be destroyed, or the barrier cannot prove that it would not be."""
        return bool(self.applicable and (self.drift.total or self.coverage.gaps) and not self.accepted_rewrite)

    @property
    def blocks_on_engine_version(self) -> bool:
        """The engine differs, or cannot be shown not to differ."""
        return bool((self.version_changed or self.version_indeterminate) and not self.accepted_version)

    @property
    def blocking(self) -> bool:
        """Whether this run must be refused."""
        return self.blocks_on_downstream or self.blocks_on_engine_version

    @property
    def acknowledged(self) -> bool:
        """Whether a real finding or a coverage gap was waived by a flag, and so must be recorded."""
        return bool(
            (self.applicable and (self.drift.total or self.coverage.gaps) and self.accepted_rewrite)
            or ((self.version_changed or self.version_indeterminate) and self.accepted_version)
        )


def _barrier_covers_path(relative: Path) -> bool:
    """Whether a bundle-relative path is accounted for by the REWRITE BARRIER.

    Only Power BI Desktop's `.pbi` sidecars are excluded, so a normal refresh never reads as
    downstream work. Applied to BOTH sides of every comparison - current disk state and recorded
    baseline alike - so a pristine bundle still reconciles exactly.

    Deliberately NOT `_is_scratch_path`. That predicate answers a DIFFERENT question - "is this a
    stable deliverable worth putting in the tamper-audit manifest?" - and reusing it here made one
    predicate do two jobs and punched a hole straight through the barrier: `_build/` is this repo's
    durable REPLAY-SCRIPT convention (`AGENTS.md`: "every edit re-runnable from `_build/`"), so
    `pbip/<project>/_build/replay.py` is precisely where an agent's re-runnable work lives - and it
    sits inside a directory the engine rmtree()s. Skipping it meant a re-run destroyed the replay
    script for the very edits it was written to reproduce, at exit 0.

    A bundle-ROOT `_build/` is still unguarded, and correctly so: it is outside `ENGINE_TREE_ROOTS`
    and therefore outside anything the engine deletes. The scope is the destructive roots, not the
    folder name.
    """
    return not any(part.lower() in VOLATILE_GENERATED_DIRS for part in relative.parts)


def engine_output_tree_hashes(bundle: Path) -> dict[str, str]:
    """Hash EVERY file under every folder a re-run may delete, with no format allowlist.

    This is the barrier's authoritative baseline and the reason it can see an added PBIR file or a
    `textscan` `.txt` extract under `<project>.Data` - neither of which carries a suffix the engine
    receipt records, and both of which sit inside a directory the engine rmtree()s.
    """
    files: dict[str, str] = {}
    for root_name in ENGINE_TREE_ROOTS:
        root = bundle / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(bundle)
            if _barrier_covers_path(relative):
                files[relative.as_posix()] = sha256_file(path)
    return files


def write_engine_output_tree(bundle: Path) -> Path:
    """Record the complete engine-output tree into ``input_manifest.json``.

    A SEPARATE key from ``generated_artifacts`` on purpose: that one is the tamper-audit baseline of
    stable *deliverables* an agent may declare edits against, and `check_migration_progress.py`
    depends on its shape and scope. This one answers a different question - "what was in the
    directories the next run will delete?" - so it is deliberately wider (flat-file extracts,
    `<project>.Data`, `reports/`, loose files) and must not be conflated with it.

    Written only on a real engine run. ``--slice-only`` has no engine-run boundary, so it can only
    hash a working copy that may already contain downstream edits; recording that here would launder
    those edits into "engine output" and is exactly the hole this key exists to close.
    """
    manifest_path = bundle / "input_manifest.json"
    manifest: dict = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        manifest = loaded if isinstance(loaded, dict) else {"engine_input_manifest": loaded}
    manifest[ENGINE_TREE_KEY] = {
        "version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": list(ENGINE_TREE_ROOTS),
        "files": engine_output_tree_hashes(bundle),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def read_engine_receipt(bundle: Path) -> tuple[dict | None, str | None]:
    """Read the receipt the bundle's last run wrote. Returns ``(receipt, gap)``.

    ``write_receipt_phase`` writes this on EVERY run and, until this guard existed, nothing ever read
    it back. A malformed receipt degrades to ``(None, gap)`` rather than raising - but the gap is a
    BLOCKING finding, not a warning: a corrupt receipt tells you less than no receipt does, and
    treating it as "no version change" is how one broken byte disabled the version guard entirely.
    """
    path = bundle / ENGINE_RECEIPT
    if not path.is_file():
        return None, None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{ENGINE_RECEIPT} is unreadable ({type(exc).__name__}) - it attests to nothing"
    if not isinstance(receipt, dict):
        return None, f"{ENGINE_RECEIPT} is not a JSON object - it attests to nothing"
    return receipt, None


def _receipt_artifact_hashes(receipt: dict | None) -> dict[str, str]:
    """The receipt's ``artifacts`` list as a path -> sha256 map, ignoring malformed entries."""
    hashes: dict[str, str] = {}
    for record in (receipt or {}).get("artifacts") or []:
        if not isinstance(record, dict):
            continue
        path, digest = record.get("path"), record.get("sha256")
        if isinstance(path, str) and isinstance(digest, str) and _barrier_covers_path(Path(path)):
            hashes[path] = digest
    return hashes


def _read_manifest(bundle: Path) -> dict:
    """``input_manifest.json`` as a dict, or empty when absent or unreadable."""
    path = bundle / "input_manifest.json"
    if not path.is_file():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _hash_map(block: object) -> dict[str, str]:
    """A ``{"files": {path: sha256}}`` block as a clean map, dropping anything malformed."""
    files = block.get("files") if isinstance(block, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        path: digest
        for path, digest in files.items()
        if isinstance(path, str) and isinstance(digest, str) and _barrier_covers_path(Path(path))
    }


def assess_coverage(bundle: Path, receipt: dict | None, receipt_gap: str | None) -> BundleCoverage:
    """Work out what the barrier can vouch for, and name every hole in it.

    Three baselines, in descending authority: the complete engine-output tree, the engine receipt,
    and the generated-artifact manifest. Only the first makes an ADDED file decidable. A
    ``--slice-only`` backfill is explicitly NOT trusted - it hashes the working copy, downstream
    edits included, so treating it as engine output blesses exactly the work the barrier protects.
    """
    gaps: list[str] = []
    known: dict[str, str] = {}
    manifest = _read_manifest(bundle)

    tree = _hash_map(manifest.get(ENGINE_TREE_KEY))
    known.update(tree)
    if not tree:
        gaps.append(
            f"no usable '{ENGINE_TREE_KEY}' baseline in input_manifest.json - a file the engine never "
            "wrote cannot be told from one it did, so ADDED downstream work is undetectable here"
        )

    receipt_hashes = _receipt_artifact_hashes(receipt)
    known.update(receipt_hashes)
    if receipt is None:
        gaps.append(receipt_gap or f"no {ENGINE_RECEIPT} - nothing attests to what the engine produced")
    elif not receipt_hashes:
        gaps.append(f"{ENGINE_RECEIPT} lists no usable artifacts - it attests to nothing")

    generated = manifest.get(GENERATED_ARTIFACTS_KEY)
    coverage = generated.get("coverage") if isinstance(generated, dict) else None
    if coverage == SLICE_ONLY_COVERAGE:
        gaps.append(
            f"'{GENERATED_ARTIFACTS_KEY}' was backfilled by --slice-only from the working copy, so its "
            "hashes are not proof of engine origin and are not trusted as a baseline here"
        )
    else:
        known.update(_hash_map(generated))

    return BundleCoverage(known, bool(tree), gaps)


def detect_downstream_work(bundle: Path, coverage: BundleCoverage) -> BundleDrift:
    """Re-hash the bundle and name every file that is no longer what the engine produced.

    ``added`` is reported ONLY when coverage is complete. With a partial baseline every unrecognised
    file is ambiguous, so listing some of them would imply the rest had been cleared; the coverage
    gap blocks instead, which is the honest answer.
    """
    current = engine_output_tree_hashes(bundle)
    modified: list[str] = []
    missing: list[str] = []
    for path, digest in coverage.known.items():
        now = current.get(path)
        if now is None:
            candidate = bundle / path
            if not candidate.is_file():
                missing.append(path)
            elif sha256_file(candidate) != digest:
                modified.append(path)
        elif now != digest:
            modified.append(path)
    added = [path for path in current if path not in coverage.known] if coverage.complete else []
    return BundleDrift(sorted(set(modified)), sorted(set(added)), sorted(set(missing)))


def _engine_versions(receipt: dict | None, engine: Path | None) -> tuple[str | None, str | None]:
    """(version that built the bundle, version about to rewrite it) - either may be unknown."""
    block = (receipt or {}).get("engine")
    recorded = block.get("version") if isinstance(block, dict) else None
    running = engine_provenance(engine)["version"] if engine is not None else None
    return (recorded if isinstance(recorded, str) else None, running if isinstance(running, str) else None)


def _looks_like_bundle(bundle: Path) -> bool:
    """Whether `--output` already holds engine output, rather than being a fresh target."""
    return any((bundle / name).is_file() for name in BUNDLE_MARKERS) or any(
        (bundle / root).is_dir() for root in ENGINE_TREE_ROOTS
    )


def assess_bundle_rewrite(args: argparse.Namespace, engine: Path | None) -> BundleRewriteFindings:
    """Decide, BEFORE the engine runs, whether this run may rewrite `--output`.

    ``--slice-only`` is exempt by construction, not by exception: it never invokes the engine (see
    ``resolve_run_engine``), so there is no delete-and-recreate to guard against, and it legitimately
    points at an existing bundle every single time.
    """
    bundle = Path(args.output)
    accepted_rewrite = bool(args.accept_bundle_rewrite)
    accepted_version = bool(args.accept_engine_version_change)
    if args.slice_only or not bundle.is_dir() or not _looks_like_bundle(bundle):
        return BundleRewriteFindings(
            bundle,
            False,
            BundleDrift([], [], []),
            BundleCoverage({}, True, []),
            None,
            None,
            accepted_rewrite,
            accepted_version,
        )

    receipt, receipt_gap = read_engine_receipt(bundle)
    coverage = assess_coverage(bundle, receipt, receipt_gap)
    recorded_version, running_version = _engine_versions(receipt, engine)
    return BundleRewriteFindings(
        bundle,
        True,
        detect_downstream_work(bundle, coverage),
        coverage,
        recorded_version,
        running_version,
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


def _print_drift(findings: BundleRewriteFindings) -> None:
    """The downstream-work half of the verdict: what is here that the engine did not put here."""
    if not findings.drift.total and not findings.coverage.gaps:
        return
    verdict = "ACCEPTED" if findings.accepted_rewrite else "REFUSED"
    print(f"\nESTATE: BUNDLE_REWRITE {verdict} - {findings.bundle}")
    if findings.drift.total:
        print(f"  {findings.drift.total} file(s) no longer match what the engine produced:")
        for label, paths in (
            ("modified", findings.drift.modified),
            ("added", findings.drift.added),
            ("missing", findings.drift.missing),
        ):
            if paths:
                print(f"  {label} ({len(paths)}):")
                print(_sample(paths))
    for gap in findings.coverage.gaps:
        print(f"  CANNOT ASSESS: {gap}")
    print(
        "  An engine re-run is DELETE-AND-RECREATE, not merge: it rmtree()s the .SemanticModel\n"
        "  folder, the .pbip project dir and <name>.Report before rewriting them, and the\n"
        "  engine's own stale-output guard EXEMPTS the --approved-dax landing path. Anything in\n"
        "  those folders would be gone, and no other gate here runs until after that has happened."
    )
    if not findings.accepted_rewrite:
        print("  -> land into a FRESH --output, or pass --accept-bundle-rewrite to proceed knowingly.")


def _print_version(findings: BundleRewriteFindings) -> None:
    """The engine-identity half: a bundle two engine versions built is two bundles in a trench coat."""
    if not (findings.version_changed or findings.version_indeterminate):
        return
    verdict = "ACCEPTED" if findings.accepted_version else "REFUSED"
    if findings.version_changed:
        print(
            f"\nESTATE: BUNDLE_REWRITE {verdict} - this bundle was built by engine "
            f"{findings.recorded_version}, this run would rewrite it with {findings.running_version}"
        )
    else:
        print(
            f"\nESTATE: BUNDLE_REWRITE {verdict} - CANNOT ASSESS the engine version "
            f"(bundle recorded: {findings.recorded_version or 'unknown'}; this run: "
            f"{findings.running_version or 'unknown'})"
        )
    print(
        "  Two engine versions are not equivalent: 2.113.0 emitted deprecated Bing shapeMap\n"
        "  visuals and dropped a density-map worksheet entirely where 2.126.0 emitted azureMap\n"
        "  with a heat layer, and nothing in the output said which one ran (#107). Rewriting in\n"
        "  place mixes both into one bundle."
    )
    if not findings.accepted_version:
        print("  -> use a FRESH --output, or pass --accept-engine-version-change to proceed knowingly.")


def print_bundle_rewrite(findings: BundleRewriteFindings) -> None:
    """Name what a re-run into this `--output` would destroy, and how to proceed deliberately."""
    _print_drift(findings)
    _print_version(findings)


def record_bundle_rewrite_acknowledgement(findings: BundleRewriteFindings) -> Path | None:
    """Append the acknowledgement to the bundle, so the ARTIFACT says the loss was deliberate.

    Written before the engine runs and at the bundle root, which the engine's `rmtree` sites do not
    touch, so it survives the rewrite it is describing. The coverage gaps are recorded alongside the
    file list: "these files were destroyed" and "and this much could not be assessed at all" are
    different admissions, and collapsing them would overstate what the record proves.
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
            "coverage_complete": findings.coverage.complete,
            "coverage_gaps": findings.coverage.gaps,
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
    parser.add_argument(
        "--input",
        type=Path,
        help="folder of .twb/.twbx/.tds/.tdsx to migrate - point it at ALL of them in ONE pass; "
        "the engine migrates datasources before workbooks and feeds the result through ds_catalog",
    )
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
    parser.add_argument(
        "--provenance-timeout-sec",
        type=float,
        default=PROVENANCE_TIMEOUT_DEFAULT_SEC,
        help=(
            "whole-phase budget for source provenance, in seconds (default "
            f"{PROVENANCE_TIMEOUT_DEFAULT_SEC:g}). Must be finite and greater than zero: there is no "
            "disable sentinel, and zero does NOT mean local-only. Expiry publishes a partial/failed "
            f"artifact and returns {EXIT_PROVENANCE_FAILED}"
        ),
    )
    return parser


def valid_provenance_timeout(timeout_sec: object) -> bool:
    """Whether the phase budget is a real duration.

    ``0`` is not "no timeout" and not "local only", and neither is a negative, ``NaN`` or infinite
    value: each of them would either publish nothing or reinstate the unbounded stall this budget
    exists to end. There is deliberately no disable sentinel - local-only operation comes from having
    no live credentials, and stays deadline-bound because local reads and hashes block too.
    """
    return (
        isinstance(timeout_sec, (int, float))
        and not isinstance(timeout_sec, bool)
        and (math.isfinite(timeout_sec) and timeout_sec > 0)
    )


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
    if args.input:
        path_ok, path_detail = preflight_estate_path_ceiling(args.input, args.output, engine)
        print(path_detail)
        if not path_ok:
            return None, EXIT_PATH_CEILING
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


def produce_and_gate_output(
    args: argparse.Namespace, engine: Path | None, phases: list[dict]
) -> tuple[dict | None, int]:
    """Run the engine, baseline what it wrote, and refuse a tree nothing downstream may consume.

    These three steps are ONE procedure, and the order is load-bearing in both directions:

    * the baselines and receipt are recorded FIRST, so a bundle refused below still says what built
      it - the receipt is exactly the evidence an upstream path-length report needs;
    * the emitted tree is measured LAST, and before this function returns, so provenance, handover
      slices, packaging, agent work and Desktop all sit behind it. The pre-engine projection cannot
      see the paths the engine really writes (issue #564), so this is the only place where a bundle
      Power BI Desktop refuses to open can still be stopped.

    Returns ``(report, exit code)``; the report is None only when there was no output to read.
    """
    if not args.slice_only:
        code = run_engine_phase(args, engine, phases)
        if code != EXIT_OK:
            # A failed engine has no output to judge, so nothing is measured and no path report is
            # written - the engine's own verdict keeps precedence.
            return None, code

    report = read_report(args.output)
    if not args.slice_only:
        record_engine_output(args.output, report, phases, engine)
    else:
        backfill_slice_only_baseline(args.output, report, phases)

    path_ok, path_detail = check_emitted_path_ceiling(args.output, phases)
    print(path_detail)
    if not path_ok:
        # The timings are written even on a refusal, and they carry no later phase: the record IS
        # the evidence that nothing downstream started. The output tree itself is left untouched.
        #
        # ⚠️ But the refusal OUTRANKS its own evidence. A bundle Power BI Desktop cannot open must
        # still refuse when the timings cannot be persisted (a full disk, a read-only mount, an
        # unserializable phase); returning EXIT_OK there - or letting the exception escape into the
        # caller's traceback - would turn a path refusal into a run that continues or into a crash
        # whose exit code says something else entirely. Only the write/serialization classes this
        # repo already catches around a JSON write are absorbed; anything else still propagates.
        try:
            write_phase_record(args.output, phases)
        except (OSError, TypeError, ValueError) as exc:
            log.warning(
                "PATH CEILING: phase timings not persisted (%s)",
                _operation_failure(WRITE_PHASE_RECORD_OPERATION, exc),
            )
        return report, EXIT_PATH_CEILING
    return report, EXIT_OK


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


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-locals,too-many-return-statements
    """CLI entry point."""
    args = build_parser().parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    phases: list[dict] = []

    # Before the engine, the path gate, the worker and the publisher: a budget that is not a duration
    # cannot bound anything, and refusing it here costs the operator a re-run rather than a migration.
    # The diagnostic is fixed text - it echoes neither the value nor anything else the user supplied.
    if not valid_provenance_timeout(args.provenance_timeout_sec):
        print("ERROR: --provenance-timeout-sec must be a finite number greater than zero", file=sys.stderr)
        return EXIT_USAGE

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

    # --- phase 1 + 1a: the engine, its recorded output, and the ceilings that output must meet --
    report, code = produce_and_gate_output(args, engine, phases)
    if code != EXIT_OK:
        return code

    # --- phase 1b: stamp where the inputs came from -------------------------------------------
    # The engine records the LOCAL half in input_manifest.json (name, size, sha256, staged path)
    # and nothing about the upstream: which Tableau site, workbook LUID, project, or product
    # version. Measured cost of that gap: filing three upstream defects required reconstructing all
    # of it by hand, and it mattered - Tableau's samples differ between releases, so figures cited
    # against "Superstore" do not reproduce against a different build and the reader cannot tell.
    # Best-effort and never fatal: a migration must not fail because a site was unreachable.
    if args.input:
        started = time.monotonic()
        stamped = stamp_inputs(args.input, args.output, args.provenance_timeout_sec)
        phases.append(
            {
                "phase": "provenance",
                "elapsed_sec": round(time.monotonic() - started, 1),
                "status": stamped.status,
            }
        )
        log.info("PROVENANCE: %s", stamped.detail)
        if not stamped.ok:
            try:
                write_phase_record(args.output, phases)
            except (OSError, TypeError, ValueError) as exc:
                log.warning(
                    "PROVENANCE: phase timings not persisted (%s)",
                    _operation_failure(WRITE_PHASE_RECORD_OPERATION, exc),
                )
            print(f"\nESTATE: PROVENANCE_FAILED - {stamped.detail}")
            return EXIT_PROVENANCE_FAILED

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
