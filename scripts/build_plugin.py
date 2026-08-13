"""
purpose: build an installable GitHub Copilot CLI marketplace from this repo's skill bundles, so the
         source-tool-agnostic ones can be installed as a plugin and referenced BY NAME from agent
         personas. A repo-local `.github/skills/` bundle is NOT registered in a subagent's skill
         registry - the `skill` tool rejects the name outright (docs/agent-architecture.md section
         6.1) - so path-reading is the only option today. Plugin-provided skills ARE registered, which
         is the whole reason this build exists.

         The marketplace is published to a SEPARATE, thin repo on purpose: `/plugin marketplace add`
         clones the whole repository, and this one carries ~108 MB of history plus ~62 MB of tracked
         files to deliver ~26 KB of skills.

usage:   python scripts/build_plugin.py --out dist/marketplace
         python scripts/build_plugin.py --out dist/marketplace --check   # verify only, exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".github" / "skills"

MARKETPLACE_NAME = "powerbi-playbook-collection"
PLUGIN_NAME = "powerbi-playbook"
VERSION = "0.3.0"
PUBLISH_REPO = "https://github.com/Guust-Franssens/powerbi-playbook"

# Only source-tool-agnostic bundles ship. `sentinel-probe` is a diagnostic, and anything
# Tableau-specific belongs in the migration repo's personas, not in a reusable plugin.
#
# `pbip-model-refresh` is HELD BACK at v0.3.0. Its persist path validates `cache.abf` as a CFBF/OLE2
# container, which the format is not — measured against three real caches in `examples/`, the check
# rejects every one, so AMO persistence cannot succeed. Four review rounds of hardening around that
# wrong premise added a lock and a commit-detection fingerprint that introduced two further ways to
# leave a project unopenable. It ships again once the persist path is simplified (see the tracking
# issue), not before: a bundle whose headline feature cannot work is worse than an absent one,
# because the gotcha bundles' cross-references at least fail loudly.
SHIPPED_SKILLS = (
    "powerbi-ai-readiness",
    "powerbi-report-gotchas",
    "powerbi-semantic-model-gotchas",
)

PLUGIN_DESCRIPTION = (
    "Reusable Power BI skills: make a semantic model AI-ready, persist a refreshed local PBIP, and "
    "avoid the PBIR and TMDL defects that pass validation but break at open, refresh or render. "
    "Covers descriptions, enumerated domains, model-level AI instructions (CustomInstructions) and "
    "the qnaEnabled switch that silently voids them; refreshing a PBIP in Power BI Desktop and "
    "saving it headlessly to cache.abf via AMO ImageSave; and two hard-won gotcha catalogues for "
    "report authoring and semantic modelling. Source-tool agnostic - the input is "
    "already a Power BI model - so it applies to Tableau, Qlik and Cognos migrations alike."
)

KEYWORDS = [
    "powerbi",
    "power-bi",
    "semantic-model",
    "tmdl",
    "pbip",
    "copilot",
    "ai-readiness",
    "migration",
    "tableau",
    "fabric",
]

README = """# Power BI migration skills

Reusable GitHub Copilot CLI skills for getting a **Power BI semantic model** production-ready:
making it answer natural-language questions correctly, and persisting a refreshed local PBIP.

Both skills are **source-tool agnostic** — the input is already a Power BI model — so they apply
equally to a Tableau, Qlik or Cognos migration.

## Install

```
/plugin marketplace add {repo_slug}
/plugin install {plugin}@{marketplace}
```

## What's included

| Skill | Use it for |
|---|---|
| `powerbi-ai-readiness` | Descriptions, enumerated categorical domains, model-level AI instructions (`CustomInstructions`), and the `qnaEnabled` switch that silently voids all of it |
| `pbip-model-refresh` | Refreshing a local PBIP/TMDL model in Power BI Desktop and persisting it to `.pbi/cache.abf` headlessly via AMO `ImageSave`, with strict pid binding |

## Why a plugin and not a repo-local skill

A skill committed to a consuming repo's `.github/skills/` is **not** registered inside a Copilot
custom-agent subagent — the `skill` tool rejects the name:

```
Skill "<name>" not found. Available skills: ...
```

Plugin-provided skills *are* registered, so an agent persona can reference them **by name** instead
of reading a file path. That is the entire reason this marketplace exists.

## Source

Generated from [{source_repo}]({source_repo_url}) by `scripts/build_plugin.py`.
Each skill folder is self-contained — `SKILL.md` plus its own `scripts/` and `tests/` — and the
source repo's CI proves that by copying the folder to a temp directory and running its tests there
with the source repo unimportable.

Do not edit these files here; edit them in the source repo and re-publish.
"""


def shipped_skill_dirs() -> list[Path]:
    """The bundles selected for publication, verified to exist."""
    dirs = []
    for name in SHIPPED_SKILLS:
        path = SKILLS_DIR / name
        if not (path / "SKILL.md").is_file():
            raise SystemExit(f"BUILD: ERROR no SKILL.md for '{name}' at {path}")
        dirs.append(path)
    return dirs


def marketplace_manifest() -> dict:
    """The `.claude-plugin/marketplace.json` contract Copilot CLI reads."""
    return {
        "name": MARKETPLACE_NAME,
        "metadata": {
            "description": "Reusable Power BI semantic-model skills for GitHub Copilot CLI",
            "version": VERSION,
        },
        "owner": {"name": "Guust Franssens"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": f"./plugins/{PLUGIN_NAME}",
                "description": PLUGIN_DESCRIPTION,
                "version": VERSION,
                "skills": [f"./skills/{name}" for name in SHIPPED_SKILLS],
                "agents": [],
                "repository": PUBLISH_REPO,
                "keywords": KEYWORDS,
                "license": "MIT",
            }
        ],
    }


def _force_remove(func, path, _exc) -> None:
    """`shutil.rmtree` error handler: clear the read-only bit and retry.

    Git marks objects in `.git/objects` read-only, and on Windows `os.unlink` refuses those outright
    with `PermissionError: [WinError 5]`. Without this, rebuilding into a directory that has ever
    been a git clone fails.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree_force(path: Path) -> None:
    """`shutil.rmtree` that clears read-only bits, on both Python 3.11 and 3.12+.

    `onexc` is Python 3.12+; this repo targets 3.11 (`pyproject.toml` py-version), where passing it
    raises `TypeError`. `onerror` is deprecated in 3.12 but still honoured, so the split below is the
    one spelling that works on both.

    This lives in a helper because it did NOT used to: the guard existed at one call site while
    `main()` called `rmtree(..., onexc=...)` directly, so `--check` crashed with `TypeError` on the
    repo's own declared Python and could never have caught an unpublished edit.
    """
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        # pylint's deprecated-argument check is static, so it flags this branch even though it only
        # ever runs on 3.11, where `onerror` is the CORRECT (and only) spelling.
        # pylint: disable-next=deprecated-argument
        shutil.rmtree(path, onerror=lambda fn, p, _exc: _force_remove(fn, Path(p), None))


def _clear_but_keep_git(out: Path) -> None:
    """Empty `out` of generated content while preserving `.git`.

    The publish workflow is clone -> rebuild -> commit -> push, so blowing away the whole directory
    would delete the clone's history and turn every republish into a fresh `git init`. Only the
    generated entries are removed.
    """
    for entry in out.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            _rmtree_force(entry)
        else:
            entry.chmod(stat.S_IWRITE)
            entry.unlink()


def build(out: Path) -> None:
    """Generate the whole marketplace tree at `out`, replacing any previously generated content."""
    skills = shipped_skill_dirs()

    if out.exists():
        _clear_but_keep_git(out)
    plugin_skills = out / "plugins" / PLUGIN_NAME / "skills"
    plugin_skills.mkdir(parents=True)

    for skill in skills:
        shutil.copytree(
            skill,
            plugin_skills / skill.name,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )

    manifest_dir = out / ".claude-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "marketplace.json").write_text(
        json.dumps(marketplace_manifest(), indent=2) + "\n", encoding="utf-8"
    )

    slug = PUBLISH_REPO.rsplit("github.com/", 1)[-1]
    (out / "README.md").write_text(
        README.format(
            repo_slug=slug,
            plugin=PLUGIN_NAME,
            marketplace=MARKETPLACE_NAME,
            source_repo="Guust-Franssens/tableau-to-powerbi-migration",
            source_repo_url="https://github.com/Guust-Franssens/tableau-to-powerbi-migration",
        ),
        encoding="utf-8",
    )
    (out / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")


def describe(out: Path) -> int:
    """Print what was produced; returns the generated file count.

    `.git` is excluded: rebuilding into a clone is the intended workflow, and counting its objects
    would report a plugin many times its real size.
    """
    files = sorted(p for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts)
    total = sum(p.stat().st_size for p in files)
    print(f"BUILD: {out}")
    print(f"  marketplace : {MARKETPLACE_NAME}")
    print(f"  plugin      : {PLUGIN_NAME} v{VERSION}")
    print(f"  skills      : {', '.join(SHIPPED_SKILLS)}")
    print(f"  files       : {len(files)} totalling {total / 1024:.1f} KB")
    return len(files)


def _generated_files(root: Path) -> dict[Path, bytes]:
    """Every generated file under `root`, keyed by relative path, with `.git` excluded.

    `.git` must be excluded or `--check` is useless on the very thing it is meant to guard: a clone
    of the marketplace repo has git objects that the freshly-built scratch tree never will, so every
    comparison would report drift.
    """
    return {
        p.relative_to(root): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def main(argv: list[str] | None = None) -> int:
    """Build the marketplace, or verify an existing build matches the current bundles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist/marketplace", help="output directory")
    parser.add_argument("--check", action="store_true", help="verify only; exit 1 if it would change")
    args = parser.parse_args(argv)

    out = (REPO_ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)

    if args.check:
        if not out.exists():
            print(f"BUILD: ERROR --check but nothing built at {out}")
            return 1
        existing = _generated_files(out)
        scratch = out.parent / f"{out.name}.check"
        build(scratch)
        fresh = _generated_files(scratch)
        _rmtree_force(scratch)
        if existing != fresh:
            drifted = sorted({*existing} ^ {*fresh}) or [k for k in existing if k in fresh and existing[k] != fresh[k]]
            print("BUILD: DRIFT - rebuild required. Differing paths:")
            for path in drifted[:10]:
                print(f"  {path}")
            return 1
        print("BUILD: OK - published tree matches the current skill bundles.")
        return 0

    build(out)
    describe(out)
    print("\nNext: publish the contents of that folder to the marketplace repo, then:")
    print(f"  /plugin marketplace add {PUBLISH_REPO.rsplit('github.com/', 1)[-1]}")
    print(f"  /plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
