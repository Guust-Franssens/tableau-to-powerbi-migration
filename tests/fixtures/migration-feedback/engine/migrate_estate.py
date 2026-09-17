"""
purpose: Fictitious engine mutation for migration-feedback controls; drops an enabled feature.
usage:   python migrate_estate.py --input <folder> --output <new-folder>
"""

import argparse
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def main() -> None:
    """Emit the deliberately wrong feature count, not an actual migration."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sources = sorted(path for path in args.input.iterdir() if path.suffix in {".twb", ".tds", ".twbx", ".tdsx"})
    if sources[0].suffix.endswith("x"):
        with zipfile.ZipFile(sources[0]) as archive:
            raw = archive.read(archive.namelist()[0])
    else:
        raw = sources[0].read_bytes()
    root = ElementTree.fromstring(raw)
    output = json.dumps({"count": 0 if root.get("feature") == "enabled" else 1}) + "\n"
    artifact = args.output / "reports" / "Fixture.Report" / "definition" / "report.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
