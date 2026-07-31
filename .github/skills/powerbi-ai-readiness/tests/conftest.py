"""Make the skill's own `scripts/` importable, from wherever this folder was copied.

The whole point of bundling the tests next to the scripts is that the pair travels as one unit: copy
`powerbi-ai-readiness/` into a Qlik or Cognos migration repo (or a global skill location) and the
tests still run there. That only holds if the import path is resolved **relative to this file** - an
absolute path, or a walk up to a repo root, would silently re-bind the tests to a host repo's
`scripts/` folder and prove nothing about the copy.
"""

import sys
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))
