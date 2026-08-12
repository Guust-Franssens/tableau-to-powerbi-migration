"""
purpose: Compatibility shim for the renamed semantic-model gate.
usage:   python scripts/check_m_syntax.py [<path to .SemanticModel or migration folder> ...]

Deprecated: use `python scripts/check_datamodel.py ...`. This shim remains until external callers and
older migration briefs have moved to the broader gate name; repo callers should not add new uses.
"""

from __future__ import annotations

import sys

from check_datamodel import main


if __name__ == "__main__":
    sys.exit(main())
