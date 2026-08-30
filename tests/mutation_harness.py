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
import json as _json
from pathlib import Path as _Path

_OUTCOMES = _Path(r"{outcome_path}")
_RECORD = {{"call_failed": [], "setup_failed": [], "collect_error": [], "internal_error": False,
            "node_down": False}}


def _flush():
    _OUTCOMES.write_text(_json.dumps(_RECORD), encoding="utf-8")


def pytest_runtest_logreport(report):
    if report.outcome != "failed":
        return
    name = report.nodeid.split("::")[-1]
    # `when` is the discriminator terminal text throws away.
    ("call_failed" if report.when == "call" else "setup_failed")
    _RECORD["call_failed" if report.when == "call" else "setup_failed"].append(name)
    _flush()


def pytest_collectreport(report):
    if report.outcome == "failed":
        _RECORD["collect_error"].append(report.nodeid or "<root>")
        _flush()


def pytest_internalerror(excrepr, excinfo):
    _RECORD["internal_error"] = True
    _flush()


def pytest_testnodedown(node, error):
    if error is not None:
        _RECORD["node_down"] = True
        _flush()


_flush()
"""


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
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT / "scripts")]),
    }
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PY_COLORS", None)
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
        "recorded": False,
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    loaded["recorded"] = True
    return loaded


def is_harness_error(outcomes: dict) -> bool:
    """True when pytest failed to run rather than a test observing anything."""
    return (
        not outcomes.get("recorded")
        or bool(outcomes.get("collect_error"))
        or outcomes.get("internal_error", False)
        or outcomes.get("node_down", False)
    )


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
            [PY, "-m", "pytest", target, "-q", "--no-header"], cwd=ROOT, capture_output=True, text=True, check=False
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
        if is_harness_error(outcomes):
            # Collection error, internal error, dead xdist worker, or no record at all.
            # pytest never ran the tests, so there is no verdict to give.
            verdict = "HARNESS-ERROR"
            harness_errors.append(f"{label} (exit {rc}, {detail})")
        elif outcomes["call_failed"]:
            verdict = "CAUGHT  "
        elif outcomes["setup_failed"]:
            # Observed, but by a setup/teardown crash rather than an assertion. Credited and
            # marked, because a test that only errors is weaker coverage than one that asserts.
            verdict = "CAUGHT* "
        elif rc == 0:
            verdict = "SURVIVED"
            survivors.append(label)
        else:
            verdict = "HARNESS-ERROR"
            harness_errors.append(f"{label} (exit {rc}, no test outcome recorded)")
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
