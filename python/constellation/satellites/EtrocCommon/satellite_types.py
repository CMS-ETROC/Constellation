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

# The keys each type reads: its DEFAULT_CONFIG (a test keeps the two equal), so that check_toml
# can name unknown keys without importing the hardware modules.
_KC705_KEYS = (
    "active_channel", "clear_fifo", "connect_timeout", "counter_duration", "data_delays_01", "data_delays_23",
    "fast_command_memo", "fc_delays", "firmware", "hostname", "num_fifo_read", "polarity", "port",
    "prescale_factor", "reset_counter", "socket_timeout", "timestamp", "triggerbit_delay",
)
CONFIG_KEYS = {
    "EtrocTransmitter": _KC705_KEYS,
    "EtrocReceiver": ("flush_interval", "keep_time", "output_path", "skip_fillers", "translate"),
    "EtrocWaveform": _KC705_KEYS + (
        "active_boards", "chip_addresses", "chip_names", "i2c_port", "offset", "output_path",
        "ws_active_channel", "ws_addresses",
    ),
    "EtrocCounters": (
        "channel", "check_layout", "chip_address", "chip_name", "chip_setup", "connect_timeout",
        "fc_mode_on_launch", "firmware_window_s", "hostname", "i2c_port", "l1a_delay", "output_path", "port",
        "power_mode", "prescale_factor", "settle_windows", "socket_timeout", "ws_address",
    ),
}
# Keys every type accepts besides its own (config.ALLOW_DEFAULTS_KEY; the counters' register table)
COMMON_KEYS = ("allow_default_config", "kc705_registers")
# 0.x keys the 1.x satellites do not implement; the satellite would ignore them silently
UNSUPPORTED_0X_KEYS = {
    "compressed_binary": "the 1.x receiver writes no compressed binary output; drop the key",
    "save_html": "the 1.x satellites write no HTML output; drop the key",
}
