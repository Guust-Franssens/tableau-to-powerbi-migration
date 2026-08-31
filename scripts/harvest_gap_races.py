"""
purpose: decide whether the bundle held still while the harvest read it - and withhold every
         authorship claim that rests on bytes two different reads disagreed about.
usage:   imported by scripts/harvest_engine_gaps.py; not a user-facing CLI

⚠️ **This module exists because closing one window at a time did not terminate.** Rounds 6, 7 and 8
of the blind review of PR #399 each reported a DIFFERENT filesystem read with an identical shape -
provenance assigned from one read, evidence taken from another:

    round 6   `_load_evidence()` adjudicates, and only then `_scan_pairs()` reads the trees
    round 7   two inventory snapshots compared end to end, defeated by an ABA edit
    round 8   `_observed_race()` validates the tree digests, and `shapes_for_change()` re-reads

The rate was not decaying, because the property generating them was structural. So the fix is stated
over the whole class rather than per window: **every read that feeds a claim must carry the digest of
the bytes it consumed, and those digests are checked against one authority before any of it becomes
evidence.** A mismatch anywhere is a race, so no individual window matters.

That guarantee is only as good as the enumeration behind it. Measured on this tree, 17 filesystem
reads feed a claim:

    6  AUTHORITY   input_manifest.json, report.json, the rglob inventory, its per-file hashes,
                   `_current_hash`, and the declaration ledger - these DEFINE the snapshot
    1  DISCOVERY   `_safe_iterdir` over reports/, semantic_models/, pbip/ and each unit
    9  EVIDENCE    2 `hash_tree` walks (digest-carrying by construction -> `observed_race`)
                   + 7 inside `harvest_gap_shapes.py`, all behind `shapes_for_change()` and
                   `bound_model_tables()` (now digest-recording -> `consumed_race`)
    1  METADATA    engine-output-receipt.json, which makes no claim about bundle bytes

Detection, not prevention: user space cannot snapshot a filesystem atomically, and a bundle CAN
legitimately change under a long harvest. The honest answer to that is `incomplete` with the paths
named and the authorship withheld - never a verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from harvest_gap_trees import TreeDelta, withdraw

if TYPE_CHECKING:  # annotations only - the runtime dependency stays one-way, races -> trees
    from harvest_engine_gaps import Evidence, Pair

DRIFT_CHANGED = "changed"
DRIFT_MISSING = "missing"
DRIFT_ADDED = "added"

LAYER_REPORT = "report"
LAYER_MODEL = "model"

PROV_ENGINE = "engine_internal"
PROV_TIER = "tier_edit"
PROV_TAMPERED = "baseline_tampered"
PROV_UNATTRIBUTED = "unattributed"
PROVENANCES = (PROV_ENGINE, PROV_TIER, PROV_TAMPERED, PROV_UNATTRIBUTED)

# Raced paths named inline in the incomplete reason. The full list is always in `snapshot_race.moved`.
RACE_PATHS_NAMED = 3


def observed_race(pair: Pair, delta: TreeDelta, evidence: Evidence, bundle: Path) -> list[dict[str, Any]]:
    """Every path where what the SCAN READ disagrees with what ADJUDICATION ASSUMED.

    ⚠️ **Equality of two independent re-reads does not prove the bundle held still, and round 6
    shipped exactly that mistake.** Blind review round 7 defeated it with an ABA edit: change a
    working visual before `_scan_pairs`, let the scan consume the changed bytes, restore the original
    before the closing read. Both endpoints matched, so the run reported `snapshot_race.count=0`,
    `status=complete` and `engine_internal=1` for a real tier edit, and emitted the clean claim.

    So provenance is tied to the digests the comparison ACTUALLY CONSUMED rather than to a separate
    opinion about them. There is no window between the observation and the evidence, because the
    observation *is* the evidence: `hash_tree` already hashed every file this delta was computed
    from, and each is checked against the snapshot adjudication was decided on. ABA cannot hide -
    the scan's own read of B is the thing being compared.

    Three exclusions, each deliberate:
    * a path the snapshot has NO opinion about (a `.pbi` sidecar, a file created before the harvest
      but after the engine) is skipped - it is `unrecorded`, hence already `unattributed`, and
      flagging it would make every bundle with a Desktop sidecar look like a race;
    * a BLOCKED path (unreadable on either side) is skipped - `hash_tree` gives it no digest, and
      "could not read" is not "changed"; it is already withdrawn from both sides and reported as
      unassessable;
    * a snapshot entry of `None` (recorded but absent) matching a scan that also did not see it is
      agreement, not a race.

    Cost: **zero extra I/O.** The digests are a by-product of a read the harvest already paid for,
    which is why this replaced the closing re-observation rather than joining it (that read cost a
    measured +4.2s / +35% on the estate bundle, and could not see ABA anyway).
    """
    if not evidence.usable or evidence.snapshot is None or pair.baseline is None or pair.working is None:
        return []
    if not delta.scoped:
        # `hash_tree` could not even locate its own failure, so `delta.blocked` cannot scope this
        # comparison: every recorded path under the unread subtree would read as "missing" and a real
        # disagreement could hide behind one. Withhold rather than fabricate - `scoped: False` tells
        # `withdraw_raced` that the affected paths are unknown, so no claim may stand.
        return [
            {
                "target": pair.working.relative_to(bundle).as_posix(),
                "kind": "unlocatable_read_failure",
                "scoped": False,
            }
        ]
    raced = []
    for root, digests in (
        (pair.baseline.relative_to(bundle).as_posix(), delta.baseline_digests),
        (pair.working.relative_to(bundle).as_posix(), delta.working_digests),
    ):
        prefix = f"{root}/"
        opinions = {rel[len(prefix) :] for rel in evidence.snapshot.hashes if rel.startswith(prefix)}
        for relative in sorted(opinions | set(digests)):
            if relative not in opinions or not withdraw({relative}, delta.blocked):
                continue
            assumed, observed = evidence.snapshot.hashes.get(f"{prefix}{relative}"), digests.get(relative)
            if assumed == observed:
                continue
            kind = DRIFT_ADDED if assumed is None else DRIFT_MISSING if observed is None else DRIFT_CHANGED
            raced.append(
                {
                    "target": f"{prefix}{relative}",
                    "kind": kind,
                    "adjudicated_sha256": assumed,
                    "scanned_sha256": observed,
                }
            )
    return raced


def consumed_race(consumed: dict[Path, str], evidence: Evidence, bundle: Path) -> list[dict[str, Any]]:
    """Every file a CLASSIFIER read whose bytes are not the bytes provenance was adjudicated from.

    ⚠️ **The class, not another window.** Rounds 6, 7 and 8 of PR #399 each reported a different
    filesystem read with one shape: provenance assigned from one read, evidence taken from another.
    Round 8's was here - `observed_race` validated the tree-scan digests, and `shapes_for_change`
    then RE-READ the same files, so an edit landing in between was observed, classified into `LAYOUT`
    and reported as the engine's byte (`exit 0`, `complete`, `snapshot_race.count=0`,
    `engine_internal=2`, clean claim) while `tamper_check()` said `DRIFT`.

    Closing windows one at a time cannot terminate. The enumeration is what makes this final: 17
    filesystem reads feed a claim, of which 9 are EVIDENCE reads - two `hash_tree` walks (digest-
    carrying by construction, checked by `observed_race`) and seven inside `harvest_gap_shapes.py`,
    all behind `shapes_for_change()` and `bound_model_tables()`. Those seven now record the digest of
    the exact bytes they used, and this checks every one against the same authority. A mismatch
    ANYWHERE is a race, so no individual window matters any more.

    Paths the snapshot has no opinion about are skipped for the reason `observed_race` skips them: a
    `.pbi` sidecar or a post-engine addition is `unrecorded`, hence already `unattributed`.
    """
    if not evidence.usable or evidence.snapshot is None:
        return []
    root = bundle.resolve()
    raced = []
    for path, digest in sorted(consumed.items()):
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        assumed = evidence.snapshot.hashes.get(relative)
        if relative not in evidence.snapshot.hashes or assumed == digest:
            continue
        raced.append(
            {
                "target": relative,
                "kind": DRIFT_ADDED if assumed is None else DRIFT_CHANGED,
                "adjudicated_sha256": assumed,
                "scanned_sha256": digest,
                "read_by": "classifier",
            }
        )
    return raced


def dedupe_races(raced: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ONE entry per path. The tree scan and a classifier can both observe the same movement.

    Without this, `snapshot_race.count` reports OBSERVATIONS rather than paths - an edit seen by both
    `observed_race` and `consumed_race` counted twice, which reads as two files having moved.
    """
    unique: dict[str, dict[str, Any]] = {}
    for item in raced:
        unique.setdefault(item["target"], item)
    return list(unique.values())


def artifact_key(target: str) -> tuple[str, str, str] | None:
    """(artifact, layer, artifact-relative path) for a bundle path, from EITHER side of the bundle."""
    parts = target.split("/")
    for index, part in enumerate(parts):
        for suffix, layer in ((".Report", LAYER_REPORT), (".SemanticModel", LAYER_MODEL)):
            if part.endswith(suffix):
                return part[: -len(suffix)], layer, "/".join(parts[index + 1 :])
    return None


def withdraw_raced(records: list[dict[str, Any]], raced: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Withdraw every AUTHORSHIP claim about a path whose two reads disagree.

    `incomplete` alone is not enough: the defect is not a missing caveat but a positively wrong
    attribution, so the claim itself is withdrawn to `unattributed` - the standing idiom, "the delta
    is real; authorship is withheld, not guessed".

    Matched on the artifact-relative triple as well as the literal paths, because a working file
    DELETED mid-scan is recorded with `working_path=None` (read as the engine's own reference-only
    emission) and would otherwise keep an `engine_internal` label the raced set cannot see. A shared
    model copied into several units matches in all of them; over-withdrawal is the safe direction.
    `baseline_tampered` is deliberately NOT withdrawn: it rests on a positive observation that a
    baseline path had ALREADY drifted, and withdrawing it would drop the run from `untrustworthy` to
    the weaker `incomplete`.

    ⚠️ **A race entry that cannot name its paths withdraws EVERYTHING.** Blind review round 7: the
    round-6 detector reported a failed closing read as a race whose `target` was the bundle root,
    which matched no record at all - exit 3 was correct, and the body still said
    `engine_internal=2, coverage.complete=true`. Exit code and body must agree. Entries carry
    `scoped`; anything false means "something moved and I cannot say what", and the only honest
    response is to withhold every authorship claim rather than the empty set of them.

    Idempotent, because it runs per pair (so `pairs[]` and `records` cannot diverge) and again over
    the combined record list (so an unpaired record touching a raced path is covered too).
    """
    if not raced:
        return records
    unscoped = any(not item.get("scoped", True) for item in raced)
    targets = {item["target"] for item in raced}
    keys = {key for key in (artifact_key(target) for target in targets) if key is not None}
    withdrawn = []
    for record in records:
        paths = {p for p in (record.get("baseline_path"), record.get("working_path")) if p}
        touched = unscoped or (record["artifact"], record["layer"], record["path"]) in keys or bool(paths & targets)
        if touched and record["provenance"] in {PROV_ENGINE, PROV_TIER}:
            record = record | {
                "provenance": PROV_UNATTRIBUTED,
                "post_engine": None,
                "declared_by": None,
                "snapshot_race": True,
            }
        elif touched:
            record = record | {"snapshot_race": True}
        withdrawn.append(record)
    return withdrawn


def race_reasons(raced: list[dict[str, Any]]) -> list[str]:
    """The incomplete reason a race produces - named paths, and whether the scope is known."""
    if not raced:
        return []
    named = ", ".join(f"{item['target']} ({item['kind']})" for item in raced[:RACE_PATHS_NAMED])
    scope = (
        " Their scope is UNKNOWN, so every authorship claim in this run is withdrawn."
        if any(not item.get("scoped", True) for item in raced)
        else " Every authorship claim touching them is withdrawn; re-run against a still bundle."
    )
    return [
        f"the bundle MOVED during this harvest: for {len(raced)} path(s) the bytes the comparison"
        f" READ are not the bytes provenance was ADJUDICATED from - {named}.{scope}"
    ]
