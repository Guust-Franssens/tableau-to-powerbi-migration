"""
purpose: assemble ONE self-contained, no-flags handover package per migration unit (report or datasource).
usage:   python scripts/package_unit.py --bundle <bundle> --out <dir> [--unit NAME ...]
                                        [--oracle <dir>] [--assets <dir>] [--json <file>] [--quiet]

Issue #446: the three things an agent needs to start one report all exist and NOTHING assembles them.
They live in four naming schemes across two trees - the engine keys `pbip/`, `reports/` and
`handover/` by sanitized workbook name, the oracle keys renders and numbers by bare view LUID in a
flat directory outside the bundle, and the source asset is a LUID-prefixed filename in a third place.
So `check_reference_readiness.py` and `check_unit.py` both need `--source`/`--oracle` arguments that
cannot be derived from the unit path, and getting one wrong reads as "this unit is broken" rather
than "you did not tell me where the workbook is".

This script emits a folder both gates accept with NO flags:

    <out>/<Unit>/
        migration-spec.json          <- parse_tableau.py; check_unit.py's expected page set (#443)
        report.json                  <- the engine's own classification, SCOPED to this unit
        source-provenance.json       <- SCOPED; the only trusted route to a workbook LUID
        engine-output-receipt.json   <- what built this (version drift stays checkable)
        assets/<luid>_<Name>.twb(x)  <- the source, under the name resolve_source() already looks for
        fabric/<Name>.Report/        <- the engine WORKING COPY (`pbip/`), never the `reports/` baseline
        fabric/<Model>.SemanticModel/
        handover/<Unit>.json         <- the engine's per-workbook slice, verbatim
        handover.md                  <- flat, one-finding-per-line, emptied visuals FIRST
        oracle/
            oracle-manifest.json     <- THIS unit's views only, paths rewritten
            dashboard/{images,data}/<Object>.<ext>   <- SINGULAR: the directory is object_identity's
            worksheet/{images,data}/<Object>.<ext>      KIND_* value verbatim, never a pluralised copy
            unknown/{images,data}/<Object>.<ext>     <- carried but MARKED, never filed as either kind
        package-manifest.json        <- what was packaged, and every omission with its reason
        README.md

Why `assets/` and not the `source/` the issue sketched: `check_reference_readiness.resolve_source`
already tries `<root>/assets/<basename>` (:544-545), and the handover slice's `workbook.source_id`
already carries that basename. Reusing the existing convention means this packaging needs ZERO
changes to either gate - the whole feature is arrangement, which is the only kind of fix that cannot
regress a verdict.

Attribution is FAIL-CLOSED, and by IDENTITY only - the one design rule
----------------------------------------------------------------------
A render this script cannot tie to a specific workbook **by LUID** is OMITTED and the reason recorded
in `package-manifest.json`; it is never copied in "because it was in the same capture" (issue #438 in
a new place), and never adopted because a display NAME happened to match.

There is exactly ONE admissible route: `oracle-manifest.json`'s `workbook_luid`, matched against the
LUID `source-provenance.json` records for the sha256 of the copied asset, cross-checked against the
asset filename's LUID prefix. A disagreement fails closed.

⚠️ **A display name is not an identity, and a name route was DELETED rather than guarded.** Two
projects can hold workbooks with the same name - the exact ambiguity `_runs/<NNN>-<slug>/` numbering
exists to avoid elsewhere in this repo. Issue #450 measured the consequence in a sibling gate:
`check_unit`'s workbook-attribution guard reads a field the capture does not write, is inert on
**360 of 360** real records, and therefore admits a foreign workbook's render as this unit's
evidence. This packager will not inherit that class. Measured cost of the deletion on the reference
estate: **zero** - the name route fired 0 times in 67 units.

Copying a render is NOT a claim that it is byte-faithful. `stamp_tableau_provenance.py` records
`origin.match: "name_only"` when the local and server bytes DIFFER, and the readiness gate refuses to
trust a LUID in that case (`check_reference_readiness._provenance_luid`). Such a unit still gets its
renders - an agent can look at them - and the gate still reports its pages BLIND. `handover.md`
carries `ORACLE_ATTRIBUTION ... match=name_only` so the difference is visible rather than inferred.

`view_type` is the ONLY type discriminator. `content_url` is `<wb>/sheets/<view>` for dashboards AND
worksheets, and `capture_tableau_oracle.py`'s type resolver is non-fatal by design, so `unknown` is a
legitimate value. An untyped render is filed under `unknown/` and named on its own `UNTYPED_RENDER`
line - never defaulted into either kind, because `reference_evidence._oracle_view_kind` treats absent
and `unknown` alike as "cannot satisfy any page".

Review contract
---------------
**Invariant.** Packaging RELOCATES and SCOPES; it never changes a page verdict. For every unit, the
entry gate run on the package must yield the same per-page readiness as the bundle-level run with
`--oracle`. Measured across the 67-unit reference estate: `pages_expected 220 / pages_ready 42 /
pages_blind 178`, identical both ways.

**Direction.** *Fail-open* (blocks merge): a render attributed to the wrong unit, crediting coverage
that does not exist. *Fail-closed* (residual, becomes an issue): a render that could have been
attributed is omitted, so a page reads BLIND - costs work, credits nothing false.

**Closed surface, N = 17 joins/transformations that can move the invariant**, plus 4 named residuals:

| # | join or transformation | key | how it is closed |
|---|---|---|---|
| 1 | `pbip/<Unit>/` -> `fabric/` | folder name | copied whole |
| 2 | report <-> model pairing | containment, NOT name | folder copied whole (a) |
| 3 | unit -> handover slice | file stem | exact |
| 4 | unit -> asset (handover) | `workbook.source_id` basename | run-root-relative, so basename only |
| 5 | unit -> asset (input manifest) | `Path(name).stem == unit` | exact, fallback only |
| 6 | asset -> workbook LUID | `input.sha256` | content-keyed; >1 LUID refuses |
| 7 | asset filename LUID | `<uuid>_` prefix | cross-check only, never a source (b) |
| 8 | workbook LUID -> oracle views | `workbook_luid` | the only route (#450) |
| 9 | view -> object kind | `view_type` | `unknown/`, marked, never defaulted |
| 10 | view -> filename | sanitized `view_name` | LUID-suffix disambiguation |
| 11 | leg -> bytes | recorded sha256/bytes/dims | verbatim copy, only `path` rewritten |
| 12 | leg claiming ok, file absent | — | status -> `omitted_by_packager` |
| 13 | unit -> engine classification | `report.json` name | exact; LISTS not sets, so duplicates show |
| 14 | unit universe | `report.json` U `pbip/` | neither side is a superset (c) |
| 15 | receipt artifacts | `pbip/<unit>/` prefix | re-rooted to `fabric/` |
| 16 | emptied visual -> page | visual id -> PBIR dir | directory lookup |
| 17 | package location -> gate discovery | `_default_dirs` scans the GRANDPARENT | shadowing refused, exit 2 |

(a) `byPath ../<Model>.SemanticModel` survives the copy, and 27 of 62 model names differ from their
unit's, so the pair is never re-established by name. (b) on a `.tds` the prefix is a DATASOURCE LUID,
a different identity namespace. (c) 4 workbooks ship no working copy, and 2 working copies are
unlisted because the engine disambiguated two same-named workbooks on disk.

**Residuals, named not guarded.** (R1) `<bundle>/reports/` is the engine BASELINE and is deliberately
never packaged - no model sits beside it, so a copy would not resolve `byPath`. (R2) issue #450 lives
in `check_unit._declared_workbook`, not here: the packaged manifest preserves `workbook_name`
verbatim and deliberately does **not** add the `workbook` key that would make that guard live, since
doing so would change a gate's verdict as a side effect of packaging - which is the invariant this
script exists to hold. (R3) `parse_tableau.py` can refuse a valid workbook (measured:
`World_Indicators`, a `quantiles` reference line outside its schema enum), so a unit may ship without
`migration-spec.json`; recorded as a `PACKAGE_NOTE`, never swallowed. (R4) an oracle capture is
default-view-state with no `?vf_` pinning, so `oracle/` is **layout/text grade only** regardless of
render leg.

Exit codes
----------
| 0 | every requested unit was packaged, engine output included, and every source its model names is
      IN the package |
| 1 | at least one unit has NO engine working copy under `pbip/`. It is still packaged - the source,
      the reference and the engine's own handover slice are all there - but there is nothing to build
      on, and `check_reference_readiness.py` reports it as a finding rather than a pass. |
| 2 | usage error (argparse), including an `--out` too deep for the paths this bundle would produce.
      Every unit is measured against `check_path_ceiling.py`'s ceilings BEFORE any is assembled, and
      the refusal names the path, its length, the ceiling and how many characters `--out` must lose.
      Nothing is packaged: a shorter `--out` moves every unit, so a partial estate would be redone
      anyway (#476). |
| 3 | at least one requested unit already has a package carrying EDITS, and this packager replaces a
      package whole. Those units were left untouched; every other requested unit was still packaged.
      `--discard-package-edits` overwrites them deliberately. |
| 4 | at least one unit is NOT SELF-CONTAINED - it ships without something it names: a source its
      model reads (the bytes could not be copied, or a literal could not be classified), or the
      semantic model its report's `definition.pbir` `byPath` points at. The package is still written
      (it carries everything else) but it is not complete. Ranked above 1 because 1 is already
      visible in every gate's verdict while this is not: a model missing its rows loads, validates
      and passes `check_datamodel.py`, and `powerbi-report-author validate` returns `errorCount: 0`
      for a `byPath` naming a model that exists nowhere. |
| 5 | at least one unit hit a CONTRADICTION this packager refuses to ship past, and NOTHING was
      written for it: a source whose bytes do not match the digest `input_manifest.json` declares, a
      unit name that would write outside `--out`, or a host path that could not be contained in the
      model. Other requested units were still packaged. Deliberately the same number and meaning as
      PR #487's `EXIT_UNIT_FAILED`, since that PR has this branch as its base. |
| 6 | at least one unit - or the bundle itself - CANNOT BE ASSESSED: an input that had to be read
      exists and is unreadable, a report declares pages while its source asset cannot be resolved, or
      the bundle names no units at all. Nothing was written for those units. Ranked ABOVE every other
      outcome because every other code is a verdict about content, and "I could not read the input"
      is not a verdict - collapsing it into 0 is the defect class this repository keeps re-finding
      (measured on this branch: a truncated `report.json`, a truncated handover slice, a deleted
      source asset and a zero-unit bundle all exited 0). |

An oracle omission INSIDE a package is not exit 1, 4 or 6: a unit whose oracle genuinely has no
render for a page is the negative control, and it must package successfully and still report that
page BLIND. A missing, absent or truncated oracle is therefore explicitly NOT an unassessable input;
the entry gate reports it, correctly, at exit 1 FINDINGS. Exit 4 is about what the package itself
promises to carry, nothing else.
"""

from __future__ import annotations

# The assembler, its scoping helpers and the CLI intentionally live together: the module IS the
# packaging contract, and the numbered join table above only reads as one document while the code it
# describes is one file. Extracting the data-source localizer (#461) into a sibling module was
# considered and rejected on the same grounds - it is a step of the assembly, not a separate concern
# like `manifest_scope.py`'s allowlists.
# pylint: disable=too-many-lines

import argparse
import errno
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import read_handover  # noqa: E402  # pylint: disable=wrong-import-position

# The path budget is measured by `check_path_ceiling.py` and NOWHERE else (#476). Its ceilings were
# taken end to end against Power BI Desktop 2.157.828.0, and its `utf16_len` counts the UTF-16 code
# units .NET counts rather than the code points `len()` counts - a distinction a second, local copy
# of "260" would lose on its first edit.
from check_path_ceiling import (  # noqa: E402  # pylint: disable=wrong-import-position
    DEFAULT_LIMITS,
    KIND_DIR,
    KIND_FILE,
    SHIPPING_ROOT_BUDGET_ADVISORY,
    WINDOWS_LIMITS,
    Limits,
    platform_limits,
    utf16_len,
)
from manifest_scope import (  # noqa: E402  # pylint: disable=wrong-import-position
    ORACLE_MANIFEST_ALLOW,
    project,
    scope_handover,
    scope_receipt,
    scope_report,
    shippable_provenance,
    stamp_scope,
)
from object_identity import (  # noqa: E402  # pylint: disable=wrong-import-position
    KIND_DASHBOARD,
    KIND_UNKNOWN,
    KIND_WORKSHEET,
)
from path_flavour import (  # noqa: E402  # pylint: disable=wrong-import-position
    flavour,
    is_host_native,
    leaf,
)
from path_flavour import separator as flavour_separator  # noqa: E402  # pylint: disable=wrong-import-position
from path_flavour import inside as inside_lexically  # noqa: E402  # pylint: disable=wrong-import-position

SCRIPT_DIR = Path(__file__).resolve().parent

#: The migration-spec CONTRACT, shipped INTO each package rather than described in its README.
#: Measured on the 2026-09-03 cold run: an agent given nothing but a package invented a plausible
#: `limitations_encountered` shape (`{id, category, objects, detail, owner, status}`) and
#: `validate_spec.py` rejected all six entries, because the real item is exactly
#: `item`/`issue`/`severity`/`stage` under `additionalProperties: false`. Learning that cost a trip
#: outside the package. Prose restating a schema is a copy that drifts; the schema itself cannot.
SPEC_SCHEMA = SCRIPT_DIR.parent / "docs" / "migration-spec.schema.json"

#: Every quoted ABSOLUTE path literal in a `.tmdl`, and the two enclosing shapes that carry one.
#: Scanning for `File.Contents` alone closed less than half of issue #461: re-measured across the 67
#: packaged units of estate run 408, `File.Contents` accounts for 22 of the 31 Windows/UNC literals,
#: and the other 9 are a FOLDER PARAMETER - `expression SourceFolder = "<bundle>\pbip\<Unit>\
#: <Unit>.Data"` with partitions doing `File.Contents(#"SourceFolder" & "\Sample - Superstore.xlsx")`.
#: Those 9 are every datasource-only unit in the estate, which the narrower scan missed entirely
#: (17 units -> 25). The defect is "an absolute path escaping the package", not "a `File.Contents`
#: call", so the general shape is what is targeted.
ABSOLUTE_LITERAL_RE = re.compile(r'"((?:[A-Za-z]:[\\/]|\\\\|/)[^"]*)"')
FILE_CONTENTS_RE = re.compile(r'File\.Contents\(\s*"([^"]*)"\s*\)')
FOLDER_PARAM_RE = re.compile(r'(?P<prefix>expression\s+(?P<name>#"[^"]+"|[^\s=]+)\s*=\s*")(?P<value>[^"]*)(?P<quote>")')
EXPRESSION_NAME_RE = re.compile(r'expression\s+(#"[^"]+"|[^\s=]+)\s*=')
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNC_PATH_RE = re.compile(r"^\\\\")

#: M record fields whose value is a SERVICE ROUTE and can never be a file-system path, so a literal
#: found in one is definitively a non-path rather than something this packager cannot classify.
#:
#: Only `HttpPath` is listed, because it is the only such field MEASURED in the estate: run 408's
#: `"/sql/1.0/warehouses/<id>"` in three units is a Databricks SQL-warehouse endpoint, passed as
#: `Databricks.Catalogs(server, httpPath, ...)` / `[HttpPath="..."]`. Shape alone cannot tell it from
#: `/mnt/lake/warehouse`, and that is the point of the distinction: the ROLE can. Adding a field here
#: on speculation would widen the silent bucket, which is the defect class this module is built
#: against - so a new entry needs a literal that was actually observed in a packaged model.
SERVICE_ROUTE_RE = re.compile(r'\bHttpPath\s*=\s*"([^"]*)"', re.IGNORECASE)

#: The three verdicts :func:`_path_verdict` may return. The third one is the point: a literal this
#: packager cannot classify is neither shipped nor cleared, and collapsing it into "not a path" is
#: how `SourceFolder = "/Users/<person>/Data/"` survived packaging unchanged with NO omission
#: recorded at all (blind-review finding 5). Unassessable input gets its own bucket and its own
#: recorded reason; it never joins the clean one.
PATH_LITERAL = "path"
NOT_A_PATH = "not-a-path"
UNCLASSIFIED = "unclassified"
UNCLASSIFIED_REASON = (
    "could not be classified as a file-system path or as a non-path, so it was neither shipped "
    "nor cleared - check it by hand"
)

#: How a folder PARAMETER is read by the model, which decides what may be copied out of the folder
#: it names. Anything other than the first two is a refusal - see :func:`_parameter_usages`.
NAMED_FILES = "named-files"
WHOLE_FOLDER = "whole-folder"
UNKNOWN_USAGE = "unknown-usage"
NO_USAGE = "no-usage"

#: Where a shipped source lands inside the package, and the M parameter a rewritten `File.Contents`
#: literal reads its folder from. Both the parameter's `meta [...]` tail and the trailing-separator
#: value shape are copied from this repo's OWN committed, Desktop-verified models
#: (`examples/*/fabric/*.SemanticModel/definition/expressions.tmdl`) rather than invented.
#:
#: The name matters twice over. `File.Contents` does **not** accept a relative path - Power Query
#: rejects it outright, so "rewrite it to a relative path" would produce a model that refreshes
#: NOWHERE, which is worse than one that refreshes on a single machine. A parameter is the
#: documented workaround. And `DataFolder` is one of the two names `scripts/set_data_folder.py`
#: already localizes, sanitizes and CI-gates, so a unit promoted to `migrations/<slug>/fabric/` is
#: covered by the existing privacy gate with no new code and no new script.
DATA_DIR = "data"
DATA_FOLDER_PARAM = "DataFolder"
FALLBACK_DATA_FOLDER_PARAM = "PackageDataFolder"
EXPRESSIONS_TMDL = "expressions.tmdl"

#: What a data-folder parameter names in the SHIPPED artifact, instead of wherever this packager
#: happened to be run. Blind-review round-2 finding 1: the value used to be the package's absolute
#: build-time location, so a handover folder that anybody moved - which is the entire point of a
#: handover folder - kept its rows on disk and unreachable, and the deliverable carried a real
#: `C:\\Users\\<name>\\...` into a customer's hands.
#:
#: ⚠️ **This is a DESIGN DECISION, not a workaround, and it costs something: the package does not
#: refresh until it is BOUND.** Power Query rejects a relative `File.Contents` argument outright, so
#: there is no portable literal that refreshes anywhere; the three available designs were (a) embed
#: the builder's path - rejected, that is the defect, (b) embed the DESTINATION's path, which the
#: builder does not know, or (c) ship a placeholder and make binding a step of CONSUMING the
#: package. (c) is chosen, and it is this repo's own committed convention already - `<REPO_ROOT>` in
#: `set_data_folder.py`, localized after clone, CI-gated by `--check`. The binder is the same script
#: (`--package`), the README leads with it, and `package-manifest.json` records `binding.state`, so
#: an unbound package is a LOUD, machine-checkable state rather than a plausible-looking path that
#: silently resolves to nothing on the recipient's machine.
PACKAGE_ROOT_TOKEN = "<PACKAGE_ROOT>"

#: What replaces a source literal this packager could not ship. A handover package is handed to a
#: customer, so leaving `C:\\Users\\<builder>\\...` in the shipped TMDL is both a privacy leak and a
#: lie - those bytes are not in the package and that path names nobody's machine but the builder's.
#: The token cannot resolve anywhere, which is the honest state, and it is greppable.
#:
#: ⚠️ **A UNC literal is deliberately NOT neutralized** (:func:`_host_local`). It names a share on a
#: network, not a directory on the packaging host, so the recipient may well be able to read it -
#: and this packager never probed it (see :func:`_classify_source`), so it has no evidence that it is
#: unavailable. Destroying a configuration that works at the customer is not an improvement on
#: shipping one that does not. Those are recorded as `data_sources.retained_network` instead.
UNAVAILABLE_TOKEN = "<UNAVAILABLE_SOURCE>"

#: The command that binds a package to wherever it now lives. Written into the README and into
#: `package-manifest.json` so the state and its remedy travel with the artifact.
BIND_COMMAND = "python scripts/set_data_folder.py --package <path-to-this-folder>"

#: This script's exit codes, named so a caller never has to read a bare integer. The table in the
#: module docstring is the contract; these are the same numbers.
#:
#: ⚠️ **5 and 6 are allocated to two DIFFERENT refusals, and the difference is the whole point.**
#: 5 is "a unit raised and nothing was written for it" - a definite contradiction this packager
#: measured (a declared digest that does not match the bytes, a unit name that escapes `--out`, a
#: host path that could not be contained). 6 is "an input that had to be read could not be
#: assessed", which is not a verdict about the package at all. Collapsing them re-creates the defect
#: class this repo keeps re-finding: unassessable input reaching a clean - or a merely-incomplete -
#: verdict. 5 also deliberately matches PR #487's `EXIT_UNIT_FAILED`, which this branch is the base
#: of, so the two do not have to be reconciled after the fact.
EXIT_OK = 0
EXIT_NO_WORKING_COPY = 1
EXIT_USAGE = 2
EXIT_EDITS_REFUSED = 3
EXIT_NOT_SELF_CONTAINED = 4
EXIT_UNIT_FAILED = 5
EXIT_CANNOT_ASSESS = 6

#: The name of the manifest that records what a package contains, INCLUDING the per-file digest that
#: makes an agent's edit to the canonical `fabric/` tree detectable on the next run. Excluded from
#: its own digest, because it is written last and would otherwise never match itself.
MANIFEST_NAME = "package-manifest.json"

#: Refuse to copy a single source larger than this, rather than silently turning a handover folder
#: into a data lake. Measured on estate run 408 the largest referenced extract is 1.33 MB and the
#: whole estate is 11.2 MB, so nothing observed comes close - the ceiling exists so that an
#: unbounded case becomes a LOUD, recorded omission instead of an unnoticed multi-GB copy.
MAX_DATA_BYTES = 256 * 1024 * 1024

#: The render legs an oracle view may claim, in the order `reference_evidence._oracle_leg` reads them.
RENDER_LEGS = ("image", "svg", "pdf")
#: Marks a leg the packager refused to copy. Anything other than "ok" makes the gate skip it.
OMITTED_STATUS = "omitted_by_packager"
#: What replaces a refused leg's `path`. The declared string is attacker-controlled - the oracle
#: manifest is written by a separate tool against a live server - so it is never echoed back into the
#: packaged manifest or into `handover.md`, which would re-open the exfiltration channel one level
#: down: the bytes would not be copied, but the absolute path would still ship.
REFUSED_PATH = "<refused-by-packager>"
KIND_DIRS = (KIND_DASHBOARD, KIND_WORKSHEET, KIND_UNKNOWN)
_LUID_PREFIX = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_")
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
#: Well short of any filesystem limit: `check_path_ceiling.py` gates the WHOLE path, and these names
#: sit under `<out>/<Unit>/oracle/<kind>/images/`, which is already five segments deep.
_MAX_OBJECT_NAME = 60

KIND_WORKBOOK = "workbook"
KIND_DATASOURCE = "datasource"
KIND_UNCLASSIFIED = "unclassified"


class PackagingError(RuntimeError):
    """An invariant this packager holds was violated, so nothing is shipped rather than something wrong.

    Every use is a TRIPWIRE behind a rule that already prevents the condition - two sources landing
    on one packaged path, or a parameter declared twice. Prevention is the fix; this exists so that
    a future edit which re-opens one fails loudly at packaging time instead of shipping a package
    whose partitions read another table's rows or whose model AMO refuses to load.
    """


class PackageEditsRefused(PackagingError):
    """Repackaging would discard edits made in the package - the tree this packager calls canonical.

    Carries the changed paths (or the reason they could not be established) so the CLI can name them
    rather than saying "something changed".
    """

    def __init__(self, unit: str, package: Path, changed: list[str], reason: str | None) -> None:
        self.unit, self.package, self.changed, self.reason = unit, package, changed, reason
        detail = reason or (
            f"{len(changed)} file(s) differ from what packaging wrote: {', '.join(changed[:5])}"
            + (" ..." if len(changed) > 5 else "")
        )
        super().__init__(
            f"refusing to repackage {unit}: {package} is the canonical place to edit, and {detail}. "
            "Re-run with --discard-package-edits to overwrite it, or move the package aside first."
        )


class PackagePathTooLong(PackagingError):
    """Assembling this unit here would produce a path Power BI Desktop refuses to open (#476).

    Raised BEFORE anything is written. The failure it replaces was `[WinError 206] The filename or
    extension is too long`, thrown mid-assembly 29 units into a 47-unit estate, naming no path, no
    limit and no remedy. Carries the measured :class:`PathBudget` so the message can name all three.
    """

    def __init__(self, budget: PathBudget) -> None:
        self.budget = budget
        super().__init__(render_path_budget(budget))


class UnassessableInput(PackagingError):
    """An input this packager HAD to read to package a unit exists but could not be assessed.

    ⚠️ **Distinct from every other refusal on purpose.** "I could not read it" is not "it is fine"
    and it is not "it is broken" - it is the third state this repository keeps losing. Measured on
    this branch before the fix: a truncated `report.json` produced `exit 0  OK Book [unclassified]`
    with no notes at all, a truncated handover slice produced `exit 0` with the slice simply absent
    from the package, and a workbook whose source asset had been deleted produced `exit 0  OK Book`
    - three different unreadable inputs, one clean verdict.

    Nothing is written for the unit: assembly happens in a staging directory that is removed on the
    way out, so a previously-good package at the same path survives untouched rather than being
    replaced by one built from input nobody could read.
    """

    def __init__(self, unit: str, reasons: list[str]) -> None:
        self.unit, self.reasons = unit, reasons
        super().__init__(
            f"cannot assess {unit}: " + "; ".join(reasons) + ". Nothing was packaged for it - a package "
            "built from input that could not be read would carry a verdict nobody can stand behind."
        )


class UnsafeUnitName(PackagingError):
    """A unit name that would write outside ``--out``, so it is refused before anything is created.

    ⚠️ **A unit name is SOURCE-CONTROLLED input, not a label we chose.** :func:`bundle_units` takes
    it from the engine's `report.json` (`workbooks[].name`) and from `pbip/` directory names, both
    of which come from the customer's Tableau estate. Measured on this branch before the fix: a
    workbook named `..\\escaped-package` wrote a full package to `<out>/../escaped-package` - outside
    the directory the operator named, with `written_is_inside_out = false` and a zero stderr.

    Refused rather than sanitized: silently rewriting `..\\x` to `_x` would package a unit under a
    name that matches nothing in the bundle, and every later join - handover slice, oracle
    attribution, `promote_unit.py`'s manifest kind - is keyed by that name.
    """

    def __init__(self, unit: str, reason: str) -> None:
        self.unit, self.reason = unit, reason
        super().__init__(
            f"refusing to package {unit!r}: {reason}. A unit name must be a single path component, so "
            "that a name taken from the customer's own workbook titles cannot choose where this "
            "packager writes."
        )


# --------------------------------------------------------------------------------------------
# reading the bundle
# --------------------------------------------------------------------------------------------


def read_json(path: Path) -> Any:
    """Parse a JSON file, or return None when it is absent or unreadable.

    ⚠️ **This collapses ABSENT and UNREADABLE, which is the right answer for a caller that treats
    both as "no data" and the wrong one for a caller that must refuse.** Use :func:`read_json_checked`
    wherever a missing file is a legitimate shape but a corrupt one is not - which is every input
    named in :func:`_unassessable_inputs`.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def read_json_checked(path: Path) -> tuple[Any, str | None]:
    """`(payload, reason it could not be assessed)` - absence and corruption told apart.

    A file that is not there returns `(None, None)`: the caller decides whether an absence is
    legitimate, and for most of these inputs it is (a datasource unit has no oracle, four workbooks
    in the reference estate have no `pbip/` working copy). A file that IS there and cannot be parsed
    returns a reason, and every caller that reads one of these must treat that as blocking - the
    packager cannot know what the file said, so it cannot honestly report on what it packaged.
    """
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"{path.name} is present but is not readable JSON ({type(exc).__name__})"
    except OSError as exc:
        return None, f"{path.name} is present but could not be read ({type(exc).__name__})"


def write_json(path: Path, payload: Any) -> None:
    """Write pretty JSON, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256_of(path: Path | None) -> str | None:
    """sha256 of a file, or None when it is absent or cannot be read."""
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def discover_dir(bundle: Path, names: tuple[str, ...]) -> Path | None:
    """First existing directory named in ``names``, looked for beside and inside the bundle."""
    for base in (bundle, bundle.parent):
        for name in names:
            candidate = base / name
            if candidate.is_dir():
                return candidate.resolve()
    return None


def engine_unit_names(engine_report: Any) -> tuple[list[str], list[str]]:
    """`(workbook names, datasource names)` EXACTLY as the engine wrote them, as lists.

    Lists rather than sets because multiplicity is itself a finding: two genuinely distinct workbooks
    whose names differ only by whitespace must not collapse into one key
    (`check_reference_readiness._unit_names`).
    """
    report = engine_report if isinstance(engine_report, dict) else {}
    return (
        [str(x["name"]) for x in report.get("workbooks") or [] if isinstance(x, dict) and x.get("name")],
        [str(x["name"]) for x in report.get("datasources") or [] if isinstance(x, dict) and x.get("name")],
    )


def unit_kind(unit: str, workbooks: list[str], datasources: list[str]) -> str:
    """How the ENGINE classifies this unit. Never inferred from the filesystem.

    Every `pbip/<Unit>/` folder in a real 2.339.0 estate run carries BOTH a `.Report` and a
    `.SemanticModel` - measured on all 62, datasource-only units included - so the filesystem cannot
    answer this question and only `report.json` can. `check_reference_readiness._datasource_only`
    makes the same call for the same reason.
    """
    if unit in workbooks:
        return KIND_WORKBOOK
    if unit in datasources:
        return KIND_DATASOURCE
    return KIND_UNCLASSIFIED


def bundle_units(bundle: Path) -> list[str]:
    """Every unit this bundle is ACCOUNTABLE for - the engine's own lists PLUS its working copies.

    Deliberately NOT just `pbip/`. Measured on a real 2.339.0 estate run: `report.json` lists 48
    workbooks but only 44 have a `pbip/<Unit>/` working copy, and `check_reference_readiness` reports
    each of the other four as a FINDING - "the engine lists this workbook but no report ships for it".
    Deriving the unit list from the filesystem alone dropped all four silently, which is the same
    class of defect this packaging exists to remove. They still package usefully: all four have a
    handover slice, a source asset and oracle renders; what they lack is the engine output, and
    `packaged: false` says so.

    Conversely `pbip/` holds units `report.json` does not name at all (the engine disambiguated two
    workbooks that share a name onto `Seed_-_R_D_2` / `_3`), so neither source is a superset.
    """
    engine_report = read_json(bundle / "report.json")
    workbooks, datasources = engine_unit_names(engine_report)
    pbip = bundle / "pbip"
    folders = [path.name for path in pbip.iterdir() if path.is_dir()] if pbip.is_dir() else []
    return sorted(set(folders) | set(workbooks) | set(datasources))


# --------------------------------------------------------------------------------------------
# the source asset, and the workbook identity that attributes renders to it
# --------------------------------------------------------------------------------------------


def resolve_asset(bundle: Path, unit: str, handover: Any, assets_dir: Path | None) -> tuple[Path | None, str]:
    """`(asset path, how it was resolved)` for the Tableau source behind ``unit``.

    Order mirrors `check_reference_readiness.resolve_source`: the handover slice's
    `workbook.source_id` (a run-root-relative path, so only its basename is portable), then
    `input_manifest.json`'s staged asset whose stem matches the unit name.

    ⚠️ **The basename is extracted with BOTH separators, never `Path(...).name`.** A `source_id` is
    written by whichever machine ran the harvest, so a Windows-separated
    `_runs\\999-x\\assets\\minimal.twb` reaching a POSIX packaging host has no separators `Path`
    recognises: its "name" is the whole string, nothing matches, and a source asset that IS present
    resolves to `unresolved` - both gates then report CANNOT_ESTABLISH (round-2 finding 2).

    ⚠️ **`staged_input_path` is interpreted in ITS OWN flavour, never the host's** - the same
    hazard, and the same fix, as :func:`_classify_source`. `Path` is the host's: on Windows
    `Path("/mnt/share/elsewhere/Book.twb")` is resolved against the CURRENT DRIVE, so a POSIX
    literal from a Linux harvest matched `C:\\mnt\\share\\elsewhere\\Book.twb` and those unrelated
    bytes were copied into the package as the customer's workbook - measured, with a clean exit 0
    and a manifest digest that said otherwise. A foreign-flavour staged path is skipped, and the
    name-based candidates below still resolve the asset where it actually is.
    """
    workbook = handover.get("workbook") if isinstance(handover, dict) else None
    source_id = workbook.get("source_id") if isinstance(workbook, dict) else None
    if isinstance(source_id, str) and source_id.strip():
        name = leaf(source_id)
        for base in (assets_dir, bundle / "assets", bundle.parent / "assets"):
            if base is not None and (base / name).is_file():
                return (base / name), "handover.workbook.source_id"

    manifest = read_json(bundle / "input_manifest.json")
    staged_assets = manifest.get("assets") or [] if isinstance(manifest, dict) else []
    for asset in staged_assets:
        if not isinstance(asset, dict) or PurePosixPath(leaf(str(asset.get("name") or ""))).stem != unit:
            continue
        staged = asset.get("staged_input_path")
        candidates = [Path(str(staged))] if staged and is_host_native(str(staged)) else []
        candidates += [
            base / str(asset.get("name"))
            for base in (assets_dir, bundle / "assets", bundle.parent / "assets")
            if base is not None
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate, "input_manifest.staged_input_path"
    return None, "unresolved"


def declared_asset_digest(bundle: Path, name: str) -> str | None:
    """The sha256 `input_manifest.json` declares for the asset called ``name``, if it declares one.

    Matched on the manifest entry's own basename, in both flavours, for the same reason
    :func:`resolve_asset` is: the manifest is written by whichever machine ran the harvest.
    """
    manifest = read_json(bundle / "input_manifest.json")
    for asset in (manifest.get("assets") or []) if isinstance(manifest, dict) else []:
        if not isinstance(asset, dict) or leaf(str(asset.get("name") or "")) != name:
            continue
        declared = asset.get("sha256")
        if isinstance(declared, str) and declared.strip():
            return declared.strip().lower()
    return None


def assert_declared_digest(unit: str, bundle: Path, asset: Path, route: str) -> None:
    """Refuse when the resolved source does not hash to what `input_manifest.json` declared for it.

    ⚠️ **This digest is the ONLY thing that can catch a resolution that found the wrong file**, and
    until now nothing consulted it. Measured on this branch: a `staged_input_path` of
    `/mnt/share/elsewhere/Book.twb` was reinterpreted by the Windows host against the current drive,
    a completely unrelated workbook was copied into the package as the customer's source, and the
    run exited **0** - with the manifest's own `sha256` (`5d65d756…`) sitting one field away from the
    bytes that actually shipped (`54a6036a…`).

    The flavour fix in :func:`resolve_asset` closes the route that produced that specific wrong file;
    this closes the CLASS. Any future resolution order, any harvest that renames an asset, any
    operator pointing `--assets` at a stale directory lands here, and lands closed: nothing is
    written for the unit, because a package whose `assets/` holds the wrong workbook silently
    invalidates every page verdict both gates then produce from it.

    A manifest that declares no digest for the asset is not a failure - `sha256` is optional in the
    shapes this repository has measured, and an absent declaration is an absence, not a mismatch.
    """
    declared = declared_asset_digest(bundle, asset.name)
    if declared is None:
        return
    actual = sha256_of(asset)
    if actual is not None and actual.lower() == declared:
        return
    raise PackagingError(
        f"refusing to package {unit}: the source resolved via {route} does not match the digest "
        f"input_manifest.json declares for {asset.name} (declared {declared[:16]}..., resolved "
        f"{(actual or 'unreadable')[:16]}...). Those are different bytes, so every page verdict a "
        "gate computes from this package would be about the wrong workbook; nothing was written."
    )


def scope_provenance(provenance: Any, asset_sha: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(unit-scoped source-provenance.json, matching entries)` for one asset's sha256.

    Scoped by CONTENT, not by filename: `source-provenance.json` is keyed by `input.sha256` and
    `check_reference_readiness._provenance_luid` looks the unit up the same way, so a scoped copy that
    kept the wrong entry would hand the gate a LUID for a different workbook.

    ⚠️ **Round-3 finding: this was still a denylist**, copying every top-level key except `inputs`
    and every field inside a retained entry - so `future_scan_root` and
    `inputs[].origin.future_source_path` both shipped. It now goes through the same `project()` as
    every other manifest, against a spec that is exactly the three fields the gate reads
    (`input.sha256`, `origin.match`, `origin.workbook_luid`). `workbook_name` and `project` are
    deliberately NOT carried: on a foreign entry they are the identity channel itself.

    The RETURNED entries are the unprojected ones, because `workbook_identity` adjudicates on
    `origin.workbook_name` before deciding whether anything may be attributed. What ships is the
    projected, adjudicated subset - see `shippable_provenance`.
    """
    payload = provenance if isinstance(provenance, dict) else {}
    entries = [
        entry
        for entry in payload.get("inputs") or []
        if isinstance(entry, dict)
        and isinstance(entry.get("input"), dict)
        and asset_sha is not None
        and entry["input"].get("sha256") == asset_sha
    ]
    return payload, entries


def filename_luid(asset: Path | None) -> str | None:
    """The LUID `harvest_estate_assets.py` prefixes onto a downloaded asset filename.

    ⚠️ **This is NOT usable as a workbook identity on its own, and is never used as one here.** The
    harvester prefixes a `.tds`/`.tdsx` with its **datasource** LUID, which lives in a different
    identity namespace from `oracle-manifest.json`'s `workbook_luid`. Measured on the 67-unit
    reference estate: **all 19** units that carry a filename LUID with no provenance entry are
    datasources. Promoting it would feed a datasource LUID into a workbook-LUID comparison - a
    category error that buys nothing (those 19 have no views) and fails OPEN if the namespaces ever
    collide.

    It is therefore only a CROSS-CHECK against a provenance LUID, and that comparison is structurally
    scoped to workbooks already: `stamp_tableau_provenance.py` stamps workbooks only, so a datasource
    never reaches it.
    """
    if asset is None:
        return None
    found = _LUID_PREFIX.match(asset.name)
    return found.group(1) if found else None


def workbook_identity(entries: list[dict[str, Any]], asset: Path | None) -> dict[str, Any]:
    """The workbook LUID this unit's renders may be attributed to, or a refusal naming why.

    Returns `{"luid", "match", "workbook_name", "reason"}`. `luid` is None whenever the identity is
    not established, and `reason` then says which precondition failed - which is the whole verdict,
    because a unit with no workbook LUID attributes nothing at all (see :func:`select_views`).

    One source, one cross-check: `source-provenance.json` keyed by the asset's **sha256**, checked
    against the asset filename's LUID prefix when there is one. A disagreement fails closed rather
    than picking whichever was read first.
    """
    stamped = filename_luid(asset)
    luids = {
        str(entry["origin"]["workbook_luid"])
        for entry in entries
        if isinstance(entry.get("origin"), dict) and entry["origin"].get("workbook_luid")
    }
    if len(luids) > 1:
        return _no_identity(f"source-provenance.json maps this asset's bytes onto {len(luids)} workbook LUIDs")
    if not luids:
        return _no_identity("no source-provenance.json entry for this asset's bytes")

    luid = next(iter(luids))
    if stamped and stamped.casefold() != luid.casefold():
        return _no_identity(
            f"asset filename declares LUID {stamped} but source-provenance.json records {luid} "
            "for these bytes - two identities that disagree are LESS evidence than none"
        )
    origin = next((entry["origin"] for entry in entries if isinstance(entry.get("origin"), dict)), {})
    return {
        "luid": luid,
        "match": origin.get("match"),
        "workbook_name": origin.get("workbook_name"),
        "reason": None,
    }


def _no_identity(reason: str) -> dict[str, Any]:
    """No usable workbook identity, carrying the precondition that failed."""
    return {"luid": None, "match": None, "workbook_name": None, "reason": reason}


# --------------------------------------------------------------------------------------------
# the oracle subset
# --------------------------------------------------------------------------------------------


def select_views(manifest: Any, identity: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """`(this unit's views, route)` from a flat oracle manifest - or `([], reason)`.

    **ONE route: `workbook_luid`.** A display name is not an identity - two projects can hold
    workbooks with the same name, which is the exact ambiguity `_runs/<NNN>-<slug>/` numbering exists
    to avoid elsewhere in this repo (issue #450).

    ⚠️ There WAS a second route here - an exact `workbook_name == <unit>` match guarded by a
    single-owner check - added because `reference_evidence.Evidence.is_for` has the same fallback.
    It is deleted rather than further guarded. Two measurements decided it:

    * it fired **0 times in 67 units** on the reference estate (46 resolve by `workbook_luid`, 21
      attribute nothing), so it was untested-in-production surface with no measured benefit; and
    * a name route is the same class as #450, where `check_unit`'s workbook guard reads a field the
      capture does not write and is inert on **360 of 360** real records - failing OPEN, admitting a
      foreign workbook's render as this unit's evidence.

    Mirroring a fallback that a sibling gate is being fixed to distrust is not a reason to keep it.
    """
    views = [view for view in (manifest or {}).get("views") or [] if isinstance(view, dict)]
    if not views:
        return [], "no views in oracle manifest"

    luid = identity.get("luid")
    if not luid:
        return [], (
            f"no workbook LUID for this unit ({identity.get('reason')}), so no render can be "
            "attributed - a display name is not an identity (#450)"
        )
    picked = [view for view in views if str(view.get("workbook_luid") or "").casefold() == luid.casefold()]
    if not picked:
        return [], f"no oracle view carries workbook_luid {luid}"
    return picked, "workbook_luid"


def view_kind(view: dict[str, Any]) -> str:
    """`dashboard`/`worksheet` when the capture RESOLVED the type, else `unknown`.

    `content_url` is `<wb>/sheets/<view>` for both kinds and is not a discriminator;
    `capture_tableau_oracle.py`'s type resolver is non-fatal, so `unknown` is a real value that must
    be carried and marked rather than defaulted.
    """
    declared = view.get("view_type")
    if isinstance(declared, str) and declared.strip().casefold() in (KIND_DASHBOARD, KIND_WORKSHEET):
        return declared.strip().casefold()
    return KIND_UNKNOWN


def object_filename(name: str, luid: str, taken: set[str]) -> str:
    """A filesystem-safe, collision-free stem for one captured object."""
    cleaned = _UNSAFE.sub("_", str(name or "")).strip(" ._") or "view"
    cleaned = cleaned[:_MAX_OBJECT_NAME].strip(" ._") or "view"
    stem = cleaned
    if stem.casefold() in taken:
        stem = f"{cleaned}__{str(luid)[:8]}"
    suffix = 2
    while stem.casefold() in taken:
        stem = f"{cleaned}__{str(luid)[:8]}_{suffix}"
        suffix += 1
    taken.add(stem.casefold())
    return stem


def _resolve_capture_file(oracle_root: Path, declared: str) -> tuple[Path | None, str | None]:
    """`(resolved file, refusal reason)` for a capture-relative path the MANIFEST asked us to copy.

    ⚠️ **The oracle manifest is UNTRUSTED INPUT.** It is written by a separate tool against a live
    Tableau server, and this function is the boundary where that matters. Round-3 review measured
    both exploits against the previous `source_dir / leg["path"]`:

    * `"../outside-secret.png"` - copied byte-identically into `oracle/worksheet/images/Sales.png`;
    * an absolute path - copied, AND written verbatim into the packaged manifest.

    So the check is containment, not sanitisation of the string: reject an absolute or drive-relative
    path outright, resolve **strictly** (which follows symlinks and normalises `..`), and require the
    result to stay under the resolved capture root. Resolving both sides is what closes the symlink
    route - a link inside the capture pointing outside it normalises to an outside path, and
    comparing unresolved strings would not see that.
    """
    if not declared:
        return None, "capture declares an empty path"
    candidate = Path(declared)
    if candidate.is_absolute() or candidate.drive or declared.startswith(("\\\\", "/")):
        return None, f"capture declares a non-relative path ({REFUSED_PATH}) - refused"
    try:
        root = oracle_root.resolve(strict=True)
        resolved = (root / candidate).resolve(strict=True)
    except OSError:
        return None, "capture path does not resolve to a file"
    if not resolved.is_relative_to(root):
        return None, f"capture path escapes the capture root ({REFUSED_PATH}) - refused"
    if not resolved.is_file():
        return None, "capture path does not resolve to a file"
    return resolved, None


def _copy_leg(
    source_dir: Path, dest_dir: Path, leg: Any, target: Path, rel_prefix: str
) -> tuple[dict[str, Any] | None, str | None]:
    """`(rewritten leg, omission reason)` for one render or data leg."""
    if not isinstance(leg, dict):
        return None, None
    if leg.get("status") != "ok" or not isinstance(leg.get("path"), str):
        return dict(leg), None
    origin, refusal = _resolve_capture_file(source_dir, leg["path"])
    if origin is None:
        rewritten = dict(leg)
        rewritten["status"] = OMITTED_STATUS
        rewritten["packaging_reason"] = refusal or "capture path unusable"
        rewritten["path"] = REFUSED_PATH
        return rewritten, rewritten["packaging_reason"]
    destination = dest_dir / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origin, destination)
    rewritten = dict(leg)
    rewritten["path"] = f"{rel_prefix}/{target.name}"
    # Normalised and capture-RELATIVE, never the declared string: the declared form is attacker-
    # controlled and was how an absolute host path reached the packaged manifest.
    rewritten["packaged_from"] = origin.resolve().relative_to(source_dir.resolve()).as_posix()
    return rewritten, None


def package_oracle(  # pylint: disable=too-many-locals
    views: list[dict[str, Any]], manifest: Any, oracle_dir: Path, dest: Path, unit: str = ""
) -> dict[str, Any]:
    """Copy this unit's renders and numbers into `<dest>`, type-separated, and rewrite the manifest.

    Bytes are copied verbatim, so every `sha256`/`bytes` the capture recorded still verifies -
    `reference_evidence.render_facts` checks exactly those, and a re-encoded copy would be rejected.
    Only `path` changes.
    """
    taken: set[str] = set()
    packaged: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []

    for view in views:
        kind = view_kind(view)
        luid = str(view.get("view_luid") or "")
        stem = object_filename(str(view.get("view_name") or view.get("view_url_name") or ""), luid, taken)
        record = dict(view)
        images: list[str] = []

        for leg_name in RENDER_LEGS:
            suffix = Path(str((view.get(leg_name) or {}).get("path") or "")).suffix or f".{leg_name}"
            rewritten, reason = _copy_leg(
                oracle_dir, dest, view.get(leg_name), Path(kind) / "images" / f"{stem}{suffix}", f"{kind}/images"
            )
            if rewritten is not None:
                record[leg_name] = rewritten
            if reason:
                omissions.append({"view_luid": luid, "leg": leg_name, "reason": reason})
            elif rewritten is not None and rewritten.get("status") == "ok":
                images.append(str(rewritten["path"]))

        rewritten, reason = _copy_leg(
            oracle_dir, dest, view.get("data"), Path(kind) / "data" / f"{stem}.csv", f"{kind}/data"
        )
        if rewritten is not None:
            record["data"] = rewritten
        if reason:
            omissions.append({"view_luid": luid, "leg": "data", "reason": reason})
        numbers = (
            str(rewritten["path"]) if rewritten is not None and rewritten.get("status") == "ok" and not reason else None
        )

        record["packaged_object_stem"] = stem
        packaged.append(record)
        objects.append(
            {
                "name": view.get("view_name"),
                "view_luid": luid,
                "view_type": kind,
                "declared_view_type": view.get("view_type"),
                # Visual and numeric are kept APART, not merged into one file list. The operator asked
                # for both and they answer different questions - and merging them mislabels a
                # data-only view as a render, which `grep -c ^ORACLE_RENDER` would then over-report.
                "images": images,
                "data": numbers,
            }
        )

    scoped = _scope_oracle_manifest(manifest, packaged, objects, unit)
    write_json(dest / "oracle-manifest.json", scoped)
    return {"objects": objects, "omissions": omissions}


def _scope_oracle_manifest(
    manifest: Any, packaged: list[dict[str, Any]], objects: list[dict[str, Any]], unit: str
) -> dict[str, Any]:
    """The capture manifest rebuilt for ONE unit: allowlisted, and every count RECOMPUTED.

    ⚠️ **Round-2 finding: this was a denylist too** - it copied every manifest key except `views`,
    so a package holding 23 views shipped **22 fields byte-identical to the 360-view estate
    manifest**. `view_count` was rewritten to 23 while `view_types` still totalled 360
    (`dashboard: 60, worksheet: 300`), beside `captured_complete: 312` and `failed: 47`. A consumer
    reading those numbers is reading the estate and being told it is this unit.

    Two different remedies, because the fields fail differently:

    * **counts are RECOMPUTED from the packaged views**, not dropped - `view_count`, `view_types` and
      the per-leg `*_ok` tallies all describe what actually shipped, and the leg tallies are taken
      AFTER copying, so a leg the packager refused is not counted as present.
    * **estate-run and foreign-identity fields are DROPPED** - `elapsed_sec`, `total_retries`,
      `total_reauths` describe the whole capture run; `captured_complete`, `failed`, `data_empty`,
      `credential_blocked`, `reference_missing`, `reference_required` and
      `credential_scrubbed_at_sink` encode capture-time semantics this packager cannot reconstruct
      faithfully, and inventing a per-unit definition under an existing name would be worse than
      omitting it.
    """
    narrowed = dict(manifest if isinstance(manifest, dict) else {})
    narrowed["views"] = packaged
    scoped, dropped = project(narrowed, ORACLE_MANIFEST_ALLOW)

    shipped = scoped.get("views") or []
    counts = dict.fromkeys(KIND_DIRS, 0)
    for obj in objects:
        counts[obj["view_type"]] = counts.get(obj["view_type"], 0) + 1
    scoped["view_count"] = len(shipped)
    scoped["view_types"] = counts
    for leg in (*RENDER_LEGS, "data"):
        scoped[f"{leg}_ok"] = sum(
            1 for view in shipped if isinstance(view.get(leg), dict) and view[leg].get("status") == "ok"
        )
    return stamp_scope(scoped, unit, dropped, "oracle-manifest.json views filtered to this unit, counts recomputed")


# --------------------------------------------------------------------------------------------
# the greppable handover
# --------------------------------------------------------------------------------------------


def visual_pages(report_dir: Path | None) -> dict[str, str]:
    """`{visual id: page id}` from the PBIR tree, so an emptied visual can name its page.

    The engine's `pbip_ref_drops[]` rows carry only `visual` - measured, the three keys are
    `dropped`, `emptied`, `visual` across all 28 rows of a real estate run - and a bare
    `v-page-Dashboard06ca9874` is not something an operator can act on.
    """
    if report_dir is None or not report_dir.is_dir():
        return {}
    pages = report_dir / "definition" / "pages"
    return {
        visual.name: page.name
        for page in sorted(pages.iterdir() if pages.is_dir() else [])
        if (page / "visuals").is_dir()
        for visual in sorted((page / "visuals").iterdir())
        if visual.is_dir()
    }


def _field(value: Any) -> str:
    """One `key=value` field's value: single-line, never empty, so a line always has all its fields."""
    text = " ".join(str(value).split()) if value not in (None, "") else "-"
    return text.replace("|", "/")


def handover_lines(workbook: dict[str, Any], pages: dict[str, str]) -> list[str]:
    """The flat, one-finding-per-line body, EMPTIED VISUALS FIRST.

    `read_handover.py` documents why they lead: an emptied visual renders blank on a report that
    validates clean, and nothing else in the toolkit surfaces them - 15 sat unremarked beside a
    170-item worklist. Every line is `PREFIX key=value ...` so `grep '^EMPTIED_VISUAL'` is the whole
    interface.
    """
    lines: list[str] = []
    for drop in read_handover._emptied_visuals(workbook):  # pylint: disable=protected-access
        visual = str(drop.get("visual") or "")
        dropped = "; ".join(str(item) for item in drop.get("dropped") or []) or "-"
        lines.append(
            f"EMPTIED_VISUAL page={_field(pages.get(visual, 'unknown'))} visual={_field(visual)} "
            f"dropped={_field(dropped)}"
        )

    for request in read_handover.requests_of(workbook):
        lines.append(
            f"STUB_MEASURE table={_field(request.get('target_table'))} name={_field(request.get('name'))} "
            f"role={_field(request.get('role'))} category={_field(request.get('category'))} "
            f"blocked_by={_field(', '.join(str(x) for x in request.get('blocked_by') or []) or '-')} "
            f"formula={_field(request.get('formula'))}"
        )

    for item in read_handover.report_items_of(workbook):
        lines.append(
            f"WORKLIST severity={_field(item.get('severity'))} category={_field(item.get('category'))} "
            f"page={_field(item.get('page_display') or item.get('page'))} visual={_field(item.get('visual'))} "
            f"worksheet={_field(item.get('worksheet'))} reason={_field(item.get('reason'))} "
            f"remediation={_field(item.get('remediation'))}"
        )

    for row in workbook.get("visuals_projecting_stub_measures") or []:
        if isinstance(row, dict):
            lines.append(
                f"STUB_PROJECTED page={_field(row.get('page'))} visual={_field(row.get('visual'))} "
                f"measure={_field(row.get('measure'))}"
            )

    status, warnings = read_handover.pbip_warning_status(workbook)
    for warning in warnings:
        lines.append(f"PBIP_WARNING text={_field(warning)}")
    if status not in (read_handover.PBIP_WARNING_PRESENT, read_handover.PBIP_WARNING_NONE):
        lines.append(f"PBIP_WARNING_UNRECORDED status={_field(status)}")

    for row in workbook.get("viz_fidelity") or []:
        if isinstance(row, dict) and row.get("evidence") != "emitted+linted":
            lines.append(
                f"FIDELITY evidence={_field(row.get('evidence'))} tier={_field(row.get('tier'))} "
                f"worksheet={_field(row.get('worksheet'))} visual_type={_field(row.get('visual_type'))} "
                f"reason={_field(row.get('reason'))}"
            )
    return lines


def render_handover(result: dict[str, Any], workbook: dict[str, Any] | None, pages: dict[str, str]) -> str:
    """The whole `handover.md`: a header an agent can read, then one finding per line."""
    identity = result["workbook_identity"]
    head = [
        f"# handover: {result['unit']}",
        "#",
        "# One finding per line, `PREFIX key=value ...`. Grep a prefix; do not parse this as prose.",
        "# Prefixes, in the order they appear: EMPTIED_VISUAL (blank on a report that validates "
        "clean - fix first), STUB_MEASURE, WORKLIST, STUB_PROJECTED, PBIP_WARNING, FIDELITY,",
        "#   then the reference inventory: ORACLE_ATTRIBUTION, ORACLE_RENDER, ORACLE_NO_RENDER, "
        "UNTYPED_RENDER, ORACLE_OMISSION, PACKAGE_NOTE.",
        "#",
        f"UNIT name={_field(result['unit'])} kind={_field(result['kind'])} engine={_field(result.get('engine'))}",
        f"PACKAGE spec={_field(result['artifacts'].get('migration_spec'))} "
        f"source={_field(result['artifacts'].get('asset'))} "
        f"report={_field(result['artifacts'].get('report'))} model={_field(result['artifacts'].get('model'))}",
    ]
    body = handover_lines(workbook, pages) if workbook else ["PACKAGE_NOTE text=no handover slice for this unit"]

    oracle = result["oracle"]
    tail = [
        f"ORACLE_ATTRIBUTION route={_field(oracle.get('route'))} luid={_field(identity.get('luid'))} "
        f"match={_field(identity.get('match'))} views={_field(len(oracle.get('objects') or []))} "
        f"reason={_field(identity.get('reason') or oracle.get('reason'))}"
    ]
    for obj in oracle.get("objects") or []:
        # Three prefixes, not one, and keyed on the IMAGE legs alone: a selected view with no usable
        # render must not be greppable as a render. `grep -c ^ORACLE_RENDER` is the inventory an agent
        # will trust, and counting a data-only view into it over-reports the reference they think they
        # have. `unknown` wins over both, because "I cannot tell what this is a picture of" is the
        # louder fact.
        if obj["view_type"] == KIND_UNKNOWN:
            prefix = "UNTYPED_RENDER"
        else:
            prefix = "ORACLE_RENDER" if obj["images"] else "ORACLE_NO_RENDER"
        tail.append(
            f"{prefix} type={_field(obj['view_type'])} object={_field(obj['name'])} "
            f"luid={_field(obj['view_luid'])} images={_field(', '.join(obj['images']) or 'none')} "
            f"data={_field(obj['data'] or 'none')}"
        )
    for omission in oracle.get("omissions") or []:
        tail.append(
            f"ORACLE_OMISSION view={_field(omission.get('view_luid'))} leg={_field(omission.get('leg'))} "
            f"reason={_field(omission.get('reason'))}"
        )
    for note in result.get("notes") or []:
        tail.append(f"PACKAGE_NOTE text={_field(note)}")
    return "\n".join(head + [""] + body + [""] + tail) + "\n"


README = """# {unit}

Handover package for one migration unit ({kind}). It carries its own rows, its own reference and its
own source - but it is **not bound to a location**, so do this FIRST, wherever this folder now is,
before opening the model. Then the two gates, each of which takes THIS FOLDER'S PATH as its only
argument - a bare unit name is a usage error, never a verdict (exit 2 from
`check_reference_readiness.py`, exit 64 from `check_unit.py`, both with a message on stderr):

    python scripts/set_data_folder.py --package <path-to-this-folder>
    python scripts/check_reference_readiness.py <path-to-this-folder>
    python scripts/check_unit.py <path-to-this-folder>

Why binding is a step rather than something already done for you: Power Query rejects a relative
`File.Contents` argument outright, so a folder parameter has to name an ABSOLUTE directory, and the
machine that built this package cannot know where you will put it. Baking in the builder's own path
is what made a moved package silently unable to reach rows that were sitting right beside it. So the
model reads `{package_root}` and the command above resolves it here; `package-manifest.json`'s
`data_sources.binding` records the state, and re-run it after every move.

A page counts as REBUILT only when its `displayName` EXACTLY equals an expected object's name AND it
ships at least one visual; one that pairs by name with no visual is reported `blank` and FAILS. The
expected set is every dashboard PLUS every worksheet not placed on one.

| path | what it is |
|---|---|
| `handover.md` | every engine finding, one per line, emptied visuals first. **Start here.** |
| `handover/{unit}.json` | the engine's slice for THIS workbook; `python scripts/read_handover.py handover/{unit}.json --viz`. Estate-wide sections are not shipped; absolute host paths are redacted. |
| `fabric/` | the engine WORKING COPY - **edit here**, and when you work from a package THIS tree is canonical; `<bundle>/pbip/` never promotes over it. Re-running `package_unit.py` into this folder REFUSES (exit 3) rather than discarding what you changed - `--discard-package-edits` overrides. Declared-edit tooling (`declare_generated_edit.py`, `--tamper`) is bundle-only. |
| `assets/` | the Tableau source this was built from |
| `data/` | the rows the model imports, shipped with it (#461), reached through a `{package_root}` folder parameter in `expressions.tmdl` - see the binding command above. Absent when nothing was shipped - either the model imports nothing, or a source it names was unavailable when this was packaged, in which case that literal now reads `{unavailable}` rather than a path on the builder's machine and `package-manifest.json`'s `data_sources` says which, one line per source, repeated in `handover.md` as a `PACKAGE_NOTE`. |
| `migration-spec.json` | the parsed source; the expected page set both gates grade against |
| `migration-spec.schema.json` | the CONTRACT `validate_spec.py` enforces. Read it before appending a `limitations_encountered` entry: exactly `item`/`issue`/`severity`/`stage`, `additionalProperties: false`, so one invented field rejects every entry. |
| `oracle/` | this unit's Tableau reference, split `dashboard/` vs `worksheet/` vs `unknown/` (**singular** - the directory is the object kind, not a plural). **`oracle/*/data/*.csv` is the NUMERIC oracle** - exact labels and figures, no OCR and no judgement. Read it first. |
| `report.json` | **gate input, and readable.** The engine's classification of THIS unit - workbook vs datasource - which is what earns a datasource-only unit `NOT_APPLICABLE` instead of a finding. Scoped to this unit. |
| `source-provenance.json` | **gate input.** The only trusted route from this package's asset to a Tableau workbook LUID, keyed by the asset's sha256; `origin.match` decides whether a render can be trusted - see UNFIXABLE below. An entry ships only when attribution was NOT refused (`scope.suppressed_reason`). |
| `engine-output-receipt.json` | **read `engine.version` when a result looks wrong** - it establishes which engine built this, so version drift stays checkable months later. Install paths are not shipped. |
| `package-manifest.json` | what was packaged, and every omission with its reason. Its `contents.files` digest is how a re-run knows this package has been edited and refuses to overwrite it. |

`oracle/` images are **layout/text grade only**: a capture is taken in the view's default state with
no `?vf_` filter pinning, so a visual PASS signed off on one alone is overstated, and it is no claim
of byte-faithfulness - see `ORACLE_ATTRIBUTION ... match=` in `handover.md`, and log the ceiling in
`limitations_encountered`. The `.png` is the only leg you can LOOK at; the `.svg` carries labels and
values as greppable `<text>` elements, except where labels render as paths - zero text is not zero
content.

## UNFIXABLE FROM THIS PACKAGE

`source-provenance.json` can report `origin.match: "name_only"` - local and server bytes may DIFFER,
so an oracle render may depict a different build than `assets/`. Re-stamping needs
`stamp_tableau_provenance.py`, Tableau Server credentials AND the fields `scope.dropped_fields`
strips here: `origin.remote_sha256`, `origin.server`, `origin.site`. Measured consequence: every
emitted page then reads `UNVERIFIABLE - REVISION NOT ESTABLISHED`, so `check_reference_readiness.py`
can NEVER exit 0 from this package alone. Log it and build anyway.
"""


# --------------------------------------------------------------------------------------------
# packaging one unit
# --------------------------------------------------------------------------------------------


def conflicting_evidence_dirs(out_root: Path) -> list[Path]:
    """Evidence directories that would SHADOW every package written under ``out_root``.

    `check_reference_readiness._collect_evidence` looks for `reference/`, `_oracle/` and `oracle/`
    beside the target, beside its parent AND beside its grandparent - so a package at
    `<out>/<Unit>/` also picks up anything at `<out>/` and `<out>/../`. Writing packages inside the
    run directory therefore lets the gate see the packaged subset AND the original flat capture at
    `_runs/<NNN>/oracle/`.

    Measured while writing this file's own fixture: with both visible, every view is matched twice,
    the gate refuses ("2 records share this name once normalized") and all four pages go from
    **ready** to **unverifiable**. That is strictly worse than not packaging at all, and it is
    silent, so it is refused up front rather than documented.
    """
    names = ("reference", "oracle", "_oracle")
    return [base / name for base in (out_root, out_root.parent) for name in names if (base / name).is_dir()]


def _copy_fabric(bundle: Path, unit: str, dest: Path) -> tuple[str | None, str | None]:
    """Copy the engine WORKING COPY into `<dest>/fabric/`; `(report name, model name)`.

    `pbip/<Unit>/` is copied whole so `definition.pbir`'s `byPath` - measured as
    `../<Model>.SemanticModel`, and a model name that differs from the unit name in 27 of 62 units -
    keeps resolving. `reports/` is the engine BASELINE and is never shipped: no model sits beside it.
    """
    source = bundle / "pbip" / unit
    if not source.is_dir():
        return None, None
    shutil.copytree(source, dest / "fabric", dirs_exist_ok=True)
    report = next((path.name for path in sorted((dest / "fabric").iterdir()) if path.name.endswith(".Report")), None)
    model = next(
        (path.name for path in sorted((dest / "fabric").iterdir()) if path.name.endswith(".SemanticModel")), None
    )
    return report, model


def _model_tmdl(dest: Path, model_name: str | None) -> list[Path]:
    """Every `.tmdl` document of the packaged model, or an empty list when there is no model."""
    if not model_name:
        return []
    definition = dest / "fabric" / model_name / "definition"
    return sorted(definition.rglob("*.tmdl")) if definition.is_dir() else []


def _packaged_data_target(source: str, taken: dict[str, str], *, keep_leaf_only: bool = False) -> str:
    """A stable, package-relative home for one referenced source, unique within this package.

    Readable first - `data/<parent folder>/<file name>` keeps a handover folder browsable - but two
    different sources can share both, and the engine's extract paths are exactly that shape
    (`.../<table>/federated_<hash>/Extract_Extract.csv`). A collision is therefore resolved by
    digesting the FULL original path, never by overwriting: two sources landing on one file would
    silently repoint one partition at the other's rows.

    ``keep_leaf_only`` is the folder-parameter case, where the literal already names a directory and
    its own leaf is the meaningful name (`<Unit>.Data`).

    ⚠️ **Uniqueness is judged over the DESTINATION TREE, not over the reservation string** - a
    folder claims everything beneath it. Blind-review finding 1: a folder source containing
    `same/x.csv` reserved `same`, a bare file whose readable home was `same/x.csv` reserved
    `same/x.csv`, the two strings differed so neither looked taken, and the second copy overwrote
    the first on disk. The package then exited 0 with TWO manifest entries for one path and one
    partition reading another table's rows - which `check_datamodel.py` cannot see, because the
    model is structurally perfect. Comparing whole strings closed the same-shape case only; the
    ancestor test closes every combination of the two shapes.
    """
    original = PurePosixPath(source.replace("\\", "/").rstrip("/"))
    name = _UNSAFE.sub("_", original.name)[:_MAX_OBJECT_NAME] or "data"
    if keep_leaf_only:
        candidate = name
    else:
        parent = _UNSAFE.sub("_", original.parent.name)[:_MAX_OBJECT_NAME] or "source"
        candidate = f"{parent}/{name}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    width = 8
    while _destination_taken(candidate, source, taken):
        if width > len(digest):
            raise PackagingError(f"no free packaged destination for {_leaf(source)}")
        candidate = f"{digest[:width]}/{name}"
        width += 8
    taken[candidate] = source
    return candidate


def _destination_taken(candidate: str, source: str, taken: dict[str, str]) -> bool:
    """Whether ``candidate`` collides with a destination another source already claimed.

    A collision is not only an equal path: a FOLDER destination owns its whole subtree, so
    `same/x.csv` collides with `same` in both directions. Compared case-insensitively because the
    package is routinely assembled and opened on Windows, where `Same/X.csv` and `same/x.csv` are
    one file - a case-sensitive dict would have called them distinct and let one overwrite the other.
    """
    key = candidate.casefold()
    for existing, owner in taken.items():
        if owner == source:
            continue
        claimed = existing.casefold()
        if key == claimed or key.startswith(f"{claimed}/") or claimed.startswith(f"{key}/"):
            return True
    return False


def _assert_distinct_destinations(record: dict[str, Any]) -> None:
    """Tripwire: two shipped entries naming one path means a partition reads another table's rows.

    :func:`_packaged_data_target` makes this unreachable. It is asserted anyway because the failure
    it guards is invisible downstream - the model loads, `check_datamodel.py` exits 0, and only the
    NUMBERS are wrong - so a regression must stop packaging rather than be discovered by a customer.
    """
    seen: list[str] = [row["path"].casefold() for row in record["shipped"]]
    duplicates = sorted({row["path"] for row in record["shipped"] if seen.count(row["path"].casefold()) > 1})
    if duplicates:
        raise PackagingError(
            "two sources were packaged onto the same destination, so a partition would read another "
            f"table's rows: {', '.join(duplicates)}"
        )


def _path_verdict(value: str) -> str:
    """`PATH_LITERAL` / `NOT_A_PATH` / `UNCLASSIFIED` for one absolute quoted literal.

    A Windows drive or UNC prefix is unambiguous. A POSIX-absolute literal mostly is NOT: measured on
    estate run 408, 9 POSIX-absolute literals appear and 8 are false positives - a Databricks
    `HttpPath = "/sql/1.0/warehouses/<id>"` in three units, and a bare `"/"` inside a
    `TableauFormula` annotation in two more. A file suffix keeps the one genuine hit (a macOS `.xlsx`
    baked into a source workbook); a TRAILING SEPARATOR is the directory convention and keeps the
    other genuine shape, a folder parameter such as `"/Users/<person>/Data/"`.

    ⚠️ **What is left over is UNCLASSIFIED, never clean.** Requiring a suffix and calling everything
    else "not a path" is what let `SourceFolder = "/Users/<person>/Data/"` through packaging
    unchanged, with no folder shipped and NO omission recorded (blind-review finding 5). The
    remaining shape - POSIX-absolute, no suffix, no trailing separator - genuinely cannot be told
    from a service path without probing, so it gets its own verdict and its own recorded reason.

    ⚠️ **This answers the question from the STRING ALONE, so it can only ever return UNCLASSIFIED
    for the Databricks shape above.** The role a literal plays in the surrounding M is a second,
    stronger source of evidence, and callers that have the document text consult it first - see
    :data:`SERVICE_ROUTE_RE` and :func:`_service_routes`. Do not fold that into this function by
    pattern-matching `/sql/`: what makes the endpoint a non-path is the field it is assigned to,
    not the letters in it.
    """
    if WINDOWS_PATH_RE.match(value) or UNC_PATH_RE.match(value):
        return PATH_LITERAL
    if not value.startswith("/"):
        return NOT_A_PATH
    if value.strip() == "/":
        return NOT_A_PATH
    if PurePosixPath(value.rstrip("/")).suffix or value.endswith("/"):
        return PATH_LITERAL
    return UNCLASSIFIED


def _is_path_literal(value: str) -> bool:
    """Whether an absolute literal is a FILE SYSTEM path this packager should ACT on.

    ⚠️ **False here means "do not act on it", NOT "definitely not a path".** It collapses
    :data:`NOT_A_PATH` and :data:`UNCLASSIFIED` into one answer because the two callers that use it
    both ask the same narrow question - "should I ship this?" - and the answer is no either way. A
    caller that has to distinguish *reported* from *silent* must call :func:`_path_verdict`; reading
    a False here as a clean bill of health is exactly how the unassessable bucket gets emptied.
    """
    return _path_verdict(value) == PATH_LITERAL


def _service_routes(text: str) -> set[str]:
    """Literals in ``text`` whose ROLE proves they are not file-system paths.

    Shape-only classification cannot separate a Databricks warehouse endpoint from a mount point:
    `"/sql/1.0/warehouses/<id>"` and `"/mnt/lake/warehouse"` are the same string shape, so
    :func:`_path_verdict` returns UNCLASSIFIED for both, and the packager would ask a human to check
    an endpoint by hand in three of estate run 408's units. The field it is assigned to settles it -
    `HttpPath` takes a route and cannot take a path - so this is evidence, not a guess, and it is
    read from the document the literal actually lives in.
    """
    return {match.group(1) for match in SERVICE_ROUTE_RE.finditer(text)}


def _inside(root: Path, value: str) -> bool:
    """Whether an absolute literal points INSIDE ``root``, judged LEXICALLY and PER FLAVOUR.

    ⚠️ **Never `Path.resolve()` one of these literals.** A UNC literal naming a host that does not
    exist blocks on SMB name resolution: measured by PR #462, that took one test module from 30
    seconds to **52 minutes** and starved a subprocess into its 600 s timeout. Containment is a
    question about the STRING, not about what happens to be mounted.

    ⚠️ **It is also a question about the literal's OWN flavour, not the host's.** This used to
    answer through `PureWindowsPath` unconditionally, whose comparison is case-INSENSITIVE, so on
    Linux an external `/data/Extract.csv` was judged to be inside a package at `/DATA`: skipped by
    localization AND by the post-rewrite scan, it reached the clean bucket with no shipment, no
    omission and no rewrite (blind-review round-2 finding 2). :func:`path_flavour.inside` decides
    flavour lexically first and then compares with that flavour's case rules.
    """
    return inside_lexically(root, value)


def _ceiling_refusal(size: int) -> str | None:
    """The refusal for a source over the package ceiling, or None. ONE comparison site on purpose.

    Both shapes - a bare file and the selected members of a folder - are measured against it here, so
    the ceiling cannot be enforced for one and forgotten for the other.
    """
    if size > MAX_DATA_BYTES:
        return f"{size / 1048576:.1f} MB exceeds the {MAX_DATA_BYTES / 1048576:.0f} MB package ceiling"
    return None


def _classify_source(value: str, *, expect_dir: bool = False) -> tuple[Path | None, str | None]:
    """`(readable path, refusal)` for one absolute literal, WITHOUT ever probing a UNC host.

    The UNC carve-out is the same hazard as :func:`_inside`: `Path.is_file()` on `\\\\nowhere\\share`
    blocks on SMB name resolution for minutes, and packaging must not be able to hang. A UNC source
    is therefore refused unprobed and recorded, which is loud and instant; the promotion gate
    (`promote_unit.py`, exit 5) refuses such a model anyway.

    ⚠️ **A FOREIGN-flavour literal is refused unprobed too, and for a worse reason than hanging.**
    `Path` is the host's, so on Windows `Path("/Users/<person>/Data/x.xlsx").is_file()` is resolved
    against the CURRENT DRIVE: a macOS literal matched `C:\\Users\\<person>\\Data\\x.xlsx` and
    unrelated local bytes were packaged as the customer's source, silently and with a clean exit
    (blind-review round-2 finding 2). Letting the host reinterpret a path it cannot own is not a
    fallback, it is a wrong answer, so the flavour is checked before anything is probed.

    A directory is only checked for EXISTENCE here. Its size is measured over the members a
    partition actually names (:func:`_relocate_folder`), because those are the only bytes the
    package ships - weighing the whole tree would refuse a 300 MB folder for one 4 KB CSV.
    """
    if UNC_PATH_RE.match(value):
        return None, "a UNC path is not probed, because resolving an absent host can block for minutes"
    if not is_host_native(value):
        return None, (
            f"names a {flavour(value) or 'relative'} path, which this machine cannot resolve without "
            "reinterpreting it as a local one"
        )
    path = Path(value)
    if expect_dir:
        if not path.is_dir():
            return None, "the folder it names is not present on the packaging machine"
        return path, None
    if not path.is_file():
        return None, "not present on the packaging machine, so its bytes could not be shipped"
    refusal = _ceiling_refusal(path.stat().st_size)
    return (None, refusal) if refusal else (path, None)


def _declared_expressions(documents: list[Path]) -> set[str]:
    """Every M expression name the model already declares, case-folded and unquoted."""
    return {
        _bare_name(match.group(1))
        for document in documents
        for match in EXPRESSION_NAME_RE.finditer(document.read_text(encoding="utf-8"))
    }


def _bare_name(token: str) -> str:
    """`#"Source Folder"` / `SourceFolder` -> the identifier itself."""
    token = token.strip()
    return token[2:-1] if token.startswith('#"') and token.endswith('"') else token


def _data_folder_param(documents: list[Path]) -> str:
    """A parameter name the model does NOT already declare.

    ⚠️ **Both preferred names can be taken, and the old check could not even see one of them.**
    Blind-review finding 4: a model already declaring `DataFolder` AND `PackageDataFolder` loads
    fine (`check_datamodel.py` exit 0), packaging appended a SECOND `PackageDataFolder`, exited 0,
    and AMO then refused the model - packaging turned a loadable model into an unloadable one. The
    substring test made it worse than it looks: `"DataFolder" in text` is TRUE for a model that
    declares only `PackageDataFolder`, so that model got a duplicate too. Names are now read as
    DECLARATIONS and the fallback is numbered, so a free name always exists.
    """
    declared = {name.casefold() for name in _declared_expressions(documents)}
    for candidate in (DATA_FOLDER_PARAM, FALLBACK_DATA_FOLDER_PARAM):
        if candidate.casefold() not in declared:
            return candidate
    suffix = 2
    while f"{FALLBACK_DATA_FOLDER_PARAM}{suffix}".casefold() in declared:
        suffix += 1
    return f"{FALLBACK_DATA_FOLDER_PARAM}{suffix}"


def _path_separator(base: str) -> str:
    """The separator ``base`` already uses, so a path EXTENDED from it stays internally consistent.

    ⚠️ **This is the fix for a Linux-only defect that Windows structurally cannot see.** Every one of
    these values was composed with a literal ``\\``, which on Windows is right and on Linux produces
    ``/tmp/.../out/Book\\data\\`` - one path segment with backslashes inside it, naming a directory
    that does not exist. Both separators resolve on Windows, so the bug was invisible in every local
    run and only ubuntu CI failed (PR #463).

    Derived from the VALUE rather than from ``os.sep`` on purpose. What matters is not the platform
    doing the composing, it is that the finished literal - which Power Query parses as ONE path - is
    not half Windows and half POSIX. Taking it from the base also makes the rule testable on either
    platform: pass a POSIX base on Windows and the answer must still be ``/``, which is what lets a
    Windows-only run demonstrate this failing.

    ⚠️ It reads the base's FLAVOUR first (:func:`path_flavour.separator`), so `C:/runs/out` - a
    Windows path written with forward slashes - still answers ``\\``. "Does the string contain a
    backslash" was only ever a proxy for the question, and `set_data_folder.py` shares this one
    composer rather than re-deriving it (round-2 finding 4).
    """
    return flavour_separator(base)


def _package_data_folder(final: PurePath) -> str:
    """The folder parameter's value: the package's own `data/`, PLACEHOLDER-rooted, trailing separator.

    ⚠️ **``final`` supplies the SEPARATOR only - never the path.** Writing the package's absolute
    build-time location here is blind-review round-2 finding 1: the rows then have exactly one
    reachable address, the one the builder's machine had, and moving the handover folder leaves them
    present and unreachable. See :data:`PACKAGE_ROOT_TOKEN` for the design and what it costs.

    The trailing separator is load-bearing - :func:`_rewrite_partitions` concatenates a relative tail
    straight onto it - so it is produced here, once, in the same flavour as the rest of the value.
    """
    sep = _path_separator(str(final))
    return f"{PACKAGE_ROOT_TOKEN}{sep}{DATA_DIR}{sep}"


def _host_local(value: str) -> bool:
    """Whether a literal is rooted in ONE MACHINE's own filesystem, so it means nothing anywhere else.

    A drive letter and a POSIX root are machine-bound; `C:\\Users\\<builder>\\...` names the
    packaging host and nothing else, which is why an unshippable one is neutralized rather than
    shipped (:data:`UNAVAILABLE_TOKEN`). A UNC literal is not: it names a share on a network that the
    recipient may share, and this packager deliberately never probed it, so it has no evidence to
    destroy a working configuration with.
    """
    return flavour(value) is not None and not UNC_PATH_RE.match(value)


def _neutralized(value: str, final: Path) -> str:
    """What replaces an unshippable host-rooted literal: a token that resolves nowhere, plus its leaf."""
    return f"{UNAVAILABLE_TOKEN}{_path_separator(str(final))}{_leaf(value)}"


def _write_data_folder_expression(dest: Path, final: Path, model_name: str, parameter: str) -> None:
    """Declare the folder parameter, in the exact shape this repo's committed models already use.

    ⚠️ The value is the FINAL package's own separator flavour, and a PLACEHOLDER root - never
    ``dest``, and since round-2 finding 1 never ``final`` either. Assembly runs in the hidden staging
    directory :func:`staging_dir` names, which `replace_dir` renames afterwards, so writing ``dest``
    here bakes a path that stops existing the moment packaging succeeds - and it would still LOOK
    right in the file. Writing ``final`` bakes the builder's own location into a deliverable that
    exists to be moved. See :data:`PACKAGE_ROOT_TOKEN`.

    Appended rather than overwritten: `expressions.tmdl` is a list of expression objects, and an
    engine model that grows one later must not have it silently replaced. The `lineageTag` is a
    uuid5 of the model and parameter names so repackaging the same unit is byte-stable.
    """
    path = dest / "fabric" / model_name / "definition" / EXPRESSIONS_TMDL
    lineage = uuid.uuid5(uuid.NAMESPACE_URL, f"package_unit:{model_name}:{parameter}")
    block = (
        f'expression {parameter} = "{_package_data_folder(final)}" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n'
        f"\tlineageTag: {lineage}\n\n"
        "\tannotation PBI_ResultType = Text\n\n"
    )
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    separator = "\n" if existing and not existing.endswith("\n\n") else ""
    path.write_text(existing + separator + block, encoding="utf-8")


def _localize_data_sources(dest: Path, final: Path, model_name: str | None) -> dict[str, Any]:
    """Ship every externally-referenced source INTO the package and repoint the model at it (#461).

    The defect: `_copy_fabric` copies the engine's working copy verbatim, and the engine writes
    absolute machine-local paths into the model, pointing back into the originating bundle. So a
    "self-contained" package (#446) carried none of its own rows, opened empty in Desktop with no
    `.pbi/cache.abf` to fall back on, could not refresh on any other machine, and embedded a real
    username in a public repo's deliverable-to-be.

    TWO shapes, because one of them is every datasource-only unit in the estate (see
    :data:`ABSOLUTE_LITERAL_RE`): a bare `File.Contents("<file>")`, and a folder PARAMETER whose
    value is a directory the partitions concatenate a file name onto. They are repaired
    differently - the first is repointed at a new parameter, the second keeps its own parameter and
    has only its VALUE moved - but both end with every literal resolving inside the package.

    What this does NOT do, deliberately: rewrite a literal to a relative path. Power Query rejects a
    relative `File.Contents` argument outright, so that would produce a model refreshing NOWHERE -
    strictly worse than one refreshing on a single machine. A folder PARAMETER is the documented
    workaround and is already this repo's committed convention; see :data:`DATA_FOLDER_PARAM`. What
    it also does not do, since round-2 finding 1, is name the BUILDER's location in that parameter:
    the value is placeholder-rooted and the package is BOUND on consumption
    (:data:`PACKAGE_ROOT_TOKEN`).

    Every reference ends in exactly one of three recorded states - shipped, an omission naming its
    reason, or (for a network share this packager may not probe) retained and recorded. There is no
    silent fourth one: that rule covers the literals this packager cannot even CLASSIFY
    (:data:`UNCLASSIFIED_REASON`), which used to be the silent state. A host-rooted literal that
    could not be shipped is NEUTRALIZED rather than left in place: it named the builder's machine,
    so leaving it ships a username to a customer and promises rows the package does not carry.
    Findings carry the LEAF name only, never the absolute path (convention adopted from #462).
    """
    record: dict[str, Any] = {
        "parameter": None,
        "shipped": [],
        "omissions": [],
        "bytes": 0,
        "neutralized": [],
        "retained_network": [],
        "binding": None,
        "self_contained": True,
    }
    documents = _model_tmdl(dest, model_name)
    if not documents:
        return record
    taken: dict[str, str] = {}
    accounted: set[str] = set()
    _localize_folder_parameters(documents, dest, final, record, taken, accounted)
    _localize_file_literals(documents, dest, final, record, taken, model_name, accounted)
    written = _model_tmdl(dest, model_name)
    record["omissions"].extend(_external_after_rewrite(written, final, accounted))
    record["neutralized"], record["retained_network"] = _neutralize_unshipped(written, final)
    _assert_no_host_path_survives(written, final)
    _assert_distinct_destinations(record)
    record["binding"] = _binding_state(written)
    record["self_contained"] = not (record["omissions"] or record["neutralized"] or record["retained_network"])
    return record


def _binding_state(documents: list[Path]) -> dict[str, str] | None:
    """How the shipped model finds its rows, or None when it reads no packaged folder at all.

    Recorded in `package-manifest.json` so "this package has not been bound to where it now lives" is
    a machine-readable state travelling WITH the artifact, rather than something a recipient learns
    from a refresh error.
    """
    if not any(PACKAGE_ROOT_TOKEN in document.read_text(encoding="utf-8") for document in documents):
        return None
    return {
        "state": "unbound",
        "token": PACKAGE_ROOT_TOKEN,
        "command": BIND_COMMAND,
        "reason": (
            "Power Query rejects a relative File.Contents argument, so a folder parameter must name an "
            "absolute directory; this package names a placeholder instead of the machine that built it, "
            "and binding resolves it wherever the package now lives"
        ),
    }


def _neutralize_unshipped(documents: list[Path], final: Path) -> tuple[list[str], list[str]]:
    """Rewrite every HOST-ROOTED literal still escaping the package to :data:`UNAVAILABLE_TOKEN`.

    `(neutralized leaves, retained network leaves)`.

    Blind-review round-2 finding 1, second half: a source that could not be copied used to keep its
    original literal, on the argument that it "still resolves wherever it did before". For a
    deliverable that is exactly wrong twice over - the path names the BUILDER's machine, so it
    resolves for nobody the package is handed to, and it carries a user-profile directory into a
    customer's artifact. The honest shipped state is a token that resolves nowhere and says why.

    A UNC literal is retained instead (:func:`_host_local`), and recorded, because it names a network
    share rather than this host - refusing to probe it is a hang-avoidance measure, not evidence that
    it is unreachable, so destroying it would break a configuration that may work at the customer.

    ⚠️ **A literal is contained when its SHAPE proves it is a path, or when its ROLE does.** Shape
    alone (:data:`PATH_LITERAL`) left a hole the size of the whole POSIX user-profile convention:
    `File.Contents("/Users/<person>/private-data")` has no suffix and no trailing separator, so
    :func:`_path_verdict` returns UNCLASSIFIED, nothing rewrote it, and the customer's - or the
    builder's - home directory shipped verbatim inside `Imported0.tmdl` at exit 4. The package was
    written anyway, because exit 4 means "incomplete", and a leaked host path is not merely
    incomplete.

    The role evidence is exactly the one :func:`_service_routes` already uses in the opposite
    direction, read from the same document: `File.Contents` takes a file path and takes nothing else,
    so a literal in that position is a path however it is spelled. A service route in an uncatalogued
    field is still left alone - it is never a `File.Contents` argument, which is what makes this
    additive rather than a reversal of the UNCLASSIFIED rule.
    """
    neutralized: list[str] = []
    retained: list[str] = []
    for document in documents:
        text = document.read_text(encoding="utf-8")
        routes = _service_routes(text)
        contained = _contained_literals(text)
        rewritten = text
        for value in sorted({match.group(1) for match in ABSOLUTE_LITERAL_RE.finditer(text)}):
            if value in routes or _inside(final, value) or value not in contained:
                continue
            if not _host_local(value):
                retained.append(_leaf(value))
                continue
            rewritten = rewritten.replace(f'"{value}"', f'"{_neutralized(value, final)}"')
            neutralized.append(_leaf(value))
        if rewritten != text:
            document.write_text(rewritten, encoding="utf-8")
    return sorted(set(neutralized)), sorted(set(retained))


def _contained_literals(text: str) -> set[str]:
    """Every literal in ``text`` this packager must not let ship verbatim: path by SHAPE or by ROLE.

    One site, so the neutralizer and its tripwire (:func:`_assert_no_host_path_survives`) can never
    disagree about which literals are in scope - a tripwire narrower than the rule it guards is
    decorative, and one that is wider fires on literals nothing was ever going to rewrite.
    """
    by_shape = {
        match.group(1) for match in ABSOLUTE_LITERAL_RE.finditer(text) if _path_verdict(match.group(1)) == PATH_LITERAL
    }
    by_role = {match.group(1) for match in FILE_CONTENTS_RE.finditer(text) if match.group(1).strip()}
    return by_shape | by_role


def _assert_no_host_path_survives(documents: list[Path], final: Path) -> None:
    """Tripwire: no shipped `.tmdl` may name a directory on the machine that built the package.

    :func:`_neutralize_unshipped` makes this unreachable, and it is asserted anyway for the same
    reason as :func:`_assert_distinct_destinations`: the consequence is invisible here and lands on
    someone else. A leaked absolute path is what `set_data_folder.py --check` fails the repo for, and
    a package is handed to a customer where no CI gate runs at all.

    ⚠️ **When it DOES fire, nothing is written.** It raises before the staging tree is renamed into
    place, so the unit is reported as failed (exit 5) with no package on disk - "a package that
    cannot be made safe must not be written". Previously it escaped as an uncaught traceback, whose
    interpreter exit 1 is indistinguishable from `EXIT_NO_WORKING_COPY`.
    """
    for document in documents:
        text = document.read_text(encoding="utf-8")
        routes = _service_routes(text)
        contained = _contained_literals(text)
        for match in ABSOLUTE_LITERAL_RE.finditer(text):
            value = match.group(1)
            if value in routes or _inside(final, value) or not _host_local(value):
                continue
            if value in contained:
                raise PackagingError(
                    f"the packaged model still names a path on this machine ({_leaf(value)}), which "
                    "resolves for nobody the package is handed to; nothing is shipped"
                )


def _localize_folder_parameters(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    documents: list[Path],
    dest: Path,
    final: Path,
    record: dict[str, Any],
    taken: dict[str, str],
    accounted: set[str],
) -> None:
    """Move a folder parameter's VALUE into the package, carrying the files a partition NAMES.

    Measured shape, 9 of the estate's 31 external literals and every datasource-only unit::

        expression SourceFolder = "<bundle>\\pbip\\<Unit>\\<Unit>.Data" meta [IsParameterQuery=...]
        ... File.Contents(#"SourceFolder" & "\\Sample - Superstore.xlsx")

    The parameter is reused rather than replaced, and the value keeps the original's separator shape,
    because the partitions' concatenation was written against it.

    ⚠️ **Only the members the model reads are copied.** Blind-review finding 2: this used to
    `copytree` the source folder, so an `unreferenced-secret.txt` sitting beside the extract was
    copied, listed in the manifest and shipped, exit 0. A package exists to be handed to someone
    else, so that is a data-leak shape rather than untidiness - and the folder is very often a
    customer's own working directory. The set of members is derived from the M that reads the
    parameter (:func:`_parameter_usages`), so it is evidence, not a guess; when the M cannot be
    enumerated, nothing is copied and the reason is recorded.
    """
    texts = [document.read_text(encoding="utf-8") for document in documents]
    for document, text in zip(documents, texts, strict=True):
        rewritten = text
        for match in FOLDER_PARAM_RE.finditer(text):
            value = match.group("value")
            verdict = _path_verdict(value)
            if verdict == NOT_A_PATH or _inside(final, value):
                continue
            accounted.add(value)
            if verdict == UNCLASSIFIED:
                record["omissions"].append({"file": _leaf(value), "reason": UNCLASSIFIED_REASON})
                continue
            moved = _relocate_folder(match.group("name"), value, texts, dest, final, record, taken)
            if moved is None:
                continue
            rewritten = rewritten.replace(
                f"{match.group('prefix')}{value}{match.group('quote')}", f'{match.group("prefix")}{moved}"'
            )
        if rewritten != text:
            document.write_text(rewritten, encoding="utf-8")


def _relocate_folder(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    name: str,
    value: str,
    texts: list[str],
    dest: Path,
    final: Path,
    record: dict[str, Any],
    taken: dict[str, str],
) -> str | None:
    """`the parameter's new value`, or None when nothing could be shipped for it (reason recorded)."""
    mode, tails, reason = _parameter_usages(texts, _bare_name(name))
    if reason is not None:
        record["omissions"].append({"file": _leaf(value), "reason": reason})
        return None
    readable, refusal = _classify_source(value, expect_dir=True)
    if readable is None:
        record["omissions"].append({"file": _leaf(value), "reason": refusal})
        return None
    members = _shippable_members(readable, mode, tails, value, record)
    if members is None:
        return None
    relative = _packaged_data_target(value, taken, keep_leaf_only=True)
    _ship_folder(readable, dest / DATA_DIR / relative, relative, record, members)
    return _moved_folder_value(final, relative, value)


def _shippable_members(
    readable: Path, mode: str, tails: set[str], value: str, record: dict[str, Any]
) -> list[Path] | None:
    """The files to ship out of a referenced folder, or None when none may be (reason recorded).

    Both refusals live here rather than at the call site so that the ceiling is measured over the
    SAME list that is copied - a folder whose members are selected in one place and weighed in
    another is how a size gate stops covering what it was written for.
    """
    if mode == WHOLE_FOLDER:
        members, problems = sorted(path for path in readable.rglob("*") if path.is_file()), []
    else:
        members, problems = _folder_members(readable, tails)
    record["omissions"].extend(problems)
    if not members:
        record["omissions"].append(
            {"file": _leaf(value), "reason": "no file this parameter names could be shipped from it"}
        )
        return None
    ceiling = _ceiling_refusal(sum(member.stat().st_size for member in members))
    if ceiling is not None:
        record["omissions"].append({"file": _leaf(value), "reason": ceiling})
        return None
    return members


def _parameter_usages(texts: list[str], bare: str) -> tuple[str, set[str], str | None]:
    """`(mode, literal tails, refusal)` - what the model actually reads through a folder parameter.

    Three answers, and the third is why this exists rather than a `copytree`:

    * :data:`NAMED_FILES` - every use is `<param> & "<literal>"`, so the members are enumerable and
      only those are shipped;
    * :data:`WHOLE_FOLDER` - a `Folder.Files`/`Folder.Contents` call reads the directory itself, so
      the whole tree genuinely IS referenced and copying it is evidenced rather than assumed;
    * a refusal - the parameter is used in a way this cannot enumerate (a computed file name), or is
      never read at all. Nothing is copied, the literal is left resolving where it did before, and
      the reason is recorded. Guessing "copy everything" there is exactly the leak.

    Every occurrence of the name is accounted for, not just the ones that match a known shape: an
    unexplained occurrence is what makes the answer a refusal.
    """
    quoted = re.escape(bare)
    reference = rf'#"{quoted}"|(?<![A-Za-z0-9_]){quoted}(?![A-Za-z0-9_])'
    token = re.compile(reference)
    concat = re.compile(rf'(?:{reference})\s*&\s*"([^"]*)"')
    whole = re.compile(rf"Folder\.(?:Files|Contents)\s*\(\s*(?:{reference})\s*[,)]")
    declaration = re.compile(rf"expression\s+(?:{reference})\s*=")
    tails: set[str] = set()
    whole_folder = False
    unexplained = 0
    for text in texts:
        spans = [match.span() for match in declaration.finditer(text)]
        for match in concat.finditer(text):
            tails.add(match.group(1))
            spans.append(match.span())
        for match in whole.finditer(text):
            whole_folder = True
            spans.append(match.span())
        for match in token.finditer(text):
            if not any(start <= match.start() and match.end() <= end for start, end in spans):
                unexplained += 1
    if unexplained:
        return (
            UNKNOWN_USAGE,
            tails,
            "the model reads this folder in a way the packager cannot enumerate, so shipping it "
            "would mean copying every file in it - nothing was shipped",
        )
    if whole_folder:
        return WHOLE_FOLDER, tails, None
    if tails:
        return NAMED_FILES, tails, None
    return NO_USAGE, tails, "no partition reads a file through this parameter, so nothing was shipped for it"


def _folder_members(readable: Path, tails: set[str]) -> tuple[list[Path], list[dict[str, str]]]:
    """`(files a partition names, omissions)` for the literal tails read through a folder parameter."""
    members: list[Path] = []
    problems: list[dict[str, str]] = []
    for tail in sorted(tails):
        parts = [part for part in re.split(r"[\\/]+", tail) if part not in ("", ".")]
        if not parts or ".." in parts:
            problems.append(
                {"file": _leaf(tail) or tail, "reason": "the name a partition builds escapes the folder it reads from"}
            )
            continue
        candidate = readable.joinpath(*parts)
        if candidate.is_file():
            members.append(candidate)
        else:
            problems.append({"file": parts[-1], "reason": "named by a partition but absent from the folder it reads"})
    return members, problems


def _moved_folder_value(final: Path, relative: str, original: str) -> str:
    """The parameter's new value, keeping the ORIGINAL's trailing-separator convention.

    Partitions concatenate onto this value - `File.Contents(#"SourceFolder" & "\\Sample -
    Superstore.xlsx")` - so adding or dropping a separator here silently breaks every path built
    from it, in a way no structural check can see.

    The separator itself comes from the destination (:func:`_path_separator`), not from a literal
    backslash: only whether a trailing one is present is copied from the original.
    """
    separator = _path_separator(str(final))
    trailing = separator if original.endswith(("\\", "/")) else ""
    return f"{_package_data_folder(final)}{relative.replace('/', separator)}{trailing}"


def _ship_folder(readable: Path, target: Path, relative: str, record: dict[str, Any], members: list[Path]) -> None:
    """Copy the NAMED members of a referenced folder into the package and record each one."""
    for member in members:
        sub = member.relative_to(readable)
        landing = target / sub
        landing.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(member, landing)
        record["shipped"].append({"path": f"{DATA_DIR}/{relative}/{sub.as_posix()}", "bytes": landing.stat().st_size})
        record["bytes"] += landing.stat().st_size


def _localize_file_literals(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    documents: list[Path],
    dest: Path,
    final: Path,
    record: dict[str, Any],
    taken: dict[str, str],
    model_name: str | None,
    accounted: set[str],
) -> None:
    """Ship each bare `File.Contents("<absolute file>")` source and repoint it at a new parameter."""
    parameter = _data_folder_param(documents)
    shipped: dict[str, str] = {}
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for source in sorted({match.group(1) for match in FILE_CONTENTS_RE.finditer(text)}):
            if not _is_path_literal(source) or _inside(final, source) or source in shipped:
                continue
            readable, refusal = _classify_source(source)
            accounted.add(source)
            if readable is None:
                record["omissions"].append({"file": _leaf(source), "reason": refusal})
                continue
            relative = _packaged_data_target(source, taken)
            _ship_file(readable, dest / DATA_DIR / relative, relative, record)
            shipped[source] = relative

    if not shipped:
        return
    _rewrite_partitions(documents, shipped, parameter, _path_separator(str(final)))
    _write_data_folder_expression(dest, final, str(model_name), parameter)
    _assert_declared_once(_model_tmdl(dest, model_name), parameter)
    record["parameter"] = parameter


def _assert_declared_once(documents: list[Path], parameter: str) -> None:
    """Tripwire: the parameter this packager introduced must be declared exactly once.

    :func:`_data_folder_param` makes a duplicate unreachable. It is asserted anyway because the
    consequence is invisible here and fatal later: a model with two `expression <name> =` blocks is
    written happily, packaging exits 0, and AMO refuses to load it (`check_datamodel.py` exit 1) on
    someone else's machine.
    """
    declared = sum(
        1
        for document in documents
        for match in EXPRESSION_NAME_RE.finditer(document.read_text(encoding="utf-8"))
        if _bare_name(match.group(1)).casefold() == parameter.casefold()
    )
    if declared != 1:
        raise PackagingError(
            f"the packaged model declares `expression {parameter}` {declared} times; a duplicate makes "
            "the model unloadable, so nothing is shipped"
        )


def _ship_file(readable: Path, target: Path, relative: str, record: dict[str, Any]) -> None:
    """Copy one referenced file into the package and record it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(readable, target)
    record["shipped"].append({"path": f"{DATA_DIR}/{relative}", "bytes": target.stat().st_size})
    record["bytes"] += target.stat().st_size


def _leaf(value: str) -> str:
    """The last segment of a path literal - all a finding may carry.

    An absolute path embeds a real username and this repo is public, so artifacts get the leaf and
    nothing else. Split lexically, with BOTH separators, for the same reason :func:`_inside` is
    lexical: no probing, and no host semantics (:func:`path_flavour.leaf`).
    """
    return leaf(value)


def _rewrite_partitions(documents: list[Path], shipped: dict[str, str], parameter: str, separator: str) -> None:
    """Point each shipped reference at the package's own copy, through the folder parameter.

    ``separator`` is the parameter value's own, not a literal backslash: the tail written here is
    concatenated straight onto that value, so the two halves of one path must agree.
    """
    for document in documents:
        text = document.read_text(encoding="utf-8")

        def _sub(match: re.Match[str], _shipped: dict[str, str] = shipped, _sep: str = separator) -> str:
            relative = _shipped.get(match.group(1))
            if relative is None:
                return match.group(0)
            return f'File.Contents({parameter} & "{relative.replace("/", _sep)}")'

        rewritten = FILE_CONTENTS_RE.sub(_sub, text)
        if rewritten != text:
            document.write_text(rewritten, encoding="utf-8")


def _external_after_rewrite(documents: list[Path], final: Path, accounted: set[str]) -> list[dict[str, str]]:
    """Read the WRITTEN files back and report every literal still pointing outside the package.

    This is the verification step, and it is deliberately the general question - "is any absolute
    path escaping the package?" - rather than "did my rewrite fire?". Scoping it to the constructs
    the rewriter understands is exactly how the first version of this fix closed less than half of
    #461: it could not see what it did not already look for. ``accounted`` holds the literals the
    repairs already reported with a SPECIFIC reason, so a known-unshippable source is named once
    rather than twice.

    ⚠️ An absolute path UNDER the package is legitimate and must not be reported. That is
    `set_data_folder.py`'s existing convention and it is what both repairs above produce; the rule is
    "absolute AND not under the destination", never "absolute" (finding from PR #462).

    ⚠️ **A literal this cannot classify is reported too, in its own words.** Silence was reserved
    for "definitely not a path", and an unclassifiable literal fell into it - so the escaping
    `"/Users/<person>/Data/"` of blind-review finding 5 left no trace anywhere. Each distinct
    literal is reported once however many documents carry it.

    ⚠️ **A literal whose ROLE proves it is not a path is silent, and only that.** A Databricks
    `HttpPath` endpoint is definitively a non-path (:func:`_service_routes`), so reporting it would
    put three of estate run 408's units into "check it by hand" for something no hand-check can
    change. That is the narrow carve-out: role-proven non-paths leave, shape-unassessable literals
    stay reported.
    """
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for document in documents:
        text = document.read_text(encoding="utf-8")
        routes = _service_routes(text)
        for match in ABSOLUTE_LITERAL_RE.finditer(text):
            value = match.group(1)
            if value in accounted or value in seen or value in routes or _inside(final, value):
                continue
            verdict = _path_verdict(value)
            if verdict == PATH_LITERAL:
                seen.add(value)
                findings.append({"file": _leaf(value), "reason": "still points outside the package after packaging"})
            elif verdict == UNCLASSIFIED:
                seen.add(value)
                findings.append({"file": _leaf(value), "reason": UNCLASSIFIED_REASON})
    return findings


def _write_spec(asset: Path | None, dest: Path) -> tuple[str | None, str | None]:
    """`(relative spec path, failure note)` - `check_unit.py` cannot grade a unit without one (#443)."""
    if asset is None:
        return None, "no migration-spec.json: the source asset could not be resolved"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT_DIR / "parse_tableau.py"), str(asset), "-o", str(dest / "migration-spec.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if proc.returncode != 0 or not (dest / "migration-spec.json").is_file():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, f"parse_tableau.py failed (exit {proc.returncode}): {detail[-1] if detail else 'no output'}"
    return "migration-spec.json", None


def _write_spec_schema(dest: Path) -> tuple[str | None, str | None]:
    """`(relative schema path, failure note)` - the spec CONTRACT, shipped rather than described.

    Copied verbatim from `docs/migration-spec.schema.json` so it can never drift from the schema
    `validate_spec.py` actually enforces; see :data:`SPEC_SCHEMA` for what an extract cost.
    """
    if not SPEC_SCHEMA.is_file():
        return None, f"no {SPEC_SCHEMA.name}: the spec contract could not be shipped from {SPEC_SCHEMA.parent.name}/"
    shutil.copy2(SPEC_SCHEMA, dest / SPEC_SCHEMA.name)
    return SPEC_SCHEMA.name, None


def _attach_oracle(oracle_dir: Path | None, identity: dict[str, Any], dest: Path, unit: str = "") -> dict[str, Any]:
    """This unit's slice of the flat capture, or an empty slice carrying the refusal reason."""
    oracle: dict[str, Any] = {"objects": [], "omissions": [], "route": None, "reason": None}
    manifest = read_json(oracle_dir / "oracle-manifest.json") if oracle_dir else None
    if manifest is None:
        oracle["reason"] = "no oracle-manifest.json found" if oracle_dir else "no oracle capture supplied"
        return oracle
    views, route = select_views(manifest, identity)
    if not views:
        oracle["reason"] = route
        return oracle
    oracle.update(package_oracle(views, manifest, oracle_dir, dest / "oracle", unit))
    oracle["route"] = route
    return oracle


def _handover_workbook(handover: Any, unit: str, dest: Path) -> dict[str, Any] | None:
    """The workbook payload inside a handover slice, via read_handover's own resolver."""
    if not isinstance(handover, dict):
        return None
    found = read_handover._workbooks_from_payload(handover, dest)  # pylint: disable=protected-access
    return next((wb for name, wb, _ in found if name == unit), found[0][1] if found else None)


# --------------------------------------------------------------------------------------------
# the path budget - measured BEFORE anything is written (#476)
# --------------------------------------------------------------------------------------------

#: The package-relative paths this packager writes that are neither the copied `fabric/` tree nor
#: the handover slice. Enumerated rather than walked because they do not exist yet, and listed at
#: all because a unit with NO engine working copy ships only these - `fabric/` is then empty and
#: something still has to be the deepest thing measured. Every one is a single short segment, so
#: none of them is ever the binding constraint when a `fabric/` tree is present.
_SCAFFOLD_FILES = (
    "README.md",
    "handover.md",
    MANIFEST_NAME,
    "report.json",
    "source-provenance.json",
    "engine-output-receipt.json",
    "migration-spec.json",
    SPEC_SCHEMA.name,
)

#: Hex characters of the unit digest that name its staging directory. The staging segment sits at the
#: DEEPEST point of every path assembly touches, so its length is pure overhead paid by every file in
#: the tree; making it a constant 9 characters (`.` + 8 hex) instead of `.{unit}.staging` reclaims
#: `len(unit)` characters for a 37-character unit name and never costs more than 8. Long enough that
#: two units in one `--out` do not collide (2**32 pairs), short enough to stop mattering.
_STAGING_STEM_CHARS = 8

#: How many offending units a batch refusal names before summarising. Matching
#: `check_path_ceiling.WORST_N`'s reasoning: naming the unit is the whole point, but an `--out` that
#: is too deep is usually too deep for many units at once, and they all share one remedy.
WORST_UNITS = 5


class ProjectedPath(NamedTuple):
    """One path packaging WILL produce, measured before it exists.

    ``tail`` is package-relative and therefore survives relocation, which is the number that answers
    "will this fit somewhere else"; ``length`` is UTF-16 code units, the unit Power BI Desktop counts.
    """

    kind: str
    tail: str
    path: str
    length: int
    ceiling: int


class PathBudget(NamedTuple):
    """What one unit's paths measure against the ceilings, and how much `--out` may spend.

    ``out_root_budget`` is the longest `--out` this unit tolerates under the ceilings that judged
    ``overruns`` - the HOST's (:func:`platform_limits`). ``shipping`` is the separate, relocation-
    invariant question: entries whose package-relative cost alone exceeds what Power BI Desktop
    accepts, so no `--out` on any machine can rescue them.

    ⚠️ **The two are different questions and the split is blind-review finding B3.** The Windows
    ceilings were measured against Desktop; applying them to an absolute POSIX path refused a
    297-character `--out` whose package was valid at 332 characters. What travels with a package is
    its tails, so that is what Desktop's ceiling is asked about; the host's own limits decide only
    whether this machine may write the tree at all.
    """

    unit: str
    out_root: Path
    out_root_length: int
    out_root_budget: int
    overruns: list[ProjectedPath]
    shipping: list[ProjectedPath]
    shipping_budget: int

    @property
    def refused(self) -> bool:
        """Whether this unit may not be assembled here - on EITHER question."""
        return bool(self.overruns or self.shipping)

    @property
    def hard_budget(self) -> int:
        """The tightest of the two budgets: negative means no `--out` anywhere can fit this unit."""
        return min(self.out_root_budget, self.shipping_budget)

    @property
    def worst(self) -> ProjectedPath:
        """The deepest offender, whichever question found it. Only meaningful when :attr:`refused`."""
        return (self.overruns or self.shipping)[0]


def _short_stem(name: str) -> str:
    """A fixed-width, collision-resistant stem for `name` - see :data:`_STAGING_STEM_CHARS`.

    ⚠️ **Resistant, not collision-PROOF, and it does not need to be.** `Unit_17592` and `Unit_58987`
    both stem to `aff484f3` - found after 58,988 generated names, so the birthday bound behaves as
    an 8-hex digest should. Packaging is serialized and every staging directory is removed in
    :func:`package_unit`'s `finally` before the next unit starts, so a colliding pair would have to
    be assembled CONCURRENTLY into one `--out`, which this CLI never does. Widening the stem would
    cost every path in the tree the characters #476 exists to reclaim.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:_STAGING_STEM_CHARS]


#: The suffix that distinguishes a package being RETIRED mid-swap from a unit being STAGED.
_RETIRED_SUFFIX = "~"

#: The one shape every scratch directory this packager creates inside `--out` has: a dot, the digest
#: of a name, and optionally the retired marker. Written as a pattern because BOTH directions are
#: enforced from it - see :func:`is_reserved_packaging_name`.
_RESERVED_NAME_RE = re.compile(rf"^\.[0-9a-f]{{{_STAGING_STEM_CHARS}}}{re.escape(_RETIRED_SUFFIX)}?$")


def is_reserved_packaging_name(name: str) -> bool:
    """Whether ``name`` is one this packager creates for its own SCRATCH, never for a package.

    ⚠️ **This is what stops `shutil.rmtree` deleting a finished package** (blind-review B2, silent
    data loss, reproduced end to end). A unit name comes from the customer's Tableau estate and a
    leading dot is legal there, so a unit could be named exactly `.d72cee2e` - which is the staging
    directory of a unit called `Victim`. Packaging both into one `--out` reported **both packaged at
    exit 0** and left only `Victim` on disk: `rmtree(staging)` had deleted a completed package that
    happened to occupy the path. Reporting success while destroying a finished package is the worst
    outcome this file can produce.

    Closed in BOTH directions from this one predicate, so the halves cannot drift:

    * :func:`unit_name_problem` refuses a unit name that matches, so nothing a customer can name
      ever occupies a scratch path; and
    * :func:`_discard_scratch` refuses to delete a directory whose name does NOT match, so a future
      change to the naming scheme fails loudly instead of deleting a package.
    """
    return bool(_RESERVED_NAME_RE.match(name))


def staging_dir(out_root: Path, unit: str) -> Path:
    """Where `unit` is assembled before `replace_dir` swaps it into `<out_root>/<unit>`.

    ⚠️ **A SIBLING of the final package, never a child, and never longer than it in any measured
    estate.** Assembly used to happen in `<out>/.{unit}.staging/`, so every one of the hundreds of
    files in a PBIR tree was written 9 characters deeper than its final home - and the crash in
    issue #476 (`[WinError 206]`, 29 units into 47) landed in exactly that margin. The name is
    hidden, and derived from the unit rather than fixed, so two units packaged into one `--out`
    cannot collide the way a shared `.staging` would.

    Kept INSIDE `out_root` deliberately, against the issue's own suggestion of `tempfile.mkdtemp()`:
    the swap is `Path.rename`, which is atomic only within one volume and fails outright across
    two - and a temp root would also move assembly outside the directory `conflicting_evidence_dirs`
    has already cleared.

    ⚠️ The name it returns is always :func:`is_reserved_packaging_name`, which is what makes the
    path un-nameable by a unit and therefore safe to `rmtree`.
    """
    return out_root / f".{_short_stem(unit)}"


def retired_dir(final: Path) -> Path:
    """Where the package at ``final`` is renamed to while its replacement lands.

    One site, so the name `replace_dir` creates and the name :func:`projected_paths` measures cannot
    disagree - the retired tree is walked by `shutil.rmtree`, so its paths have to be openable too.
    """
    return final.with_name(f".{_short_stem(final.name)}{_RETIRED_SUFFIX}")


def _discard_scratch(path: Path) -> None:
    """`rmtree` a directory this packager NAMED, and refuse to touch anything else.

    The tripwire behind :func:`is_reserved_packaging_name`'s first half. Prevention is the fix - a
    unit may not be named like a scratch directory - and this is asserted anyway for the reason every
    tripwire in this file exists: the consequence is invisible here and lands on someone else, as a
    package that was reported shipped and is not on disk.
    """
    if not is_reserved_packaging_name(path.name):
        raise PackagingError(
            f"refusing to delete {path}: {path.name!r} is not a name this packager gives its own "
            "scratch directories, so removing it could destroy a finished package. Staging and "
            "retired trees are named `.<digest>` and `.<digest>~`; nothing else may be swept."
        )
    shutil.rmtree(path, ignore_errors=True)


def _predicted_asset(bundle: Path, unit: str, assets_dir: Path | None) -> Path | None:
    """The asset :func:`_stage_asset` will copy in, resolved through the SAME two calls it makes.

    ⚠️ **Not a re-implementation.** The writer reads `handover/<unit>.json`, scopes it, and asks
    :func:`resolve_asset`; so does this. Guessing the packaged name from the bundle directly would be
    a second rule that could drift from the first, which is the failure mode this whole projection
    is being made exhaustive to avoid.
    """
    handover = read_json(bundle / "handover" / f"{unit}.json")
    if isinstance(handover, dict):
        handover, _redactions = scope_handover(handover, unit)
    return resolve_asset(bundle, unit, handover, assets_dir)[0]


def _generated_tails(bundle: Path, unit: str, assets_dir: Path | None) -> list[tuple[str, str]]:
    """The package-relative paths assembly creates that are NOT copies of the engine working copy.

    Three groups, and each is here because it was missing when blind review measured this (B1):

    * `assets/<name>` - the harvested `.twb`/`.twbx`/`.tds`, whose filename is the CUSTOMER's. A
      valid 204-character name projected a maximum of 102 and packaged a 279-character path at exit
      0. This is the one that is genuinely long, and it is resolved rather than bounded.
    * `expressions.tmdl` - written into the packaged model when the data-source localizer introduces
      a folder parameter and the engine copy has no such document yet.
    * the `data/` and `oracle/` CONTAINERS. Their leaf names are bounded (an oracle stem is capped at
      :data:`_MAX_OBJECT_NAME`) but their MEMBERS are not - a folder parameter ships nested member
      paths verbatim - so the containers are projected here and the members are measured for real by
      :func:`assert_assembled_fits` once they exist. Each container is a single short segment, so
      projecting one that a given unit never creates cannot bind before the `fabric/` tree does.
    """
    tails: list[tuple[str, str]] = [(KIND_DIR, DATA_DIR), (KIND_DIR, "assets"), (KIND_DIR, "oracle")]
    tails.append((KIND_FILE, "oracle/oracle-manifest.json"))
    for kind in KIND_DIRS:
        tails.append((KIND_DIR, f"oracle/{kind}"))
        tails.extend((KIND_DIR, f"oracle/{kind}/{leg}") for leg in ("images", "data"))
    asset = _predicted_asset(bundle, unit, assets_dir)
    if asset is not None:
        tails.append((KIND_FILE, f"assets/{asset.name}"))
    model = next(
        (path.name for path in sorted((bundle / "pbip" / unit).glob("*.SemanticModel")) if path.is_dir()), None
    )
    if model:
        tails.append((KIND_DIR, f"fabric/{model}/definition"))
        tails.append((KIND_FILE, f"fabric/{model}/definition/{EXPRESSIONS_TMDL}"))
    return tails


def _package_tails(bundle: Path, unit: str, assets_dir: Path | None = None) -> list[tuple[str, str]]:
    """`(kind, package-relative path)` for every path packaging can predict before writing one.

    ⚠️ **This is the PRE-FLIGHT half of a two-part guarantee - it is no longer the guarantee.** It
    used to say it was "not exhaustive, and deliberately so in the safe direction", which was wrong
    in the only direction that matters: a budget that measures a SUBSET and then reports exit 0 is
    fail-open. Blind review measured it - `assets/`, `data/`, `oracle/` and a freshly generated
    `expressions.tmdl` were all unmeasured, and a valid 204-character workbook filename shipped a
    279-character path at exit 0.

    What holds now:

    * everything derivable from the bundle is projected here, BEFORE any work is done, so an
      unfittable estate costs one message instead of 29 written packages; and
    * everything else - oracle renders, shipped data members, and any output a future edit adds - is
      measured by :func:`assert_assembled_fits` against the tree assembly ACTUALLY produced, before
      it is swapped into place. That walk is derived from the writer's own output rather than from a
      model of it, which is what makes an incomplete projection impossible to ship rather than
      merely discouraged.
    """
    tails: list[tuple[str, str]] = [(KIND_DIR, "")]
    source = bundle / "pbip" / unit
    if source.is_dir():
        tails.append((KIND_DIR, "fabric"))
        tails.extend(
            (KIND_DIR if path.is_dir() else KIND_FILE, f"fabric/{path.relative_to(source).as_posix()}")
            for path in sorted(source.rglob("*"))
        )
    tails.append((KIND_DIR, "handover"))
    tails.append((KIND_FILE, f"handover/{unit}.json"))
    tails.extend((KIND_FILE, name) for name in _SCAFFOLD_FILES)
    tails.extend(_generated_tails(bundle, unit, assets_dir))
    return sorted(set(tails))


def _measure(roots: tuple[Path, ...], tails: list[tuple[str, str]], limits: Limits) -> list[ProjectedPath]:
    """Every `(root, tail)` pair as a measured :class:`ProjectedPath`."""
    projected: list[ProjectedPath] = []
    for kind, tail in tails:
        ceiling = limits.dir_ceiling if kind == KIND_DIR else limits.file_ceiling
        for root in roots:
            full = str(root / tail) if tail else str(root)
            projected.append(ProjectedPath(kind, tail, full, utf16_len(full), ceiling))
    return projected


def package_roots(out_root: Path, unit: str) -> tuple[Path, ...]:
    """Every root one unit's tree occupies while packaging, in the order it occupies them.

    THREE, not two. The staged tree is written, the final tree ships - and on a re-run the package
    is renamed to :func:`retired_dir` and `shutil.rmtree` WALKS it, so those paths have to be
    openable as well. Taking the longest of the three is what makes the answer independent of which
    happens to be deeper for a given unit name: staging costs a constant 9 characters and retirement
    10, so for a unit named `B` both scratch roots are deeper than the package itself.
    """
    final = out_root / unit
    return (final, staging_dir(out_root, unit), retired_dir(final))


def projected_paths(
    bundle: Path,
    unit: str,
    out_root: Path,
    *,
    limits: Limits = DEFAULT_LIMITS,
    assets_dir: Path | None = None,
) -> list[ProjectedPath]:
    """Measure every predictable path at every root the unit passes through."""
    return _measure(package_roots(out_root, unit), _package_tails(bundle, unit, assets_dir), limits)


def _budget(unit: str, out_root: Path, projected: list[ProjectedPath], limits: Limits) -> PathBudget:
    """Turn a measured set into the two verdicts and the two budgets - see :class:`PathBudget`.

    ``cost`` is what a path spends ABOVE `--out`, which is the relocation-invariant number: every
    root measured is a child of `--out`, so subtracting its length leaves the package-relative shape.
    """
    root_length = utf16_len(str(out_root))

    def cost(path: ProjectedPath) -> int:
        return path.length - root_length

    def windows_ceiling(path: ProjectedPath) -> int:
        return WINDOWS_LIMITS.dir_ceiling if path.kind == KIND_DIR else WINDOWS_LIMITS.file_ceiling

    overruns = sorted(
        (path for path in projected if path.length > path.ceiling),
        key=lambda path: path.length - path.ceiling,
        reverse=True,
    )
    shipping = sorted(
        (path for path in projected if cost(path) > windows_ceiling(path)),
        key=lambda path: cost(path) - windows_ceiling(path),
        reverse=True,
    )
    return PathBudget(
        unit,
        out_root,
        root_length,
        min((path.ceiling - cost(path) for path in projected), default=limits.file_ceiling),
        overruns,
        shipping,
        min((windows_ceiling(path) - cost(path) for path in projected), default=WINDOWS_LIMITS.file_ceiling),
    )


def path_budget(
    bundle: Path,
    unit: str,
    out_root: Path,
    *,
    limits: Limits | None = None,
    assets_dir: Path | None = None,
) -> PathBudget:
    """What `unit` would measure under `out_root`, and how long an `--out` it can tolerate.

    ``limits`` defaults to the HOST's (:func:`platform_limits`), not to Windows'. Desktop's ceiling
    is still applied - to the package-relative tails, through :attr:`PathBudget.shipping` - because
    that is the part of a path that travels with the package (blind-review B3).
    """
    limits = platform_limits() if limits is None else limits
    return _budget(
        unit, out_root, projected_paths(bundle, unit, out_root, limits=limits, assets_dir=assets_dir), limits
    )


def assembled_budget(unit: str, staging: Path, final: Path, out_root: Path, limits: Limits) -> PathBudget:
    """Measure what assembly ACTUALLY wrote, at all three roots the tree passes through.

    Walking the staged tree is what makes the measurement exhaustive: it is derived from the writer's
    own output, so an output a future edit adds appears here without anyone remembering to declare
    it. :func:`_package_tails` is the cheap pre-flight; this is the guarantee.
    """
    tails = [(KIND_DIR, "")]
    tails += [
        (KIND_DIR if path.is_dir() else KIND_FILE, path.relative_to(staging).as_posix())
        for path in sorted(staging.rglob("*"))
    ]
    return _budget(unit, out_root, _measure(package_roots(out_root, final.name), tails, limits), limits)


def assert_assembled_fits(unit: str, staging: Path, final: Path, out_root: Path, limits: Limits) -> None:
    """Refuse a tree that would not survive its own swap, BEFORE anything is published.

    Nothing has been shipped when this raises: the staged tree is removed by :func:`package_unit`'s
    `finally` and a previously-good package at ``final`` is untouched, because the swap has not
    happened yet.
    """
    budget = assembled_budget(unit, staging, final, out_root, limits)
    if budget.refused:
        raise PackagePathTooLong(budget)


#: The two ways a filesystem says "that name is too long". Checked rather than assumed because the
#: platforms disagree: Windows raises `[WinError 206]` (`ERROR_FILENAME_EXCED_RANGE`) with an errno
#: Python maps to `EINVAL`, POSIX raises `ENAMETOOLONG`.
_TOO_LONG_WINERROR = 206


def _assembly_refusal(unit: str, error: OSError) -> PackagingError | None:
    """A length failure the OS itself raised mid-assembly, restated so it names path and remedy.

    The backstop under :func:`assert_assembled_fits`, for the machine that cannot even WRITE the
    tree: on stock Windows the write throws before there is a tree to measure. `[WinError 206] The
    filename or extension is too long` named no path, no limit and no remedy, and escaped as an
    uncaught traceback whose exit 1 is indistinguishable from `EXIT_NO_WORKING_COPY`. Anything else
    is somebody else's error and is re-raised untouched.
    """
    if getattr(error, "winerror", None) != _TOO_LONG_WINERROR and error.errno != errno.ENAMETOOLONG:
        return None
    return PackagingError(
        f"refusing {unit}: the filesystem rejected a path as too long while assembling it "
        f"({error.filename or 'path not reported by the OS'}). Nothing was packaged for this unit. "
        "Shorten --out, or the name the engine repeats in <Unit>/<Unit>.Report/; ceilings are "
        "measured in scripts/check_path_ceiling.py, background in docs/windows-path-limits.md."
    )


def render_path_budget(budget: PathBudget) -> str:
    """The actionable refusal for ONE unit: which path, how long, against what, and what fixes it."""
    worst = budget.worst
    kind = "directory" if worst.kind == KIND_DIR else "file"
    remedy = (
        f"--out is {budget.out_root_length} character(s) long; it must be at most "
        f"{budget.hard_budget} ({budget.out_root_length - budget.hard_budget} shorter) for this unit to fit."
        if budget.hard_budget >= 0
        else (
            f"NO --out can fit this unit: its package-relative shape is already "
            f"{-budget.hard_budget} character(s) over on its own. The lever is the name the engine "
            f"repeats in <Unit>/<Unit>.Report/, not the output directory."
        )
    )
    return (
        f"{budget.unit}: packaging it under {budget.out_root} would produce {len(budget.overruns or budget.shipping)} "
        f"path(s) Power BI Desktop refuses to open, so it was not assembled at all.\n"
        f"  deepest: {worst.length} UTF-16 units, {worst.length - worst.ceiling} over the "
        f"{worst.ceiling}-character {kind} ceiling\n"
        f"    {worst.path}\n"
        f"{remedy}\n"
        f"Ceilings are measured in scripts/check_path_ceiling.py; background in docs/windows-path-limits.md."
    )


def render_out_too_deep(budgets: list[PathBudget], total: int) -> str:
    """The actionable refusal for a BATCH, raised before any unit is assembled.

    Every unit is named with its own overage rather than only the first, because they all share one
    remedy - a shorter `--out` - and the operator needs the single number that satisfies all of them.
    Packaging the units that DO fit was considered and rejected: the estate would have to be
    repackaged wholesale under the shorter `--out` anyway, and a half-packaged estate is exactly the
    state issue #476 was reported from.
    """
    lines = [
        f"--out {budgets[0].out_root} is too deep: {len(budgets)} of {total} unit(s) would be assembled at paths "
        "Power BI Desktop refuses to open, so NOTHING was packaged."
    ]
    for budget in budgets[:WORST_UNITS]:
        worst = budget.worst
        kind = "directory" if worst.kind == KIND_DIR else "file"
        lines.append(
            f"  {budget.unit}: {worst.length} UTF-16 units, {worst.length - worst.ceiling} over the "
            f"{worst.ceiling}-character {kind} ceiling"
        )
        lines.append(f"    {worst.path}")
    if len(budgets) > WORST_UNITS:
        lines.append(f"  ... and {len(budgets) - WORST_UNITS} more")
    fits = min(budget.hard_budget for budget in budgets)
    root_length = budgets[0].out_root_length
    lines.append(
        f"--out is {root_length} character(s) long; it must be at most {fits} ({root_length - fits} shorter) "
        "for every unit to fit."
        if fits >= 0
        else (
            f"No --out can fit every unit: the worst is {-fits} character(s) over on its "
            "package-relative shape alone, so shortening the output directory cannot rescue it - the "
            "lever is the name the engine repeats in <Unit>/<Unit>.Report/. Package the units that fit "
            "with --unit."
        )
    )
    lines.append("Ceilings are measured in scripts/check_path_ceiling.py; background in docs/windows-path-limits.md.")
    return "\n".join(lines)


def render_shipping_advisory(budgets: list[PathBudget]) -> str | None:
    """A WARNING, never a refusal: these packages fit HERE and barely fit a Windows machine.

    The other half of the B3 split. A package built under a long POSIX `--out` is not a defect - it
    relocates - but a package whose TAILS leave less than :data:`SHIPPING_ROOT_BUDGET_ADVISORY`
    characters for the root it lands under is a shipping hazard wherever it was built, and nothing
    downstream measures it again. Advisory because the evidence supports "tight", not "broken":
    `C:\\Users\\<name>\\Documents\\` is already ~28 characters before a customer makes one folder.
    """
    tight = sorted(
        (budget for budget in budgets if 0 <= budget.shipping_budget < SHIPPING_ROOT_BUDGET_ADVISORY),
        key=lambda budget: budget.shipping_budget,
    )
    if not tight:
        return None
    named = ", ".join(f"{budget.unit} ({budget.shipping_budget})" for budget in tight[:WORST_UNITS])
    more = f", and {len(tight) - WORST_UNITS} more" if len(tight) > WORST_UNITS else ""
    return (
        f"WARN: {len(tight)} package(s) tolerate a Windows root of fewer than "
        f"{SHIPPING_ROOT_BUDGET_ADVISORY} characters, so they may not open where a customer unpacks "
        f"them even though they are valid here: {named}{more}. The lever is the name the engine "
        "repeats in <Unit>/<Unit>.Report/, not this --out."
    )


def package_unit(  # pylint: disable=too-many-arguments
    bundle: Path,
    unit: str,
    out_root: Path,
    *,
    oracle_dir: Path | None,
    assets_dir: Path | None,
    discard_edits: bool = False,
    limits: Limits | None = None,
) -> dict[str, Any]:
    """Assemble one unit's package. Returns the record written to `package-manifest.json`.

    ⚠️ **Refuses rather than overwriting a package that has been edited.** This packager declares
    `<package>/fabric/` the canonical place to work, and `replace_dir` replaces the package whole -
    so re-running the same command silently deleted an agent's TMDL (blind-review finding 6). Silent
    loss is the one unacceptable outcome; refusing costs a flag and names the files. ``discard_edits``
    (`--discard-package-edits`) is the deliberate override.

    ⚠️ **The digest is checked TWICE, and the second time is the one that matters.** Checking only
    before assembly leaves the whole assembly window - copying a 51 MB render tree, running
    `parse_tableau.py` on the source - during which an edit to the canonical package is accepted by
    the filesystem and then destroyed by the swap: exactly the loss #460 exists to prevent, with the
    guard already "passed" (blind-review round-2 finding 3, reproduced: `edit_survived_repackage=False`).
    The re-check runs INSIDE `replace_dir`, after the existing package has been renamed out of the
    way, which is what makes it a check under the lock rather than one more racing read: once the
    directory is retired, nothing can reach it by the path a writer would use, and if it changed it
    is renamed straight back.
    ⚠️ **The unit name is checked BEFORE anything is created.** It comes from the engine's
    `report.json` or from a `pbip/` directory name, both of which originate in the customer's Tableau
    estate - so it is source-controlled input, and `..\\escaped-package` used to write a whole
    package outside `--out` (see :class:`UnsafeUnitName`).

    ⚠️ **Refuses BEFORE assembling anything that would not fit, and again once it HAS been
    assembled** (#476). The pre-flight budget is measured on the destination the name check just
    cleared, so an overlong `--out` costs one message instead of a `[WinError 206]` thrown 29 units
    into a 47-unit estate with a half-written staging tree behind it. Order is load-bearing: the name
    decides WHERE we write, so it is settled first, and only then is that destination measured.

    The second measurement is the one that closes the class. A projection is a MODEL of the output
    and blind review measured it missing four real ones (B1); :func:`assert_assembled_fits` walks the
    tree assembly actually produced, before the swap, so nothing can be added to this packager
    without appearing in the budget. Both refusals leave nothing behind: the staged tree is removed
    in `finally` and the package at ``final`` is never touched until the swap.

    ``limits`` defaults to the HOST's ceilings (:func:`platform_limits`); it is a parameter so that a
    caller - or a test on either CI runner - can state which platform's arithmetic it means rather
    than inheriting the one it happens to run on.
    """
    limits = platform_limits() if limits is None else limits
    final = assert_package_destination(out_root, unit)
    budget = path_budget(bundle, unit, out_root, limits=limits, assets_dir=assets_dir)
    if budget.refused:
        raise PackagePathTooLong(budget)
    if final.is_dir() and not discard_edits:
        _refuse_if_edited(unit, final)
    staging = staging_dir(out_root, unit)
    _discard_scratch(staging)
    try:
        result = _assemble_unit(bundle, unit, staging, final=final, oracle_dir=oracle_dir, assets_dir=assets_dir)
        assert_assembled_fits(unit, staging, final, out_root, limits)
        replace_dir(staging, final, verify=None if discard_edits else partial(_refuse_if_edited, unit))
    except OSError as failure:
        refusal = _assembly_refusal(unit, failure)
        if refusal is None:
            raise
        raise refusal from failure
    finally:
        _discard_scratch(staging)
    return result


def _refuse_if_edited(unit: str, package: Path) -> None:
    """Raise :class:`PackageEditsRefused` unless ``package`` still matches the digest it recorded.

    One site, called from both ends of the assembly window, so the two checks can never drift into
    asking different questions - which is how a "guard" ends up protecting only the cheap half of an
    operation.
    """
    changed, reason = package_edits(package)
    if reason is not None or changed:
        raise PackageEditsRefused(unit, package, changed, reason)


def package_contents(root: Path) -> dict[str, str]:
    """`{package-relative path: sha256}` for every file in a package except the manifest itself.

    The manifest is excluded because it is written last and CARRIES this map - including it would
    make every package differ from its own record.
    """
    return {
        path.relative_to(root).as_posix(): sha256_of(path) or ""
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() != MANIFEST_NAME
    }


def package_edits(root: Path) -> tuple[list[str], str | None]:
    """`(paths that differ from what packaging wrote, reason it could not be established)`.

    Whole-package, not just `fabric/`: the package README also tells an agent to append
    `limitations_encountered` entries to `migration-spec.json`, and losing those silently is the same
    defect wearing different clothes.

    A package with no recorded digest returns a REASON, never an empty change list. "I cannot tell
    whether this was edited" is not "it was not edited" - collapsing the two is how unassessable
    input ends up in the clean bucket, which is the defect class this whole review round is about.
    """
    manifest = read_json(root / MANIFEST_NAME)
    recorded = ((manifest or {}).get("contents") or {}).get("files") if isinstance(manifest, dict) else None
    if not isinstance(recorded, dict):
        return [], (
            f"it carries no {MANIFEST_NAME} content digest, so whether anything was edited in it cannot be established"
        )

    current = package_contents(root)
    changed = set(recorded) ^ set(current)
    changed |= {path for path in set(recorded) & set(current) if recorded[path] != current[path]}
    return sorted(changed), None


#: Every separator either flavour recognises, checked REGARDLESS of the host. A packaging host is not
#: necessarily the harvest host, and `..\\x` is a traversal on Windows while `PurePosixPath` reads it
#: as one innocent-looking filename - so asking `Path` (which is the host's) answers the wrong
#: question on exactly the platform pairing that matters.
_NAME_SEPARATORS = ("/", "\\")


def unit_name_problem(unit: str) -> str | None:
    """Why ``unit`` may not be used as a directory name under ``--out``, or None when it is safe.

    ⚠️ **Both separators, on both platforms.** `..\\escaped` reaching a POSIX packaging host is one
    filename to `pathlib` and a traversal to the Windows machine that will later read the estate, and
    `../escaped` is a traversal on both. Neither is a name any real Tableau workbook has, so refusing
    the whole class costs nothing and closes it for good.

    Rejects, in order: an empty or whitespace-only name; a name containing either separator; `.` and
    `..`; a name this packager reserves for its own scratch directories; a drive-qualified name
    (`C:` / `C:\\x`, which `os.path.join` on Windows resolves against the *current directory of that
    drive*); and a name that is otherwise not a single component.

    Written as an ordered table rather than a ladder of returns so a new rule is one row: the order
    is the message an operator sees, and every predicate is total on any string, so evaluating them
    all costs nothing and cannot raise on input an earlier rule would have caught.
    """
    refusals: tuple[tuple[bool, str], ...] = (
        (not unit or not unit.strip(), "it is empty"),
        (
            any(separator in unit for separator in _NAME_SEPARATORS),
            "it contains a path separator, so it names a location rather than a unit",
        ),
        (unit in {".", ".."}, "it is a relative directory reference, not a name"),
        (
            is_reserved_packaging_name(unit),
            "it is the shape this packager gives its own staging and retired directories "
            "(`.<digest>` / `.<digest>~`), so packaging another unit into the same --out would delete it",
        ),
        (
            bool(re.match(r"^[A-Za-z]:", unit)),
            "it is drive-qualified, which resolves against that drive rather than under --out",
        ),
        (PurePath(unit).name != unit or PureWindowsPath(unit).name != unit, "it is not a single path component"),
    )
    return next((reason for refused, reason in refusals if refused), None)


def assert_package_destination(out_root: Path, unit: str) -> Path:
    """`<out_root>/<unit>`, having proved BOTH that the name is safe and that the result is inside.

    Two checks rather than one, because they fail differently and only the pair is closed. The name
    check refuses the traversal that source-controlled input can express (:class:`UnsafeUnitName`);
    the containment check is the tripwire behind it, and it resolves both sides - a lexical
    comparison passes a directory junction pointing out of the tree, which is the same defect
    `promote_unit._refuse_aliased_root` exists for one hop later.

    Resolving is safe here in a way it is NOT for a data-source literal: both operands are local
    paths this process chose, never a UNC literal out of a customer's M query, so there is no SMB
    host to block on (compare :func:`_inside`).
    """
    problem = unit_name_problem(unit)
    if problem is not None:
        raise UnsafeUnitName(unit, problem)
    root = out_root.resolve()
    destination = (out_root / unit).resolve()
    if destination.parent != root:
        raise UnsafeUnitName(unit, f"it resolves to {destination.name} outside --out")
    return out_root / unit


def replace_dir(staged: Path, final: Path, verify: Callable[[Path], None] | None = None) -> None:
    """Put ``staged`` at ``final``, REPLACING whatever was there - never merging into it.

    ⚠️ **Round-2 blocker: packaging used to merge into an existing `<out>/<unit>`**, because every
    write was `mkdir(exist_ok=True)` / `copytree(dirs_exist_ok=True)` and nothing ever removed a file
    the new input no longer produced. Reproduced end to end: package a unit with a 4-view oracle
    (entry gate READY, 4 ready / 0 blind), then re-run the documented CLI into the SAME `--out` with
    an EMPTY oracle directory. Both runs exit 0, the new `package-manifest.json` correctly reports
    zero oracle objects and `"no oracle-manifest.json found"` - and the PREVIOUS
    `oracle/oracle-manifest.json` survives, so the entry gate still returns **READY, 4 ready / 0
    blind**. An agent then builds against evidence that no longer exists, with a gate agreeing.
    Stale `assets/` and `fabric/` files persisted the same way.

    Replace-not-merge is the fix, staged so a crash mid-build cannot leave a half-package in place of
    a good one. The retired directory is moved aside before the swap and only deleted once the new
    one has landed, and it is restored if the rename fails - on Windows a directory rename onto an
    existing target fails outright, so the move-aside is required rather than defensive.

    ⚠️ The retired name is **shorter than the package it retires**, for the same reason
    :func:`staging_dir` is (#476): `.{name}.replaced` made every path in a package that is being
    REPLACED 10 characters longer than the one being measured, and `shutil.rmtree` then walks it -
    so a package that fits could still fail its second run, in a tree nothing had measured.

    ``verify`` is called with the RETIRED directory after it has been renamed aside and before the
    new one lands. Raising from it restores the retired directory and aborts the swap. That ordering
    is the whole point: the check happens when nothing can still be written to the package through
    its own path, so "unchanged since I looked" is established rather than assumed (round-2 finding
    3). It is also why the deletion of the retired tree is the LAST thing that happens.
    """
    final.parent.mkdir(parents=True, exist_ok=True)
    if not final.exists():
        _rename_retrying(staged, final)
        return
    retired = retired_dir(final)
    _discard_scratch(retired)
    _rename_retrying(final, retired)
    swapped = False
    try:
        if verify is not None:
            verify(retired)
        _rename_retrying(staged, final)
        swapped = True
    finally:
        if not swapped:
            _rename_retrying(retired, final)
    _discard_scratch(retired)


#: Windows denies a directory rename while anything still holds a handle inside it, and a scanner
#: routinely does for a moment after a large write. Measured: renaming a freshly-assembled
#: `HR_Dashboard` staging tree (337 entries, 51 MB of renders) failed `WinError 5` once and succeeded
#: on the first attempt when retried. So this is a race, not a defect - but it is one a user would
#: hit, so it is retried on a BOUNDED budget (2 s) rather than either ignored or waited on forever.
_SWAP_ATTEMPTS = 10
_SWAP_BACKOFF_SEC = 0.2


def _rename_retrying(src: Path, dst: Path) -> None:
    """Rename, retrying a transient Windows lock on a bounded budget before giving up."""
    for attempt in range(1, _SWAP_ATTEMPTS + 1):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == _SWAP_ATTEMPTS:
                raise
            time.sleep(_SWAP_BACKOFF_SEC)


#: The bundle inputs whose CORRUPTION makes a unit unassessable. Absence is legitimate for every one
#: of them (a datasource unit has no handover slice; four workbooks in the reference estate have no
#: engine output at all), so the entry is a path and the caller distinguishes the two states through
#: :func:`read_json_checked`.
#:
#: ⚠️ **`oracle-manifest.json` is deliberately NOT here, and the omission is load-bearing.** An
#: oracle that is missing, absent or truncated must still PACKAGE, so that the entry gate can report
#: the pages it covers as BLIND - that is the negative control the whole packaging contract is
#: written around ("an oracle omission INSIDE a package is not exit 1 or 4", module docstring), and
#: it is verified working. The oracle is evidence ABOUT the unit; these four files are what the unit
#: IS.
def _unassessable_inputs(bundle: Path, unit: str) -> list[str]:
    """Every reason this unit's bundle input exists but could not be read. Empty means assessable."""
    reasons = []
    for path in (
        bundle / "report.json",
        bundle / "handover" / f"{unit}.json",
        bundle / "source-provenance.json",
        bundle / "engine-output-receipt.json",
        bundle / "input_manifest.json",
    ):
        _payload, reason = read_json_checked(path)
        if reason is not None:
            reasons.append(reason)
    return reasons


def _report_pages(dest: Path, report_name: str | None) -> int:
    """How many pages the packaged report declares, or 0 when there is no report to declare any."""
    if not report_name:
        return 0
    pages = read_json(dest / "fabric" / report_name / "definition" / "pages" / "pages.json")
    order = pages.get("pageOrder") if isinstance(pages, dict) else None
    return len(order) if isinstance(order, list) else 0


def _model_binding(dest: Path, report_name: str | None) -> dict[str, Any]:
    """How the packaged report finds its semantic model, and whether that model is IN the package.

    ⚠️ **A `byPath` that does not resolve inside the package is not self-contained**, and nothing
    used to say so. Measured on this branch: a report whose `definition.pbir` reads
    `byPath: ../../Shared/Shared.SemanticModel` - the ordinary shared/published-datasource shape,
    which has fixtures in this repository - packaged at **exit 0** with `manifest_model: null` and
    `self_contained: true`, while the model it names existed nowhere in the folder. The consequence
    is silent by construction: `powerbi-report-author validate` returns `errorCount: 0` for a
    `byPath` naming a model that exists nowhere (it checks reference SHAPE, not target), and the
    report then opens in Desktop with no model at all.

    `byConnection` makes no containment claim - the report is bound to a published model and the
    package was never supposed to carry one - so it is recorded and passed. An absent
    `definition.pbir` is recorded as `absent` and also passed: the report declares no binding, which
    is a different (engine-side) problem from one that declares a binding it cannot honour.
    """
    if not report_name:
        return {"kind": "no_report", "path": None, "resolves_in_package": True}
    pbir_path = dest / "fabric" / report_name / "definition.pbir"
    payload, reason = read_json_checked(pbir_path)
    if reason is not None:
        return {"kind": "unreadable", "path": None, "resolves_in_package": False, "detail": reason}
    if not isinstance(payload, dict):
        return {"kind": "absent", "path": None, "resolves_in_package": True}
    reference = payload.get("datasetReference") if isinstance(payload.get("datasetReference"), dict) else {}
    by_path = reference.get("byPath") if isinstance(reference.get("byPath"), dict) else None
    if by_path is None:
        kind = "byConnection" if reference.get("byConnection") else "absent"
        return {"kind": kind, "path": None, "resolves_in_package": True}
    declared = str(by_path.get("path") or "")
    target = (pbir_path.parent / declared).resolve() if declared else None
    inside = target is not None and dest.resolve() in target.parents
    return {
        "kind": "byPath",
        "path": declared or None,
        "resolves_in_package": bool(target is not None and target.is_dir() and inside),
    }


def _data_source_notes(data_sources: dict[str, Any]) -> list[str]:
    """One `PACKAGE_NOTE` line per way a source did not end up in the package, plus the bind reminder.

    `handover.md` renders these, so every state `_localize_data_sources` records is readable by an
    agent that never opens `package-manifest.json` - which is the file it is told to start from.
    """
    notes = [f"data source {row['file']} not shipped: {row['reason']}" for row in data_sources["omissions"]]
    notes += [
        f"data source {name} could not be shipped, so the model no longer names it: its literal was "
        f"replaced with {UNAVAILABLE_TOKEN} because the original named the packaging machine only"
        for name in data_sources["neutralized"]
    ]
    notes += [
        f"data source {name} is a network share this packager does not probe, so it was left in the "
        "model verbatim and its bytes are NOT in this package"
        for name in data_sources["retained_network"]
    ]
    if data_sources["binding"]:
        notes.append(
            f"this package is UNBOUND: its folder parameter reads {PACKAGE_ROOT_TOKEN}, so run "
            f"`{BIND_COMMAND}` before opening the model"
        )
    return notes


def _stage_asset(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    bundle: Path, unit: str, dest: Path, handover: Any, assets_dir: Path | None, report_name: str | None
) -> tuple[Path | None, str, str | None]:
    """`(packaged asset, route, note)` - copy the source in, or refuse when its absence blinds a gate.

    ⚠️ **A report with pages and no source is UNASSESSABLE, not merely incomplete.** `check_unit`
    cannot derive an expected page set without it (#443) and the entry gate returns
    CANNOT_ESTABLISH, so every per-page verdict such a package would produce is "I do not know" -
    and this used to report `exit 0  OK Book`. A unit with no report (every datasource-only unit,
    18 of 67 in the reference run) makes no page claim, so its missing asset stays a recorded note.
    """
    asset, route = resolve_asset(bundle, unit, handover, assets_dir)
    if asset is not None:
        assert_declared_digest(unit, bundle, asset, route)
        (dest / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, dest / "assets" / asset.name)
        return dest / "assets" / asset.name, route, None
    pages = _report_pages(dest, report_name)
    if pages:
        raise UnassessableInput(
            unit,
            [
                f"its source asset could not be resolved ({route}) while its report declares {pages} "
                "page(s), so neither gate can establish a page verdict"
            ],
        )
    return None, route, f"source asset unresolved ({route}); both gates will report CANNOT_ESTABLISH"


def _stage_handover(bundle: Path, unit: str, dest: Path) -> tuple[Any, list[str], str | None]:
    """`(scoped handover slice, redacted paths, note)` written into the package."""
    handover = read_json(bundle / "handover" / f"{unit}.json")
    (dest / "handover").mkdir(parents=True, exist_ok=True)
    if not isinstance(handover, dict):
        return handover, [], f"no handover slice at handover/{unit}.json"
    cleaned, redactions = scope_handover(handover, unit)
    write_json(dest / "handover" / f"{unit}.json", cleaned)
    return cleaned, redactions, None


def _assemble_unit(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    bundle: Path, unit: str, dest: Path, *, final: Path, oracle_dir: Path | None, assets_dir: Path | None
) -> dict[str, Any]:
    """Build one unit's package into ``dest``, which is always a fresh, empty directory.

    ``final`` is where ``dest`` will be renamed to. Anything that must record its OWN separator
    flavour - only the data-folder parameter today - has to use it, because ``dest`` stops existing
    the moment packaging succeeds. Neither path is written INTO the package (round-2 finding 1).

    ⚠️ **The assessability check runs FIRST, before a single byte is copied.** An input that exists
    and cannot be read is refused here rather than absorbed into a note, so nothing is written and a
    previously-good package at ``final`` survives untouched.
    """
    unassessable = _unassessable_inputs(bundle, unit)
    if unassessable:
        raise UnassessableInput(unit, unassessable)
    engine_report = read_json(bundle / "report.json")
    workbooks, datasources = engine_unit_names(engine_report)
    dest.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    report_name, model_name = _copy_fabric(bundle, unit, dest)
    if report_name is None and model_name is None:
        notes.append(f"no engine working copy at pbip/{unit} - nothing to build on")
    data_sources = _localize_data_sources(dest, final, model_name)
    notes.extend(_data_source_notes(data_sources))

    handover, redactions, handover_note = _stage_handover(bundle, unit, dest)
    if handover_note:
        notes.append(handover_note)
    asset, asset_route, asset_note = _stage_asset(bundle, unit, dest, handover, assets_dir, report_name)
    if asset_note:
        notes.append(asset_note)

    _payload, entries = scope_provenance(read_json(bundle / "source-provenance.json"), sha256_of(asset))
    identity = workbook_identity(entries, asset)
    write_json(dest / "source-provenance.json", shippable_provenance(entries, identity, unit))
    write_json(dest / "report.json", scope_report(engine_report, unit))
    receipt = scope_receipt(read_json(bundle / "engine-output-receipt.json"), unit)
    if receipt is not None:
        write_json(dest / "engine-output-receipt.json", receipt)

    oracle = _attach_oracle(oracle_dir, identity, dest, unit)
    spec, spec_note = _write_spec(asset, dest)
    if spec_note:
        notes.append(spec_note)
    schema, schema_note = _write_spec_schema(dest)
    if schema_note:
        notes.append(schema_note)
    if redactions:
        notes.append(
            f"redacted {len(redactions)} absolute host path(s) from the handover slice: {', '.join(redactions[:5])}"
        )

    binding = _model_binding(dest, report_name)
    if binding["kind"] == "byPath" and not binding["resolves_in_package"]:
        notes.append(
            f"the report's definition.pbir names a semantic model at {binding['path']} that is NOT in "
            "this package, so it opens with no model; promote the model this report shares and rewrite "
            "byPath, or repackage the unit that owns it"
        )
    result = {
        "unit": unit,
        "kind": unit_kind(unit, workbooks, datasources),
        "engine": ((receipt or {}).get("engine") or {}).get("version"),
        "packaged": report_name is not None or model_name is not None,
        "self_contained": bool(data_sources["self_contained"] and binding["resolves_in_package"]),
        "artifacts": {
            "migration_spec": spec,
            "migration_spec_schema": schema,
            "asset": f"assets/{asset.name}" if asset else None,
            "asset_route": asset_route,
            "report": f"fabric/{report_name}" if report_name else None,
            "model": f"fabric/{model_name}" if model_name else None,
            "handover": f"handover/{unit}.json" if isinstance(handover, dict) else None,
        },
        "model_binding": binding,
        "workbook_identity": identity,
        "data_sources": data_sources,
        "oracle": oracle,
        "notes": notes,
    }
    report_dir = dest / "fabric" / report_name if report_name else None
    workbook = _handover_workbook(handover, unit, dest)
    (dest / "handover.md").write_text(render_handover(result, workbook, visual_pages(report_dir)), encoding="utf-8")
    (dest / "README.md").write_text(
        README.format(unit=unit, kind=result["kind"], package_root=PACKAGE_ROOT_TOKEN, unavailable=UNAVAILABLE_TOKEN),
        encoding="utf-8",
    )
    result["contents"] = {"files": package_contents(dest)}
    write_json(dest / MANIFEST_NAME, result)
    return result


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def _run_totals(
    results: list[dict[str, Any]], refused: list[PackageEditsRefused], failed: list[PackagingError]
) -> list[str]:
    """The lines after the per-unit list: every state that must not be inferred from a silence."""
    lines: list[str] = []
    starved = sorted(result["unit"] for result in results if not result["packaged"])
    if starved:
        lines.append(
            f"WARN: {len(starved)} unit(s) have NO engine working copy under pbip/ - packaged for their "
            f"source, reference and handover only, with nothing to build on: {', '.join(starved)}"
        )
    if refused:
        lines.append(
            f"REFUSED: {len(refused)} unit(s) already carry edits in the package, which is the canonical "
            "place to work - they were left untouched. Re-run with --discard-package-edits to overwrite."
        )
    unassessable = sorted(item.unit for item in failed if isinstance(item, UnassessableInput))
    if unassessable:
        lines.append(
            f"CANNOT ASSESS: {len(unassessable)} unit(s) had an input that exists but could not be read, "
            "so nothing was packaged for them - this is neither a clean nor a failed verdict: "
            f"{', '.join(unassessable)}"
        )
    hard = sorted(getattr(item, "unit", "?") for item in failed if not isinstance(item, UnassessableInput))
    if hard:
        lines.append(
            f"UNIT FAILED: {len(hard)} unit(s) hit a contradiction this packager refuses to ship past, "
            f"so nothing was written for them: {', '.join(hard)}"
        )
    incomplete = sorted(result["unit"] for result in results if not result.get("self_contained", True))
    if incomplete:
        lines.append(
            f"NOT SELF-CONTAINED: {len(incomplete)} unit(s) ship without something they name - a source "
            "their model reads, or the semantic model their report's byPath points at - so the package "
            f"alone is not enough to build on; see {MANIFEST_NAME}: {', '.join(incomplete)}"
        )
    unbound = sorted(result["unit"] for result in results if result["data_sources"].get("binding"))
    if unbound:
        lines.append(
            f"UNBOUND: {len(unbound)} unit(s) read their rows through a {PACKAGE_ROOT_TOKEN} placeholder. "
            f"Wherever a package ends up, run `{BIND_COMMAND}` there before opening the model."
        )
    return lines


def render(
    results: list[dict[str, Any]],
    out_root: Path,
    refused: list[PackageEditsRefused] | None = None,
    failed: list[PackagingError] | None = None,
) -> str:
    """The human verdict: one line per unit, then the totals that make an omission visible."""
    lines = [f"package_unit: {len(results)} unit(s) -> {out_root}"]
    for result in sorted(results, key=lambda item: item["unit"]):
        oracle = result["oracle"]
        objects = oracle.get("objects") or []
        untyped = sum(1 for obj in objects if obj["view_type"] == KIND_UNKNOWN)
        detail = f"{len(objects)} oracle object(s) via {oracle.get('route')}" if objects else "no oracle evidence"
        lines.append(
            f"  {'OK  ' if result['packaged'] else 'MISS'} {result['unit']} [{result['kind']}] - {detail}"
            + (f", {untyped} untyped" if untyped else "")
            + (f"; {len(result['notes'])} note(s)" if result["notes"] else "")
        )
    for refusal in sorted(refused or [], key=lambda item: item.unit):
        lines.append(f"  KEPT {refusal.unit} - not repackaged, the existing package carries edits")
    for failure in sorted(failed or [], key=lambda item: getattr(item, "unit", "")):
        label = "CANT" if isinstance(failure, UnassessableInput) else "FAIL"
        lines.append(f"  {label} {getattr(failure, 'unit', '?')} - NOT packaged: {failure}")
    packaged = sum(1 for result in results if result["packaged"])
    with_oracle = sum(1 for result in results if result["oracle"].get("objects"))
    lines.append(f"packaged {packaged}/{len(results)}; {with_oracle} carry oracle evidence")
    lines.extend(_run_totals(results, refused or [], failed or []))
    return "\n".join(lines)


def _package_each(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    units: list[str],
    bundle: Path,
    out_root: Path,
    oracle_dir: Path | None,
    assets_dir: Path | None,
    discard_edits: bool,
    results: list[dict[str, Any]],
    refused: list[PackageEditsRefused],
    failed: list[PackagingError] | None = None,
) -> None:
    """Package each unit, collecting refusals instead of stopping at the first one.

    One unit's edits must not stop the rest of the estate being packaged; `main` still returns 3 for
    any refusal, so this cannot pass unnoticed.

    ⚠️ **Every :class:`PackagingError` is collected, not just the edit refusal.** Before this, an
    unassessable input, an unsafe unit name or a containment tripwire escaped as an uncaught
    traceback: the interpreter's exit 1 is indistinguishable from `EXIT_NO_WORKING_COPY`, and the
    remaining units of an estate were never packaged at all. Collecting them keeps the run going and
    gives each class its own exit code; nothing is written for a unit that raises, because assembly
    happens in a staging directory that the `finally` in :func:`package_unit` removes.
    """
    for unit in units:
        try:
            results.append(
                package_unit(
                    bundle,
                    unit,
                    out_root,
                    oracle_dir=oracle_dir,
                    assets_dir=assets_dir,
                    discard_edits=discard_edits,
                )
            )
        except PackageEditsRefused as refusal:
            print(str(refusal), file=sys.stderr)
            refused.append(refusal)
        except PackagingError as failure:
            print(str(failure), file=sys.stderr)
            if failed is None:
                raise
            failed.append(failure)


def _build_parser() -> argparse.ArgumentParser:
    """The CLI surface, in its own function so ``main`` stays inside its complexity budget."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("Attribution", maxsplit=1)[0])
    parser.add_argument("--bundle", type=Path, required=True, help="engine bundle root (holds pbip/, handover/)")
    parser.add_argument("--out", type=Path, required=True, help="directory to write <Unit>/ packages into")
    parser.add_argument("--unit", action="append", default=[], help="package only this unit (repeatable)")
    parser.add_argument("--oracle", type=Path, help="oracle capture holding oracle-manifest.json")
    parser.add_argument("--assets", type=Path, help="directory holding the harvested .twb/.twbx/.tds assets")
    parser.add_argument("--json", type=Path, help="write the machine-readable packaging report here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered summary")
    parser.add_argument(
        "--discard-package-edits",
        action="store_true",
        help="overwrite an existing package even though it carries edits made since it was written",
    )
    return parser


def _refuse_zero_units(bundle: Path, out_root: Path, json_path: Path | None) -> int:
    """The verdict for a bundle that names no units at all.

    ⚠️ **`packaged 0/0` at exit 0 is TRUE and useless**, and it is the shape a caller reads as "the
    estate is packaged" - measured on this branch. The emptiness is a fact about the BUNDLE rather
    than about the command line, so it gets the cannot-assess code rather than argparse's 2, and the
    `--json` envelope is still written so an automated caller has a record of the refusal.
    """
    print(
        f"cannot assess {bundle}: its report.json lists no workbooks or datasources and it has no "
        "pbip/ working copies, so there is nothing to package and 'packaged 0/0' would read as "
        "success. Point --bundle at an engine run, or check that report.json parsed.",
        file=sys.stderr,
    )
    if json_path:
        write_json(
            json_path,
            {
                "id": "package-unit",
                "bundle": str(bundle),
                "out": str(out_root),
                "units": [],
                "refused": [],
                "failed": [],
                "cannot_assess": ["the bundle names no units"],
            },
        )
    return EXIT_CANNOT_ASSESS


def _run_verdict(
    results: list[dict[str, Any]], refused: list[PackageEditsRefused], failed: list[PackagingError]
) -> int:
    """The run's exit code, worst first.

    The two "nothing was written" states rank ABOVE every verdict about content: a verdict computed
    from input that could not be read, or reported alongside a unit that never got packaged at all,
    is the fail-open shape this ordering exists to make impossible.
    """
    if any(isinstance(failure, UnassessableInput) for failure in failed):
        return EXIT_CANNOT_ASSESS
    if failed:
        return EXIT_UNIT_FAILED
    if refused:
        return EXIT_EDITS_REFUSED
    if any(not result.get("self_contained", True) for result in results):
        return EXIT_NOT_SELF_CONTAINED
    return EXIT_OK if all(result["packaged"] for result in results) else EXIT_NO_WORKING_COPY


def _refuse_out_too_deep(
    parser: argparse.ArgumentParser, bundle: Path, units: list[str], out_root: Path, assets_dir: Path | None
) -> list[PathBudget]:
    """Refuse the WHOLE run when any unit's projected paths would not fit under ``out_root`` (#476).

    Measured for EVERY unit before ANY of them is assembled. The field failure this replaces crashed
    with `[WinError 206]` having already written 29 of 47 packages, so the operator paid for 29 units
    of work AND still had to repackage the estate under a shorter `--out`.

    Returns the budgets so the shipping advisory reads the same measurement rather than taking it
    twice and risking a different answer.
    """
    budgets = [path_budget(bundle, unit, out_root, assets_dir=assets_dir) for unit in units]
    too_deep = [budget for budget in budgets if budget.refused]
    if too_deep:
        parser.error(render_out_too_deep(too_deep, len(units)))
    return budgets


def _prepare_out(parser: argparse.ArgumentParser, requested: Path) -> Path:
    """The resolved `--out`, created, having proved it does not shadow the evidence the gates scan."""
    out_root = requested.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    shadowing = conflicting_evidence_dirs(out_root)
    if shadowing:
        parser.error(
            f"--out {requested} sits beside evidence the gates also scan "
            f"({', '.join(str(path) for path in shadowing)}). A package there is matched against BOTH "
            "its own oracle and that one, and every page becomes 'unverifiable' rather than ready. "
            "Choose an --out outside the capture tree."
        )
    return out_root


def _warn_shipping(budgets: list[PathBudget]) -> None:
    """Print the relocation advisory, if any unit earned one. Never changes the verdict."""
    advisory = render_shipping_advisory(budgets)
    if advisory:
        print(advisory, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Package the requested units and report what each one carries."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    bundle = args.bundle.resolve()
    if not bundle.is_dir():
        parser.error(f"--bundle {args.bundle} is not a directory")
    oracle_dir = args.oracle.resolve() if args.oracle else discover_dir(bundle, ("oracle", "_oracle"))
    assets_dir = args.assets.resolve() if args.assets else discover_dir(bundle, ("assets",))

    available = bundle_units(bundle)
    units = args.unit or available
    unknown = [unit for unit in units if unit not in available]
    if unknown:
        parser.error(f"the bundle's report.json and pbip/ know nothing of: {', '.join(sorted(unknown))}")

    out_root = _prepare_out(parser, args.out)
    if not units:
        return _refuse_zero_units(bundle, out_root, args.json)

    budgets = _refuse_out_too_deep(parser, bundle, sorted(units), out_root, assets_dir)
    _warn_shipping(budgets)
    results: list[dict[str, Any]] = []
    refused: list[PackageEditsRefused] = []
    failed: list[PackagingError] = []
    _package_each(
        sorted(units), bundle, out_root, oracle_dir, assets_dir, args.discard_package_edits, results, refused, failed
    )

    payload = {
        "id": "package-unit",
        "bundle": str(bundle),
        "out": str(out_root),
        "oracle": str(oracle_dir) if oracle_dir else None,
        "assets": str(assets_dir) if assets_dir else None,
        "units": results,
        "refused": [
            {"unit": refusal.unit, "changed": refusal.changed, "reason": refusal.reason} for refusal in refused
        ],
        "failed": [
            {
                "unit": getattr(failure, "unit", None),
                "state": "cannot_assess" if isinstance(failure, UnassessableInput) else "unit_failed",
                "reason": str(failure),
            }
            for failure in failed
        ],
        # The relocation number, per unit, travelling WITH the report: how long a Windows root each
        # package still tolerates. Nothing downstream re-measures it, and it is the one figure that
        # says whether a package that is valid here will open where a customer unpacks it.
        "shipping_root_budget": {budget.unit: budget.shipping_budget for budget in budgets},
    }
    if args.json:
        write_json(args.json, payload)
    if not args.quiet:
        print(render(results, out_root, refused, failed))
    return _run_verdict(results, refused, failed)


if __name__ == "__main__":
    raise SystemExit(main())
