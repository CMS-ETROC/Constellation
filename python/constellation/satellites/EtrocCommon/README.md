# ETROC satellites on Constellation 1.x (branch ETROC2v3)

This branch carries the ETROC satellites (EtrocTransmitter, EtrocReceiver,
EtrocWaveform, EtrocCounters) on upstream Constellation v1.0.2. EtrocCommon holds
the code they share: `cmd_interpret` (KC705 TCP register protocol), `fast_command`
(FC-memory programming from a memo), `commands` (KC705 register commands),
`config` (typed configuration loading), `new_wrapper_i2cGui2` (ETROC2 I2C) and
`convert_toml`.

## Install

Constellation 1.x needs Python >= 3.11 (`requires-python` in `pyproject.toml`).

```sh
python3.13 -m venv venv-cnstln1          # any Python >= 3.11
source venv-cnstln1/bin/activate
pip install meson-python meson ninja
pip install --no-build-isolation -e ".[cli,etroc]"
```

On a machine that already runs the 0.7 satellites (etroc3: pyenv 3.13.1 with
ConstellationDAQ 0.7 installed editable from `/home/daq/ETROC2/Constellation`), use a
separate venv and clone as above so the 0.7 setup keeps working. Do not run 0.7 and
1.x satellites or controllers in the same group.

The satellites are `SatelliteEtrocTransmitter`, `SatelliteEtrocReceiver`,
`SatelliteEtrocWaveform` and `SatelliteEtrocCounters`; all report version `ETROC2v3`.

## Controller files

1.x matches a TOML section `[<SatelliteType>.<name>]`
(docs/operator_guide/concepts/configuration_files.md). Our older files use
`[satellites.ETROC2Classic.One]`; those sections reach no satellite, and the
satellites used to run on their defaults without notice. The satellites now refuse an
empty configuration (set `allow_default_config = true` to run on defaults on purpose),
warn about keys they do not use, and log the effective configuration.

Convert an old file:

```sh
python -m constellation.satellites.EtrocCommon.convert_toml old.toml > new.toml
```

It maps ETROC2Classic to EtrocTransmitter, ETROC2Receiver to EtrocReceiver,
ETROC2Waveform to EtrocWaveform and ETROC2Counters to EtrocCounters, and keeps keys
and comments.

## API changes from 0.7 to 1.0 used here

| Change | Reference |
|---|---|
| `@cscp_requestable(allowed_states, unpack_list)` is a decorator factory; the payload is unpacked into typed arguments (int/str/bool/float checked); a single non-list argument needs `unpack_list=False` | docs/application_development/functionality/custom_commands.md; `core/commandmanager.py` `_extract_payload_args` |
| `_<cmd>_is_allowed` hooks are no longer called; `allowed_states` replaces them | same |
| BOR/EOR user tags via the `self.bor` / `self.eor` properties; the BOR carries the configuration | docs/application_development/functionality/data_transmission.md |
| Satellite version via `class X(Satellite, version=...)` and `SatelliteArgumentParser(version=...)` | `core/satellite.py` |
| Metrics: `@schedule_metric(unit, interval, allowed_states)`, `register_metric` + `stat`; a scheduled metric is polled only with subscribers, `None` is skipped | docs/application_development/functionality/metrics.md; `core/monitoring.py` |
| Configuration: typed getters (`get_int`, `get_float`, `get_bool`, `get_str`, `get_array`), sections via `get_section`; unused keys are reported | docs/application_development/functionality/configuration_sections.md; `core/configuration.py` |
| `fail_gracefully()` on entering ERROR; `stop_requested()` in `do_run` | docs/application_development/tutorials/satellite_py.md |
| Receiver hooks `receive_bor/receive_data/receive_eor`, built-in rx metrics | docs/application_development/functionality/data_reception.md |
| Build from source with meson-python | docs/application_development/intro/install_from_source.md |

## Behaviour changes in the ETROC satellites

- Register commands are allowed in ORBIT only. Single-value commands take a plain value
  (`set_ledpage(2)`); two-value commands take `[delay, channel]`.
- KC705 connections use a 5 s connect timeout and an I/O timeout (EtrocTransmitter 60 s,
  EtrocWaveform 10 s, EtrocCounters 5 s; `socket_timeout = 0` turns it off), so a busy or
  stalled board ends in ERROR with a message about the single TCP client instead of
  hanging.
- `fail_gracefully()` closes the KC705 socket, so a new initialize can reconnect.
- EtrocReceiver writes each BOR/EOR as JSON into the run directory and publishes an
  input-rate metric.
- FC memos are validated: unknown words are rejected instead of ignored.
