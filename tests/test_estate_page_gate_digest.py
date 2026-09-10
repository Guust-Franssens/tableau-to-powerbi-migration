"""Tool-boundary tests for `tests/estate_page_gate_digest.py` (issue #562).

Scope, deliberately narrow: this file covers the ONE production consumer of `check_unit`'s direct
target-bearing helpers. The measurement itself is pinned by `tests/estate_page_gate_expected.json`
against a real estate; what is asserted here is the boundary contract that estate run depends on -
a staged unit whose package boundary cannot be established must not produce a clean digest, and no
supplied path component may travel out of the refusal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import check_unit as cu  # noqa: E402  # pylint: disable=wrong-import-position
import estate_page_gate_digest as epgd  # noqa: E402  # pylint: disable=wrong-import-position

UNIT_LUID = "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"


def _write_unit(pbip_unit: Path, specs: Path, name: str) -> None:
    """One engine `pbip/<unit>` plus its parsed spec: a single dashboard page carrying one visual."""
    page = pbip_unit / "Book.Report" / "definition" / "pages" / "p1"
    (page / "visuals" / "v0").mkdir(parents=True)
    (page / "visuals" / "v0" / "visual.json").write_text(json.dumps({"name": "v0"}), encoding="utf-8")
    (page / "page.json").write_text(
        json.dumps({"name": "p1", "displayName": "Revenue", "width": 1600, "height": 900}), encoding="utf-8"
    )
    (page.parent / "pages.json").write_text(json.dumps({"pageOrder": ["p1"]}), encoding="utf-8")
    specs.mkdir(parents=True, exist_ok=True)
    (specs / f"{name}.json").write_text(
        json.dumps(
            {
                "source": {"file_name": f"{UNIT_LUID}_Book.twbx"},
                "dashboards": [{"id": "dash.0", "name": "Revenue"}],
                "worksheets": [],
            }
        ),
        encoding="utf-8",
    )


def _write_oracle(oracle: Path) -> None:
    """A shared Tableau-Server capture with one certified render + data leg for `Revenue`."""
    (oracle / "images").mkdir(parents=True, exist_ok=True)
    (oracle / "data").mkdir(parents=True, exist_ok=True)
    (oracle / "images" / "Revenue__0.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    (oracle / "data" / "Revenue__0.csv").write_text("a\n1\n", encoding="utf-8")
    (oracle / "oracle-manifest.json").write_text(
        json.dumps(
            {
                "views": [
                    {
                        "view_name": "Revenue",
                        "view_type": "dashboard",
                        "workbook_luid": UNIT_LUID,
                        "data": {
                            "status": "ok",
                            "certification": "certified",
                            "path": "data/Revenue__0.csv",
                            "row_count": 1,
                        },
                        "image": {"status": "ok", "path": "images/Revenue__0.png"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _estate(tmp_path: Path, unit_name: str = "Unit") -> tuple[Path, Path, Path]:
    bundle = tmp_path / "bundle"
    specs = tmp_path / "specs"
    oracle = tmp_path / "oracle"
    _write_unit(bundle / "pbip" / unit_name, specs, unit_name)
    (bundle / "handover").mkdir(parents=True, exist_ok=True)
    _write_oracle(oracle)
    return bundle, specs, oracle


def _run(
    bundle: Path,
    specs: Path,
    work: Path,
    out: Path,
    *,
    oracle: Path | None = None,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the tool exactly as documented, as a subprocess, so stderr is a real channel."""
    argv = [
        sys.executable,
        str(REPO_ROOT / "tests" / "estate_page_gate_digest.py"),
        "--bundle",
        str(bundle),
        "--specs",
        str(specs),
        "--work",
        str(work),
        "--json",
        str(out),
        *extra,
    ]
    if oracle is not None:
        argv += ["--oracle", str(oracle)]
    return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def test_a_safe_staged_estate_measures_exactly_what_it_measured_before(tmp_path: Path) -> None:
    """The positive control: an ordinary staged unit's summary and digest are pinned literally.

    Pinned as a whole dict rather than field-by-field, because the claim being protected is that the
    direct-helper guard changed NOTHING for a safe target - a summary that merely still had the right
    shape would not establish it. The four catching helpers all run here: `load_exemptions`,
    `page_expectation` and `check_page_parity` through the verdict, `check_oracle_coverage` twice.

    ⚠️ Scope: `oracle_layer_facts` is deliberately NOT exercised. Its synthetic kind-less probe
    passes `workbook=None` into `WorkbookIdentity.attribute`, which raises `AttributeError` on the
    current `object_identity` - a pre-existing defect unrelated to this boundary and outside this
    change's closed surface, reproduced in the PR report rather than fixed here.
    """
    bundle, specs, oracle = _estate(tmp_path)
    units, unstaged = epgd.stage(bundle, specs, tmp_path / "work")

    summary = epgd.measure(units, oracle)

    assert unstaged == []
    assert summary == {
        "units": 1,
        "verdicts": {"PASS": 1},
        "dispositions": {},
        "blank_pages": 0,
        "unaccounted_extra_pages": 0,
        "contested_names": 0,
        "disagreements": [],
    }
    assert epgd.digest(summary) == "8e2866d250bd56b0b4b7a0705ff34b162bc3ca1f1ff59fd9f245dd9bc2c39934"


@pytest.mark.parametrize("with_oracle", [True, False], ids=["oracle", "no-oracle"])
def test_a_staged_unit_whose_boundary_is_unproven_exits_unmeasurable_without_leaking_a_component(
    tmp_path: Path, with_oracle: bool
) -> None:
    """Kills: gating the boundary refusal on `--oracle`, and any leak through stderr.

    Staged into a `packages/` directory - the real shape an operator produces by pointing `--work` at
    a run's package root - every staged unit is lexically package-shaped with no
    `package-manifest.json`, so its boundary is unproven.

    ⚠️ The **`no-oracle`** case is the one that was wrong: the refusal used to be noticed only where
    it happened to surface, inside the `--oracle` layer, so a run without `--oracle` collected the
    resulting `NOT_CHECKED` rows as if they were data and published a digest anyway (exit 1). A
    measurement that could not be made must not report a clean digest either way, and the message is
    a CONSTANT plus the classifier's stable code: the unit here is named for a customer, which is
    exactly the component that must not travel.
    """
    bundle, specs, oracle = _estate(tmp_path, unit_name="Contoso-Secret")
    work = tmp_path / "run" / "packages"
    out = tmp_path / "result.json"

    completed = _run(bundle, specs, work, out, oracle=oracle if with_oracle else None)

    assert completed.returncode == epgd.EXIT_UNMEASURABLE, completed.stdout + completed.stderr
    assert epgd.REFUSED_BOUNDARY_MESSAGE in completed.stderr
    assert not out.exists(), "an unmeasurable run must not write a digest"
    assert "sha256" not in completed.stdout, "an unmeasurable run must not publish a digest on stdout"
    everywhere = completed.stdout + completed.stderr
    for supplied in (str(work), str(bundle), str(tmp_path), "Contoso-Secret"):
        assert supplied not in everywhere, supplied
    assert "Traceback" not in everywhere


def test_a_safe_estate_without_oracle_still_publishes_its_digest(tmp_path: Path) -> None:
    """The control that keeps the refusal above from being "the tool refuses everything now".

    An ordinary staged unit with no `--oracle` still measures and publishes: exit 1 only because this
    synthetic estate is not the committed expectation. `--oracle` is deliberately absent here - see
    the scope note on the summary control above.
    """
    bundle, specs, _oracle = _estate(tmp_path)
    out = tmp_path / "result.json"

    completed = _run(bundle, specs, tmp_path / "work", out)

    assert completed.returncode == epgd.EXIT_DIFFERS, completed.stdout + completed.stderr
    assert epgd.REFUSED_BOUNDARY_MESSAGE not in completed.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["sha256"] == (
        "8e2866d250bd56b0b4b7a0705ff34b162bc3ca1f1ff59fd9f245dd9bc2c39934"
    )


def _main_with_redirected_expectation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, expectation: Path, argv: list[str]
) -> int:
    """Run `main` in-process with `EXPECTED` redirected, so `--update` can never touch the real one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(epgd, "EXPECTED", expectation)
    return epgd.main(argv)


def test_update_refuses_an_unproven_boundary_and_leaves_the_expectation_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Kills: letting `--update` past the boundary gate.

    `--update` is the worst case: it returns **0** and REWRITES the committed expectation, so a
    boundary that was never established would become the number every later run is compared against.
    The expectation file is asserted byte-identical, and so is the real committed one.
    """
    bundle, specs, _oracle = _estate(tmp_path, unit_name="Contoso-Secret")
    expectation = tmp_path / "expected.json"
    expectation.write_bytes(b'{"sha256": "sentinel"}\n')
    committed = REPO_ROOT / "tests" / "estate_page_gate_expected.json"
    committed_before = committed.read_bytes()
    out = tmp_path / "result.json"

    code = _main_with_redirected_expectation(
        monkeypatch,
        tmp_path,
        expectation,
        [
            "--bundle",
            str(bundle),
            "--specs",
            str(specs),
            "--work",
            str(tmp_path / "run" / "packages"),
            "--json",
            str(out),
            "--update",
        ],
    )

    assert code == epgd.EXIT_UNMEASURABLE
    assert expectation.read_bytes() == b'{"sha256": "sentinel"}\n'
    assert committed.read_bytes() == committed_before, "the committed expectation must never be touched"
    assert not out.exists()
    assert epgd.REFUSED_BOUNDARY_MESSAGE in capsys.readouterr().err


def test_update_still_rewrites_the_expectation_for_a_safe_estate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paired control: `--update` is not simply broken now - a safe estate still rewrites."""
    bundle, specs, _oracle = _estate(tmp_path)
    expectation = tmp_path / "expected.json"
    expectation.write_bytes(b'{"sha256": "sentinel"}\n')

    code = _main_with_redirected_expectation(
        monkeypatch,
        tmp_path,
        expectation,
        ["--bundle", str(bundle), "--specs", str(specs), "--work", str(tmp_path / "work"), "--update"],
    )

    assert code == epgd.EXIT_MATCH
    assert json.loads(expectation.read_text(encoding="utf-8"))["sha256"] == (
        "8e2866d250bd56b0b4b7a0705ff34b162bc3ca1f1ff59fd9f245dd9bc2c39934"
    )


def test_the_boundary_gate_also_refuses_an_aliased_staged_unit(tmp_path: Path) -> None:
    """The alias shape, through the SAME mechanism - cheap, because no staging is needed.

    `stage()` only ever creates real directories, so an aliased unit cannot arise from it; the gate
    is asked directly instead, which is the function `main` calls with the same spelling.
    """
    bundle, specs, _oracle = _estate(tmp_path)
    units, _unstaged = epgd.stage(bundle, specs, tmp_path / "work")
    alias = tmp_path / "alias"
    if sys.platform == "win32":
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(units[0])], capture_output=True, check=False
        )
        if linked.returncode != 0:
            pytest.skip(f"could not create junction: {linked.stderr.decode(errors='replace').strip()}")
    else:
        try:
            alias.symlink_to(units[0], target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - privilege-dependent
            pytest.skip("this platform/account cannot create symlinks without elevation")

    assert epgd._refused_boundary(units) is None  # pylint: disable=protected-access
    assert epgd._refused_boundary([alias]) is not None  # pylint: disable=protected-access


def test_the_boundary_gate_catches_only_the_typed_refusal_and_not_every_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills: broadening the gate's `except` to `Exception`.

    A blanket catch would turn any unrelated failure into a tidy exit 3 - the
    unassessable-collapsing-into-clean shape this gate exists to remove, inverted. An unrelated error
    must still propagate.
    """
    bundle, specs, _oracle = _estate(tmp_path)
    monkeypatch.chdir(tmp_path)

    def unrelated(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError("unrelated")

    monkeypatch.setattr(cu, "_checked_direct_target", unrelated)

    with pytest.raises(RuntimeError, match="unrelated"):
        epgd.main(["--bundle", str(bundle), "--specs", str(specs), "--work", str(tmp_path / "work")])
