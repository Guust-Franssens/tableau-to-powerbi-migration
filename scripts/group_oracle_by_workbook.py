"""
purpose: group flat `capture_tableau_oracle.py` captures into per-workbook reference folders
usage:   python scripts/group_oracle_by_workbook.py --oracle-root _oracle
         python scripts/group_oracle_by_workbook.py --oracle _oracle [--oracle _oracle-retry ...]
                                                    [--migrations migrations/workbooks] [--dry-run]

`capture_tableau_oracle.py` writes every view flat into `<oracle>/data/` and `<oracle>/images/`,
with workbook association living only in `oracle-manifest.json`. That is deliberate and is NOT
changed here: a LUID-keyed flat layout survives a workbook or view rename, which a folder-per-
workbook layout cannot, so the capture stays the authoritative artifact.

This script is the browse-time convenience on top of it. It COPIES (never moves) each view's files
into `migrations/workbooks/<slug>/reference/{images,data}/`, which is the layout the rest of this
toolkit already uses, and writes a per-workbook `oracle-manifest.json` subset beside them.

Why a separate script rather than a `--group-by-workbook` flag on the capture:

* it re-runs against an EXISTING capture, costing no REST calls. Tableau's `/views/.../data` and
  `/image` endpoints are metered (100 calls/hr/Creator), so re-capturing merely to change the
  on-disk layout is the expensive way to get the same bytes.
* the capture can therefore stay a pure "talk to the API" step, and this a pure "arrange local
  files" step, which is testable with no network at all.

MATCHING IS AGAINST FOLDERS THAT ALREADY EXIST, and never by slugifying a name into a path.
Both sides are normalized (lowercased, non-alphanumerics dropped) and compared; a workbook whose
folder is absent is REPORTED, not created, and a name that normalizes onto two folders is reported
as ambiguous rather than resolved by picking one.

That normalizer is deliberately narrow, and its limits are known: dropping non-alphanumerics
collapses punctuation and case but never words, so a name carrying Tableau's cross-project
disambiguation suffix (`"Sales | Project : Finance"`) does NOT match a `sales` folder. It is
reported as unmatched -- which is the honest outcome, and the reason this script's exit code
distinguishes "grouped everything" from "grouped what it could".

`--oracle` IS REPEATABLE, and it has to be (issue #423)
-------------------------------------------------------
A metered, timing-out capture is re-run in BATCHES, and the same view can succeed in a later batch
having failed in an earlier one. Field evidence: *Daily Monitoring* failed its data leg twice, then
on a third batch produced both a data leg and 905,098 bytes of PNG -- and the workbook's
`reference/` folder only ever cross-referenced the first two batches, so a successful capture sat
unused on disk. Grouping one directory at a time cannot fix that: the last invocation overwrites the
per-workbook manifest, so it does not merely miss the good artifact, it can REPLACE a good one with
a failure from a partial re-run.

So every batch is read and merged per view and PER LEG, newest-successful-wins:

* a leg is a candidate only if its status is `ok` AND the artifact it names is on disk -- a manifest
  entry alone is a claim, and this script already refuses to promote claims it cannot back;
* if no batch has a successful leg, the newest batch **that has a record for THAT leg** is kept, so
  the failure (or `not_attempted`, or `source_credential`) stays visible. Not the newest batch
  overall: a later data-only batch has no `image` record at all, and taking its view wholesale threw
  an older batch's `image: transient` away -- a known render gap silently reclassified as "never
  requested", which is the collapse this whole change exists to prevent;
* render INTENT is UNIONED across batches (`requested_renders`, `reference_required`), never taken
  from the newest alone, for the same reason: a batch that asked for nothing cannot retract another
  batch's request. `requested_renders_by_batch` records who asked for what, so the disagreement is
  visible and not merely resolved;
* every promoted leg records `source_batch`, and the merged manifest lists `batches`, so "which
  capture did this image come from" is answerable from the artifact rather than from memory.

WARNING: "Newest" is the view record's `captured_at`, falling back to the batch manifest's, and
`merge_order_basis` has THREE values because two of them are not "the timestamps decided it":

* `captured_at` -- every batch dated, and no tie decided a leg;
* `captured_at, ties broken by argument order` -- dated, but two candidates for some leg shared a
  timestamp, so the last `--oracle` won it. Measured: identical stamps produced DIFFERENT winners
  when the arguments were reversed while the manifest still claimed time had decided it. The tied
  legs are named in `merge_order_ties`;
* `argument order` -- a batch carries no timestamp anywhere, so nothing can be dated.

EVERY BATCH ON DISK, and the four things that must be true for that to be safe (#423 criterion 3)
--------------------------------------------------------------------------------------------------
Reading only the directories somebody typed does not merge "every batch on disk" -- it merges every
argument the operator remembered, and the difference is invisible in the output. Measured: a third
retry whose PNG had finally landed sat unread on disk while the merged manifest reported
`image: transient` and listed only the two batches it was given, exit 0.

* `--oracle-root DIR` DISCOVERS batches instead of listing them. Discovery needs a defined ROOT --
  there is no filesystem-wide answer to "where are my captures" -- and a defined SHAPE: a directory
  holding an `oracle-manifest.json` whose `schema` is `tableau-oracle/1`. The root may itself be a
  batch, which is the ordinary `_oracle/` layout.
* Anything else under that root is a BLOCKING answer, never a skip: an unclassifiable directory means
  either the root is wrong or a capture is damaged, and skipping it would move the boundary a third
  time, to "every directory I recognised".
* With `--oracle`, a capture batch sitting UNLISTED beside a given one is refused rather than
  silently omitted, so the listing mode cannot quietly under-merge either.
* `--exclude DIR` is the one auditable escape from both refusals, and is recorded in the merged
  manifest as `excluded_paths` -- an exclusion nothing records is indistinguishable from an omission.

A MANIFEST IS VALIDATED, NOT MERELY PARSED (exit 2)
----------------------------------------------------
`--oracle-root` has always defined the shape it accepts (`is_capture_batch`); `--oracle` accepted
whatever `json.loads` returned, and the gap between "this is JSON" and "this is a capture manifest"
was reported as a clean merge. Measured through the CLI: `{}` and a schema-carrying manifest with no
`views` key each exited **0** claiming `0 grouped`, and `[]` exited **1** with an unhandled
`AttributeError` -- while a zero-byte or truncated file was already correctly refused at 2. Both
modes now go through one validator, so an unassessable input is an input REFUSAL (exit 2) rather
than the "clean" bucket: a JSON object, carrying `schema` `tableau-oracle/1`, with a `views` list
(EMPTY is legitimate -- a capture that selected nothing; ABSENT is a damaged file) whose every
record is an object carrying a `view_luid`, and every field this script reads carries the type it
reads it as (`_MANIFEST_TYPES`/`_VIEW_TYPES`/`_LEG_TYPES` -- one declared table, so the next
unvalidated field is not discovered by a crash: `view_luid: 123` was truthy and merged CLEAN, while
`view_luid: {...}` and `image: "junk"` crashed at exit 1).

That last one is data loss rather than a missing field, which is why it refuses instead of being
bucketed like a missing `workbook_luid`: merging is keyed on view identity, so records without one
collapse onto a single bucket and newest-wins discards the rest before any outcome bucket exists to
report them. Measured: two different views, both without `view_luid`, in ONE valid manifest ->
`grouped_views: 1`, exit 0, view B named nowhere. A view name is not a substitute identity.

WORKBOOKS ARE KEYED BY LUID, AND A DESTINATION COLLISION IS REFUSED
-------------------------------------------------------------------
Views are bucketed by `workbook_luid`, never by display name. Two DIFFERENT workbooks whose names
normalize onto one key used to target one folder, the second manifest overwriting the first while
both workbooks' files stayed on disk -- and on the real 48-workbook reference estate that is not
hypothetical: `Seed - R&D`, `Seed - R+D` and `Seed - R/D` are three distinct LUIDs and ONE normalized
key. Neither side of a collision is written; both are reported. A view with no `workbook_luid` is
refused for the same reason: a display name is not an identity.

PROMOTION IS RECONCILED, NOT LAYERED
-------------------------------------
Copying the selected artifacts is only half of promotion. An artifact an earlier run promoted and
this one REFUSES (a stale cross-revision render, say) stays physically in `reference/images/` unless
something removes it, and a consumer that reads the directory rather than the manifest then gets
evidence this merge explicitly rejected. Files a previous grouped manifest named are removed; files
nothing accounts for are REPORTED and left alone, because they may not be ours to delete.
"""

from __future__ import annotations

# The module is long because the rules are: nine short functions carry ~400 lines of measured
# provenance for the guards below, and splitting the merge from the placement would put a shared
# invariant in two files. Same waiver as `assess_estate.py`, `check_unit.py` and `run_estate.py`.
# pylint: disable=too-many-lines

import argparse
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any

import tableau_oracle_manifest

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The ONE census, shared with the capture rather than re-derived here: the two manifests must agree
# on what "no establishable render" means, and a second copy of that rule is how they drift apart.
# This is a pure function over records -- it makes no request and needs no session -- so importing it
# keeps this script the offline, network-free step its module docstring promises.
from tableau_oracle_manifest import render_unestablished  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("group-oracle")

MANIFEST_NAME = "oracle-manifest.json"
UNMATCHED_REPORT = "oracle-grouping-report.json"
# The `schema` a CAPTURE manifest carries (`tableau_oracle_manifest.py`). Discovery matches on it
# rather than on the filename, because this script's own per-workbook output uses the same filename
# with schema `tableau-oracle-workbook/1` -- accepting that as a batch would feed output into input.
CAPTURE_SCHEMA = "tableau-oracle/1"
# The subdirectories a capture writes beside its manifest. Under `--oracle-root`, when the root is
# itself a batch, these are its structure rather than candidate batches.
CAPTURE_SUBDIRS = frozenset({"images", "data"})


@dataclass(frozen=True)
class _Context:
    """The per-run state `_group_one` reads; everything here is constant across workbooks."""

    manifest: dict[str, Any]
    destinations: dict[str, list[Path]]
    roots: dict[str, Path]
    dry_run: bool


@dataclass(frozen=True)
class _Batch:
    """One `_oracle/<dir>` capture, plus the two things the merge orders by.

    ``label`` is the directory NAME -- what a report shows and what a leg's ``source_batch`` records.
    ``order`` is the position on the command line, and is the LAST-RESORT tiebreak only: a batch that
    carries no ``captured_at`` anywhere cannot be dated, and argv order is an operator's habit rather
    than evidence, so relying on it is reported (see :func:`merge_batches`).
    """

    directory: Path
    manifest: dict[str, Any]
    label: str
    order: int

    @property
    def captured_at(self) -> str:
        """The batch-level capture time, or ``""`` when this manifest does not carry one."""
        return self.manifest.get("captured_at") or ""


class DuplicateBatchLabel(ValueError):
    """Two capture directories would carry the same ``source_batch`` label. Refused, never resolved."""


class IncompatibleBatchSources(ValueError):
    """Two capture directories record DIFFERENT Tableau sources. Refused, never merged.

    ⚠️ Cross-tenant evidence mixing, and the reason this is a hard refusal rather than a warning.
    Measured before this existed: two batches from different servers and different sites, sharing only
    a workbook CAPTION, were folded into one manifest that declared tenant B's ``server`` and ``site``
    at the top level while its views carried artifacts from tenant A **and** tenant B. Nothing in the
    output said so; ``source_batch`` named a directory, not a tenant, and a reader with the manifest in
    front of them had no way to tell.

    A caption collision across tenants is not exotic -- "Sales Dashboard" and "HR Dashboard" exist
    everywhere -- and the consequence is one customer's data presented as another's reference evidence.
    """


class UnestablishedBatchSource(ValueError):
    """A batch does not record its source at all, so sameness CANNOT BE ESTABLISHED. Refused.

    Deliberately a different type from :class:`IncompatibleBatchSources`: "these are two tenants" and
    "we cannot tell whether these are two tenants" are different answers, and a test that can only
    assert *something* refused cannot tell which guard it exercised. Both block; only one of them is a
    statement about the data.
    """


class UnlistedBatchOnDisk(ValueError):
    """A capture batch sits beside the ones that were listed and was not passed. Refused.

    ⚠️ Review round 3, finding 5. Merging "every batch the operator remembered to type" is not the
    same promise as merging every batch on disk, and the difference is invisible in the output:
    measured, a third retry whose PNG had finally landed sat unread while the merged manifest reported
    ``image: transient`` and listed only the two batches it was given. Passing the directory, or
    ``--oracle-root``, or an explicit ``--exclude``, are all fine; silently proceeding is not.
    """


class UnclassifiedCaptureDirectory(ValueError):
    """A directory under ``--oracle-root`` is neither a capture batch nor excluded. Refused.

    Discovery only means "every batch on disk" if the shape of a batch is defined AND anything not
    matching it is a blocking answer. Skipping the unrecognised directory would move the boundary a
    third time -- from "every argument you typed" to "every directory I happened to recognise".
    """


class MalformedCaptureManifest(ValueError):
    """A named ``oracle-manifest.json`` parsed as JSON but is NOT a capture manifest. Refused.

    ⚠️ The file-level failures were already fail-closed and this one was not: a zero-byte or truncated
    manifest raises ``JSONDecodeError`` and exits 2, but valid JSON that is not a capture manifest was
    handed straight to the merge. Measured through the CLI before this existed:

    * ``{}`` -> **exit 0**, "0 workbook(s) ... 0 grouped" -- a damaged manifest reported as a clean merge;
    * ``{"schema": "tableau-oracle/1", "server": ..., "site": ...}`` with no ``views`` -> **exit 0**;
    * ``[]`` -> **exit 1** with an unhandled ``AttributeError`` from ``merge_batches``, i.e. a crash
      rather than the documented input refusal.

    ``--oracle-root`` never had this hole, because :func:`is_capture_batch` defines the shape it will
    accept; ``--oracle`` bypassed that definition entirely. The asymmetry WAS the defect, so this
    applies the same definition to both, plus the structure the merge actually depends on.
    """


class UnidentifiedCaptureView(ValueError):
    """A capture manifest carries a view with no ``view_luid``, so WHICH view it is cannot be established.

    ⚠️ This is silent data LOSS, not merely a missing field, and that is why it refuses at load rather
    than being bucketed like a missing ``workbook_luid``. :func:`merge_batches` folds records together
    by view identity; coercing a missing one to ``""`` makes every identity-less record in the estate
    collide onto a single bucket, and newest-wins then discards all but one. Measured before this
    existed, on ONE valid manifest holding two different views that both lacked ``view_luid``::

        exit: 0    input_views: 2    grouped_views: 1    names: ["A"]

    View B was not reported anywhere -- not as failed, not as unidentified, not in a count. The
    missing-``workbook_luid`` precedent is deliberately NOT reused here: that one loses nothing (the
    view survives, is bucketed under ``""``, is named in ``unidentified`` and holds the exit code
    non-zero), because attribution is what cannot be established. Here the RECORD does not survive the
    merge at all, so there is nothing downstream left to report it -- the refusal has to happen before
    the fold, which makes it an input refusal (exit 2) like :class:`UnestablishedBatchSource`.

    A view NAME is not a substitute identity: it is server-supplied response data, it is not unique,
    and this repository already refuses to build an artifact stem from anything but a validated LUID
    (``capture_tableau_oracle.artifact_stem``). The real capture writer always stamps ``view_luid``
    (``view_luid = view["id"]``), so a manifest without one is damaged or hand-edited, never routine.

    ⚠️ **A WRONG-TYPED identity is the same defect and was the worse half of it.** The first version of
    this guard tested truthiness, which answers "is there something there" rather than "is it an
    identity". Measured through the CLI afterwards:

    * ``view_luid: 123`` -> truthy and hashable, so it bucketed, merged and reported a **clean** run;
    * ``view_luid: {"nested": "id"}`` / ``["a"]`` -> truthy and UNHASHABLE, ``TypeError`` at exit 1;
    * ``view_luid: true`` -> truthy, merged, and grouped under the bucket key ``True``.

    :func:`_is_view_identity` types it instead: a non-empty string, and nothing else.
    """


def _capture_schema(directory: Path) -> str | None:
    """The ``schema`` of ``directory``'s capture manifest, or ``None`` when there is no readable one.

    Read defensively on purpose: discovery must classify a directory without trusting its contents,
    and an unreadable or non-JSON manifest is "not a batch I can recognise", which the caller turns
    into a blocking answer rather than a skip.
    """
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return payload.get("schema") if isinstance(payload, dict) else None


def is_capture_batch(directory: Path) -> bool:
    """Is this directory a CAPTURE batch -- the defined shape discovery is allowed to accept?

    ⚠️ The schema check is not decoration. ``migrations/workbooks/<slug>/reference/`` also holds a
    file called ``oracle-manifest.json``, and it is a GROUPED subset (``tableau-oracle-workbook/1``),
    not a capture. Accepting one as a batch would feed this script's own output back into its input.
    """
    return _capture_schema(directory) == CAPTURE_SCHEMA


def discover_batches(root: Path, excluded: frozenset[Path]) -> list[Path]:
    """Every capture batch under ``root`` -- the on-disk reading of #423's acceptance criterion 3.

    "Every batch on disk" needs two definitions before it can be safe, and this supplies both:

    * a DEFINED ROOT. There is no filesystem-wide answer to "where are my captures", so the operator
      still names the tree. What discovery adds -- and what listing directories cannot -- is that a
      batch under that root which nobody typed IS found.
    * a DEFINED SHAPE. A batch is a directory holding an ``oracle-manifest.json`` whose ``schema`` is
      :data:`CAPTURE_SCHEMA`. ``root`` ITSELF may be a batch (the ordinary ``_oracle/`` layout), in
      which case its structural subdirectories are expected rather than candidates.

    ⚠️ Anything else under ``root`` is a BLOCKING answer, never a skip. A directory that cannot be
    classified means either the root is wrong or a capture is damaged, and both need the operator --
    silently ignoring it would move the boundary from "every argument you typed" to "every directory
    I recognised", which is the same defect wearing different clothes. ``--exclude`` is the explicit,
    recorded way to say "I have seen this and it is not evidence".
    """
    if not root.is_dir():
        raise FileNotFoundError(f"--oracle-root {root} is not a directory")
    found: list[Path] = []
    root_is_batch = is_capture_batch(root)
    if root_is_batch:
        found.append(root)
    unclassified: list[Path] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        child_resolved = child.resolve()
        oracle = child / "oracle"
        oracle_resolved = oracle.resolve()
        if child_resolved in excluded or oracle_resolved in excluded:
            continue
        if root_is_batch and child.name in CAPTURE_SUBDIRS:
            continue
        if is_capture_batch(child):
            found.append(child)
            continue
        if is_capture_batch(oracle):
            found.append(oracle)
            continue
        unclassified.append(child)
    if unclassified:
        raise UnclassifiedCaptureDirectory(
            f"{len(unclassified)} director(ies) under {root} are not capture batches and were not "
            f"excluded: {', '.join(str(p) for p in unclassified)}. A discovery root means every batch "
            f"beneath it is merged, so a directory that cannot be classified is a blocking answer, "
            f"not something to skip -- either --oracle-root names the wrong tree, or one of these is "
            f"a damaged capture. Pass --exclude <dir> for each one that is deliberately not evidence."
        )
    if not found:
        raise FileNotFoundError(
            f"no capture batch under {root}: nothing there holds an {MANIFEST_NAME} with "
            f"schema {CAPTURE_SCHEMA!r}. Run capture_tableau_oracle.py first."
        )
    return found


def _refuse_unlisted_siblings(listed: list[Path], excluded: frozenset[Path]) -> None:
    """Refuse when a capture batch sits beside a listed one and was not given (#423, criterion 3).

    ⚠️ This is what stops ``--oracle`` from quietly meaning "every batch you remembered". The scan is
    narrow on purpose -- only the immediate parents of the directories actually named, and only
    siblings that ARE capture batches by :func:`is_capture_batch`. A non-batch sibling is not blocking
    here, unlike under ``--oracle-root``: naming a batch does not declare its parent to be a tree of
    captures, so this cannot object to whatever else happens to live in ``_runs/<run>/``.

    The remedy is in the message and all three options are legitimate: pass it, switch to
    ``--oracle-root``, or ``--exclude`` it -- the last being recorded in the merged manifest, so an
    excluded batch is an auditable decision rather than an omission nothing can see.
    """
    listed_resolved = {path.resolve() for path in listed}
    unlisted: list[Path] = []
    for parent in sorted({path.resolve().parent for path in listed}):
        if not parent.is_dir():
            continue
        for child in sorted(p for p in parent.iterdir() if p.is_dir()):
            resolved = child.resolve()
            if resolved in listed_resolved or resolved in excluded or resolved in unlisted:
                continue
            if is_capture_batch(child):
                unlisted.append(resolved)
    if unlisted:
        raise UnlistedBatchOnDisk(
            f"{len(unlisted)} capture batch(es) sit beside the ones given and were not passed: "
            f"{', '.join(str(p) for p in unlisted)}. Promotion must consider every batch on disk -- a "
            f"retry that finally succeeded is exactly the batch an operator forgets, and merging "
            f"without it reports the earlier failure as the current state. Pass each one with "
            f"--oracle, use --oracle-root to discover them, or --exclude the ones that are "
            f"deliberately not part of this merge."
        )


def resolve_batch_dirs(
    oracle: Path | list[Path] | None, oracle_root: Path | None, exclude: list[Path] | tuple[Path, ...]
) -> tuple[list[Path], frozenset[Path]]:
    """Turn the CLI's three path arguments into the batch list this run will merge.

    ``--oracle-root`` discovers; ``--oracle`` lists and is then checked against its own siblings. Both
    honour ``--exclude``, which is the single auditable escape from either refusal.
    """
    excluded = frozenset(path.resolve() for path in exclude)
    if oracle_root is not None:
        return discover_batches(oracle_root, excluded), excluded
    listed = [oracle] if isinstance(oracle, Path) else list(oracle or [])
    if not listed:
        raise FileNotFoundError("no capture directory given: pass --oracle DIR or --oracle-root DIR")
    canonical = [path.resolve() for path in listed]
    if all(path.name == "oracle" for path in canonical):
        scopes = {path.parent.parent for path in canonical}
        if len(scopes) == 1 and next(iter(scopes)).name == "_runs":
            discovered = discover_batches(next(iter(scopes)), excluded)
            discovered_set = set(discovered)
            listed_set = set(canonical)
            omitted = sorted(discovered_set - listed_set - excluded)
            if omitted:
                raise UnlistedBatchOnDisk(
                    f"{len(omitted)} canonical capture batch(es) sit beside the ones given and were not "
                    f"passed: {', '.join(str(path) for path in omitted)}. Promotion must consider every "
                    "batch on disk -- pass each one with --oracle, use --oracle-root to discover them, "
                    "or --exclude the ones deliberately not part of this merge."
                )
    _refuse_unlisted_siblings(listed, excluded)
    return listed, excluded


def _batch_labels(oracle_dirs: list[Path]) -> list[str]:
    """A label per directory: its NAME where that is unique, else enough of its path to separate it.

    ⚠️ The label is the identity every promoted leg records as ``source_batch``, and it keys ``roots``,
    the map a leg's artifact is resolved against. Measured before this existed: ``run1\\oracle`` and
    ``run2\\oracle`` collapsed into ONE ``roots["oracle"]`` pointing at the second, ``batches`` read
    ``["oracle", "oracle"]``, and two legs claimed indistinguishable provenance -- so an older
    candidate could resolve against the wrong directory, and one batch's render intent could be
    erased. Same failure class as review round 1's finding 2, arriving through provenance rather than
    through merge order.

    Disambiguation is by ADDING parent components, never by appending an index: an index says only
    "these two differ", while ``run1/oracle`` says WHICH capture a reader is looking at, which is the
    whole point of recording provenance. Two directories that resolve to the same absolute path are a
    genuine duplicate and are REFUSED rather than silently deduplicated -- passing the same capture
    twice is a mistake worth telling the operator about, not a no-op.
    """
    resolved = [directory.resolve() for directory in oracle_dirs]
    repeated = sorted({str(path) for path in resolved if resolved.count(path) > 1})
    if repeated:
        raise DuplicateBatchLabel(
            f"the same capture directory was given more than once: {', '.join(repeated)}. Each "
            f"--oracle must name a distinct capture; merging a batch with itself cannot add evidence."
        )
    for depth in range(1, max((len(path.parts) for path in resolved), default=1) + 1):
        labels = ["/".join(path.parts[-depth:]) for path in resolved]
        if len(set(labels)) == len(labels):
            return labels
    raise DuplicateBatchLabel(f"cannot build distinct labels for {[str(p) for p in resolved]}")


def normalize(name: str) -> str:
    """Match key: lowercased with every non-alphanumeric removed.

    Lossy on purpose so `DS Tail Level`, `ds-tail-level` and `DS_Tail_Level` agree. It does not
    remove words, so a caption suffix survives normalization and simply fails to match.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def index_destinations(migrations_root: Path) -> tuple[dict[str, list[Path]], int]:
    """Map normalized folder name -> the existing folders that produce it.

    A list, not a single path, so an ambiguous key stays visible instead of being silently resolved.
    """
    index: dict[str, list[Path]] = {}
    if not migrations_root.is_dir():
        return index, 0
    folders = sorted(p for p in migrations_root.iterdir() if p.is_dir())
    for folder in folders:
        index.setdefault(normalize(folder.name), []).append(folder)
    return index, len(folders)


@dataclass(frozen=True)
class _Typed:
    """One field this script READS out of a capture manifest, and the JSON type it must then carry.

    ``items`` types the elements of a list field; ``None`` means the elements are not read.
    """

    name: str
    kind: type
    items: type | None = None


# What a JSON value's Python type is CALLED in a refusal message. An operator reading it is looking at
# a `.json` file, so `dict`/`str` name the wrong vocabulary for the thing they will go and edit.
_JSON_TYPE_NAMES: dict[type, str] = {
    bool: "boolean",
    dict: "object",
    float: "number",
    int: "number",
    list: "array",
    str: "string",
    type(None): "null",
}

# ------------------------------------------------------------------- the CONSUMED surface, declared
# ⚠️ ONE table, applied ONCE, instead of one predicate per newly-discovered field. Three review rounds
# added a guard for exactly the field that had just been reported -- `views`, then `view_luid`, then
# `image.path` -- and each round left the NEXT unvalidated field crashing at exit 1 (the "grouped what
# it could" code) or, worse, passing: a `view_luid` of `123` is truthy, so it bucketed, merged and
# reported a CLEAN merge on a manifest whose identities are not identities.
#
# These entries are not a sample. They are every field this module reads out of manifest-sourced data,
# and `tests/test_group_oracle_multi_batch.py::test_the_type_table_covers_EVERY_manifest_field_this_module_reads`
# fails if a new consumer is added without either typing it here or declaring why its type cannot
# matter -- which is what makes "no fourth round" checkable rather than merely intended.
#
# ABSENT and JSON `null` are NOT type errors. The real writer emits `"workbook_luid": null` and
# `"view_name": null` whenever the REST response omitted them (`capture_tableau_oracle.py`), and every
# consumer below already handles the absence. This table types what is PRESENT; it makes nothing
# required. The one required field is `view_luid`, whose absence destroys a record rather than
# degrading it -- see :class:`UnidentifiedCaptureView`.
_MANIFEST_TYPES: tuple[_Typed, ...] = (
    # Sorted against other batches' stamps to order the merge; a non-string breaks the comparison.
    _Typed("captured_at", str),
    # Normalized (`.strip().casefold()`) for the cross-tenant identity check.
    _Typed("server", str),
    _Typed("site", str),
    # `sorted()` over mixed element types raises, and the union feeds the render-intent report.
    _Typed("requested_renders", list, items=str),
)
_VIEW_TYPES: tuple[_Typed, ...] = (
    # A BUCKET KEY. `group_views` does `buckets.setdefault(view.get("workbook_luid") or "", [])`, so an
    # unhashable value crashed at exit 1 -- the same shape as the `view_luid` finding one field over.
    _Typed("workbook_luid", str),
    # A bucket key too (`workbook_names`), and `normalize()` does string work on it.
    _Typed("workbook_name", str),
    _Typed("captured_at", str),
    _Typed("updated_at", str),
)
_LEG_TYPES: tuple[_Typed, ...] = (
    _Typed("status", str),
    # Joined onto a Path (`root / relative`) and read for its `.name`. Measured: an object-valued
    # `path` raised `TypeError: unsupported operand type(s) for /` at exit 1.
    _Typed("path", str),
)


def _json_type(value: Any) -> str:
    """What ``value`` is called in JSON, for a message an operator can act on."""
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


def _refuse_untyped(path: Path, where: str, mapping: dict[str, Any], specs: tuple[_Typed, ...]) -> None:
    """Refuse ``mapping`` if any field it CARRIES has a type this script cannot consume.

    Absent and ``null`` are skipped: see :data:`_MANIFEST_TYPES` for why that is the writer's own
    shape rather than leniency. Raising here rather than at the consumer is the whole point -- a
    ``try``/``except`` around the consumer would convert a crash into a refusal without ever
    establishing the shape, so the next unvalidated field would simply crash somewhere new.
    """
    for spec in specs:
        value = mapping.get(spec.name)
        if value is None:
            continue
        if not isinstance(value, spec.kind):
            raise MalformedCaptureManifest(
                f"{path}: {where} carries {spec.name!r} as a JSON {_json_type(value)}, not a "
                f"{_JSON_TYPE_NAMES[spec.kind]}. This script reads that field, so a value of the wrong "
                f"type is not a cosmetic difference -- it either crashes the merge or, when it happens "
                f"to be usable, merges on a value that is not what it claims to be. Re-capture with "
                f"capture_tableau_oracle.py."
            )
        if spec.items is None:
            continue
        wrong = [index for index, item in enumerate(value) if not isinstance(item, spec.items)]
        if wrong:
            raise MalformedCaptureManifest(
                f"{path}: {where} carries {spec.name!r} with {len(wrong)} element(s) that are not "
                f"{_JSON_TYPE_NAMES[spec.items]}s (position(s) {', '.join(str(i) for i in wrong[:8])})."
            )


def _refuse_untyped_views(path: Path, views: list[Any]) -> None:
    """Type every view record and every render leg it carries, by POSITION only.

    Legs are swept from :data:`RENDER_LEGS` rather than named here, so a render tier added to the
    capture is validated the day it is added instead of the round after it crashes.
    """
    for index, view in enumerate(views):
        _refuse_untyped(path, f"view {index}", view, _VIEW_TYPES)
        for kind, _sub in RENDER_LEGS:
            leg = view.get(kind)
            if leg is None:
                continue
            if not isinstance(leg, dict):
                raise MalformedCaptureManifest(
                    f"{path}: view {index}'s {kind!r} leg is a JSON {_json_type(leg)}, not an object. "
                    f"A leg is read for its status and its path, so a scalar there is an unassessable "
                    f"record, not an empty one -- measured, it raised AttributeError at exit 1."
                )
            _refuse_untyped(path, f"view {index}'s {kind!r} leg", leg, _LEG_TYPES)


def _normalize_timestamp(path: Path, where: str, value: Any) -> str | None:
    """Return one canonical UTC representation, or preserve an absent value."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MalformedCaptureManifest(
            f"{path}: {where} carries an empty or invalid timestamp. Present timestamps must be "
            "non-blank ISO-8601 values; omit the field when it is genuinely unavailable."
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedCaptureManifest(
            f"{path}: {where} carries invalid timestamp {value!r}; expected ISO-8601."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if "T" not in value and " " not in value:
        return parsed.date().isoformat() + "Z"
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_view_identity(value: Any) -> bool:
    """Is ``value`` something the merge can key a view on? A NON-EMPTY STRING, and nothing else.

    ⚠️ Truthiness is not the question, and answering it was the third round of this same finding.
    ``view_luid = 123`` is truthy, hashable and therefore merged, grouped and reported at exit 0 --
    a clean verdict on a manifest whose identities are not identities. ``{...}`` and ``[...]`` are
    truthy AND unhashable, so they crashed at exit 1 instead. Three inputs, three different wrong
    answers, one missing type check. The writer stamps ``view_luid = view["id"]``, a REST LUID
    string, so a non-string is damaged or hand-edited and never routine.
    """
    return isinstance(value, str) and bool(value.strip())


def _validate_manifest(path: Path, payload: Any) -> dict[str, Any]:
    """Refuse anything that parsed as JSON but is not a capture manifest this run can merge.

    Six checks, in the order a reader would ask them, and each one is a measured fail-open rather
    than defensive decoration -- see :class:`MalformedCaptureManifest` and
    :class:`UnidentifiedCaptureView` for the reproductions.

    1. **A JSON object.** ``[]`` reached :func:`merge_batches` and died on ``.get``, exit 1.
    2. **The capture schema.** The same definition :func:`is_capture_batch` already applies under
       ``--oracle-root``. Strict equality, deliberately: a future ``tableau-oracle/2`` is a manifest
       this code has not been shown to understand, and discovery already refuses it. It also keeps
       this script's own OUTPUT (``tableau-oracle-workbook/1``) out of its input.
    3. **A ``views`` list.** ``{"schema": ..., "server": ..., "site": ...}`` with the key absent was
       read as "a capture of nothing" and reported as a clean merge. An EMPTY list is left legitimate:
       a capture that selected no view really does produce one, and refusing it would refuse a true
       statement about the data. Absent is not empty -- one is a damaged file, the other is evidence.
    4. **Every view identified.** Positions only in the message: a view NAME is server-supplied
       response data, and this repository does not put that in a filename or, therefore, in a log.
    5. **Every field this script READS has the type it reads it as** (:data:`_MANIFEST_TYPES`,
       :data:`_VIEW_TYPES`, :data:`_LEG_TYPES`). Declared once and applied once, because the previous
       three rounds each added a predicate for one field and left the next one to be discovered by a
       crash.

    Returns the payload so the caller reads as one expression; raises otherwise.
    """
    if not isinstance(payload, dict):
        raise MalformedCaptureManifest(
            f"{path} is not a capture manifest: its top level is a JSON {type(payload).__name__}, not an "
            f"object. A grouping run cannot establish what it holds, so it is refused rather than merged."
        )
    schema = payload.get("schema")
    if schema != CAPTURE_SCHEMA:
        found = f"schema {str(schema)[:60]!r}" if schema is not None else "no 'schema' key"
        raise MalformedCaptureManifest(
            f"{path} declares {found}, not {CAPTURE_SCHEMA!r}. Only a capture "
            f"manifest can be merged -- this script's own per-workbook output and any unrelated JSON "
            f"file of the same name are refused, because grouping either one would report a clean "
            f"merge of evidence that is not a capture."
        )
    views = payload.get("views")
    if not isinstance(views, list):
        raise MalformedCaptureManifest(
            f"{path} carries no 'views' list (found {type(views).__name__}). A capture manifest with no "
            f"views key is damaged, not empty: without it the run reports '0 workbook(s), 0 grouped' and "
            f"exits 0, which is a clean-merge verdict on a file nothing was read from. Re-run "
            f"capture_tableau_oracle.py."
        )
    unreadable = [index for index, view in enumerate(views) if not isinstance(view, dict)]
    if unreadable:
        raise MalformedCaptureManifest(
            f"{path} holds {len(unreadable)} view record(s) that are not JSON objects "
            f"(position(s) {', '.join(str(i) for i in unreadable[:8])}). Refused rather than skipped."
        )
    anonymous = [index for index, view in enumerate(views) if not _is_view_identity(view.get("view_luid"))]
    if anonymous:
        raise UnidentifiedCaptureView(
            f"{path} holds {len(anonymous)} of {len(views)} view record(s) with no usable view_luid "
            f"-- absent, empty, or not a JSON string -- (position(s) "
            f"{', '.join(str(i) for i in anonymous[:8])}). Merging is keyed on view "
            f"identity, so records without one collapse onto a single bucket and all but one are "
            f"discarded silently -- measured: two views in, one view out, exit 0. A view name is not an "
            f"identity, and neither is a number that happens to be truthy. Re-capture with "
            f"capture_tableau_oracle.py, which stamps view_luid from the server's own view id."
        )
    _refuse_untyped(path, "the manifest", payload, _MANIFEST_TYPES)
    _refuse_untyped_views(path, views)
    if "captured_at" in payload:
        payload["captured_at"] = _normalize_timestamp(path, "manifest 'captured_at'", payload["captured_at"])
    for index, view in enumerate(views):
        for field in ("captured_at", "updated_at"):
            if field in view:
                view[field] = _normalize_timestamp(path, f"view {index}'s {field!r}", view[field])
    return payload


def load_manifest(oracle_dir: Path) -> dict[str, Any]:
    """Read the capture manifest, or raise a message that names the file we wanted.

    Read through :func:`tableau_oracle_manifest.read_manifest`, not ``json.loads``: an OLDER capture
    names uncertified bytes under the data leg's ``path``, and ``copy_view_files`` keys on exactly
    that -- so reading raw would place a body nothing established as CSV at ``<workbook>/data/*.csv``
    in the grouped folder, which is the shape #480 exists to remove.

    ⚠️ Reading is not accepting. Restoring the evidence-path rule answers "which of these bytes may
    be read as evidence", which is a strictly weaker question than "is this a capture manifest", and
    the gap between them was reported as a clean merge -- :func:`_validate_manifest` closes it before
    any ``_Batch`` is constructed. Both run, in that order: withhold first, then refuse.
    """
    path = oracle_dir / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} in {oracle_dir} - run capture_tableau_oracle.py first")
    return _validate_manifest(path, tableau_oracle_manifest.read_manifest(path))


def load_batches(oracle_dirs: list[Path]) -> list[_Batch]:
    """Read every named capture directory, preserving the order they were given in.

    A missing manifest is fatal for the WHOLE run rather than skipped: silently grouping two of three
    batches would produce a merged folder that looks complete and is not, which is the exact failure
    class this script exists to make visible.

    Labels come from :func:`_batch_labels`, which guarantees they are DISTINCT -- ``run1/oracle`` and
    ``run2/oracle`` are two captures, not one, and collapsing them silently pointed every leg of both
    at whichever directory was read last (review round 2, finding 2).
    """
    labels = _batch_labels(oracle_dirs)
    return [
        _Batch(directory, load_manifest(directory), label, index)
        for index, (directory, label) in enumerate(zip(oracle_dirs, labels, strict=True))
    ]


def _contained_artifact(root: Path, relative: Any) -> Path | None:
    """Resolve an artifact only when it remains beneath its capture batch."""
    if not isinstance(relative, str) or not relative.strip():
        return None
    windows = PureWindowsPath(relative)
    candidate = Path(relative)
    if candidate.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        return None
    if ".." in candidate.parts:
        return None
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


def _leg_is_promotable(entry: dict[str, Any], root: Path) -> bool:
    """A leg may only win the merge if it is ``ok`` AND the artifact it names is on disk.

    ⚠️ Both halves. A manifest entry is a CLAIM; a later batch whose manifest says ``ok`` for a file
    somebody has since deleted must not displace an earlier batch that still has the bytes. Without
    the on-disk half, merging could make a reference set worse than either input.
    """
    source = _contained_artifact(root, entry.get("path"))
    return bool(entry.get("status") == "ok" and source is not None and source.is_file())


def _stamp(batch: _Batch, view: dict[str, Any]) -> str:
    """When this VIEW was captured: its own ``captured_at``, else its batch manifest's, else ``""``.

    Per-view first because a batch can span time -- a long capture's views are not simultaneous.
    """
    return view.get("captured_at") or batch.captured_at or ""


def _revision(view: dict[str, Any]) -> str:
    """WHICH revision of the view this record describes -- Tableau's own ``updated_at``, or ``""``.

    ⚠️ Not the same question as :func:`_stamp`, and conflating them is the whole of the second
    blocker. ``captured_at`` says when WE looked; ``updated_at`` says when the workbook last changed.
    Two records can be minutes apart in capture time and describe different content entirely.
    """
    return view.get("updated_at") or ""


def _source_identity(manifest: dict[str, Any]) -> tuple[str, str] | None:
    """``(server, site)`` normalized for comparison, or ``None`` when it CANNOT BE ESTABLISHED.

    Case and a trailing slash are cosmetic -- ``https://Example.online.tableau.com/`` and
    ``https://example.online.tableau.com`` are one server. Nothing else is normalized away: a
    different host or a different site content-URL IS a different source, and a merge across one is
    the cross-tenant defect.

    ⚠️ ``None`` is NOT a value that compares equal to itself, and that distinction is review round 3's
    first blocker. This used to map an unrecorded source onto ``("", "")``, so two anonymous manifests
    produced ONE identity, the cardinality check saw no disagreement, and they merged -- measured,
    exit 0 with tenant B's image promoted beside tenant A's data and ``server``/``site`` both ``null``.
    Sameness was never established; it was assumed from a shared absence.

    ⚠️ An EMPTY ``site`` is a recorded value, not an absence: ``tableau_env`` canonicalises
    ``TABLEAU_SITE`` to ``""`` because *"an empty site IS the documented Default site"* on Tableau
    Server. So the test is whether the manifest CARRIES the field, never whether it is truthy --
    treating ``""`` as unrecorded would refuse every legitimate Default-site merge.
    """
    server_raw = manifest.get("server")
    site_raw = manifest.get("site")
    if not isinstance(server_raw, str) or not server_raw.strip():
        return None
    if not isinstance(site_raw, str):
        return None
    return server_raw.strip().rstrip("/").casefold(), site_raw.strip().casefold()


def _describe_identity(batch: _Batch) -> str:
    """One batch's source, for a refusal message -- naming WHICH half is missing when it is."""
    identity = _source_identity(batch.manifest)
    if identity is not None:
        server, site = identity
        return f"{batch.label}: server={server} site={site or '<default site>'}"
    server_raw, site_raw = batch.manifest.get("server"), batch.manifest.get("site")
    server = server_raw if isinstance(server_raw, str) and server_raw.strip() else "<not recorded>"
    site = site_raw if isinstance(site_raw, str) else "<not recorded>"
    return f"{batch.label}: server={server} site={site}"


def _refuse_incompatible_sources(batches: list[_Batch]) -> None:
    """Refuse a merge whose batches do not PROVABLY describe the SAME Tableau server and site.

    Two refusals, and they are deliberately separate exception types so a test can assert WHICH one
    fired rather than merely that something did:

    * :class:`UnestablishedBatchSource` -- at least one batch does not record its source at all. "We
      cannot tell which tenant this came from" is its own blocking state, never a value that compares
      equal to another unknown. Before this existed, two anonymous manifests merged silently.
    * :class:`IncompatibleBatchSources` -- every batch records a source and they disagree.

    A SINGLE batch that records nothing still merges fine: there is nothing to establish sameness
    against, no artifact crosses a boundary, and refusing it would break every anonymous capture for
    no gain. The refusal exists precisely where a claim of sameness is being made.
    """
    if len(batches) < 2:
        return
    unestablished = [batch for batch in batches if _source_identity(batch.manifest) is None]
    if unestablished:
        raise UnestablishedBatchSource(
            f"{len(unestablished)} of {len(batches)} captures do not record which Tableau server and "
            f"site they came from ({', '.join(_describe_identity(b) for b in unestablished)}), so "
            f"these batches CANNOT BE SHOWN to describe the same source. A shared absence is not "
            f"evidence of sameness -- merging them could present one tenant's artifacts as another's "
            f"reference evidence. Re-capture with capture_tableau_oracle.py (which records both), or "
            f"group each capture on its own."
        )
    identities = {_source_identity(batch.manifest) for batch in batches}
    if len(identities) < 2:
        return
    described = ", ".join(_describe_identity(batch) for batch in batches)
    raise IncompatibleBatchSources(
        f"these captures describe {len(identities)} different Tableau sources and cannot be merged "
        f"into one manifest ({described}). Merging them would present one tenant's artifacts as "
        f"another's reference evidence, under a single top-level server/site that names only one of "
        f"them. Group each source separately."
    )


def _freshness(pair: tuple[_Batch, dict[str, Any]]) -> tuple[str, int]:
    """Sort key: capture time, then ARGUMENT ORDER as the last-resort tiebreak.

    ⚠️ The second element is not evidence. When two candidates share a timestamp it is the only thing
    separating them, and it is an operator's typing habit -- which is why :func:`_merge_one_view`
    reports every leg a tie actually decided instead of letting the manifest imply otherwise.
    """
    batch, view = pair
    return (_stamp(batch, view), batch.order)


def _resolve_leg(
    candidates: list[tuple[_Batch, dict[str, Any]]], kind: str, roots: dict[str, Path]
) -> tuple[tuple[_Batch, dict[str, Any]] | None, list[str]]:
    """Pick one leg's winner from newest-first ``candidates``, and report a deciding TIE.

    Two passes, and the second is finding #2 from review round 1. The first pass takes the newest
    PROMOTABLE record. The second -- reached only when no batch established the leg -- takes the
    newest batch that has a record for it AT ALL, which is not the same as the newest batch overall:
    a later **data-only** batch has no ``image`` record, and taking the newest view wholesale threw
    an older batch's ``image: transient`` away. Measured before the fix: an older `png` batch with
    `image.status="transient"` followed by a data-only batch produced no merged `image` key and
    ``render_unestablished == 0`` -- a known render gap silently reclassified as "never requested",
    which is precisely the collapse this PR exists to prevent.

    The returned tie list names the batches that could not be separated by time. A tie is reported
    only when it was DECIDING -- another candidate of equal standing shares the winner's timestamp --
    because a tie between a winner and a candidate that was never eligible changes nothing.
    """
    promotable = [pair for pair in candidates if _leg_is_promotable(pair[1].get(kind) or {}, roots[pair[0].label])]
    present = [pair for pair in candidates if isinstance(pair[1].get(kind), dict)]
    pool = promotable or present
    if not pool:
        return None, []
    winner = pool[0]
    tied = [batch.label for batch, view in pool[1:] if _stamp(batch, view) == _stamp(*winner)]
    return winner, ([winner[0].label, *tied] if tied else [])


STALE_REVISION_STATUS = "stale_revision"


def _merge_one_view(  # pylint: disable=too-many-locals
    candidates: list[tuple[_Batch, dict[str, Any]]], roots: dict[str, Path]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge one view across batches, newest-successful-wins PER LEG -- WITHIN ONE SOURCE REVISION.

    ``candidates`` is already ordered newest-first. The view's identity fields come from the newest
    batch that saw it at all; each leg is then resolved independently, because the field case that
    started this is precisely a view whose data and image succeeded in DIFFERENT batches.

    ⚠️ **A leg may only be taken from another batch when both records provably describe the SAME
    revision of the view**, and that constraint is the second blocker. Measured before it existed: an
    old batch with ``image: ok`` and a newer batch with ``data: ok, image: transient`` merged into one
    record carrying the NEW revision's ``updated_at``, the new data, and the **old** render -- reported
    as ``data_ok=1, image_ok=1, failed=0, render_unestablished=0``, i.e. entirely healthy. A stale
    picture of a workbook that has since changed is not weaker evidence than no picture; it is
    evidence pointing the wrong way, and it arrives with a digest and a timestamp that say otherwise.

    Sameness must be PROVED, so an absent or empty ``updated_at`` disqualifies a cross-batch
    promotion rather than permitting one -- "we cannot tell" and "they match" are different answers.
    The newest candidate is always eligible for its own legs; only importing from an older batch is
    gated.

    A leg the revision gate refused is recorded rather than dropped: the merged record carries an
    explicit :data:`STALE_REVISION_STATUS` entry naming both revisions and the status the older batch
    had, so a reader sees "there is an image, for a version of this view that no longer exists"
    instead of a silent absence. It is not ``ok``, so no counter credits it. A refusal is reported only
    when the current revision did NOT establish the leg itself -- otherwise every leg of every view
    captured twice would be listed, and the entries that matter would be buried.

    Returns ``(merged view, ties, stale)``.
    """
    newest_batch, newest_view = candidates[0]
    revision = _revision(newest_view)
    eligible = [candidates[0]] + [pair for pair in candidates[1:] if revision and _revision(pair[1]) == revision]
    refused = [pair for pair in candidates[1:] if not (revision and _revision(pair[1]) == revision)]
    merged = dict(newest_view)
    merged["source_batch"] = newest_batch.label
    ties: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for kind, _sub in RENDER_LEGS:
        winner, tied = _resolve_leg(eligible, kind, roots)
        if tied:
            ties.append({"view_luid": merged.get("view_luid"), "leg": kind, "batches": tied})
        # Only look for a refused older candidate when the CURRENT revision failed to establish this
        # leg. If the newest revision has its own good render, an older revision's copy of it is not a
        # gap and reporting it would bury the entries that are -- measured: every leg of every view
        # captured twice would appear here, which is a report nobody reads.
        established = winner is not None and _leg_is_promotable(winner[1].get(kind) or {}, roots[winner[0].label])
        blocked = None if established else _first_blocked_by_revision(refused, kind, roots)
        if blocked is not None:
            batch, view = blocked
            stale.append(
                {
                    "view_luid": merged.get("view_luid"),
                    "leg": kind,
                    "batch": batch.label,
                    "captured_revision": _revision(view) or None,
                    "current_revision": revision or None,
                    "promoted": False,
                }
            )
        if winner is None:
            if blocked is None:
                # No batch anywhere has a record for this leg. Leave it absent rather than inventing one.
                merged.pop(kind, None)
                continue
            batch, view = blocked
            merged[kind] = {
                "status": STALE_REVISION_STATUS,
                "source_batch": batch.label,
                "recorded_status": (view[kind] or {}).get("status"),
                "captured_revision": _revision(view) or None,
                "current_revision": revision or None,
            }
            continue
        batch, view = winner
        merged[kind] = {**view[kind], "source_batch": batch.label}
    return merged, ties, stale


def _first_blocked_by_revision(
    refused: list[tuple[_Batch, dict[str, Any]]], kind: str, roots: dict[str, Path]
) -> tuple[_Batch, dict[str, Any]] | None:
    """The newest refused candidate that WOULD have won this leg, had its revision matched.

    Reported rather than discarded: "an older revision has this render" is the fact an operator needs
    to decide whether to re-capture, and silently dropping it is how a known gap becomes an unknown
    one. Only a candidate that is actually promotable counts -- a stale FAILURE is not evidence that
    anything was lost.
    """
    for pair in refused:
        if _leg_is_promotable(pair[1].get(kind) or {}, roots[pair[0].label]):
            return pair
    return None


def _merge_render_intent(batches: list[_Batch], views: list[dict[str, Any]]) -> dict[str, Any]:
    """What the batches TOGETHER asked for -- unioned, never taken from the newest alone (#2).

    ⚠️ Render intent is the thing that makes an absent leg readable. Copying ``requested_renders``
    from the newest batch let a later **data-only** run rewrite it to ``[]``, after which a view with
    a failed image reads as one for which no image was ever wanted -- and both the capture-wide and
    per-workbook UNESTABLIHED counts drop to zero. Intent is therefore a UNION across batches, and
    ``reference_required`` an ``any``: a batch that did not ask for a render cannot retract another
    batch's request.

    ``reference_missing`` is RECOMPUTED rather than carried, because it is a verdict about the merged
    evidence, and the newest batch's own verdict is about a different (possibly smaller) set of views.

    ``requested_renders_by_batch`` is kept so a reader can see the disagreement instead of only its
    resolution -- a data-only batch mixed into a render capture is worth noticing.
    """
    per_batch = {batch.label: sorted(batch.manifest.get("requested_renders") or []) for batch in batches}
    requested = sorted({kind for kinds in per_batch.values() for kind in kinds})
    required = any(batch.manifest.get("reference_required") for batch in batches)
    rendered = any((view.get(leg) or {}).get("status") == "ok" for view in views for leg in ("image", "svg", "pdf"))
    return {
        "requested_renders": requested,
        "requested_renders_by_batch": per_batch,
        "reference_required": required,
        "reference_missing": bool(required and not rendered),
    }


def merge_batches(batches: list[_Batch]) -> tuple[dict[str, Any], dict[str, Path], str]:  # pylint: disable=R0914
    """Fold every batch into ONE manifest, newest-successful-wins per view and per leg.

    Returns ``(merged manifest, label -> directory, the basis the ordering used)``.

    The newest batch supplies the provenance fields (`server`, `site`, `rest_api_version`, the
    `#403` capability block), because those describe the run that produced the winning artifacts more
    often than any older one does. `batches` records every input in newest-first order, so a reader
    can see what was merged rather than infer it from one `source_batch` at a time.

    ⚠️ Render INTENT is the exception and is unioned instead -- see :func:`_merge_render_intent`.

    ⚠️ ``merge_order_basis`` has THREE values, not two (finding #5 from review round 1). Reporting
    ``captured_at`` whenever timestamps merely EXIST was false: two batches with identical stamps
    produced different winners when the arguments were reversed -- one keeping ``image: failed``, the
    other ``image: transient`` -- while both claimed the timestamps had decided it. A tie is now
    detected, named in ``merge_order_ties``, and said out loud in the basis.

    ⚠️ **Two things are refused rather than merged, and both were fail-open before.**

    * Batches describing **different servers or sites** (:func:`_refuse_incompatible_sources`). Two
      tenants sharing a workbook caption were folded into one manifest that declared only one of their
      server/site pairs.
    * A leg from an older batch describing a **different revision** of the same view
      (:func:`_merge_one_view`). Identity came from the newest record and each leg was then taken from
      anywhere, so a merged view carried the new revision's ``updated_at`` beside the OLD revision's
      render -- and reported ``failed=0, render_unestablished=0``.

    ``merge_stale_candidates`` records every leg the revision gate refused, so the evidence is visible
    rather than merely absent.
    """
    roots = {batch.label: batch.directory for batch in batches}
    _refuse_incompatible_sources(batches)
    dated = [batch for batch in batches if all(_stamp(batch, view) for view in batch.manifest.get("views", []) or [{}])]

    by_view: dict[str, list[tuple[_Batch, dict[str, Any]]]] = {}
    for batch in batches:
        for view in batch.manifest.get("views", []):
            by_view.setdefault(view.get("view_luid") or "", []).append((batch, view))

    views, ties, stale = [], [], []
    for candidates in by_view.values():
        candidates.sort(key=_freshness, reverse=True)
        merged_view, view_ties, view_stale = _merge_one_view(candidates, roots)
        views.append(merged_view)
        ties.extend(view_ties)
        stale.extend(view_stale)

    if len(dated) != len(batches):
        basis = "argument order"
    elif ties:
        basis = "captured_at, ties broken by argument order"
    else:
        basis = "captured_at"

    newest = max(batches, key=lambda b: (b.captured_at, b.order))
    merged = {key: value for key, value in newest.manifest.items() if key != "views"}
    merged["views"] = views
    merged["batches"] = [b.label for b in sorted(batches, key=lambda b: (b.captured_at, b.order), reverse=True)]
    merged["merge_order_basis"] = basis
    merged["merge_order_ties"] = ties
    merged["merge_stale_candidates"] = stale
    merged.update(_merge_render_intent(batches, views))
    return merged, roots, basis


def group_views(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Bucket the manifest's views by workbook **LUID**, preserving capture order within each bucket.

    ⚠️ By IDENTITY, not by display name, and that is review round 3's third blocker. Bucketing by
    ``workbook_name`` merged two genuinely different workbooks whenever their names matched, and the
    normalizer made that far more likely than an exact collision: measured on the real 48-workbook
    reference estate, ``Seed - R&D``, ``Seed - R+D`` and ``Seed - R/D`` are three distinct LUIDs that
    normalize onto ONE key. The name route is the same class ``package_unit.py`` deleted rather than
    guarded (#450 measured it failing open on 360 of 360 real records).

    A view carrying no ``workbook_luid`` is bucketed under ``""`` and refused downstream: "we cannot
    tell which workbook this belongs to" is a blocking state, not a bucket to merge things into.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for view in manifest.get("views", []):
        buckets.setdefault(view.get("workbook_luid") or "", []).append(view)
    return buckets


def workbook_names(views: list[dict[str, Any]]) -> list[str]:
    """The distinct display names one workbook's views carry, in first-seen order."""
    seen: dict[str, None] = {}
    for view in views:
        seen.setdefault(view.get("workbook_name") or "", None)
    return list(seen)


RENDER_LEGS: tuple[tuple[str, str], ...] = (("data", "data"), ("image", "images"), ("svg", "images"), ("pdf", "images"))

# Status stamped on a leg the SOURCE manifest called `ok` but whose artifact could not be copied.
# Deliberately not `ok` and not `failed`: the capture succeeded, the *grouping* did not, and a reader
# has to be able to tell those apart when deciding whether to re-capture (metered) or re-group (free).
NOT_COPIED_STATUS = "not_copied"


def copy_view_files(
    view: dict[str, Any], roots: dict[str, Path], destination: Path, *, dry_run: bool
) -> tuple[list[str], dict[str, Any]]:
    """Copy one view's captured artifacts. Returns ``(relative paths written, the view AS GROUPED)``.

    A view whose capture failed has no `path` key, so nothing is copied and nothing is invented --
    the per-workbook manifest still records its failure status, which is the honest evidence grade.

    ⚠️ Every render leg the oracle can write MUST appear in ``RENDER_LEGS``. `--reference-best` now
    normally yields **SVG** on Cloud, and while this handled only `data` and `image` the grouped
    manifest asserted `svg.path`/`pdf.path` for files that were never copied -- a manifest pointing at
    absent evidence, which is worse than omitting it.

    ⚠️ **A copy that could not happen is returned as a DOWNGRADED leg, not merely warned about.**
    Skipping a missing artifact while handing the caller the source manifest's own `status: ok` and
    `path` re-creates that same shape one level up: the grouped folder asserts evidence nothing ever
    put there. The returned view is a copy -- the capture manifest is never mutated -- whose affected
    legs carry ``NOT_COPIED_STATUS``, no ``path``, and the reason.

    ⚠️ ``roots`` is a MAP, not one directory, because after #423 two legs of the same view can come
    from two different batches. Each leg is resolved against the batch that actually produced it
    (``source_batch``); resolving everything against a single root is what stranded a good image.
    """
    written: list[str] = []
    grouped = dict(view)
    for kind, sub in RENDER_LEGS:
        entry = view.get(kind) or {}
        relative = entry.get("path")
        if entry.get("status") != "ok" or not relative:
            continue
        oracle_dir = roots[entry.get("source_batch", next(iter(roots)))]
        source = _contained_artifact(oracle_dir, relative)
        if source is None or not source.is_file():
            LOG.warning("  MISSING on disk, not copied: %s (%s)", relative, oracle_dir.name)
            downgraded = {k: v for k, v in entry.items() if k != "path"}
            downgraded["status"] = NOT_COPIED_STATUS
            downgraded["not_copied_reason"] = (
                f"the capture manifest names {relative}, which is absent from {oracle_dir}"
            )
            grouped[kind] = downgraded
            continue
        target = destination / sub / Path(relative).name
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        written.append(f"{sub}/{Path(relative).name}")
    return written, grouped


def subset_manifest(manifest: dict[str, Any], workbook: str, views: list[dict[str, Any]]) -> dict[str, Any]:
    """A per-workbook manifest carrying the SAME evidence grade as the capture-wide one.

    ⚠️ ``views`` must be the views **as grouped** -- ``copy_view_files``' second return value, not the
    capture manifest's own list. The counts below are what a consumer reads instead of listing the
    folder, so computing them from the capture's statuses claims evidence for artifacts this run may
    have failed to copy. Passing the raw views is the defect, not a shortcut.

    Counts are recomputed over this workbook's views rather than copied, so a folder that holds
    three good captures and one credential-blocked view says exactly that. The capture-wide GRADE
    fields are carried across verbatim: a consumer that reads only this file must still be able to
    see which render tier was obtained and whether a required reference went missing (#403), rather
    than inferring it from which files happen to exist.
    """

    def status_of(view: dict[str, Any], kind: str, default: str | None = None) -> str | None:
        return (view.get(kind) or ({"status": default} if default else {})).get("status")

    render_kinds = [kind for kind, _ in RENDER_LEGS if kind != "data"]

    def render_statuses(view: dict[str, Any], default: str | None = None) -> list[str | None]:
        return [status_of(view, kind, default) for kind in render_kinds]

    ok = [v for v in views if status_of(v, "data") == "ok"]
    blocked = [v for v in views if "source_credential" in {status_of(v, "data"), *render_statuses(v)}]
    failed = [
        v
        for v in views
        if any(
            status not in {"ok", "source_credential"} for status in (status_of(v, "data"), *render_statuses(v, "ok"))
        )
    ]
    not_copied = sum(1 for v in views for kind, _ in RENDER_LEGS if status_of(v, kind) == NOT_COPIED_STATUS)
    # ⚠️ Recomputed over the GROUPED views, not carried from the capture (#423). This is the manifest
    # a fidelity review reads, and it must answer "for which pages of THIS workbook can no visual
    # finding be made" -- which is not the capture-wide answer, and is not the capture's answer
    # either: a leg the capture obtained but this grouping could not place (`not_copied`) means the
    # reference folder does not hold that image, so the view IS unestablished here.
    unestablished = render_unestablished(views, frozenset(manifest.get("requested_renders") or []))
    subset = {
        "schema": "tableau-oracle-workbook/1",
        "grouped_from": manifest.get("schema"),
        "captured_at": manifest.get("captured_at"),
        "server": manifest.get("server"),
        "site": manifest.get("site"),
        "rest_api_version": manifest.get("rest_api_version"),
        "workbook_name": workbook,
        "workbook_luid": next((v.get("workbook_luid") for v in views if v.get("workbook_luid")), None),
        "view_count": len(views),
        "data_ok": len(ok),
        # ⚠️ IMPORTED, not re-implemented (#471). This was a second copy of "row_count == 0", and a
        # second copy of a rule is how a per-workbook subset comes to disagree with the capture it
        # was sliced from. The count is unchanged; what is new is that it is now the SAME predicate
        # the capture-wide manifest counts with -- and the views are NAMED here too, for the reason
        # `data_empty_views` exists at all: a count cannot tell a reviewer which page to open.
        "data_empty": len([v for v in ok if tableau_oracle_manifest.empty_classification(v)]),
        "data_empty_views": tableau_oracle_manifest.data_empty_views(ok),
        # The third state, carried at the same grade as the other two. A per-workbook reader is the
        # one who acts on this, and a subset that reported `data_ok: 4` with nothing beside it said
        # "four good captures" about views whose rows were never measured.
        "data_unassessable": len([v for v in ok if tableau_oracle_manifest.unassessable_reason(v)]),
        "data_unassessable_views": tableau_oracle_manifest.data_unassessable_views(ok),
        "credential_blocked": len(blocked),
        "failed": len(failed),
        "render_unestablished": len(unestablished),
        "render_unestablished_views": unestablished,
        # Legs the CAPTURE obtained but this grouping could not place. Separate from `failed` so a
        # reader knows to re-run the (free) grouping rather than the (metered) capture.
        "not_copied": not_copied,
        "views": views,
    }
    for kind in render_kinds:
        subset[f"{'image' if kind == 'image' else kind}_ok"] = sum(1 for v in views if status_of(v, kind) == "ok")
    # Carried, not recomputed: these describe the CAPTURE RUN, not this workbook's slice of it.
    # `batches` / `merge_order_basis` travel with them (#423) so a consumer reading ONLY this file can
    # see which captures were folded together and on what evidence "newest" was decided -- otherwise
    # the per-leg `source_batch` names a directory the reader has no list of.
    for field in (
        "render_capability",
        "requested_renders",
        "requested_renders_by_batch",
        "reference_required",
        "reference_missing",
        "batches",
        "excluded_paths",
        "merge_order_basis",
        "merge_order_ties",
        "merge_stale_candidates",
    ):
        if field in manifest:
            subset[field] = manifest[field]
    return subset


def build_parser() -> argparse.ArgumentParser:
    """CLI surface: which captures to read, which folder tree to group them into."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--oracle",
        action="append",
        type=Path,
        metavar="DIR",
        help=(
            "a capture directory holding oracle-manifest.json. WARNING: REPEATABLE, and normally should be: "
            "a metered capture is re-run in batches, and the same view can succeed in a later one "
            "having failed earlier. Every batch given is merged newest-successful-wins per view and "
            "per LEG, and each promoted artifact records the batch it came from. A capture batch "
            "sitting BESIDE the ones given and not passed is REFUSED, not skipped -- 'every batch you "
            "remembered' is not the promise. Prefer --oracle-root"
        ),
    )
    source.add_argument(
        "--oracle-root",
        type=Path,
        metavar="DIR",
        help=(
            "discover and merge EVERY capture batch under DIR, including ones nobody listed (#423). "
            "A batch is a directory holding an oracle-manifest.json with schema "
            f"{CAPTURE_SCHEMA!r}; DIR itself may be one. Any other directory under DIR is a blocking "
            "error rather than a skip -- pass --exclude for each one that is not evidence"
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        type=Path,
        default=[],
        metavar="DIR",
        help=(
            "a directory that is deliberately NOT part of this merge. The single auditable escape "
            "from the unlisted-batch and unclassified-directory refusals; recorded in every merged "
            "manifest as excluded_paths, so an omission is a decision a reader can see"
        ),
    )
    parser.add_argument(
        "--migrations",
        type=Path,
        default=Path("migrations/workbooks"),
        help="root holding the per-workbook <slug>/ folders (default: migrations/workbooks)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied, write nothing")
    return parser


# Why a workbook was refused, as recorded in the grouping report and asserted by name in the tests.
# Named constants rather than prose because this script now has several fail-closed guards and a test
# that can only assert "it refused" cannot say WHICH one it exercised.
REFUSAL_NO_LUID = "workbook_luid_missing"
REFUSAL_NAME_AMBIGUOUS = "source_name_ambiguous"
REFUSAL_DESTINATION_AMBIGUOUS = "destination_ambiguous"
REFUSAL_DESTINATION_COLLISION = "destination_collision"
REFUSAL_UNATTRIBUTED = "unattributed_reference_files"

OUTCOME_BUCKETS = ("grouped", "incomplete", "unmatched", "ambiguous", "collision", "unidentified", "unreconciled")


def _previously_grouped_files(destination: Path) -> set[str] | None:
    """The artifact paths the destination's EXISTING grouped manifest names, or ``None`` if there is none.

    This is the attribution half of reconciliation: it says which files under ``reference/{images,data}/``
    THIS script put there on a previous run, so removing them is undoing our own work rather than
    deleting somebody else's evidence. A file under those directories that no previous grouped manifest
    named is not ours to delete, and :func:`_reconcile_destination` reports it instead.
    """
    path = destination / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "tableau-oracle-workbook/1":
        return None
    named: set[str] = set()
    for view in payload.get("views") or []:
        for kind, sub in RENDER_LEGS:
            relative = (view.get(kind) or {}).get("path")
            if relative:
                named.add(f"{sub}/{Path(relative).name}")
    return named


def _reconcile_destination(
    destination: Path, written: set[str], previous: set[str] | None, *, dry_run: bool
) -> tuple[list[str], list[str]]:
    """Make ``reference/{images,data}/`` hold what this run promoted -- and nothing it refused.

    ⚠️ Review round 3's second blocker. Copying the selected artifacts is only half of promotion: an
    artifact an EARLIER run promoted and this one REFUSES stays physically on disk unless something
    removes it. Measured before this existed: a render refused by the cross-revision gate was correctly
    marked ``transient`` with no ``path`` in the manifest, and the old-revision PNG was still sitting
    in ``reference/images/`` -- so any consumer that reads the directory rather than the manifest got
    evidence the merge had explicitly rejected. #451 fixed the sibling shape in ``package_unit.py`` the
    same way: a re-run REPLACES staged content instead of layering over the previous run's.

    Removal is by ATTRIBUTION, never by "everything I did not write". ``previous`` is the set of paths
    the destination's own grouped manifest named, i.e. what this script put there; anything else under
    those two directories was placed by someone else and is REPORTED rather than deleted -- silently
    removing a hand-dropped reference would be a worse failure than the one being fixed.

    Returns ``(removed, unattributed)`` as relative ``<sub>/<name>`` paths.
    """
    on_disk: set[str] = set()
    for _kind, sub in RENDER_LEGS:
        folder = destination / sub
        if folder.is_dir():
            on_disk |= {f"{sub}/{child.name}" for child in folder.iterdir() if child.is_file()}
    extra = on_disk - written
    mine = previous or set()
    removed = sorted(extra & mine)
    unattributed = sorted(extra - mine)
    if not dry_run:
        for relative in removed:
            (destination / relative).unlink(missing_ok=True)
    return removed, unattributed


def _destination_of(views: list[dict[str, Any]], ctx: _Context) -> tuple[Path | None, str, dict[str, Any] | None]:
    """Resolve one workbook's destination folder, or say why it cannot be resolved.

    Returns ``(folder, display name, refusal record)``; exactly one of folder/refusal is set.
    Split out of :func:`_group_one` because :func:`_group_all` must resolve EVERY workbook's
    destination before it writes ANY of them -- a collision is a property of the set, and detecting
    it while writing is how the first workbook's manifest got overwritten by the second's.
    """
    names = workbook_names(views)
    display = names[0] if names else ""
    keys = {normalize(name) for name in names}
    if len(keys) > 1:
        return (
            None,
            display,
            {"refusal": REFUSAL_NAME_AMBIGUOUS, "workbook": display, "names": names, "views": len(views)},
        )
    matches = ctx.destinations.get(normalize(display), [])
    if len(matches) > 1:
        return (
            None,
            display,
            {
                "refusal": REFUSAL_DESTINATION_AMBIGUOUS,
                "workbook": display,
                "folders": [str(m) for m in matches],
                "views": len(views),
            },
        )
    if not matches:
        return (
            None,
            display,
            {"workbook": display, "normalized": normalize(display), "views": len(views)},
        )
    return matches[0], display, None


def _group_one(
    workbook: str,
    views: list[dict[str, Any]],
    folder: Path,
    ctx: _Context,
) -> tuple[str, dict[str, Any]]:
    """Copy one workbook's views into an already-resolved destination folder.

    Reconciliation runs AFTER the copies and is part of promotion, not cleanup: the folder must end up
    holding what this run promoted, so an artifact the merge refused cannot survive there from an
    earlier run (review round 3, finding 2).
    """
    destination = folder / "reference"
    previous = _previously_grouped_files(destination)
    files: list[str] = []
    grouped_views: list[dict[str, Any]] = []
    for view in views:
        written, grouped = copy_view_files(view, ctx.roots, destination, dry_run=ctx.dry_run)
        files.extend(written)
        grouped_views.append(grouped)
    subset = subset_manifest(ctx.manifest, workbook, grouped_views)
    removed, unattributed = _reconcile_destination(destination, set(files), previous, dry_run=ctx.dry_run)
    if not ctx.dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / MANIFEST_NAME).write_text(json.dumps(subset, indent=2) + "\n", encoding="utf-8")
    record = {
        "workbook": workbook,
        "folder": str(folder),
        "views": len(views),
        "files": len(files),
        "not_copied": subset["not_copied"],
        "removed": removed,
        "unattributed": unattributed,
    }
    if removed:
        LOG.info(
            "  reconciled %s: removed %d artifact(s) this merge does not promote (%s)",
            folder.name,
            len(removed),
            ", ".join(removed[:4]),
        )
    if unattributed:
        # NOT deleted and NOT accepted. These sit under `reference/{images,data}/`, which is this
        # script's tree, but no grouped manifest ever named them -- so we cannot say they are ours to
        # remove, and we cannot say the folder holds only promoted evidence either.
        record["refusal"] = REFUSAL_UNATTRIBUTED
        LOG.warning(
            "UNRECONCILED %-43s -> %s holds %d file(s) no grouped manifest names: %s. They were NOT "
            "removed (they may not be ours) and are NOT promoted evidence -- move or delete them.",
            workbook[:43],
            folder.name,
            len(unattributed),
            ", ".join(unattributed[:4]),
        )
        return "unreconciled", record
    if subset["not_copied"]:
        # NOT "grouped". The folder exists and holds some evidence, but the capture manifest named
        # artifacts that are not on disk, so this workbook's reference set is incomplete and the
        # command must not report success for it.
        LOG.warning(
            "INCOMPLETE %-45s -> %s (%d view(s), %d file(s), %d artifact(s) missing from the capture)",
            workbook[:45],
            folder.name,
            len(views),
            len(files),
            subset["not_copied"],
        )
        return "incomplete", record
    LOG.info("ok         %-45s -> %s (%d view(s), %d file(s))", workbook[:45], folder.name, len(views), len(files))
    return "grouped", record


@dataclass(frozen=True)
class _Resolved:
    """One workbook that HAS a destination -- the input to the collision pass and then to copying."""

    luid: str
    display: str
    views: list[dict[str, Any]]
    folder: Path


def _resolve_destinations(
    buckets: dict[str, list[dict[str, Any]]], ctx: _Context, outcomes: dict[str, list[dict[str, Any]]]
) -> list[_Resolved]:
    """Pass one: turn LUID buckets into destinations, bucketing everything that cannot resolve.

    Nothing is written here. A collision is a property of the SET of workbooks, so every destination
    has to be known before any of them is copied into.
    """
    resolved: list[_Resolved] = []
    for luid, views in sorted(buckets.items(), key=lambda item: (workbook_names(item[1]) or [""])[0]):
        names = workbook_names(views)
        if not luid:
            LOG.warning("NO LUID    %-45s (%d view(s) carry no workbook_luid)", (names[0] or "?")[:45], len(views))
            outcomes["unidentified"].append(
                {"refusal": REFUSAL_NO_LUID, "workbook": names[0] if names else "", "names": names, "views": len(views)}
            )
            continue
        folder, display, refusal = _destination_of(views, ctx)
        if refusal is None:
            resolved.append(_Resolved(luid, display, views, folder))
            continue
        refusal["workbook_luid"] = luid
        if refusal.get("refusal") == REFUSAL_NAME_AMBIGUOUS:
            LOG.warning("RENAMED    %-45s -> %s", display[:45], ", ".join(refusal["names"]))
            outcomes["ambiguous"].append(refusal)
        elif refusal.get("refusal") == REFUSAL_DESTINATION_AMBIGUOUS:
            LOG.warning("AMBIGUOUS  %-45s -> %s", display[:45], ", ".join(Path(f).name for f in refusal["folders"]))
            outcomes["ambiguous"].append(refusal)
        else:
            LOG.warning("NO FOLDER  %-45s (normalized: %s)", display[:45], normalize(display))
            outcomes["unmatched"].append(refusal)
    return resolved


def _contested(resolved: list[_Resolved]) -> dict[Path, list[str]]:
    """Which destination folder is claimed by which workbook LUIDs -- the collision test, by itself.

    A separate function because the answer is about the SET: a folder claimed by two distinct LUIDs
    is a collision no matter what either workbook looks like on its own, and computing it while
    writing is how the first workbook's manifest got overwritten by the second's.
    """
    claimed: dict[Path, list[str]] = {}
    for item in resolved:
        claimed.setdefault(item.folder, []).append(item.luid)
    return claimed


def _group_all(buckets: dict[str, list[dict[str, Any]]], ctx: _Context) -> dict[str, list[dict[str, Any]]]:
    """Place every workbook, bucketed by outcome. Two passes, and the first one is the point.

    ⚠️ Every destination is resolved BEFORE anything is written, because a collision is a property of
    the SET of workbooks, not of any one of them (review round 3, finding 3). Writing as it went, two
    distinct workbook LUIDs whose display names normalize identically both targeted one folder: the
    second manifest overwrote the first, both workbooks' image files stayed on disk, and the surviving
    manifest reported one view while the folder held two -- exit 0, no warning, one workbook's
    evidence silently attributed to another.

    Neither side of a collision is written. Picking a winner would be resolving an ambiguity this
    script exists not to resolve; the folder keeps whatever it held, and both workbooks are reported.
    """
    outcomes: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in OUTCOME_BUCKETS}
    resolved = _resolve_destinations(buckets, ctx, outcomes)
    claimed = _contested(resolved)
    for item in resolved:
        if len(claimed.get(item.folder, [item.luid])) > 1:
            others = [other for other in claimed[item.folder] if other != item.luid]
            LOG.warning(
                "COLLISION  %-45s -> %s is also claimed by %d other workbook(s): %s",
                item.display[:45],
                item.folder.name,
                len(others),
                ", ".join(others),
            )
            outcomes["collision"].append(
                {
                    "refusal": REFUSAL_DESTINATION_COLLISION,
                    "workbook": item.display,
                    "workbook_luid": item.luid,
                    "folder": str(item.folder),
                    "colliding_workbook_luids": sorted(claimed[item.folder]),
                    "views": len(item.views),
                }
            )
            continue
        bucket, record = _group_one(item.display, item.views, item.folder, ctx)
        record["workbook_luid"] = item.luid
        outcomes[bucket].append(record)
    return outcomes


@dataclass(frozen=True)
class _RunInputs:
    """How this run was ASKED for, as opposed to what it found. Written into the grouping report.

    Kept together because the three provenance answers -- discovered under a root or listed, which
    directories were deliberately excluded, and on what evidence "newest" was decided -- are only
    meaningful side by side. An exclusion nothing records is indistinguishable from an omission.
    """

    batches: list[_Batch]
    migrations_root: Path
    basis: str
    dry_run: bool
    excluded: frozenset[Path] = frozenset()
    oracle_root: Path | None = None


def _write_grouping_report(inputs: _RunInputs, outcomes: dict[str, list[dict[str, Any]]]) -> Path:
    """Write the run report beside the LAST capture given, and return that directory.

    ``oracle_dirs`` and ``merge_order_basis`` are new (#423): with several batches folded together,
    "which captures produced this" and "on what evidence was newest decided" are the two questions a
    reader of a merged reference folder actually has. ``oracle_dir`` is kept for callers that read it.
    """
    report_dir = inputs.batches[-1].directory
    report = {
        "schema": "tableau-oracle-grouping/1",
        "oracle_dir": str(report_dir),
        "oracle_dirs": [str(b.directory) for b in inputs.batches],
        "oracle_root": str(inputs.oracle_root) if inputs.oracle_root is not None else None,
        "excluded_paths": sorted(str(path) for path in inputs.excluded),
        "merge_order_basis": inputs.basis,
        "migrations_root": str(inputs.migrations_root),
        "dry_run": inputs.dry_run,
        **{f"workbooks_{bucket}": len(outcomes[bucket]) for bucket in OUTCOME_BUCKETS},
        **outcomes,
    }
    if not inputs.dry_run:
        (report_dir / UNMATCHED_REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report_dir


def _incomplete(outcomes: dict[str, list[dict[str, Any]]], manifest: dict[str, Any]) -> bool:
    """Did this run fail to establish everything it was asked for? -- the whole exit-code rule.

    One function so the rule is stated once and can be argued with. Two terms:

    * any outcome bucket other than ``grouped`` -- a workbook that did not land, for any of the six
      reasons this script now distinguishes;
    * ⚠️ ``merge_stale_candidates`` -- review round 3's finding 4. A refused cross-revision leg used
      to warn and persist a count while returning 0, so a gate reading only the exit code was told
      everything landed. The merge is correct; the reference set the operator asked for is not
      established, and those are different claims.
    """
    return bool(
        any(outcomes[bucket] for bucket in OUTCOME_BUCKETS if bucket != "grouped")
        or manifest.get("merge_stale_candidates")
    )


def run(  # pylint: disable=too-many-locals
    oracle: Path | list[Path] | None,
    migrations_root: Path,
    *,
    dry_run: bool,
    oracle_root: Path | None = None,
    exclude: list[Path] | tuple[Path, ...] = (),
) -> int:
    """Group every capture on disk. Returns 0 when every workbook landed, 1 when some could not.

    "Could not" now covers seven outcomes, each with its own bucket in the report: no destination
    folder; an ambiguous destination; one workbook whose views disagree on their own name; two
    workbooks colliding onto one folder; a view carrying no workbook LUID; a destination holding files
    no grouped manifest accounts for; and a destination that was reached but did **not** receive every
    artifact the capture manifest named. All mean the same to a caller gating on the exit code -- the
    per-workbook copies are partial and the flat capture remains the authoritative one.

    A refused cross-revision leg (``merge_stale_candidates``) is ALSO non-zero, which review round 3's
    finding 4 found missing: the merge is correct, but the reference set the operator asked for was
    not fully established, and a gate reading only the exit code was told everything landed.

    ``oracle`` accepts a single ``Path`` as well as a list, deliberately: the single-capture call is
    what every existing caller writes. ``oracle_root`` DISCOVERS batches instead of listing them, and
    is the reading of #423's criterion 3 that finds a batch nobody typed; with ``oracle`` a capture
    batch sitting unlisted beside a given one is refused rather than silently omitted.
    """
    batch_dirs, excluded = resolve_batch_dirs(oracle, oracle_root, exclude)
    batches = load_batches(batch_dirs)
    manifest, roots, basis = merge_batches(batches)
    manifest["excluded_paths"] = sorted(str(path) for path in excluded)
    destinations, folder_count = index_destinations(migrations_root)
    buckets = group_views(manifest)
    LOG.info(
        "%d workbook(s) across %d capture(s)%s, %d candidate folder(s) under %s%s",
        len(buckets),
        len(batches),
        f" discovered under {oracle_root}" if oracle_root is not None else "",
        folder_count,
        migrations_root,
        " [DRY RUN]" if dry_run else "",
    )
    if len(batches) > 1 and basis == "argument order":
        # Not a detail. Merging is "newest wins", so an undated batch means the WINNER is decided by
        # the order somebody happened to type -- which is a habit, not evidence. Say so rather than
        # letting a merged manifest imply a provenance it does not have.
        LOG.warning(
            "at least one capture carries no captured_at, so 'newest' fell back to ARGUMENT ORDER "
            "(last --oracle wins). The merged manifests record merge_order_basis='%s'; pass the "
            "batches oldest-first, or re-capture with a manifest that carries a timestamp.",
            basis,
        )
    elif manifest.get("merge_order_ties"):
        # The subtler half (finding #5, review round 1). Timestamps EXIST, so the old code reported
        # `captured_at` -- but equal timestamps separate nothing, and reversing the arguments picked a
        # different winner while the manifest still claimed time had decided it.
        ties = manifest["merge_order_ties"]
        LOG.warning(
            "%d leg(s) had two or more captures with the SAME captured_at, so ARGUMENT ORDER (last "
            "--oracle wins) decided them -- reversing the arguments would pick differently. Recorded "
            "as merge_order_basis='%s' with the tied batches in merge_order_ties: %s",
            len(ties),
            basis,
            "; ".join(f"{t['leg']} <- {'/'.join(t['batches'])}" for t in ties[:4]),
        )
    if manifest.get("merge_stale_candidates"):
        # ⚠️ The blocker: an older batch's SUCCESSFUL leg, refused because the view has changed since.
        # It is a warning rather than a failure -- the merge is correct, and the operator's real
        # question is whether to re-capture -- but it must be said out loud, because the alternative
        # (promoting it) reported a stale render as current evidence with a digest beside it.
        stale = manifest["merge_stale_candidates"]
        LOG.warning(
            "%d leg(s) exist in an older capture for a DIFFERENT revision of the view and were NOT "
            "promoted: %s. A render of a workbook that has since changed is not weaker evidence than "
            "no render, it is evidence pointing the wrong way. Re-capture those views to establish "
            "them; merge_stale_candidates records each one with both revisions.",
            len(stale),
            "; ".join(f"{s['leg']} <- {s['batch']} ({s['captured_revision']})" for s in stale[:4]),
        )
    if len(batches) > 1 and len({tuple(v) for v in (manifest.get("requested_renders_by_batch") or {}).values()}) > 1:
        # Mixing a data-only batch into a render capture is legitimate -- and it used to REWRITE the
        # merged intent to that batch's, erasing every known render gap. Intent is unioned now; this
        # says the disagreement existed, because a data-only retry is worth noticing.
        LOG.warning(
            "the captures do not agree on what to render (%s). Intent is UNIONED, so a batch that "
            "asked for nothing cannot retract another batch's request; requested_renders_by_batch "
            "records who asked for what.",
            ", ".join(
                f"{label}={kinds or 'data only'}" for label, kinds in manifest["requested_renders_by_batch"].items()
            ),
        )

    ctx = _Context(manifest=manifest, destinations=destinations, roots=roots, dry_run=dry_run)
    outcomes = _group_all(buckets, ctx)
    report_dir = _write_grouping_report(
        _RunInputs(batches, migrations_root, basis, dry_run, excluded, oracle_root), outcomes
    )

    LOG.info(
        "\n%s%s",
        ", ".join(f"{len(outcomes[bucket])} {bucket}" for bucket in OUTCOME_BUCKETS),
        "" if dry_run else f" -> {report_dir / UNMATCHED_REPORT}",
    )
    if outcomes["incomplete"]:
        LOG.warning(
            "%d workbook(s) are missing artifacts their capture manifest names. The grouped manifests "
            "mark those legs '%s' rather than claiming them; re-run the grouping if the capture is "
            "intact, and only re-capture (metered) if it is not.",
            len(outcomes["incomplete"]),
            NOT_COPIED_STATUS,
        )
    if outcomes["collision"]:
        LOG.warning(
            "%d workbook(s) target a destination folder another workbook also claims, and NEITHER was "
            "written -- their names normalize onto one key while their LUIDs differ, so grouping them "
            "would attribute one workbook's evidence to another. Give each its own folder (the "
            "normalizer drops punctuation and case, so 'R&D' and 'R+D' collide).",
            len(outcomes["collision"]),
        )
    if outcomes["unidentified"]:
        LOG.warning(
            "%d workbook bucket(s) carry no workbook_luid, so which workbook their views belong to "
            "cannot be established and they were not grouped. Re-capture with a manifest that records "
            "workbook_luid; a display name is not an identity.",
            len(outcomes["unidentified"]),
        )
    if _incomplete(outcomes, manifest):
        LOG.warning(
            "the capture(s) in %s remain complete and authoritative - only the per-workbook copies are partial",
            ", ".join(str(b.directory) for b in batches),
        )
        return 1
    return 0


REFUSALS = (
    FileNotFoundError,
    json.JSONDecodeError,
    DuplicateBatchLabel,
    IncompatibleBatchSources,
    UnestablishedBatchSource,
    UnlistedBatchOnDisk,
    UnclassifiedCaptureDirectory,
    MalformedCaptureManifest,
    UnidentifiedCaptureView,
)


def main() -> int:
    """Entry point: parse arguments, group every capture on disk, map failures onto an exit code."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()
    try:
        return run(
            args.oracle,
            args.migrations,
            dry_run=args.dry_run,
            oracle_root=args.oracle_root,
            exclude=args.exclude,
        )
    except REFUSALS as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
