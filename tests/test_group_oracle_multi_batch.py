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

LUID = "0979a4f9-1111-2222-3333-444444444444"
OTHER = "0979a4f9-5555-6666-7777-888888888888"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
CSV = "region,sales\nEast,12\n"


def _view(luid: str, name: str, *, data: str, image: str | None, captured_at: str) -> dict:
    """One view record, shaped exactly as `capture_tableau_oracle` writes it."""
    view: dict = {
        "view_luid": luid,
        "view_name": name,
        "workbook_luid": "wb-1",
        "workbook_name": "airborne services",
        "captured_at": captured_at,
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


def _batch(root: Path, name: str, views: list[dict], *, captured_at: str | None = "2026-08-18T14:46:00Z") -> Path:
    """Write one `_oracle/<batch>/` directory, materialising only the artifacts its views claim."""
    directory = root / name
    (directory / "data").mkdir(parents=True, exist_ok=True)
    (directory / "images").mkdir(parents=True, exist_ok=True)
    for view in views:
        if (view.get("data") or {}).get("status") == "ok":
            (directory / "data" / f"{view['view_luid']}.csv").write_text(CSV, encoding="utf-8")
        if (view.get("image") or {}).get("status") == "ok":
            (directory / "images" / f"{view['view_luid']}.png").write_bytes(PNG)
    manifest: dict = {"schema": "tableau-oracle/1", "requested_renders": ["png"], "views": views}
    if captured_at is not None:
        manifest["captured_at"] = captured_at
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
    """The customer's actual sequence: two failures, then a batch where both legs succeeded."""
    oracle = tmp_path / "_oracle"
    return [
        _batch(
            oracle,
            "airborne-services",
            [_view(LUID, "Daily Monitoring", data="transient", image="transient", captured_at="2026-08-17T20:17:00Z")],
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


# --------------------------------------------------------------------- the stranded artifact


def test_a_later_batch_that_finally_succeeded_is_promoted(tmp_path):
    """⚠️ THE anchor of this file. Reverting the merge to read only one batch must fail it."""
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)

    assert grp.run(batches, migrations, dry_run=False) == 0

    grouped = _grouped(migrations)
    view = grouped["views"][0]
    assert view["image"]["status"] == "ok"
    assert view["data"]["status"] == "ok"
    assert (migrations / "airborne-services" / "reference" / "images" / f"{LUID}.png").read_bytes() == PNG


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


# ------------------------------------------------------------------------------- CLI and compatibility


def test_the_oracle_flag_is_repeatable():
    args = grp.build_parser().parse_args(["--oracle", "a", "--oracle", "b", "--oracle", "c"])
    assert [p.name for p in args.oracle] == ["a", "b", "c"]


def test_a_single_path_still_works_unchanged(tmp_path):
    """Every existing caller passes one `Path`. Breaking that to add a list would be a migration this
    change does not need -- and `main()` always hands over a list, so both shapes are exercised."""
    batches = _three_batches(tmp_path)
    migrations = _migrations(tmp_path)
    assert grp.run(batches[2], migrations, dry_run=False) == 0
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
