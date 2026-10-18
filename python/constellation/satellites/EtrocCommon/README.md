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
empty configuration, and also a configuration that holds none of the keys every real
section of the type sets (`satellite_types.REQUIRED_KEYS`): that one came from
`[_default]` or `[<Type>._default]` alone, because the satellite's own section is
misspelled or in the old grammar. The error names the section the satellite expected,
e.g. `no [EtrocTransmitter.One] section reached this satellite`. Set
`allow_default_config = true` to run on defaults on purpose. The satellites also warn
about keys they do not use and log the effective configuration.

| Type | Required keys (at least one must reach the satellite; the preflight wants all) |
|---|---|
| EtrocTransmitter | `fast_command_memo`, `polarity`, `active_channel`, `timestamp` |
| EtrocReceiver | `output_path`, `translate`, `skip_fillers` |
| EtrocWaveform | the EtrocTransmitter keys, `i2c_port`, `chip_addresses`, `ws_addresses`, `chip_names` |
| EtrocCounters | `output_path`, `channel`, `fc_mode_on_launch` |

The lists come from the 2025 controller files (every section of a type sets these keys;
`hostname` is left out because the files rely on its default). A `[_default]` that itself
sets one of them hides a missing section from the satellite; the preflight below still
reports it.

Check a file before loading it in the controller:

```sh
python -m constellation.satellites.EtrocCommon.check_toml run.toml
```

It reports tables in the old `[satellites.*]` grammar, type names that differ from an ETROC
type only in case or are 0.x names or close misspellings (with the spelling to use), other
top-level tables that are not `[_default]` or a known satellite type (pass `--allow-type X`
for a C++ satellite of the group), and for every `[<EtrocType>.<name>]` the configuration
the satellite receives, merged by the controller's own parser (`[_default]` <
`[<Type>._default]` < `[<Type>.<name>]`), with the required keys missing from it. Exit
status 0 when clean, 1 with problems, 2 when the file cannot be read.

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
| `@cscp_requestable(allowed_states, unpack_list)` is a decorator factory; the payload is a list unpacked into typed arguments, one element per argument (int/str/bool/float checked); `unpack_list=False` hands the whole payload to a single argument (used here only by the `start` override, as in the framework) | docs/application_development/functionality/custom_commands.md; `core/commandmanager.py` `_extract_payload_args` |
| `_<cmd>_is_allowed` hooks are no longer called; `allowed_states` replaces them | same |
| BOR/EOR user tags via the `self.bor` / `self.eor` properties; the BOR carries the configuration | docs/application_development/functionality/data_transmission.md |
| Satellite version via `class X(Satellite, version=...)` and `SatelliteArgumentParser(version=...)` | `core/satellite.py` |
| Metrics: `@schedule_metric(unit, interval, allowed_states)`, `register_metric` + `stat`; a scheduled metric is polled only with subscribers, `None` is skipped | docs/application_development/functionality/metrics.md; `core/monitoring.py` |
| Configuration: typed getters (`get_int`, `get_float`, `get_bool`, `get_str`, `get_array`), sections via `get_section`; unused keys are reported | docs/application_development/functionality/configuration_sections.md; `core/configuration.py` |
| `fail_gracefully()` on entering ERROR; `stop_requested()` in `do_run` | docs/application_development/tutorials/satellite_py.md |
| Receiver hooks `receive_bor/receive_data/receive_eor`, built-in rx metrics | docs/application_development/functionality/data_reception.md |
| Build from source with meson-python | docs/application_development/intro/install_from_source.md |

## Behaviour changes in the ETROC satellites

- Register commands are allowed in ORBIT only. Every command payload is a list with one
  element per argument, as MissionControl sends it: `set_ledpage([2])`,
  `set_data_phase_channel_delay([5, 1])`, `set_fast_command_memo(["Start Triggerbit"])`.
  A bare value (`set_ledpage(2)`) is refused with "Payload must be a list".
- KC705 connections use a 5 s connect timeout and an I/O timeout (EtrocTransmitter 60 s,
  EtrocWaveform 10 s, EtrocCounters 5 s), so a busy or stalled board ends in ERROR with a
  message about the single TCP client instead of hanging. `socket_timeout = 0` turns it
  off for EtrocTransmitter and EtrocWaveform; EtrocCounters requires both timeouts > 0.
- Float keys accept integers (`socket_timeout = 5` is read as 5.0).
- `fail_gracefully()` closes the KC705 socket, so a new initialize can reconnect.
  EtrocWaveform also releases the USB-ISS adapter there, on land and on re-initialize;
  its I2C messages appear under the satellite's logger (`EtrocWaveform.I2C`), once.
- EtrocTransmitter, with `clear_fifo = 1`, also resets the FIFO at every start and then
  reprograms the FC memory (the reset stops the FC playback); a start takes about 2 s
  longer. The BOR user tags carry `kc705_readback`: config registers 4-8 and 13-15 read
  from the board at start, so register commands sent in ORBIT are recorded. The FC
  memory cannot be read back; `fast_command_memo` in the BOR is the memo last programmed
  from the configuration.
- EtrocReceiver writes each BOR/EOR as JSON into the run directory and publishes an
  input-rate metric. It takes data from one transmitter: data from a second sender ends
  the run in ERROR. With several transmitters in the group, restrict each receiver:

  ```toml
  [EtrocReceiver.rx1._data]
  receive_from = ["EtrocTransmitter.tx1"]
  ```

- EtrocReceiver flushes its file every `flush_interval` seconds (default 10; 0 = only at
  rotation and stop). On ERROR or a re-initialize during a run it closes the file and
  writes `RUN_NOT_STOPPED_CLEANLY.txt` into the run directory. Translated files rotate at
  50,000 lines also when only fillers arrive. For quiet runs set `skip_fillers = 1` so the
  files hold events; for high rates use `translate = 0` (raw `.bin`, translated offline),
  since the translation runs in Python per word.
- FC memos are validated: unknown words are rejected instead of ignored.
