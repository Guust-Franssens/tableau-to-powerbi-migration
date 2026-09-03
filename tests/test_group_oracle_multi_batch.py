"""Promotion must read EVERY batch present for a view, newest-successful-wins (#423).

A metered, timing-out capture is re-run in batches, and the same view can succeed in a later batch
having failed in an earlier one. Field evidence: *Daily Monitoring* failed its data leg twice, then
on a third batch produced BOTH a data leg and 905,098 bytes of PNG -- and the workbook's
`reference/` folder only ever cross-referenced the first two batches, so a successful capture sat
unused at `_oracle/airborne-services-retry2/images/Daily_Monitoring__0979a4f9.png`.

Grouping one directory at a time cannot fix that, and the failure is worse than "missed": the last
invocation OVERWRITES the per-workbook manifest, so a partial re-run can replace a good artifact
with a failure from a batch that only covered some views.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import group_oracle_by_workbook as grp  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_oracle_manifest as verdict  # noqa: E402  # pylint: disable=wrong-import-position

LUID = "0979a4f9-1111-2222-3333-444444444444"
OTHER = "0979a4f9-5555-6666-7777-888888888888"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
CSV = "region,sales\nEast,12\n"
# The view's own last-modified stamp, as Tableau reports it. ⚠️ NOT `captured_at`: that says when WE
# looked, this says when the workbook last changed, and the difference is what decides whether two
# batches describe the same thing. Shared by default so the existing scenarios below keep testing what
# they were written for -- two captures of ONE revision, which is the ordinary case.
REVISION = "2026-07-01T00:00:00Z"
# The `_batch` default capture stamp, named so a test that only needs "some consistent time" says so.
STAMP = "2026-08-18T14:46:00Z"


def _view(
    luid: str, name: str, *, data: str, image: str | None, captured_at: str, updated_at: str | None = REVISION
) -> dict:
    """One view record, shaped exactly as `capture_tableau_oracle` writes it.

    ⚠️ That claim used to be FALSE in a load-bearing way: the real writer stamps
    `"updated_at": view.get("updatedAt")` (`capture_tableau_oracle.py`), and this fixture omitted it
    entirely. Every scenario here therefore described views of unknown revision, which is the one shape
    that cannot support a cross-batch promotion -- so the fixture could not distinguish "the same view,
    captured twice" from "two different versions of a view", which is exactly the merge defect.

    `updated_at=None` is still reachable on purpose, because a server that omits `updatedAt` really
    does produce it, and the fail-closed handling of that case is pinned rather than assumed.
    """
    view: dict = {
        "view_luid": luid,
        "view_name": name,
        "workbook_luid": "wb-1",
        "workbook_name": "airborne services",
        "captured_at": captured_at,
        "updated_at": updated_at,
        "data": (
            {"status": "ok", "path": f"data/{luid}.csv", "row_count": 1, "bytes": len(CSV)}
            if data == "ok"
            else {"status": data, "error": "HTTP 0", "detail": "read operation timed out"}
        ),
    }
    if image == "ok":
        view["image"] = {"status": "ok", "format": "png", "path": f"images/{luid}.png", "bytes": len(PNG)}
    elif image is not None:
        view["image"] = {"status": image, "error": "HTTP 0", "detail": "read operation timed out"}
    return view


def _batch(
    root: Path,
    name: str,
    views: list[dict],
    *,
    captured_at: str | None = "2026-08-18T14:46:00Z",
    requested_renders: list[str] | None = None,
    server: str | None = "https://example.online.tableau.com",
    site: str | None = "acme",
) -> Path:
    """Write one `_oracle/<batch>/` directory, materialising only the artifacts its views claim.

    ``requested_renders`` defaults to ``["png"]`` and is settable to ``[]`` so a **data-only** batch
    can be built -- the shape that erased another batch's render intent (review round 1, finding 2).

    ``server``/``site`` default to ONE source, because that is what merging two batches presupposes.
    They are settable so the cross-tenant refusal can be exercised, and settable to ``None`` so a
    manifest that records no source at all -- an older capture -- can be built too.
    """
    directory = root / name
    (directory / "data").mkdir(parents=True, exist_ok=True)
    (directory / "images").mkdir(parents=True, exist_ok=True)
    for view in views:
        if (view.get("data") or {}).get("status") == "ok":
            (directory / "data" / f"{view['view_luid']}.csv").write_text(CSV, encoding="utf-8")
        if (view.get("image") or {}).get("status") == "ok":
            (directory / "images" / f"{view['view_luid']}.png").write_bytes(PNG)
    renders = ["png"] if requested_renders is None else requested_renders
    manifest: dict = {"schema": "tableau-oracle/1", "requested_renders": renders, "views": views}
    if captured_at is not None:
        manifest["captured_at"] = captured_at
    if server is not None:
        manifest["server"] = server
    if site is not None:
        manifest["site"] = site
    (directory / grp.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return directory


def _migrations(tmp_path: Path) -> Path:
    root = tmp_path / "migrations" / "workbooks"
    (root / "airborne-services").mkdir(parents=True)
    return root


def _grouped(migrations: Path) -> dict:
    text = (migrations / "airborne-services" / "reference" / grp.MANIFEST_NAME).read_text(encoding="utf-8")
    return json.loads(text)


def _three_batches(tmp_path: Path) -> list[Path]:
    """The customer's actual sequence, and the retries are PARTIAL because real retries are.

    Batch 1 is the full sweep: *Daily Monitoring* failed both legs, *Availability Summary by Tail*
    succeeded at both. Batches 2 and 3 re-run only the view that failed, and batch 3 is where it
    finally produced 905,098 bytes of PNG.

    ⚠️ The partial shape is load-bearing, not decoration (review round 1, finding 6). With all three
    batches carrying every view and the last one carrying both successful legs, reading ONLY the last
    batch produced an identical answer -- so the `merge-reads-only-the-last-batch` mutation SURVIVED
    against this file's advertised anchor. A fixture whose final batch already contains the whole
    answer cannot demonstrate that more than the last batch was read.
    """
    oracle = tmp_path / "_oracle"
    sibling = _view(OTHER, "Availability Summary by Tail", data="ok", image="ok", captured_at="2026-08-17T20:17:00Z")
    return [
        _batch(
            oracle,
            "airborne-services",
            [
                _view(
                    LUID, "Daily Monitoring", data="transient", image="transient", captured_at="2026-08-17T20:17:00Z"
                ),
                sibling,
            ],
            captured_at="2026-08-17T20:17:00Z",
        ),
        _batch(
            oracle,
            "airborne-services-retry",
            [_view(LUID, "Daily Monitoring", data="transient", image="transient", captured_at="2026-08-17T20:32:00Z")],
            captured_at="2026-08-17T20:32:00Z",
        ),
        _batch(
            oracle,
            "airborne-services-retry2",
            [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
            captured_at="2026-08-18T14:46:00Z",
        ),
    ]


def _by_luid(grouped: dict) -> dict[str, dict]:
    return {view["view_luid"]: view for view in grouped["views"]}


# --------------------------------------------------------------------- the stranded artifact


def test_a_later_batch_that_finally_succeeded_is_promoted(tmp_path):
    """⚠️ THE anchor of this file, and it now requires reading MORE than one batch to satisfy.

    Two assertions, deliberately: the view that finally succeeded on batch 3 is promoted, AND the
    sibling that only ever appeared in batch 1 -- because the retries were partial, as real retries
    are -- is still there. Reading only the last batch satisfies the first and fails the second,
    which is what makes `merge-reads-only-the-last-batch` fail here rather than SURVIVE.
    """
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)

    assert grp.run(batches, migrations, dry_run=False) == 0

    views = _by_luid(_grouped(migrations))
    assert set(views) == {LUID, OTHER}, "a partial retry must not drop the views it did not cover"
    assert views[LUID]["image"]["status"] == "ok"
    assert views[LUID]["data"]["status"] == "ok"
    assert views[LUID]["image"]["source_batch"] == "airborne-services-retry2"
    assert views[OTHER]["image"]["source_batch"] == "airborne-services", "only batch 1 ever saw it"
    assert (migrations / "airborne-services" / "reference" / "images" / f"{LUID}.png").read_bytes() == PNG
    assert (migrations / "airborne-services" / "reference" / "images" / f"{OTHER}.png").read_bytes() == PNG


def test_every_promoted_artifact_names_the_batch_it_came_from(tmp_path):
    """'Which capture did this image come from' has to be answerable from the artifact, not from
    somebody's memory of which retry directory they ran last."""
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)
    grp.run(batches, migrations, dry_run=False)

    grouped = _grouped(migrations)
    view = grouped["views"][0]
    assert view["image"]["source_batch"] == "airborne-services-retry2"
    assert view["data"]["source_batch"] == "airborne-services-retry2"
    assert grouped["batches"][0] == "airborne-services-retry2", "newest first, so a reader sees the winner"


def test_the_two_legs_may_come_from_DIFFERENT_batches(tmp_path):
    """The merge is per LEG, not per view. A batch that recovered only the image must not have to
    also beat the earlier batch's data leg to contribute what it did establish."""
    oracle = tmp_path / "_oracle"
    batches = [
        _batch(
            oracle,
            "first",
            [_view(LUID, "Daily Monitoring", data="ok", image="transient", captured_at="2026-08-17T20:17:00Z")],
            captured_at="2026-08-17T20:17:00Z",
        ),
        _batch(
            oracle,
            "second",
            [_view(LUID, "Daily Monitoring", data="transient", image="ok", captured_at="2026-08-18T14:46:00Z")],
            captured_at="2026-08-18T14:46:00Z",
        ),
    ]
    migrations = _migrations(tmp_path)
    grp.run(batches, migrations, dry_run=False)

    view = _grouped(migrations)["views"][0]
    assert (view["data"]["status"], view["data"]["source_batch"]) == ("ok", "first")
    assert (view["image"]["status"], view["image"]["source_batch"]) == ("ok", "second")


def test_a_partial_re_run_cannot_overwrite_a_view_it_never_captured(tmp_path):
    """The destructive half. A retry batch covering ONE view used to replace the whole per-workbook
    manifest, so a sibling view's good artifacts vanished from the reference folder."""
    oracle = tmp_path / "_oracle"
    full = _batch(
        oracle,
        "full",
        [
            _view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-17T20:17:00Z"),
            _view(OTHER, "Availability Summary by Tail", data="ok", image="ok", captured_at="2026-08-17T20:17:00Z"),
        ],
        captured_at="2026-08-17T20:17:00Z",
    )
    partial = _batch(
        oracle,
        "retry-one-view",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
    )
    migrations = _migrations(tmp_path)
    grp.run([full, partial], migrations, dry_run=False)

    grouped = _grouped(migrations)
    by_luid = {v["view_luid"]: v for v in grouped["views"]}
    assert set(by_luid) == {LUID, OTHER}, "the view the retry did not cover must survive the merge"
    assert by_luid[OTHER]["image"]["status"] == "ok"
    assert by_luid[OTHER]["image"]["source_batch"] == "full"
    assert by_luid[LUID]["image"]["source_batch"] == "retry-one-view"


# ------------------------------------------------- a claim is not evidence, and a gap stays visible


def test_a_later_DATA_ONLY_batch_does_not_erase_a_known_render_gap(tmp_path):
    """⚠️ Review round 1, finding 2 -- reproduced exactly, then fixed.

    An older batch that asked for `png` and got `image: transient`, followed by a newer **data-only**
    batch, used to produce `requested_renders=[]`, NO merged `image` key, and
    `render_unestablished == 0`. A known render gap silently became "nothing was ever requested" --
    which is the `UNESTABLISHED != absent` regression this whole PR exists to prevent, re-created by
    the merge one level up.

    Two independent causes, both fixed and both asserted here: intent was copied from the newest
    batch alone, and the per-leg fallback took the newest VIEW rather than the newest view that has
    a record for that leg.
    """
    oracle_dir = tmp_path / "_oracle"
    asked_and_failed = _batch(
        oracle_dir,
        "png-batch",
        [_view(LUID, "Daily Monitoring", data="transient", image="transient", captured_at="2026-08-17T20:17:00Z")],
        captured_at="2026-08-17T20:17:00Z",
        requested_renders=["png"],
    )
    data_only = _batch(
        oracle_dir,
        "data-only-retry",
        [_view(LUID, "Daily Monitoring", data="ok", image=None, captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
        requested_renders=[],
    )
    migrations = _migrations(tmp_path)
    grp.run([asked_and_failed, data_only], migrations, dry_run=False)

    grouped = _grouped(migrations)
    assert grouped["requested_renders"] == ["png"], "intent is UNIONED; a data-only batch cannot retract it"
    view = grouped["views"][0]
    assert view["data"]["status"] == "ok", "the newer batch still wins the leg it did capture"
    assert view["data"]["source_batch"] == "data-only-retry"
    assert view["image"]["status"] == "transient", "the older batch's known gap must survive"
    assert view["image"]["source_batch"] == "png-batch"
    assert grouped["render_unestablished"] == 1
    assert grouped["render_unestablished_views"][0]["renders"] == {"png": "transient"}


def test_the_capture_wide_merge_also_keeps_the_render_intent(tmp_path):
    """The same erasure at the merged-manifest level, which is what `capture_unestablished` reads.
    Asserted on `merge_batches` directly so a grouping-layer fix cannot mask a merge-layer one."""
    oracle_dir = tmp_path / "_oracle"
    batches = grp.load_batches(
        [
            _batch(
                oracle_dir,
                "png-batch",
                [
                    _view(
                        LUID,
                        "Daily Monitoring",
                        data="transient",
                        image="transient",
                        captured_at="2026-08-17T20:17:00Z",
                    )
                ],
                captured_at="2026-08-17T20:17:00Z",
                requested_renders=["png"],
            ),
            _batch(
                oracle_dir,
                "data-only-retry",
                [_view(LUID, "Daily Monitoring", data="ok", image=None, captured_at="2026-08-18T14:46:00Z")],
                captured_at="2026-08-18T14:46:00Z",
                requested_renders=[],
            ),
        ]
    )
    manifest, _roots, _basis = grp.merge_batches(batches)

    assert manifest["requested_renders"] == ["png"]
    assert manifest["requested_renders_by_batch"] == {"png-batch": ["png"], "data-only-retry": []}
    assert manifest["views"][0]["image"]["status"] == "transient"
    census = verdict.render_unestablished(manifest["views"], frozenset(manifest["requested_renders"]))
    assert len(census) == 1


def test_a_batch_that_required_a_reference_is_not_overruled_by_one_that_did_not(tmp_path):
    """`reference_required` is intent too, so it is an `any` for the same reason -- and
    `reference_missing` is RECOMPUTED, because the newest batch's verdict is about a different set of
    views than the merged one."""
    oracle_dir = tmp_path / "_oracle"
    required = _batch(
        oracle_dir,
        "wanted-a-reference",
        [_view(LUID, "Daily Monitoring", data="ok", image="transient", captured_at="2026-08-17T20:17:00Z")],
        captured_at="2026-08-17T20:17:00Z",
    )
    (required / grp.MANIFEST_NAME).write_text(
        json.dumps(
            {**json.loads((required / grp.MANIFEST_NAME).read_text(encoding="utf-8")), "reference_required": True}
        ),
        encoding="utf-8",
    )
    later = _batch(
        oracle_dir,
        "did-not-ask",
        [_view(LUID, "Daily Monitoring", data="ok", image=None, captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
        requested_renders=[],
    )
    manifest, _roots, _basis = grp.merge_batches(grp.load_batches([required, later]))

    assert manifest["reference_required"] is True
    assert manifest["reference_missing"] is True, "no view has an ok render, and one was required"


def test_reference_missing_is_false_once_any_render_landed(tmp_path):
    """Positive control for the recompute above: it must be able to be False, or it is a constant."""
    oracle_dir = tmp_path / "_oracle"
    batch = _batch(
        oracle_dir,
        "only",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
    )
    manifest, _roots, _basis = grp.merge_batches(grp.load_batches([batch]))
    assert manifest["reference_missing"] is False


def test_a_newer_batch_whose_file_is_GONE_does_not_displace_an_older_one_that_has_it(tmp_path):
    """A manifest entry is a CLAIM. Promoting a newer `ok` whose bytes have since been deleted would
    make the merged reference set worse than either input on its own."""
    oracle = tmp_path / "_oracle"
    older = _batch(
        oracle,
        "older",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-17T20:17:00Z")],
        captured_at="2026-08-17T20:17:00Z",
    )
    newer = _batch(
        oracle,
        "newer",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
    )
    (newer / "images" / f"{LUID}.png").unlink()

    migrations = _migrations(tmp_path)
    assert grp.run([older, newer], migrations, dry_run=False) == 0

    view = _grouped(migrations)["views"][0]
    assert view["image"]["source_batch"] == "older", "the batch that still HAS the bytes must win"
    assert (migrations / "airborne-services" / "reference" / "images" / f"{LUID}.png").is_file()


def test_a_view_no_batch_could_render_stays_visibly_unestablished(tmp_path):
    """The merge must not quietly drop a view nothing succeeded for. That collapse -- unassessable
    state landing in the clean bucket -- is the defect class this whole issue is about."""
    oracle = tmp_path / "_oracle"
    batches = [
        _batch(
            oracle,
            "first",
            [
                _view(
                    LUID,
                    "Availability Summary by Tail",
                    data="transient",
                    image="transient",
                    captured_at="2026-08-17T20:17:00Z",
                )
            ],
            captured_at="2026-08-17T20:17:00Z",
        ),
        _batch(
            oracle,
            "second",
            [
                _view(
                    LUID,
                    "Availability Summary by Tail",
                    data="transient",
                    image="transient",
                    captured_at="2026-08-18T14:46:00Z",
                )
            ],
            captured_at="2026-08-18T14:46:00Z",
        ),
    ]
    migrations = _migrations(tmp_path)
    assert grp.run(batches, migrations, dry_run=False) == 0

    view = _grouped(migrations)["views"][0]
    assert view["image"]["status"] == "transient", "the failure must survive, not be dropped"
    assert view["image"]["source_batch"] == "second", "and the newest attempt is the one reported"


# ---------------------------------------------------------------- ordering, and when it is not evidence


def test_argument_order_does_not_decide_the_winner_when_timestamps_do(tmp_path):
    """Passing the batches newest-first must give the same answer as oldest-first. Otherwise the
    result depends on an operator's typing habit, and two people "merge" differently.

    ⚠️ BOTH batches must hold a promotable leg, or this asserts nothing. Measured: an earlier version
    reused the three-batch fixture where only ONE batch has an `ok` image, so every ordering picked
    the same winner by elimination and the mutation that sorts by argv order SURVIVED. Two winners in
    contention is what makes the ordering rule observable.
    """
    oracle_dir = tmp_path / "_oracle"
    older = _batch(
        oracle_dir,
        "older",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-17T20:17:00Z")],
        captured_at="2026-08-17T20:17:00Z",
    )
    newer = _batch(
        oracle_dir,
        "newer",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
    )
    oldest_first = _migrations(tmp_path / "a")
    newest_first = _migrations(tmp_path / "b")

    grp.run([older, newer], oldest_first, dry_run=False)
    grp.run([newer, older], newest_first, dry_run=False)

    assert _grouped(oldest_first)["views"][0]["image"]["source_batch"] == "newer"
    assert _grouped(newest_first)["views"][0]["image"]["source_batch"] == "newer"


def test_a_per_view_timestamp_outranks_the_batch_manifest_timestamp(tmp_path):
    """A batch can span time, so the VIEW's own `captured_at` is the primary key and the manifest's
    is the fallback. Without this the merge dates every view in a long capture identically."""
    oracle_dir = tmp_path / "_oracle"
    stale_batch_fresh_view = _batch(
        oracle_dir,
        "stale-batch",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-19T09:00:00Z")],
        captured_at="2026-08-01T00:00:00Z",
    )
    fresh_batch_stale_view = _batch(
        oracle_dir,
        "fresh-batch",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-20T00:00:00Z",
    )
    migrations = _migrations(tmp_path)
    grp.run([fresh_batch_stale_view, stale_batch_fresh_view], migrations, dry_run=False)

    assert _grouped(migrations)["views"][0]["image"]["source_batch"] == "stale-batch"


def test_an_undated_batch_is_reported_rather_than_dated_by_argv(tmp_path, caplog):
    """⚠️ 'Newest wins' with no timestamp means the winner is decided by the order somebody typed,
    which is a habit, not evidence. Say so instead of implying a provenance the merge does not have."""
    import logging  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    oracle = tmp_path / "_oracle"
    undated_view = _view(LUID, "Daily Monitoring", data="ok", image="transient", captured_at="")
    undated_view.pop("captured_at")
    batches = [
        _batch(oracle, "undated", [undated_view], captured_at=None),
        _batch(
            oracle,
            "dated",
            [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
            captured_at="2026-08-18T14:46:00Z",
        ),
    ]
    migrations = _migrations(tmp_path)
    with caplog.at_level(logging.WARNING, logger="group-oracle"):
        grp.run(batches, migrations, dry_run=False)

    assert "ARGUMENT ORDER" in caplog.text
    assert _grouped(migrations)["merge_order_basis"] == "argument order"


def test_a_single_dated_capture_reports_captured_at_as_the_basis(tmp_path):
    """Positive control: the warning above must be able to NOT fire, or it is noise on every run."""
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)
    grp.run(batches, migrations, dry_run=False)
    assert _grouped(migrations)["merge_order_basis"] == "captured_at"
    assert _grouped(migrations)["merge_order_ties"] == []


# ---------------------------------------------- equal timestamps decide NOTHING, and must say so


def _tied_pair(tmp_path: Path, stamp: str = "2026-08-18T14:46:00Z") -> tuple[Path, Path]:
    """Two batches with IDENTICAL timestamps, each holding a different failed image status.

    Both fail, so neither is promotable and the winner comes from the per-leg fallback -- which is
    where argument order silently takes over.
    """
    oracle_dir = tmp_path / "_oracle"
    first = _batch(
        oracle_dir,
        "alpha",
        [_view(LUID, "Daily Monitoring", data="ok", image="failed", captured_at=stamp)],
        captured_at=stamp,
    )
    second = _batch(
        oracle_dir,
        "beta",
        [_view(LUID, "Daily Monitoring", data="ok", image="transient", captured_at=stamp)],
        captured_at=stamp,
    )
    return first, second


def test_equal_timestamps_are_reported_as_a_tie_not_as_captured_at(tmp_path):
    """⚠️ Review round 1, finding 5 -- reproduced exactly, then fixed.

    Two batches with identical stamps produced DIFFERENT winners when the arguments were reversed --
    one keeping `image: failed`, the other `image: transient` -- while both claimed
    `merge_order_basis == "captured_at"`. Timestamps that are equal separate nothing; saying they
    decided it is the same class of false confidence as an absent leg reading as "not requested".
    """
    first, second = _tied_pair(tmp_path)
    forwards = _migrations(tmp_path / "a")
    backwards = _migrations(tmp_path / "b")

    grp.run([first, second], forwards, dry_run=False)
    grp.run([second, first], backwards, dry_run=False)

    one, other = _grouped(forwards), _grouped(backwards)
    assert one["views"][0]["image"]["status"] != other["views"][0]["image"]["status"], (
        "the fixture must actually be order-sensitive, or this test proves nothing"
    )
    for grouped in (one, other):
        assert grouped["merge_order_basis"] == "captured_at, ties broken by argument order"
        image_ties = [tie for tie in grouped["merge_order_ties"] if tie["leg"] == "image"]
        assert len(image_ties) == 1, grouped["merge_order_ties"]
        assert set(image_ties[0]["batches"]) == {"alpha", "beta"}
        assert image_ties[0]["view_luid"] == LUID


def test_the_tie_warning_names_the_batches_that_could_not_be_separated(tmp_path, caplog):
    """A basis string in a JSON file is not an alert. The operator has to be told at the console."""
    import logging  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    first, second = _tied_pair(tmp_path)
    with caplog.at_level(logging.WARNING, logger="group-oracle"):
        grp.run([first, second], _migrations(tmp_path), dry_run=False)

    assert "SAME captured_at" in caplog.text
    assert "ARGUMENT ORDER" in caplog.text
    assert "alpha" in caplog.text and "beta" in caplog.text


def test_a_tie_that_decides_NOTHING_is_not_reported(tmp_path):
    """The discriminating half. A tie is only worth reporting when it picked the winner: two batches
    sharing a timestamp where only ONE has a promotable leg are separated by evidence, not by argv,
    and flagging that would make the field fire on ordinary runs until nobody reads it."""
    oracle_dir = tmp_path / "_oracle"
    stamp = "2026-08-18T14:46:00Z"
    good = _batch(
        oracle_dir,
        "good",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at=stamp)],
        captured_at=stamp,
    )
    bad = _batch(
        oracle_dir,
        "bad",
        [_view(LUID, "Daily Monitoring", data="ok", image="transient", captured_at=stamp)],
        captured_at=stamp,
    )
    migrations = _migrations(tmp_path)
    grp.run([bad, good], migrations, dry_run=False)

    grouped = _grouped(migrations)
    assert grouped["views"][0]["image"]["source_batch"] == "good", "promotability, not order, decided it"
    ties = [t for t in grouped["merge_order_ties"] if t["leg"] == "image"]
    assert ties == [], "only a DECIDING tie is worth reporting"


def test_the_workbook_manifest_says_which_views_have_no_establishable_render(tmp_path):
    """⚠️ The per-workbook manifest is what a fidelity review actually opens, and it must answer
    "for which pages of THIS workbook can no visual finding be made". The capture-wide count cannot:
    it spans every workbook, and it is computed BEFORE grouping, so it cannot see a leg the capture
    obtained but the grouping could not place."""
    oracle_dir = tmp_path / "_oracle"
    batch = _batch(
        oracle_dir,
        "only",
        [
            _view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z"),
            _view(
                OTHER,
                "Availability Summary by Tail",
                data="transient",
                image="transient",
                captured_at="2026-08-18T14:46:00Z",
            ),
        ],
    )
    migrations = _migrations(tmp_path)
    grp.run([batch], migrations, dry_run=False)

    grouped = _grouped(migrations)
    assert grouped["render_unestablished"] == 1
    named = grouped["render_unestablished_views"]
    assert [v["view_name"] for v in named] == ["Availability Summary by Tail"]
    assert named[0]["renders"] == {"png": "transient"}


def test_an_artifact_the_grouping_could_not_place_counts_as_unestablished(tmp_path):
    """The half the capture-wide count structurally cannot see. The capture says `image: ok`; the
    file is gone, so the reference folder does NOT hold that image -- and a reviewer reading only
    this manifest must not be told the page is covered."""
    oracle_dir = tmp_path / "_oracle"
    batch = _batch(
        oracle_dir,
        "only",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
    )
    (batch / "images" / f"{LUID}.png").unlink()

    migrations = _migrations(tmp_path)
    assert grp.run([batch], migrations, dry_run=False) == 1

    grouped = _grouped(migrations)
    assert grouped["image_ok"] == 0
    assert grouped["render_unestablished"] == 1
    assert grouped["render_unestablished_views"][0]["renders"] == {"png": grp.NOT_COPIED_STATUS}


def test_a_workbook_whose_renders_all_landed_reports_zero_unestablished(tmp_path):
    """Positive control: the field must be able to be zero, or it is a view count in disguise."""
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)
    grp.run(batches, migrations, dry_run=False)
    grouped = _grouped(migrations)
    assert grouped["render_unestablished"] == 0
    assert grouped["render_unestablished_views"] == []


# ------------------------------------------------------------- batch identity must be unambiguous


def test_two_captures_with_the_same_directory_NAME_stay_distinguishable(tmp_path):
    """⚠️ Review round 2, finding 2 -- reproduced exactly, then fixed.

    `run1\\oracle` and `run2\\oracle` are two captures. Labelled by `directory.name` they collapsed
    into ONE `roots["oracle"]` pointing at whichever was read last, `batches` read
    `["oracle", "oracle"]`, and both legs claimed indistinguishable `source_batch="oracle"` -- so an
    older candidate could resolve its artifact against the WRONG directory, and one batch's render
    intent could be erased. Same class as round 1's finding 2, arriving through provenance rather
    than through merge order.
    """
    first = _batch(
        tmp_path / "run1",
        "oracle",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-17T20:17:00Z")],
        captured_at="2026-08-17T20:17:00Z",
    )
    second = _batch(
        tmp_path / "run2",
        "oracle",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
    )
    migrations = _migrations(tmp_path)
    assert grp.run([first, second], migrations, dry_run=False) == 0

    grouped = _grouped(migrations)
    assert len(set(grouped["batches"])) == 2, f"two captures collapsed into one label: {grouped['batches']}"
    assert grouped["views"][0]["image"]["source_batch"] == "run2/oracle", "the newer capture must win, by NAME"
    assert set(grouped["requested_renders_by_batch"]) == {"run1/oracle", "run2/oracle"}


def test_a_disambiguated_label_says_WHICH_capture_not_merely_that_they_differ(tmp_path):
    """An index suffix (`oracle`, `oracle-2`) would satisfy uniqueness and destroy the point: the
    label is provenance, and a reader has to be able to find the directory it names."""
    labels = grp._batch_labels([tmp_path / "run1" / "oracle", tmp_path / "run2" / "oracle"])
    assert labels == ["run1/oracle", "run2/oracle"]


def test_unique_names_are_left_alone(tmp_path):
    """Discriminating control: disambiguation must be the exception. Prefixing every label with its
    parent would churn `source_batch` for every existing capture and make the common case unreadable."""
    labels = grp._batch_labels([tmp_path / "_oracle" / "first", tmp_path / "_oracle" / "second"])
    assert labels == ["first", "second"]


def test_the_same_capture_given_twice_is_refused_not_deduplicated(tmp_path):
    """Merging a batch with itself cannot add evidence, so it is a mistake worth naming rather than a
    no-op -- and silently deduplicating would hide a mis-typed command line."""
    only = _batch(
        tmp_path / "_oracle",
        "only",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
    )
    with pytest.raises(grp.DuplicateBatchLabel) as excinfo:
        grp.run([only, only], _migrations(tmp_path), dry_run=False)
    assert "more than once" in str(excinfo.value)


def test_a_duplicate_capture_exits_2_rather_than_crashing(tmp_path):
    """The operator sees an exit code, not an exception. `main` must classify this like every other
    unusable input."""
    only = _batch(
        tmp_path / "_oracle",
        "only",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
    )
    migrations = _migrations(tmp_path)
    argv = ["--oracle", str(only), "--oracle", str(only), "--migrations", str(migrations)]
    sys.argv = ["group_oracle_by_workbook.py", *argv]
    assert grp.main() == 2


# ------------------------------------------------------------------------------- CLI and compatibility


def test_the_oracle_flag_is_repeatable():
    args = grp.build_parser().parse_args(["--oracle", "a", "--oracle", "b", "--oracle", "c"])
    assert [p.name for p in args.oracle] == ["a", "b", "c"]


def test_a_single_path_still_works_unchanged(tmp_path):
    """Every existing caller passes one `Path`. Breaking that to add a list would be a migration this
    change does not need -- and `main()` always hands over a list, so both shapes are exercised.

    ⚠️ The batch lives in a parent of its own now, deliberately. Passing `_three_batches()[2]` alone
    is no longer "one capture": its two siblings are on disk, and merging without them is exactly the
    silent under-merge finding 5 refuses. That the old form of this test broke is the guard working.
    """
    oracle = tmp_path / "_oracle"
    only = _batch(
        oracle,
        "airborne-services",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
    )
    migrations = _migrations(tmp_path)
    assert grp.run(only, migrations, dry_run=False) == 0
    assert _grouped(migrations)["views"][0]["image"]["status"] == "ok"


def test_a_missing_manifest_in_ANY_batch_is_fatal_not_skipped(tmp_path):
    """Silently grouping two of three batches would produce a folder that looks complete and is not."""
    batches = _three_batches(tmp_path)
    (batches[1] / grp.MANIFEST_NAME).unlink()
    with pytest.raises(FileNotFoundError) as excinfo:
        grp.run(batches, _migrations(tmp_path), dry_run=False)
    assert "airborne-services-retry" in str(excinfo.value)


def test_the_grouping_report_names_every_batch_it_merged(tmp_path):
    batches = _three_batches(tmp_path)
    grp.run(batches, _migrations(tmp_path), dry_run=False)
    report = json.loads((batches[-1] / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert [Path(p).name for p in report["oracle_dirs"]] == [b.name for b in batches]
    assert report["merge_order_basis"] == "captured_at"


def test_the_capture_manifests_are_never_mutated_by_merging(tmp_path):
    """The flat captures stay authoritative, so the merge must work on copies throughout."""
    batches = _three_batches(tmp_path)
    before = [(b / grp.MANIFEST_NAME).read_text(encoding="utf-8") for b in batches]
    grp.run(batches, _migrations(tmp_path), dry_run=False)
    after = [(b / grp.MANIFEST_NAME).read_text(encoding="utf-8") for b in batches]
    assert before == after


# ---------------------------------------------------------------- review round 6: PROVENANCE, not
# merely freshness. Both findings below are fail-OPEN: the merged manifest reported itself entirely
# healthy while carrying evidence that did not belong to what it claimed to describe.


def test_batches_from_DIFFERENT_tenants_are_refused_rather_than_merged(tmp_path):
    """⚠️ Cross-tenant evidence mixing -- the more serious of the two, and it needed no exotic setup.

    Two sites sharing only a workbook CAPTION were folded into one manifest that declared tenant B's
    `server` and `site` at the top level while its views carried artifacts from tenant A **and**
    tenant B. `source_batch` named a directory, not a tenant, so nothing in the output said so.
    Captions like "Sales Dashboard" collide across customers as a matter of course.
    """
    tenant_a = _batch(
        tmp_path,
        "tenant-a",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-01T00:00:00Z")],
        captured_at="2026-08-01T00:00:00Z",
        server="https://tenant-a.online.tableau.com",
        site="alpha",
    )
    tenant_b = _batch(
        tmp_path,
        "tenant-b",
        [_view(OTHER, "Summary", data="ok", image="ok", captured_at="2026-08-20T00:00:00Z")],
        captured_at="2026-08-20T00:00:00Z",
        server="https://tenant-b.online.tableau.com",
        site="beta",
    )

    with pytest.raises(grp.IncompatibleBatchSources) as refusal:
        grp.merge_batches(grp.load_batches([tenant_a, tenant_b]))

    message = str(refusal.value)
    assert "tenant-a" in message and "tenant-b" in message, message
    assert "alpha" in message and "beta" in message, "the refusal must name the SITES too, not only the hosts"


def test_the_same_tenant_spelled_differently_is_still_one_tenant(tmp_path):
    """The positive control, and the reason the comparison normalizes at all.

    ⚠️ Without it, "refuse anything that is not byte-identical" would pass the test above while making
    the merge feature unusable: a trailing slash or a capitalised host is the same server, and
    refusing those would be a regression dressed as a security fix.
    """
    first = _batch(
        tmp_path,
        "one",
        [_view(LUID, "Summary", data="transient", image="ok", captured_at="2026-08-01T00:00:00Z")],
        captured_at="2026-08-01T00:00:00Z",
        server="https://Example.Online.Tableau.com/",
        site="Acme",
    )
    second = _batch(
        tmp_path,
        "two",
        [_view(LUID, "Summary", data="ok", image="transient", captured_at="2026-08-20T00:00:00Z")],
        captured_at="2026-08-20T00:00:00Z",
        server="https://example.online.tableau.com",
        site="acme",
    )

    merged, _roots, _basis = grp.merge_batches(grp.load_batches([first, second]))
    view = merged["views"][0]

    assert view["data"]["status"] == "ok" and view["data"]["source_batch"] == "two"
    assert view["image"]["status"] == "ok" and view["image"]["source_batch"] == "one", (
        "a case difference in the server URL blocked a legitimate promotion, which would make the "
        "multi-batch merge useless in practice"
    )


def test_a_manifest_that_records_NO_source_cannot_be_merged_with_one_that_does(tmp_path):
    """⚠️ Fail-closed on the ambiguous case, deliberately, because "unknown" is not "the same".

    An absent server/site cannot establish sameness, so it blocks -- and it blocks under its OWN
    exception type. `IncompatibleBatchSources` is a statement about the data ("these are two
    tenants"); this is a statement about our knowledge ("we cannot tell"), and a test that could only
    assert *something* refused would not notice the two being collapsed back together.
    """
    known = _batch(
        tmp_path,
        "known",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-01T00:00:00Z")],
        captured_at="2026-08-01T00:00:00Z",
    )
    anonymous = _batch(
        tmp_path,
        "anonymous",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-20T00:00:00Z")],
        captured_at="2026-08-20T00:00:00Z",
        server=None,
        site=None,
    )

    with pytest.raises(grp.UnestablishedBatchSource):
        grp.merge_batches(grp.load_batches([known, anonymous]))

    # ...but ONE such batch on its own is not ambiguous, and must still group.
    merged, _roots, _basis = grp.merge_batches(grp.load_batches([anonymous]))
    assert merged["views"][0]["data"]["status"] == "ok"


# --------------------------------------------------------- review round 3: "cannot establish" is a
# STATE, not a value. Each of the three blockers below reported exit 0 while doing the thing the
# guard beside it was supposed to prevent, and each closed a different way of collapsing an
# unassessable input into the clean bucket.


def test_TWO_anonymous_captures_do_not_merge_just_because_both_are_anonymous(tmp_path):
    """⚠️ Blocker 1: two UNKNOWN sources were treated as ONE source.

    `_source_identity` mapped a missing identity onto `("", "")` and the refusal counted DISTINCT
    identities, so two manifests that each recorded nothing produced one identity, the cardinality
    check saw no disagreement, and they merged. Measured before the fix: exit 0, `server`/`site` both
    `null`, and `anonymous-b`'s image promoted beside `anonymous-a`'s data -- sameness assumed from a
    shared absence, which is the single most common defect class in this repo's gates.

    The assertion is on the GUARD, by name: `UnestablishedBatchSource`, not merely "it raised".
    """
    first = _batch(
        tmp_path,
        "anonymous-a",
        [_view(LUID, "Daily Monitoring", data="ok", image="transient", captured_at="2026-08-17T20:17:00Z")],
        captured_at="2026-08-17T20:17:00Z",
        server=None,
        site=None,
    )
    second = _batch(
        tmp_path,
        "anonymous-b",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        captured_at="2026-08-18T14:46:00Z",
        server=None,
        site=None,
    )
    with pytest.raises(grp.UnestablishedBatchSource) as excinfo:
        grp.merge_batches(grp.load_batches([first, second]))
    assert "anonymous-a" in str(excinfo.value) and "anonymous-b" in str(excinfo.value)


def test_an_unestablished_source_is_NOT_reported_as_a_tenant_disagreement(tmp_path):
    """The two refusals must stay distinguishable, in both directions.

    A single broad "sources incompatible" type would let the fail-open collapse return unnoticed: the
    anonymous pair would raise the same thing the tenant pair does, and every test would still pass.
    """
    anonymous = _batch(
        tmp_path,
        "anonymous",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-01T00:00:00Z")],
        server=None,
        site=None,
    )
    other = _batch(
        tmp_path,
        "other",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-02T00:00:00Z")],
        server=None,
        site=None,
    )
    assert not issubclass(grp.UnestablishedBatchSource, grp.IncompatibleBatchSources)
    with pytest.raises(grp.UnestablishedBatchSource):
        grp.merge_batches(grp.load_batches([anonymous, other]))


def test_a_recorded_but_EMPTY_site_is_the_default_site_not_an_absence(tmp_path):
    """⚠️ The fail-CLOSED half, and it is not hypothetical: `""` is Tableau Server's Default site.

    `tableau_env` canonicalises `TABLEAU_SITE` to `""` precisely because "an empty site IS the
    documented Default site", so a guard that tested truthiness rather than PRESENCE would refuse
    every legitimate Default-site merge -- trading one fail-open for a fail-closed that makes the
    tool unusable on Tableau Server.
    """
    first = _batch(
        tmp_path,
        "default-a",
        [_view(LUID, "Summary", data="ok", image="transient", captured_at="2026-08-01T00:00:00Z")],
        captured_at="2026-08-01T00:00:00Z",
        site="",
    )
    second = _batch(
        tmp_path,
        "default-b",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-02T00:00:00Z")],
        captured_at="2026-08-02T00:00:00Z",
        site="",
    )
    merged, _roots, _basis = grp.merge_batches(grp.load_batches([first, second]))
    assert merged["views"][0]["image"]["source_batch"] == "default-b"


def test_a_single_anonymous_capture_still_groups(tmp_path):
    """One batch establishes nothing about sameness because it claims nothing. It must not be refused."""
    only = _batch(
        tmp_path / "_oracle",
        "anonymous",
        [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-18T14:46:00Z")],
        server=None,
        site=None,
    )
    migrations = _migrations(tmp_path)
    assert grp.run([only], migrations, dry_run=False) == 0
    assert _grouped(migrations)["views"][0]["image"]["status"] == "ok"


def test_an_older_render_of_a_DIFFERENT_revision_is_not_promoted_beside_newer_data(tmp_path):
    """⚠️ The stale-evidence blocker, reproduced exactly as the review measured it.

    Old batch: `image=ok`. Newer batch, a revision later: `data=ok, image=transient`. Before the fix
    the merged record took `updated_at` from the NEW revision, data from the new batch and the image
    from the OLD one, then reported `data_ok=1, image_ok=1, failed=0, render_unestablished=0` -- a
    picture of a workbook that no longer exists, credited as current evidence, with a digest and a
    timestamp asserting otherwise.
    """
    old = _batch(
        tmp_path,
        "old-revision",
        [
            _view(
                LUID,
                "Summary",
                data="transient",
                image="ok",
                captured_at="2026-08-01T00:00:00Z",
                updated_at="2026-07-01T00:00:00Z",
            )
        ],
        captured_at="2026-08-01T00:00:00Z",
    )
    new = _batch(
        tmp_path,
        "new-revision",
        [
            _view(
                LUID,
                "Summary",
                data="ok",
                image="transient",
                captured_at="2026-08-20T00:00:00Z",
                updated_at="2026-08-19T00:00:00Z",
            )
        ],
        captured_at="2026-08-20T00:00:00Z",
    )

    merged, _roots, _basis = grp.merge_batches(grp.load_batches([old, new]))
    view = merged["views"][0]

    assert view["updated_at"] == "2026-08-19T00:00:00Z", "identity still comes from the newest record"
    assert view["data"]["source_batch"] == "new-revision"
    assert view["image"]["status"] != "ok", (
        f"the older revision's render was promoted beside the newer revision's data: {view['image']!r}"
    )
    assert view["image"]["source_batch"] == "new-revision", (
        "the newer revision's own FAILURE must be kept, rather than replaced by an older success"
    )
    assert grp.render_unestablished(merged["views"], frozenset(merged["requested_renders"])) != [], (
        "the view reports an established render, so a fidelity defect on it would read as verified"
    )


def test_a_refused_stale_render_is_RECORDED_rather_than_silently_dropped(tmp_path):
    """Refusing to promote it must not also hide that it exists.

    "An older revision has a render for this view" is the fact that decides whether to re-capture. A
    silent absence turns a known gap into an unknown one, which is the same collapse this file's
    round-1 findings were about, arriving from the other direction.
    """
    old = _batch(
        tmp_path,
        "old-revision",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-01T00:00:00Z", updated_at="2026-07-01Z")],
        captured_at="2026-08-01T00:00:00Z",
    )
    new = _batch(
        tmp_path,
        "new-revision",
        [_view(LUID, "Summary", data="ok", image=None, captured_at="2026-08-20T00:00:00Z", updated_at="2026-08-19Z")],
        captured_at="2026-08-20T00:00:00Z",
    )

    merged, _roots, _basis = grp.merge_batches(grp.load_batches([old, new]))
    view = merged["views"][0]

    assert merged["merge_stale_candidates"] == [
        {
            "view_luid": LUID,
            "leg": "image",
            "batch": "old-revision",
            "captured_revision": "2026-07-01Z",
            "current_revision": "2026-08-19Z",
            "promoted": False,
        }
    ], merged["merge_stale_candidates"]
    assert view["image"]["status"] == grp.STALE_REVISION_STATUS, view["image"]
    assert view["image"]["recorded_status"] == "ok", "the older batch's own verdict is preserved, unpromoted"
    assert "path" not in view["image"], "a stale leg must not name an artifact a consumer would copy"


def test_a_view_whose_revision_is_UNKNOWN_does_not_promote_across_batches(tmp_path):
    """⚠️ Fail-closed again: a server that omits `updatedAt` gives us nothing to prove sameness with.

    This is a real shape -- `capture_tableau_oracle` writes `view.get("updatedAt")`, which is `None`
    when the listing omits it -- so the behaviour is pinned rather than left to be discovered. The
    cost is a promotion not made; the alternative cost is a stale render credited as current.
    """
    old = _batch(
        tmp_path,
        "old",
        [_view(LUID, "Summary", data="transient", image="ok", captured_at="2026-08-01T00:00:00Z", updated_at=None)],
        captured_at="2026-08-01T00:00:00Z",
    )
    new = _batch(
        tmp_path,
        "new",
        [_view(LUID, "Summary", data="ok", image="transient", captured_at="2026-08-20T00:00:00Z", updated_at=None)],
        captured_at="2026-08-20T00:00:00Z",
    )

    merged, _roots, _basis = grp.merge_batches(grp.load_batches([old, new]))
    view = merged["views"][0]

    assert view["data"]["status"] == "ok", "the newest batch's own legs are always its own to keep"
    assert view["image"]["status"] != "ok", (
        "an unknown revision was treated as a matching one, so 'we cannot tell' silently became "
        "'they are the same' -- the exact inference this gate exists to refuse"
    )
    assert merged["merge_stale_candidates"], "the refused candidate must still be reported"


def test_the_stale_refusal_is_warned_about_on_the_console(tmp_path, caplog):
    """A manifest field nobody reads is not a report. The operator has to be told to re-capture."""
    old = _batch(
        tmp_path,
        "old",
        [_view(LUID, "Summary", data="ok", image="ok", captured_at="2026-08-01T00:00:00Z", updated_at="2026-07-01Z")],
        captured_at="2026-08-01T00:00:00Z",
    )
    new = _batch(
        tmp_path,
        "new",
        [_view(LUID, "Summary", data="ok", image=None, captured_at="2026-08-20T00:00:00Z", updated_at="2026-08-19Z")],
        captured_at="2026-08-20T00:00:00Z",
    )

    with caplog.at_level("WARNING"):
        grp.run([old, new], _migrations(tmp_path), dry_run=False)

    assert "DIFFERENT revision" in caplog.text, caplog.text
    assert "old" in caplog.text and "2026-07-01Z" in caplog.text, caplog.text


# --------------------------------------------------------- review round 3, blocker 2: PROMOTION IS
# RECONCILED. Copying the selected artifacts is only half of it -- an artifact a previous run
# promoted and this one REFUSES stays physically on disk unless something removes it.


def _one_view_batch(root: Path, name: str, *, image: str, stamp: str, revision: str) -> Path:
    return _batch(
        root,
        name,
        [_view(LUID, "Daily Monitoring", data="ok", image=image, captured_at=stamp, updated_at=revision)],
        captured_at=stamp,
    )


def test_a_REFUSED_old_revision_artifact_does_not_stay_in_reference_images(tmp_path):
    """⚠️ The measured blocker, reproduced exactly: manifest right, directory wrong.

    Run 1 promotes an image. The workbook is then edited and run 2 sees `image: transient` for the new
    revision, so the cross-revision gate correctly refuses the old render -- the merged manifest says
    `transient` with no `path`. Measured before the fix: `file_after_refusal: true`. The old-revision
    PNG was still sitting in `reference/images/`, so any consumer that reads the DIRECTORY rather than
    the manifest got evidence the merge had explicitly rejected.
    """
    oracle = tmp_path / "_oracle"
    old = _one_view_batch(oracle, "old-revision", image="ok", stamp="2026-08-17T20:17:00Z", revision="2026-07-01Z")
    migrations = _migrations(tmp_path)
    image = migrations / "airborne-services" / "reference" / "images" / f"{LUID}.png"

    assert grp.run([old], migrations, dry_run=False) == 0
    assert image.is_file(), "fixture precondition: run 1 must actually promote the image"

    new = _one_view_batch(
        oracle, "new-revision", image="transient", stamp="2026-08-18T14:46:00Z", revision="2026-08-18T09:00Z"
    )
    exit_code = grp.run([old, new], migrations, dry_run=False)

    entry = _grouped(migrations)["views"][0]["image"]
    assert entry["status"] == "transient" and "path" not in entry, entry
    assert not image.is_file(), (
        "the manifest refused the stale render and the file stayed on disk anyway -- a consumer "
        "listing reference/images/ gets refused evidence"
    )
    assert exit_code == 1, "a refused cross-revision leg must reach the exit code (finding 4)"


def test_reconciliation_removes_only_files_a_PREVIOUS_grouping_named(tmp_path):
    """⚠️ Removal is by ATTRIBUTION. `reference/images/` is our tree, but not everything in it is ours.

    A hand-dropped file no grouped manifest names must NOT be deleted -- silently removing somebody
    else's reference would be a worse failure than the one being fixed -- and must not be accepted
    either, because the folder then holds bytes the manifest does not account for.
    """
    oracle = tmp_path / "_oracle"
    old = _one_view_batch(oracle, "old-revision", image="ok", stamp="2026-08-17T20:17:00Z", revision="2026-07-01Z")
    migrations = _migrations(tmp_path)
    grp.run([old], migrations, dry_run=False)

    reference = migrations / "airborne-services" / "reference"
    foreign = reference / "images" / "hand-dropped.png"
    foreign.write_bytes(PNG)

    new = _one_view_batch(
        oracle, "new-revision", image="transient", stamp="2026-08-18T14:46:00Z", revision="2026-08-18T09:00Z"
    )
    exit_code = grp.run([old, new], migrations, dry_run=False)

    assert foreign.is_file(), "a file we cannot attribute to ourselves must not be deleted"
    assert not (reference / "images" / f"{LUID}.png").is_file(), "our own refused artifact must still go"
    report = json.loads((new / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert [r["refusal"] for r in report["unreconciled"]] == [grp.REFUSAL_UNATTRIBUTED], report["unreconciled"]
    assert report["unreconciled"][0]["unattributed"] == ["images/hand-dropped.png"]
    assert exit_code == 1


def test_a_dry_run_reconciles_NOTHING(tmp_path):
    """`--dry-run` writes nothing, and deleting is writing. Reporting the removal is fine; doing it is not."""
    oracle = tmp_path / "_oracle"
    old = _one_view_batch(oracle, "old-revision", image="ok", stamp="2026-08-17T20:17:00Z", revision="2026-07-01Z")
    migrations = _migrations(tmp_path)
    grp.run([old], migrations, dry_run=False)
    image = migrations / "airborne-services" / "reference" / "images" / f"{LUID}.png"

    new = _one_view_batch(
        oracle, "new-revision", image="transient", stamp="2026-08-18T14:46:00Z", revision="2026-08-18T09:00Z"
    )
    grp.run([old, new], migrations, dry_run=True)

    assert image.is_file(), "a dry run deleted a file"


def test_an_UNCHANGED_re_run_removes_nothing(tmp_path):
    """Reconciliation must be a no-op when the merge promotes what is already there.

    The negative control for the removal: a rule that deleted on every run would still pass the
    blocker test above while destroying a good reference folder on the next re-group.
    """
    oracle = tmp_path / "_oracle"
    only = _one_view_batch(oracle, "capture", image="ok", stamp="2026-08-17T20:17:00Z", revision="2026-07-01Z")
    migrations = _migrations(tmp_path)
    assert grp.run([only], migrations, dry_run=False) == 0
    image = migrations / "airborne-services" / "reference" / "images" / f"{LUID}.png"
    assert image.is_file()

    assert grp.run([only], migrations, dry_run=False) == 0
    assert image.is_file(), "a re-run that promotes the same artifact deleted it"
    report = json.loads((only / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert report["grouped"][0]["removed"] == []


# --------------------------------------------------------- review round 3, blocker 3: WORKBOOKS ARE
# KEYED BY LUID. Two DIFFERENT source workbooks whose names normalize identically both targeted one
# destination folder; the last manifest overwrote the first and both workbooks' files remained.

RND_AMP = "wb-rnd-amp"
RND_PLUS = "wb-rnd-plus"


def _collision_batch(root: Path) -> Path:
    """One capture holding two DISTINCT workbooks whose display names normalize onto one key.

    Not invented: on the real 48-workbook reference estate, `Seed - R&D`, `Seed - R+D` and
    `Seed - R/D` are three distinct workbook LUIDs and ONE normalized key -- which is why the estate
    has 48 LUIDs and 46 name keys.
    """
    first = _view("aaaaaaaa-1111-2222-3333-444444444444", "Sheet A", data="ok", image="ok", captured_at=STAMP)
    second = _view("bbbbbbbb-1111-2222-3333-444444444444", "Sheet B", data="ok", image="ok", captured_at=STAMP)
    first["workbook_name"], first["workbook_luid"] = "Seed - R&D", RND_AMP
    second["workbook_name"], second["workbook_luid"] = "Seed - R+D", RND_PLUS
    return _batch(root, "one-capture", [first, second], captured_at=STAMP)


def test_two_DIFFERENT_workbooks_normalizing_onto_one_folder_are_both_refused(tmp_path):
    """⚠️ The measured blocker: exit 0, one manifest, two workbooks' images, one view reported.

    Destination AMBIGUITY (one name, two folders) was detected; the source side was not. `_group_all`
    wrote them in sequence, the second manifest overwrote the first, and `seed-rd/reference/images/`
    ended up holding two files while the surviving manifest named one -- one workbook's evidence
    silently attributed to another, with nothing in the output saying so.
    """
    capture = _collision_batch(tmp_path / "_oracle")
    migrations = tmp_path / "migrations" / "workbooks"
    (migrations / "seed-rd").mkdir(parents=True)

    exit_code = grp.run([capture], migrations, dry_run=False)

    reference = migrations / "seed-rd" / "reference"
    assert exit_code == 1
    assert not (reference / grp.MANIFEST_NAME).is_file(), "neither side of a collision may be written"
    assert not list((reference / "images").glob("*")) if (reference / "images").is_dir() else True

    report = json.loads((capture / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert report["workbooks_collision"] == 2, report
    assert {r["refusal"] for r in report["collision"]} == {grp.REFUSAL_DESTINATION_COLLISION}
    assert sorted(report["collision"][0]["colliding_workbook_luids"]) == sorted([RND_AMP, RND_PLUS])


def test_the_collision_refusal_is_not_the_ambiguous_destination_one(tmp_path):
    """Two guards, two names. A test asserting only "exit 1" cannot tell them apart -- and this file
    now has five fail-closed guards that would all satisfy a bare `!= 0`."""
    capture = _collision_batch(tmp_path / "_oracle")
    migrations = tmp_path / "migrations" / "workbooks"
    (migrations / "seed-rd").mkdir(parents=True)
    grp.run([capture], migrations, dry_run=False)
    report = json.loads((capture / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert report["workbooks_ambiguous"] == 0 and report["workbooks_unmatched"] == 0, report


def test_two_workbooks_with_their_OWN_folders_still_group(tmp_path):
    """The negative control. A guard that refused any two workbooks sharing a normalized prefix -- or
    simply refused pairs -- would pass the collision test and break every ordinary estate."""
    capture = _collision_batch(tmp_path / "_oracle")
    migrations = tmp_path / "migrations" / "workbooks"
    (migrations / "seed-rd").mkdir(parents=True)
    (migrations / "seed-rplusd").mkdir(parents=True)
    # Rename one workbook so the two names no longer normalize onto one key.
    manifest = json.loads((capture / grp.MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["views"][1]["workbook_name"] = "Seed - RplusD"
    (capture / grp.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert grp.run([capture], migrations, dry_run=False) == 0
    assert (migrations / "seed-rd" / "reference" / grp.MANIFEST_NAME).is_file()
    assert (migrations / "seed-rplusd" / "reference" / grp.MANIFEST_NAME).is_file()


def test_a_view_with_NO_workbook_luid_is_refused_rather_than_bucketed_by_name(tmp_path):
    """ "Which workbook is this?" has no name-shaped answer. A display name is not an identity."""
    orphan = _view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at=STAMP)
    orphan["workbook_luid"] = None
    capture = _batch(tmp_path / "_oracle", "capture", [orphan], captured_at=STAMP)
    migrations = _migrations(tmp_path)

    assert grp.run([capture], migrations, dry_run=False) == 1
    report = json.loads((capture / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert [r["refusal"] for r in report["unidentified"]] == [grp.REFUSAL_NO_LUID], report
    assert not (migrations / "airborne-services" / "reference" / grp.MANIFEST_NAME).is_file()


def test_two_workbooks_sharing_an_EXACT_name_are_kept_apart_by_luid(tmp_path):
    """Normalization is not the only way names collide -- two workbooks can simply share one.

    Bucketing by name merged them into a single manifest with no collision detectable at all; keying
    by LUID turns the same input into a reported destination collision.
    """
    first = _view("aaaaaaaa-1111-2222-3333-444444444444", "Sheet A", data="ok", image="ok", captured_at=STAMP)
    second = _view("bbbbbbbb-1111-2222-3333-444444444444", "Sheet B", data="ok", image="ok", captured_at=STAMP)
    first["workbook_luid"], second["workbook_luid"] = "wb-one", "wb-two"
    capture = _batch(tmp_path / "_oracle", "capture", [first, second], captured_at=STAMP)
    migrations = _migrations(tmp_path)

    assert grp.run([capture], migrations, dry_run=False) == 1
    report = json.loads((capture / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert report["workbooks_collision"] == 2, report


def test_a_workbook_RENAMED_between_batches_is_reported_not_guessed(tmp_path):
    """One LUID, two normalized names: which folder is the destination is unanswerable.

    ⚠️ It takes two DIFFERENT views to reach this, and that is not a fixture convenience -- it is what
    the shape actually is. A merged record for ONE view takes its identity from the newest batch, so a
    rename seen twice for the same view leaves no disagreement behind. The disagreement survives only
    across views, which is exactly the partial-retry pattern: batch 1 captured `Daily Monitoring`
    while the workbook was called one thing, batch 2 captured a different view after the rename.
    """
    early = _view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at="2026-08-01T00:00:00Z")
    late = _view(OTHER, "Availability Summary", data="ok", image="ok", captured_at="2026-08-02T00:00:00Z")
    early["workbook_name"], late["workbook_name"] = "airborne services", "airborne services EMEA"
    first = _batch(tmp_path / "_oracle", "first", [early], captured_at="2026-08-01T00:00:00Z")
    second = _batch(tmp_path / "_oracle", "second", [late], captured_at="2026-08-02T00:00:00Z")
    migrations = _migrations(tmp_path)

    assert grp.run([first, second], migrations, dry_run=False) == 1
    report = json.loads((second / grp.UNMATCHED_REPORT).read_text(encoding="utf-8"))
    assert [r["refusal"] for r in report["ambiguous"]] == [grp.REFUSAL_NAME_AMBIGUOUS], report
    assert not (migrations / "airborne-services" / "reference" / grp.MANIFEST_NAME).is_file(), (
        "half a workbook's views were grouped under a name the other half disputes"
    )


# --------------------------------------------------------- review round 3, blocker 5: EVERY BATCH ON
# DISK. Reading only the directories somebody typed moves the boundary from one batch to "every
# argument the operator remembered" -- narrower, and invisible in the output.


def test_a_batch_on_disk_that_was_not_passed_is_REFUSED_not_skipped(tmp_path):
    """⚠️ The measured shape: `retry2` present, not listed, and its good render never read.

    Before: exit 0, `image: transient` from `airborne-services-retry`, `batches` listing the two that
    were given -- and the PNG that would have answered the question sitting unopened on disk.
    """
    batches = _three_batches(tmp_path)
    with pytest.raises(grp.UnlistedBatchOnDisk) as excinfo:
        grp.run(batches[:2], _migrations(tmp_path), dry_run=False)
    assert "airborne-services-retry2" in str(excinfo.value)


def test_oracle_root_DISCOVERS_the_batch_nobody_listed(tmp_path):
    """AC#3 as implemented: a defined root, a defined batch shape, and an UNLISTED batch found there.

    Nothing is passed at all -- the root is the only argument -- and the promoted image comes from the
    third retry, which is the batch the listing mode could not see.
    """
    _three_batches(tmp_path)
    migrations = _migrations(tmp_path)

    assert grp.run(None, migrations, dry_run=False, oracle_root=tmp_path / "_oracle") == 0

    grouped = _grouped(migrations)
    assert grouped["views"][0]["image"]["status"] == "ok"
    assert grouped["views"][0]["image"]["source_batch"] == "airborne-services-retry2"
    assert set(grouped["batches"]) == {"airborne-services", "airborne-services-retry", "airborne-services-retry2"}


def test_a_root_that_is_ITSELF_a_capture_is_the_ordinary_layout(tmp_path):
    """`_oracle/` with `images/`, `data/` and a manifest is one batch. Its own subdirs are structure."""
    oracle = tmp_path / "_oracle"
    _batch(oracle.parent, oracle.name, [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at=STAMP)])
    migrations = _migrations(tmp_path)

    assert grp.run(None, migrations, dry_run=False, oracle_root=oracle) == 0
    assert _grouped(migrations)["views"][0]["image"]["status"] == "ok"


def test_a_directory_under_the_root_that_is_NOT_a_batch_blocks(tmp_path):
    """⚠️ Otherwise the boundary moves a third time -- to "every directory I happened to recognise"."""
    _three_batches(tmp_path)
    (tmp_path / "_oracle" / "half-written-capture").mkdir()
    with pytest.raises(grp.UnclassifiedCaptureDirectory) as excinfo:
        grp.run(None, _migrations(tmp_path), dry_run=False, oracle_root=tmp_path / "_oracle")
    assert "half-written-capture" in str(excinfo.value)


def test_a_GROUPED_manifest_is_not_mistaken_for_a_capture_batch(tmp_path):
    """`reference/` carries a file with the same NAME. Accepting it would feed output back into input."""
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / grp.MANIFEST_NAME).write_text(json.dumps({"schema": "tableau-oracle-workbook/1"}), encoding="utf-8")
    assert not grp.is_capture_batch(reference)


def test_exclude_is_the_ONE_auditable_escape_and_is_recorded(tmp_path):
    """An exclusion nothing records is indistinguishable from an omission."""
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)

    assert grp.run(batches[:2], migrations, dry_run=False, exclude=[batches[2]]) == 0

    grouped = _grouped(migrations)
    assert grouped["views"][0]["image"]["status"] != "ok", "the excluded batch's render must not be promoted"
    assert [Path(p).name for p in grouped["excluded_paths"]] == ["airborne-services-retry2"]


def test_a_non_batch_sibling_does_not_block_the_LISTED_mode(tmp_path):
    """Naming a batch does not declare its parent a tree of captures.

    `_runs/<run>/oracle` sits beside `assessment/`, `bundle/`, `scratch/` -- none of them captures.
    Refusing those would make the listed mode unusable on the layout this repo actually writes.
    """
    oracle = tmp_path / "_oracle"
    only = _batch(oracle, "capture", [_view(LUID, "Daily Monitoring", data="ok", image="ok", captured_at=STAMP)])
    (oracle / "bundle").mkdir()
    (oracle / "scratch").mkdir()

    assert grp.run([only], _migrations(tmp_path), dry_run=False) == 0


def test_the_source_flags_are_mutually_exclusive_and_one_is_required():
    parser = grp.build_parser()
    args = parser.parse_args(["--oracle-root", "_oracle", "--exclude", "x", "--exclude", "y"])
    assert args.oracle_root.name == "_oracle" and [p.name for p in args.exclude] == ["x", "y"]
    with pytest.raises(SystemExit):
        parser.parse_args(["--oracle", "a", "--oracle-root", "b"])
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_an_empty_discovery_root_says_so_rather_than_grouping_nothing(tmp_path):
    root = tmp_path / "_oracle"
    root.mkdir()
    with pytest.raises(FileNotFoundError) as excinfo:
        grp.run(None, _migrations(tmp_path), dry_run=False, oracle_root=root)
    assert "no capture batch" in str(excinfo.value)


def test_every_refusal_reaches_exit_2_through_main(tmp_path, monkeypatch, capsys):
    """The guards are only worth what the CLI does with them. Exit code, never printed text."""
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)
    argv = ["group", "--oracle", str(batches[0]), "--oracle", str(batches[1]), "--migrations", str(migrations)]
    monkeypatch.setattr(sys, "argv", argv)
    assert grp.main() == 2
    capsys.readouterr()
