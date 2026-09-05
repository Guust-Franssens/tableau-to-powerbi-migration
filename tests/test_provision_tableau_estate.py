"""Behavioural contract for `provision_tableau_estate.py`.

This module publishes to a LIVE Tableau site, and until these tests existed nothing exercised
`capture`, `apply_manifest` or `empty_leaf_projects` at all: the three invariants its own docstrings
call load-bearing were entirely unpinned. A blind review applied four mutations at once - publishing
workbooks before datasources, forcing `mode = "Overwrite"`, returning a `"/"`-joined path string, and
gutting the seed template - and the full suite still passed, exit 0.

No network, no credentials, no live site. `tsc.Pager` and the four endpoints are replaced by the
fakes below; the ITEM classes are the real `tableauserverclient` ones, so a wrong value (an invalid
`content_permissions`, say) is rejected here exactly as Tableau's client would reject it.

What each block pins:

* **dry run** - the critical defect. `path_to_id` was written only outside `if not dry_run`, so a dry
  run against an EMPTY site resolved no parent from depth 2 down and reported all ten descendants of
  `ZZ Deep` as orphans, exit 1. It survived because a dry run against the POPULATED source site
  pre-fills `path_to_id` from the live server - i.e. it worked everywhere except the one scenario the
  tool exists for.
* **ordering** - datasources strictly before workbooks. A workbook published first silently rebinds
  to nothing, which is the empty-report failure the rest of the pipeline spends its effort detecting.
* **identity** - project paths are SEGMENTS. The trial site has a project literally named `R/D`; a
  `/`-joined key makes it indistinguishable from `D` nested inside `R`.
* **secrets** - this is the one script that puts a warehouse password in a request body, and its
  error text reaches a durable `manifest.json`.
* **refusals** - never write customer content to a path git would commit; never overwrite a workbook
  we cannot prove we published; never recreate a project whose ancestry we could not resolve.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

tsc = pytest.importorskip("tableauserverclient")

import make_seed_workbook as seed  # noqa: E402  # pylint: disable=wrong-import-position
import harvest_estate_assets as harvest  # noqa: E402  # pylint: disable=wrong-import-position
import provision_tableau_estate as prov  # noqa: E402  # pylint: disable=wrong-import-position

# Long enough to clear `redact`'s 8-character floor, and shaped like the real things.
PAT_SECRET = "OJnfLKmfQ8-SESSION-TOKEN-DO-NOT-LEAK"
SF_PASSWORD = "sn0wflake-p4ssw0rd-do-not-leak"
DBX_TOKEN = "dapi-databricks-token-do-not-leak"

ENV = {
    "TABLEAU_SERVER_URL": "https://10ax.online.tableau.com",
    "TABLEAU_SITE": "trialsite",
    "TABLEAU_PAT_NAME": "provisioner",
    "TABLEAU_PAT_SECRET": PAT_SECRET,
    "TABLEAU_SF_USER": "sfuser",
    "TABLEAU_SF_PASSWORD": SF_PASSWORD,
    "TABLEAU_DBX_TOKEN": DBX_TOKEN,
}


# --------------------------------------------------------------------------- fakes


@dataclass
class FakeProject:
    """What `tsc.Pager(server.projects)` yields."""

    id: str
    name: str
    parent_id: str | None = None
    description: str = ""
    content_permissions: str = ""


@dataclass
class FakeContent:
    """What `tsc.Pager(server.datasources | server.workbooks)` yields."""

    id: str
    name: str
    project_id: str
    project_name: str = ""
    datasource_type: str = ""
    show_tabs: bool = False


@dataclass
class PublishCall:
    """One recorded publish, in the order it happened."""

    kind: str
    name: str
    project_id: str
    source: str
    mode: str
    credentials: Any = None
    show_tabs: bool | None = None


class FakePager:
    """`tsc.Pager(endpoint)` - snapshot the endpoint's items, exactly as paging does."""

    def __init__(self, endpoint: Any, *_a: Any, **_k: Any) -> None:
        self._items = list(endpoint.items)

    def __iter__(self):
        return iter(self._items)


class FakeProjectEndpoint:
    """`server.projects`."""

    def __init__(self, items, log, fail_on=(), error=None):
        self.items = list(items)
        self.log = log
        self.fail_on = set(fail_on)
        self.error = error
        self.created: list[FakeProject] = []

    def create(self, item):
        if item.name in self.fail_on:
            raise self.error or RuntimeError(f"403000 forbidden creating {item.name}")
        made = FakeProject(
            id=f"new-project-{len(self.created) + 1}",
            name=item.name,
            parent_id=item.parent_id,
            description=item.description or "",
            content_permissions=item.content_permissions or "",
        )
        self.created.append(made)
        self.items.append(made)
        self.log.append(("project.create", made.name))
        return made


class FakeGroupEndpoint:
    """`server.groups`."""

    def __init__(self, items, log, fail_on=(), error=None):
        self.items = [FakeProject(id=f"g{i}", name=n) for i, n in enumerate(items)]
        self.log = log
        self.fail_on = set(fail_on)
        self.error = error
        self.created: list[str] = []

    def create(self, item):
        if item.name in self.fail_on:
            raise self.error or RuntimeError(f"403000 forbidden creating group {item.name}")
        self.created.append(item.name)
        self.items.append(FakeProject(id=f"g-new-{len(self.created)}", name=item.name))
        self.log.append(("group.create", item.name))
        return item


class FakeContentEndpoint:
    """`server.datasources` / `server.workbooks`."""

    def __init__(self, kind, items, log, *, publish_error=None, download_error=None, payloads=None):
        self.kind = kind
        self.items = list(items)
        self.log = log
        self.publish_error = publish_error
        self.download_error = download_error
        self.payloads: dict[str, Path] = dict(payloads or {})

    def publish(self, item, file, mode, connection_credentials=None):
        call = PublishCall(
            kind=self.kind,
            name=item.name,
            project_id=item.project_id,
            source=str(file),
            mode=mode,
            credentials=connection_credentials,
            show_tabs=getattr(item, "show_tabs", None),
        )
        self.log.append(call)
        if self.publish_error is not None:
            raise self.publish_error
        self.items.append(
            FakeContent(id=f"{self.kind}-{len(self.items) + 1}", name=item.name, project_id=item.project_id)
        )
        return item

    def download(self, luid, filepath=None, include_extract=False):  # noqa: ARG002
        if self.download_error is not None:
            raise self.download_error
        target = Path(filepath)
        if target.is_dir():
            target = target / f"{luid}.twbx"
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self.payloads.get(luid)
        target.write_bytes(source.read_bytes() if source else b"fake asset bytes")
        return str(target)


class FakeAuth:
    """`server.auth` - a sign-in context manager that records that it was used."""

    def __init__(self):
        self.sign_ins = 0

    def sign_in(self, _auth):
        self.sign_ins += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@dataclass
class FakeServer:
    """A whole fake site. `log` is SHARED, so publish ORDER across endpoints is observable."""

    projects_in: tuple = ()
    groups_in: tuple = ()
    datasources_in: tuple = ()
    workbooks_in: tuple = ()
    fail_projects: tuple = ()
    fail_groups: tuple = ()
    create_error: Exception | None = None
    publish_error: Exception | None = None
    download_error: Exception | None = None
    payloads: dict = field(default_factory=dict)

    def __post_init__(self):
        self.log: list[Any] = []
        self.auth = FakeAuth()
        self.projects = FakeProjectEndpoint(self.projects_in, self.log, self.fail_projects, self.create_error)
        self.groups = FakeGroupEndpoint(self.groups_in, self.log, self.fail_groups, self.create_error)
        self.datasources = FakeContentEndpoint(
            "datasource",
            self.datasources_in,
            self.log,
            publish_error=self.publish_error,
            download_error=self.download_error,
            payloads=self.payloads,
        )
        self.workbooks = FakeContentEndpoint(
            "workbook",
            self.workbooks_in,
            self.log,
            publish_error=self.publish_error,
            download_error=self.download_error,
            payloads=self.payloads,
        )

    @property
    def publishes(self) -> list[PublishCall]:
        return [c for c in self.log if isinstance(c, PublishCall)]


@pytest.fixture(name="install")
def install_fixture(monkeypatch):
    """Point the module at a fake site, keeping the REAL tsc item classes for their validation."""

    def _install(server: FakeServer) -> FakeServer:
        monkeypatch.setattr(
            prov,
            "tsc",
            SimpleNamespace(
                Pager=FakePager,
                ProjectItem=tsc.ProjectItem,
                WorkbookItem=tsc.WorkbookItem,
                DatasourceItem=tsc.DatasourceItem,
                GroupItem=tsc.GroupItem,
                ConnectionCredentials=tsc.ConnectionCredentials,
            ),
        )
        monkeypatch.setattr(prov, "_sign_in", lambda _env: (server, object()))
        return server

    return _install


# --------------------------------------------------------------------------- manifest builders


def project_rec(path, **extra):
    rec = {
        "path": list(path),
        "name": path[-1],
        "description": "",
        "content_permissions": "",
        "tableau_managed": False,
    }
    rec.update(extra)
    return rec


def content_rec(kind, path, name, filename, **extra):
    rec = {
        "name": name,
        "project_path": list(path),
        "kind": kind,
        "content_type": "",
        "filename": filename,
        "embed_credentials": True,
        "show_tabs": False,
        "tableau_managed": False,
        "notes": [],
    }
    rec.update(extra)
    return rec


def manifest(projects=(), groups=(), content=()):
    return {
        "schema_version": "1.1",
        "site": "trialsite",
        "projects": [project_rec(p) if isinstance(p, (list, tuple)) else p for p in projects],
        "groups": list(groups),
        "content": list(content),
    }


def deep_chain(depth: int = 11) -> list[list[str]]:
    """`ZZ Deep > L1 > ... > L10` - the 11-level chain the trial site carries."""
    paths, current = [], ["ZZ Deep"]
    paths.append(list(current))
    for level in range(1, depth):
        current = current + [f"L{level}"]
        paths.append(list(current))
    return paths


def assets_dir(tmp_path: Path, *names: str) -> Path:
    out = tmp_path / "assets"
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        (out / name).write_bytes(b"asset")
    return out


# =========================================================================== CRITICAL: dry run


def test_dry_run_against_an_empty_site_plans_every_nested_project(install, tmp_path, capsys):
    """The defect, exactly as reported: 11 nested projects, empty target, `--dry-run`.

    Before the fix this printed `+ project CREATE ZZ Deep` and then ten problems - one per level -
    and exited 1, for the one workflow the whole feature exists to support.
    """
    server = install(FakeServer())
    code = prov.apply_manifest(manifest(deep_chain()), tmp_path, ENV, dry_run=True, overwrite=False)
    out = capsys.readouterr().out

    assert code == 0, out
    assert out.count("+ project CREATE") == 11
    assert "problem(s)" not in out
    assert "parent" not in out
    assert server.projects.created == [], "a dry run must not create anything"


def test_the_cli_dry_run_exits_zero_against_an_empty_site(install, tmp_path, monkeypatch, capsys):
    """The gate as an operator meets it: `apply --manifest ... --dry-run`, empty target, exit 0."""
    install(FakeServer())
    monkeypatch.setattr(prov, "resolve_env", lambda *_a, **_k: ENV)
    doc = tmp_path / "manifest.json"
    doc.write_text(json.dumps(manifest(deep_chain())), encoding="utf8")

    assert prov.main(["apply", "--manifest", str(doc), "--dry-run"]) == 0
    assert capsys.readouterr().out.count("+ project CREATE") == 11


def test_dry_run_plans_exactly_what_a_real_run_creates(install, tmp_path, capsys):
    """The stronger claim. A plan that does not match the run it previews is worse than no plan."""
    paths = deep_chain()
    install(FakeServer())
    prov.apply_manifest(manifest(paths), tmp_path, ENV, dry_run=True, overwrite=False)
    planned = [line for line in capsys.readouterr().out.splitlines() if "project CREATE" in line]

    real = install(FakeServer())
    prov.apply_manifest(manifest(paths), tmp_path, ENV, dry_run=False, overwrite=False)
    done = [line for line in capsys.readouterr().out.splitlines() if "project CREATE" in line]

    assert planned == done
    assert [p.name for p in real.projects.created] == [p[-1] for p in paths]


def test_a_real_run_threads_the_parent_luid_down_the_chain(install, tmp_path):
    """Ordering alone is not enough - each child must name the parent that was just created."""
    server = install(FakeServer())
    prov.apply_manifest(manifest(deep_chain()), tmp_path, ENV, dry_run=False, overwrite=False)
    created = server.projects.created
    assert created[0].parent_id is None
    assert [c.parent_id for c in created[1:]] == [c.id for c in created[:-1]]


def test_dry_run_still_reports_content_whose_project_is_genuinely_absent(install, tmp_path, capsys):
    """The control for the sentinel. If a planned project resolved everything, the sentinel would be
    hiding real problems instead of fixing one."""
    install(FakeServer())
    assets = assets_dir(tmp_path, "orphan.twbx")
    code = prov.apply_manifest(
        manifest(content=[content_rec("workbook", ["Nowhere"], "Orphan", "orphan.twbx")]),
        assets,
        ENV,
        dry_run=True,
        overwrite=False,
    )
    assert code == 1
    assert "not found" in capsys.readouterr().out


def test_a_second_run_is_a_clean_no_op(install, tmp_path, capsys):
    """Idempotency. Re-running after a partial failure is the documented recovery, so a second run
    must create and publish nothing."""
    assets = assets_dir(tmp_path, "ds.tdsx", "wb.twbx")
    doc = manifest(
        projects=[["Sales"]],
        groups=["Entitled"],
        content=[
            content_rec("datasource", ["Sales"], "Orders", "ds.tdsx"),
            content_rec("workbook", ["Sales"], "Report", "wb.twbx"),
        ],
    )
    server = install(FakeServer())
    assert prov.apply_manifest(doc, assets, ENV, dry_run=False, overwrite=False) == 0
    before = len(server.log)

    assert prov.apply_manifest(doc, assets, ENV, dry_run=False, overwrite=False) == 0
    out = capsys.readouterr().out
    assert len(server.log) == before, "second run wrote to the site"
    assert "= project exists" in out and "= datasource exists" in out and "= workbook exists" in out


# =========================================================================== ordering and mode


def test_datasources_are_published_before_workbooks(install, tmp_path):
    """`Ordering is not cosmetic` (module docstring). A workbook published first silently rebinds to
    nothing. The manifest deliberately lists the WORKBOOK first, so only the loop order can save it.
    """
    assets = assets_dir(tmp_path, "ds.tdsx", "wb.twbx")
    server = install(FakeServer())
    prov.apply_manifest(
        manifest(
            projects=[["Sales"]],
            content=[
                content_rec("workbook", ["Sales"], "Report", "wb.twbx"),
                content_rec("datasource", ["Sales"], "Orders", "ds.tdsx"),
            ],
        ),
        assets,
        ENV,
        dry_run=False,
        overwrite=False,
    )
    assert [c.kind for c in server.publishes] == ["datasource", "workbook"]


def test_every_datasource_lands_before_any_workbook_even_across_projects(install, tmp_path):
    """A workbook in project A can bind a datasource in project B, so the rule is global, not
    per-project."""
    assets = assets_dir(tmp_path, "a.tdsx", "b.tdsx", "a.twbx", "b.twbx")
    server = install(FakeServer())
    prov.apply_manifest(
        manifest(
            projects=[["A"], ["B"]],
            content=[
                content_rec("workbook", ["A"], "WA", "a.twbx"),
                content_rec("datasource", ["B"], "DB", "b.tdsx"),
                content_rec("workbook", ["B"], "WB", "b.twbx"),
                content_rec("datasource", ["A"], "DA", "a.tdsx"),
            ],
        ),
        assets,
        ENV,
        dry_run=False,
        overwrite=False,
    )
    kinds = [c.kind for c in server.publishes]
    assert kinds == ["datasource", "datasource", "workbook", "workbook"]


@pytest.mark.parametrize(("overwrite", "expected"), [(False, "CreateNew"), (True, "Overwrite")])
def test_publish_mode_follows_the_overwrite_flag(install, tmp_path, overwrite, expected):
    """`--overwrite` decides whether an existing workbook is replaced. Hard-coding `Overwrite` makes
    every run destructive while every message still says otherwise."""
    assets = assets_dir(tmp_path, "wb.twbx")
    server = install(FakeServer())
    prov.apply_manifest(
        manifest(projects=[["Sales"]], content=[content_rec("workbook", ["Sales"], "Report", "wb.twbx")]),
        assets,
        ENV,
        dry_run=False,
        overwrite=overwrite,
    )
    assert [c.mode for c in server.publishes] == [expected]


def test_existing_content_is_skipped_unless_overwrite_is_asked_for(install, tmp_path):
    """The other half: without `--overwrite`, content already on the site is left alone entirely."""
    assets = assets_dir(tmp_path, "wb.twbx")
    site = FakeServer(
        projects_in=(FakeProject("p1", "Sales"),),
        workbooks_in=(FakeContent("w1", "Report", "p1"),),
    )
    server = install(site)
    prov.apply_manifest(
        manifest(projects=[["Sales"]], content=[content_rec("workbook", ["Sales"], "Report", "wb.twbx")]),
        assets,
        ENV,
        dry_run=False,
        overwrite=False,
    )
    assert server.publishes == []


def test_a_warehouse_datasource_publishes_with_the_credentials_from_env(install, tmp_path):
    """The fixture value of the credential-free datasource is the ABSENCE of a credential, so the two
    branches must stay distinguishable."""
    assets = assets_dir(tmp_path, "sf.tdsx", "none.tdsx")
    server = install(FakeServer())
    prov.apply_manifest(
        manifest(
            projects=[["Live"]],
            content=[
                content_rec("datasource", ["Live"], "SF", "sf.tdsx", content_type="snowflake"),
                content_rec(
                    "datasource",
                    ["Live"],
                    "No credential",
                    "none.tdsx",
                    content_type="snowflake",
                    embed_credentials=False,
                ),
            ],
        ),
        assets,
        ENV,
        dry_run=False,
        overwrite=False,
    )
    by_name = {c.name: c for c in server.publishes}
    assert by_name["SF"].credentials is not None
    assert by_name["No credential"].credentials is None


# =========================================================================== identity: segments


def test_a_slash_in_a_project_name_is_not_a_path_separator():
    """`R/D` (one project) must never collide with `D` inside `R` (two). The trial site has both."""
    slash = FakeProject("a", "R/D")
    parent = FakeProject("b", "R")
    child = FakeProject("c", "D", parent_id="b")
    by_id = {p.id: p for p in (slash, parent, child)}

    assert prov.project_path_of(slash, by_id) == ("R/D",)
    assert prov.project_path_of(child, by_id) == ("R", "D")
    assert prov.project_path_of(slash, by_id) != prov.project_path_of(child, by_id)


def test_a_path_is_a_tuple_of_segments_not_a_joined_string():
    """Stated separately because a joined string is the exact regression this function was rewritten
    to prevent, and it type-checks fine everywhere it is used."""
    result = prov.project_path_of(FakeProject("a", "R", parent_id=None), {})
    assert isinstance(result, tuple)
    assert result == ("R",)


def test_apply_tells_the_two_apart_against_a_live_site(install, tmp_path, capsys):
    """The consequence downstream: `R > D` already exists, `R/D` does not, and the plan must say so.
    A joined key makes both look like `R/D` and the answer is wrong in one direction or the other."""
    site = FakeServer(projects_in=(FakeProject("p1", "R"), FakeProject("p2", "D", parent_id="p1")))
    install(site)
    prov.apply_manifest(manifest([["R"], ["R", "D"], ["R/D"]]), tmp_path, ENV, dry_run=True, overwrite=False)
    out = capsys.readouterr().out

    assert "= project exists   R > D" in out
    assert "+ project CREATE   R/D" in out


def test_seed_names_come_from_the_project_not_from_its_path(install, tmp_path):
    """`Seed - <leaf>` derived by splitting a joined path gives `Seed - D` for a project named
    `R/D` - the original defect, in the place where it was actually seen."""
    site = FakeServer(projects_in=(FakeProject("p1", "R/D"),))
    install(site)
    assert prov.seed_empty_projects(ENV, tmp_path, dry_run=False) == 0
    assert [c.name for c in site.publishes] == ["Seed - R/D"]


# =========================================================================== secrets


def test_a_download_failure_does_not_write_the_session_token_into_the_manifest(install, tmp_path):
    """The durable case. `ContentRecord.notes` is copied verbatim into `manifest.json` by `asdict`,
    and a REST failure can echo the request that caused it."""
    leak = RuntimeError(f"500 Server Error: X-Tableau-Auth: {PAT_SECRET} while downloading")
    install(
        FakeServer(
            workbooks_in=(FakeContent("w1", "Report", "p1"),),
            projects_in=(FakeProject("p1", "Sales"),),
            download_error=leak,
        )
    )
    result = prov.capture(tmp_path, ENV, include_extract=False, download=True)

    serialised = json.dumps(result)
    assert PAT_SECRET not in serialised
    assert "[REDACTED]" in serialised
    assert "download failed" in serialised


def test_a_publish_failure_does_not_print_the_warehouse_password(install, tmp_path, capsys):
    """This is the only script in the repo that puts a warehouse password in a request body, so the
    PAT is not the only secret its error text can carry."""
    assets = assets_dir(tmp_path, "sf.tdsx")
    install(FakeServer(publish_error=RuntimeError(f"400 bad request body pwd={SF_PASSWORD} token={DBX_TOKEN}")))
    code = prov.apply_manifest(
        manifest(
            projects=[["Live"]],
            content=[content_rec("datasource", ["Live"], "SF", "sf.tdsx", content_type="snowflake")],
        ),
        assets,
        ENV,
        dry_run=False,
        overwrite=False,
    )
    out = capsys.readouterr().out

    assert code == 1
    assert SF_PASSWORD not in out and DBX_TOKEN not in out
    assert "[REDACTED]" in out


def test_a_seed_publish_failure_is_redacted_too(install, tmp_path, capsys):
    """Same class, different code path - the seed publisher had its own unredacted formatter."""
    install(
        FakeServer(
            projects_in=(FakeProject("p1", "Empty"),), publish_error=RuntimeError(f"401 x-tableau-auth: {PAT_SECRET}")
        )
    )
    assert prov.seed_empty_projects(ENV, tmp_path, dry_run=False) == 1
    out = capsys.readouterr().out
    assert PAT_SECRET not in out and "[REDACTED]" in out


def test_a_failed_create_is_redacted(install, tmp_path, capsys):
    """And the third: the two `create` calls used to raise straight out of the function, so their
    message reached the terminal as an untouched traceback."""
    install(FakeServer(fail_projects=("Sales",), create_error=RuntimeError(f"403 X-Tableau-Auth: {PAT_SECRET}")))
    assert prov.apply_manifest(manifest([["Sales"]]), tmp_path, ENV, dry_run=False, overwrite=False) == 1
    out = capsys.readouterr().out
    assert PAT_SECRET not in out and "[REDACTED]" in out


# =========================================================================== failure containment


def test_a_failed_project_create_still_prints_what_was_already_created(install, tmp_path, capsys):
    """An exception escaping `apply_manifest` discards `planned` - the operator's only record of what
    this run already put on a LIVE site - and replaces it with a traceback."""
    server = install(FakeServer(fail_projects=("Boom",)))
    code = prov.apply_manifest(
        manifest([["Fine"], ["Boom"], ["Boom", "Child"]]), tmp_path, ENV, dry_run=False, overwrite=False
    )
    out = capsys.readouterr().out

    assert code == 1
    assert "+ project CREATE   Fine" in out, "the plan was lost"
    assert "create failed" in out
    assert "parent 'Boom' missing" in out, "the cascade must be reported, not crashed on"
    assert [p.name for p in server.projects.created] == ["Fine"]


def test_a_failed_group_create_does_not_abort_the_run(install, tmp_path, capsys):
    """Groups are created before any content; a raise here loses the whole publish phase."""
    assets = assets_dir(tmp_path, "wb.twbx")
    server = install(FakeServer(fail_groups=("Entitled",)))
    code = prov.apply_manifest(
        manifest(
            projects=[["Sales"]], groups=["Entitled"], content=[content_rec("workbook", ["Sales"], "Report", "wb.twbx")]
        ),
        assets,
        ENV,
        dry_run=False,
        overwrite=False,
    )
    assert code == 1
    assert "create failed" in capsys.readouterr().out
    assert [c.name for c in server.publishes] == ["Report"], "content phase was skipped"


# =========================================================================== unresolvable ancestry


def test_a_project_whose_parent_is_invisible_is_marked_not_flattened():
    """A PAT that can see a nested project but not its ancestors recorded it as TOP-LEVEL, and
    `apply` then recreated it - with its content - at the site root."""
    child = FakeProject("c", "Nested", parent_id="invisible")
    path = prov.project_path_of(child, {"c": child})

    assert path != ("Nested",)
    assert prov.path_is_truncated(path)


def test_a_cyclic_parent_chain_terminates_and_is_marked():
    """The case that was already handled - kept so the two markers stay symmetric."""
    a = FakeProject("a", "A", parent_id="b")
    b = FakeProject("b", "B", parent_id="a")
    path = prov.project_path_of(a, {"a": a, "b": b})

    assert prov.path_is_truncated(path)
    assert "A" in path and "B" in path


def test_a_fully_visible_chain_is_not_marked():
    """The control: without it, a marker on everything would make the assertions above vacuous."""
    top = FakeProject("t", "Top")
    mid = FakeProject("m", "Mid", parent_id="t")
    assert not prov.path_is_truncated(prov.project_path_of(mid, {"t": top, "m": mid}))


def test_apply_refuses_to_recreate_a_project_with_unresolved_ancestry(install, tmp_path, capsys):
    """Marking is only half the fix. Creating a project named `<unresolved-parent:...>` would be a
    second wrong answer."""
    server = install(FakeServer())
    code = prov.apply_manifest(
        manifest([[prov.UNRESOLVED_PARENT_MARKER.format("abc"), "Nested"]]),
        tmp_path,
        ENV,
        dry_run=False,
        overwrite=False,
    )
    assert code == 1
    assert "ancestry could not be resolved" in capsys.readouterr().out
    assert server.projects.created == []


def test_capture_marks_content_whose_project_is_invisible(install, tmp_path):
    """The same hole on the content side: `else (item.project_name,)` also flattened to top level."""
    install(FakeServer(workbooks_in=(FakeContent("w1", "Report", "hidden-project", project_name="Nested"),)))
    result = prov.capture(tmp_path, ENV, include_extract=False, download=False)
    assert prov.path_is_truncated(result["content"][0]["project_path"])


# =========================================================================== capture fidelity


def test_content_permissions_and_show_tabs_survive_capture_and_apply(install, tmp_path):
    """Both were silently dropped. `LockedToProject` came back `ManagedByOwner`, and TSC omits
    `showTabs` from a publish when it is false, so every workbook came back with its tabs hidden -
    which changes what the migration engine sees."""
    site = FakeServer(
        projects_in=(FakeProject("p1", "Locked", content_permissions="LockedToProject"),),
        workbooks_in=(FakeContent("w1", "Report", "p1", show_tabs=True),),
    )
    install(site)
    captured = prov.capture(tmp_path, ENV, include_extract=False, download=False)

    assert captured["projects"][0]["content_permissions"] == "LockedToProject"
    assert captured["content"][0]["show_tabs"] is True

    captured["content"][0]["filename"] = "wb.twbx"
    target = install(FakeServer())
    prov.apply_manifest(captured, assets_dir(tmp_path, "wb.twbx"), ENV, dry_run=False, overwrite=False)

    assert target.projects.created[0].content_permissions == "LockedToProject"
    assert target.publishes[0].show_tabs is True


def test_the_completion_note_names_what_is_not_reproduced(install, tmp_path, capsys):
    """The note is the only place a user learns what did NOT come back. Naming only permissions
    implied the structure was faithful."""
    install(FakeServer())
    prov.apply_manifest(manifest(), tmp_path, ENV, dry_run=True, overwrite=False)
    out = capsys.readouterr().out.lower()
    for term in ("membership", "ownership", "tags", "certification", "extract"):
        assert term in out, f"the not-reproduced note never mentions {term}"


def test_tableau_managed_content_is_captured_but_never_recreated(install, tmp_path, capsys):
    """`Admin Insights` is generated by Tableau Cloud; recreating it either fails or shadows the real
    one."""
    site = FakeServer(
        projects_in=(FakeProject("p1", "Admin Insights"),),
        datasources_in=(FakeContent("d1", "TS Events", "p1"),),
    )
    install(site)
    captured = prov.capture(tmp_path, ENV, include_extract=False, download=True)
    assert captured["projects"][0]["tableau_managed"] is True
    assert captured["content"][0]["tableau_managed"] is True
    assert captured["content"][0]["filename"] == "", "a managed asset must not be downloaded"

    target = install(FakeServer())
    assert prov.apply_manifest(captured, tmp_path, ENV, dry_run=False, overwrite=False) == 0
    capsys.readouterr()
    assert target.projects.created == [] and target.publishes == []


# =========================================================================== seeding


def _publish_seed_into(tmp_path: Path, name: str) -> Path:
    """A real seed archive, as `make_seed_workbook` builds it."""
    return seed.build_twbx(name, tmp_path / "existing.twbx")


def _foreign_workbook(tmp_path: Path) -> Path:
    """A workbook that is NOT ours, but is named like a seed."""
    path = tmp_path / "foreign.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Real Analysis.twb", "<workbook><datasources/></workbook>")
    return path


def test_refresh_seeded_refuses_to_overwrite_a_workbook_it_did_not_publish(install, tmp_path):
    """`name.startswith("Seed - ")` is a naming convention, not proof of authorship. A leaf holding
    ONE genuine workbook called `Seed - <project>` counted as `non_seed == 0`, became a target, and
    was replaced by a 1.8 KB stub."""
    site = FakeServer(
        projects_in=(FakeProject("p1", "Leaf"),),
        workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
        payloads={"w1": _foreign_workbook(tmp_path)},
    )
    install(site)
    code = prov.seed_empty_projects(ENV, tmp_path / "work", dry_run=False, include_seeded=True)

    assert code == 1
    assert site.publishes == [], "real content was overwritten"


def test_refresh_seeded_does_replace_a_seed_this_tool_published(install, tmp_path):
    """The other direction. Refusing everything would make `--refresh-seeded` useless, which is how a
    site full of untypeable seeds happened in the first place."""
    site = FakeServer(
        projects_in=(FakeProject("p1", "Leaf"),),
        workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
        payloads={"w1": _publish_seed_into(tmp_path, "Seed - Leaf")},
    )
    install(site)
    assert prov.seed_empty_projects(ENV, tmp_path / "work", dry_run=False, include_seeded=True) == 0
    assert [(c.name, c.mode) for c in site.publishes] == [("Seed - Leaf", "Overwrite")]


def test_an_empty_leaf_is_seeded_with_createnew(install, tmp_path):
    """Nothing is there, so nothing can be clobbered - and `CreateNew` says so."""
    site = FakeServer(projects_in=(FakeProject("p1", "Leaf"),))
    install(site)
    assert prov.seed_empty_projects(ENV, tmp_path, dry_run=False) == 0
    assert [(c.name, c.mode) for c in site.publishes] == [("Seed - Leaf", "CreateNew")]


def test_a_leaf_holding_real_content_is_never_a_target(install, tmp_path):
    site = FakeServer(
        projects_in=(FakeProject("p1", "Leaf"),),
        workbooks_in=(FakeContent("w1", "Quarterly Review", "p1"),),
    )
    install(site)
    assert prov.empty_leaf_projects(site, include_seeded=True) == []


def test_a_project_with_children_is_not_a_leaf(install, tmp_path):
    """Only leaves are seeded: a parent holding a child is already on a walked path."""
    site = FakeServer(projects_in=(FakeProject("p1", "Top"), FakeProject("p2", "Child", parent_id="p1")))
    install(site)
    assert [t.segments for t in prov.empty_leaf_projects(site)] == [("Top", "Child")]


def test_the_deliberately_empty_fixture_is_never_seeded(install, tmp_path):
    """`ZZ Migration Torture > Empty On Purpose` tests what an empty project does. Seeding it deletes
    the fixture."""
    site = FakeServer(
        projects_in=(
            FakeProject("p1", "ZZ Migration Torture"),
            FakeProject("p2", "Empty On Purpose", parent_id="p1"),
            FakeProject("p3", "Seed Me", parent_id="p1"),
        )
    )
    install(site)
    assert [t.name for t in prov.empty_leaf_projects(site)] == ["Seed Me"]


def test_a_tableau_managed_leaf_is_never_seeded(install, tmp_path):
    site = FakeServer(projects_in=(FakeProject("p1", "Samples"),))
    install(site)
    assert prov.empty_leaf_projects(site) == []


def test_seed_dry_run_publishes_nothing(install, tmp_path, capsys):
    site = FakeServer(projects_in=(FakeProject("p1", "Leaf"),))
    install(site)
    assert prov.seed_empty_projects(ENV, tmp_path, dry_run=True) == 0
    assert site.publishes == []
    assert "seed PLAN" in capsys.readouterr().out


def test_seed_dry_run_reports_the_refusal_the_real_run_would_make(install, tmp_path, capsys):
    """The plan/run divergence, in the shape `apply` was just fixed for. The authorship check lived
    inside `_publish_seed`, which a dry run skips - so the plan printed a successful replace, exit 0,
    for a target the real run refuses, exit 1. A plan that disagrees with its run is worse than none.
    """

    def _site() -> FakeServer:
        return FakeServer(
            projects_in=(FakeProject("p1", "Leaf"),),
            workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
            payloads={"w1": _foreign_workbook(tmp_path)},
        )

    install(_site())
    planned_code = prov.seed_empty_projects(ENV, tmp_path / "plan", dry_run=True, include_seeded=True)
    planned = capsys.readouterr().out

    install(_site())
    real_code = prov.seed_empty_projects(ENV, tmp_path / "run", dry_run=False, include_seeded=True)
    real = capsys.readouterr().out

    assert planned_code == real_code == 1
    assert "refusing to Overwrite" in planned
    assert "seed PLAN" not in planned, "the plan claimed a replace it had not proven it could make"
    assert planned.replace("PLAN", "PUBLISH") == real


def test_seed_dry_run_names_the_mode_it_would_publish_with(install, tmp_path, capsys):
    """And the positive half: an empty leaf plans `CreateNew`, an owned seed plans `Overwrite`."""
    install(FakeServer(projects_in=(FakeProject("p1", "Leaf"),)))
    prov.seed_empty_projects(ENV, tmp_path / "a", dry_run=True)
    assert "[CreateNew]" in capsys.readouterr().out

    install(
        FakeServer(
            projects_in=(FakeProject("p1", "Leaf"),),
            workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
            payloads={"w1": _publish_seed_into(tmp_path, "Seed - Leaf")},
        )
    )
    prov.seed_empty_projects(ENV, tmp_path / "b", dry_run=True, include_seeded=True)
    assert "[Overwrite]" in capsys.readouterr().out


# --------------------------------------------------------------------------- _is_our_seed itself
#
# The newest destructive-safety logic in the tool, and the branch that matters is the one that runs
# when verification is IMPOSSIBLE. `return True` there passes every end-to-end test above, because
# they all supply a readable payload - so the failure modes are asserted directly.


def test_a_real_seed_is_recognised(install, tmp_path):
    site = FakeServer(
        workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
        payloads={"w1": _publish_seed_into(tmp_path, "Seed - Leaf")},
    )
    install(site)
    assert prov._is_our_seed(site, "w1", tmp_path / "work") is True  # pylint: disable=protected-access


@pytest.mark.parametrize("broken", ["download_raises", "not_an_archive", "archive_without_a_twb", "foreign_twb"])
def test_an_unprovable_candidate_is_never_treated_as_ours(install, tmp_path, broken):
    """Unprovable means refuse, in every direction. `return True` here would silently re-open the
    destructive path the download exists to close."""
    payload = tmp_path / "payload"
    if broken == "not_an_archive":
        payload.write_text("this is not a workbook at all", encoding="utf8")
    elif broken == "archive_without_a_twb":
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("Data/seed/seed_data.csv", "region,amount\n")
    elif broken == "foreign_twb":
        payload = _foreign_workbook(tmp_path)

    site = FakeServer(
        workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
        download_error=RuntimeError("404 not found") if broken == "download_raises" else None,
        payloads={} if broken == "download_raises" else {"w1": payload},
    )
    install(site)
    assert prov._is_our_seed(site, "w1", tmp_path / "work") is False  # pylint: disable=protected-access


def test_verification_leaves_nothing_behind(install, tmp_path):
    """It downloads into the work dir; a probe that accumulates copies of live content is its own
    problem."""
    work = tmp_path / "work"
    site = FakeServer(
        workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
        payloads={"w1": _publish_seed_into(tmp_path, "Seed - Leaf")},
    )
    install(site)
    prov._is_our_seed(site, "w1", work)  # pylint: disable=protected-access
    assert not (work / "_verify").exists()


def test_a_failed_verification_stops_the_publish_end_to_end(install, tmp_path):
    """The consequence: an unreadable candidate must not be overwritten either."""
    site = FakeServer(
        projects_in=(FakeProject("p1", "Leaf"),),
        workbooks_in=(FakeContent("w1", "Seed - Leaf", "p1"),),
        download_error=RuntimeError("503 service unavailable"),
    )
    install(site)
    assert prov.seed_empty_projects(ENV, tmp_path, dry_run=False, include_seeded=True) == 1
    assert site.publishes == []


# =========================================================================== the output guard


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text("/_*\n", encoding="utf8")
    return repo


@pytest.fixture(name="capture_spy")
def capture_spy_fixture(monkeypatch):
    """Replace `capture` with a recorder, so no test can reach a network path."""
    seen: list[Path] = []

    def _fake_capture(out_dir, _env, **_kw):
        seen.append(out_dir)
        return {"schema_version": "1.1", "projects": [], "groups": [], "content": []}

    monkeypatch.setattr(prov, "capture", _fake_capture)
    monkeypatch.setattr(prov, "resolve_env", lambda *_a, **_k: ENV)
    return seen


def test_capture_refuses_to_write_a_site_inventory_to_an_unignored_path(tmp_path, capture_spy):
    """`manifest.json` names every project, workbook and datasource on a live site, and this repo is
    PUBLIC. `harvest_estate_assets.py` already refuses this; `capture` did not (issue #322)."""
    repo = _git_repo(tmp_path)
    assert prov.main(["capture", "--out", str(repo / "docs" / "estate")]) == 2
    assert capture_spy == [], "the site was enumerated anyway"


def test_capture_proceeds_when_the_output_is_ignored(tmp_path, capture_spy):
    """The control. A guard that refuses everything is indistinguishable from a broken tool."""
    repo = _git_repo(tmp_path)
    assert prov.main(["capture", "--out", str(repo / "_runs" / "tableau-estate")]) == 0
    assert len(capture_spy) == 1


def test_the_escape_hatch_still_exists(tmp_path, capture_spy):
    repo = _git_repo(tmp_path)
    assert prov.main(["capture", "--out", str(repo / "docs" / "estate"), "--allow-unignored-out"]) == 0
    assert len(capture_spy) == 1


def test_the_guard_and_the_write_are_given_the_same_path(tmp_path, monkeypatch, capture_spy):
    """A guard that validates one form of a path while the write uses another proves nothing - it is
    a real defect shape in this repo, not a hypothetical."""
    guarded: list[Path] = []
    monkeypatch.setattr(prov, "refuse_unignored_output", lambda out, _allow, **_kw: bool(guarded.append(out)))

    assert prov.main(["capture", "--out", str(tmp_path / "out")]) == 0
    assert guarded == capture_spy
    assert guarded[0].is_absolute()


def test_the_manifest_lands_under_the_guarded_directory(tmp_path, capture_spy):
    """And it must actually be written there, or the guarded path was the wrong question."""
    out = tmp_path / "estate"
    assert prov.main(["capture", "--out", str(out)]) == 0
    assert json.loads((out / "manifest.json").read_text(encoding="utf8"))["schema_version"] == "1.1"


def test_the_guard_asks_about_the_files_CAPTURE_actually_writes(tmp_path, capture_spy):
    """`capture` writes `manifest.json`; the harvester writes `parse-sweep.*`. Passing harvest's
    default artifact names would still refuse an entirely unignored `--out`, so the obvious test
    passes for the wrong reason and `CAPTURE_ARTIFACTS` goes unverified.

    This `.gitignore` separates them: every harvest artifact is ignored, `manifest.json` is not. With
    the default names the guard says SAFE; only the capture names see the leak.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text(
        "*.twb\n*.twbx\n*.tds\n*.tdsx\nparse-sweep.*\nparse-sweep-totals.json\n", encoding="utf8"
    )
    out = repo / "estate"

    assert harvest.refuse_unignored_output(out, allow_unignored=False) is False, (
        "fixture is wrong: harvest's own artifacts must be ignored here, or this proves nothing"
    )
    assert prov.main(["capture", "--out", str(out)]) == 2
    assert capture_spy == []


def test_the_capture_artifact_list_names_the_manifest_and_the_downloads(tmp_path):
    """The list itself, stated once: a `manifest.json` that names a live site's whole inventory is as
    sensitive as the assets beside it, and each entry must be a FILE (a directory-only ignore rule is
    not applied to a path git does not know is a directory)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text("*.twbx\n*.tdsx\n", encoding="utf8")

    unignored = harvest.unignored_output_paths(repo / "estate", prov.CAPTURE_ARTIFACTS)
    assert unignored == [(repo / "estate" / "manifest.json").resolve()]
    assert all("/" in a or a.endswith(".json") for a in prov.CAPTURE_ARTIFACTS)
