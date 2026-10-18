"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocCounters: the KC705 per-channel counters ("FPGA scalars") as a Constellation satellite.

ORBIT: read the latched 1 s counters of one channel, switch channel and fast-command
pattern, and run threshold scans (trigger-bit noise peak, QInj S-curve edges) that
set the pixel DAC over I2C and read one settled window per point.
RUN: record every window of the selected channel to the run directory and publish the
rates as metrics.

The KC705 serves a single TCP client: EtrocTransmitter and EtrocWaveform must be landed
(or not running) while this satellite is launched.
"""

import csv
import json
import threading
import time
from pathlib import Path
from typing import Any

from constellation.core.commandmanager import cscp_requestable
from constellation.core.configuration import Configuration
from constellation.core.monitoring import schedule_metric
from constellation.core.protocol.cscp1 import SatelliteState
from constellation.core.satellite import Satellite

from ..EtrocCommon import ETROC_SATELLITES_VERSION, cmd_interpret, fast_command
from ..EtrocCommon.config import load_typed_config
from ..EtrocCommon.reconfigure import EtrocReconfigure
from ..EtrocCommon.satellite_types import REQUIRED_KEYS
from .chipio import POWER_MODES
from .counters import (
    FC_MODES,
    WINDOW_S,
    WRITABLE_REGISTERS,
    CounterReader,
    CounterTimeoutError,
    CounterWindow,
    KC705Link,
    refresh_delta,
)
from .scans import QINJ_L1A_SPACING_DEFAULT_DELAY, ScanAborted, ScanEngine, fpga_data_line, validate_scan_params

ORBIT = [SatelliteState.ORBIT]
ORBIT_RUN = [SatelliteState.ORBIT, SatelliteState.RUN]
PRESCALE_FACTORS = (2048, 4096, 8192, 16384)
METRIC_INTERVAL_S = 1.0
# metrics publish nothing when the cached window is older than this many windows
METRIC_MAX_AGE_WINDOWS = 3


class _StopFlag:
    """Adapter so CounterReader.wait_windows can poll Satellite.stop_requested()."""

    def __init__(self, satellite: "EtrocCounters"):
        self.satellite = satellite

    def is_set(self) -> bool:
        return self.satellite.stop_requested()


def _scan_params(params: dict | str) -> dict:
    """Scan parameters as a dict; a JSON string is accepted for callers that can only send strings."""
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as e:
            raise ValueError(f"scan parameters are not valid JSON: {e}") from e
    if not isinstance(params, dict):
        raise TypeError("scan parameters must be a dictionary or a JSON object string")
    return params


class EtrocCounters(EtrocReconfigure, Satellite, version=ETROC_SATELLITES_VERSION):
    """KC705 counter readout (data, header, trigger-bit, coincidence per 1 s window) and threshold scans."""

    REQUIRED_KEYS = REQUIRED_KEYS["EtrocCounters"]

    DEFAULT_CONFIG = {
        "hostname": "192.168.2.3",
        "port": 1024,
        "connect_timeout": 5.0,
        "socket_timeout": 5.0,
        "channel": 0,
        "prescale_factor": 2048,
        "fc_mode_on_launch": "idle",
        "check_layout": True,
        "output_path": "./",
        "settle_windows": 2,
        "firmware_window_s": 1.0,
        "chip_name": "ETROC2",
        "i2c_port": "",
        "chip_address": 0x60,
        "ws_address": 0,
        "chip_setup": "none",
        "power_mode": "high",
        "l1a_delay": QINJ_L1A_SPACING_DEFAULT_DELAY,
    }

    # Keys `reconfigure` may change in ORBIT (not while a scan runs). The connection, the I2C
    # adapter and chip, the output directory and firmware_window_s (reader and poller timing,
    # built at launch) need a landing.
    RECONFIGURABLE_KEYS = ("channel", "fc_mode_on_launch", "prescale_factor", "check_layout", "settle_windows", "l1a_delay")
    RECONFIGURABLE_SECTIONS = ("kc705_registers",)

    def __init__(self, *args: Any, **kwargs: Any):
        self._link: KC705Link | None = None
        self._reader: CounterReader | None = None
        self._chip = None
        self._engine: ScanEngine | None = None
        self._scan_thread: threading.Thread | None = None
        self._scan_result: dict[str, Any] | None = None
        self._fc_mode = "idle"
        self._settle_ref: int | None = None
        self._cache: CounterWindow | None = None
        self._cache_time = 0.0
        self._cache_lock = threading.Lock()
        self._poller: threading.Thread | None = None
        self._poller_stop = threading.Event()
        self._run_files: dict[str, Any] = {}
        self._run_info: dict[str, Any] = {}
        self._effective_config: dict[str, Any] = {}
        self.firmware_window_s = 1.0
        self.socket_timeout = 5.0
        self.settle_windows = 2
        super().__init__(*args, **kwargs)

    # ----------------------------------------------------------- FSM handlers
    def do_initializing(self, config: Configuration) -> str:
        if not self._stop_scan(join=True):
            raise RuntimeError("A scan thread is still running; cannot re-initialize")
        self._release()
        self._effective_config = load_typed_config(self, config, self.DEFAULT_CONFIG, extra_keys=("kc705_registers",))
        self._validate_settings(self._effective_config)
        self._warn_l1a_delay()

        self.kc705_registers: dict[str, int] = self._plan_sections(config).get("kc705_registers", {})
        self._effective_config["kc705_registers"] = dict(self.kc705_registers)
        return "Initialized"

    def _validate_settings(self, values: dict[str, Any]) -> None:
        """Value checks of the keys present (initialize: all; reconfigure: the changed ones)."""
        if "channel" in values and not 0 <= values["channel"] <= 3:
            raise ValueError(f"channel must be 0-3, got {values['channel']}")
        if "prescale_factor" in values and values["prescale_factor"] not in PRESCALE_FACTORS:
            raise ValueError(f"prescale_factor must be one of {PRESCALE_FACTORS}, got {values['prescale_factor']}")
        if "fc_mode_on_launch" in values:
            self._parse_fc_mode(values["fc_mode_on_launch"])
        if "settle_windows" in values and values["settle_windows"] < 2:
            raise ValueError("settle_windows must be >= 2: the first window after a change can be partial")
        if "chip_setup" in values and values["chip_setup"] not in ("none", "basic"):
            raise ValueError("chip_setup must be 'none' or 'basic'")
        if "power_mode" in values and values["power_mode"] not in POWER_MODES:
            raise ValueError(f"power_mode must be one of {POWER_MODES}, got '{values['power_mode']}'")
        if "firmware_window_s" in values and values["firmware_window_s"] <= 0:
            raise ValueError("firmware_window_s must be > 0")
        if values.get("connect_timeout", 1.0) <= 0 or values.get("socket_timeout", 1.0) <= 0:
            # without a timeout a dead link blocks a register read forever, and landing waits on it
            raise ValueError("connect_timeout and socket_timeout must be > 0")

    def _warn_l1a_delay(self) -> None:
        if self.l1a_delay != QINJ_L1A_SPACING_DEFAULT_DELAY:
            self.log.warning(
                f"l1a_delay 0x{self.l1a_delay:03x} differs from 0x{QINJ_L1A_SPACING_DEFAULT_DELAY:03x}, the value "
                f"used with the qinj_l1a pattern (QInj at BX 0x005, L1A at BX 0x1fd)"
            )

    def _plan_sections(self, config: Configuration) -> dict[str, Any]:
        """The kc705_registers section as {register: value}; only WRITABLE_REGISTERS (never Reg7)."""
        if not config.has("kc705_registers"):
            return {}
        registers: dict[str, int] = {}
        section = config.get_section("kc705_registers")
        for key in section.get_keys():
            if key not in WRITABLE_REGISTERS:
                raise ValueError(
                    f"kc705_registers.{key} is not allowed; EtrocCounters writes only {WRITABLE_REGISTERS} "
                    f"(never Reg7 / counter_duration)"
                )
            registers[key] = section.get_int(key)
        return {"kc705_registers": registers}

    def do_launching(self) -> str:
        self._link = KC705Link(self.hostname, self.port, self.connect_timeout, self.socket_timeout)
        self._link.open()
        self._reader = CounterReader(self._link, window_s=self.firmware_window_s, poll_s=min(0.1, self.firmware_window_s / 5))

        layout = self._reader.check_status_layout()
        if not layout["ok"]:
            msg = f"Status-word layout check failed: page 2 read {layout['words']}, expected {layout['expected']}"
            if self.check_layout:
                raise RuntimeError(msg + " (set check_layout = false to launch anyway)")
            self.log.warning(msg)
        else:
            self.log.info("Status-word layout check passed (page 2 fixed patterns)")

        self._write_kc705_registers(self.kc705_registers)

        self._reader.set_channel(self.channel)
        self._apply_fc_mode(self.fc_mode_on_launch)

        if self.i2c_port:
            from . import chipio

            self._chip = chipio.I2CGui2ChipIO(
                self.i2c_port, self.chip_address, self.ws_address or None, self.chip_name, logger=self.log
            )
            if self.chip_setup == "basic":
                self._chip.setup_chip(self.power_mode)
                self.log.info("Chip set up: PLL/FC calibration, peripherals, all pixels disabled")
        else:
            self.log.warning("i2c_port is empty: counters only, noise_scan/qinj_scurve are disabled")

        self._engine = None
        if self._chip is not None:
            self._engine = ScanEngine(
                reader=self._reader,
                chip=self._chip,
                set_fc_mode=self._apply_fc_mode,
                get_fc_mode=lambda: self._fc_mode,
                output_path=Path(self.output_path),
                chip_name=self.chip_name,
                chip_address=self.chip_address,
                log=self.log,
                settle_windows=self.settle_windows,
                l1a_delay=self.l1a_delay,
                on_window=self._store_window,
            )
        self._start_poller()
        return f"Launched - KC705 {self.hostname}:{self.port}, channel {self.channel}, FC {self._fc_mode}"

    def do_landing(self) -> str:
        if not self._stop_scan(join=True):
            # keep link and chip so the scan thread can still restore the pixel and the FC mode
            raise RuntimeError("Scan thread did not stop; KC705 and I2C left open for its register restore")
        self._idle_fc_quietly("landing")
        self._release()
        return "Landed - fast commands idle, KC705 connection closed"

    def do_starting(self, run_identifier: str) -> str:
        if self._busy():
            raise RuntimeError("A scan is running; stop_scan (or wait for it) before starting a run")
        run_dir = Path(self.output_path) / run_identifier
        run_dir.mkdir(parents=True, exist_ok=True)
        csv_file = open(run_dir / "counters_run.csv", "w", newline="")
        writer = csv.DictWriter(csv_file, fieldnames=_RUN_COLUMNS)
        writer.writeheader()
        self._run_files = {
            "dir": run_dir,
            "csv_file": csv_file,
            "csv": writer,
            "dat": open(run_dir / "FPGA_Data.dat", "w"),
            "windows": 0,
            "stalled": 0,
            "missed": 0,
        }
        self._run_info = {
            "run_identifier": run_identifier,
            "satellite": self.name,
            "version": ETROC_SATELLITES_VERSION,
            "start_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "channel": self._reader.get_channel(),
            "fc_mode": self._fc_mode,
            "window_s": WINDOW_S,
            "configuration": self._effective_config,
        }
        self._write_run_json()
        return f"Run {run_identifier} started, recording channel {self._run_info['channel']}"

    def do_run(self) -> str:
        stop = _StopFlag(self)
        last_refresh: int | None = None
        while not self.stop_requested():
            try:
                window = self._reader.wait_windows(1, stop, start_refresh=last_refresh)
            except InterruptedError:
                break
            except CounterTimeoutError as e:
                # a stalled or re-aligning link is recorded, not fatal; the wait then starts afresh
                self.log.warning(f"Counter window stalled: {e}")
                self._record_stall()
                last_refresh = None
                continue
            missed = 0
            if last_refresh is not None:
                delta = refresh_delta(window.refresh, last_refresh)
                missed = delta - 1 if delta < 0x8000 else 0
            last_refresh = window.refresh
            self._store_window(window)
            self._record_run_window(window, missed)
        return f"Recorded {self._run_files.get('windows', 0)} windows"

    def do_stopping(self) -> str:
        n = self._run_files.get("windows", 0)
        if self._run_info:
            self._run_info.update(
                stop_time=time.strftime("%Y-%m-%dT%H:%M:%S"),
                windows=n,
                stalled=self._run_files.get("stalled", 0),
                missed_windows=self._run_files.get("missed", 0),
            )
            self._write_run_json()
        self._close_run_files()
        self._run_info = {}
        return f"Run stopped after {n} windows"

    def fail_gracefully(self) -> str:
        stopped = self._stop_scan(join=True)
        if self._run_info:
            self._run_info.update(stop_time=time.strftime("%Y-%m-%dT%H:%M:%S"), ended_by="failure")
            self._write_run_json()
        self._close_run_files()
        self._run_info = {}
        if not stopped:
            return "Failed; scan thread still running, KC705 and I2C left open for its register restore"
        self._idle_fc_quietly("failure")
        self._release()
        return "Failed gracefully, fast commands idle, KC705 connection closed"

    # ------------------------------------------------------------ reconfigure
    def _check_reconfigure_allowed(self) -> None:
        self._refuse_while_scanning()

    def _apply_reconfigure(self, plan: dict[str, Any]) -> list[str]:
        changes = []
        for key, value in plan.items():
            if key != "kc705_registers":
                setattr(self, key, value)
                self._effective_config[key] = value
        if "kc705_registers" in plan:
            self.kc705_registers.update(plan["kc705_registers"])
            self._effective_config["kc705_registers"] = dict(self.kc705_registers)
            self._write_kc705_registers(plan["kc705_registers"])
            changes += [f"kc705_registers.{k} = 0x{v:04x}" for k, v in plan["kc705_registers"].items()]
        if "channel" in plan:
            self._reader.set_channel(self.channel)
            self._mark_change()
            changes.append(f"channel = {self.channel}")
        if "fc_mode_on_launch" in plan or "prescale_factor" in plan:
            # a new prescale factor reaches the board with the next FC memory programming
            mode = plan.get("fc_mode_on_launch", self._fc_mode)
            self._apply_fc_mode(mode)
            changes.append(f"FC {self._fc_mode} (prescale {self.prescale_factor})")
        if self._engine is not None:
            self._engine.settle_windows = self.settle_windows
            self._engine.l1a_delay = self.l1a_delay
        if "l1a_delay" in plan:
            self._warn_l1a_delay()
        handled = {"kc705_registers", "channel", "fc_mode_on_launch", "prescale_factor"}
        changes += [f"{k} = {v!r}" for k, v in plan.items() if k not in handled]
        return changes

    # --------------------------------------------------------------- commands
    @cscp_requestable(unpack_list=False)
    def start(self, run_identifier: str) -> tuple[str, Any, dict[str, Any]]:
        """Start a run (refused while a scan is running)."""
        if self._busy():
            raise RuntimeError("A scan is running; stop_scan (or wait for it) before starting a run")
        return super().start(run_identifier)

    @cscp_requestable(ORBIT_RUN)
    def read_counters(self) -> tuple[str, Any, dict]:
        """Last completed 1 s window of the selected channel: refresh, data, header, trigbit, combine, LED bits.

        'settled' is false until settle_windows windows have completed after the last channel/FC change.
        """
        window = self._current_window(read_if_stale=not self._busy() and self.fsm.state == SatelliteState.ORBIT)
        if window is None:
            return "No window available yet", None, {}
        d = window.to_dict()
        d["fc_mode"] = self._fc_mode
        d["settled"] = self._is_settled(window)
        return f"refresh {window.refresh}: header {window.header}, data {window.data}, trigbit {window.trigbit}", d, {}

    @cscp_requestable(ORBIT)
    def set_channel(self, channel: int) -> tuple[str, Any, dict]:
        """Show the counters of channel 0-3 (Reg13 LED_Pages = channel, status page 0). Payload: [channel]."""
        self._refuse_while_scanning()
        self._reader.set_channel(channel)
        self.channel = channel
        self._mark_change()
        return f"Channel {channel} selected", self._reader.get_channel(), {}

    @cscp_requestable(ORBIT)
    def set_fc_mode(self, mode: str) -> tuple[str, Any, dict]:
        """Fast commands: ["idle"], ["qinj_l1a"] (QInj + L1A once per orbit) or ["custom <memo>"] (with Start and Triggerbit)."""
        self._refuse_while_scanning()
        self._apply_fc_mode(mode)
        return f"Fast commands: {self._fc_mode}", self._fc_mode, {}

    @cscp_requestable(ORBIT)
    def noise_scan(self, params: dict | str) -> tuple[str, Any, dict]:
        """Trigger-bit noise scan in the background. Payload: [{"pixels": [[row, col]], ...}] or [JSON string]."""
        return self._start_scan("noise_scan", _scan_params(params))

    @cscp_requestable(ORBIT)
    def qinj_scurve(self, params: dict | str) -> tuple[str, Any, dict]:
        """QInj S-curve edges in the background. Payload: [{"pixels": [[row, col]], "qsels": [27]}] or [JSON string]."""
        return self._start_scan("qinj_scurve", _scan_params(params))

    @cscp_requestable()
    def stop_scan(self) -> tuple[str, Any, dict]:
        """Stop a running scan after the current point; pixel registers and FC pattern are restored."""
        if not self._busy():
            return "No scan running", None, {}
        self._stop_scan(join=False)
        return "Stop requested", None, {}

    @cscp_requestable()
    def get_scan_status(self) -> tuple[str, Any, dict]:
        """State of the current or last scan: running/finished/aborted/failed, points measured, last point."""
        status = dict(self._engine.status) if self._engine is not None else {"state": "idle"}
        return status.get("state", "idle"), status, {}

    @cscp_requestable()
    def get_scan_result(self) -> tuple[str, Any, dict]:
        """Per-pixel results and file locations of the last finished scan."""
        if self._scan_result is None:
            return "No scan result", None, {}
        return self._scan_result.get("status", "?"), self._scan_result, {}

    @cscp_requestable(ORBIT)
    def check_status_layout(self) -> tuple[str, Any, dict]:
        """Read the fixed patterns of status page 2 to confirm the status-word addressing."""
        self._refuse_while_scanning()
        result = self._reader.check_status_layout()
        return ("Layout OK" if result["ok"] else "Layout MISMATCH"), result, {}

    # ---------------------------------------------------------------- metrics
    # Metrics only read the cached window; the ORBIT poller, the scans and do_run fill it.
    @schedule_metric("", METRIC_INTERVAL_S, ORBIT_RUN)
    def REFRESH(self) -> int | None:
        """refreshCounter of the selected channel (completed 1 s windows since the link aligned)"""
        w = self._metric_window()
        return None if w is None else w.refresh

    @schedule_metric("Hz", METRIC_INTERVAL_S, ORBIT_RUN)
    def DATA_RATE(self) -> float | None:
        """Data words (hits) per second of the selected channel"""
        w = self._metric_window()
        return None if w is None else w.data / WINDOW_S

    @schedule_metric("Hz", METRIC_INTERVAL_S, ORBIT_RUN)
    def HEADER_RATE(self) -> float | None:
        """Frame headers (L1As answered) per second of the selected channel"""
        w = self._metric_window()
        return None if w is None else w.header / WINDOW_S

    @schedule_metric("Hz", METRIC_INTERVAL_S, ORBIT_RUN)
    def TRIGBIT_RATE(self) -> float | None:
        """Trigger bits per second of the selected channel"""
        w = self._metric_window()
        return None if w is None else w.trigbit / WINDOW_S

    @schedule_metric("Hz", METRIC_INTERVAL_S, ORBIT_RUN)
    def COINCIDENCE_RATE(self) -> float | None:
        """BXs per second with trigger bits on all enabled channels (combineTriggerCount); latched on its own 1 s tick, not in step with the per-channel counters"""
        w = self._metric_window()
        return None if w is None else w.combine / WINDOW_S

    @schedule_metric("", METRIC_INTERVAL_S, ORBIT_RUN)
    def CHANNEL(self) -> int | None:
        """Channel whose counters are shown"""
        w = self._metric_window()
        return None if w is None else w.channel

    # ---------------------------------------------------------------- helpers
    def _parse_fc_mode(self, mode: str) -> str:
        """Return the FC memo for a mode name; raises ValueError for anything else."""
        mode = mode.strip()
        if mode in FC_MODES:
            return FC_MODES[mode]
        if mode.startswith("custom "):
            memo = mode[len("custom "):].strip()
            fast_command.parse_memo(memo)
            words = memo.split()
            missing = [w for w in ("Start", "Triggerbit") if w not in words]
            if missing:
                raise ValueError(
                    f"custom memo '{memo}' lacks {missing}: without 'Start' the FC playback is not started, "
                    f"without 'Triggerbit' the chip sends no trigger bits and trigbit stays 0"
                )
            return memo
        raise ValueError(f"FC mode must be one of {list(FC_MODES)} or 'custom <memo words>', got '{mode}'")

    def _apply_fc_mode(self, mode: str) -> None:
        memo = self._parse_fc_mode(mode)
        self._link.configure_fc(memo, self.prescale_factor)
        self._fc_mode = mode.strip()
        self._mark_change()
        self.log.info(f"Fast commands set to '{self._fc_mode}' ({memo})")

    def _idle_fc_quietly(self, reason: str) -> None:
        if self._link is not None and self._link.is_open:
            try:
                self._apply_fc_mode("idle")
            except Exception as e:
                self.log.warning(f"Could not set FC to idle on {reason}: {e}")

    def _mark_change(self) -> None:
        """Remember the refresh value at a channel/FC change; windows count as settled settle_windows later."""
        try:
            self._settle_ref = self._reader.read_refresh()
        except Exception:
            self._settle_ref = None
        with self._cache_lock:
            self._cache = None

    def _is_settled(self, window: CounterWindow) -> bool:
        if self._settle_ref is None:
            return True
        delta = refresh_delta(window.refresh, self._settle_ref)
        if delta >= 0x8000:
            # refresh restarted (counter reset or link re-alignment): count from the new value
            self._settle_ref = window.refresh
            return False
        return delta >= self.settle_windows

    def _busy(self) -> bool:
        return self._scan_thread is not None and self._scan_thread.is_alive()

    def _refuse_while_scanning(self) -> None:
        if self._busy():
            raise RuntimeError("A scan is running; stop_scan first")

    def _store_window(self, window: CounterWindow) -> None:
        with self._cache_lock:
            self._cache = window
            self._cache_time = time.monotonic()

    def _current_window(self, read_if_stale: bool) -> CounterWindow | None:
        """The cached window; re-read from the board when stale and nothing else owns the readout."""
        with self._cache_lock:
            fresh = self._cache is not None and time.monotonic() - self._cache_time < self.firmware_window_s
            cached = self._cache
        if fresh or not read_if_stale or self._reader is None:
            return cached
        window = self._reader.read_window()
        self._store_window(window)
        return window

    def _metric_window(self) -> CounterWindow | None:
        max_age = METRIC_MAX_AGE_WINDOWS * self.firmware_window_s + METRIC_INTERVAL_S
        with self._cache_lock:
            if self._cache is None or time.monotonic() - self._cache_time > max_age:
                return None
            return self._cache

    def _start_poller(self) -> None:
        self._poller_stop.clear()
        self._poller = threading.Thread(target=self._poll_loop, name="counter-poller", daemon=True)
        self._poller.start()

    def _stop_poller(self) -> None:
        self._poller_stop.set()
        poller = self._poller
        if poller is not None and poller is not threading.current_thread():
            poller.join(timeout=self.socket_timeout + 2.0)
        self._poller = None

    def _poll_loop(self) -> None:
        """Refresh the cached window once per period in ORBIT while no scan owns the readout."""
        period = min(self.firmware_window_s, METRIC_INTERVAL_S)
        warned = False
        while not self._poller_stop.wait(period):
            reader = self._reader
            if self.fsm.state != SatelliteState.ORBIT or self._busy() or reader is None:
                continue
            try:
                self._store_window(reader.read_window())
                warned = False
            except Exception as e:
                if not warned:
                    self.log.warning(f"Counter poll failed: {e}")
                    warned = True

    def _start_scan(self, kind: str, params: dict) -> tuple[str, Any, dict]:
        if self._engine is None:
            raise RuntimeError("Scans need the chip: set i2c_port (and chip_address) in the configuration")
        self._refuse_while_scanning()
        validate_scan_params(kind, params)
        self._scan_result = None
        engine = self._engine
        engine.stop_event.clear()
        engine.status = {"state": "starting", "kind": kind}
        self._scan_thread = threading.Thread(
            target=self._scan_worker, args=(engine, kind, dict(params)), name=kind, daemon=True
        )
        self._scan_thread.start()
        return f"{kind} started; poll get_scan_status / get_scan_result", None, {}

    def _scan_worker(self, engine: ScanEngine, kind: str, params: dict) -> None:
        try:
            run = engine.noise_scan if kind == "noise_scan" else engine.qinj_scurve
            record = run(params)
            self._scan_result = _summary(record)
            self.log.info(f"{kind} finished: {record['scan_dir']}")
        except ScanAborted as e:
            self.log.warning(f"{kind} aborted: {e}")
            self._scan_result = {"status": "aborted", "error": str(e), **engine.status}
        except Exception as e:
            self.log.error(f"{kind} failed: {type(e).__name__}: {e}")
            self._scan_result = {"status": "failed", "error": f"{type(e).__name__}: {e}", **engine.status}

    def _stop_scan(self, join: bool) -> bool:
        """Ask a running scan to stop; with join, wait for it. Returns False while the thread is alive."""
        if self._engine is not None:
            self._engine.stop_event.set()
        thread = self._scan_thread
        if thread is None or thread is threading.current_thread():
            return True
        if join:
            # one point takes at most (settle_windows + 2) windows + 1 s, plus the register restore
            thread.join(timeout=(self.settle_windows + 3) * self.firmware_window_s + 30.0)
            if thread.is_alive():
                self.log.critical("Scan thread did not stop; pixel registers may not be restored yet")
                return False
            self._scan_thread = None
        return not thread.is_alive()

    def _record_run_window(self, window: CounterWindow, missed: int) -> None:
        files = self._run_files
        if not files:
            return
        files["dat"].write(fpga_data_line(window, -1))
        files["dat"].flush()
        row = {k: v for k, v in window.to_dict().items() if k in _RUN_COLUMNS}
        row.update(fc_mode=self._fc_mode, window_s=WINDOW_S, stalled=0, missed_windows=missed)
        files["csv"].writerow(row)
        files["csv_file"].flush()
        files["windows"] += 1
        files["missed"] += missed

    def _record_stall(self) -> None:
        files = self._run_files
        if not files:
            return
        files["csv"].writerow({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "unix_time": f"{time.time():.3f}",
            "fc_mode": self._fc_mode, "window_s": WINDOW_S, "stalled": 1,
        })
        files["csv_file"].flush()
        files["stalled"] += 1

    def _write_run_json(self) -> None:
        run_dir = self._run_files.get("dir")
        if run_dir is None:
            return
        with open(Path(run_dir) / "run.json", "w") as f:
            json.dump(self._run_info, f, indent=2, default=str)

    def _close_run_files(self) -> None:
        for key in ("csv_file", "dat"):
            f = self._run_files.get(key)
            if f is not None:
                f.close()
        self._run_files = {}

    def _write_kc705_registers(self, registers: dict[str, int]) -> None:
        for name, value in registers.items():
            self._link.write_named_config(name, value)
            self.log.info(f"KC705 {name} (Reg{cmd_interpret.CONFIG_REGISTERS[name]}) = 0x{value:04x}")

    def _release(self) -> None:
        """Close chip and link; never while a scan thread may still use them."""
        if self._busy():
            self.log.critical("Not releasing KC705/I2C: the scan thread is still running")
            return
        self._stop_poller()
        if self._chip is not None:
            self._chip.close()
            self._chip = None
        if self._link is not None:
            self._link.close()
        self._link = None
        self._reader = None
        self._engine = None
        with self._cache_lock:
            self._cache = None


_RUN_COLUMNS = [
    "timestamp", "unix_time", "channel", "fc_mode", "refresh", "en_l1a", "window_s",
    "data", "header", "trigbit", "combine", "led8", "ws_ready", "missed_windows", "stalled",
]


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    """Scan record without the per-point lists, small enough for a CSCP reply."""
    out = {k: v for k, v in record.items() if k != "results"}
    out["results"] = [{k: v for k, v in r.items() if k != "points"} for r in record.get("results", [])]
    return json.loads(json.dumps(out, default=str))
