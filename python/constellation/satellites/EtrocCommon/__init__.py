"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Code shared by the ETROC satellites: KC705 TCP protocol (cmd_interpret),
fast-command memory programming, typed configuration loading and the
i2c_gui2 wrapper for the ETROC2 chip.
"""

# Reported by `get_version` and `--version` of every ETROC satellite on this branch.
ETROC_SATELLITES_VERSION = "ETROC2v3"
