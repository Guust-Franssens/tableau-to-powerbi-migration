"""
purpose: forwarding shim. The real script now ships INSIDE the `pbip-model-refresh` skill, at
         `.github/skills/pbip-model-refresh/scripts/refresh_pbip_model.py`, so the skill is one
         self-contained copyable unit. This keeps `python scripts/refresh_pbip_model.py ...`
         working - the four agent personas and `docs/` still invoke that path.
usage:   python scripts/refresh_pbip_model.py [--pid <pbidesktop-pid>] [--tables "A" "B"] [--save]

Temporary by design: delete it once every caller points at the skill path (the personas under
`.github/agents/` are the last holdouts). `tests/test_skills.py` proves the forward actually reaches
the bundled script, so a stale shim fails CI instead of failing at 2am in a migration.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / ".github" / "skills" / "pbip-model-refresh" / "scripts" / "refresh_pbip_model.py"


def main() -> int:
    """Run the bundled script in this process, with `sys.argv` untouched."""
    if not TARGET.exists():
        print(f"REFRESH: ERROR bundled script not found at {TARGET}")
        return 2
    # The skill's scripts import each other by bare module name, so their own folder must win.
    sys.path.insert(0, str(TARGET.parent))
    runpy.run_path(str(TARGET), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
