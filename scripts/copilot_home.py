"""
purpose: select the Copilot configuration home without checking or substituting paths.
internal: true
internal-reason: shared path-only library for toolkit consumers, not an agent-facing CLI.
"""

import os
from pathlib import Path

COPILOT_HOME_ENV = "COPILOT_HOME"


def copilot_home() -> Path:
    """Return the configured home, or the default only when unset or empty."""
    configured = os.environ.get(COPILOT_HOME_ENV)
    return Path(configured) if configured else Path.home() / ".copilot"
