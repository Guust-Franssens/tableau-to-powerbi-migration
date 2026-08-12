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


def run(name: str, code: str, target: str) -> tuple[str, int, str]:
    plugin = ROOT / "tests" / "_mutation_plugin.py"
    plugin.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, r'{ROOT / 'scripts'}')\nsys.path.insert(0, r'{ROOT / 'tests'}')\n" + code,
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PY, "-m", "pytest", target, "-q", "-p", "_mutation_plugin", "--no-header", "-x", "--tb=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT / "scripts")]),
        },
    )
    plugin.unlink(missing_ok=True)
    if "Error importing plugin" in proc.stdout + proc.stderr:
        raise SystemExit(f"{name}: the mutation never applied - the harness would report a FALSE 'CAUGHT'")
    caught = [line.split("::")[-1].split(" ")[0] for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    if caught:
        return name, proc.returncode, caught[0]
    lines = (proc.stdout.strip() or proc.stderr.strip() or "(no output)").splitlines()
    return name, proc.returncode, lines[-1][:120]


def main() -> int:
    targets = ["tests/test_e2e_offline.py", "tests/test_mock_fabric.py", "tests/test_mock_tableau.py"]
    for target in targets:
        baseline = subprocess.run(
            [PY, "-m", "pytest", target, "-q", "--no-header"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        print(f"BASELINE {target:32s} exit={baseline.returncode}  {baseline.stdout.strip().splitlines()[-1]}")
    print()
    survivors = []
    for name, code in MUTATIONS.items():
        if name.startswith("mock-"):
            target = "tests/test_mock_fabric.py"
        elif name.startswith("tableau-mock-"):
            target = "tests/test_mock_tableau.py"
        else:
            target = "tests/test_e2e_offline.py"
        label, rc, detail = run(name, code, target)
        verdict = "CAUGHT " if rc != 0 else "SURVIVED"
        if rc == 0:
            survivors.append(label)
        print(f"{verdict}  {label:52s} -> {detail}")
    print()
    print("survivors (holes in the suite):", survivors or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
