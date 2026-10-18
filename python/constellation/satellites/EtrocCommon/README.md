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

| Type | Required keys (at least one must reach the satellite, or it refuses to initialize) |
|---|---|
| EtrocTransmitter | `fast_command_memo`, `polarity`, `active_channel`, `timestamp` |
| EtrocReceiver | `output_path`, `translate`, `skip_fillers` |
| EtrocWaveform | the EtrocTransmitter keys, `i2c_port`, `chip_addresses`, `ws_addresses`, `chip_names` |
| EtrocCounters | `output_path`, `channel`, `fc_mode_on_launch` |

The lists come from the 2025 controller files (every section of a type sets these keys;
`hostname` is left out because the files rely on its default). A `[_default]` or
`[<Type>._default]` that itself sets one of them hides a missing section from that check,
because every satellite then receives the key; the preflight below reports such a default
section as a problem, and `--satellites` names the satellites that must have a section.

Check a file before loading it in the controller:

```sh
python -m constellation.satellites.EtrocCommon.check_toml run.toml \
    --satellites EtrocTransmitter.One,EtrocReceiver.One
```

It reports tables in the old `[satellites.*]` grammar, type names that differ from an ETROC
type only in case or are 0.x names or close misspellings (with the spelling to use), other
top-level tables that are not `[_default]` or a known satellite type (pass `--allow-type X`
for a C++ satellite of the group), and for every `[<EtrocType>.<name>]` the configuration
the satellite receives, merged by the controller's own parser (`[_default]` <
`[<Type>._default]` < `[<Type>.<name>]`). It lists the satellite sections it found, and
reports as problems: a section the satellite would refuse (none of the required keys and no
`allow_default_config`), required keys in a default section, an expected `--satellites`
entry without a section, invalid satellite names, and keys the type does not read (with
the likely spelling for a typo). Required keys partly missing are a note. `$_{VAR}`
references are checked as written. Exit status 0 when clean, 1 with problems, 2 when the
file cannot be read. `kc705_registers` is an EtrocCounters key only; in another type's
section it is reported as unknown.

A required key in `[<Type>._default]` (or `[_default]`) is reported as "[EtrocTransmitter._default]
holds `polarity`, a required key of EtrocTransmitter: ..." and the exit status is 1, on
purpose: every satellite of the type then receives the key, so the initialize guard can no
longer tell that a satellite's own section is missing. Fix: move the required keys into
each `[<Type>.<name>]` section; shared non-required keys can stay in the default section.

`compressed_binary` and `save_html` from the 0.x files are not supported in 1.x: the
satellites never read them, so the preflight reports them and the operator drops the key.
Of the eleven 2025 controller files, ten set `compressed_binary` and fail the preflight
until it is removed.

Convert an old file:

```sh
python -m constellation.satellites.EtrocCommon.convert_toml old.toml > new.toml
```

It maps ETROC2Classic to EtrocTransmitter, ETROC2Receiver to EtrocReceiver,
ETROC2Waveform to EtrocWaveform and ETROC2Counters to EtrocCounters, and keeps keys
and comments. A parent table that holds keys becomes the matching default section
(`[satellites.ETROC2Classic]` -> `[EtrocTransmitter._default]`, a bare `[satellites]` ->
`[_default]`); a `[satellites...]` header it cannot map (quoted keys, `[[...]]`) is refused
with exit status 1. Run the preflight on the result.

After a `git pull` that adds Python files, re-run the meson editable install
(`pip install --no-build-isolation -e ".[cli,etroc]"` in the Constellation checkout, as above): the editable
loader imports only the files it was installed with, so a new module (e.g.
`EtrocCommon/lifecycle.py`) imports under pytest but not in the satellite process.

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
- `reconfigure` (ORBIT only) takes a dictionary of keys and applies them through the
  launch code: register keys are written and read back (the reply lists e.g.
  `polarity = 0x4123 (Reg14 read back 0x4123)`), a new `fast_command_memo` or
  `prescale_factor` reprograms the FC memory, and the next BOR's `kc705_readback` shows
  the new values. Keys that need a landing (connection, I2C and chip identity,
  `output_path`) are refused with "'key' cannot change in ORBIT; land and re-initialize to
  change it", and keys the initialize configuration did not hold, or of another type, are
  refused too; a refused reconfigure is a failed command and the satellite stays in
  ORBIT. Changeable keys:

  | Type | Keys |
  |---|---|
  | EtrocTransmitter | `active_channel`, `timestamp`, `triggerbit_delay`, `polarity`, `counter_duration`, `fc_delays`, `data_delays_01`, `data_delays_23`, `prescale_factor`, `fast_command_memo`, `num_fifo_read`, `clear_fifo`, `reset_counter`, `firmware` |
  | EtrocWaveform | the EtrocTransmitter keys, `ws_active_channel`, `active_boards` |
  | EtrocReceiver | `translate`, `skip_fillers`, `keep_time`, `flush_interval` (takes effect at the next start) |
  | EtrocCounters | `channel`, `fc_mode_on_launch`, `prescale_factor`, `check_layout`, `settle_windows`, `l1a_delay`, the `kc705_registers` section (keys present at initialize); refused while a scan runs |

- Ctrl-C stops the satellite in order. Before, the run thread kept running and the process
  hung at exit with the KC705 DAQ still started (a second Ctrl-C left it running). Now
  reentry stops the run thread (bounded wait), stops the CDTP thread without an EOR, and
  releases the hardware: EtrocTransmitter stops the DAQ, sets the FC to idle and closes
  the socket; EtrocWaveform also restores Reg15 and releases the USB-ISS; EtrocCounters
  ends the run files (`run.json` `ended_by = "reentry"`) and releases the board;
  EtrocReceiver closes the data file and writes the not-stopped-cleanly marker. Each step
  logs its own failure and the next one still runs; a second reentry does nothing.
  Every bounded wait is logged before it starts. A further Ctrl-C during the shutdown is
  logged ("Ctrl-C ignored: shutdown in progress, waiting up to N s for <step>; press 3
  times to abort the shutdown with the hardware as is (k/3)") and the shutdown continues.
  The third press is logged as an error and restores the default Ctrl-C action: a fourth
  ends the process at once and leaves the KC705 and I2C as they are (the way out of a long
  bounded wait, e.g. EtrocWaveform with four chips: up to 86 s). A Ctrl-C in the main
  loop's idle sleep, or one that arrives before the shutdown's Ctrl-C handler is in
  place, still runs the shutdown.
- The shutdown claims a hardware state only when the board answered a read after the
  writes: the stop pulse and FC idle go out first, then a fence of reads whose answers are
  known (2 s timeout each): Reg13 is written with status page 2 selected and read back,
  and status word 6 must read the page's fixed pattern 0x3C5C (word 5 must carry 0x0F17
  before its session_DAQ_on bit is trusted). Stray data on the stream, such as the late
  reply to a FIFO read the run thread left pending, fails the fence instead of passing as
  an answer. The log and the `fail_gracefully` status then say "DAQ stopped" / "fast
  commands idle"; otherwise "DAQ/FC state UNKNOWN: board did not answer", and failed steps
  are named.
  A run thread blocked on a stalled board gets its socket shut down and a second 3 s wait;
  the stop sequence then goes out on a fresh connection. If the thread still runs, no
  register traffic is sent and the state is reported UNKNOWN. EtrocWaveform sizes its run
  thread wait from the number of chips (one WS read each).
- `reconfigure` refuses framework sections (`_data`, `_role`, ...): they need a
  re-initialize. It also refuses, before leaving ORBIT, keys the type does not read (a
  typo such as `triggerbit_dleay`) and keys it reads only at initialize
  (`allow_default_config`); Constellation would otherwise reject them as unused inside the
  transition, in ERROR. The key sets are `satellite_types.known_keys`. Register values must be 0-0xFFFF and `num_fifo_read` 1-65536; a register
  that reads back different from what was written fails the transition (ERROR).
  `prescale_factor` reprograms the FC memory, but the SEU2025 firmware notes list the Reg10
  prescale bits as unused, so the change may have no effect on that firmware.
- `fail_gracefully()` (entering ERROR) runs the same release. For EtrocTransmitter and
  EtrocWaveform this is new: ERROR during a run now also stops the DAQ and idles the FC,
  not only closes the socket.
