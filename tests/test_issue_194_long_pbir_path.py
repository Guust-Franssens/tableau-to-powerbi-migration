"""The issue-194 downloadable repro: reproducible, public-safe, and boundary-crossing.

This is an **end-to-end upstream repro**, not another identifier-cap matrix. It asserts exactly three
things, and deliberately nothing that `tests/test_run_estate.py` already owns:

1. the two committed `.twbx` archives are byte-reproducible from `build_repro.py` and carry nothing
   private (offline, runs in CI);
2. the two archives differ **only** in the identity names — same CSV bytes, same workbook structure
   once the names are mapped (offline, runs in CI);
3. canonical engine output at a `C:\\tfmig`-equivalent short root crosses Power BI Desktop's file
   ceiling for the long case and does not for the short control, with the offender being a
   **required** child of the semantic model rather than the `.pbip` pointer (engine-dependent).

The measured refusal, quoted from Desktop's own modal so it is not inferred from a window title, is
in `fixtures/upstream-repros/issue-194-long-pbir-path/README.md`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engine_source  # noqa: E402  # pylint: disable=wrong-import-position
from check_path_ceiling import DIR_CEILING, FILE_CEILING, utf16_len  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURE = REPO / "fixtures" / "upstream-repros" / "issue-194-long-pbir-path"
BUILDER = FIXTURE / "build_repro.py"


def _builder_cases() -> dict[str, dict[str, str]]:
    """The builder's own CASES table, imported rather than duplicated."""
    spec = importlib.util.spec_from_file_location("issue194_build_repro", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CASES


#: Archive names are DERIVED from the builder, never hard-coded: a mutation that renames a case must
#: reach the artifact the test reads, or the mutation silently tests nothing.
LONG_ARCHIVE = f"{_builder_cases()['long']['stem']}.twbx"
SHORT_ARCHIVE = f"{_builder_cases()['short']['stem']}.twbx"

SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"
ENGINE_SKIP_REASON = "deterministic tier not installed"

#: The skill's ordinary run root is `C:\tfmig\runs\NNNN\out` - 22 UTF-16 units. The engine's output
#: paths are root-independent, so the test reproduces that LENGTH under pytest's own temp directory
#: rather than writing to a shared machine path.
SKILL_ROOT_LEN = utf16_len(r"C:\tfmig\runs\9194\out")

#: Text that must never appear in a public repro. Hostnames and site slugs are what a harvested
#: Tableau workbook leaks; `repository-location` is the element that carries them.
FORBIDDEN = ("repository-location", "onmicrosoft.com", "tableau.com", "10ax.online", "password=")


def _contract() -> Path | None:
    if os.environ.get(SIMULATE_ENGINE_ABSENT):
        return None
    try:
        return engine_source.engine_root()
    except engine_source.EngineNotFoundError:
        return None


def requires_engine(test):
    """Mark an engine-dependent test and skip it when the canonical engine is absent."""
    test = pytest.mark.engine_dependency(expected_skip_reason=ENGINE_SKIP_REASON)(test)
    return pytest.mark.skipif(_contract() is None, reason=ENGINE_SKIP_REASON)(test)


def _members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist()}


# -- offline: the archives are reproducible and public-safe ----------------------------------------
def test_the_committed_archives_rebuild_byte_for_byte() -> None:
    """A maintainer must be able to regenerate exactly what they downloaded."""
    done = subprocess.run(
        [sys.executable, str(BUILDER), "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, (
        "the committed .twbx archives no longer match a rebuild from src/. Re-run build_repro.py and "
        f"commit the result, or the download and the recipe disagree.\n{done.stdout}\n{done.stderr}"
    )
    assert "DIFFER" not in done.stdout and "MISSING" not in done.stdout, done.stdout


def test_the_archives_carry_no_private_identifiers() -> None:
    """Public-safe means checked, not assumed: no server, site, account or credential text."""
    for archive in (LONG_ARCHIVE, SHORT_ARCHIVE):
        for name, payload in _members(FIXTURE / archive).items():
            text = payload.decode("utf-8", "replace")
            for needle in FORBIDDEN:
                assert needle not in text and needle not in name, (
                    f"{archive}:{name} contains {needle!r}. This fixture is linked from a public "
                    "upstream issue; it must carry nothing but synthetic data and generic names."
                )


def test_the_two_cases_differ_only_in_their_identity_names() -> None:
    """One variable. Same CSV bytes, and the same workbook XML once the names are mapped back."""
    long_members = _members(FIXTURE / LONG_ARCHIVE)
    short_members = _members(FIXTURE / SHORT_ARCHIVE)

    long_csv = next(n for n in long_members if n.endswith(".csv"))
    short_csv = next(n for n in short_members if n.endswith(".csv"))
    assert hashlib.sha256(long_members[long_csv]).hexdigest() == hashlib.sha256(short_members[short_csv]).hexdigest(), (
        "the two cases no longer ship identical data; the A/B would then have two variables"
    )

    cases = _builder_cases()
    long_xml = long_members[next(n for n in long_members if n.endswith(".twb"))].decode("utf-8")
    short_xml = short_members[next(n for n in short_members if n.endswith(".twb"))].decode("utf-8")
    tokens = {"datasource": "@@D@@", "dashboard": "@@B@@", "worksheet": "@@W@@", "csv": "@@C@@"}
    # Longest value first: the short case's datasource name ("Regional Sales") is a PREFIX of its
    # dashboard name ("Regional Sales Review"), so a naive order rewrites half a title.
    for side, xml_name in (("long", "long_xml"), ("short", "short_xml")):
        text = long_xml if side == "long" else short_xml
        for key in sorted(tokens, key=lambda k, s=side: -len(cases[s][k])):
            text = text.replace(cases[side][key], tokens[key])
        if xml_name == "long_xml":
            long_xml = text
        else:
            short_xml = text
    assert ET.canonicalize(long_xml) == ET.canonicalize(short_xml), (
        "with the identity names mapped back to placeholders the two workbooks must be identical; "
        "anything else means the A/B changes more than the names"
    )


def test_the_long_case_carries_a_plausible_name_not_padding() -> None:
    """A repro a maintainer will act on cannot be `AAAA...`."""
    for key, value in _builder_cases()["long"].items():
        words = re.findall(r"[A-Za-z][a-z]+", value)
        assert len(words) >= 4, f"long {key} {value!r} does not read like a real title"
        assert not re.search(r"(.)\1{4,}", value), f"long {key} {value!r} looks like padding"


# -- engine-dependent: the boundary is crossed by real emitted output ------------------------------
@pytest.fixture(scope="session", name="engine_runs")
def _engine_runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Run the canonical engine on BOTH archives, each into its own per-process temp root."""
    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles collection-time absence
        pytest.skip(ENGINE_SKIP_REASON)
    base = tmp_path_factory.mktemp("issue-194")
    runs: dict[str, dict[str, Any]] = {}
    for case, archive in (("long", LONG_ARCHIVE), ("short", SHORT_ARCHIVE)):
        source = base / case / "in"
        source.mkdir(parents=True)
        shutil.copy2(FIXTURE / archive, source / archive)
        out = base / case / "out"
        done = subprocess.run(
            [
                sys.executable,
                str(engine_source.engine_scripts_dir(engine) / "migrate_estate.py"),
                "-i",
                str(source),
                "-o",
                str(out),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=1800,
            check=False,
        )
        assert done.returncode == 0, f"harness failure running the engine on {archive}:\n{done.stdout}\n{done.stderr}"
        files = [p for p in out.rglob("*") if p.is_file()]
        dirs = [p for p in out.rglob("*") if p.is_dir()]
        deepest = max(files, key=lambda p, root=out: utf16_len(str(p.relative_to(root))))
        runs[case] = {
            "out": out,
            "files": len(files),
            "dirs": len(dirs),
            "deepest_tail": str(deepest.relative_to(out)).replace("\\", "/"),
            "deepest_tail_len": utf16_len(str(deepest.relative_to(out))),
            "deepest_dir_tail_len": max(utf16_len(str(p.relative_to(out))) for p in dirs),
            "pbip_tail_len": max(utf16_len(str(p.relative_to(out))) for p in out.rglob("*.pbip")),
        }
    return {"version": engine_source.engine_version(engine), "cases": runs}


@requires_engine
def test_the_long_case_crosses_the_file_ceiling_at_the_skill_default_root(engine_runs) -> None:
    """The repro's claim, measured on emitted paths at the skill's own 22-unit root length."""
    long_case = engine_runs["cases"]["long"]
    at_root = SKILL_ROOT_LEN + 1 + long_case["deepest_tail_len"]
    assert at_root > FILE_CEILING, (
        f"the long case measures {at_root} at a {SKILL_ROOT_LEN}-unit root "
        f"({long_case['deepest_tail']!r}, tail {long_case['deepest_tail_len']}) on engine "
        f"{engine_runs['version']} - it no longer reproduces issue #194. If an upstream cap now "
        "covers the table filename, that is the fix; retire this fixture rather than padding it."
    )
    assert ".SemanticModel/definition/tables/" in long_case["deepest_tail"], (
        f"the offender moved to {long_case['deepest_tail']!r}. This repro is specifically about the "
        "UNCAPPED semantic-model table filename; a different offender is a different report."
    )
    assert SKILL_ROOT_LEN + 1 + long_case["pbip_tail_len"] <= FILE_CEILING, (
        "the .pbip pointer itself must stay short - the point of the repro is that a legal entry "
        "file still cannot be opened because of a nested required child"
    )


@requires_engine
def test_the_short_control_stays_inside_both_ceilings_at_the_same_root(engine_runs) -> None:
    """The A/B control: same shape, same data, short names, comfortably legal."""
    short_case = engine_runs["cases"]["short"]
    file_at_root = SKILL_ROOT_LEN + 1 + short_case["deepest_tail_len"]
    dir_at_root = SKILL_ROOT_LEN + 1 + short_case["deepest_dir_tail_len"]
    assert file_at_root <= FILE_CEILING, f"short control file {file_at_root} over {FILE_CEILING}"
    assert dir_at_root <= DIR_CEILING, f"short control directory {dir_at_root} over {DIR_CEILING}"
    long_case = engine_runs["cases"]["long"]
    assert (short_case["files"], short_case["dirs"]) == (long_case["files"], long_case["dirs"]), (
        f"the two cases emitted different structures ({short_case['files']}/{short_case['dirs']} vs "
        f"{long_case['files']}/{long_case['dirs']}); the A/B would then differ in more than names"
    )
