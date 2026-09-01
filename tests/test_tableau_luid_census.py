"""Offline contract for `tableau_luid_census`.

⚠️ The point of this file is that the census cannot be trusted to be safe by inspection. It handles a
credentialed response, so "it only prints counts" has to be ENFORCED and then TESTED, not asserted in
a docstring. `_emit`'s refusal is the enforcement; these are the tests.

The live half -- whether a real site actually emits blank LUIDs -- cannot be tested offline, and
deliberately is not faked here. It is recorded as a measurement in the #402 fixtures' docstrings, and
re-measurable at any time by running the script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tableau_luid_census as census_mod  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_view_types as view_types_mod  # noqa: E402  # pylint: disable=wrong-import-position

LUID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def _payload(workbooks):
    return {"data": {"workbooks": workbooks}}


# --------------------------------------------------------------------------- the safety enforcement


@pytest.mark.parametrize(
    "value",
    [
        "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
        "Regional Sales Dashboard",
        ["a", "b"],
        {"luid": "x"},
        3.5,
        b"bytes",
    ],
)
def test_emit_refuses_anything_that_could_carry_an_identifier(value):
    """⚠️ The load-bearing safety property, and it must be a REFUSAL rather than a convention.

    This module reads a credentialed, site-wide response. The promise that it reports "counts and
    shapes only" is worth nothing if a later edit can quietly print a workbook name -- so `_emit`
    raises instead, and a careless change fails loudly at the first call.

    ⚠️ The label is a LEGITIMATE one on purpose. It used to be the literal "label", and when the
    round-3 allowlist landed that stopped being an allowed label -- so this test kept passing while
    the VALUE check it exists to cover was never reached. Fifth time on this PR that a new guard has
    quietly made an older fixture vacuous; the fixture must reach its own subject.
    """
    with pytest.raises(SystemExit):
        census_mod._emit("blank_luids", value)  # pylint: disable=protected-access


@pytest.mark.parametrize("value", [0, 42, True, False, None])
def test_emit_allows_counts_and_flags(value):
    """The other half: refusing everything would be safe and useless."""
    census_mod._emit("blank_luids", value)  # pylint: disable=protected-access


def test_the_census_never_reads_a_luid_VALUE_only_its_shape():
    """Two sites, identical shapes, completely different identifiers -> identical census.

    That is the structural statement of "no identity is ever recorded". A census that varied with the
    luid values would be carrying them, however indirectly.
    """
    one = census_mod.census(_payload([{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}]}]))
    two = census_mod.census(_payload([{"dashboards": [{"luid": OTHER}], "sheets": [{"luid": "   "}]}]))
    assert one == two


# --------------------------------------------------------------------------- the census itself


def test_a_blank_luid_is_counted_separately_from_a_malformed_one():
    """⚠️ The distinction the whole #402 round-2 fix rests on, asserted at the census layer too.

    If these ever landed in one bucket the census would report a healthy site as broken, or hide a
    real malformed identifier among the expected hidden-sheet blanks.
    """
    counts = census_mod.classify([{"luid": LUID}, {"luid": ""}, {"luid": "   "}, {"luid": "D-1"}, {"luid": None}])
    assert counts == {
        "total": 5,
        "missing_key": 0,
        "non_string": 1,
        "blank": 2,
        "uuid": 1,
        "non_uuid_non_blank": 1,
    }


def test_a_node_with_no_luid_key_is_its_own_bucket():
    """Absent and null are different failures upstream, so they stay different here."""
    counts = census_mod.classify([{"name": "x"}, {"luid": None}])
    assert counts["missing_key"] == 1
    assert counts["non_string"] == 1


def test_a_workbook_is_counted_once_however_many_blanks_it_holds():
    """ "How many workbooks are affected" is the number an operator acts on, not the node total."""
    totals = census_mod.census(
        _payload([{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}, {"luid": ""}, {"luid": ""}]}])
    )
    assert totals["blank_luids"] == 3
    assert totals["workbooks_with_a_blank_luid"] == 1


def test_a_workbook_missing_a_declared_collection_is_reported_not_skipped_silently():
    """The schema declares both non-null, so an absent one is a finding rather than an empty list."""
    totals = census_mod.census(_payload([{"dashboards": [{"luid": LUID}]}]))
    assert totals["workbooks_with_an_unusable_collection"] == 1
    assert totals["nodes"] == 0


@pytest.mark.parametrize(
    "totals, expected",
    [
        ({"blank_luids": 116, "nodes": 476, "workbooks_with_an_unusable_collection": 0}, "CONFIRMED"),
        ({"blank_luids": 0, "nodes": 476, "workbooks_with_an_unusable_collection": 0}, "NOT-PRESENT"),
        ({"blank_luids": 0, "nodes": 0, "workbooks_with_an_unusable_collection": 0}, "CANNOT-TELL"),
    ],
)
def test_the_three_verdicts_are_distinguishable(totals, expected):
    """⚠️ CANNOT-TELL must not collapse into NOT-PRESENT.

    "No blank luids" and "nothing here could have had one" are different claims, and only the first
    is evidence about the site. Collapsing them is how a vacuous run gets cited as a clean result --
    the defect class this repository keeps finding in its own gates.
    """
    assert census_mod.verdict(totals, refused=False) == expected


def test_the_census_and_the_parser_agree_on_what_counts_as_a_luid():
    """Two implementations of the shape rule would drift; there is deliberately only one.

    `classify` calls the same public `is_luid` the parser uses, so a change to the rule cannot leave
    the census describing a site by a rule the parser no longer applies.
    """
    assert census_mod.classify([{"luid": LUID}])["uuid"] == 1
    assert view_types_mod.is_luid(LUID)
    assert not view_types_mod.is_luid("")
    assert not view_types_mod.is_luid("D-1")


def test_the_json_output_carries_only_integer_counts(tmp_path):
    """`--json` writes a file someone may paste into an issue, so it must be safe by construction."""
    totals = census_mod.census(_payload([{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}]}]))
    out = tmp_path / "census.json"
    out.write_text(json.dumps(totals, indent=2, sort_keys=True), encoding="utf-8")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded and all(isinstance(v, int) for v in loaded.values())
    assert LUID not in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- round 3, finding 2
#
# ⚠️ `_emit` validated only its `value`. `label` was printed verbatim -- measured, an arbitrary
# identifier reached stdout through it -- and the taint gate then certified the parameter
# unconditionally, so nothing anywhere was actually checking it.
#
# The byte-identical-census test above is genuinely good and still could not see this: it only ever
# places identifiers in the VALUE position, so the label path was a branch its fixtures never
# entered. That is vacuity mode 2, in the one test that carried the whole safety argument.


@pytest.mark.parametrize(
    "label",
    [
        LUID,
        "Regional Sales Dashboard",
        f"dashboards_{LUID}",
        "blank_luids ",
        "BLANK_LUIDS",
        "",
    ],
)
def test_emit_refuses_a_label_this_module_did_not_author(label):
    """⚠️ The second half of the safety argument, and the half that was missing.

    A label is as much a print as a value is. The last three cases matter as much as the first
    three: an allowlist that tolerated a trailing space or a case fold would be one `f"{...}"` away
    from being no allowlist at all.
    """
    with pytest.raises(SystemExit):
        census_mod._emit(label, 1)  # pylint: disable=protected-access


def test_the_label_refusal_does_not_echo_the_label_it_rejected(capsys):
    """⚠️ Quoting the rejected label back would reintroduce the leak ON THE ERROR PATH.

    That is not hypothetical in this repo: the reflected-credential rounds that produced
    `tableau_http` were all about a server-controlled string reaching a diagnostic, and an exception
    message is a diagnostic.
    """
    with pytest.raises(SystemExit) as excinfo:
        census_mod._emit(LUID, 1)  # pylint: disable=protected-access
    assert LUID not in str(excinfo.value)
    assert LUID not in capsys.readouterr().out


def test_every_label_the_script_actually_uses_is_allowed():
    """The mirror assertion: an allowlist that refused a real label would be safe and broken.

    Built from the census keys the script really produces, so adding a bucket without adding it to
    `LABELS` fails here rather than at the end of a live run against a customer site.
    """
    totals = census_mod.census(_payload([{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}]}]))
    totals["assessable"] = 1
    for key in totals:
        census_mod._emit(key, totals[key])  # pylint: disable=protected-access
    for label in census_mod.FIXED_LABELS:
        census_mod._emit(label, 0)  # pylint: disable=protected-access


# --------------------------------------------------------------------------- round 3, finding 1
#
# ⚠️ The census reported an authoritative verdict on input it had REFUSED. Measured: a response
# carrying GraphQL `errors` beside one valid dashboard produced `VERDICT: NOT-PRESENT`, exit 0 -- a
# permanent measurement artifact stating a site is clean when it was never assessed, and the omitted
# workbooks are exactly the ones that might have carried the blank luids being looked for.
#
# This is the most repeated defect class in this repository: unassessable input collapsing into the
# clean bucket.


class _Stub:
    """A session whose single GraphQL answer is scripted. No network."""

    def __init__(self, body):
        self.body = body

    def sign_in(self):
        return None

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None):  # noqa: ARG002
        return 200, self.body.encode("utf-8"), {}


@pytest.fixture(name="run_census")
def _run_census(monkeypatch):
    """Drive `main()` against a scripted body. Returns (exit_code, stdout)."""

    def run(payload, json_out=None):
        monkeypatch.setattr(census_mod, "_session", lambda _path: _Stub(json.dumps(payload)))
        argv = ["--env", "unused"] + (["--json", str(json_out)] if json_out else [])
        return census_mod.main(argv)

    return run


ONE_DASHBOARD = [{"dashboards": [{"luid": LUID}], "sheets": []}]


@pytest.mark.parametrize(
    "payload, why",
    [
        ({"errors": [{"message": "boom"}], "data": {"workbooks": ONE_DASHBOARD}}, "graphql errors beside usable data"),
        ({"data": {"workbooks": [{"dashboards": 7, "sheets": []}]}}, "a collection that is not a list"),
        ({"data": {"workbooks": [{"dashboards": [{"luid": LUID}]}]}}, "a collection that is absent"),
        ({"data": {"workbooks": [{"dashboards": [{"luid": LUID}], "sheets": None}]}}, "a collection that is null"),
    ],
)
def test_a_refused_or_unreadable_response_never_yields_an_authoritative_verdict(payload, why, run_census, capsys):
    """⚠️ The load-bearing property, and the one the script exists to make permanent.

    Each of these once produced `NOT-PRESENT` (or an uncaught `TypeError`). A zero here does not
    measure the site; it measures how little of it we could read.
    """
    code = run_census(payload)
    out = capsys.readouterr().out
    assert code == census_mod.EXIT_CANNOT_TELL, f"{why} must not exit as a completed measurement"
    assert "VERDICT: CANNOT-TELL" in out, f"{why} must not report an authoritative verdict"
    assert "NOT-PRESENT" not in out and "CONFIRMED" not in out


def test_a_healthy_response_still_produces_an_authoritative_answer(run_census, capsys):
    """⚠️ The control. Refusing everything would satisfy the test above and destroy the tool."""
    code = run_census({"data": {"workbooks": [{"dashboards": [{"luid": LUID}], "sheets": [{"luid": ""}]}]}})
    out = capsys.readouterr().out
    assert code == census_mod.EXIT_OK
    assert "VERDICT: CONFIRMED" in out


def test_a_clean_site_is_reported_as_NOT_PRESENT_not_as_cannot_tell(run_census, capsys):
    """The other control: CANNOT-TELL must not swallow a real negative result."""
    code = run_census({"data": {"workbooks": ONE_DASHBOARD}})
    out = capsys.readouterr().out
    assert code == census_mod.EXIT_OK
    assert "VERDICT: NOT-PRESENT" in out


def test_the_exit_code_follows_the_verdict_even_with_nothing_to_assess(run_census, capsys):
    """A run that PRINTED cannot-tell still exited 0, so an automated caller read it as clean."""
    code = run_census({"data": {"workbooks": []}})
    assert "VERDICT: CANNOT-TELL" in capsys.readouterr().out
    assert code == census_mod.EXIT_CANNOT_TELL


def test_the_json_of_an_unassessable_run_says_so(run_census, tmp_path):
    """⚠️ The JSON outlives the terminal output, so it carries the flag rather than implying it.

    A consumer must not be able to read `blank_luids: 0` without also seeing whether that zero is a
    measurement of the site or of our own blindness.
    """
    out = tmp_path / "census.json"
    run_census({"errors": [{"message": "boom"}], "data": {"workbooks": ONE_DASHBOARD}}, json_out=out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["assessable"] == 0
    assert all(isinstance(value, int) for value in loaded.values())


def test_the_json_of_a_real_measurement_says_so_too(run_census, tmp_path):
    """The flag has to distinguish, so it must take the other value on a good run."""
    out = tmp_path / "census.json"
    run_census({"data": {"workbooks": ONE_DASHBOARD}}, json_out=out)
    assert json.loads(out.read_text(encoding="utf-8"))["assessable"] == 1


@pytest.mark.parametrize(
    "totals, refused, expected",
    [
        ({"blank_luids": 116, "nodes": 476, "workbooks_with_an_unusable_collection": 0}, False, "CONFIRMED"),
        ({"blank_luids": 0, "nodes": 476, "workbooks_with_an_unusable_collection": 0}, False, "NOT-PRESENT"),
        ({"blank_luids": 0, "nodes": 0, "workbooks_with_an_unusable_collection": 0}, False, "CANNOT-TELL"),
        # ⚠️ Both unassessable routes, INCLUDING the one that would otherwise have said CONFIRMED --
        # a partial answer is not evidence even when the part we read looks decisive.
        ({"blank_luids": 116, "nodes": 476, "workbooks_with_an_unusable_collection": 0}, True, "CANNOT-TELL"),
        ({"blank_luids": 116, "nodes": 476, "workbooks_with_an_unusable_collection": 1}, False, "CANNOT-TELL"),
        ({"blank_luids": 0, "nodes": 476, "workbooks_with_an_unusable_collection": 1}, False, "CANNOT-TELL"),
    ],
)
def test_the_verdict_requires_having_actually_assessed_the_site(totals, refused, expected):
    """`refused` is a REQUIRED argument precisely so a caller cannot forget it and get a clean answer."""
    assert census_mod.verdict(totals, refused) == expected


def test_an_unreadable_collection_is_counted_rather_than_crashing():
    """`dashboards: 7` reached `for node in 7` and aborted the whole run with a TypeError."""
    totals = census_mod.census(_payload([{"dashboards": 7, "sheets": []}, {"dashboards": [], "sheets": None}]))
    assert totals["workbooks_with_an_unusable_collection"] == 2
    assert totals["nodes"] == 0


def test_the_recorded_live_measurement_replays_to_the_same_verdict(run_census, capsys, tmp_path):
    """⚠️ Ties the measured citation to executable code, so it cannot rot into folklore.

    Reconstructed from the counts recorded on 2026-09-01 against our Tableau Cloud trial: 48
    workbooks, 60 dashboards all with real luids, 416 sheets of which 116 are blank, those blanks
    spread across 5 workbooks. Nothing here is an identifier -- the shape is the whole measurement,
    which is the same reason the census reports counts and never names anything.

    This is a REPLAY, not a re-measurement: it proves the pipeline still turns that shape into
    CONFIRMED and exit 0. If the recorded numbers in the fixture docstring are ever edited without
    re-running the script, this stays green -- so it pins reproducibility, not truth. The truth is
    re-established by running `scripts/tableau_luid_census.py`.
    """
    workbooks = []
    blanks_left, blank_workbooks_left = 116, 5
    for index in range(48):
        # 60 dashboards over 48 workbooks, and 416 sheets, distributed so the totals land exactly.
        dashboards = [{"luid": f"{index:08x}-1111-4111-8111-aaaaaaaaaaaa"}] + (
            [{"luid": f"{index:08x}-2222-4222-8222-aaaaaaaaaaaa"}] if index < 12 else []
        )
        take = 0
        if blank_workbooks_left and blanks_left:
            take = min(blanks_left, 24 if blank_workbooks_left > 1 else blanks_left)
            blanks_left -= take
            blank_workbooks_left -= 1
        sheets = [{"luid": ""} for _ in range(take)]
        sheets += [
            {"luid": f"{index:08x}-3333-4333-8333-{position:012x}"}
            for position in range(300 // 48 + (index < 300 % 48))
        ]
        workbooks.append({"dashboards": dashboards, "sheets": sheets})

    out = tmp_path / "census.json"
    code = run_census({"data": {"workbooks": workbooks}}, json_out=out)
    printed = capsys.readouterr().out
    loaded = json.loads(out.read_text(encoding="utf-8"))

    assert loaded["workbooks"] == 48
    assert loaded["dashboards_total"] == 60
    assert loaded["dashboards_blank"] == 0
    assert loaded["sheets_total"] == 416
    assert loaded["sheets_blank"] == 116
    assert loaded["workbooks_with_a_blank_luid"] == 5
    assert loaded["sheets_non_uuid_non_blank"] == 0
    assert loaded["dashboards_non_uuid_non_blank"] == 0
    assert loaded["assessable"] == 1
    assert code == census_mod.EXIT_OK
    assert "VERDICT: CONFIRMED" in printed


# --------------------------------------------------------------------------- round 4
#
# ⚠️ Every fixture above this line constructs a valid ENVELOPE and varies what is inside it. A defect
# ABOVE where a fixture starts is structurally invisible to it, which is exactly why the workbook-
# level fix of round 3 left this open -- and it is the same class as the round-3 `_emit` hole, where
# the safety test only ever placed identifiers in the value position and so could never reach the
# label path. These start above the envelope.


@pytest.mark.parametrize(
    "payload, why",
    [
        (None, "a top-level null"),
        ([{"data": {"workbooks": []}}], "a top-level list"),
        ("nope", "a top-level string"),
        (7, "a top-level int"),
        ({"data": None}, "`data` is null"),
        ({"data": []}, "`data` is a list"),
        ({"data": "nope"}, "`data` is a string"),
        ({"extensions": {}}, "`data` is absent"),
        ({"data": {"workbooks": None}}, "`workbooks` is null"),
        ({"data": {"workbooks": {"dashboards": []}}}, "`workbooks` is a dict"),
        ({"data": {}}, "`workbooks` is absent"),
    ],
)
def test_a_malformed_envelope_cannot_bypass_the_guarantees(payload, why, run_census, capsys, tmp_path):
    """⚠️ Each of these once escaped as an uncaught TypeError or AttributeError, BEFORE any verdict,
    exit code or assessability flag existed - so a server-controlled body bypassed every guarantee.
    """
    out = tmp_path / "census.json"
    code = run_census(payload, json_out=out)
    printed = capsys.readouterr().out
    assert code == census_mod.EXIT_CANNOT_TELL, f"{why} must not exit as a completed measurement"
    assert "VERDICT: CANNOT-TELL" in printed, why
    assert json.loads(out.read_text(encoding="utf-8"))["assessable"] == 0, why


@pytest.mark.parametrize(
    "payload",
    [None, [], "nope", 7, 3.5, True, {"data": None}, {"data": []}, {"data": "x"}, {"data": {"workbooks": None}}],
)
def test_census_is_total_over_arbitrary_decoded_json(payload):
    """`census` is public and its whole job is to describe a POSSIBLY MALFORMED response.

    ⚠️ An unreadable envelope must produce zeroes WITH `envelope_readable = 0`. A zero that means
    "we could not look" being indistinguishable from one that means "we looked and found none" is
    the defect class this whole script exists to avoid.
    """
    totals = census_mod.census(payload)
    assert totals["nodes"] == 0
    assert totals["blank_luids"] == 0
    assert totals["envelope_readable"] == 0


def test_a_readable_envelope_says_so():
    """The mirror. A flag that is always 0 would satisfy the test above and mean nothing."""
    totals = census_mod.census(_payload([{"dashboards": [{"luid": LUID}], "sheets": []}]))
    assert totals["envelope_readable"] == 1
    assert census_mod.census({"data": {"workbooks": []}})["envelope_readable"] == 1


def test_each_unassessable_route_is_pinned_independently():
    """⚠️ Two of `assessable`'s three clauses are IMPLIED by the first, so nothing driven through
    `main()` can kill them - a clause no test can fail is worse than no clause.

    They are pinned here directly, against a `totals` today's loader would not produce, exactly as an
    arithmetically-implied clause was pinned in the #384 campaign. Each row removes ONE reason, so it
    fails if that clause is dropped.
    """
    healthy = {"envelope_readable": 1, "workbooks_with_an_unusable_collection": 0}
    assert census_mod.assessable(healthy, refused=False)
    assert not census_mod.assessable(healthy, refused=True)
    assert not census_mod.assessable({**healthy, "envelope_readable": 0}, refused=False)
    assert not census_mod.assessable({**healthy, "workbooks_with_an_unusable_collection": 1}, refused=False)


@pytest.mark.parametrize(
    "payload",
    [None, [], "nope", 7, {"data": None}, {"data": []}, {"data": {"workbooks": None}}, {"data": {}}],
)
def test_an_unreadable_envelope_always_makes_the_parser_refuse(payload):
    """⚠️ The IMPLICATION itself, asserted rather than assumed.

    `assessable`'s envelope clause is redundant only for as long as this holds. Asserting it is what
    lets the clause be kept as an independent requirement instead of quietly deleted - and if a
    future parser change breaks the implication, this fails rather than the clause silently becoming
    load-bearing without anyone noticing.
    """
    assert census_mod.census(payload)["envelope_readable"] == 0
    _mapping, unavailable = view_types_mod.parse_payload(payload)
    assert unavailable, "an envelope the census cannot read must also be one the parser refuses"
