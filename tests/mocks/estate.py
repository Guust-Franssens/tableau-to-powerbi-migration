"""A synthetic Tableau estate built from this repo's REAL fixtures, plus a stand-in for the engine.

Two pieces, both of which exist so an end-to-end rehearsal does real work on real bytes:

:func:`build_site`
    A :class:`~tests.mocks.tableau.TableauSite` populated from ``tests/fixtures/*.twb`` - a nested
    project tree, a published data source shared by two workbooks, usage counters, groups and grants.
    Nothing here is invented Tableau XML; the workbooks are the ones the parser suite already uses.

:func:`install_fake_engine`
    A stand-in for the deterministic engine's ``migrate_estate.py``, laid out at the path
    ``run_estate.py`` looks for (``<engine>/skills/tableau-migration/scripts/``), so ``run_estate``
    itself runs unmodified: its definition-of-done gate, approval-collision check, handover slicing
    and manifest all execute for real.

    **This is a stand-in, not a mock of measured behaviour.** The real engine is a ~200-file plugin
    that is not installed in CI, and reproducing its PBIP output is not something this harness can
    claim to do faithfully. What it reproduces is the *contract* ``run_estate`` and ``deploy_estate``
    consume: ``report.json`` with a ``definition_of_done``, and ``pbip/<workbook>/<name>.SemanticModel``
    + a schema-valid ``<name>.Report`` with a ``byPath`` dataset reference where the workbook has
    convertible pages. It parses each workbook with THIS repo's real parser, so the count of pages it
    emits is derived from the workbook, not hard-coded.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

from .tableau import TableauSite

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def build_site(**kwargs) -> TableauSite:
    """A small estate with the shapes that have historically broken this pipeline.

    * a NESTED project tree (``Finance/Q1.2026``), including a name Fabric will not accept as a
      folder - the dot is rejected anywhere in a Fabric folder name;
    * a project name (``R&D``) that sanitises into the same Fabric name as another (``R/D`` would),
      which is where a silent content merge used to happen;
    * a published data source that TWO workbooks depend on, so migration ORDER matters;
    * a workbook with no deliberate-use signal at all, so tiering has something to tier.
    """
    site = TableauSite(**kwargs)
    finance = site.project("Finance")
    quarter = site.project("Q1.2026", parent=finance)
    research = site.project("R&D")

    shared = site.datasource(
        "Corporate Cities",
        finance,
        FIXTURES / "standalone_datasource.tds",
        is_certified=True,
        has_extracts=True,
        extract_last_refresh="2026-08-01T00:00:00Z",
    )

    sales = site.workbook("Sales Review", quarter, FIXTURES / "minimal.twb", views=2, usage=120)
    ops = site.workbook("Ops Dashboard", research, FIXTURES / "federated_multi_connection.twb", views=1, usage=40)
    site.workbook("Attic Copy", finance, FIXTURES / "published_datasource.twb", views=1, usage=0)

    site.publish_dependency(sales, shared)
    site.publish_dependency(ops, shared)

    local = _group("All Users", "local", ["ana", "ben"])
    entra = _group("Entra Finance", "example.com", ["cara"])
    site.groups.extend([local, entra])
    site.grant(finance, local, "Read")
    site.grant(finance, entra, "Write")
    site.grant(sales, local, "ViewUnderlyingData")

    site.subscriptions.append({"id": "sub-1", "content": {"id": site.views[0].luid, "type": "View"}})
    site.alerts.append({"id": "alert-1", "view": {"id": site.views[0].luid}})
    return site


def _group(name: str, domain: str, members: list[str]):
    from .tableau import Group  # noqa: PLC0415  # local import keeps the public surface small

    return Group(luid=f"group-{name.lower().replace(' ', '-')}", name=name, domain=domain, members=members)


# --------------------------------------------------------------------------- the engine stand-in

_FAKE_ENGINE = '''\
"""A stand-in for the deterministic engine's migrate_estate.py: enough of its OUTPUT CONTRACT to run
`run_estate.py` offline. Not a fidelity claim about the engine - see tests/mocks/estate.py."""

import argparse
import json
import sys
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, {repo_scripts!r})

from parse_tableau import parse_workbook


def sanitise(name):
    """The engine renames a workbook into a filesystem-safe project name; so do we."""
    return "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()


class LocalFilesSource:
    """The selected-engine naming surface consumed by run_estate's path preflight."""

    def __init__(self, root):
        self.root = Path(root)

    def list_datasources(self):
        return sorted(list(self.root.glob("*.tds")) + list(self.root.glob("*.tdsx")))

    def list_workbooks(self):
        return sorted(list(self.root.glob("*.twb")) + list(self.root.glob("*.twbx")))

    @staticmethod
    def asset_name(asset_id):
        return Path(asset_id).stem


def _safe_folder(name, used):
    """Allocate names exactly as this stand-in's own output loop does."""
    base = sanitise(name) or "datasource"
    candidate = base
    index = 2
    while candidate.lower() in used:
        candidate = f"{{base}}_{{index}}"
        index += 1
    used.add(candidate.lower())
    return candidate


def emit(spec, out, name):
    """Write one workbook's PBIP: a model and a report bound to it BY PATH (Git-integration shape)."""
    project = out / "pbip" / name
    model = project / (name + ".SemanticModel")
    (model / "definition" / "tables").mkdir(parents=True, exist_ok=True)
    (model / "definition.pbism").write_text(
        json.dumps({{"version": "4.0", "settings": {{"qnaEnabled": True}}}}, indent=2), encoding="utf-8"
    )
    (model / "definition" / "model.tmdl").write_text("model Model\\n\\tculture: en-US\\n", encoding="utf-8")
    for source in spec.get("data_sources") or []:
        table = sanitise(source.get("name") or "Table")
        (model / "definition" / "tables" / (table + ".tmdl")).write_text(
            "table " + table + "\\n\\tcolumn Value\\n\\t\\tdataType: string\\n", encoding="utf-8"
        )

    pages = [f"page-{{index}}" for index, _worksheet in enumerate(spec.get("worksheets") or [], start=1)]
    if not pages:
        # A page-less report cannot pass the current PBIR structural gate or be deployed. The
        # model remains a valid migration result, while the deployer's empty-report rule is tested
        # directly in the joined suite.
        return 0

    report = project / (name + ".Report")
    (report / "definition" / "pages").mkdir(parents=True, exist_ok=True)
    (report / ".platform").write_text(
        json.dumps(
            {{
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
                "metadata": {{"type": "Report", "displayName": name}},
                "config": {{"version": "2.0", "logicalId": str(uuid.uuid5(uuid.NAMESPACE_URL, "mock/" + name))}},
            }},
            indent=2,
        ),
        encoding="utf-8",
    )
    (report / "definition.pbir").write_text(
        json.dumps(
            {{
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
                "version": "4.0",
                "datasetReference": {{"byPath": {{"path": "../" + name + ".SemanticModel"}}}},
            }},
            indent=2,
        ),
        encoding="utf-8",
    )
    (report / "definition" / "version.json").write_text(
        json.dumps(
            {{
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
                "version": "2.0.0",
            }},
            indent=2,
        ),
        encoding="utf-8",
    )
    (report / "definition" / "report.json").write_text(
        json.dumps(
            {{
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.0.0/schema.json",
                "themeCollection": {{}},
                "layoutOptimization": "None",
                "resourcePackages": [],
            }},
            indent=2,
        ),
        encoding="utf-8",
    )
    (report / "definition" / "pages" / "pages.json").write_text(
        json.dumps(
            {{
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
                "pageOrder": pages or [],
                "activePageName": pages[0] if pages else "",
            }},
            indent=2,
        ),
        encoding="utf-8",
    )
    for page in pages:
        (report / "definition" / "pages" / page).mkdir()
        (report / "definition" / "pages" / page / "page.json").write_text(
            json.dumps(
                {{
                    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.0.0/schema.json",
                    "name": page,
                    "displayName": page,
                    "displayOption": "FitToPage",
                    "height": 720,
                    "width": 1280,
                }},
                indent=2,
            ),
            encoding="utf-8",
        )
    (project / (name + ".pbip")).write_text(json.dumps({{"version": "1.0"}}), encoding="utf-8")
    return len(pages)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True, type=Path)
    ap.add_argument("-o", "--output", required=True, type=Path)
    ap.add_argument("--approved-dax", type=Path)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    workbooks, bound, failed = [], 0, 0
    local_source = LocalFilesSource(args.input)
    used_names = set()
    for source in local_source.list_datasources():
        _safe_folder(local_source.asset_name(source), used_names)
    for source in local_source.list_workbooks():
        name = _safe_folder(local_source.asset_name(source), used_names)
        try:
            spec = parse_workbook(source)
        except Exception as exc:  # a parse failure is a workbook-level error, never a batch abort
            workbooks.append({{"name": name, "error": str(exc)[:200]}})
            failed += 1
            continue
        pages = emit(spec, args.output, name)
        bound += 1 if pages else 0
        workbooks.append(
            {{
                "name": name,
                "bound_model": name,
                "source_file": source.name,
                "pages": pages,
                "model_translation_handoff": {{"requests": []}},
                "viz_fidelity": [],
            }}
        )

    report = {{
        "tool": "tableau-fabric-skills (mock stand-in)",
        "generated_at": "2026-08-12T00:00:00Z",
        "source": {{"kind": "folder", "root": str(args.input)}},
        "pending_gates": [],
        "definition_of_done": {{
            "applicable": True,
            "status": "failed" if failed else "pass",
            "reports_bound": bound,
            "reports_failed": failed,
            "reports_warned": 0,
            "workbooks_total": len(workbooks),
        }},
        "summary": {{"workbook_calcs_stubbed": 0, "visuals_warned": 0}},
        "workbooks": workbooks,
    }}
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.output / "input_manifest.json").write_text(
        json.dumps({{"inputs": [{{"name": w.get("source_file")}} for w in workbooks]}}, indent=2), encoding="utf-8"
    )
    print("[OK] mock engine wrote", len(workbooks), "workbook(s)")
    return 0  # the engine exits 0 even on a failed DoD - the whole reason run_estate.py exists


if __name__ == "__main__":
    raise SystemExit(main())
'''


def install_fake_engine(root: Path) -> Path:
    """Write the engine stand-in under ``root`` and return the path to pass as ``--engine``."""
    scripts = root / "skills" / "tableau-migration" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "migrate_estate.py").write_text(
        _FAKE_ENGINE.format(repo_scripts=str(REPO_ROOT / "scripts")), encoding="utf-8"
    )
    return root


def engine_scripts_dir() -> Path | None:
    """The REAL engine's scripts folder, or None when the plugin is not installed (e.g. in CI)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from harvest_estate_assets import engine_scripts_dir as resolve  # noqa: PLC0415

    return resolve()


def run_engine_script(script: str, argv: list[str], env: dict[str, str], *, timeout: int = 60) -> Any:
    """Run one ENGINE script with the two guards that turn a silent hang into a loud failure.

    MEASURED, and it cost 13 minutes of a real session: ``estate_survey.py`` resolves its PAT secret
    through ``credential_resolver.resolve_secret``, whose LAST layer is a ``getpass`` prompt. With
    ``TABLEAU_PAT_VALUE`` unset and a TTY attached it prints its prompt to the terminal and blocks
    forever - no output, no error, no exit. Our ``.env`` does not prevent it either: the bridge in
    ``tableau_env.engine_child_env`` only reaches an engine script *our Python* spawns.

    So this refuses to launch without the variable the engine actually reads, always passes
    ``--no-prompt``, and gives the child no stdin and a deadline. Any one of the three would have
    turned that hang into an error in seconds.
    """
    import subprocess  # noqa: PLC0415

    if not env.get("TABLEAU_PAT_VALUE"):
        raise SystemExit(
            f"refusing to run {script}: TABLEAU_PAT_VALUE is not set.\n"
            "  The engine reads the PAT secret under ITS name, not ours (TABLEAU_PAT_SECRET), and\n"
            "  without it the script falls through to a hidden getpass prompt and blocks forever."
        )
    scripts = engine_scripts_dir()
    if scripts is None:
        raise SystemExit("the deterministic engine is not installed on this machine")
    command = [sys.executable, str(scripts / script), *argv]
    if "--no-prompt" not in command:
        command.append("--no-prompt")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**env},
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


def harvest(site: TableauSite, out: Path, *, base_url: str = "", token: str = "") -> list[Path]:
    """Download every workbook and published data source to ``out/assets`` as REAL packaged bytes.

    The engine's ``fetch_tds.py`` is the production path (see ``scripts/harvest_estate_assets.py``);
    this is the same set of REST calls without the plugin dependency, so the offline chain still has
    a genuine download step whose output is what the parser then reads.
    """
    import urllib.request  # noqa: PLC0415

    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    jobs = [("workbooks", w.luid, w.name, w.extension) for w in site.workbooks]
    jobs += [("datasources", d.luid, d.name, ".tdsx") for d in site.datasources]
    for collection, luid, name, extension in jobs:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
        target = assets / f"{safe}{extension}"
        if base_url:
            url = f"{base_url}/api/{site.rest_version}/sites/{site.site_id}/{collection}/{luid}/content"
            request = urllib.request.Request(url, method="GET")
            request.add_header("X-Tableau-Auth", token or next(iter(site.tokens), ""))
            with urllib.request.urlopen(request, timeout=30) as response:
                target.write_bytes(response.read())
        else:
            status, _headers, payload = site.handle(
                "GET",
                f"http://127.0.0.1/api/{site.rest_version}/sites/{site.site_id}/{collection}/{luid}/content",
                {"x-tableau-auth": next(iter(site.tokens), "")},
                b"",
            )
            if status != 200:
                raise RuntimeError(f"download of {name} failed: HTTP {status}")
            target.write_bytes(payload)
        written.append(target)
    return written


def project_tree(site: TableauSite) -> dict[str, str]:
    """``workbook name -> "Parent/Child"`` project path, for asserting the mirrored folder tree."""
    by_luid = {p.luid: p for p in site.projects}
    out = {}
    for workbook in site.workbooks:
        parts, node = [], workbook.project
        while node is not None:
            parts.append(node.name)
            node = by_luid.get(node.parent_luid or "")
        out[workbook.name] = "/".join(reversed(parts))
    return out


def write_estate_db(site: TableauSite, path: Path) -> Path:
    """A minimal ``estate.db`` in ``assess_estate``'s schema, for tools that read it directly.

    Prefer running the real ``assess_estate.py`` against the mock site (the E2E does); this exists
    for tests that need the db without paying for the whole assessment.
    """
    import sqlite3  # noqa: PLC0415

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from assess_estate import SCHEMA  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO project VALUES (?,?,?,?)",
        [(p.luid, p.name, p.parent_luid, p.content_permissions) for p in site.projects],
    )
    connection.executemany(
        "INSERT INTO workbook (luid, name, project) VALUES (?,?,?)",
        [(w.luid, w.name, w.project.name) for w in site.workbooks],
    )
    connection.executemany(
        "INSERT INTO datasource (luid, name, project) VALUES (?,?,?)",
        [(d.luid, d.name, d.project.name) for d in site.datasources],
    )
    connection.commit()
    connection.close()
    return path


def summarise(site: TableauSite) -> str:
    """A one-line description of the estate, handy in an assertion message."""
    return textwrap.dedent(
        f"""\
        {len(site.workbooks)} workbook(s), {len(site.datasources)} published datasource(s),
        {len(site.projects)} project(s), {len(site.views)} view(s)"""
    ).replace("\n", " ")


def json_dump(value: Any) -> str:
    """Compact JSON, for embedding a payload in a test failure message."""
    return json.dumps(value, indent=2, sort_keys=True)[:2000]
