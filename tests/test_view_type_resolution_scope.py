"""`view_type_resolution` must survive every hop between the capture and a consumer (#560, round 1).

The capture records HOW the one site-wide Metadata call that types every view turned out: whether it
lost its session and recovered (`reauths`), and whether typing stayed unavailable at all
(`unavailable_reason`). Two consumers then re-write that manifest, and both DROPPED the record:

* `group_oracle_by_workbook.subset_manifest` - a per-workbook manifest asserted a `view_types`
  census with nothing saying it had been arrived at through a 401 -> reauth -> 200;
* `manifest_scope.ORACLE_MANIFEST_ALLOW` - the packaged manifest a customer receives, the same way.

And the merge had a second, quieter version of the same defect: `merge_batches` takes its non-view
fields from the NEWEST batch, so a clean re-run OVERWROTE an earlier batch's recovery.

Every test here is one of the seven controls that correction was accepted against, plus the two that
give the vocabulary check its power: an unauthored reason must be refused (or the check proves
nothing), and a refusal must not echo what it refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import group_oracle_by_workbook as grp  # noqa: E402  # pylint: disable=wrong-import-position
import manifest_scope as scope  # noqa: E402  # pylint: disable=wrong-import-position
import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_view_types as vt  # noqa: E402  # pylint: disable=wrong-import-position

LUID = "0979a4f9-1111-2222-3333-444444444444"
CSV = "region,sales\nEast,12\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
REVISION = "2026-07-01T00:00:00Z"
#: A reason `tableau_view_types` really does author, on the path #560 is about.
PERSISTENT = "metadata api returned HTTP 401; re-authentication failed: RuntimeError"
#: What a reflecting proxy put into that field in the measured incident: a live session token.
SECRET = "0aBcD3fGhIjKlMnOpQrStUvWxYz012345678=="


def _view(*, captured_at: str = "2026-08-18T14:46:00Z") -> dict:
    return {
        "view_luid": LUID,
        "view_name": "Detail",
        "workbook_luid": "wb-1",
        "workbook_name": "airborne services",
        "captured_at": captured_at,
        "updated_at": REVISION,
        "data": {
            "status": "ok",
            "certification": "certified",
            "path": f"data/{LUID}.csv",
            "row_count": 1,
            "bytes": len(CSV),
        },
        "image": {"status": "ok", "format": "png", "path": f"images/{LUID}.png", "bytes": len(PNG)},
    }


def _batch(root: Path, name: str, resolution: object, *, captured_at: str = "2026-08-18T14:46:00Z") -> Path:
    """One capture batch on disk, carrying `resolution` verbatim as its `view_type_resolution`.

    `resolution` is deliberately typed `object`: half of these controls drive a value the capture
    would never write, because "a record we cannot read" is one of the states that must not become
    clean evidence.
    """
    directory = root / name
    (directory / "data").mkdir(parents=True, exist_ok=True)
    (directory / "images").mkdir(parents=True, exist_ok=True)
    (directory / "data" / f"{LUID}.csv").write_text(CSV, encoding="utf-8")
    (directory / "images" / f"{LUID}.png").write_bytes(PNG)
    manifest: dict = {
        "schema": "tableau-oracle/1",
        "captured_at": captured_at,
        "server": "https://example.online.tableau.com",
        "site": "acme",
        "requested_renders": ["png"],
        "view_types": {"dashboard": 1, "worksheet": 0, "unknown": 0},
        "views": [_view(captured_at=captured_at)],
    }
    if resolution is not _ABSENT:
        manifest["view_type_resolution"] = resolution
    (directory / grp.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return directory


_ABSENT = object()


def _grouped(tmp_path: Path, *batches: Path) -> dict:
    """Group the given batches and return the per-workbook manifest that was written."""
    root = tmp_path / "migrations" / "workbooks"
    (root / "airborne-services").mkdir(parents=True)
    grp.run(list(batches), root, dry_run=False)
    written = root / "airborne-services" / "reference" / grp.MANIFEST_NAME
    assert written.is_file(), "the grouping wrote no per-workbook manifest"
    return json.loads(written.read_text(encoding="utf-8"))


def _packaged(resolution: object, by_batch: object = _ABSENT) -> dict:
    """The manifest a PACKAGE would ship, through the real packaging seam (no filesystem needed)."""
    manifest: dict = {"schema": "tableau-oracle/1", "captured_at": "2026-08-18T14:46:00Z", "views": []}
    if resolution is not _ABSENT:
        manifest["view_type_resolution"] = resolution
    if by_batch is not _ABSENT:
        manifest["view_type_resolution_by_batch"] = by_batch
    return pkg._scope_oracle_manifest(manifest, [], [], "Airborne_Services")  # pylint: disable=protected-access


# --------------------------------------------------------------------- control 1: a recovery survives


def test_a_single_recovered_capture_survives_grouping_exactly(tmp_path: Path) -> None:
    """The whole point of #560: `reauths: 1` is the only evidence the run healed itself."""
    grouped = _grouped(tmp_path, _batch(tmp_path / "_oracle", "b1", {"reauths": 1, "unavailable_reason": None}))
    assert grouped["view_type_resolution"] == {"reauths": 1, "unavailable_reason": None}


def test_the_packaged_manifest_keeps_the_recovery_too(tmp_path: Path) -> None:
    """Both consumers dropped it, so both are pinned. `tmp_path` unused - the seam needs no disk."""
    assert _packaged({"reauths": 1, "unavailable_reason": None})["view_type_resolution"] == {
        "reauths": 1,
        "unavailable_reason": None,
    }


# ------------------------------------------------- control 2: a clean run stays distinguishable


def test_a_clean_one_shot_run_stays_distinguishable_from_a_recovered_one(tmp_path: Path) -> None:
    clean = _grouped(tmp_path / "clean", _batch(tmp_path / "clean" / "_oracle", "b1", {"reauths": 0}))
    recovered = _grouped(tmp_path / "hurt", _batch(tmp_path / "hurt" / "_oracle", "b1", {"reauths": 1}))
    assert clean["view_type_resolution"] == {"reauths": 0, "unavailable_reason": None}
    assert clean["view_type_resolution"] != recovered["view_type_resolution"]


def test_a_capture_that_never_resolved_typing_is_not_reported_as_clean(tmp_path: Path) -> None:
    """`null` and an absent key both mean "not resolved" - neither may read as `reauths: 0`."""
    explicit = _grouped(tmp_path / "null", _batch(tmp_path / "null" / "_oracle", "b1", None))
    absent = _grouped(tmp_path / "gone", _batch(tmp_path / "gone" / "_oracle", "b1", _ABSENT))
    assert explicit.get("view_type_resolution") is None
    assert absent.get("view_type_resolution") is None


# ------------------------------------------- control 3: a persistent unavailable reason survives


def test_a_persistent_unavailable_reason_survives_grouping_and_packaging(tmp_path: Path) -> None:
    record = {"reauths": 0, "unavailable_reason": PERSISTENT}
    grouped = _grouped(tmp_path, _batch(tmp_path / "_oracle", "b1", record))
    assert grouped["view_type_resolution"] == record
    assert _packaged(record)["view_type_resolution"] == record


# --------------------------------------------------- control 4: two batches, neither one erased


def test_two_merged_batches_preserve_BOTH_recoveries_rather_than_the_newest(tmp_path: Path) -> None:
    """Last-wins would report the newest batch's `reauths: 0` over the older batch's recovery."""
    oracle = tmp_path / "_oracle"
    older = _batch(oracle, "b1", {"reauths": 1, "unavailable_reason": None}, captured_at="2026-08-18T10:00:00Z")
    newer = _batch(oracle, "b2", {"reauths": 0, "unavailable_reason": PERSISTENT}, captured_at="2026-08-18T14:00:00Z")
    grouped = _grouped(tmp_path, older, newer)

    aggregate = grouped["view_type_resolution"]
    assert aggregate["reauths"] == 1, "the older batch's recovery was erased by the newer batch"
    assert aggregate["unavailable_reason"], "the newer batch's failure was erased by the aggregate"

    rows = {row["batch"]: row for row in grouped["view_type_resolution_by_batch"]}
    assert len(rows) == 2
    assert [row["reauths"] for row in rows.values()].count(1) == 1
    assert [row["unavailable_reason"] for row in rows.values()].count(PERSISTENT) == 1


def test_the_merged_aggregate_is_clean_only_when_every_batch_was(tmp_path: Path) -> None:
    """The positive control for the one above: nothing to report means no reason is invented."""
    oracle = tmp_path / "_oracle"
    first = _batch(oracle, "b1", {"reauths": 0}, captured_at="2026-08-18T10:00:00Z")
    second = _batch(oracle, "b2", {"reauths": 0}, captured_at="2026-08-18T14:00:00Z")
    grouped = _grouped(tmp_path, first, second)
    assert grouped["view_type_resolution"] == {"reauths": 0, "unavailable_reason": None}


def test_a_batch_with_no_record_is_named_rather_than_treated_as_clean() -> None:
    aggregate, rows = scope.merged_view_type_resolution(
        [("b2", None), ("b1", {"reauths": 1, "unavailable_reason": None})]
    )
    assert aggregate["reauths"] == 1
    assert [row["record"] for row in rows] == [scope.RESOLUTION_ABSENT, scope.RESOLUTION_RESOLVED]


# ------------------------------------- control 5: the package allowlist keeps it, and only it


def test_package_scoping_keeps_the_two_typed_fields_and_drops_unknown_nested_keys() -> None:
    scoped = _packaged({"reauths": 1, "unavailable_reason": None, "future_nested": {"host": "C:\\Users\\someone"}})
    assert scoped["view_type_resolution"] == {"reauths": 1, "unavailable_reason": None}
    assert "view_type_resolution.future_nested" in scoped["scope"]["dropped_fields"]
    assert "someone" not in json.dumps(scoped, ensure_ascii=False)


def test_package_scoping_keeps_the_per_batch_rows_and_drops_unknown_row_keys() -> None:
    scoped = _packaged(
        {"reauths": 1, "unavailable_reason": None},
        [{"batch": "b1", "record": "resolved", "reauths": 1, "unavailable_reason": None, "future": "x"}],
    )
    assert scoped["view_type_resolution_by_batch"] == [
        {"batch": "b1", "record": "resolved", "reauths": 1, "unavailable_reason": None}
    ]
    assert "view_type_resolution_by_batch[].future" in scoped["scope"]["dropped_fields"]


def test_a_foreign_shaped_by_batch_value_is_refused_rather_than_shipped() -> None:
    scoped = _packaged({"reauths": 0}, {"not": "a list"})
    assert scoped["view_type_resolution_by_batch"] == []


# ------------------------------- control 6: a malformed record never becomes clean evidence


@pytest.mark.parametrize("malformed", [[], "recovered", 1, True, {"reauths": "1"}, {"reauths": -3}])
def test_a_malformed_record_never_becomes_clean_recovery_evidence(tmp_path: Path, malformed: object) -> None:
    grouped = _grouped(tmp_path, _batch(tmp_path / "_oracle", "b1", malformed))
    record = grouped["view_type_resolution"]
    assert record["reauths"] is None, "an unreadable count must not read as 'no recovery happened'"
    assert record != {"reauths": 0, "unavailable_reason": None}
    packaged = _packaged(malformed)["view_type_resolution"]
    assert packaged["reauths"] is None


def test_an_unreadable_record_says_so_without_echoing_what_it_refused() -> None:
    record, refused = scope.scope_view_type_resolution({"unavailable_reason": SECRET, "reauths": SECRET})
    assert SECRET not in json.dumps(record)
    assert record["unavailable_reason"] == scope.REASON_REFUSED
    assert record["reauths"] is None
    assert refused == ["view_type_resolution.reauths", "view_type_resolution.unavailable_reason"]


# --------------------------------- control 7: a reflected secret reaches neither manifest


def test_a_reflected_secret_in_the_reason_reaches_neither_manifest(tmp_path: Path) -> None:
    """Measured in #560's own module docstring: a proxy reflected `X-Tableau-Auth` into a message."""
    record = {"reauths": 1, "unavailable_reason": f"metadata api returned HTTP 401: {SECRET}"}
    grouped = _grouped(tmp_path, _batch(tmp_path / "_oracle", "b1", record))
    assert SECRET not in json.dumps(grouped, ensure_ascii=False)
    assert grouped["view_type_resolution"]["unavailable_reason"] == scope.REASON_REFUSED
    assert grouped["view_type_resolution"]["reauths"] == 1, "the FACT of the recovery still survives"
    assert SECRET not in json.dumps(_packaged(record), ensure_ascii=False)


def test_a_reflected_secret_in_a_per_batch_row_reaches_neither_manifest(tmp_path: Path) -> None:
    oracle = tmp_path / "_oracle"
    older = _batch(
        oracle,
        "b1",
        {"reauths": 0, "unavailable_reason": f"metadata api call failed: {SECRET}"},
        captured_at="2026-08-18T10:00:00Z",
    )
    newer = _batch(oracle, "b2", {"reauths": 0}, captured_at="2026-08-18T14:00:00Z")
    grouped = _grouped(tmp_path, older, newer)
    assert SECRET not in json.dumps(grouped, ensure_ascii=False)
    assert grouped["view_type_resolution"]["unavailable_reason"], "the failure itself must still be reported"


# ------------------------------------------------ the vocabulary: power, and drift protection


@pytest.mark.parametrize(
    "reason",
    [
        "metadata api returned HTTP 401",
        "metadata api returned HTTP 401; re-authentication failed: RuntimeError",
        "metadata api call failed: URLError",
        "metadata api response was not usable JSON: RecursionError",
        "metadata api returned 2 graphql error(s); response refused",
        "metadata api returned no dashboards or sheets carrying a luid",
        "the same luid was reported as both a dashboard and a worksheet; response refused",
    ],
)
def test_the_vocabulary_accepts_what_this_repository_authors(reason: str) -> None:
    assert scope.ships_reason(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "metadata api returned HTTP 401: token=abc",
        "metadata api returned HTTP 401 " + SECRET,
        "Contact alice@example.com about the Northwind Q3 pipeline",
        "",
        None,
        123,
    ],
)
def test_the_vocabulary_refuses_everything_else(reason: object) -> None:
    """The negative control. Without this the check could accept everything and look identical."""
    assert not scope.ships_reason(reason)


def test_every_reason_tableau_view_types_can_author_is_shippable() -> None:
    """The drift guard: a NEW producer reason must fail here rather than ship unchecked.

    Driven through the module's own public seams, not copied from its source, so this fails when the
    producer's wording changes - which is the only way a value-shaped allowlist can stay honest
    without a heuristic.
    """

    class _Session:  # pylint: disable=too-few-public-methods
        def __init__(self, status: int, body: bytes) -> None:
            self._answer = (status, body, {})

        def _request(self, *_args, **_kwargs):
            return self._answer

        def sign_in(self):
            raise RuntimeError("no")

    payloads = [
        None,
        [],
        "text",
        {"errors": [{"message": "boom"}]},
        {"errors": {}},
        {"data": None},
        {"data": {"workbooks": "no"}},
        {"data": {"workbooks": []}},
        {"data": {"workbooks": ["no"]}},
        {"data": {"workbooks": [{"dashboards": []}]}},
        {"data": {"workbooks": [{"dashboards": "no", "sheets": []}]}},
        {"data": {"workbooks": [{"dashboards": ["no"], "sheets": []}]}},
        {"data": {"workbooks": [{"dashboards": [{"luid": 7}], "sheets": []}]}},
        {"data": {"workbooks": [{"dashboards": [{"luid": "nope"}], "sheets": []}]}},
        {"data": {"workbooks": [{"dashboards": [{"luid": LUID}], "sheets": [{"luid": LUID}]}]}},
    ]
    reasons = [reason for _mapping, reason in map(vt.parse_payload, payloads) if reason]
    for status, body in ((404, b"{}"), (200, b"not json"), (401, b"{}")):
        _payload, reason, _reauths = vt._view_types_recovering(  # pylint: disable=protected-access
            _Session(status, body)
        )
        if reason:
            reasons.append(reason)
    assert len(reasons) >= 15, "the drift guard stopped exercising the producer's failure paths"
    unshippable = [reason for reason in reasons if not scope.ships_reason(reason)]
    assert not unshippable, f"tableau_view_types authors a reason manifest_scope would refuse: {unshippable}"


def test_the_rule_is_idempotent_across_the_two_hops() -> None:
    """A grouped manifest is re-read by the packager; evidence must not decay one hop at a time."""
    for value in (
        {"reauths": 1, "unavailable_reason": PERSISTENT},
        [],
        {"unavailable_reason": SECRET},
        {"reauths": "1"},
        {"reauths": 2},
    ):
        once, _ = scope.scope_view_type_resolution(value)
        twice, _ = scope.scope_view_type_resolution(once)
        assert once == twice


# =====================================================================================
# The correction round. Two findings, one class: a count that is PRESENT but impossible
# was being read as an absence, so the aggregate came out clean.
# =====================================================================================

# ------------------------ finding 1: a malformed count is refusal EVIDENCE, not an absence


def test_a_present_but_unreadable_count_becomes_explicit_refusal_evidence() -> None:
    """The measured shape: a string `"1"`. `reauths: null` alone is indistinguishable from "no reauth"."""
    record, refused = scope.scope_view_type_resolution({"reauths": "1"})
    assert record == {"reauths": None, "unavailable_reason": scope.COUNT_REFUSED}
    assert "view_type_resolution.reauths" in refused


def test_a_missing_count_is_not_the_same_answer_as_an_absent_record() -> None:
    """The discriminating pair. Neither may read as a refusal, and neither may read as the other."""
    missing, _ = scope.scope_view_type_resolution({"unavailable_reason": None})
    absent, _ = scope.scope_view_type_resolution(None)
    assert missing == {"reauths": None, "unavailable_reason": None}
    assert absent is None
    assert missing != {"reauths": None, "unavailable_reason": scope.COUNT_REFUSED}


def test_a_batch_with_an_unreadable_count_is_classified_unreadable_not_resolved() -> None:
    """The classification is the hinge: `resolved` is what let the aggregate come out clean."""
    aggregate, rows = scope.merged_view_type_resolution([("b2", {"reauths": 0}), ("b1", {"reauths": "1"})])
    assert [row["record"] for row in rows] == [scope.RESOLUTION_RESOLVED, scope.RESOLUTION_UNREADABLE]
    assert aggregate != {"reauths": 0, "unavailable_reason": None}
    assert aggregate["unavailable_reason"], "a batch we could not read must force a non-null aggregate reason"


def test_one_clean_and_one_malformed_batch_never_group_into_a_clean_aggregate(tmp_path: Path) -> None:
    """The same finding through the REAL grouping seam, and on through packaging."""
    oracle = tmp_path / "_oracle"
    clean = _batch(oracle, "b1", {"reauths": 0}, captured_at="2026-08-18T10:00:00Z")
    malformed = _batch(oracle, "b2", {"reauths": "1"}, captured_at="2026-08-18T14:00:00Z")
    grouped = _grouped(tmp_path, clean, malformed)

    aggregate = grouped["view_type_resolution"]
    assert aggregate != {"reauths": 0, "unavailable_reason": None}
    assert aggregate["unavailable_reason"]
    assert scope.RESOLUTION_UNREADABLE in [row["record"] for row in grouped["view_type_resolution_by_batch"]]

    packaged = _packaged(aggregate, grouped["view_type_resolution_by_batch"])["view_type_resolution"]
    assert packaged["unavailable_reason"], "the package restored the clean record the grouping refused"


def test_the_whole_path_stays_non_clean_for_a_malformed_batch(tmp_path: Path) -> None:
    """merge_batches -> subset_manifest -> package scope, end to end, on one document.

    The positive control it is judged against is `test_the_merged_aggregate_is_clean_only_when_every
    _batch_was`: with both batches readable this same path DOES produce `{reauths: 0, reason: null}`,
    so the assertion below is not satisfied by a rule that simply never emits a clean record.
    """
    oracle = tmp_path / "_oracle"
    first = _batch(oracle, "b1", {"reauths": "1"}, captured_at="2026-08-18T10:00:00Z")
    second = _batch(oracle, "b2", {"reauths": 0}, captured_at="2026-08-18T14:00:00Z")
    grouped = _grouped(tmp_path, first, second)
    packaged = _packaged(grouped["view_type_resolution"], grouped["view_type_resolution_by_batch"])
    assert packaged["view_type_resolution"] != {"reauths": 0, "unavailable_reason": None}
    assert packaged["view_type_resolution"]["unavailable_reason"]


# ---------------- finding 2: one capture can only have re-authenticated once, or not at all


@pytest.mark.parametrize("count", ["1", 1.0, True, False, -1, 2, 7])
def test_an_individual_count_outside_zero_or_one_is_refused(count: object) -> None:
    """`tableau_view_types.MAX_REAUTH == 1`, so `2` is not a bigger number - it is a forged record."""
    record, refused = scope.scope_view_type_resolution({"reauths": count})
    assert record == {"reauths": None, "unavailable_reason": scope.COUNT_REFUSED}
    assert "view_type_resolution.reauths" in refused


@pytest.mark.parametrize("count", [0, 1])
def test_the_two_counts_a_capture_can_actually_produce_still_ship(count: int) -> None:
    """The negative control: without this the bound could refuse everything and look identical."""
    record, refused = scope.scope_view_type_resolution({"reauths": count})
    assert record == {"reauths": count, "unavailable_reason": None}
    assert refused == []


def test_two_recovered_batches_aggregate_to_two_with_two_supporting_rows(tmp_path: Path) -> None:
    """The only way a total above one is legitimate: it is the SUM of validated per-batch rows."""
    oracle = tmp_path / "_oracle"
    older = _batch(oracle, "b1", {"reauths": 1}, captured_at="2026-08-18T10:00:00Z")
    newer = _batch(oracle, "b2", {"reauths": 1}, captured_at="2026-08-18T14:00:00Z")
    grouped = _grouped(tmp_path, older, newer)

    assert grouped["view_type_resolution"]["reauths"] == 2
    rows = grouped["view_type_resolution_by_batch"]
    assert [row["reauths"] for row in rows] == [1, 1]

    packaged = _packaged(grouped["view_type_resolution"], rows)
    assert packaged["view_type_resolution"]["reauths"] == 2, "a corroborated merge lost its evidence"
    assert packaged["view_type_resolution"]["unavailable_reason"] is None
    assert "view_type_resolution.reauths" not in packaged["scope"]["dropped_fields"]


def test_a_forged_aggregate_count_with_no_rows_at_all_is_refused() -> None:
    """Nothing corroborates it, and no single capture could have produced it."""
    packaged = _packaged({"reauths": 2, "unavailable_reason": None})
    assert packaged["view_type_resolution"] == {"reauths": None, "unavailable_reason": scope.COUNT_REFUSED}
    assert "view_type_resolution.reauths" in packaged["scope"]["dropped_fields"]


@pytest.mark.parametrize(
    ("claimed", "rows", "expected"),
    [
        # inflated: two claimed, one row supports one.
        (2, [{"batch": "b1", "record": "resolved", "reauths": 1}], 1),
        # erased: zero claimed, two rows each recovered - the last-wins defect, asserted downstream.
        (0, [{"batch": "b1", "reauths": 1}, {"batch": "b2", "reauths": 1}], 2),
        # rows that are themselves unreadable corroborate nothing, so they cannot lift the bound.
        (2, [{"batch": "b1", "reauths": "1"}, {"batch": "b2", "reauths": "1"}], 0),
    ],
)
def test_an_aggregate_count_is_recomputed_from_the_rows_never_believed(claimed: int, rows: list, expected: int) -> None:
    """`never trust a caller-supplied aggregate count over the rows` - in BOTH directions."""
    packaged = _packaged({"reauths": claimed, "unavailable_reason": None}, rows)
    assert packaged["view_type_resolution"]["reauths"] == expected
    assert packaged["view_type_resolution"]["unavailable_reason"] == scope.COUNT_RECOMPUTED
    assert "view_type_resolution.reauths" in packaged["scope"]["dropped_fields"]


def test_a_per_batch_row_is_held_to_the_individual_bound_too() -> None:
    """A row is one capture. `2` there is the same forgery, and it must not become a summand."""
    rows, refused = scope.scope_view_type_resolution_batches([{"batch": "b1", "record": "resolved", "reauths": 2}])
    assert rows == [{"batch": "b1", "record": "resolved", "reauths": None, "unavailable_reason": scope.COUNT_REFUSED}]
    assert "view_type_resolution_by_batch[].reauths" in refused


def test_a_forged_per_batch_row_count_is_never_a_summand() -> None:
    """The bound that makes the aggregate trustworthy: rows are validated BEFORE they are summed.

    Three claimed, and the only row a capture could have written contributes one. A rule that summed
    the rows as given would ratify the `2` it was handed and report three re-authentications from two
    batches that can structurally have had at most two.
    """
    packaged = _packaged(
        {"reauths": 3, "unavailable_reason": None},
        [{"batch": "b1", "reauths": 2}, {"batch": "b2", "reauths": 1}],
    )
    assert packaged["view_type_resolution"]["reauths"] == 1
    assert packaged["view_type_resolution"]["unavailable_reason"] == scope.COUNT_RECOMPUTED
    assert packaged["view_type_resolution_by_batch"][0]["reauths"] is None


def test_a_sibling_reading_sanitiser_is_refused_where_there_is_no_sibling() -> None:
    """The fail-closed half of `siblings=True`: a spec that cannot honour it raises.

    Silently passing the field its OWN value instead would restore exactly the blind aggregate the
    correction removed, and nothing in the output would say the rows had never been consulted.
    """
    with pytest.raises(TypeError):
        scope.project({"reauths": 1}, scope.Sanitized(scope.scope_grouped_resolution, siblings=True))


def test_the_paired_rule_is_idempotent_across_the_grouping_to_packaging_hop() -> None:
    """A corroborated merge re-read by the packager must survive byte-identical, twice over."""
    aggregate, rows = scope.merged_view_type_resolution([("b1", {"reauths": 1}), ("b2", {"reauths": 1})])
    once = _packaged(aggregate, rows)
    twice = _packaged(once["view_type_resolution"], once["view_type_resolution_by_batch"])
    assert once["view_type_resolution"] == twice["view_type_resolution"] == aggregate
    assert once["view_type_resolution_by_batch"] == twice["view_type_resolution_by_batch"]
