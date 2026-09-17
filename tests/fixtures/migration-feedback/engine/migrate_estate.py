"""
purpose: Fictitious engine mutation for migration-feedback controls; drops an enabled feature.
usage:   python migrate_estate.py --input <folder> --output <new-folder>
"""

import argparse
import hashlib
import io
import json
import sys
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
    consumed = sources[0].read_bytes()
    if sources[0].suffix.endswith("x"):
        with zipfile.ZipFile(io.BytesIO(consumed)) as archive:
            raw = archive.read(archive.namelist()[0])
    else:
        raw = consumed
    root = ElementTree.fromstring(raw)
    output = json.dumps({"count": 0 if root.get("feature") == "enabled" else 1}) + "\n"
    artifact = args.output / "reports" / "Fixture.Report" / "definition" / "report.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(output, encoding="utf-8")
    print(output, end="")
    print(
        json.dumps(
            {
                "schema_version": 1,
                "command": sys.orig_argv,
                "cwd": str(Path.cwd()),
                "owner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "input_sha256": hashlib.sha256(consumed).hexdigest(),
                "output_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
