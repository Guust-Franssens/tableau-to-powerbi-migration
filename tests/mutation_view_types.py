"""Mutation campaign for the #402 modules. NOT collected by pytest -- it DRIVES pytest.

Covers BOTH `tableau_view_types` (against ``tests/test_capture_tableau_oracle.py``) and
`tableau_luid_census` (against ``tests/test_tableau_luid_census.py``). The census is in the same
campaign rather than a file of its own because it exists only to keep the view-type premise
measurable: a census that reported a clean verdict on a response it had refused would quietly retire
the very finding this campaign defends. Each mutation names its own suite; both suites are
baselined before anything is mutated.

    python tests/mutation_view_types.py [name ...]

Why this is committed rather than run once
------------------------------------------
Every mutation here corresponds to a defect that was really in this module and was really found by
review. The tests that catch them are committed; the *proof that those tests can fail* was not, and
so evaporated with the session that produced it. A test credited as coverage that cannot actually
fail is the single most common defect class this repository keeps finding in its own gates -- and it
happened to THIS module's suite twice in one day:

* adding ``_LUID_RE`` made every fails-closed fixture refuse on luid SHAPE before reaching the branch
  it was written for;
* adding the ``dashboards``/``sheets`` presence guard did the same to three more.

Both times the suite stayed green while covering strictly less. The committed fix for that is the
``guard`` column in ``test_view_types_fails_closed_and_never_guesses``; this file is the fix for the
other half -- it re-proves, on demand, that each guard is load-bearing.

How a verdict is reached
------------------------
Verdicts come from pytest's own lifecycle records via :mod:`tests.mutation_harness`, never from
scraping a summary line. ``mutation_harness``'s docstring explains why: a collection error and a
dying xdist worker both *look* like a named failure in terminal text.

Guard removals are **source-level** -- the module's own text is edited and re-executed into its
existing ``__dict__``, so the mutant is the real code path with one guard deleted. An earlier version
replaced whole functions with hand-written re-implementations, which is weaker: a re-implementation
differs from the original in ways beyond the intended change, and a CAUGHT verdict then gets credited
to the wrong difference. Two mutations here were measured being killed by their *reason text* rather
than their behaviour, and were rewritten to keep the original wording.

⚠️ **Anchor rot fails LOUDLY.** ``_source_mutation`` asserts its anchor is present, so a snippet that
no longer matches the source raises at plugin import and is scored INVALID -- never SURVIVED. That
matters: a silent no-op would read as "the suite has a hole" and send someone hunting one that is not
there. ``absent-anchor-stale-source-text`` is the standing control for exactly that.

Controls, and why both directions are needed
--------------------------------------------
* ``cosmetic-*`` must **SURVIVE**. If a comment rewrite is "caught", the suite is asserting on
  incidental text and every other CAUGHT above it is suspect.
* ``absent-anchor-*`` must be **INVALID**. This proves the harness distinguishes "the mutation never
  applied" from "the mutation was caught" -- the failure that once scored 22 of 22 false positives.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_harness as mh  # noqa: E402  # pylint: disable=wrong-import-position

TARGET = "tests/test_capture_tableau_oracle.py"
CENSUS_TARGET = "tests/test_tableau_luid_census.py"

#: Mutations whose subject is `tableau_luid_census` rather than `tableau_view_types`, and which
#: therefore have to be run against the census suite. The census exists ONLY to keep the view-type
#: premise measurable, so its guards belong in the same campaign: an unassessable run that reports a
#: clean verdict would retire the very finding this campaign exists to defend.
CENSUS_MUTATIONS: set[str] = set()

#: ⚠️ mutation -> the single test node it must be caught by.
#:
#: ⚠️ AND FOR A DIFFERENTIAL ASSERTION, THE MUTATION MUST MOVE EXACTLY ONE ARM.
#:
#: A parity test says "A refuses, B does not". Any mutation that moves A and B TOGETHER defeats it --
#: and still goes red, on the arm that was supposed to be the control, so it LOOKS caught. That risk
#: is intrinsic to every differential test, and this campaign has one as its central correctness
#: property (`test_the_census_and_the_shipped_parser_agree_on_the_same_bytes`). It is the seventh
#: distinct vacuity mode found on this PR, and the only one an anchor map cannot catch: the anchor
#: was right, the verdict was CAUGHT, and the evidence was still about something else.
#:
#: The other six, for the record: an assertion against its own constant; a branch the fixture never
#: enters; a clause implied by its siblings; a rule that made a test pass for two reasons; a broad
#: "it refused" satisfiable by any guard; and a fixture short-circuiting on an earlier guard than the
#: one it names.
#:
#: `mutation_harness.run` passes `-x`, so a CAUGHT verdict names whichever test failed FIRST -- not
#: necessarily the one the mutation was written for. That is not pedantry: crediting the wrong test
#: inverted three verdicts on a sibling PR the same day. Where a mutation declares an anchor here it
#: is run against THAT NODE ALONE, which is strictly stronger evidence -- the named test is then the
#: sole reason to refuse, and a mutation that survives it cannot be rescued by a bystander.
INTENDED: dict[str, str] = {}


def _source_mutation(old: str, new: str) -> str:
    """A plugin snippet that replaces real source text and re-execs the module IN PLACE.

    Re-exec into the existing module ``__dict__`` rather than a fresh module object: other modules
    have already done ``import tableau_view_types``, and rebinding ``sys.modules`` would leave their
    attribute lookups pointing at the unmutated original.
    """
    return f"""
import tableau_view_types as vt
_src = open(vt.__file__, encoding="utf-8").read()
_old = {old!r}
_new = {new!r}
assert _old in _src, "mutation anchor not found - the snippet is stale, not the code"
exec(compile(_src.replace(_old, _new, 1), vt.__file__, "exec"), vt.__dict__)
"""


# ⚠️ Indentation is part of every anchor. `_fold_workbook` and `_fold_nodes` each de-indented the
# lines below them when the module was split, and a stale anchor is scored INVALID rather than
# SURVIVED precisely so that churn cannot masquerade as a hole in the suite.
_STATUS_GUARD = '    if status != 200:\n        return {}, f"metadata api returned HTTP {status}"\n'
_SIZE_GUARD = "    if len(body) > _MAX_BODY_BYTES:"
_STRICT_DECODE = 'payload = json.loads(body.decode("utf-8"))'
_BROAD_PARSE_CATCH = (
    "    except Exception as exc:  # pylint: disable=broad-exception-caught\n"
    '        return {}, f"metadata api response was not usable JSON: {type(exc).__name__}"'
)
_TOPLEVEL_GUARD = "    if not isinstance(payload, dict):"
_ERRORS_PRESENCE = '    if "errors" not in payload:'
_ERRORS_NONLIST = '    return f"metadata api `errors` was {type(errors).__name__}, not a list; response refused"'
_ERRORS_COUNT = '        return f"metadata api returned {len(errors)} graphql error(s); response refused"'
_WORKBOOK_GUARD = "    if not isinstance(workbook, dict):"
_KEY_PRESENCE = "        if key not in workbook:"
_NODES_LIST_GUARD = "        if not isinstance(nodes, list):"
_NODE_GUARD = "        if not isinstance(node, dict):"
_LUID_TYPE_GUARD = "        if not isinstance(luid, str):"
_BLANK_SKIP = "        if not stripped:\n            continue"
_LUID_SHAPE_GUARD = "        if not is_luid(stripped):"
_CONTRADICTION_GUARD = "        if mapping.get(key_luid, kind) != kind:"
_REFUSAL_PROPAGATE = "        if refused:\n            return {}, refused"
_FOLD_PROPAGATE = (
    "        refused = _fold_nodes(key, kind, nodes, mapping)\n        if refused:\n            return refused"
)
_EXC_TYPE = 'return {}, f"metadata api call failed: {type(exc).__name__}"'
_BAD_LUID_REASON = 'return f"a `{key}` node carried a non-empty value that is not a luid; response refused"'

MUTATIONS: dict[str, str] = {
    # --- refuse the WHOLE answer, never a part of it --------------------------------------------
    # ⚠️ Keeps the original refusal wording, so the `guard` assertion cannot kill it and only the
    # partial-trust fixture can. Measured: with a different reason string it was caught by the wrong
    # test entirely.
    "skip-malformed-node-keep-siblings": """
import tableau_view_types as vt
_D, _W = vt.DASHBOARD, vt.WORKSHEET
def _mapping_from(payload):
    mapping = {}
    for wb in ((payload.get("data") or {}).get("workbooks") or []):
        if not isinstance(wb, dict):
            continue
        for key, kind in (("dashboards", _D), ("sheets", _W)):
            for node in (wb.get(key) or []):
                luid = node.get("luid") if isinstance(node, dict) else None
                if isinstance(luid, str) and luid.strip():
                    mapping[luid.strip().lower()] = kind
    if not mapping:
        return {}, "metadata api returned no dashboards or sheets carrying a luid"
    return mapping, None
vt._mapping_from = _mapping_from
""",
    "duplicate-luid-last-wins": _source_mutation(_CONTRADICTION_GUARD, "        if False:"),
    "no-toplevel-shape-check": _source_mutation(_TOPLEVEL_GUARD, "    if False:"),
    "errors-block-ignored": _source_mutation(_ERRORS_PRESENCE, "    if True:"),
    # ⚠️ The measured fail-open: the check was TRUTHINESS, so `"errors": 0` walked past it and its
    # `data` was trusted.
    "errors-tested-for-truthiness-again": _source_mutation(_ERRORS_PRESENCE, '    if not payload.get("errors"):'),
    "errors-nonlist-shape-accepted": _source_mutation(_ERRORS_NONLIST, "    return None"),
    "http-status-guard-removed": _source_mutation(_STATUS_GUARD, ""),
    "size-ceiling-removed": _source_mutation(_SIZE_GUARD, "    if False:"),
    # ⚠️ Neither escape is a JSONDecodeError, which is why the enumerated catch let both abort the
    # whole capture: a 5000-digit int raises ValueError (CPython's int-conversion limit) and 200k
    # nested brackets raise RecursionError.
    "parse-catch-narrowed-to-jsondecodeerror": _source_mutation(
        _BROAD_PARSE_CATCH,
        "    except json.JSONDecodeError as exc:\n"
        '        return {}, f"metadata api response was not usable JSON: {type(exc).__name__}"',
    ),
    # ⚠️ `replace` silently rewrote an invalid byte to U+FFFD and carried on, producing a TRUSTED
    # mapping from a body we could not actually read.
    "decode-errors-replaced-again": _source_mutation(
        _STRICT_DECODE, 'payload = json.loads(body.decode("utf-8", "replace"))'
    ),
    "workbook-shape-guard-removed": _source_mutation(_WORKBOOK_GUARD, "    if False:"),
    # ⚠️ The schema declares both collections non-null, so ABSENT is malformed rather than empty.
    "absent-collection-treated-as-empty": _source_mutation(
        _KEY_PRESENCE, "        if key not in workbook:\n            continue\n        if False:"
    ),
    "null-collection-treated-as-empty": _source_mutation(
        _NODES_LIST_GUARD,
        "        if nodes is None:\n            continue\n        if not isinstance(nodes, list):",
    ),
    "node-shape-guard-removed": _source_mutation(_NODE_GUARD, "        if False:"),
    # The two halves of the luid rule, proved SEPARATELY. A single "remove the whole condition"
    # mutation is killed by whichever half a fixture happens to exercise, and credited to both.
    "luid-shape-guard-removed": """
import tableau_view_types as vt
import re
vt._LUID_RE = re.compile(r"^.+$")   # any non-empty string is a "luid"
""",
    "luid-isinstance-guard-removed": _source_mutation(_LUID_TYPE_GUARD, "        if False:"),
    # --- the hidden-sheet rule, BOTH directions ---------------------------------------------------
    # ⚠️ These must be caught by DIFFERENT tests. If one test kills both, the rule has collapsed into
    # a single behaviour and the feature is either inert (everything refuses) or fail-open
    # (everything is skipped). Measured on a real site: 116 blank luids across 5 of 48 workbooks, so
    # the first mutation below reproduces a state in which typing is OFF for the entire estate.
    "blank-luid-refuses-the-whole-site": _source_mutation(
        _BLANK_SKIP,
        '        if not stripped:\n            return f"a `{key}` node carried a blank luid; response refused"',
    ),
    "garbage-luid-skipped-like-a-blank-one": _source_mutation(
        _LUID_SHAPE_GUARD, "        if not is_luid(stripped):\n            continue\n        if False:"
    ),
    # --- the seams themselves ----------------------------------------------------------------------
    "fold-nodes-refusal-ignored": _source_mutation(
        _FOLD_PROPAGATE,
        "        refused = _fold_nodes(key, kind, nodes, mapping)\n        if False:\n            return refused",
    ),
    "fold-refusal-ignored": _source_mutation(_REFUSAL_PROPAGATE, "        if False:\n            return {}, refused"),
    # ⚠️ The accumulator is PARTIALLY FILLED at the moment of refusal (earlier workbooks already
    # folded in), so returning it rather than a fresh {} ships exactly the partial answer this
    # function exists to refuse, reached from the other direction.
    "refused-returns-the-partial-mapping": _source_mutation(
        _REFUSAL_PROPAGATE, "        if refused:\n            return mapping, refused"
    ),
    # --- no server-controlled text in any reason ---------------------------------------------------
    "graphql-error-message-reported": _source_mutation(
        _ERRORS_COUNT, '        return "metadata api error: " + str(errors[0].get("message"))[:120]'
    ),
    "exception-message-reported": _source_mutation(_EXC_TYPE, 'return {}, f"metadata api call failed: {exc}"'),
    # ⚠️ Keeps the guard wording verbatim and only APPENDS the server's value, so the `guard`
    # assertion still passes and the credential-sentinel test is the only thing that can catch it.
    "bad-luid-value-reported": _source_mutation(
        _BAD_LUID_REASON,
        'return f"a `{key}` node carried a non-empty value that is not a luid: {luid!r}; response refused"',
    ),
    # --- the join, and the reporting ---------------------------------------------------------------
    # ⚠️ This one SURVIVED all 53 tests when the review found it: every fixture's `name` was absent
    # from the mapping, so a name-keyed lookup fell through to `unknown` and merely looked cautious.
    "stamp-keyed-on-name-not-id": _source_mutation(
        'luid = str(view.get("id") or "").strip().lower()', 'luid = str(view.get("name") or "").strip().lower()'
    ),
    "stamp-defaults-to-worksheet": _source_mutation(
        "view[VIEW_TYPE_KEY] = mapping.get(luid, UNKNOWN)", "view[VIEW_TYPE_KEY] = mapping.get(luid, WORKSHEET)"
    ),
    "resolve_and_stamp-swallows-the-warning": _source_mutation("    if unavailable:", "    if False:"),
    "census-drops-zero-keys": _source_mutation(
        'return {kind: sum(1 for r in records if r.get("view_type") == kind) for kind in (DASHBOARD, WORKSHEET, UNKNOWN)}',
        'return {kind: sum(1 for r in records if r.get("view_type") == kind) '
        'for kind in (DASHBOARD, WORKSHEET, UNKNOWN) if any(r.get("view_type") == kind for r in records)}',
    ),
    # --- CONTROLS ----------------------------------------------------------------------------------
    "cosmetic-comment-reworded": _source_mutation(
        "# `luid` is not queried anywhere else in this repo",
        "# COSMETIC MUTATION: this comment was reworded and nothing else changed",
    ),
    "cosmetic-docstring-rewritten": """
import tableau_view_types as vt
vt.view_types.__doc__ = "cosmetic change with no behavioural effect"
vt.__doc__ = "cosmetic"
""",
    "absent-anchor-no-such-symbol": """
import tableau_view_types as vt
vt.this_symbol_does_not_exist.attribute = 1
""",
    "absent-anchor-stale-source-text": _source_mutation("this exact text is not in the module", "irrelevant"),
}

# The census half of the campaign, run against the census suite.
MUTATIONS.update(
    {
        "census-verdict-ignores-the-parser-refusal": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    if not assessable(totals, refused):\\n        return "CANNOT-TELL"\'\n_new = \'    if False:\\n        return "CANNOT-TELL"\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        # ⚠️ `census-ignores-an-unreadable-workbook` lived here until round 4 replaced the single
        # -clause `assessable` return with three clauses. Its anchor went stale and it scored
        # INVALID -- the mechanism working. Not re-anchored: `census-assessable-drops-the-collection
        # -clause` is the same mutation with a valid anchor and an intended node, and a redundant
        # duplicate would produce a CAUGHT verdict that proves nothing the other does not.
        "census-exit-code-always-ok": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    return EXIT_OK if answer != "CANNOT-TELL" else EXIT_CANNOT_TELL\'\n_new = \'    return EXIT_OK\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-json-drops-the-assessable-flag": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    totals["assessable"] = int(assessable(totals, bool(unavailable)))\'\n_new = \'    totals["assessable"] = 1\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-unreadable-collection-crashes-again": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    return all(isinstance(workbook.get(key), list) for key in ("dashboards", "sheets"))\'\n_new = \'    return all(key in workbook for key in ("dashboards", "sheets"))\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-label-allowlist-removed": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    if label not in LABELS:\'\n_new = \'    if False:\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-label-allowlist-case-folded": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    if label not in LABELS:\'\n_new = \'    if label.strip().lower() not in {x.lower() for x in LABELS}:\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-label-refusal-echoes-the-label": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'        raise SystemExit("REFUSED to print a label this module did not author (see LABELS)")\'\n_new = \'        raise SystemExit(f"REFUSED to print {label!r}: not an authored label")\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "cosmetic-census-comment-reworded": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'#: Everything this module is willing to put on stdout.\'\n_new = \'#: COSMETIC MUTATION: reworded, nothing else changed.\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "absent-anchor-stale-census-text": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'this exact text is not in the census\'\n_new = \'irrelevant\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
    }
)
MUTATIONS.update(
    {
        "seam-requires-a-dict-envelope-again": '\nimport tableau_view_types as vt\n_src = open(vt.__file__, encoding="utf-8").read()\n_old = \'    if not isinstance(payload, dict):\\n        return {}, f"metadata api response was {type(payload).__name__}, not an object"\\n    refused = _errors_refusal(payload)\'\n_new = \'    refused = _errors_refusal(payload)\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), vt.__file__, "exec"), vt.__dict__)\n',
        "census-envelope-assumed-to-be-a-dict-again": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    data = payload.get("data") if isinstance(payload, dict) else None\\n    workbooks = data.get("workbooks") if isinstance(data, dict) else None\'\n_new = \'    data = payload.get("data", {})\\n    workbooks = data.get("workbooks", [])\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-envelope-flag-always-readable": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    totals["envelope_readable"] = int(isinstance(workbooks, list))\'\n_new = \'    totals["envelope_readable"] = 1\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-envelope-flag-always-unreadable": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    totals["envelope_readable"] = int(isinstance(workbooks, list))\'\n_new = \'    totals["envelope_readable"] = 0\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-assessable-drops-the-envelope-clause": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'        not refused and totals.get("envelope_readable", 1) == 1 and totals["workbooks_with_an_unusable_collection"] == 0\'\n_new = \'        not refused and totals["workbooks_with_an_unusable_collection"] == 0\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-assessable-drops-the-collection-clause": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'        not refused and totals.get("envelope_readable", 1) == 1 and totals["workbooks_with_an_unusable_collection"] == 0\'\n_new = \'        not refused and totals.get("envelope_readable", 1) == 1\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        "census-assessable-drops-the-refusal-clause": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'        not refused and totals.get("envelope_readable", 1) == 1 and totals["workbooks_with_an_unusable_collection"] == 0\'\n_new = \'        totals.get("envelope_readable", 1) == 1 and totals["workbooks_with_an_unusable_collection"] == 0\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
    }
)
CENSUS_MUTATIONS.update(
    {
        "census-assessable-drops-the-collection-clause",
        "census-envelope-flag-always-unreadable",
        "census-assessable-drops-the-envelope-clause",
        "census-assessable-drops-the-refusal-clause",
        "census-envelope-flag-always-readable",
        "census-envelope-assumed-to-be-a-dict-again",
    }
)
# ⚠️ The three label mutations exercise the RUNTIME allowlist, not a static call-site finding.
# The gate CAN see `_emit`'s parameters through intra-module propagation - removing the
# certification produces findings for `label`, `value` and `type(value).__name__` inside the
# callee - but it is not a recognised sink CALL, so it cannot report a leak at the call site.
# The certification therefore documents the enforcement; the allowlist IS the enforcement, and
# these mutations must (and do) delete the runtime check rather than expect a static finding.
# ⚠️ Proves the `guard` column is load-bearing rather than decorative. This mutation leaves every
# other assertion in those tables true -- empty mapping, a reason present, CANNOT-TELL, exit 2,
# assessable 0 -- and changes only WHICH guard refused. Nothing but the guard assertion can
# catch it, which is the countermeasure to this module's systematic vacuity: several fail-closed
# guards all satisfy a broad 'it refused', so the guard under test never has to be the reason.
MUTATIONS.update(
    {
        "guard-column-is-load-bearing-seam": '\nimport tableau_view_types as vt\n# Every fail-closed guard replaced by ONE generic refusal. Still refuses, still empty, still a\n# reason -- only the REASON\'s identity is lost.\nvt.parse_payload = lambda payload: ({}, "metadata api response refused")\n',
        "guard-column-is-load-bearing-census": '\nimport tableau_view_types as vt\n# Every fail-closed guard replaced by ONE generic refusal. Still refuses, still empty, still a\n# reason -- only the REASON\'s identity is lost.\nvt.parse_payload = lambda payload: ({}, "metadata api response refused")\n',
    }
)
CENSUS_MUTATIONS.add("guard-column-is-load-bearing-census")
# ⚠️ Round 5: the transport layer, and the meta-mutation extended to it. `census-does-its-own-
# transport-again` is the original defect reproduced -- a `fetch_payload` that skips the ceiling,
# the status check and the request-exception handling, exactly as the census's own copy did.
MUTATIONS.update(
    {
        "guard-column-is-load-bearing-transport": '\nimport tableau_view_types as vt\n_orig = vt.fetch_payload\ndef fetch_payload(session):\n    payload, reason = _orig(session)\n    return payload, ("metadata api response refused" if reason else None)\nvt.fetch_payload = fetch_payload\n',
        "census-ignores-the-transport-refusal": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'    if refusal:\'\n_new = \'    if False:\'\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
        # ⚠️ CENSUS-ONLY, and the scope IS the point. The first version replaced
        # `tableau_view_types.fetch_payload`, which is the transport for BOTH arms -- so it did
        # not recreate the divergence (shipped correct, census weaker), it degraded both
        # equally. Measured: the shipped parser then typed one view from the oversized body and
        # refused nothing, so the parity test went red on its FIRST assertion (`the shipped
        # parser must refuse this`) rather than on census drift -- red for a real reason, but
        # not its own. This edits the census's CALL SITE and leaves `fetch_payload` untouched:
        # measured, the shipped arm still refuses while the census reports NOT-PRESENT, exit 0.
        "census-does-its-own-transport-again": '\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding="utf-8").read()\n_old = \'        payload, refusal = tableau_view_types.fetch_payload(session)\'\n_new = (\n    \'        _s, _b, _h = session._request("POST", "/graphql",\'\n    \' body={"query": tableau_view_types.VIEW_TYPE_QUERY}, api="metadata")\\n\'\n    \'        payload, refusal = json.loads(_b.decode("utf-8", "replace")), None\'\n)\nassert _old in _src, "mutation anchor not found - the snippet is stale, not the code"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, "exec"), census.__dict__)\n',
    }
)
CENSUS_MUTATIONS.update(
    {
        "guard-column-is-load-bearing-transport",
        "census-ignores-the-transport-refusal",
        "census-does-its-own-transport-again",
    }
)
# ⚠️ Proves the request-count test can fail. It is the ONLY guard on the script's
# 'read-only, ONE query' claim against a credentialed site-wide endpoint, and until it drove
# `main()` it measured only the requests the SEAM made -- a second call anywhere else in `main`
# was invisible to it. This mutation adds exactly that.
MUTATIONS["census-makes-a-second-request"] = (
    "\nimport tableau_luid_census as census\n_src = open(census.__file__, encoding=\"utf-8\").read()\n_old = '        payload, refusal = tableau_view_types.fetch_payload(session)'\n_new = (\n    '        tableau_view_types.fetch_payload(session)\\n'\n    '        payload, refusal = tableau_view_types.fetch_payload(session)'\n)\nassert _old in _src, \"mutation anchor not found - the snippet is stale, not the code\"\nexec(compile(_src.replace(_old, _new, 1), census.__file__, \"exec\"), census.__dict__)\n"
)
CENSUS_MUTATIONS.add("census-makes-a-second-request")
INTENDED["census-makes-a-second-request"] = (
    "tests/test_tableau_luid_census.py::test_the_census_makes_exactly_one_request"
)

INTENDED.update(
    {
        "guard-column-is-load-bearing-transport": "tests/test_tableau_luid_census.py::test_the_census_and_the_shipped_parser_agree_on_the_same_bytes",
        "census-ignores-the-transport-refusal": "tests/test_tableau_luid_census.py::test_the_census_and_the_shipped_parser_agree_on_the_same_bytes",
        "census-does-its-own-transport-again": "tests/test_tableau_luid_census.py::test_the_census_and_the_shipped_parser_agree_on_the_same_bytes",
    }
)

INTENDED.update(
    {
        "guard-column-is-load-bearing-seam": "tests/test_capture_tableau_oracle.py::test_parse_payload_accepts_arbitrary_decoded_json",
        "guard-column-is-load-bearing-census": "tests/test_tableau_luid_census.py::test_a_malformed_envelope_cannot_bypass_the_guarantees",
    }
)

INTENDED.update(
    {
        "census-label-allowlist-removed": "tests/test_tableau_luid_census.py::test_emit_refuses_a_label_this_module_did_not_author",
        "census-label-allowlist-case-folded": "tests/test_tableau_luid_census.py::test_emit_refuses_a_label_this_module_did_not_author",
        "census-label-refusal-echoes-the-label": "tests/test_tableau_luid_census.py::test_the_label_refusal_does_not_echo_the_label_it_rejected",
    }
)
INTENDED.update(
    {
        "seam-requires-a-dict-envelope-again": "tests/test_capture_tableau_oracle.py::test_parse_payload_accepts_arbitrary_decoded_json",
        "census-envelope-assumed-to-be-a-dict-again": "tests/test_tableau_luid_census.py::test_census_is_total_over_arbitrary_decoded_json",
        "census-envelope-flag-always-readable": "tests/test_tableau_luid_census.py::test_census_is_total_over_arbitrary_decoded_json",
        "census-envelope-flag-always-unreadable": "tests/test_tableau_luid_census.py::test_a_readable_envelope_says_so",
        "census-assessable-drops-the-envelope-clause": "tests/test_tableau_luid_census.py::test_each_unassessable_route_is_pinned_independently",
        "census-assessable-drops-the-collection-clause": "tests/test_tableau_luid_census.py::test_each_unassessable_route_is_pinned_independently",
        "census-assessable-drops-the-refusal-clause": "tests/test_tableau_luid_census.py::test_each_unassessable_route_is_pinned_independently",
    }
)

CENSUS_MUTATIONS.update(
    {
        "census-label-allowlist-removed",
        "census-exit-code-always-ok",
        "census-json-drops-the-assessable-flag",
        "cosmetic-census-comment-reworded",
        "census-ignores-an-unreadable-workbook",
        "census-verdict-ignores-the-parser-refusal",
        "census-label-allowlist-case-folded",
        "absent-anchor-stale-census-text",
        "census-unreadable-collection-crashes-again",
        "census-label-refusal-echoes-the-label",
    }
)


def main(argv: list[str]) -> int:
    """Run the campaign. Exit 0 only when every mutation lands on its EXPECTED verdict."""
    wanted = argv or list(MUTATIONS)
    for suite in (TARGET, CENSUS_TARGET):
        baseline = mh.subprocess.run(
            [mh.PY, "-m", "pytest", suite, "-q", "--no-header", "--color=no"],
            cwd=mh.ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=mh.sanitized_env(),
        )
        print(f"BASELINE {suite:38s} exit={baseline.returncode}  {mh.last_line(baseline)}")
        if baseline.returncode != 0:
            # A mutation is only evidence against a clean baseline: an already-failing test would be
            # credited to every mutation that follows it.
            print("HARNESS ERROR: baseline is not clean, so no mutation verdict is trustworthy")
            return 2

    bad: list[str] = []
    count = 0
    for name in wanted:
        # An intended anchor wins: the named test alone is then the sole reason to refuse.
        suite = INTENDED.get(name) or (CENSUS_TARGET if name in CENSUS_MUTATIONS else TARGET)
        try:
            label, _rc, detail, outcomes = mh.run(name, MUTATIONS[name], suite)
        except SystemExit as exc:
            verdict, label, detail = "INVALID ", name, str(exc)
        else:
            if mh.observed_mutation(outcomes):
                verdict = "CAUGHT  " if outcomes["call_failed"] else "CAUGHT* "
            elif mh.session_is_trustworthy(outcomes):
                verdict = "SURVIVED"
            else:
                verdict = "INVALID "
        count += 1
        got = verdict.strip()
        if name.startswith("cosmetic-"):
            expected = "SURVIVED"
        elif name.startswith("absent-anchor-"):
            expected = "INVALID"
        else:
            expected = "CAUGHT"
        if not got.startswith(expected):
            bad.append(f"{name}: expected {expected}, got {got} -- {detail}")
        print(f"{verdict}  {label:42s} -> {detail}")

    print()
    if bad:
        print("MUTATION FAILURES:")
        for item in bad:
            print(f"  {item}")
        return 1
    print(f"all {count} mutations landed on their expected verdict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
