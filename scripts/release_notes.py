"""Print one version's section of CHANGELOG.md, for GitHub release notes.

    python scripts/release_notes.py 0.2.0          # or v0.2.0
    python scripts/release_notes.py --check 0.2.0  # exit 1 if missing/empty

The section runs from its ``## [X.Y.Z]`` heading to the next ``## [``
heading (exclusive), without the heading itself and without the link
references at the bottom of the file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(version: str, text: str) -> str | None:
    version = version.lstrip("v")
    lines = text.splitlines()
    head = re.compile(rf"^## \[{re.escape(version)}\]")
    start = next((i for i, line in enumerate(lines) if head.match(line)), None)
    if start is None:
        return None
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## [") or re.match(r"^\[[^\]]+\]: ", line):
            break
        body.append(line)
    out = "\n".join(body).strip()
    return out or None


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    notes = section(args[0], CHANGELOG.read_text(encoding="utf-8"))
    if notes is None:
        print(f"CHANGELOG.md has no (non-empty) section for {args[0]}", file=sys.stderr)
        return 1
    if not check:
        print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
