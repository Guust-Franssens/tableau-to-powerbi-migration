"""
purpose: forwarding shim. The real script now ships INSIDE the `powerbi-ai-readiness` skill, at
         `.github/skills/powerbi-ai-readiness/scripts/set_ai_instructions.py`, so the skill is one
         self-contained copyable unit. This keeps `python scripts/set_ai_instructions.py ...`
         working - `.github/agents/pbi-semantic-builder.agent.md` and `docs/` still invoke that path.
usage:   python scripts/set_ai_instructions.py --model <path to *.SemanticModel> [--md <file.md>]
         python scripts/set_ai_instructions.py --check [--strict] [--model <path>]

Temporary by design: delete it once every caller points at the skill path (the personas under
`.github/agents/` are the last holdouts). `tests/test_skills.py` proves the forward actually reaches
the bundled script, so a stale shim fails CI instead of failing mid-migration.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / ".github" / "skills" / "powerbi-ai-readiness" / "scripts" / "set_ai_instructions.py"


def main() -> int:
    """Run the bundled script in this process, with `sys.argv` untouched."""
    if not TARGET.exists():
        print(f"AI-INSTRUCTIONS: ERROR bundled script not found at {TARGET}")
        return 2
    # The skill's scripts resolve sibling modules by bare name, so their own folder must win.
    sys.path.insert(0, str(TARGET.parent))
    runpy.run_path(str(TARGET), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
