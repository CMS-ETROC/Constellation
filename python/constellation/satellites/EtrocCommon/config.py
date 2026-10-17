"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Typed configuration loading shared by the ETROC satellites.
"""

from typing import Any

from constellation.core.configuration import Configuration

# Key that lets an operator run a satellite on its built-in defaults on purpose.
ALLOW_DEFAULTS_KEY = "allow_default_config"


def user_keys(config: Configuration) -> list[str]:
    """Keys set in the configuration file, without framework sections (`_autonomy`, `_data`, ...)."""
    return [k for k in config.get_keys() if not k.startswith("_")]


def get_typed(config: Configuration, key: str, default_value: Any) -> Any:
    """Get `key` converted to the type of `default_value` (ints are accepted for float keys)."""
    if isinstance(default_value, bool):
        return config.get_bool(key, default_value)
    if isinstance(default_value, int):
        return config.get_int(key, default_value)
    if isinstance(default_value, float):
        return config.get_float(key, default_value)
    if isinstance(default_value, str):
        return config.get_str(key, default_value)
    if isinstance(default_value, list):
        element_type = type(default_value[0]) if default_value else None
        return config.get_array(key, default_value, element_type=element_type)
    return config.get(key, default_value)


def load_typed_config(
    satellite: Any, config: Configuration, defaults: dict[str, Any], extra_keys: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Read every key of `defaults` from `config` with the default's type and set it on `satellite`.

    Refuses an empty configuration: Constellation 1.x matches a TOML section
    `[<SatelliteType>.<name>]` (for example `[EtrocTransmitter.One]`). Older
    files written as `[satellites.ETROC2Classic.One]` register a satellite
    "satellites.etroc2classic" instead, and the real satellite then receives
    nothing and would silently run on defaults.

    `extra_keys` are keys (or sections) the satellite reads itself.
    Returns the effective configuration (file values plus defaults) for logging and BOR.
    """
    given = user_keys(config)
    allow_defaults = config.get_bool(ALLOW_DEFAULTS_KEY, False)
    if not given and not allow_defaults:
        raise RuntimeError(
            f"{satellite.name} received an empty configuration. The TOML section must be "
            f"[{type(satellite).__name__}.<name>] (Constellation 1.x: type.name); sections such as "
            f"[satellites.ETROC2Classic.One] are not matched to this satellite. "
            f"Set `{ALLOW_DEFAULTS_KEY} = true` to run on the built-in defaults deliberately."
        )

    unknown = sorted(set(given) - set(k.lower() for k in defaults) - {ALLOW_DEFAULTS_KEY} - set(extra_keys))
    if unknown:
        satellite.log.warning(f"Configuration keys not used by {type(satellite).__name__}: {unknown}")

    effective: dict[str, Any] = {}
    for key, default_value in defaults.items():
        value = get_typed(config, key, default_value)
        setattr(satellite, key, value)
        effective[key] = value
    satellite.log.info(f"Effective configuration: {effective}")
    return effective
