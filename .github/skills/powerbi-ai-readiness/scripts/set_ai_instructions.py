"""
purpose: stamp owned CustomInstructions without rewriting unrelated model bytes.
usage:   python .github/skills/powerbi-ai-readiness/scripts/set_ai_instructions.py
             --model <*.SemanticModel> --md <instructions.md>
         Add --plan to validate without writing; --check --strict is the scoped handoff gate.

This standalone skill deliberately edits culture TMDL; see SKILL.md section 0 for policy and limits.
Sibling staging and per-file atomic replacement support caught-failure rollback, not a filesystem
transaction. Close Desktop before stamping, then reopen/refresh/save and repeat the explicit-md gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("set_ai_instructions")
MIGRATION_TREES = ("examples", "migrations/workbooks", "migrations/datasources")
HARD_CHAR_LIMIT = 10_000
CONTEXT_ROT_SOFT_LIMIT = 4_000
MARKER = "T2P_CustomInstructionsSha256"
_META_RE = re.compile(r"(?m)^(?P<indent>[ \t]+)linguisticMetadata[ \t]*=[ \t\r\n]*")


def host_root(start: Path | None = None) -> Path:
    """Find the host migration repository, including when this skill is copied out."""
    for parent in (start or Path(__file__).resolve()).parents:
        if any((parent / tree).is_dir() for tree in MIGRATION_TREES):
            return parent
    return Path.cwd()


REPO_ROOT = host_root()


def read_utf8(path: Path) -> str:
    """Decode strictly without newline translation; BOMs are refused, never normalized."""
    text = path.read_bytes().decode("utf-8")
    if text.startswith("\ufeff"):
        raise ValueError(f"BOM is not supported: {path.name}")
    return text


def text_sha256(text: str) -> str:
    """Hash the exact decoded instruction text, independently of JSON escaping."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_markdown(path: Path) -> str:
    """Retain the original stamper's universal-newline markdown semantics, not model rewriting."""
    return read_utf8(path).replace("\r\n", "\n").replace("\r", "\n").strip()


def _unique_object(pairs: list[tuple]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"Duplicate JSON key: {key}")
        obj[key] = value
    return obj


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


DECODER = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _object(text: str) -> tuple[dict, dict[str, tuple[int, int]]]:
    """Validate JSON, then locate top-level value spans without dumping other members."""
    obj = DECODER.decode(text)
    if not isinstance(obj, dict):
        raise ValueError("Expected a JSON object")
    spans = {}
    pos = text.index("{") + 1
    while True:
        pos = _skip_space(text, pos)
        if text[pos] == "}":
            return obj, spans
        key, pos = DECODER.raw_decode(text, pos)
        pos = _skip_space(text, pos)
        pos = _skip_space(text, pos + 1)  # colon already validated by decode
        start = pos
        _, pos = DECODER.raw_decode(text, pos)
        spans[key] = (start, pos)
        pos = _skip_space(text, pos)
        if text[pos] == ",":
            pos += 1


def _member(text: str, key: str, value: str) -> str:
    obj, spans = _object(text)
    if key in spans:
        start, end = spans[key]
        return text[:start] + value + text[end:]
    end = text.rindex("}")
    return text[:end] + ("," if obj else "") + json.dumps(key) + ": " + value + text[end:]


def _metadata(text: str) -> tuple[int, int, dict]:
    matches = list(_META_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError("Expected exactly one linguisticMetadata block")
    start = matches[0].end()
    obj, end = DECODER.raw_decode(text, start)
    if not isinstance(obj, dict) or not re.match(r"[ \t\r\n]*\r?\n[ \t]+contentType[ \t]*:[ \t]*json\b", text[end:]):
        raise ValueError("Expected linguisticMetadata object and contentType: json")
    return start, end, obj


def _ownership(text: str) -> tuple[str | None, re.Match | None]:
    candidates = list(re.finditer(rf"(?m)^[ \t]*annotation[ \t]+['\"]?{MARKER}\b[^\r\n]*", text))
    if not candidates:
        return None, None
    if len(candidates) != 1:
        raise ValueError("Duplicate ownership marker")
    match = candidates[0]
    valid = re.fullmatch(rf'[ \t]+annotation {MARKER} = (?:"([0-9a-f]{{64}})"|([0-9a-f]{{64}}))[ \t]*', match.group())
    if not valid:
        raise ValueError("Malformed ownership marker")
    indent = _META_RE.search(text).group("indent")
    if not match.group().startswith(indent + "annotation "):
        raise ValueError("Ownership marker is not a Culture annotation")
    return valid.group(1) or valid.group(2), match


class ModelObjectNames(set[str]):
    """Keep set compatibility without discarding table membership needed by qualified references."""

    def __init__(self) -> None:
        super().__init__()
        self.table_fields: dict[str, set[str]] = {}


def model_object_names(model_dir: Path) -> ModelObjectNames:
    """Declared names and their owning tables, used to validate explicit instruction references."""
    names = ModelObjectNames()
    definition = model_dir / "definition"
    for tmdl in list(definition.glob("tables/*.tmdl")) + list(definition.glob("*.tmdl")):
        current_table = None
        for match in re.finditer(
            r"^(?P<indent>[ \t]*)(?P<kind>table|column|measure)[ \t]+"
            r"(?P<token>'(?P<quoted>(?:[^']|'')+)'|[^\s=]+)",
            read_utf8(tmdl),
            re.MULTILINE,
        ):
            name = (match.group("quoted") or match.group("token")).replace("''", "'")
            if match.group("kind") == "table":
                current_table = name if not match.group("indent") else None
                if current_table is not None:
                    names.table_fields.setdefault(current_table, set())
            elif current_table is not None:
                names.table_fields[current_table].add(name)
            names.add(name)
    return names


_BRACKET_RE = re.compile(r"\[((?:[^\]`]|\]\])+)\]")
_QUALIFIED_RE = re.compile(
    r"(?P<code>`?)(?P<table>'(?:[^']|'')+'|(?<![\w'])[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<end_code>`?)(?P<gap>[ \t\r\n]*)(?P<field_code>`?)"
    r"\[(?P<field>(?:[^\]`]|\]\])+)\]"
)


def _is_template(name: str) -> bool:
    return any(token in name for token in ("<", "...", "*"))


def unresolved_references(md_text: str, known: set[str]) -> list[str]:
    """Validate names and qualified table-field pairs; a flattened set cannot prove a pair."""
    cited = {name.replace("''", "'") for name in re.findall(r"`'((?:[^']|'')+)'(?=`|\s*\[)", md_text)}
    cited.update(match.group(1).replace("]]", "]") for match in _BRACKET_RE.finditer(md_text))
    missing = {name for name in cited if not _is_template(name) and name not in known}
    table_fields = getattr(known, "table_fields", {})
    for match in _QUALIFIED_RE.finditer(md_text):
        token = match.group("table")
        table = token[1:-1].replace("''", "'") if token.startswith("'") else token
        field = match.group("field").replace("]]", "]")
        explicit = token.startswith("'") or not match.group("gap") or match.group("code") or match.group("end_code")
        if not explicit and table not in table_fields:
            continue  # Prose such as "use [Measure]" is not an unquoted table claim.
        if _is_template(table) or _is_template(field):
            continue
        if table not in table_fields:
            missing.add(table)
        elif field not in table_fields[table]:
            missing.add(match.group(0))
    return sorted(missing)


def lint_instructions(md_text: str, known_names: set[str] | None = None) -> list[str]:
    """Advisory style lints; hard validity is enforced separately."""
    warnings = []
    if len(md_text) > HARD_CHAR_LIMIT:
        warnings.append(f"exceeds Power BI's {HARD_CHAR_LIMIT}-char cap")
    elif len(md_text) > CONTEXT_ROT_SOFT_LIMIT:
        warnings.append(f"long ({len(md_text)} chars > {CONTEXT_ROT_SOFT_LIMIT}); risk of context rot")
    if "#" not in md_text:
        warnings.append("no markdown headings")
    if "`" not in md_text and "[" not in md_text:
        warnings.append("no field references")
    if "avoid" not in md_text.lower():
        warnings.append("no 'things to avoid' guidance")
    if known_names is not None and unresolved_references(md_text, known_names):
        warnings.append("references objects this model does NOT declare")
    return warnings


def _validate_instructions(text: str, model: Path) -> None:
    if not text or len(text) > HARD_CHAR_LIMIT:
        raise ValueError("Instructions must contain 1–10,000 decoded characters")
    missing = unresolved_references(text, model_object_names(model))
    if missing:
        raise ValueError("Unresolved explicit object references: " + ", ".join(missing))


def read_model_culture(model_dir: Path) -> str:
    """Read the default culture; permit only a file-safe unquoted culture identifier."""
    text = read_utf8(model_dir / "definition" / "model.tmdl")
    match = re.search(r"(?m)^[ \t]*culture:[ \t]*(\S+)", text)
    culture = match.group(1) if match else "en-US"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", culture):
        raise ValueError("Unsupported culture identifier")
    return culture


def find_culture_file(model_dir: Path) -> Path:
    """Only the model's default culture may be stamped or accepted by the gate."""
    path = model_dir / "definition" / "cultures" / f"{read_model_culture(model_dir)}.tmdl"
    if not path.is_file():
        raise FileNotFoundError("No default culture file")
    return path


def default_md_for(model_dir: Path) -> Path:
    """Default only covers committed migration shapes; bundle callers must supply --md."""
    return model_dir.parent.parent / "ai-instructions.md"


def get_instructions(culture_path: Path) -> str | None:
    """Read the exact decoded value without newline normalization."""
    return _metadata(read_utf8(culture_path))[2].get("CustomInstructions")


def read_qna_enabled(model_dir: Path) -> bool | None:
    """Read qna strictly; duplicate/malformed JSON is a refusal."""
    obj, _ = _object(read_utf8(model_dir / "definition.pbism"))
    settings = obj.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    return settings.get("qnaEnabled")


def _culture_edit(text: str, instructions: str, adopt_existing: str | None) -> str:
    start, end, obj = _metadata(text)
    existing = obj.get("CustomInstructions")
    if "CustomInstructions" in obj and not isinstance(existing, str):
        raise ValueError("CustomInstructions must be a string")
    marker, match = _ownership(text)
    if marker is not None and "CustomInstructions" not in obj:
        raise ValueError("Ownership marker has no CustomInstructions member")
    if marker is not None and marker != text_sha256(existing or ""):
        raise ValueError("Ownership marker does not match existing text")
    if adopt_existing is not None and (existing != instructions or adopt_existing != text_sha256(existing or "")):
        raise ValueError("Adoption requires the pinned existing text to equal the markdown")
    if existing and marker is None and adopt_existing is None:
        raise ValueError("Unowned CustomInstructions; explicit hash-pinned adoption required")
    # Adoption is marker-only, including preservation of the original JSON escaping.
    new_json = (
        text[start:end]
        if existing == instructions
        else _member(text[start:end], "CustomInstructions", json.dumps(instructions, ensure_ascii=False))
    )
    owned_marker = f'{_META_RE.search(text).group("indent")}annotation {MARKER} = "{text_sha256(instructions)}"'
    if match:
        text = text[: match.start()] + owned_marker + text[match.end() :]
    else:
        # Put the annotation before translations/children, after all Culture properties.
        tail = text[end:]
        prop = re.match(r"[ \t\r\n]*\r?\n[ \t]+contentType[ \t]*:[ \t]*json[ \t]*", tail)
        insert = end + prop.end()
        eol = "\r\n" if "\r\n" in text else "\n"
        text = text[:insert] + eol + owned_marker + text[insert:]
    # The marker is after the JSON, so the original JSON offsets still apply.
    return text[:start] + new_json + text[end:]


def plan_stamp(model_dir: Path, md_path: Path, adopt_existing: str | None = None) -> dict[Path, bytes]:
    """Compute and validate every output before touching the filesystem."""
    instructions = read_markdown(md_path)
    _validate_instructions(instructions, model_dir)
    culture = read_model_culture(model_dir)
    model_path = model_dir / "definition" / "model.tmdl"
    model_text = read_utf8(model_path)
    eol = "\r\n" if "\r\n" in model_text else "\n"
    culture_path = model_dir / "definition" / "cultures" / f"{culture}.tmdl"
    culture_text = (
        read_utf8(culture_path)
        if culture_path.exists()
        else (
            f"cultureInfo {culture}{eol}{eol}"
            f'\tlinguisticMetadata = {{"Version": "2.0.0", "Language": "{culture}"}}{eol}'
            f"\t\tcontentType: json{eol}"
        )
    )
    if not re.search(rf"(?m)^cultureInfo {re.escape(culture)}[ \t]*\r?$", culture_text):
        raise ValueError("Culture declaration does not match model culture")
    culture_text = _culture_edit(culture_text, instructions, adopt_existing)
    if not re.search(rf"(?m)^[ \t]*ref cultureInfo {re.escape(culture)}[ \t]*\r?$", model_text):
        model_text += ("" if model_text.endswith("\n") else eol) + f"ref cultureInfo {culture}{eol}"
    outputs = {
        culture_path: culture_text,
        model_path: model_text,
        model_dir / "definition.pbism": _qna_edit(read_utf8(model_dir / "definition.pbism")),
    }
    if _metadata(culture_text)[2]["CustomInstructions"] != instructions:
        raise ValueError("Instruction postcondition failed")
    return {
        path: text.encode("utf-8")
        for path, text in outputs.items()
        if not path.exists() or path.read_bytes() != text.encode("utf-8")
    }


def _qna_edit(pbism_text: str) -> str:
    obj, spans = _object(pbism_text)
    settings = obj.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    if settings.get("qnaEnabled") is not True:
        settings_text = pbism_text[slice(*spans["settings"])] if "settings" in spans else "{}"
        pbism_text = _member(pbism_text, "settings", _member(settings_text, "qnaEnabled", "true"))
    if _object(pbism_text)[0]["settings"]["qnaEnabled"] is not True:
        raise ValueError("qnaEnabled postcondition failed")
    return pbism_text


def _stage(path: Path, content: bytes) -> Path:
    sibling = path.with_name(f".{path.name}.{uuid.uuid4().hex}.stage")
    try:
        with sibling.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        sibling.unlink(missing_ok=True)
        raise
    return sibling


def _restore(replaced: list[Path], originals: dict[Path, bytes | None]) -> None:
    failures = []
    for path in reversed(replaced):
        try:
            if originals[path] is None:
                path.unlink()
            else:
                sibling = _stage(path, originals[path])
                try:
                    os.replace(sibling, path)
                finally:
                    sibling.unlink(missing_ok=True)
        except OSError:
            failures.append(path.name)
    if failures:
        raise OSError("Rollback incomplete; restore originals for: " + ", ".join(failures))


def apply_plan(outputs: dict[Path, bytes]) -> None:
    """Stage siblings, replace per file, roll back all prior replacements on caught failure."""
    originals = {path: path.read_bytes() if path.exists() else None for path in outputs}
    staged = {}
    replaced = []
    created_dirs = []
    try:
        for path, content in outputs.items():
            if not path.parent.exists():
                path.parent.mkdir()
                created_dirs.append(path.parent)
            staged[path] = _stage(path, content)
        for path, sibling in staged.items():
            os.replace(sibling, path)
            replaced.append(path)
    except OSError:
        _restore(replaced, originals)
        raise
    finally:
        for sibling in staged.values():
            sibling.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            if not any(directory.iterdir()):
                directory.rmdir()


def stamp_one(model_dir: Path, md_path: Path, adopt_existing: str | None = None) -> bool:
    """Stamp one model after full validation."""
    outputs = plan_stamp(model_dir, md_path, adopt_existing)
    apply_plan(outputs)
    for warning in lint_instructions(read_markdown(md_path)):
        log.warning("  [!] %s", warning)
    return bool(outputs)


def iter_models(root: Path) -> list[Path]:
    """Discover committed migration shapes."""
    return sorted(p for tree in MIGRATION_TREES for p in root.glob(f"{tree}/*/fabric/*.SemanticModel") if p.is_dir())


def cmd_check(root: Path, strict: bool = False, model_dir: Path | None = None, md_path: Path | None = None) -> int:
    """Scoped strict gates hard findings; repo-wide advisory remains exit zero."""
    models = [model_dir] if model_dir else iter_models(root)
    findings = 0
    for model in models:
        try:
            culture = find_culture_file(model)
            text = read_utf8(culture)
            instr = get_instructions(culture)
            if not isinstance(instr, str):
                raise ValueError("No CustomInstructions")
            _validate_instructions(instr, model)
            if read_qna_enabled(model) is not True:
                raise ValueError("qnaEnabled is not true")
            marker, _ = _ownership(text)
            if marker is not None and marker != text_sha256(instr):
                raise ValueError("Ownership marker mismatch")
            if md_path is not None:
                if marker != text_sha256(instr):
                    raise ValueError("Missing valid ownership marker")
                if instr != read_markdown(md_path):
                    raise ValueError("Stamped text differs from explicit markdown")
            log.info("  OK  %s %d chars", model.name, len(instr))
            for warning in lint_instructions(instr):
                log.warning("  [!] %s", warning)
        except (OSError, ValueError) as exc:
            findings += 1
            log.warning("  --  %s (%s)", model.name, exc)
    log.info("%d/%d models pass AI instructions checks", len(models) - findings, len(models))
    return 1 if strict and findings else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: usage 2, refused stamp 3, strict findings 1, success/advisory 0."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--md", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--plan", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--adopt-existing", help="explicit SHA-256 pin; only adopts text equal to markdown")
    args = parser.parse_args(argv)
    if args.all and args.model:
        parser.error("--all cannot be combined with --model")
    if args.strict and not args.check:
        parser.error("--strict requires --check")
    if args.md and not args.model:
        parser.error("--md requires --model")
    if args.adopt_existing and (not args.model or args.all or args.check):
        parser.error("--adopt-existing requires a single stamp/plan target")
    if args.adopt_existing and not re.fullmatch(r"[0-9a-f]{64}", args.adopt_existing):
        parser.error("--adopt-existing must be a lowercase SHA-256")
    if args.check:
        return cmd_check(REPO_ROOT, args.strict, args.model, args.md)
    if not args.model and not args.all:
        parser.error("provide --model, --all or --check")
    models = iter_models(REPO_ROOT) if args.all else [args.model.resolve()]
    try:
        outputs = {}
        for model in models:
            md_path = args.md or default_md_for(model)
            if args.all and not md_path.is_file():
                continue
            outputs.update(plan_stamp(model, md_path, args.adopt_existing))
        for path in outputs:
            relative = path.relative_to(args.model.resolve()) if args.model else path.relative_to(REPO_ROOT)
            log.info("%s %s", "changed" if path.exists() else "added", relative)
        if not args.plan:
            apply_plan(outputs)
        log.info("STAMP" if outputs else "NO_CHANGE")
    except (OSError, ValueError) as exc:
        log.error("REFUSE: %s", exc)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
