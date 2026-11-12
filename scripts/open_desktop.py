"""
purpose: forwarding shim. The real script ships INSIDE the `pbip-model-refresh` skill, at
         `.github/skills/pbip-model-refresh/scripts/open_desktop.py`, so the skill is one
         self-contained copyable unit. This keeps `python scripts/open_desktop.py ...` working.
usage:   python scripts/open_desktop.py <path-to.pbip>
         python scripts/open_desktop.py --check

`tests/test_skills.py` proves the forward actually reaches the bundled script, so a stale shim fails
CI instead of failing in a migration.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / ".github" / "skills" / "pbip-model-refresh" / "scripts" / "open_desktop.py"


def main() -> int:
    """Run the bundled script in this process, with `sys.argv` untouched."""
    if not TARGET.exists():
        print(f"OPEN_DESKTOP: ERROR bundled script not found at {TARGET}")
        return 2
    # The skill's scripts import each other by bare module name, so their own folder must win.
    sys.path.insert(0, str(TARGET.parent))
    runpy.run_path(str(TARGET), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
