"""Guard current Power BI skill routes and their migration safety boundaries (#728)."""

from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_plugin  # noqa: E402  # pylint: disable=wrong-import-position

RETIRED = re.compile(
    r"(?<![\w-])(powerbi-report-(?:planning|design|authoring|management)|check-updates|"
    r"semantic-model-consumption|powerbi-authoring-cli|powerbi-consumption-cli|"
    r"powerbi-semantic-model-authoring)(?![\w-])"
)
# Exact frozen 2026-07-31 evidence, not a present-day instruction to load this name.
HISTORICAL = {
    (
        "docs/agent-architecture.md",
        '   | `skill("powerbi-report-authoring")` (plugin) | **SUCCESS**, base dir under '
        r"`.copilot\installed-plugins\fabric-collection\powerbi-authoring\` |",
    )
}
PERSONAS = ("pbi-report-builder", "pbi-migration-validator")
CORE_ROLES = (
    ("pbi-report-builder", "powerbi-report-gotchas", "PBIR"),
    ("pbi-semantic-builder", "powerbi-semantic-model-gotchas", "TMDL"),
)
CORE_ORDER = "Before loading an upstream procedure or authoring {artifact}, acquire migration core `{core}`:"
CORE_INVOKE = "invoke it when advertised;"
CORE_ABSENT_READ = "only if absent from advertised skills, read its exact current host file"
CORE_HOST = "Host is the migration checkout supplying this role, not a caller/package directory or guessed plugin copy."
CORE_FAILED_CALL = "Advertised-but-failing invocation is not absence and cannot activate this file route."
CORE_CONTENT_STOP = "If content cannot be acquired, BLOCK the affected operation; never skip the core."
CORE_READ_EVIDENCE = "A file read is content access, not skill invocation."
CORE_AUTHORITY = "This core-only rule changes no actor/operation authority or other-owner access rule."
CORE_ACCESS = " ".join(
    (
        CORE_ORDER,
        CORE_INVOKE,
        CORE_ABSENT_READ + r" `.github\skills\{core}\SKILL.md`.",
        CORE_HOST,
        CORE_FAILED_CALL,
        CORE_CONTENT_STOP,
        CORE_READ_EVIDENCE,
        CORE_AUTHORITY,
    )
)
HOST_PREFLIGHT = (
    r"Host startup/version preflight means `scripts\preflight.ps1` with the existing `AGENTS.md` timing and flags; "
    "never invoke a generic `preflight` skill as its substitute."
)
REPORT_CORE_EXCEPTION = "Missing skill/command/capability → BLOCKED outside this core-only access exception"
SEMANTIC_AUTHORING_SKILL = "powerbi-authoring:semantic-model-authoring"
SEMANTIC_MANDATORY_SELECTION = "**Load `powerbi-authoring:semantic-model-authoring`**"
SEMANTIC_RESOLUTION_STOP = (
    "Unavailable/ambiguous selection blocks dependent authoring: "
    "no unqualified name, other provider or local-file fallback."
)
SEMANTIC_AI_SELECTION = (
    "Invoke `powerbi-ai-readiness` §0 as sole local AI owner; "
    "its narrow override below outranks generic upstream AI guidance."
)
SEMANTIC_LOCAL_CORE_READ = CORE_ABSENT_READ + r" `.github\skills\powerbi-semantic-model-gotchas\SKILL.md`."


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _persona(name: str) -> str:
    return _text(f".github/agents/{name}.agent.md")


def test_retired_skill_census() -> None:
    """Check guidance paths and contents; exclude provenance examples/migrations and negative tests."""
    paths = [ROOT / name for name in ("AGENTS.md", "README.md", "CONTRIBUTING.md")]
    for pattern in (
        ".github/agents/*.agent.md",
        ".github/skills/**/SKILL.md",
        ".github/pbi.kb/**/*.md",
        "docs/**/*.md",
        "scripts/**/*.py",
        "scripts/**/*.ps1",
        "scripts/**/*.md",
    ):
        paths.extend(ROOT.glob(pattern))
    found = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        assert not RETIRED.search(relative), f"Retired skill path: {relative}"
        found.extend((relative, line) for line in path.read_text(encoding="utf-8").splitlines() if RETIRED.search(line))
    assert sorted(found) == sorted(HISTORICAL), f"Retired skill references: {found}"


def test_playbook_release_contract() -> None:
    """Stage the approved compact cores once; publication/installation is separately authorized."""
    assert build_plugin.VERSION == "0.3.2"


def test_current_modes_and_approval_stop() -> None:
    """Existing repair is direct; greenfield cannot silently become a build."""
    builder = " ".join(_persona(PERSONAS[0]).replace("**", "").split())
    validator = " ".join(_persona(PERSONAS[1]).replace("**", "").split())
    for text in (builder, validator, _text("README.md")):
        assert "powerbi-report-cli" in text
    assert "existing → `authoring`" in builder, "existing-report authoring"
    assert "missing page → `design` then `authoring`" in builder, "missing-page design then authoring"
    assert "references/<mode>.md" in builder
    assert "by intent, not flags" in builder
    assert re.search(r"greenfield.*`planning`", builder, re.I)
    assert "_brief/report-spec.md" in builder
    assert "approval_pending" in builder
    assert "end the turn. Require later explicit human approval of that locked spec" in builder, "human approval stop"
    assert "not autonomy or assumed migration-brief approval" in builder, "approval cannot be inferred"
    assert "Until then, no PBIR/semantic edit/scaffold, synthetic data or Fabric writes" in builder
    assert re.search(r"`authoring`.*read-only inventory/preview/evidence", validator, re.I)
    assert "No planning/design/management/publish/rebind/install/update/`@latest`" in validator
    assert "V writes private review outputs only" in validator


@pytest.mark.parametrize("name", PERSONAS)
def test_no_management_or_mid_migration_install(name: str) -> None:
    """A prohibitive clause, not mere presence of 'latest' and 'preflight', is required."""
    text = " ".join(_persona(name).replace("**", "").split())
    if name == "pbi-report-builder":
        assert "Never `management`, publish or rebind" in text, "report publication stop"
        assert "no install/update, including npm `@latest`" in text, "no mid-migration installation"
        assert "Missing skill/command/capability → BLOCKED" in text, "missing capability blocks"
        assert REPORT_CORE_EXCEPTION in text, "missing capability stop respects only the core access exception"
        assert "route its operation to preflight, no DIY/retired names" in text, "no DIY fallback"
        assert "Preflight is the sole version authority" in text
    else:
        assert "No planning/design/management/publish/rebind/install/update/`@latest`" in text
        assert "Unavailable owner/capability → BLOCKED affected operation to M/preflight" in text
        assert "never DIY/skipped-schema fallback sign-off" in text
    assert not re.search(r"\bnpm\s+(?:install|update|i|add)\b", text, re.I)


@pytest.mark.parametrize("role", CORE_ROLES, ids=["report", "semantic"])
def test_local_core_access_is_content_first_and_fail_closed(role: tuple[str, str, str]) -> None:
    """Guard content routes, not a fabricated registry or a claim of runtime invocation."""
    name, core, artifact = role
    text = " ".join(_persona(name).replace("**", "").split())
    assert CORE_ORDER.format(artifact=artifact, core=core) in text, "core content precedes upstream and artifact work"
    assert CORE_INVOKE in text, "advertised core must be invoked"
    assert f"{CORE_ABSENT_READ} `.github\\skills\\{core}\\SKILL.md`." in text, "absent core exact host read only"
    assert CORE_HOST in text, "core host is the current migration checkout"
    assert CORE_FAILED_CALL in text, "failed advertised invocation is not absence"
    assert CORE_CONTENT_STOP in text, "unavailable core content blocks affected work"
    assert CORE_READ_EVIDENCE in text, "file read is not skill invocation evidence"
    assert CORE_AUTHORITY in text, "core-only authority boundary"
    contract = CORE_ACCESS.format(core=core, artifact=artifact)
    assert text.count(contract) == 1, "single contiguous core access contract"
    # Core acquisition conditionals belong to this block, not appended overrides.
    assert not re.search(
        r"\b(?:if|when)\b[^.;!?]*\b(?:core|content|advertised|invocation)\b",
        text.replace(contract, "", 1),
        re.I,
    ), "core-only access routing stays in the approved contract"


@pytest.mark.parametrize("role", CORE_ROLES, ids=["report", "semantic"])
@pytest.mark.parametrize(
    "reversal",
    [
        (CORE_INVOKE, "read its file instead when advertised;", "advertised core must be invoked"),
        (CORE_INVOKE, "skip it when advertised;", "advertised core must be invoked"),
        (CORE_ABSENT_READ, "read this file regardless of advertisement", "absent core exact host read only"),
        (
            r"`.github\skills\{core}\SKILL.md`",
            r"`.github\skills\{core}\README.md`",
            "absent core exact host read only",
        ),
        (
            r"`.github\skills\{core}\SKILL.md`",
            r"`.github\skills\powerbi-ai-readiness\SKILL.md`",
            "absent core exact host read only",
        ),
        (
            CORE_HOST,
            "Host is any caller/package directory or plugin copy.",
            "core host is the current migration checkout",
        ),
        (
            CORE_FAILED_CALL,
            "An advertised invocation failure counts as absence and activates this file route.",
            "failed advertised invocation is not absence",
        ),
        (
            CORE_ORDER,
            "After loading an upstream procedure or authoring {artifact}, acquire migration core `{core}`:",
            "core content precedes upstream and artifact work",
        ),
        (
            CORE_CONTENT_STOP,
            "If content cannot be acquired, continue without the core.",
            "unavailable core content blocks",
        ),
        (CORE_READ_EVIDENCE, "A file read counts as successful skill invocation.", "file read is not skill invocation"),
        (CORE_AUTHORITY, "This file rule grants access to any skill or other owner.", "core-only authority boundary"),
        (
            CORE_AUTHORITY,
            CORE_AUTHORITY + r" If core content is unavailable, read `.github\skills\powerbi-ai-readiness\SKILL.md`"
            " and continue to the upstream procedure.",
            "core-only access routing stays in the approved contract",
        ),
        (
            CORE_AUTHORITY,
            CORE_AUTHORITY + r" If the core is advertised, read `.github\skills\{core}\SKILL.md` directly"
            " and continue to the upstream procedure.",
            "core-only access routing stays in the approved contract",
        ),
        (
            CORE_AUTHORITY,
            CORE_AUTHORITY + r" If an advertised invocation fails, read `.github\skills\{core}\SKILL.md`"
            " and continue to the upstream procedure.",
            "core-only access routing stays in the approved contract",
        ),
    ],
    ids=[
        "advertised-read-instead",
        "advertised-skip",
        "absent-route-removed",
        "absent-wrong-path",
        "absent-wrong-owner",
        "wrong-host",
        "advertised-failure-fallback",
        "wrong-order",
        "missing-content-continue",
        "read-as-invocation",
        "broaden-other-owner",
        "extra-unavailable-wrong-owner",
        "extra-advertised-read",
        "extra-failed-invocation-rescue",
    ],
)
def test_core_access_reversals_fail_the_named_assertion_then_restore(
    monkeypatch: pytest.MonkeyPatch, role: tuple[str, str, str], reversal: tuple[str, str, str]
) -> None:
    """Each direct reversal must fail its semantic assertion, never a pin or setup error."""
    name, core, artifact = role
    before, after, diagnostic = (part.format(core=core, artifact=artifact) for part in reversal)
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / f"{name}.agent.md"
    source = " ".join(original_read(target, encoding="utf-8").split())
    test_local_core_access_is_content_first_and_fail_closed(role)
    assert source.count(before) == 1, "reversal must change one production instruction"
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_text",
            lambda current, *args, **kwargs: (
                source.replace(before, after, 1) if current == target else original_read(current, *args, **kwargs)
            ),
        )
        with pytest.raises(AssertionError, match=diagnostic):
            test_local_core_access_is_content_first_and_fail_closed(role)
    test_local_core_access_is_content_first_and_fail_closed(role)


@pytest.mark.parametrize("role", CORE_ROLES, ids=["report", "semantic"])
def test_host_preflight_is_the_existing_script_not_a_skill(role: tuple[str, str, str]) -> None:
    """Identity only: existing startup flags/timing and authorized CLI status checks stay owned."""
    text = " ".join(_persona(role[0]).replace("**", "").split())
    assert HOST_PREFLIGHT in text, "host preflight script identity and existing timing/flags"
    assert not re.search(r"\b(?:load|invoke|use|select|call)\s+(?:\*\*)?`preflight`", text, re.I), (
        "no generic preflight skill substitution"
    )


@pytest.mark.parametrize("role", CORE_ROLES, ids=["report", "semantic"])
@pytest.mark.parametrize("additional", [False, True], ids=["replace-identity", "extra-skill-selection"])
def test_generic_preflight_selection_fails_its_named_assertion_then_restore(
    monkeypatch: pytest.MonkeyPatch, role: tuple[str, str, str], additional: bool
) -> None:
    """Replacing or supplementing the host script with a generic skill cannot pass."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / f"{role[0]}.agent.md"
    source = " ".join(original_read(target, encoding="utf-8").split())
    test_host_preflight_is_the_existing_script_not_a_skill(role)
    assert source.count(HOST_PREFLIGHT) == 1
    selection = "Invoke `preflight` for host startup/version checks."
    changed = source + " " + selection if additional else source.replace(HOST_PREFLIGHT, selection, 1)
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_text",
            lambda current, *args, **kwargs: changed if current == target else original_read(current, *args, **kwargs),
        )
        with pytest.raises(
            AssertionError,
            match="no generic preflight skill substitution" if additional else "host preflight script identity",
        ):
            test_host_preflight_is_the_existing_script_not_a_skill(role)
    test_host_preflight_is_the_existing_script_not_a_skill(role)


def test_report_core_exception_cannot_negate_access_or_waive_upstream_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exception permits only core content access; absent upstream capabilities still block."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / "pbi-report-builder.agent.md"
    source = " ".join(original_read(target, encoding="utf-8").split())
    test_no_management_or_mid_migration_install("pbi-report-builder")
    assert source.count(REPORT_CORE_EXCEPTION) == 1
    for replacement, diagnostic in (
        (
            "Missing skill/command/capability → BLOCKED including canonical core access",
            "missing capability stop respects only the core access exception",
        ),
        ("Missing upstream skill/command/capability permits DIY.", "missing capability blocks"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(
                Path,
                "read_text",
                lambda current, *args, replacement=replacement, **kwargs: (
                    source.replace(REPORT_CORE_EXCEPTION, replacement, 1)
                    if current == target
                    else original_read(current, *args, **kwargs)
                ),
            )
            with pytest.raises(AssertionError, match=diagnostic):
                test_no_management_or_mid_migration_install("pbi-report-builder")
        test_no_management_or_mid_migration_install("pbi-report-builder")


@pytest.mark.parametrize("role", CORE_ROLES, ids=["report", "semantic"])
def test_descriptive_topics_and_cli_preflight_vocabulary_remain_legal(
    monkeypatch: pytest.MonkeyPatch, role: tuple[str, str, str]
) -> None:
    """Descriptive topic/module/CLI vocabulary is not an instruction to select a provider."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / f"{role[0]}.agent.md"
    source = original_read(target, encoding="utf-8")
    description = (
        "\nTopic label only: `semantic-model-authoring`; this sentence selects no provider.\n"
        "Topic label only: `fabric-skills:semantic-model-authoring`; this sentence selects no provider.\n"
        "Topic labels only: advertised core, invocation failure and core content; no routing instruction.\n"
        "The `preflight` module and `preview … --status` CLI preflight are descriptive vocabulary.\n"
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_text",
            lambda current, *args, **kwargs: (
                source + description if current == target else original_read(current, *args, **kwargs)
            ),
        )
        test_local_core_access_is_content_first_and_fail_closed(role)
        test_host_preflight_is_the_existing_script_not_a_skill(role)
        if role[0] == "pbi-semantic-builder":
            test_semantic_authoring_provider_selection_is_exact_and_fail_closed()
    test_local_core_access_is_content_first_and_fail_closed(role)
    test_host_preflight_is_the_existing_script_not_a_skill(role)
    test_semantic_authoring_provider_selection_is_exact_and_fail_closed()


def test_current_references_and_query_surfaces() -> None:
    """Selected mode/topic pointers are finite; disk checks do not prove fresh-role resolution."""
    gotchas = _text(".github/skills/powerbi-report-gotchas/SKILL.md")
    assert "Portable upstream procedures: installed `powerbi-report-cli`" in gotchas, "report upstream owner"
    assert "existing report → `authoring`; missing page → `design` then `authoring`" in gotchas, "report intent mode"
    assert "Modes select `references/<mode>.md`, not CLI flags" in gotchas, "selected-mode entry"
    assert set(re.findall(r"`(references/[^`]+\.md)`", gotchas)) == {
        "references/<mode>.md",
        *(
            f"references/authoring/{topic}.md"
            for topic in (
                "expressions",
                "conditional-formatting",
                "field-parameters",
                "slicers-part-02",
                "map",
                "model-binding",
                "screenshot-review",
            )
        ),
    }, "report authoring topics"
    semantic = _text(".github/skills/powerbi-semantic-model-gotchas/SKILL.md")
    assert "installed `semantic-model-authoring` existing-model workflow" in semantic, "semantic upstream owner"
    assert set(re.findall(r"`(references/[^`]+\.md)`", semantic)) == {
        "references/tmdl-guidelines.md",
        "references/authoring/field-parameters.md",
    }, "semantic parameter topics"
    assert "Until fresh S resolves both parameter topics, keep this constructor guard" in semantic
    guide = _text("docs/tableau-dax-translation-guide.md")
    query = guide.split("LOD expressions are the highest-risk", 1)[1].split("before trusting it.", 1)[0]
    for token in (
        "EVALUATE",
        "scripts/probe_desktop_query.py",
        "powerbi-modeling-mcp",
        "dax_query_operations",
        "powerbi-remote",
        "ExecuteQuery",
        "Python replica",
    ):
        assert token in query
    assert "semantic-model-authoring" not in query
    assert "references/design/chart-selection.md" in guide
    for name in ("pbi-semantic-builder", *PERSONAS):
        text = " ".join(_persona(name).replace("**", "").split())
        assert r"scripts\probe_desktop_query.py" in text, f"{name}: real local query owner"
        assert re.search(r"ConnectFolder.*metadata-only", text), f"{name}: offline query ceiling"
        assert re.search(r"`powerbi-remote`.*published", text), f"{name}: published-only query ceiling"


def test_semantic_authoring_provider_selection_is_exact_and_fail_closed() -> None:
    """One canonical load can replace duplicate imperatives without losing the AI override."""
    text = " ".join(_persona("pbi-semantic-builder").split())
    assert text.count(SEMANTIC_MANDATORY_SELECTION) == 1, "mandatory semantic selection"
    selections = re.findall(
        r"\b(?:load|invoke|use|select|call)\s+(?:the\s+)?(?:\*\*)?"
        r"`((?:[\w-]+:)?(?:semantic-model-authoring|powerbi-semantic-model-authoring))`",
        text,
        re.I,
    )
    assert selections and all(name == SEMANTIC_AUTHORING_SKILL for name in selections), (
        "every semantic selection qualified"
    )
    assert SEMANTIC_AI_SELECTION in text, "AI policy override retained"
    assert text.count(SEMANTIC_LOCAL_CORE_READ) == 1, "repo-local fallback boundary and resolution stop"
    assert SEMANTIC_RESOLUTION_STOP in text, "repo-local fallback boundary and resolution stop"


@pytest.mark.parametrize(
    "additional_selection",
    [False, True],
    ids=["canonical-load", "additional-imperative"],
)
@pytest.mark.parametrize(
    "replacement",
    [
        "semantic-model-authoring",
        "fabric-skills:semantic-model-authoring",
        "skills-for-fabric:semantic-model-authoring",
        "powerbi-semantic-model-authoring",
    ],
    ids=["bare", "fabric-skills", "skills-for-fabric", "retired"],
)
def test_semantic_provider_reversals_fail_each_selection_then_restore(
    monkeypatch: pytest.MonkeyPatch, additional_selection: bool, replacement: str
) -> None:
    """A wrong second selection must fail even while the canonical load remains correct."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / "pbi-semantic-builder.agent.md"
    source = original_read(target, encoding="utf-8")
    test_semantic_authoring_provider_selection_is_exact_and_fail_closed()
    assert source.count(SEMANTIC_MANDATORY_SELECTION) == 1
    changed = (
        source + f"\nLoad `{replacement}` for authoring.\n"
        if additional_selection
        else source.replace(SEMANTIC_MANDATORY_SELECTION, f"**Load `{replacement}`**", 1)
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_text",
            lambda current, *args, **kwargs: changed if current == target else original_read(current, *args, **kwargs),
        )
        with pytest.raises(
            AssertionError,
            match="every semantic selection qualified" if additional_selection else "mandatory semantic selection",
        ):
            test_semantic_authoring_provider_selection_is_exact_and_fail_closed()
    test_semantic_authoring_provider_selection_is_exact_and_fail_closed()


@pytest.mark.parametrize(
    "selection",
    [
        "Invoke `semantic-model-authoring`",
        "Use `semantic-model-authoring`",
        "Select `semantic-model-authoring`",
        "Call `semantic-model-authoring`",
        "Load the `fabric-skills:semantic-model-authoring` skill",
        "Invoke the `semantic-model-authoring` skill",
    ],
)
def test_additional_semantic_imperatives_fail_provider_selection_then_restore(
    monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    """Known imperative forms remain selections even with the canonical load intact."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / "pbi-semantic-builder.agent.md"
    source = original_read(target, encoding="utf-8")
    test_semantic_authoring_provider_selection_is_exact_and_fail_closed()
    changed = source + f"\n{selection} for authoring.\n"
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_text",
            lambda current, *args, **kwargs: changed if current == target else original_read(current, *args, **kwargs),
        )
        with pytest.raises(AssertionError, match="every semantic selection qualified"):
            test_semantic_authoring_provider_selection_is_exact_and_fail_closed()
    test_semantic_authoring_provider_selection_is_exact_and_fail_closed()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            SEMANTIC_LOCAL_CORE_READ,
            r"If any skill fails, read `.github\skills\<name>\SKILL.md` as its fallback.",
        ),
        (SEMANTIC_RESOLUTION_STOP, "Unavailable/ambiguous selection permits another provider."),
        (SEMANTIC_RESOLUTION_STOP, "Unavailable/ambiguous selection permits an upstream local-file fallback."),
    ],
    ids=["unrestricted-fallback", "retry-after-ambiguity", "upstream-file-fallback"],
)
def test_semantic_fallback_reversals_fail_the_stop_then_restore(
    monkeypatch: pytest.MonkeyPatch, before: str, after: str
) -> None:
    """The stop itself must fail, not a selection census, pin mismatch or unrelated error."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / "pbi-semantic-builder.agent.md"
    source = " ".join(original_read(target, encoding="utf-8").split())
    test_semantic_authoring_provider_selection_is_exact_and_fail_closed()
    assert source.count(before) == 1
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_text",
            lambda current, *args, **kwargs: (
                source.replace(before, after, 1) if current == target else original_read(current, *args, **kwargs)
            ),
        )
        with pytest.raises(AssertionError, match="repo-local fallback boundary and resolution stop"):
            test_semantic_authoring_provider_selection_is_exact_and_fail_closed()
    test_semantic_authoring_provider_selection_is_exact_and_fail_closed()


def test_compact_core_citations_keep_their_meaning() -> None:
    """Surviving numbers are not evidence that a citation still points to the same rule."""
    report = " ".join(_persona("pbi-report-builder").split())
    semantic = " ".join(_persona("pbi-semantic-builder").split())
    mapping = report.split("Prefer formatting/layout", 1)[1].split("Unknown encoding", 1)[0]
    assert "source intent/core §8" in mapping, "mapping source-intent citation"
    assert "current typed vocabulary" in mapping, "mapping procedure owner"
    assert "§9" not in mapping, "removed report section"
    scale = report.split("Sample every percentage/scaled", 1)[1].split("6.", 1)[0]
    assert "via real-model DAX" in scale and "core §8" in scale, "scale source/query citation"
    assert r"scripts\probe_desktop_query.py" in report, "scale source/query citation"
    assert r"scripts\check_datamodel.py <SemanticModel>" in semantic, "uniqueness executable-owner citation"
    assert "local rename/reference and uniqueness checks; require final exits 0" in semantic
    assert "real-row canaries for **every commissioned import/live table**" in semantic, "row-proof owner citation"
    assert r".github\skills\pbip-model-refresh\SKILL.md" in semantic, "held-out refresh owner"
    assert "Held out of the playbook: missing host path blocks dependent handoff" in semantic


@pytest.mark.parametrize(
    ("path", "before", "after", "diagnostic"),
    [
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "installed `powerbi-report-cli`",
            "installed `missing-skill`",
            "report upstream owner",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "installed `powerbi-report-cli`",
            "installed `powerbi-report-authoring`",
            "report upstream owner",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "existing report → `authoring`",
            "existing report → `planning`",
            "report intent mode",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "references/authoring/expressions.md",
            "references/expressions.md",
            "report authoring topics",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "references/authoring/slicers-part-02.md",
            "references/authoring/slicers-part-20.md",
            "report authoring topics",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "`references/authoring/map.md`, ",
            "",
            "report authoring topics",
        ),
        (
            ".github/skills/powerbi-semantic-model-gotchas/SKILL.md",
            "installed `semantic-model-authoring`",
            "installed `missing-skill`",
            "semantic upstream owner",
        ),
        (
            ".github/skills/powerbi-semantic-model-gotchas/SKILL.md",
            "references/authoring/field-parameters.md",
            "references/design/field-parameters.md",
            "semantic parameter topics",
        ),
    ],
)
def test_missing_retired_or_wrong_mode_pointers_fail_their_owner_assertion(
    monkeypatch: pytest.MonkeyPatch, path: str, before: str, after: str, diagnostic: str
) -> None:
    """Direct pointer negatives, not a fabricated installed-skill registry or runtime canary."""
    original_read = Path.read_text
    target = ROOT / path
    source = original_read(target, encoding="utf-8")
    assert source.count(before) == 1
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda current, *args, **kwargs: (
            source.replace(before, after) if current == target else original_read(current, *args, **kwargs)
        ),
    )
    with pytest.raises(AssertionError, match=diagnostic):
        test_current_references_and_query_surfaces()


@pytest.mark.parametrize(
    ("name", "before", "after", "diagnostic"),
    [
        (
            "pbi-report-builder",
            "check `formatString` (core §8)",
            "check `formatString` (core §3)",
            "scale source/query citation",
        ),
        (
            "pbi-semantic-builder",
            r"`python scripts\check_datamodel.py <SemanticModel>`",
            r"`python scripts\missing_datamodel.py <SemanticModel>`",
            "uniqueness executable-owner citation",
        ),
        (
            "pbi-report-builder",
            "source intent/core §8",
            "source intent/core §9",
            "mapping source-intent citation",
        ),
    ],
)
def test_same_number_or_removed_section_reversals(
    monkeypatch: pytest.MonkeyPatch, name: str, before: str, after: str, diagnostic: str
) -> None:
    """Reintroducing the old meaning must fail even if the old section number still exists."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / f"{name}.agent.md"
    source = original_read(target, encoding="utf-8")
    assert source.count(before) == 1
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda current, *args, **kwargs: (
            source.replace(before, after) if current == target else original_read(current, *args, **kwargs)
        ),
    )
    with pytest.raises(AssertionError, match=diagnostic):
        test_compact_core_citations_keep_their_meaning()


def test_historical_probe_and_pending_current_probe() -> None:
    """Do not turn old evidence into a claimed successful new-name runtime probe."""
    doc = _text("docs/agent-architecture.md")
    assert "then-current retired name" in doc
    assert 'skill("powerbi-report-cli")' in doc
    assert "post-maintenance" in doc and "PENDING" in doc
    assert "date, CLI version, plugin version" in doc
    assert "not a successful runtime probe" in doc


@pytest.mark.parametrize(
    ("name", "baseline"),
    [("pbi-report-builder", 21902), ("pbi-migration-validator", 21799), ("tableau-migrator", 21951)],
)
def test_personas_are_net_negative(name: str, baseline: int) -> None:
    """Compare conservative CRLF size with reviewed #745 head 1823251d, also on Linux."""
    text = _persona(name)
    size = len(text.replace("\n", "\r\n"))
    assert size < baseline, f"{name}: {size} >= reviewed baseline {baseline}"
