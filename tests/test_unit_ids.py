"""Cross-run unit IDs (#836 slice 1): allocator, per-run dictionary, README projection, lookup."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import t2p  # noqa: E402
import unit_ids  # noqa: E402
from unit_ids import UnitRequest  # noqa: E402
import work_dirs  # noqa: E402


def _run(parent: Path, slug: str) -> Path:
    return work_dirs.allocate_run(slug, repo_root=parent).root


def _ids(entries: list[dict]) -> list[str]:
    return [entry["id"] for entry in entries]


def _interrupt(parent: Path, number: int) -> None:
    """A reservation whose owner pointer never landed: a crash between mkdir and pointer write."""
    (parent / "_runs" / ".unit-id-reservations" / str(number).zfill(4)).mkdir(parents=True)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under root with its bytes (None for a directory), so any write shows up."""
    return {str(path): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def _edit_json(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_counter_spans_runs_and_never_reuses_a_gap_or_interrupted_reservation(tmp_path: Path) -> None:
    first, second = _run(tmp_path, "first"), _run(tmp_path, "second")
    created = unit_ids.allocate_unit_ids(
        first, [UnitRequest("assets/a.twbx", "workbook", "A"), UnitRequest("assets/d.tdsx", "datasource", "D")]
    )
    _interrupt(tmp_path, 4)
    later = unit_ids.allocate_unit_ids(second, [UnitRequest("assets/a.twbx", "workbook", "A")])

    assert _ids(created) == ["u0001", "u0002"]
    assert _ids(later) == ["u0005"], "next is highest-ever + 1: neither the gap u0003 nor interrupted u0004"
    shown = unit_ids.show_unit(tmp_path / "_runs", "u0005")
    assert (shown["result"], shown["candidates"][0]["run"]) == ("found", second.name), "ID-only lookup"
    interrupted = unit_ids.show_unit(tmp_path / "_runs", "u0004")
    assert (interrupted["result"], interrupted["reason"]) == ("unassessable", "reservation_interrupted")
    assert unit_ids.show_unit(tmp_path / "_runs", "u0003")["result"] == "missing"


def test_rerun_in_the_same_run_reuses_ids_for_exact_keys_and_regenerates_the_readme(tmp_path: Path) -> None:
    run = _run(tmp_path, "rerun")
    first = unit_ids.allocate_unit_ids(run, [UnitRequest("assets/Sales.twbx", "workbook", "Sales")])
    again = unit_ids.allocate_unit_ids(
        run,
        [
            UnitRequest("assets/Sales.twbx", "workbook", "Sales Renamed", project_name="Finance"),
            UnitRequest("assets/sales.twbx", "workbook", "Sales"),
        ],
    )

    assert _ids(first) == ["u0001"]
    assert _ids(again) == ["u0001", "u0002"], "same exact key reuses; a case-different key is another unit"
    index = json.loads((run / unit_ids.INDEX_FILE_NAME).read_text(encoding="utf-8"))
    assert index["units"][0]["source"]["name"] == "Sales Renamed", "a source rename updates display metadata"
    readme = (run / unit_ids.README_FILE_NAME).read_text(encoding="utf-8")
    assert "| u0001 | Workbook | Sales Renamed | Finance | not emitted |" in readme
    assert unit_ids.render_units_readme(run.name, index["units"]) == readme, "README is a projection of the index"
    unit_ids.set_unit_artifacts(run, "u0001", {"project_file": "bundle/pbip/Sales Renamed.pbip"})
    readme = (run / unit_ids.README_FILE_NAME).read_text(encoding="utf-8")
    assert "[Sales Renamed.pbip](bundle/pbip/Sales%20Renamed.pbip) (project file)" in readme


def test_an_identical_retry_repairs_an_interrupted_readme_publication(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, "retry")
    unit_ids.allocate_unit_ids(run, [UnitRequest("k", "workbook", "Sales")])
    renamed = [UnitRequest("k", "workbook", "Sales Renamed")]
    write = unit_ids._atomic_write_text  # pylint: disable=protected-access

    def fail_readme(target: Path, text: str) -> None:
        if target.name == unit_ids.README_FILE_NAME:
            raise OSError("injected after the index commit")
        write(target, text)

    monkeypatch.setattr(unit_ids, "_atomic_write_text", fail_readme)
    with pytest.raises(OSError):
        unit_ids.allocate_unit_ids(run, renamed)
    monkeypatch.undo()
    unit_ids.allocate_unit_ids(run, renamed)

    index = json.loads((run / unit_ids.INDEX_FILE_NAME).read_text(encoding="utf-8"))
    readme = (run / unit_ids.README_FILE_NAME).read_text(encoding="utf-8")
    assert "Sales Renamed" in readme and readme == unit_ids.render_units_readme(run.name, index["units"])


def test_an_index_entry_owned_by_another_run_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    first, second = _run(tmp_path, "first"), _run(tmp_path, "second")
    request = UnitRequest("k", "workbook", "W")
    unit_ids.allocate_unit_ids(first, [request])
    unit_ids.allocate_unit_ids(second, [request])
    _edit_json(second / unit_ids.INDEX_FILE_NAME, lambda index: index["units"][0].update(id="u0001"))
    before = _snapshot(tmp_path / "_runs")

    with pytest.raises(unit_ids.UnitIdError, match="not reserved for this run and key"):
        unit_ids.allocate_unit_ids(second, [request, UnitRequest("new", "workbook", "N")])
    with pytest.raises(unit_ids.UnitIdError, match="not reserved for this run and key"):
        unit_ids.set_unit_artifacts(second, "u0001", {"report": "r.Report"})
    assert _snapshot(tmp_path / "_runs") == before, "no index, README or reservation was written"


@pytest.mark.parametrize(
    ("prior", "requests"),
    [
        ([UnitRequest("k", "workbook", "W")], [UnitRequest("k", "datasource", "W")]),
        ([], [UnitRequest("k", "workbook", "W"), UnitRequest("k", "datasource", "W")]),
    ],
    ids=["kind_conflict", "duplicate_request"],
)
def test_conflicting_requests_are_refused_before_reserving(tmp_path: Path, prior: list, requests: list) -> None:
    run = _run(tmp_path, "conflict")
    if prior:
        unit_ids.allocate_unit_ids(run, prior)
    before = _snapshot(tmp_path / "_runs")

    with pytest.raises(unit_ids.UnitIdError, match="another kind|same unit key twice"):
        unit_ids.allocate_unit_ids(run, requests)
    assert _snapshot(tmp_path / "_runs") == before


def test_concurrent_allocations_in_one_root_get_distinct_ids(tmp_path: Path) -> None:
    runs = [_run(tmp_path, f"race{index}") for index in range(8)]
    barrier = threading.Barrier(len(runs))
    results: dict[str, list[str]] = {}

    def allocate(run: Path) -> None:
        barrier.wait()
        requests = [UnitRequest(f"assets/{index}.twbx", "workbook", "W") for index in range(4)]
        results[run.name] = _ids(unit_ids.allocate_unit_ids(run, requests))

    threads = [threading.Thread(target=allocate, args=(run,)) for run in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    allocated = [unit_id for ids in results.values() for unit_id in ids]
    assert len(results) == len(runs) and len(allocated) == len(set(allocated)) == 32
    listed = unit_ids.list_units(tmp_path / "_runs")
    assert listed["result"] == "found" and all(row["state"] == "found" for row in listed["candidates"])


def test_a_stale_counter_read_cannot_take_a_reserved_number(tmp_path: Path, monkeypatch) -> None:
    """The atomic mkdir, not the counter read, owns the claim: a racer reserved u0001 first."""
    run = _run(tmp_path, "stale")
    _interrupt(tmp_path, 1)
    monkeypatch.setattr(unit_ids, "highest_reserved", lambda _root: 0)

    assert _ids(unit_ids.allocate_unit_ids(run, [UnitRequest("k", "workbook", "W")])) == ["u0002"]


def test_find_returns_every_duplicate_and_reports_legacy_runs(tmp_path: Path) -> None:
    for slug in ("east", "west"):
        unit_ids.allocate_unit_ids(_run(tmp_path, slug), [UnitRequest("k", "workbook", "Sales Overview")])
    legacy = _run(tmp_path, "legacy")
    root = tmp_path / "_runs"

    found = unit_ids.find_units(root, name="sales overview")
    assert (found["result"], _ids(found["candidates"])) == ("ambiguous", ["u0001", "u0002"])
    assert unit_ids.lookup_exit_code(found) == 1
    assert {row["run"]: row["state"] for row in found["runs"]}[legacy.name] == "legacy"
    assert unit_ids.find_units(root, name="nothing")["result"] == "legacy", "legacy runs were not searched"
    assert unit_ids.read_unit_index(legacy) is None and not (legacy / unit_ids.INDEX_FILE_NAME).exists()


def test_run_discovery_ignores_the_unit_id_metadata_directory(tmp_path: Path) -> None:
    unit_ids.allocate_unit_ids(_run(tmp_path, "only"), [UnitRequest("k", "datasource", "D")])
    assert (tmp_path / "_runs" / ".unit-id-reservations" / "0001" / "owner.json").is_file()

    runs, counts = work_dirs.verify_runs(tmp_path)
    assert [Path(run["root"]).name for run in runs] == ["001-only"]
    assert work_dirs.verify_exit_code(counts) == 0
    assert [Path(run["root"]).name for run in work_dirs.list_runs(tmp_path)] == ["001-only"]


@pytest.mark.parametrize("escape", ["../outside", "/abs", "a/../../x", "C:/x", "a\\b", "./a", ""])
def test_escaping_artifact_paths_are_refused_on_write(tmp_path: Path, escape: str) -> None:
    run = _run(tmp_path, "esc")
    unit_ids.allocate_unit_ids(run, [UnitRequest("k", "workbook", "W")])
    before = (run / unit_ids.INDEX_FILE_NAME).read_bytes()

    with pytest.raises(unit_ids.UnitIdError):
        unit_ids.set_unit_artifacts(run, "u0001", {"report": escape})
    assert (run / unit_ids.INDEX_FILE_NAME).read_bytes() == before


def test_an_artifact_path_through_a_link_out_of_the_run_is_refused(tmp_path: Path) -> None:
    run = _run(tmp_path, "link")
    unit_ids.allocate_unit_ids(run, [UnitRequest("k", "workbook", "W")])
    outside = tmp_path / "outside"
    outside.mkdir()
    if sys.platform == "win32":
        import _winapi  # pylint: disable=import-outside-toplevel

        _winapi.CreateJunction(str(outside), str(run / "linked"))
    else:
        (run / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(unit_ids.UnitIdError, match="outside its run"):
        unit_ids.set_unit_artifacts(run, "u0001", {"report": "linked/x.Report"})


def test_a_second_writer_on_the_same_run_is_refused_not_taken_over(tmp_path: Path) -> None:
    run = _run(tmp_path, "locked")
    (run / unit_ids.LOCK_FILE_NAME).write_text("held", encoding="utf-8")

    with pytest.raises(unit_ids.UnitIdError, match="unit-index.lock exists"):
        unit_ids.allocate_unit_ids(run, [UnitRequest("k", "workbook", "W")])
    assert (run / unit_ids.LOCK_FILE_NAME).read_text(encoding="utf-8") == "held"
    assert not (run / unit_ids.INDEX_FILE_NAME).exists()
    assert not (tmp_path / "_runs" / ".unit-id-reservations").exists(), "a refused writer reserves nothing"


def test_escaping_or_corrupt_index_is_unassessable_on_read(tmp_path: Path) -> None:
    run = _run(tmp_path, "esc")
    unit_ids.allocate_unit_ids(run, [UnitRequest("k", "workbook", "W")])
    index_path = run / unit_ids.INDEX_FILE_NAME
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["units"][0]["artifacts"]["report"] = "../../escape.Report"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    root = tmp_path / "_runs"

    shown = unit_ids.show_unit(root, "u0001")
    assert (shown["result"], shown["reason"]) == ("unassessable", "artifact_path_escapes_run")
    assert unit_ids.lookup_exit_code(shown) == 3
    assert unit_ids.list_units(root)["result"] == "unassessable"
    index_path.write_text("{", encoding="utf-8")
    assert unit_ids.find_units(root, name="W")["reason"] == "index_invalid"


def _tamper_index(change):
    return lambda run, _root: _edit_json(run / unit_ids.INDEX_FILE_NAME, change)


def _tamper_owner(field: str, value: str):
    owner = Path(".unit-id-reservations", "0001", "owner.json")
    return lambda _run, root: _edit_json(root / owner, lambda data: data.update({field: value}))


def _duplicate_run_key(run: Path, _root: Path) -> None:
    index_path = run / unit_ids.INDEX_FILE_NAME
    text = index_path.read_text(encoding="utf-8")
    index_path.write_text(text.replace(f'"run": "{run.name}"', f'"run": "999-x", "run": "{run.name}"'), "utf-8")


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (_tamper_index(lambda index: index.update(schema_version=2)), "index_invalid"),
        (_tamper_index(lambda index: index.update(run="999-x")), "index_run_mismatch"),
        (_tamper_index(lambda index: index["units"][1].update(id="u0001")), "index_invalid"),
        (_tamper_index(lambda index: index["units"][1].update(key="k1")), "index_invalid"),
        (_duplicate_run_key, "index_invalid"),
        (_tamper_owner("key", "other"), "owner_index_mismatch"),
        (_tamper_owner("run", "999-x"), "owner_index_mismatch"),
        (_tamper_owner("id", "u0002"), "owner_pointer_invalid"),
    ],
    ids=["version", "wrong_run", "dup_id", "dup_key", "dup_json_key", "owner_key", "owner_run", "owner_id"],
)
def test_untrusted_index_or_owner_pointer_makes_the_survey_unassessable(tmp_path: Path, tamper, reason: str) -> None:
    run = _run(tmp_path, "tamper")
    unit_ids.allocate_unit_ids(run, [UnitRequest("k1", "workbook", "A"), UnitRequest("k2", "workbook", "B")])
    root = tmp_path / "_runs"
    tamper(run, root)

    listed = unit_ids.list_units(root)
    assert (listed["result"], listed["reason"]) == ("unassessable", reason)


def test_readme_escapes_labels_and_lookups_filter_and_report_missing(tmp_path: Path) -> None:
    run = _run(tmp_path, "md")
    unit_ids.allocate_unit_ids(
        run, [UnitRequest("k1", "workbook", "a|b [x](y)", luid="L1"), UnitRequest("k2", "workbook", "plain")]
    )
    root = tmp_path / "_runs"

    readme = (run / unit_ids.README_FILE_NAME).read_text(encoding="utf-8")
    assert "| a\\|b \\[x\\](y) |" in readme
    assert readme.split("## Source identities\n\n")[1] == "- u0001: luid=L1\n", "only units with a LUID"
    assert _ids(unit_ids.find_units(root, luid="l1")["candidates"]) == ["u0001"]
    for lookup in (unit_ids.show_unit(root, "u0009"), unit_ids.find_units(root, name="zzz")):
        assert (lookup["result"], unit_ids.lookup_exit_code(lookup)) == ("missing", 1)


def test_ids_widen_past_9999_and_parse_only_canonical_spellings(tmp_path: Path) -> None:
    run = _run(tmp_path, "wide")
    _interrupt(tmp_path, 9999)

    assert _ids(unit_ids.allocate_unit_ids(run, [UnitRequest("k", "workbook", "W")])) == ["u10000"]
    assert unit_ids.show_unit(tmp_path / "_runs", "u10000")["result"] == "found"
    assert [unit_ids.parse_unit_id(text) for text in ("u0001", "u00001", "u1", "u0000")] == [1, None, None, None]


def test_t2p_units_envelope_is_read_only_and_schema_valid(tmp_path: Path, capsys) -> None:
    run = _run(tmp_path, "cli")
    unit_ids.allocate_unit_ids(
        run,
        [UnitRequest("k", "workbook", "W", luid="L1", project_name="Finance"), UnitRequest("k2", "workbook", "X")],
    )
    unit_ids.set_unit_artifacts(run, "u0001", {"report": "b/W.Report", "semantic_model": "b/W.SemanticModel"})
    parent = ["--runs-parent", str(tmp_path)]
    before = _snapshot(tmp_path)

    def lookup(*argv: str) -> tuple[int, dict]:
        code = t2p.main(["units", *argv, *parent, "--json"])
        return code, json.loads(capsys.readouterr().out)

    code, result = lookup("show", "u0001")
    jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
    assert (code, result["status"], result["unit_lookup"]["candidates"][0]["run"]) == (0, "ok", "001-cli")
    code, result = lookup("list")
    assert (code, result["unit_lookup"]["action"]) == (0, "list")
    assert _ids(result["unit_lookup"]["candidates"]) == ["u0001", "u0002"]
    code, result = lookup("find", "--name", "x")
    assert (code, _ids(result["unit_lookup"]["candidates"])) == (0, ["u0002"])
    assert t2p.main(["units", "find", *parent, "--json"]) == 2, "find needs --name/--luid"
    capsys.readouterr()
    assert t2p.main(["units", "show", "u0001", *parent]) == 0
    human = capsys.readouterr().out
    assert "project=Finance; luid=L1" in human and "semantic_model=b/W.SemanticModel; report=b/W.Report" in human
    assert _snapshot(tmp_path) == before, "lookup must not write"
    (run / unit_ids.INDEX_FILE_NAME).write_text("{", encoding="utf-8")
    code, result = lookup("show", "u0001")
    assert (code, result["status"]) == (3, "cannot_establish")
