"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Rewrite old ETROC controller TOMLs to the Constellation 1.x layout.

Old files (ETROC2Classic grammar) use `[satellites.ETROC2Classic.One]`; the
1.x controller reads `[<SatelliteType>.<name>]`, so those sections never
reach the satellites. This tool renames the section headers and drops the
empty `[satellites.X]` parent tables. Keys and comments are kept.

Usage:
    python -m constellation.satellites.EtrocCommon.convert_toml old.toml > new.toml
"""

import re
import sys

TYPE_MAP = {
    "etroc2classic": "EtrocTransmitter",
    "etroc2receiver": "EtrocReceiver",
    "etroc2waveform": "EtrocWaveform",
    "etroc2counters": "EtrocCounters",
}

_HEADER = re.compile(r"^\s*\[\s*satellites\.([A-Za-z0-9_]+)(?:\.([A-Za-z0-9_]+))?\s*\]\s*(#.*)?$")


def convert(text: str) -> str:
    out = []
    for line in text.splitlines():
        m = _HEADER.match(line)
        if not m:
            out.append(line)
            continue
        sat_type, name, comment = m.group(1), m.group(2), m.group(3)
        new_type = TYPE_MAP.get(sat_type.lower(), sat_type)
        if name is None:
            # parent table only; 1.x does not need it
            continue
        out.append(f"[{new_type}.{name}]" + (f"  {comment}" if comment else ""))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with open(args[0]) as f:
        sys.stdout.write(convert(f.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
