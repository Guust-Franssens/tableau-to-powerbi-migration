"""
purpose: sync a semantic model's AI instructions (an editable markdown file) into the model's
         culture linguisticMetadata.CustomInstructions key. That key is what Power BI Copilot and
         Fabric data agents read as model-level AI instructions ("Prep data for AI" > AI instructions).
         Writing the TMDL directly avoids an XMLA refresh (and the LCID-4096 refresh bug on this box).
usage:   python scripts/set_ai_instructions.py --model <path to *.SemanticModel> [--md <file.md>]
         python scripts/set_ai_instructions.py --all            # stamp every migration that has an ai-instructions.md
         python scripts/set_ai_instructions.py --check          # report which models have instructions

By convention, a model's editable source lives at <migration>/ai-instructions.md (two levels above the
*.SemanticModel folder). The culture file (definition/cultures/<lcid>.tmdl) is the generated artifact.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("set_ai_instructions")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Advisory authoring thresholds (see docs/ai-instructions-authoring-guide.md). Not enforced; warned.
HARD_CHAR_LIMIT = 10_000  # Power BI's own cap
CONTEXT_ROT_SOFT_LIMIT = 4_000  # above this, "informative yet tight" is at risk


def lint_instructions(md_text: str) -> list[str]:
    """Return advisory warnings about instruction quality (context-rot, structure, metadata restating)."""
    warnings: list[str] = []
    n = len(md_text)
    if n > HARD_CHAR_LIMIT:
        warnings.append(f"exceeds Power BI's {HARD_CHAR_LIMIT}-char cap ({n}); it will be rejected/truncated")
    elif n > CONTEXT_ROT_SOFT_LIMIT:
        warnings.append(f"long ({n} chars > {CONTEXT_ROT_SOFT_LIMIT}); risk of context rot — trim to high-signal lines")
    if "#" not in md_text:
        warnings.append("no markdown headings; use short sections, not a wall of prose")
    if "`" not in md_text and "[" not in md_text:
        warnings.append("no field references (no `backticks`/[brackets]); ground each line in real objects")
    if not re.search(r"(?im)^\s*#+\s*.*(avoid|do not|don't)", md_text) and "avoid" not in md_text.lower():
        warnings.append("no 'things to avoid' guidance; call out misuse (e.g. parameter tables, re-aggregation)")
    return warnings


# Captures the `linguisticMetadata =` keyword, the JSON object (greedy to the closing brace that
# precedes contentType, which never appears inside the JSON), and the trailing `contentType: json`
# sub-property. Whitespace/newlines between `=`/`{` and `}`/`contentType` are normalized on write.
_META_RE = re.compile(
    r"(?s)(?P<pre>linguisticMetadata[ \t]*=)[ \t]*\n?[ \t]*"
    r"(?P<json>\{.*\})(?P<post>\s*\n[ \t]*contentType[ \t]*:[ \t]*json)"
)


def read_model_culture(model_dir: Path) -> str:
    """Return the model's default culture (e.g. en-US) from model.tmdl, defaulting to en-US."""
    model_tmdl = model_dir / "definition" / "model.tmdl"
    if model_tmdl.is_file():
        match = re.search(r"^\s*culture:\s*(\S+)", model_tmdl.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return match.group(1)
    return "en-US"


def ensure_qna_enabled(model_dir: Path) -> bool:
    """Ensure definition.pbism has settings.qnaEnabled = true (required for the model's Q&A / Copilot
    natural-language experience to actually consume the linguistic metadata + AI instructions). Returns
    True if the file was changed."""
    pbism = model_dir / "definition.pbism"
    if not pbism.is_file():
        return False
    obj = json.loads(pbism.read_text(encoding="utf-8"))
    settings = obj.setdefault("settings", {})
    if settings.get("qnaEnabled") is True:
        return False
    settings["qnaEnabled"] = True
    pbism.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    log.info("  qna      set settings.qnaEnabled = true in definition.pbism")
    return True


def read_qna_enabled(model_dir: Path) -> bool | None:
    """Return the current settings.qnaEnabled for a model, or None if the file/key is absent."""
    pbism = model_dir / "definition.pbism"
    if not pbism.is_file():
        return None
    return json.loads(pbism.read_text(encoding="utf-8")).get("settings", {}).get("qnaEnabled")


def ensure_culture(model_dir: Path) -> Path:
    """Create a minimal cultureInfo (+ ref in model.tmdl) if the model has none. Returns the culture file."""
    culture = read_model_culture(model_dir)
    cultures_dir = model_dir / "definition" / "cultures"
    culture_path = cultures_dir / f"{culture}.tmdl"
    if not culture_path.is_file():
        cultures_dir.mkdir(parents=True, exist_ok=True)
        scaffold = (
            f"cultureInfo {culture}\n\n"
            f'\tlinguisticMetadata = {{"Version": "4.2.0", "Language": "{culture}"}}\n'
            f"\t\tcontentType: json\n"
        )
        culture_path.write_text(scaffold, encoding="utf-8")
        log.info("  created  %s", culture_path.relative_to(model_dir))

    model_tmdl = model_dir / "definition" / "model.tmdl"
    if model_tmdl.is_file():
        text = model_tmdl.read_text(encoding="utf-8")
        if not re.search(rf"^\s*ref\s+cultureInfo\s+{re.escape(culture)}\s*$", text, re.MULTILINE):
            text = text.rstrip("\n") + f"\n\nref cultureInfo {culture}\n"
            model_tmdl.write_text(text, encoding="utf-8")
            log.info("  ref      added `ref cultureInfo %s` to model.tmdl", culture)
    return culture_path


def find_culture_file(model_dir: Path) -> Path:
    """Return the culture TMDL file for a *.SemanticModel folder (prefers the model's culture, then en-US)."""
    cultures = model_dir / "definition" / "cultures"
    if not cultures.is_dir():
        raise FileNotFoundError(f"No cultures folder under {model_dir}")
    files = sorted(cultures.glob("*.tmdl"))
    if not files:
        raise FileNotFoundError(f"No culture .tmdl in {cultures}")
    preferred = read_model_culture(model_dir).lower()
    for f in files:
        if f.stem.lower() == preferred:
            return f
    for f in files:
        if f.stem.lower() == "en-us":
            return f
    return files[0]


def default_md_for(model_dir: Path) -> Path:
    """The editable markdown source lives at <migration>/ai-instructions.md."""
    return model_dir.parent.parent / "ai-instructions.md"


def _parse_metadata(text: str) -> re.Match[str]:
    match = _META_RE.search(text)
    if not match:
        raise ValueError("Could not locate a `linguisticMetadata = { ... } contentType: json` block")
    return match


def get_instructions(culture_path: Path) -> str | None:
    """Return the current CustomInstructions for a culture file, or None."""
    match = _parse_metadata(culture_path.read_text(encoding="utf-8"))
    obj = json.loads(match.group("json"))
    return obj.get("CustomInstructions")


def set_instructions(culture_path: Path, md_text: str) -> bool:
    """Inject/replace CustomInstructions in the culture linguisticMetadata. Returns True if changed."""
    text = culture_path.read_text(encoding="utf-8")
    match = _parse_metadata(text)
    obj = json.loads(match.group("json"))
    obj["CustomInstructions"] = md_text
    dumped = json.dumps(obj, ensure_ascii=False)
    new_text = text[: match.start()] + f"{match.group('pre')} {dumped}" + match.group("post") + text[match.end() :]
    if new_text == text:
        return False

    # Round-trip guard: the rewritten block must still parse back to the same object.
    check = _parse_metadata(new_text)
    if json.loads(check.group("json")) != obj:
        raise RuntimeError(f"Re-serialization of {culture_path} did not round-trip; aborting write")

    culture_path.write_text(new_text, encoding="utf-8")
    return True


def iter_models(root: Path) -> list[Path]:
    """All *.SemanticModel folders under migrations/*/fabric/."""
    return sorted(p for p in root.glob("migrations/*/fabric/*.SemanticModel") if p.is_dir())


def cmd_check(root: Path) -> int:
    """Print a per-model report of which semantic models carry AI instructions."""
    models = iter_models(root)
    if not models:
        log.info("No models found under %s", root / "migrations")
        return 0
    missing = 0
    for model in models:
        try:
            culture = find_culture_file(model)
            instr = get_instructions(culture)
        except (FileNotFoundError, ValueError) as exc:
            log.info("  ??  %-40s %s", model.name, exc)
            missing += 1
            continue
        if instr:
            flags = lint_instructions(instr)
            if read_qna_enabled(model) is not True:
                flags.append("qnaEnabled is not true (Q&A/Copilot will ignore these instructions)")
            suffix = f"  [!] {'; '.join(flags)}" if flags else ""
            log.info("  OK  %-40s %d chars%s", model.name, len(instr), suffix)
        else:
            log.info("  --  %-40s (no CustomInstructions)", model.name)
            missing += 1
    log.info("%d/%d models have AI instructions", len(models) - missing, len(models))
    return 0


def stamp_one(model_dir: Path, md_path: Path) -> bool:
    """Stamp a single model's culture with the markdown instructions; returns True if it changed."""
    md_text = md_path.read_text(encoding="utf-8").strip()
    if len(md_text) > HARD_CHAR_LIMIT:
        raise ValueError(f"AI instructions exceed the {HARD_CHAR_LIMIT}-char limit ({len(md_text)}): {md_path}")
    ensure_culture(model_dir)
    ensure_qna_enabled(model_dir)
    culture = find_culture_file(model_dir)
    changed = set_instructions(culture, md_text)
    verb = "stamped" if changed else "unchanged"
    log.info("  %s  %s  <- %s (%d chars)", verb, culture.relative_to(model_dir), md_path.name, len(md_text))
    for warning in lint_instructions(md_text):
        log.warning("    [!] %s", warning)
    return changed


def cmd_all(root: Path) -> int:
    """Stamp every migration that has an ai-instructions.md next to its fabric folder."""
    changed = 0
    for model in iter_models(root):
        md_path = default_md_for(model)
        if not md_path.is_file():
            continue
        if stamp_one(model, md_path):
            changed += 1
    log.info("Done. %d model(s) updated.", changed)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, help="Path to a *.SemanticModel folder")
    parser.add_argument(
        "--md", type=Path, help="Markdown instructions file (defaults to <migration>/ai-instructions.md)"
    )
    parser.add_argument("--all", action="store_true", help="Stamp every migration that has an ai-instructions.md")
    parser.add_argument("--check", action="store_true", help="Report which models have AI instructions")
    args = parser.parse_args(argv)

    if args.check:
        return cmd_check(REPO_ROOT)
    if args.all:
        return cmd_all(REPO_ROOT)
    if not args.model:
        parser.error("provide --model, or use --all / --check")

    model_dir = args.model.resolve()
    md_path = (args.md or default_md_for(model_dir)).resolve()
    if not md_path.is_file():
        parser.error(f"instructions file not found: {md_path}")
    stamp_one(model_dir, md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
