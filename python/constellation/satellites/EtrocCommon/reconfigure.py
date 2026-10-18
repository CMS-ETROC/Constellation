"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

`reconfigure` (ORBIT -> reconfiguring -> ORBIT) for the ETROC satellites.

Constellation enables the `reconfigure` command when the class defines `do_reconfigure`.
Any exception inside the transition sends the satellite to ERROR (`handle_error`), and so do
keys the satellite leaves unused or that the stored configuration cannot take
(`Satellite._update_config`). This mixin therefore checks the request on the command thread,
before the FSM leaves ORBIT: keys that need a landing, invalid values, unknown keys and type
changes are refused as a failed command (the controller gets the message, the satellite stays
in ORBIT). `do_reconfigure` repeats the key/value check and then applies the change through the
satellite's own launch code paths.
"""

import copy
from typing import Any

from constellation.core.configuration import Configuration

from .config import get_typed, user_keys
from .satellite_types import CONFIG_KEYS, known_keys

LAND_HINT = "land and re-initialize to change it"


class EtrocReconfigure:
    """Mixin (before the Satellite base). The satellite defines:

    - `DEFAULT_CONFIG` and `RECONFIGURABLE_KEYS` (keys of DEFAULT_CONFIG that may change in ORBIT;
      every other DEFAULT_CONFIG key needs a landing),
    - `_validate_settings(values)` (raises ValueError for invalid values of the keys present),
    - `_apply_reconfigure(plan) -> list[str]` (writes the hardware, returns what changed),
    - optionally `RECONFIGURABLE_SECTIONS` and `_plan_sections(partial) -> dict`.
    """

    RECONFIGURABLE_KEYS: tuple[str, ...] = ()
    RECONFIGURABLE_SECTIONS: tuple[str, ...] = ()

    def reconfigure(self, config_dict: dict) -> tuple[str, Any, dict[str, Any]]:
        """Change configuration keys in ORBIT; keys that need a landing are refused and the satellite stays in ORBIT."""
        if not isinstance(config_dict, dict):
            raise TypeError(f"reconfigure takes a dictionary of keys, got {type(config_dict).__name__}")
        framework = sorted(k for k in config_dict if isinstance(k, str) and k.startswith("_"))
        if framework:
            # e.g. _data, _role: the framework applies those sections only at initialize
            raise ValueError(
                f"reconfigure does not change framework sections ({', '.join(framework)}); re-initialize to change them"
            )
        config_dict = self._match_stored_types(config_dict)
        self._check_reconfigure_allowed()
        self._plan_reconfigure(Configuration(copy.deepcopy(config_dict)))
        stored = getattr(self, "_config", None)
        if stored is not None:
            try:
                stored._validate_update(Configuration(copy.deepcopy(config_dict)))
            except Exception as e:
                raise ValueError(
                    f"{e}. reconfigure changes only keys the initialize configuration holds, with the same "
                    f"type; for anything else {LAND_HINT}"
                ) from e
        return super().reconfigure(config_dict)  # type: ignore[misc]

    def do_reconfigure(self, partial_config: Configuration) -> str:
        self._check_reconfigure_allowed()
        plan = self._plan_reconfigure(partial_config)
        changes = self._apply_reconfigure(plan)
        msg = "Reconfigured: " + ("; ".join(changes) if changes else "no keys given")
        self.log.info(msg)
        return msg

    # ------------------------------------------------------------------ helpers
    def _check_reconfigure_allowed(self) -> None:
        """Satellite-specific refusals (e.g. a scan owns the board)."""

    def _plan_sections(self, partial: Configuration) -> dict[str, Any]:
        return {}

    def _plan_reconfigure(self, partial: Configuration) -> dict[str, Any]:
        """Typed values of the keys to change; raises ValueError for keys that need a landing or bad values."""
        sat_type = type(self).__name__
        keys = user_keys(partial)
        changeable = set(self.RECONFIGURABLE_KEYS) | set(self.RECONFIGURABLE_SECTIONS)
        fixed = sorted(k for k in keys if k in self.DEFAULT_CONFIG and k not in changeable)
        if fixed:
            names = ", ".join(f"'{k}'" for k in fixed)
            raise ValueError(f"{sat_type}: {names} cannot change in ORBIT; {LAND_HINT}")
        known = set(known_keys(sat_type)) if sat_type in CONFIG_KEYS else set(self.DEFAULT_CONFIG)
        unknown = sorted(set(keys) - known - changeable)
        if unknown:
            # Constellation would refuse them as unused inside the transition, i.e. in ERROR
            raise ValueError(f"{sat_type} does not read {unknown}; reconfigure refused (check the spelling)")
        not_here = sorted(k for k in keys if k in known and k not in self.DEFAULT_CONFIG and k not in changeable)
        if not_here:
            names = ", ".join(f"'{k}'" for k in not_here)
            raise ValueError(f"{sat_type}: {names} cannot change in ORBIT; {LAND_HINT}")
        plan = {k: get_typed(partial, k, self.DEFAULT_CONFIG[k]) for k in keys if k in self.RECONFIGURABLE_KEYS}
        plan.update(self._plan_sections(partial))
        self._validate_settings(plan)
        return plan

    def _match_stored_types(self, config_dict: dict) -> dict:
        """Integers for keys stored as float become floats (`flush_interval = 5`), as at initialize."""
        stored = getattr(getattr(self, "_config", None), "_dictionary", {})
        out = {}
        for key, value in config_dict.items():
            if type(value) is int and isinstance(stored.get(str(key).lower()), float):
                value = float(value)
            out[key] = value
        return out
