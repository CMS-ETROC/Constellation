---
# SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
# SPDX-License-Identifier: CC-BY-4.0
title: "EtrocCounters"
description: "KC705 per-channel counters (FPGA scalars) and fast threshold scans for ETROC2"
category: "Readout Systems"
---

## Description

EtrocCounters reads the per-channel counters that the KC705 firmware latches every
second and uses them for fast threshold scans of single ETROC2 pixels: the trigger-bit
noise peak around the auto-calibrated baseline, and the QInj S-curve turn-on / turn-off
edges. It replaces the "FPGA scalar" workflow of the 2023-24 DAQ
(`daq_helpers.py` `Save_FPGA_data` in ETROC-DAQ and `scripts/TIDboard_noisePeak.py` in
i2c_gui) and writes the same 7-column `FPGA_Data.dat` files, so the existing analysis
notebooks read its output unchanged.

It does not read the data FIFO. It needs the KC705 TCP link, and the ETROC2 I2C link
(USB-ISS) for the scans.

### What the firmware counts

ETROC2TestFirmware, SEU2025 build (d1248d5), as read from the source:

| Quantity | Source | Meaning |
|---|---|---|
| window | `src/ETROCBoard.v`, `timerClocks` 40,000,000, compare-equal | 40,000,001 clk40 cycles = 1 s, fixed (no register sets it) |
| `refresh` | `refreshCounter[15:0]`, per channel | completed windows since the counters were last reset |
| `data` | `dataCounter[31:0]` | data words (hits) received in the last window |
| `header` | `headerCounter[31:0]` | frame headers (one per L1A answered) in the last window |
| `trigbit` | `trigCounter[31:0]` | trigger bits received in the last window |
| `combine` | `combineTriggerCount[15:0]` (`gtx_receiver.v`) | BXs with trigger bits on all enabled channels |
| LED8 | `LED8Bit` | `{monitorSel[1:0], isDataEx, isHeadEx, trigSynched, trigError, synched, dataError}`; bits 7, 5, 3, 1 flash on an alignment error |

- Status page 0 (`kc705_mig.v`, `assign status_reg`) is
  `{combine16, trig32, refresh16, header32, data32, 8'd0, LED8, 8'd0, 8'd0, 7'd0, ws_ready}`,
  zero-extended to 176 bits. Status word *n* (address 0-10) is `status_reg[16n+15:16n]`
  (`src/control_interface.vhd`, STATUS_REG read). EtrocWaveform reads `ws_ready` as bit 0
  of word 0 and EtrocTransmitter reads `{0x0F17, session_DAQ_on, hist_fill_done}` as word 5
  of page 2, both consistent with this convention.
- Config Reg13 `[1:0]` selects the status page, `[4:2]` is `LED_Pages`; the channel whose
  counters are shown is `monitorSel = LED_Pages[1:0]`. Each channel has its own counters,
  including its own `refresh`.
- All counters, `refresh` included, are held at zero while the channel link is not aligned
  (`RC_aligned & trigSynched`) and are reset by the `clear_fifo` pulse (`fifo_reset` feeds the
  global reset, which also stops the fast-command playback). EtrocCounters never sends
  `clear_fifo`.
- Config Reg7 is the FC delay line and trigger-bit delays on SEU2025, not a counter
  window. EtrocCounters never writes it (the old DAQ wrote `counter_duration` there).
- Status page 2 carries fixed patterns (words 6-10 = 0x3C5C, word 5 >> 2 = 0x0F17).
  EtrocCounters reads them at launch to check the word addressing on the real board.

The satellite waits for at least two `refresh` increments after every DAC or
fast-command change (`settle_windows`, minimum 2): the first window after a change is
partial, the second one started after it.

### Single TCP client

The KC705 TCP server is built with `NTCPSTREAMS = 1`
(`src/gig_eth/tcp_server/com5402pkg.vhd`) and accepts a SYN only in its LISTEN state
(`tcp_server.vhd`). While one program holds the connection, a second client gets no
answer: its connect hangs rather than being refused (inferred from the source, not yet
seen on the board). EtrocCounters connects with a 5 s `connect_timeout` and then fails
with a message naming the single-client constraint.

Before launching EtrocCounters, land EtrocTransmitter / EtrocWaveform and close any
notebook or script that holds the KC705 socket. The same holds for the USB-ISS I2C
adapter: only one program can open it.

## Installation on the DAQ machine (etroc3)

Constellation 1.x needs Python >= 3.11. etroc3 has pyenv Python 3.13.1, which is fine,
but its site-packages hold the 0.7 install that the running satellites use. Install
1.x into a separate venv so the 0.7 setup stays usable, and do not mix 0.7 and 1.x
satellites or controllers in one group.

```sh
cd /home/daq/ETROC2
git clone -b ETROC2v3 <Constellation repository> Constellation-ETROC2v3
/home/daq/.pyenv/versions/3.13.1/bin/python -m venv venv-cnstln1
source venv-cnstln1/bin/activate
pip install meson-python meson ninja
cd Constellation-ETROC2v3
pip install --no-build-isolation -e ".[cli,etroc]"
SatelliteEtrocCounters --help
```

`.[etroc]` pulls numpy, pandas, tqdm and I2C-GUI2 (needed for the scans). With the
editable install keep the venv's `bin` on `PATH` (activated venv); the package
rebuilds itself through ninja on import.

## Launching

```sh
SatelliteEtrocCounters -g wh -n One
Controller -g wh -n One -c example_counters.toml      # or MissionControl
```

From the controller console:

```python
constellation.initialize(cfg)
constellation.launch()
constellation.EtrocCounters.One.read_counters()
constellation.EtrocCounters.One.set_fc_mode(["qinj_l1a"])
constellation.EtrocCounters.One.noise_scan([{"pixels": [[0, 14]], "rel_min": -10, "rel_max": 20}])
constellation.EtrocCounters.One.get_scan_status()
constellation.EtrocCounters.One.get_scan_result()
```

A command payload is a list with one element per argument (`set_channel([1])`); a bare
value is refused. Scan parameters can also be a JSON string, which is what MissionControl's
command field passes on: `noise_scan(['{"pixels": [[0, 14]]}'])`.

Scans run in a background thread (the controller times out a command after 5 s);
poll `get_scan_status` and fetch `get_scan_result` at the end. `start` is refused while a
scan runs.

## Commands

| Command | Payload | States |
|---|---|---|
| `read_counters` | none | ORBIT, RUN |
| `set_channel` | `[channel]`, 0-3 | ORBIT |
| `set_fc_mode` | `"idle"` (IDLE only), `"qinj_l1a"` (QInj at BX 5 and L1A at BX 509 of every orbit, about 11,223 per second) or `"custom <memo>"` (EtrocTransmitter memo words, plus `l1a_offset=K`; must contain `Start` and `Triggerbit`, the latter optional only with `allow_unpaired_memo = true`) | ORBIT |
| `noise_scan` | dict or JSON string, see below | ORBIT |
| `qinj_scurve` | dict or JSON string, see below | ORBIT |
| `acc_scan` | dict or JSON string, see below | ORBIT |
| `stop_scan` | none | any |
| `get_scan_status`, `get_scan_result` | none | any |
| `check_status_layout` | none | ORBIT |

`read_counters` returns the last completed window (refresh, data, header, trigbit,
combine, channel, en_L1A, LED bits, the raw status words) plus `settled`, which is false
until `settle_windows` windows have completed after the last channel, FC or KC705
register change (also a `reconfigure` of `kc705_registers`); if the refresh counter could
not be read at the change, the count starts at the next window read.
`set_channel` and `set_fc_mode` are refused while a scan runs.

`noise_scan` (FC set to idle for the scan): `pixels` `[[row, col], ...]` (required);
`rel_min` (-10), `rel_max` (20), `step` (1) around the auto-calibrated baseline, or
`dac_min` / `dac_max` absolute (no auto-calibration); `data_readout` (false); `tag` ("").
The pixel is set to `disDataReadout=1, QInjEn=0, disTrigPath=0, Bypass_THCal=1,
enable_TDC=1`, all TDC windows open.

`qinj_scurve` (auto-calibration of all pixels first with FC idle, then FC set to qinj_l1a
for the scan): `pixels` (required); `qsels` ([27]);
`method` "binary" (default) or "sweep"; `turn_on` / `turn_off` (true); `turnoff_counter`
"data" (default) or "trigbit"; `turnoff_dac_max` (1020); for "sweep" `dac_min` /
`dac_max` / `step` (BL-10 .. BL+150, 2); `tag`. The pixel is set to `disDataReadout=0,
QInjEn=1, QSel, L1Adelay = l1a_delay, Bypass_THCal=1, enable_TDC=1`. With an `l1a_delay`
other than 0x1F5 the log warns: the qinj_l1a pattern puts the L1A 504 BX after the QInj.
Optional, for the measurement plan items A2, A3 and A5 (`notes/measurement-plan-2026-10.md`
of the ETROC-DAQ workspace):
- `fc_mode` (`"qinj_l1a"`): the FC pattern while charge is injected, any mode `set_fc_mode`
  accepts that contains `QInj` and `L1A` (checked before the scan starts), e.g.
  `"custom Start QInj L1A Triggerbit multiple=64"` (64 QInj + L1A pairs per orbit, the A2 rate
  ladder) or `"custom Start QInj L1A Triggerbit l1a_offset=-3"` (every L1A 3 BX earlier, 501 BX
  after its QInj: the A3 reverse scan at fixed L1Adelay). The auto-calibration still runs with
  FC idle; the previous pattern is restored at the end, also on errors.
- `together` (false; sweep with absolute `dac_min` / `dac_max` only): all listed pixels
  injected and read out at once, every one at the same DAC of the sweep; one result for the
  group with `hits_per_l1a_per_pixel` = data / header / pixels per point (counters.csv rows
  carry `row` / `col` as `r1;r2;...`).
- `trig_path` (true; false = `disTrigPath=1`, sweep only): the pixel's hits are read out with
  its trigger path off (A5: headers and data, no trigger bits). The pixel registers are
  restored at the end as for every scan, so no pixel is left injecting with its trigger path off.

- Turn-on: bisection on [0, BL+3NW] for the lowest DAC with trigger bits > 0.
- Turn-off: bisection on [BL, turnoff_dac_max] for the highest DAC whose counter is at
  least half of the header count of the first point (the L1As per window); the scan
  stops with an error if that header count is 0.
- Both searches re-measure the result DAC last, so the last line of `FPGA_Data.dat` holds it.

`acc_scan` (measurement A1 of the ETROC-DAQ measurement plan, `notes/measurement-plan-2026-10.md`:
the pixel's own discriminator duty cycle against the trigger-bit and data counters):
`pixels` `[[row, col], ...]` (required; scanned one at a time, all other pixels stay as
they are, i.e. disabled after the chip setup); `qsel` (null: noise, `QInjEn=0`; an
integer: `QInjEn=1`, `QSel`, `L1Adelay = l1a_delay`); `rel_min` (-16) .. `rel_max` (24)
around `center`, or `dac_min` / `dac_max` absolute; `fine_step` (1) within `center` ±
`fine_range` (8) and `coarse_step` (4) outside it, both end points included; `center`
(default: the auto-calibrated BL for noise, and with `qsel` the data-counter turn-off found
by the qinj_scurve bisection on [BL, 1020]); `acc_reads` (3); `windows` (3); `together`
(false; true adds a pass with all listed pixels enabled at once, each at its own centre +
offset, for the OR term in the trigger bit); `tag` ("", appended to the scan type). Every
pixel is auto-calibrated first with FC idle. The pixel is set to `disDataReadout=0,
disTrigPath=0, Bypass_THCal=1, CLKEn_THCal=1, BufEn_THCal=1, enable_TDC=1`, all TDC
windows open. Per DAC: write the DAC; pulse `RSTn_THCal`; rising edge of
`ScanStart_THCal`; wait 2 ms (the accumulator window is 32768 clocks, 0.8192 ms, ETROC2
manual §3.1.3); read `ScanDone` and `ACC` `acc_reads` times (a point where `ScanDone` is not
1 is flagged, the scan goes on); then, for noise, `windows` settled windows with FC idle
(C2), FC qinj_l1a and `windows` settled windows (C3), FC idle again. With `qsel` the FC
stays qinj_l1a for the whole scan, because the charge has to be injected while ACC and the
trigger bits count, and the same windows give C2 and C3. About 8 s per point for noise
with the defaults (2 x 3 windows of 1 s plus the settling after the DAC and FC changes),
about 4 s with `qsel`; the default grid has 23 points.

Every scan saves the Pixel Config fields it touches (DAC, QInjEn, QSel, L1Adelay,
disDataReadout, disTrigPath, Bypass_THCal, enable_TDC, CLKEn_THCal, BufEn_THCal,
RSTn_THCal, ScanStart_THCal, TH_offset and the TDC windows) before the auto-calibration and writes them back afterwards, also when the
scan fails or is stopped; the previous FC mode is restored as well. Every I2C write is
read back.

`charge_fC` is written as `QSel + 1`; the QSel register value is stored next to it and
is what the `QInj_<N>` directory names carry. Confirm the QSel-to-charge relation
against the ETROC2 manual before quoting charges.

## Outputs

Under `<output_path>/<YYYY-MM-DD>_Array_Test_Results/`:

- one directory per pixel (and charge), named as the 2023-24 scripts did:
  `<chip>_VRef_SCurve_NoiseOnly_Pixel_C<c>_R<r>`,
  `<chip>_VRef_SCurve_BinarySearch_TurnOn_Pixel_C<c>_R<r>`,
  `<chip>_VRef_SCurve_BinarySearch_TurnOff_Pixel_C<c>_R<r>_QInj_<QSel>`,
  `<chip>_VRef_SCurve_Sweep_Pixel_C<c>_R<r>_QInj_<QSel>`; an existing name gets `_r2`, `_r3`.
  Each holds `FPGA_Data.dat`, one line per point in the order measured:
  `refresh,en_L1A,window_s,data,header,trigbit,DAC` (en_L1A = Reg8 bit 10, window_s = 1);
- `<chip>_<ScanType>_<HHMMSS>_scan/counters.csv`: every window with scan id, time,
  phase, chip, channel, row, col, QSel, charge, DAC, BL, NW, FC mode and all counters;
- `<chip>_<ScanType>_<HHMMSS>_scan/scan.json`: parameters, per-pixel results, status
  (finished / aborted / failed) and the error.
- `acc_scan` writes no `FPGA_Data.dat`, only the scan directory
  (`<chip>_ACCScan_Noise_<HHMMSS>_scan` or `<chip>_ACCScan_QInj_<QSel>_<HHMMSS>_scan`).
  `counters.csv` has one row per window, `phase` = `c2_idle`, `c3_qinj_l1a` (noise),
  `c2c3_qinj_l1a` (qsel) or `center_search` (the turn-off bisection; its 7-column lines are
  also in `center_search_R<r>_C<c>_QInj_<QSel>.dat`); rows of the `together` pass carry
  `row`, `col`, `dac`, BL and NW as `;`-joined lists. `scan.json` holds per pixel the
  auto-cal `baseline` and `noise_width`, `center`, and per point `dac`, `acc` (every read),
  `scan_done`, `c1` (mean ACC / 32768), `c2` (trigger bits per window), `c2_duty` (mean
  trigger bits / 40,000,001 clocks), `c3` (`data`, `header` per window), `c3_eff` (sum data /
  sum header) and `flag`. Derived: `acc_bl`, `trig_bl`, `data_bl` (DAC where C1, C2, C3
  cross 0.5, linear interpolation, the crossing nearest `center`), `acc_nw`, `trig_nw`,
  `data_nw` (half the DAC distance between the 0.16 and 0.84 crossings),
  `acc_nw_transition` (DAC codes with 0 < ACC < 32768, the manual's NW definition),
  `acc_bl_vs_cal` = `acc_bl` - BL and, for noise, `positive_control` (pass when within
  1 LSB: A1 check (a)). With `qsel` each curve is divided by its maximum (`*_plateau`)
  before the crossings, since C1 and C2 only reach the pulse duty cycle. The `together`
  entry lists per point the DACs, ACCs and C1 of every pixel and `c1_or_indep` = 1 -
  prod(1 - C1).

In RUN the satellite records every window of the selected channel to
`<output_path>/<run_id>/counters_run.csv` and `FPGA_Data.dat` (DAC column -1). A
`missed_windows` column counts windows skipped between two reads; when no new window
arrives within three windows plus 1 s (link lost, counters held at 0) a row
with `stalled = 1` is written and the run continues. `run.json` in the same directory holds
the run identifier, start/stop time, channel, FC mode, the effective configuration and the
window/stall counts. EtrocCounters sends no data records, so it has no BOR/EOR.

Metrics (1 s, ORBIT and RUN): `REFRESH`, `DATA_RATE`, `HEADER_RATE`, `TRIGBIT_RATE`,
`CHANNEL` show the last completed window; `COINCIDENCE_RATE` is the firmware's combined
trigger count, which ticks on its own timer and is not aligned with `refresh`. Metrics
never talk to the board: in ORBIT a poller thread reads one window per second into a
cache (paused during scans), in RUN the run loop fills it; a metric is skipped when the
cache is older than three windows.

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `hostname`, `port` | `192.168.2.3`, `1024` | KC705 (IP 192.168.2.<DIP switch>) |
| `connect_timeout`, `socket_timeout` | `5.0`, `5.0` | seconds, both > 0 |
| `channel` | `0` | channel shown at launch |
| `fc_mode_on_launch` | `"idle"` | FC pattern programmed at launch |
| `prescale_factor` | `2048` | Reg10 prescale for the FC memory writes |
| `check_layout` | `true` | refuse to launch if the page-2 pattern check fails |
| `kc705_registers` | none | optional section writing fc_delays, data_delays_01/23, triggerbit_delay, polarity, active_channel (Reg7 is refused) |
| `output_path` | `./` | output directory |
| `settle_windows` | `2` | windows to wait after a DAC/FC change (>= 2) |
| `i2c_port` | `""` | USB-ISS port, e.g. `/dev/ttyACM0`; empty = counters only |
| `chip_address`, `ws_address`, `chip_name` | `0x60`, `0`, `"ETROC2"` | the scanned chip |
| `chip_setup` | `"none"` | `"basic"` = PLL/FC calibration, peripherals, all pixels off, PLL/FC again |
| `power_mode` | `"high"` | for `chip_setup = "basic"`: `"high"`, `"010"`, `"101"` or `"low"` |
| `l1a_delay` | `0x1F5` | pixel L1Adelay for QInj scans (the 2023-24 value 501) |
| `allow_unpaired_memo` | `false` | accept a custom memo without `Triggerbit` (with a warning): the firmware's trigDataSize then no longer matches the chip's trigger bits (R-KC7-SW-09). Only for measuring that signature (plan A5); withdrawing it by `reconfigure` idles such a pattern |
| `firmware_window_s` | `1.0` | wait timing only (tests use a shorter simulated window); programs nothing |

An empty configuration is refused, and so is one that holds none of `output_path`,
`channel` and `fc_mode_on_launch` (it came from `[_default]` alone): the TOML section must
be `[EtrocCounters.<name>]` (Constellation 1.x), see `example_counters.toml`, and the
error names the expected section. `python -m constellation.satellites.EtrocCommon.check_toml
file.toml` checks a file before it is loaded (see `EtrocCommon/README.md`).

In ORBIT, `reconfigure` changes `channel`, `fc_mode_on_launch`, `prescale_factor`,
`check_layout`, `settle_windows`, `l1a_delay`, `allow_unpaired_memo` and the `kc705_registers` keys set at
initialize, without a landing (refused while a scan runs). Other keys are refused with
"land and re-initialize to change it" and the satellite stays in ORBIT. On Ctrl-C the
satellite stops the run or scan (waiting for the pixel-register restore; a further Ctrl-C
there is logged and ignored), writes `ended_by = "reentry"` into `run.json`, sets the FC to
idle, reads back known values (Reg13 with status page 2 selected, and the page's fixed
patterns 0x3C5C / 0x0F17) and releases the KC705 and the USB-ISS. It reports "fast
commands idle" only when those reads answered as expected, otherwise "FC state UNKNOWN";
ERROR (`fail_gracefully`) and landing do the same. If the counter poller does not stop
(blocked on a stalled board, holding the link), no register traffic is sent: only I2C is
released and the state is reported UNKNOWN.

## First test on hardware (checklist)

1. Land EtrocTransmitter / EtrocWaveform; nothing else holds the KC705 socket or the USB-ISS.
2. Launch. The log must say "Status-word layout check passed". If it fails, stop:
   the word addressing differs from the source and every counter would be misread.
3. `read_counters()` twice, about 2 s apart: `refresh` grows by about 1 per second.
   If it stays at 0, the channel link is not aligned (check `led_bits`: synched, trig_synched).
4. `set_fc_mode(["qinj_l1a"])`, wait until `settled`: `header` about 11,223 per window on an
   aligned channel with the chip configured. `set_fc_mode(["idle"])`: `header` 0.
5. `set_channel([1])` on an unused channel: `refresh` 0 and `led_bits.monitor_sel` = 1.
   Back to the chip's channel.
6. With I2C: `noise_scan` on one known-good pixel; the peak DAC should be within NW of BL.
7. `qinj_scurve` on the same pixel with QSel 27 (and `"method": "sweep"` once to
   cross-check the bisection), compared with the 2023-24 numbers for that chip type.
8. After each scan, check that the pixel registers are back (`get_scan_result`, log lines
   "Restored pixel ...").
9. A short run (`start`, 30 s, `stop`): `counters_run.csv` has one row per second,
   `missed_windows` 0, no `stalled` rows; `run.json` has the stop time.

## Tests

Offline, no hardware: a simulated KC705 (same TCP word protocol as `cmd_interpret`,
SEU2025 status layout built from the firmware concatenation) and a simulated pixel matrix.

```sh
pytest -o consider_namespace_packages=true python/constellation/satellites/EtrocCounters/tests
```

Run it with the venv's `pytest` from the repository root (`python -m pytest` puts the
root on `sys.path` and gives the test package a different name).
