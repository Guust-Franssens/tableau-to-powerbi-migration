"""Mutation harness: prove the offline suite's assertions can actually fail.

    python tests/mutation_harness.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest. Each mutation is a snippet
that monkeypatches the REAL deployer (or the mock) at interpreter start, injected as a pytest plugin
into a subprocess; then the relevant suite is re-run. A mutation that produces a GREEN run is a HOLE
in the tests, not a success.

Read ``docs/offline-mock-harness.md`` for the result table and for the two holes this found.

One trap is guarded explicitly: if the injected plugin fails to import, pytest exits non-zero before
running a single test and a naive harness scores that as CAUGHT. The first run of this file reported
22/22 caught for exactly that reason. ``run()`` raises rather than reporting a false green.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# name -> a conftest-style patch injected via a sitecustomize-like plugin file
MUTATIONS = {
    "no-dedup-landing-claim": """
import deploy_estate as de
# Pretend the workspace is always empty: every run creates fresh items.
de.Landing.claim = lambda self, item, journal: (None, None)
""",
    "bind-report-to-wrong-model": """
import deploy_estate as de
_orig = de.rebind
def rebind(parts, workspace_name, model_name, model_id):
    return _orig(parts, workspace_name, model_name, "00000000-0000-0000-0000-000000000000")
de.rebind = rebind
""",
    "reports-before-models": """
import deploy_estate as de
_orig = de.discover
def discover(bundle):
    # Swap model/report so reports are deployed first.
    return [(n, r or m, m) for n, m, r in _orig(bundle)]
de.discover = discover
""",
    "flatten-the-folder-tree": """
import deploy_estate as de
de.project_parents = lambda estate_db: {}
""",
    "drop-the-provenance-stamp": """
import deploy_estate as de
de.stamp_for = lambda item: ""
""",
    "listing-failure-means-empty": """
import deploy_estate as de
_orig = de.Landing.read
def read(cls, workspace, tok, adopt=False):
    landing, why = _orig(workspace, tok, adopt)
    return (landing or cls([], adopt)), ""
de.Landing.read = classmethod(read)
""",
    "no-empty-report-skip": """
import deploy_estate as de
de.report_is_empty = lambda folder: False
""",
    "skip-the-folder-sanitiser": """
import deploy_estate as de
de.folder_display_name = lambda name: name
""",
    "mock-omits-nothing-folderid-null-at-root": """
import mocks.fabric as mf
_orig = mf.Item.row
@property
def row(self):
    out = dict(_orig.fget(self))
    out.setdefault("folderId", None)
    return out
mf.Item.row = row
""",
    "mock-rejects-duplicate-names": """
import mocks.fabric as mf
_orig = mf.FabricService._create_item
def create(self, body):
    name, kind = body.get("displayName"), body.get("type")
    if any(i.display_name == name and i.item_type == kind for i in self.items.values()):
        return mf._error(400, "ItemDisplayNameAlreadyInUse", "already in use")
    return _orig(self, body)
mf.FabricService._create_item = create
""",
    "mock-accepts-bypath-reports": """
import mocks.fabric as mf
mf.FabricService._reject_report = lambda self, by_path: None
""",
    "mock-coerces-bad-folder-names": """
import mocks.fabric as mf
mf.FabricService._reject_folder = lambda self, name, parent: None
""",
    "mock-ignores-the-folder-depth-limit": """
import mocks.fabric as mf
mf.MAX_FOLDER_DEPTH = 100
""",
    "mock-never-throttles": """
import mocks.fabric as mf
mf.FabricService.throttle = lambda self, **kw: None
""",
    "mock-updatedefinition-also-moves-and-restamps": """
import mocks.fabric as mf
_orig = mf.FabricService._update_definition
def upd(self, item, body):
    out = _orig(self, item, body)
    item.description = None
    item.folder_id = None
    return out
mf.FabricService._update_definition = upd
""",
    "mock-forgets-the-item-limit": """
import mocks.fabric as mf
mf.WORKSPACE_ITEM_LIMIT = 10 ** 9
_orig = mf.FabricService.__init__
def init(self, **kw):
    kw.setdefault("item_limit", 10 ** 9)
    _orig(self, **kw)
mf.FabricService.__init__ = init
""",
    "tableau-mock-accepts-any-pat-secret": """
import mocks.tableau as mt
_orig = mt.TableauSite._signin
def signin(self, body):
    import json, uuid
    creds = (json.loads(body or b"{}").get("credentials") or {})
    if (creds.get("site") or {}).get("contentUrl") != self.content_url:
        return _orig(self, body)
    token = str(uuid.uuid4())
    self.tokens.add(token)
    return self._json(200, {"credentials": {"token": token,
        "site": {"id": self.site_id, "contentUrl": self.content_url}, "user": {"id": "user-1"}}})
mt.TableauSite._signin = signin
""",
    "tableau-mock-returns-empty-graphql-instead-of-errors": """
import mocks.tableau as mt
_orig = mt.TableauSite._graphql
def gql(self, body):
    out = _orig(self, body)
    import json
    payload = json.loads(out[2])
    if payload.get("errors"):
        return self._json(200, {"data": {}})
    return out
mt.TableauSite._graphql = gql
""",
    "tableau-mock-always-sends-usage-statistics": """
import mocks.tableau as mt
_orig = mt.TableauSite._collection
def coll(self, collection, query):
    if collection == "views":
        query = dict(query, includeUsageStatistics=["true"])
    return _orig(self, collection, query)
mt.TableauSite._collection = coll
""",
    "tableau-mock-uses-the-standard-content-disposition": """
import mocks.tableau as mt
_orig = mt.TableauSite._content
def content(self, collection, luid):
    status, headers, payload = _orig(self, collection, luid)
    if "Content-Disposition" in headers:
        headers["Content-Disposition"] = headers["Content-Disposition"].replace("name=", "filename=")
    return status, headers, payload
mt.TableauSite._content = content
""",
    "tableau-mock-emits-integer-pagination": """
import mocks.tableau as mt
import json
_orig = mt.TableauSite._page
def page(self, rows, collection, item, query):
    status, headers, payload = _orig(self, rows, collection, item, query)
    body = json.loads(payload)
    body["pagination"] = {k: int(v) for k, v in body["pagination"].items()}
    return status, headers, json.dumps(body).encode()
mt.TableauSite._page = page
""",
    "engine-stand-in-emits-no-pages": """
import mocks.estate as me
me._FAKE_ENGINE = me._FAKE_ENGINE.replace('"pageOrder": pages or []', '"pageOrder": []')
""",
}


OUTCOME_HOOKS = """

# --- appended by mutation_harness: structured lifecycle recording ------------------
# Terminal text cannot distinguish "a test observed the mutation" from "pytest failed to
# run". A collection error on a CLASS emits `ERROR path::TestName`, and a dying xdist
# worker emits `FAILED path::test_name` for a test that never executed - both parse as a
# named outcome. So record the real lifecycle instead of scraping the summary.
#
# The record must answer "did a COMPLETE session observe this", not merely "did the plugin
# load". An empty record written at import time is evidence of nothing: measured,
# `pytest.exit(returncode=0)` from `pytest_sessionstart` produced exit 0 with a valid empty
# record and no tests at all, and that scored SURVIVED.
import json as _json
from pathlib import Path as _Path

import pytest as _pytest

_OUTCOMES = _Path(r"{outcome_path}")
_RECORD = {{
    "call_failed": [],
    "setup_failed": [],
    "collect_error": [],
    "internal_error": False,
    "node_down": False,
    "session_finished": False,
    "exitstatus": None,
    "saw_call_phase": False,
}}


def _flush():
    _OUTCOMES.write_text(_json.dumps(_RECORD), encoding="utf-8")


def pytest_runtest_logreport(report):
    # `saw_call_phase` and not merely "a report happened": a setup-phase report is emitted
    # even when no test BODY runs, so `pytest.exit()` from `pytest_runtest_call` produced a
    # finished session with a report and exit 0 while stdout said `no tests ran`.
    if report.when == "call":
        _RECORD["saw_call_phase"] = True
    if report.outcome == "failed":
        # `when` is the discriminator terminal text throws away.
        name = report.nodeid.split("::")[-1]
        _RECORD["call_failed" if report.when == "call" else "setup_failed"].append(name)
    _flush()


def pytest_collectreport(report):
    if report.outcome == "failed":
        _RECORD["collect_error"].append(report.nodeid or "<root>")
        _flush()


def pytest_internalerror(excrepr, excinfo):
    _RECORD["internal_error"] = True
    _flush()


@_pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    # xdist-only, and it MUST be declared optional: without xdist installed pytest rejects
    # the whole plugin with `PluginValidationError: unknown hook 'pytest_testnodedown'`,
    # which made every serial mutation unusable in that environment.
    if error is not None:
        _RECORD["node_down"] = True
        _flush()


def pytest_sessionfinish(session, exitstatus):
    _RECORD["session_finished"] = True
    _RECORD["exitstatus"] = int(exitstatus)
    _flush()


_flush()
"""

# pytest returns 0 (all passed) or 1 (tests failed) when a session ran to a verdict. Every
# other code means something happened TO the run: 2 interrupted, 3 internal error, 4 usage
# error, 5 nothing collected. An outcome recorded alongside one of those is not a verdict --
# measured, a call failure followed by KeyboardInterrupt in teardown exits 2 with
# `call_failed` populated, and the previous ordering reported CAUGHT.
VERDICT_BEARING_EXITS = frozenset({0, 1})


def sanitized_env() -> dict:
    """The ONE environment both baseline and mutation runs use.

    They must be identical or the comparison is meaningless. Measured: baselines inherited
    ``PYTEST_ADDOPTS`` while mutation runs stripped it, so ``PYTEST_ADDOPTS=--collect-only``
    made the baseline exit 0 having executed **no test at all** (``18 tests collected``),
    after which every mutation ran the real suite and could be credited with a pre-existing
    failure.

    ``PY_COLORS`` is stripped for a different reason: ANSI prefixes on the summary tokens
    silently turned real detections into harness errors while the verdict was text-based.
    """
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT / "scripts")]),
    }
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PY_COLORS", None)
    return env


def run(name: str, code: str, target: str) -> tuple[str, int, str, dict]:
    """Apply one mutation and report ``(name, exit_code, detail, outcomes)``.

    ``outcomes`` is the load-bearing return value, and it comes from pytest's own lifecycle
    hooks rather than its terminal output. Three measured reasons text parsing is not enough:

    * a non-zero exit alone means nothing -- ``pytest tests/does_not_exist.py`` exits 4 having
      run no test, and the pre-fix verdict scored it CAUGHT;
    * a **collection** failure on a class emits ``ERROR path::TestName``, which looks exactly
      like a named test error;
    * a dying **xdist** worker emits ``FAILED path::test_name`` for a test that never executed.

    ``--color=no`` and a scrubbed ``PYTEST_ADDOPTS`` are belt-and-braces: with ``PY_COLORS=1``
    the summary tokens carry ANSI prefixes, which silently turned real detections into
    harness errors.
    """
    plugin = ROOT / "tests" / "_mutation_plugin.py"
    outcomes_file = ROOT / "tests" / "_mutation_outcomes.json"
    outcomes_file.unlink(missing_ok=True)
    plugin.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, r'{ROOT / 'scripts'}')\nsys.path.insert(0, r'{ROOT / 'tests'}')\n"
        + code
        + OUTCOME_HOOKS.format(outcome_path=str(outcomes_file)),
        encoding="utf-8",
    )
    env = sanitized_env()
    proc = subprocess.run(
        [PY, "-m", "pytest", target, "-q", "-p", "_mutation_plugin", "--no-header", "-x", "--tb=no", "--color=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    plugin.unlink(missing_ok=True)
    if "Error importing plugin" in proc.stdout + proc.stderr:
        outcomes_file.unlink(missing_ok=True)
        raise SystemExit(f"{name}: the mutation never applied - the harness would report a FALSE 'CAUGHT'")
    outcomes = read_outcomes(outcomes_file)
    outcomes_file.unlink(missing_ok=True)
    detail = describe(proc, outcomes)
    return name, proc.returncode, detail, outcomes


def read_outcomes(path: Path) -> dict:
    """Load the plugin's record. A missing or unreadable file is itself a harness error."""
    empty = {
        "call_failed": [],
        "setup_failed": [],
        "collect_error": [],
        "internal_error": False,
        "node_down": False,
        "session_finished": False,
        "exitstatus": None,
        "saw_report": False,
        "recorded": False,
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    loaded["recorded"] = True
    return loaded


def observed_mutation(outcomes: dict) -> bool:
    """True when a NAMED test genuinely observed the mutation.

    Detection evidence is **durable**: a test that failed in its ``call`` phase noticed the
    mutation, and nothing that happens afterwards un-notices it. Round-3 review measured the
    cost of the opposite rule -- a real assertion failure followed by ``KeyboardInterrupt`` in
    teardown exits 2, and erasing the detection turned a genuine CAUGHT into a HARNESS-ERROR.

    ``node_down`` is the exception and not an inconsistency: a dying xdist worker emits
    ``FAILED path::test_name`` for a test that **never executed**, so that record is synthetic
    rather than observed.
    """
    if outcomes.get("node_down"):
        return False
    return bool(outcomes.get("call_failed") or outcomes.get("setup_failed"))


def session_is_trustworthy(outcomes: dict) -> bool:
    """True when a COMPLETE, coherent session ran to a verdict.

    Required before concluding SURVIVED, because absence of evidence is only evidence of
    absence when the run actually finished. Three measured shapes that pass a naive check:

    * ``pytest.exit(returncode=0)`` from ``pytest_sessionstart`` -- exit 0, a valid record
      written at plugin import, no tests at all;
    * the same from ``pytest_runtest_call`` -- ``session_finished``, ``exitstatus=0`` and a
      setup-phase report present, while stdout says ``no tests ran``;
    * a call failure then ``KeyboardInterrupt`` -- exit 2, session incoherent.

    Hence ``saw_call_phase``: a test BODY must have run, not merely a setup report.
    """
    return (
        bool(outcomes.get("recorded"))
        and bool(outcomes.get("session_finished"))
        and bool(outcomes.get("saw_call_phase"))
        and outcomes.get("exitstatus") in VERDICT_BEARING_EXITS
        and not outcomes.get("collect_error")
        and not outcomes.get("internal_error", False)
        and not outcomes.get("node_down", False)
    )


def session_ended_abnormally(outcomes: dict) -> bool:
    """True when something happened TO the run, as opposed to the run reaching a verdict.

    Deliberately narrower than ``not session_is_trustworthy``: a session in which every test
    errored in **setup** has no call phase and is therefore not "trustworthy" for concluding
    SURVIVED -- but nothing went wrong with it, and annotating that as abnormal would fire on
    every legitimate ``CAUGHT*`` and train the reader to ignore the warning.
    """
    return (
        not outcomes.get("recorded")
        or not outcomes.get("session_finished")
        or outcomes.get("exitstatus") not in VERDICT_BEARING_EXITS
        or bool(outcomes.get("collect_error"))
        or outcomes.get("internal_error", False)
        or outcomes.get("node_down", False)
    )


def is_harness_error(outcomes: dict) -> bool:
    """True when there is neither a detection nor a trustworthy session.

    The asymmetry is deliberate: **detection is durable, absence is not.** A partial run can
    prove a mutation was caught; only a complete one can prove it survived.
    """
    return not observed_mutation(outcomes) and not session_is_trustworthy(outcomes)


def describe(proc: subprocess.CompletedProcess, outcomes: dict) -> str:
    """One-line detail for the report, preferring the structured record."""
    if outcomes.get("call_failed"):
        return outcomes["call_failed"][0]
    if outcomes.get("setup_failed"):
        return f"{outcomes['setup_failed'][0]} (errored, did not assert)"
    if outcomes.get("collect_error"):
        return f"collection error: {outcomes['collect_error'][0]}"
    if outcomes.get("internal_error"):
        return "pytest internal error"
    if outcomes.get("node_down"):
        return "xdist worker died"
    if not outcomes.get("recorded"):
        return "no lifecycle record written - pytest never started"
    return last_line(proc)


def last_line(proc: subprocess.CompletedProcess) -> str:
    """Diagnostic text from wherever pytest actually wrote it.

    Indexing stdout unconditionally raised IndexError when a usage/plugin error left stdout
    empty, so the baseline precondition crashed instead of reporting a harness error.
    """
    lines = (proc.stdout.strip() or proc.stderr.strip() or "(no output)").splitlines()
    return lines[-1][:120] if lines else "(no output)"


def named_outcomes(stdout: str) -> tuple[list[str], list[str]]:
    """Parse pytest's terminal summary. RETAINED FOR DIAGNOSTICS ONLY -- not a verdict source.

    Blind review of PR #409 established that this cannot decide anything: a **collection**
    failure on a class emits ``ERROR path::TestName`` and a dying **xdist** worker emits
    ``FAILED path::test_name`` for a test that never ran, so both shapes are indistinguishable
    here from a real observation. ANSI colouring also defeats the ``startswith`` entirely.
    The verdict comes from ``read_outcomes`` / ``is_harness_error`` instead.
    """
    failed, errored = [], []
    for line in stdout.splitlines():
        if line.startswith("FAILED"):
            failed.append(line.split("::")[-1].split(" ")[0])
        elif line.startswith("ERROR ") and "::" in line:
            errored.append(line.split("::")[-1].split(" ")[0])
    return failed, errored


def named_failures(stdout: str) -> list[str]:
    """Test names pytest reported as FAILED. Kept for callers that only want assertions."""
    return named_outcomes(stdout)[0]


def main() -> int:
    targets = ["tests/test_e2e_offline.py", "tests/test_mock_fabric.py", "tests/test_mock_tableau.py"]
    dirty = []
    for target in targets:
        baseline = subprocess.run(
            [PY, "-m", "pytest", target, "-q", "--no-header", "--color=no"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=sanitized_env(),
        )
        print(f"BASELINE {target:32s} exit={baseline.returncode}  {last_line(baseline)}")
        if baseline.returncode != 0:
            dirty.append(f"{target} (exit {baseline.returncode})")
    if dirty:
        # A mutation is only evidence against a clean baseline: an already-failing test would be
        # credited to every mutation that follows it.
        print("\nHARNESS ERROR: baseline is not clean, so no mutation verdict is trustworthy:")
        for item in dirty:
            print(f"  {item}")
        return 2
    print()
    survivors, harness_errors = [], []
    for name, code in MUTATIONS.items():
        if name.startswith("mock-"):
            target = "tests/test_mock_fabric.py"
        elif name.startswith("tableau-mock-"):
            target = "tests/test_mock_tableau.py"
        else:
            target = "tests/test_e2e_offline.py"
        label, rc, detail, outcomes = run(name, code, target)
        if observed_mutation(outcomes):
            # Detection is durable. A real call-phase failure noticed the mutation even if the
            # session later fell over; only a synthetic `node_down` record is excluded.
            verdict = "CAUGHT  " if outcomes["call_failed"] else "CAUGHT* "
            if session_ended_abnormally(outcomes):
                detail = f"{detail} [session ended abnormally: exit {rc}]"
        elif session_is_trustworthy(outcomes):
            verdict = "SURVIVED"
            survivors.append(label)
        else:
            # Neither a detection nor a complete session: pytest never reached a verdict.
            verdict = "HARNESS-ERROR"
            harness_errors.append(f"{label} (exit {rc}, {detail})")
        print(f"{verdict}  {label:52s} -> {detail}")
    print()
    print("survivors (holes in the suite):", survivors or "none")
    print("CAUGHT* = a named test ERRORED rather than asserting; weaker coverage, still observed")
    if harness_errors:
        print("HARNESS ERRORS (no verdict - pytest never ran the tests):")
        for item in harness_errors:
            print(f"  {item}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
