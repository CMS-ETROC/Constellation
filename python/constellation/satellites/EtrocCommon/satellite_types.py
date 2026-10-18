"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

The ETROC satellite types, their 0.x names and the keys every real section of a type sets.

One table for the satellites (the initialize guard in `config.load_typed_config`), the
TOML converter (`convert_toml`) and the controller-side preflight (`check_toml`).
"""

# 1.x satellite type -> type name in the 0.x controller files ([satellites.<old>.<name>])
OLD_TYPE_NAMES = {
    "EtrocTransmitter": "ETROC2Classic",
    "EtrocReceiver": "ETROC2Receiver",
    "EtrocWaveform": "ETROC2Waveform",
    "EtrocCounters": "ETROC2Counters",
}

# Keys every real section of the type sets (never hostname: the team's files rely on its default).
# A configuration with keys but none of these came from [_default] / [<Type>._default] alone,
# i.e. the satellite's own section did not reach it. Evidence (sidecar/etroc3/constellation_tomls,
# 11 files of 2025): every ETROC2Classic section sets fast_command_memo, polarity, active_channel
# and timestamp (triggerbit_delay is missing in two); every ETROC2Receiver section sets
# output_path, translate and skip_fillers; the one ETROC2Waveform section (testws.toml) sets the
# transmitter keys plus the chip identity. EtrocCounters has no field files yet: the keys are
# those of example_counters.toml that a counters-only bench also needs.
REQUIRED_KEYS = {
    "EtrocTransmitter": ("fast_command_memo", "polarity", "active_channel", "timestamp"),
    "EtrocReceiver": ("output_path", "translate", "skip_fillers"),
    "EtrocWaveform": (
        "fast_command_memo", "polarity", "active_channel", "timestamp",
        "i2c_port", "chip_addresses", "ws_addresses", "chip_names",
    ),
    "EtrocCounters": ("output_path", "channel", "fc_mode_on_launch"),
}

ETROC_TYPES = tuple(OLD_TYPE_NAMES)
