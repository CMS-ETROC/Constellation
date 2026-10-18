"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Typed configuration loading shared by the ETROC satellites.
"""

from typing import Any

from constellation.core.configuration import Configuration

from .satellite_types import OLD_TYPE_NAMES

# Key that lets an operator run a satellite on its built-in defaults on purpose.
ALLOW_DEFAULTS_KEY = "allow_default_config"


def user_keys(config: Configuration) -> list[str]:
    """Keys set in the configuration file, without framework sections (`_autonomy`, `_data`, ...)."""
    return [k for k in config.get_keys() if not k.startswith("_")]


def get_typed(config: Configuration, key: str, default_value: Any) -> Any:
    """Get `key` converted to the type of `default_value`.

    Float keys accept integers (`socket_timeout = 60` as well as `60.0`); the value is
    returned as float. All other types must match exactly.
    """
    if isinstance(default_value, bool):
        return config.get_bool(key, default_value)
    if isinstance(default_value, int):
        return config.get_int(key, default_value)
    if isinstance(default_value, float):
        return float(config.get_num(key, default_value))
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

    A configuration that has keys but none of the satellite's `REQUIRED_KEYS`
    (satellite_types.REQUIRED_KEYS) is refused as well: it was merged from `[_default]`
    (or `[<Type>._default]`) alone, so the satellite's own section, misspelled or in the
    old grammar, never reached it.

    `extra_keys` are keys (or sections) the satellite reads itself.
    Returns the effective configuration (file values plus defaults) for logging and BOR.
    """
    given = user_keys(config)
    allow_defaults = config.get_bool(ALLOW_DEFAULTS_KEY, False)
    sat_type = type(satellite).__name__
    if not given and not allow_defaults:
        raise RuntimeError(
            f"{satellite.name} received an empty configuration. The TOML section must be "
            f"[{sat_type}.<name>] (Constellation 1.x: type.name); sections such as "
            f"[satellites.ETROC2Classic.One] are not matched to this satellite. "
            f"Set `{ALLOW_DEFAULTS_KEY} = true` to run on the built-in defaults deliberately."
        )
    required = tuple(getattr(satellite, "REQUIRED_KEYS", ()))
    if required and not allow_defaults and not any(k in given for k in required):
        header = satellite.name if "." in satellite.name else f"{sat_type}.{satellite.name}"
        old = OLD_TYPE_NAMES.get(sat_type, "ETROC2Classic")
        raise RuntimeError(
            f"no [{header}] section reached this satellite (none of {list(required)} present); "
            f"check the section spelling, the old [satellites.{old}.<name>] grammar is not read by 1.x. "
            f"It received only {sorted(given)}, from [_default] or [{sat_type}._default]. "
            f"Check the file with `python -m constellation.satellites.EtrocCommon.check_toml <file>`; "
            f"set `{ALLOW_DEFAULTS_KEY} = true` to run without these keys deliberately."
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
