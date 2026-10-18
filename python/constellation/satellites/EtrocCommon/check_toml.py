"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Preflight a Constellation 1.x controller TOML before loading it for the ETROC satellites.

A section the controller does not match to a satellite (misspelled type, the 0.x grammar
`[satellites.ETROC2Classic.One]`) is dropped silently: the satellite then initializes from
`[_default]` alone. This check reports

1. tables in the 0.x `[satellites.*]` grammar,
2. tables whose type is an ETROC type in other case, an 0.x type name or a close misspelling,
3. other top-level tables that are neither `[_default]` nor a known satellite type,
4. `[_default]` / `[<EtrocType>._default]` holding a required key of an ETROC type: every
   satellite then receives it, and the initialize guard can no longer tell that a satellite's
   own section is missing,
5. with `--satellites Type.Name,...`: every expected satellite without a section of its own,
6. for every `[<EtrocType>.<name>]`: the configuration the satellite receives, merged by the
   controller's own parser (`[_default]` < `[<Type>._default]` < `[<Type>.<name>]`); an error
   where the initialize guard would refuse it (none of satellite_types.REQUIRED_KEYS and no
   `allow_default_config`), a note for required keys partly missing, and the keys the type
   does not read (typos, 0.x keys such as `compressed_binary`).

`$_{VAR}` references are checked as written: the controller substitutes them when it loads the
file, so a variable missing in this shell is not an error here.

Usage:
    python -m constellation.satellites.EtrocCommon.check_toml file.toml [--allow-type TYPE ...]
        [--satellites EtrocTransmitter.One,EtrocReceiver.One]

Exit status 0 when clean, 1 with a report of the problems, 2 for a usage error.
"""

import argparse
import difflib
import pkgutil
import re
import sys
import tomllib
from dataclasses import dataclass, field
from typing import Any

from constellation.core.controller_configuration import ControllerConfiguration, FileType

from constellation.core.protocol.cscp1 import is_valid_satellite_name, is_valid_satellite_type
from .satellite_types import CONFIG_KEYS, ETROC_TYPES, OLD_TYPE_NAMES, REQUIRED_KEYS, UNSUPPORTED_0X_KEYS, known_keys

# Framework sections (keys inside a satellite or default section, never satellite types)
FRAMEWORK_SECTIONS = ("_autonomy", "_conditions", "_data")
CLOSE_MATCH_CUTOFF = 0.75
ALLOW_DEFAULTS_KEY = "allow_default_config"
_ENV_REF = re.compile(r"(?<!\\)\$_\{(\w+)(?::-([^}]*))?\}")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # (canonical name, merged configuration, missing required keys)
    sections: list[tuple[str, dict[str, Any], list[str]]] = field(default_factory=list)
    # every [<EtrocType>.<name>] found, as written
    instances: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _python_satellite_types() -> set[str]:
    """Satellite types shipped as Python packages next to the ETROC ones (Keithley, LeCroy, ...)."""
    try:
        from constellation import satellites

        names = {m.name for m in pkgutil.iter_modules(satellites.__path__) if m.ispkg}
    except Exception:
        return set()
    return {n for n in names if not n.startswith("_") and n != "EtrocCommon"} - set(ETROC_TYPES)


def _instance_names(table: Any) -> list[str]:
    if not isinstance(table, dict):
        return []
    return [n for n, v in table.items() if isinstance(v, dict) and n.lower() != "_default"]


def _unknown_key_problem(where: str, key: str, known: list[str]) -> str:
    if key in UNSUPPORTED_0X_KEYS:
        return f"{where}: `{key}` is not supported in 1.x ({UNSUPPORTED_0X_KEYS[key]})"
    close = difflib.get_close_matches(key, known, n=1, cutoff=CLOSE_MATCH_CUTOFF)
    hint = f"; did you mean `{close[0]}`?" if close else ""
    return f"{where}: unknown key `{key}`, the satellite does not read it{hint}"


def check(text: str, allow_types: tuple[str, ...] = (), satellites: tuple[str, ...] = ()) -> Report:
    report = Report()
    env_refs = sorted({m.group(1) for m in _ENV_REF.finditer(text)})
    if env_refs:
        report.notes.append(
            f"$_{{VAR}} references ({', '.join(env_refs)}) are checked as written; the controller substitutes "
            f"them at load time and refuses the file if one is undefined there without a :-default"
        )
        # the controller's parser substitutes at load time; give it literal markers instead
        text = _ENV_REF.sub(lambda m: f"ENV_{m.group(1)}", text)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        report.errors.append(f"not valid TOML: {e}")
        return report

    etroc_lc = {t.lower(): t for t in ETROC_TYPES}
    old_lc = {old.lower(): new for new, old in OLD_TYPE_NAMES.items()}
    other_lc = {t.lower() for t in (*_python_satellite_types(), *allow_types)}
    etroc_tables: list[tuple[str, str]] = []  # (key as written, ETROC type)

    for key, value in data.items():
        lc = key.lower()
        if lc == "_default":
            if not isinstance(value, dict):
                report.errors.append("`_default` must be a table: [_default]")
            continue
        if not isinstance(value, dict):
            report.errors.append(f"top-level key `{key}` is not a table; keys belong in a section such as [_default]")
            continue
        names = _instance_names(value) or ["<name>"]
        if lc == "satellites":
            for old_type, table in value.items():
                new = old_lc.get(old_type.lower(), "<SatelliteType>")
                for name in _instance_names(table) or ["<name>"]:
                    report.errors.append(
                        f"[satellites.{old_type}.{name}] is the 0.x grammar, 1.x does not read it: write "
                        f"[{new}.{name}] (python -m constellation.satellites.EtrocCommon.convert_toml converts the file)"
                    )
            continue
        if lc in FRAMEWORK_SECTIONS:
            report.errors.append(
                f"[{key}] at top level is read as a satellite type; framework sections go inside a "
                f"satellite or default section, e.g. [_default.{key}] or [EtrocTransmitter.<name>.{key}]"
            )
            continue
        if key in ETROC_TYPES:
            etroc_tables.append((key, key))
            continue
        if lc in etroc_lc:
            report.notes.append(
                f"[{key}.*] differs from the type {etroc_lc[lc]} only in case; the controller matches types "
                f"case-insensitively, so it works, but [{etroc_lc[lc]}.<name>] is the spelling to keep"
            )
            etroc_tables.append((key, etroc_lc[lc]))
            continue
        if lc in old_lc:
            fixes = ", ".join(f"[{old_lc[lc]}.{n}]" for n in names)
            report.errors.append(f"[{key}.*] uses the 0.x type name; no 1.x satellite has it: write {fixes}")
            continue
        if lc in other_lc:
            report.notes.append(f"[{key}.*] is not an ETROC satellite type; not checked")
            continue
        close = difflib.get_close_matches(lc, [*etroc_lc, *old_lc], n=1, cutoff=CLOSE_MATCH_CUTOFF)
        if close:
            suggestion = etroc_lc.get(close[0]) or old_lc[close[0]]
            for name in names:
                report.errors.append(
                    f"[{key}.{name}] matches no satellite type; did you mean [{suggestion}.{name}]? As written "
                    f"no satellite receives it, and {suggestion}.{name} initializes from [_default] alone"
                )
            continue
        report.errors.append(
            f"unknown top-level table [{key}]: neither [_default] nor a known satellite type "
            f"(ETROC types: {', '.join(ETROC_TYPES)}); if it is another satellite type of the group, "
            f"pass --allow-type {key}"
        )

    every_required = {k: [t for t in ETROC_TYPES if k in REQUIRED_KEYS[t]] for t in ETROC_TYPES for k in REQUIRED_KEYS[t]}
    default = next((v for k, v in data.items() if k.lower() == "_default" and isinstance(v, dict)), {})
    for k in default:
        if k in every_required:
            report.errors.append(
                f"[_default] holds `{k}`, a required key of {'/'.join(every_required[k])}: every satellite receives "
                f"it, so a missing [<Type>.<name>] section goes unnoticed at initialize; move it to the satellite's "
                f"own section"
            )
        elif not k.startswith("_") and not any(k in known_keys(t) for t in ETROC_TYPES):
            report.errors.append(_unknown_key_problem("[_default]", k, sorted({x for t in ETROC_TYPES for x in CONFIG_KEYS[t]})))

    instances: list[tuple[str, str]] = []
    for key, sat_type in etroc_tables:
        for name, value in data[key].items():
            if not isinstance(value, dict):
                report.errors.append(
                    f"[{key}] holds the key `{name}` directly; the controller refuses that. Keys for every "
                    f"{sat_type} go in [{sat_type}._default], keys for one satellite in [{sat_type}.<name>]"
                )
            elif name.lower() == "_default":
                for k in value:
                    if k in REQUIRED_KEYS[sat_type]:
                        report.errors.append(
                            f"[{key}._default] holds `{k}`, a required key of {sat_type}: every {sat_type} receives "
                            f"it, so a missing [{sat_type}.<name>] section goes unnoticed at initialize; move it to "
                            f"the satellite's own section"
                        )
            else:
                instances.append((sat_type, name))
                report.instances.append(f"{key}.{name}")
                if not is_valid_satellite_name(name):
                    report.errors.append(
                        f"[{key}.{name}]: `{name}` is not a valid satellite name (letters, digits and _ only)"
                    )

    try:
        merged_config = ControllerConfiguration.from_string(text, FileType.TOML)
    except Exception as e:
        report.errors.append(f"the controller refuses the file: {e}")
        return report

    for sat_type, name in instances:
        try:
            merged = merged_config.get_satellite_configuration(f"{sat_type}.{name}")._dictionary
        except Exception as e:
            report.errors.append(f"[{sat_type}.{name}]: the controller cannot merge its configuration: {e}")
            continue
        missing = [k for k in REQUIRED_KEYS[sat_type] if k not in merged]
        report.sections.append((f"{sat_type}.{name}", merged, missing))
        allow_defaults = bool(merged.get(ALLOW_DEFAULTS_KEY, False))
        if len(missing) == len(REQUIRED_KEYS[sat_type]) and not allow_defaults:
            report.errors.append(
                f"[{sat_type}.{name}]: none of the required keys {list(REQUIRED_KEYS[sat_type])} reaches it; the "
                f"satellite refuses to initialize (set `{ALLOW_DEFAULTS_KEY} = true` to run on defaults deliberately)"
            )
        elif missing:
            report.notes.append(
                f"[{sat_type}.{name}]: required keys {missing} not set; the satellite uses its defaults for them"
            )
        known = sorted(CONFIG_KEYS[sat_type])
        own = {}
        for table in (data.get(sat_type, {}), *(v for k, v in data.items() if k.lower() == sat_type.lower() and k != sat_type)):
            for section, values in table.items():
                if isinstance(values, dict) and section.lower() in ("_default", name.lower()):
                    own.update(values)
        for k in own:
            if k.startswith("_") or k in known_keys(sat_type):
                continue
            report.errors.append(_unknown_key_problem(f"[{sat_type}.{name}]", k, known))

    found = {i.lower() for i in report.instances}
    for expected in satellites:
        if "." not in expected or not all(map(is_valid_satellite_type, expected.split(".", 1))):
            report.errors.append(f"--satellites: `{expected}` is not Type.Name")
            continue
        if expected.lower() not in found:
            report.errors.append(
                f"[{expected}] has no section in the file; that satellite would initialize from [_default] alone. "
                f"Sections found: {', '.join(report.instances) or 'none'}"
            )
    if not instances and not report.errors:
        report.errors.append(f"no [<Type>.<name>] section of an ETROC type ({', '.join(ETROC_TYPES)}) found")
    return report


def format_report(path: str, report: Report) -> str:
    lines = [f"check_toml: {path}", f"ETROC satellite sections found: {', '.join(report.instances) or 'none'}"]
    for canonical, merged, missing in report.sections:
        sat_type = canonical.split(".")[0]
        lines.append(
            f"[{canonical}] receives ([_default] < [{sat_type}._default] < [{canonical}]):"
        )
        for key, value in merged.items():
            lines.append(f"    {key} = {value!r}")
        lines.append(f"    required keys {list(REQUIRED_KEYS[sat_type])}: " + (f"MISSING {missing}" if missing else "present"))
    if report.notes:
        lines.append("Notes:")
        lines += [f"  - {n}" for n in report.notes]
    if report.errors:
        lines.append(f"Problems ({len(report.errors)}):")
        lines += [f"  - {e}" for e in report.errors]
    else:
        lines.append("OK")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m constellation.satellites.EtrocCommon.check_toml",
        description="Preflight a Constellation 1.x controller TOML for the ETROC satellites.",
    )
    parser.add_argument("file", help="controller configuration (TOML)")
    parser.add_argument(
        "--allow-type", action="append", default=[], metavar="TYPE",
        help="another satellite type in the group (e.g. a C++ satellite); its tables are not reported",
    )
    parser.add_argument(
        "--satellites", default="", metavar="Type.Name,...",
        help="the satellites the group runs (e.g. EtrocTransmitter.One,EtrocReceiver.One); each must have a section",
    )
    args = parser.parse_args(argv)
    try:
        with open(args.file, "rb") as f:
            text = f.read().decode()
    except OSError as e:
        print(f"check_toml: cannot read {args.file}: {e}", file=sys.stderr)
        return 2
    report = check(text, tuple(args.allow_type), tuple(s.strip() for s in args.satellites.split(",") if s.strip()))
    print(format_report(args.file, report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
