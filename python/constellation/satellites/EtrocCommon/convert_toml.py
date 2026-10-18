"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Rewrite old ETROC controller TOMLs to the Constellation 1.x layout.

Old files (ETROC2Classic grammar) use `[satellites.ETROC2Classic.One]`; the
1.x controller reads `[<SatelliteType>.<name>]`, so those sections never
reach the satellites. This tool renames the section headers and drops the
empty `[satellites.X]` parent tables; a parent table that holds keys becomes
`[<NewType>._default]` (`[_default]` for a bare `[satellites]`). Keys and
comments are kept; a `[satellites...]` header it cannot map is refused.

Usage:
    python -m constellation.satellites.EtrocCommon.convert_toml old.toml > new.toml
"""

import re
import sys

from .satellite_types import OLD_TYPE_NAMES

# lower-case 0.x type -> 1.x type
TYPE_MAP = {old.lower(): new for new, old in OLD_TYPE_NAMES.items()}

# [satellites], [satellites.<Type>], [satellites.<Type>.<name>] and deeper ([...<name>._data])
_HEADER = re.compile(r"^\s*\[\s*satellites((?:\.[A-Za-z0-9_-]+)*)\s*\]\s*(#.*)?$")
# any other header that starts in [satellites ... (quoted keys, arrays of tables, spaces around dots)
_OTHER_SATELLITES_HEADER = re.compile(r"""^\s*\[\[?\s*["']?satellites\b""")


def convert(text: str) -> str:
    """Rename the 0.x section headers. A parent table that holds keys becomes the matching
    default section ([satellites] -> [_default], [satellites.<Old>] -> [<New>._default]);
    a header this cannot map raises ValueError instead of being passed through."""
    out = []
    pending = None  # default-section header to write if the dropped parent table holds keys
    for number, line in enumerate(text.splitlines(), start=1):
        m = _HEADER.match(line)
        if m:
            parts = [p for p in m.group(1).split(".") if p]
            comment = f"  {m.group(2)}" if m.group(2) else ""
            if parts:
                parts[0] = TYPE_MAP.get(parts[0].lower(), parts[0])
            if len(parts) >= 2:
                out.append("[" + ".".join(parts) + "]" + comment)
                pending = None
            else:
                # parent table: dropped unless keys follow it
                pending = ("[_default]" if not parts else f"[{parts[0]}._default]") + comment
            continue
        if _OTHER_SATELLITES_HEADER.match(line):
            raise ValueError(
                f"line {number}: cannot convert {line.strip()!r}; rewrite it by hand as [<Type>.<name>]"
            )
        stripped = line.strip()
        if stripped.startswith("["):
            pending = None
        elif pending is not None and stripped and not stripped.startswith("#"):
            out.append(pending)
            pending = None
        out.append(line)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with open(args[0]) as f:
        try:
            converted = convert(f.read())
        except ValueError as e:
            print(f"convert_toml: {args[0]}: {e}", file=sys.stderr)
            return 1
    sys.stdout.write(converted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
