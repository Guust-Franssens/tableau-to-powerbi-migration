"""Tests for the AI-readiness layer bundled in this skill.

Two factual errors shipped in the first two hand-written `ai-instructions.md` files, and both were
found by human/model review rather than by any check:

  * wind-energy described `'NL Densification'` - a disconnected spiral-geometry scaffold - as
    "geography for the map";
  * it told Copilot to compare by "ANSP", a term belonging to an entirely different migration.

Instructions that name the wrong object are worse than no instructions: they actively steer Copilot
to the wrong table or to a concept that does not exist. These tests lock in the checks added in
response, and the gate's ability to FAIL - it previously exited 0 unconditionally, so it could never
have blocked anything.
"""

from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path

import check_ai_readiness
import pytest
import set_ai_instructions
from set_ai_instructions import (
    cmd_check,
    host_root,
    lint_instructions,
    model_object_names,
    read_qna_enabled,
    unresolved_references,
)

SKILL_ROOT = Path(__file__).resolve().parents[1]


def _example_models() -> list[Path]:
    """The host repo's committed `examples/` corpus, if it has one.

    Ground truth for a fabricated-reference check has to come from real models. They are a HOST-repo
    fixture, though, not part of the skill: a Qlik or Cognos repo that copies this folder has no
    `examples/` tree, so the corpus tests must skip with a reason there rather than fail (or, worse,
    silently parametrize to nothing). `tests/test_skills.py` in this repo asserts the corpus is
    non-empty HERE, so the skip cannot quietly turn into a no-op where it is meant to run.
    """
    for parent in Path(__file__).resolve().parents:
        models = sorted((parent / "examples").glob("*/fabric/*.SemanticModel"))
        if models:
            return models
    return []


EXAMPLE_MODELS = _example_models()
WIND_MODEL = next((m for m in EXAMPLE_MODELS if m.name == "WindEnergyUtilization.SemanticModel"), None)
needs_wind = pytest.mark.skipif(WIND_MODEL is None, reason="no wind-energy example model in this repo")
needs_corpus = pytest.mark.skipif(not EXAMPLE_MODELS, reason="no examples/*/fabric/*.SemanticModel corpus")


def test_the_suite_exercises_the_scripts_bundled_beside_it() -> None:
    """A copied skill must test ITS OWN scripts, not a same-named module from the host repo.

    This repo keeps forwarding shims at `scripts/set_ai_instructions.py` (so existing agent
    invocations do not break), which is exactly the shape that could shadow the real modules if
    anything put the host repo's `scripts/` on `sys.path` first. Then every test below would pass
    while proving nothing about the files that actually ship.
    """
    for module in (set_ai_instructions, check_ai_readiness):
        assert Path(module.__file__).resolve().parent == SKILL_ROOT / "scripts", (
            f"{module.__name__} was imported from {module.__file__}, not from this skill's scripts/"
        )


@needs_wind
def test_model_object_names_reads_the_tmdl() -> None:
    names = model_object_names(WIND_MODEL)
    assert len(names) > 50, "should find the model's tables/columns/measures"
    assert "NL Densification" in names


@needs_wind
def test_fabricated_references_are_detected() -> None:
    """The class of error that shipped: naming an object the model does not declare."""
    known = model_object_names(WIND_MODEL)
    text = "Use `'Nonexistent Table'` for the map and report [Fake Measure]."
    assert unresolved_references(text, known) == ["Fake Measure", "Nonexistent Table"]


@needs_wind
def test_placeholders_are_not_flagged() -> None:
    """Templates and globs are deliberate, not claims about the schema - flagging them cries wolf."""
    known = model_object_names(WIND_MODEL)
    text = "`'* Parameter'` tables are proxies; `[<metric> Delta]` is the pattern; `[... Cars Offset]`."
    assert unresolved_references(text, known) == []


@needs_corpus
def test_shipped_instructions_reference_only_real_objects() -> None:
    """Every hand-written ai-instructions.md must resolve against its own model.

    This is the regression guard for the two errors above.
    """
    offenders: dict[str, list[str]] = {}
    for model in EXAMPLE_MODELS:
        md = model.parents[1] / "ai-instructions.md"
        if not md.exists():
            continue
        missing = unresolved_references(md.read_text(encoding="utf-8"), model_object_names(model))
        if missing:
            offenders[md.parent.name] = missing
    assert not offenders, f"ai-instructions reference objects that do not exist: {offenders}"


@needs_wind
def test_lint_flags_unresolved_references() -> None:
    known = model_object_names(WIND_MODEL)
    warnings = lint_instructions("# X\n\n## Things to avoid\n- Use `'Ghost Table'`.\n", known)
    assert any("does NOT declare" in w for w in warnings)


@needs_corpus
def test_stamped_models_have_qna_enabled() -> None:
    """A stamped model with qnaEnabled false is a SILENT no-op - Copilot ignores the instructions."""
    for model in EXAMPLE_MODELS:
        culture = model / "definition" / "cultures" / "en-US.tmdl"
        if not culture.exists() or "CustomInstructions" not in culture.read_text(encoding="utf-8"):
            continue
        assert read_qna_enabled(model) is True, f"{model.name} is stamped but Q&A is disabled"


@needs_corpus
def test_owned_examples_match_their_markdown_and_exact_decoded_text_marker() -> None:
    """Committed examples use explicit ownership; no text change is licensed by adoption."""
    checked = []
    for model in EXAMPLE_MODELS:
        md = model.parents[1] / "ai-instructions.md"
        if model.parents[1].name not in {"health-tracker", "wind-energy-utilization"}:
            continue
        culture = model / "definition" / "cultures" / "en-US.tmdl"
        instr = set_ai_instructions.get_instructions(culture)
        pin = hashlib.sha256(instr.encode()).hexdigest()
        assert f'annotation T2P_CustomInstructionsSha256 = "{pin}"' in culture.read_text(encoding="utf-8")
        assert instr == md.read_text(encoding="utf-8").strip()
        assert cmd_check(model.parents[3], True, model, md) == 0
        checked.append(model.parents[1].name)
    assert set(checked) == {"health-tracker", "wind-energy-utilization"}


def test_scaffold_uses_a_real_linguistic_schema_version() -> None:
    """ "4.2.0" was copy-pasted from definition.pbism, a different schema.

    Power BI-generated culture files use 2.0.0 / 1.0.0. Measured 2026-07-30: a model published to
    Fabric with "4.2.0" round-tripped through `getDefinition` with CustomInstructions intact, so this
    is a correctness tidy-up rather than a fix for an observed data loss.
    """
    source = (SKILL_ROOT / "scripts" / "set_ai_instructions.py").read_text(encoding="utf-8")
    assert '"Version": "2.0.0"' in source
    assert '"Version": "4.2.0"' not in source
    for model in EXAMPLE_MODELS:
        for culture in model.glob("definition/cultures/*.tmdl"):
            assert '"Version": "4.2.0"' not in culture.read_text(encoding="utf-8"), culture


def test_host_root_finds_the_repo_that_owns_the_migrations(tmp_path: Path) -> None:
    """The scripts ship inside a skill folder, so their depth below the repo root is not fixed.

    A hard-coded `parents[N]` resolves to the skill folder itself, where every glob matches nothing -
    `--check` then reports "no models found" and exits 0: a clean pass over a repo it never read.
    """
    root = tmp_path / "host"
    (root / "migrations" / "workbooks").mkdir(parents=True)
    deep = root / ".github" / "skills" / "powerbi-ai-readiness" / "scripts"
    deep.mkdir(parents=True)
    assert host_root(deep / "set_ai_instructions.py") == root
    assert check_ai_readiness.host_root(deep / "check_ai_readiness.py") == root


def test_host_root_does_not_invent_a_root_when_there_is_no_migration_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copied into a repo with no migration tree, it must fall back rather than climb to the drive."""
    lonely = tmp_path / "somewhere" / "scripts"
    lonely.mkdir(parents=True)
    # The test's scratch root may itself live inside a host migration checkout.
    for module in (set_ai_instructions, check_ai_readiness):
        monkeypatch.setattr(module, "MIGRATION_TREES", ("no-such-fixture-migration-tree",))
    assert host_root(lonely / "set_ai_instructions.py") == Path.cwd()


def _fake_model(root: Path, slug: str, name: str, *, stamped: bool) -> Path:
    model = root / "examples" / slug / "fabric" / f"{name}.SemanticModel"
    (model / "definition" / "cultures").mkdir(parents=True, exist_ok=True)
    (model / "definition" / "model.tmdl").write_text("model Model\n\tculture: en-US\n", encoding="utf-8")
    if stamped:
        payload = json.dumps({"Version": "2.0.0", "CustomInstructions": "Latest = the max date in the data."})
        body = "\n".join("\t\t\t" + line for line in payload.splitlines())
        (model / "definition" / "cultures" / "en-US.tmdl").write_text(
            f"cultureInfo en-US\n\n\tlinguisticMetadata =\n{body}\n\n\t\tcontentType: json\n", encoding="utf-8"
        )
        (model / "definition.pbism").write_text(
            json.dumps({"version": "4.2.0", "settings": {"qnaEnabled": True}}), encoding="utf-8"
        )
    return model


def test_check_can_be_scoped_to_one_model(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """`--strict` is only usable by an agent if it can gate the model it just built.

    Repo-wide `--strict` fails on every model that predates this layer, so an unscoped gate is one
    nobody can ever turn on - which is why the gate added alongside `--strict` was never wired
    anywhere. Scoping is what turns it from a slogan into a hand-off check.
    """
    root = tmp_path / "host"
    stamped = _fake_model(root, "good", "Good", stamped=True)
    bare = _fake_model(root, "legacy", "Legacy", stamped=False)

    with caplog.at_level(logging.INFO):
        assert cmd_check(root, strict=True) == 1, "repo-wide strict must still fail on the legacy model"
        caplog.clear()
        assert cmd_check(root, strict=True, model_dir=stamped) == 0, "scoped strict must pass on the stamped model"
        scoped_report = caplog.text
        caplog.clear()
        assert cmd_check(root, strict=True, model_dir=bare) == 1, "scoped strict must fail on the bare model"
    assert "Legacy.SemanticModel" not in scoped_report, "a scoped check must not report models outside its scope"


def _stamp_fixture(root: Path, existing: str | None = None, marker: str = "") -> tuple[Path, Path, Path]:
    model = _fake_model(root, "fixture", "Fixture", stamped=False)
    md = model.parents[1] / "ai-instructions.md"
    md.write_bytes(b"# Rules\nLatest = max date; avoid today.\n")
    (model / "definition.pbism").write_bytes(
        b'{ "version": "4.2.0", "settings": { "qnaEnabled" : false }, "n":2.50 }\n'
    )
    culture = model / "definition" / "cultures" / "en-US.tmdl"
    payload = '{"Version":"2.0.0","Entities":{"a":"\\\\u0061"},"n":1e3'
    if existing is not None:
        payload += ',"CustomInstructions":' + json.dumps(existing)
    culture.write_bytes(
        (
            "/// description\ncultureInfo en-US\n\n\tlinguisticMetadata = " + payload + "}\n"
            "\t\tcontentType: json\n" + marker + "\n\tannotation Keep = untouched\n"
            "\n\ttranslations\n\t\ttable Sales\n\t\t\tcaption: Ventes\n"
        ).encode()
    )
    return model, md, culture


def _snapshot(model: Path) -> dict[str, bytes]:
    return {str(p.relative_to(model)): p.read_bytes() for p in model.rglob("*") if p.is_file()}


@pytest.mark.parametrize("md_eol", ["\n", "\r\n"])
def test_unowned_even_equal_refuses_and_hash_pinned_adoption_is_marker_only(tmp_path: Path, md_eol: str) -> None:
    model, md, culture = _stamp_fixture(tmp_path)
    instructions = md.read_text(encoding="utf-8").strip()
    model, md, culture = _stamp_fixture(tmp_path, instructions)
    md.write_bytes((instructions.replace("\n", md_eol) + md_eol).encode())
    before = _snapshot(model)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before
    pin = hashlib.sha256(instructions.encode()).hexdigest()
    original = culture.read_bytes()
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md), "--adopt-existing", pin]) == 0
    assert set_ai_instructions.get_instructions(culture) == instructions
    assert (
        culture.read_bytes().replace(f'\n\tannotation T2P_CustomInstructionsSha256 = "{pin}"'.encode(), b"", 1)
        == original
    )
    assert cmd_check(tmp_path, True, model, md) == 0
    assert not set_ai_instructions.stamp_one(model, md)


@pytest.mark.parametrize("pin_kind", ["wrong", "different"])
def test_adoption_requires_both_pin_and_equal_markdown(tmp_path: Path, pin_kind: str) -> None:
    model, md, _ = _stamp_fixture(tmp_path, "Human text")
    before = _snapshot(model)
    pin = hashlib.sha256(b"Human text").hexdigest() if pin_kind == "different" else "0" * 64
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md), "--adopt-existing", pin]) == 3
    assert _snapshot(model) == before


@pytest.mark.parametrize("marker_kind", ["missing", "malformed", "mismatch", "duplicate", "nested"])
def test_invalid_ownership_never_authorizes_overwrite(tmp_path: Path, marker_kind: str) -> None:
    pin = hashlib.sha256(b"Human text").hexdigest()
    marker = f'\tannotation T2P_CustomInstructionsSha256 = "{pin}"\n'
    markers = {
        "missing": "",
        "malformed": marker.replace(pin, "no"),
        "mismatch": marker.replace(pin, "0" * 64),
        "duplicate": marker * 2,
        "nested": "\t" + marker,
    }
    model, md, _ = _stamp_fixture(tmp_path, "Human text", markers[marker_kind])
    before = _snapshot(model)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing", ["", "\n"])
def test_owned_replacement_preserves_unowned_bytes(tmp_path: Path, eol: str, trailing: str) -> None:
    old = "Line one\r\né"
    pin = hashlib.sha256(old.encode()).hexdigest()
    model, md, culture = _stamp_fixture(tmp_path, old, f'\tannotation T2P_CustomInstructionsSha256 = "{pin}"\n')
    original = culture.read_bytes().decode().rstrip("\n").replace("\n", eol) + trailing.replace("\n", eol)
    culture.write_bytes(original.encode())
    model_path = model / "definition" / "model.tmdl"
    model_path.write_bytes(b"model Model\r\n\tculture: en-US\r\n\r\n/// Keep\r\n")
    old_model = model_path.read_bytes()
    pbism = model / "definition.pbism"
    old_pbism = pbism.read_bytes()
    new = md.read_text(encoding="utf-8").strip()
    assert set_ai_instructions.stamp_one(model, md)
    expected = original.replace(json.dumps(old), json.dumps(new, ensure_ascii=False), 1).replace(
        pin, hashlib.sha256(new.encode()).hexdigest(), 1
    )
    assert culture.read_bytes() == expected.encode()
    assert model_path.read_bytes() == old_model + b"ref cultureInfo en-US\r\n"
    assert pbism.read_bytes() == old_pbism.replace(b"false", b"true", 1)
    assert cmd_check(tmp_path, True, model, md) == 0


@needs_corpus
def test_desktop_generated_culture_preserves_entities_and_all_original_bytes(tmp_path: Path) -> None:
    """Exercise a real Desktop-formatted multiline payload, not just the inline fixture."""
    candidates = [
        model / "definition" / "cultures" / "en-US.tmdl"
        for model in EXAMPLE_MODELS
        if "AirlineAllianceActivity" in model.name
    ]
    assert len(candidates) == 1
    original = candidates[0].read_bytes()
    assert b'"Entities"' in original
    model, md, culture = _stamp_fixture(tmp_path)
    culture.write_bytes(original)
    assert set_ai_instructions.stamp_one(model, md)
    instructions = md.read_text(encoding="utf-8").strip()
    inserted = (',"CustomInstructions": ' + json.dumps(instructions, ensure_ascii=False)).encode()
    marker = (
        '\tannotation T2P_CustomInstructionsSha256 = "' + hashlib.sha256(instructions.encode()).hexdigest() + '"'
    ).encode()
    output = culture.read_bytes()
    eol = b"\r\n" if b"\r\n" in original else b"\n"
    assert output.replace(inserted, b"", 1).replace(eol + marker, b"", 1) == original


@pytest.mark.parametrize("target", ["culture", "model", "pbism", "md"])
def test_bom_refuses_without_writes(tmp_path: Path, target: str) -> None:
    model, md, culture = _stamp_fixture(tmp_path)
    path = {
        "culture": culture,
        "model": model / "definition" / "model.tmdl",
        "pbism": model / "definition.pbism",
        "md": md,
    }[target]
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    before = _snapshot(model)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before


@pytest.mark.parametrize(
    "payload",
    [
        '{"CustomInstructions":"","CustomInstructions":""}',
        '{"Entities":{"a":1,"a":2}}',
        '{"n":NaN}',
        '{"n":1,}',
        '{"n":/*comment*/1}',
        '{"CustomInstructions":null}',
    ],
)
def test_invalid_metadata_refuses_before_other_writes(tmp_path: Path, payload: str) -> None:
    model, md, culture = _stamp_fixture(tmp_path)
    culture.write_text(
        f"cultureInfo en-US\n\tlinguisticMetadata = {payload}\n\t\tcontentType: json\n", encoding="utf-8"
    )
    before = _snapshot(model)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "{",
        '{"settings":{},"settings":{}}',
        '{"settings":{"qnaEnabled":false,"qnaEnabled":true}}',
        '{"settings":[]}',
        '{"settings":null}',
    ],
)
def test_impossible_qna_refuses_the_whole_stamp(tmp_path: Path, payload: str | None) -> None:
    model, md, _ = _stamp_fixture(tmp_path)
    pbism = model / "definition.pbism"
    if payload is None:
        pbism.unlink()
    else:
        pbism.write_text(payload, encoding="utf-8")
    before = _snapshot(model)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before


@pytest.mark.parametrize("text", ["x" * 10_001, "Use `'Ghost Table'`.", "Use [X].", "Use [" + "Ghost" * 20 + "]."])
def test_cap_and_explicit_references_are_hard_failures(tmp_path: Path, text: str) -> None:
    model, md, _ = _stamp_fixture(tmp_path)
    md.write_text(text, encoding="utf-8")
    before = _snapshot(model)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("new_culture", [False, True])
def test_every_replace_boundary_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: int, new_culture: bool
) -> None:
    model, md, culture = _stamp_fixture(tmp_path)
    if new_culture:
        culture.unlink()
        culture.parent.rmdir()
    before = _snapshot(model)
    replace = set_ai_instructions.os.replace
    calls = 0

    def failing_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            raise OSError("injected replace boundary")
        replace(source, target)

    monkeypatch.setattr(set_ai_instructions.os, "replace", failing_replace)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert calls >= boundary, "the intended replace assertion must be reached"
    assert _snapshot(model) == before
    assert not list(model.rglob("*.stage"))
    assert culture.parent.exists() is (not new_culture)


def test_stage_failure_and_translations_only_do_not_partially_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, md, culture = _stamp_fixture(tmp_path)
    before = _snapshot(model)
    stage = set_ai_instructions._stage
    calls = 0

    def fail_second_stage(path: Path, content: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staging failure")
        return stage(path, content)

    monkeypatch.setattr(set_ai_instructions, "_stage", fail_second_stage)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert calls == 2
    assert _snapshot(model) == before
    culture.write_bytes(b"cultureInfo en-US\n\ttranslations\n")
    before = _snapshot(model)
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before


def test_plan_is_read_only_and_strict_explicit_md_is_a_seal(tmp_path: Path) -> None:
    model, md, culture = _stamp_fixture(tmp_path)
    before = _snapshot(model)
    assert set_ai_instructions.main(["--plan", "--model", str(model), "--md", str(md)]) == 0
    assert _snapshot(model) == before
    assert set_ai_instructions.stamp_one(model, md)
    assert cmd_check(tmp_path, True, model, md) == 0
    original = culture.read_bytes()
    culture.write_bytes(
        b"\n".join(line for line in original.split(b"\n") if b"T2P_CustomInstructionsSha256" not in line)
    )
    assert cmd_check(tmp_path, True, model, md) == 1
    assert cmd_check(tmp_path, False, model, md) == 0
    culture.write_bytes(original)
    md.write_text("Changed markdown", encoding="utf-8")
    assert cmd_check(tmp_path, True, model, md) == 1
    md.write_bytes(b"# Rules\nLatest = max date; avoid today.\n")
    pbism = model / "definition.pbism"
    pbism.write_bytes(pbism.read_bytes().replace(b"true", b"false"))
    assert cmd_check(tmp_path, True, model, md) == 1


@pytest.mark.parametrize("text", ["x" * 10_001, "Use [Ghost]."])
def test_strict_rechecks_cap_and_references_without_md(tmp_path: Path, text: str) -> None:
    model, _, _ = _stamp_fixture(tmp_path, text)
    pbism = model / "definition.pbism"
    pbism.write_bytes(b'{"settings":{"qnaEnabled":true}}')
    assert cmd_check(tmp_path, True, model) == 1
    assert cmd_check(tmp_path, False, model) == 0


def test_exact_cap_and_empty_settings_succeed(tmp_path: Path) -> None:
    model, md, culture = _stamp_fixture(tmp_path)
    md.write_text("x" * 10_000, encoding="utf-8")
    (model / "definition.pbism").write_bytes(b'{"version":"4.2.0"}')
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 0
    assert len(set_ai_instructions.get_instructions(culture)) == 10_000
    assert read_qna_enabled(model) is True
    assert cmd_check(tmp_path, True, model, md) == 0


@pytest.mark.parametrize("qna", [False, "true", 1, None, True])
def test_qna_postcondition_is_the_json_boolean_not_truthiness(tmp_path: Path, qna: object) -> None:
    """False, string and numeric lookalikes may never pass a scoped gate."""
    model, md, _ = _stamp_fixture(tmp_path)
    set_ai_instructions.stamp_one(model, md)
    pbism = model / "definition.pbism"
    pbism.write_text(json.dumps({"settings": {"qnaEnabled": qna}}), encoding="utf-8")
    assert cmd_check(tmp_path, True, model, md) == (0 if qna is True else 1)
    set_ai_instructions.stamp_one(model, md)
    assert read_qna_enabled(model) is True


def test_all_validates_every_model_before_any_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A later refusal cannot leave the first model stamped in --all mode."""
    first, _, _ = _stamp_fixture(tmp_path / "first")
    second, _, _ = _stamp_fixture(tmp_path / "second")
    pbism = second / "definition.pbism"
    original = pbism.read_bytes()
    pbism.unlink()
    before = (_snapshot(first), _snapshot(second))
    monkeypatch.setattr(set_ai_instructions, "iter_models", lambda _: [first, second])
    monkeypatch.setattr(set_ai_instructions, "REPO_ROOT", tmp_path)
    assert set_ai_instructions.main(["--all"]) == 3
    assert (_snapshot(first), _snapshot(second)) == before
    pbism.write_bytes(original)
    assert set_ai_instructions.main(["--all"]) == 0
    assert read_qna_enabled(first) is True
    assert read_qna_enabled(second) is True


def test_qualified_backticked_table_is_an_explicit_reference(tmp_path: Path) -> None:
    """A real column cannot hide a nonexistent table in a qualified markdown reference."""
    model, md, _ = _stamp_fixture(tmp_path)
    tables = model / "definition" / "tables"
    tables.mkdir()
    (tables / "Real.tmdl").write_text("table 'Real Table'\n\tcolumn Real\n", encoding="utf-8")
    md.write_text("Use `'Ghost Table'[Real]`.", encoding="utf-8")
    before = _snapshot(model)
    assert unresolved_references(md.read_text(encoding="utf-8"), model_object_names(model)) == ["Ghost Table"]
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 3
    assert _snapshot(model) == before
    md.write_text("Use `'Real Table'[Real]`.", encoding="utf-8")
    assert set_ai_instructions.main(["--model", str(model), "--md", str(md)]) == 0
    assert cmd_check(tmp_path, True, model, md) == 0
    culture = model / "definition" / "cultures" / "en-US.tmdl"
    old_pin = hashlib.sha256(b"Use `'Real Table'[Real]`.").hexdigest().encode()
    new_pin = hashlib.sha256(b"Use `'Ghost Table'[Real]`.").hexdigest().encode()
    culture.write_bytes(culture.read_bytes().replace(b"Real Table", b"Ghost Table").replace(old_pin, new_pin))
    assert cmd_check(tmp_path, True, model) == 1
