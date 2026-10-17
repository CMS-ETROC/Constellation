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
from .counters import (
    FC_MODES,
    WINDOW_S,
    WRITABLE_REGISTERS,
    CounterReader,
    CounterWindow,
    KC705Link,
    refresh_delta,
)
from .scans import ScanAborted, ScanEngine, fpga_data_line, validate_scan_params

ORBIT = [SatelliteState.ORBIT]
ORBIT_RUN = [SatelliteState.ORBIT, SatelliteState.RUN]
PRESCALE_FACTORS = (2048, 4096, 8192, 16384)
METRIC_INTERVAL_S = 1.0
# windows older than this are re-read for metrics/read_counters in ORBIT
CACHE_MAX_AGE_S = 1.0


class _StopFlag:
    """Adapter so CounterReader.wait_windows can poll Satellite.stop_requested()."""

    def __init__(self, satellite: "EtrocCounters"):
        self.satellite = satellite

    def is_set(self) -> bool:
        return self.satellite.stop_requested()


class EtrocCounters(Satellite, version=ETROC_SATELLITES_VERSION):
    """KC705 counter readout (data, header, trigger-bit, coincidence per 1 s window) and threshold scans."""

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
        "l1a_delay": 0x1F5,
    }

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
        self._run_files: dict[str, Any] = {}
        super().__init__(*args, **kwargs)

    # ----------------------------------------------------------- FSM handlers
    def do_initializing(self, config: Configuration) -> str:
        self._stop_scan(join=True)
        self._release()
        load_typed_config(self, config, self.DEFAULT_CONFIG, extra_keys=("kc705_registers",))

        if not 0 <= self.channel <= 3:
            raise ValueError(f"channel must be 0-3, got {self.channel}")
        if self.prescale_factor not in PRESCALE_FACTORS:
            raise ValueError(f"prescale_factor must be one of {PRESCALE_FACTORS}, got {self.prescale_factor}")
        self._parse_fc_mode(self.fc_mode_on_launch)
        if self.settle_windows < 2:
            raise ValueError("settle_windows must be >= 2: the first window after a change can be partial")
        if self.chip_setup not in ("none", "basic"):
            raise ValueError("chip_setup must be 'none' or 'basic'")
        if self.firmware_window_s <= 0:
            raise ValueError("firmware_window_s must be > 0")

        self.kc705_registers: dict[str, int] = {}
        if config.has("kc705_registers"):
            section = config.get_section("kc705_registers")
            for key in section.get_keys():
                if key not in WRITABLE_REGISTERS:
                    raise ValueError(
                        f"kc705_registers.{key} is not allowed; EtrocCounters writes only {WRITABLE_REGISTERS} "
                        f"(never Reg7 / counter_duration)"
                    )
                self.kc705_registers[key] = section.get_int(key)
        return "Initialized"

    def do_launching(self) -> str:
        self._link = KC705Link(self.hostname, self.port, self.connect_timeout, self.socket_timeout or None)
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

        for name, value in self.kc705_registers.items():
            self._link.write_named_config(name, value)
            self.log.info(f"KC705 {name} (Reg{cmd_interpret.CONFIG_REGISTERS[name]}) = 0x{value:04x}")

        self._reader.set_channel(self.channel)
        self._apply_fc_mode(self.fc_mode_on_launch)

        if self.i2c_port:
            from .chipio import I2CGui2ChipIO

            self._chip = I2CGui2ChipIO(self.i2c_port, self.chip_address, self.ws_address or None, self.chip_name)
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
        return f"Launched - KC705 {self.hostname}:{self.port}, channel {self.channel}, FC {self._fc_mode}"

    def do_landing(self) -> str:
        self._stop_scan(join=True)
        if self._link is not None and self._link.is_open:
            try:
                self._apply_fc_mode("idle")
            except Exception as e:
                self.log.warning(f"Could not set FC to idle while landing: {e}")
        self._release()
        return "Landed - fast commands idle, KC705 connection closed"

    def do_starting(self, run_identifier: str) -> str:
        self._stop_scan(join=True)
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
        }
        self.bor = {"channel": self.channel, "fc_mode": self._fc_mode, "window_s": WINDOW_S}
        return f"Run {run_identifier} started, recording channel {self._reader.get_channel()}"

    def do_run(self) -> str:
        stop = _StopFlag(self)
        while not self.stop_requested():
            try:
                window = self._reader.wait_windows(1, stop)
            except InterruptedError:
                break
            self._store_window(window)
            self._record_run_window(window)
        return f"Recorded {self._run_files.get('windows', 0)} windows"

    def do_stopping(self) -> str:
        n = self._run_files.get("windows", 0)
        self._close_run_files()
        self.eor = {"windows": n}
        return f"Run stopped after {n} windows"

    def fail_gracefully(self) -> str:
        self._stop_scan(join=False)
        self._close_run_files()
        self._release()
        return "Failed gracefully, KC705 connection closed"

    # --------------------------------------------------------------- commands
    @cscp_requestable(ORBIT_RUN)
    def read_counters(self) -> tuple[str, Any, dict]:
        """Last completed 1 s window of the selected channel: refresh, data, header, trigbit, combine, LED bits.

        'settled' is false until settle_windows windows have completed after the last channel/FC change.
        """
        window = self._current_window(force=not self._busy())
        if window is None:
            return "No window available yet", None, {}
        d = window.to_dict()
        d["fc_mode"] = self._fc_mode
        d["settled"] = self._is_settled(window)
        return f"refresh {window.refresh}: header {window.header}, data {window.data}, trigbit {window.trigbit}", d, {}

    @cscp_requestable(ORBIT, unpack_list=False)
    def set_channel(self, channel: int) -> tuple[str, Any, dict]:
        """Show the counters of channel 0-3 (Reg13 LED_Pages = channel, status page 0)."""
        self._refuse_while_scanning()
        self._reader.set_channel(channel)
        self.channel = channel
        self._mark_change()
        return f"Channel {channel} selected", self._reader.get_channel(), {}

    @cscp_requestable(ORBIT, unpack_list=False)
    def set_fc_mode(self, mode: str) -> tuple[str, Any, dict]:
        """Fast-command pattern: "idle" (IDLE only), "qinj_l1a" (QInj + L1A once per orbit) or "custom <memo>"."""
        self._refuse_while_scanning()
        self._apply_fc_mode(mode)
        return f"Fast commands: {self._fc_mode}", self._fc_mode, {}

    @cscp_requestable(ORBIT, unpack_list=False)
    def noise_scan(self, params: dict) -> tuple[str, Any, dict]:
        """Trigger-bit noise scan in the background. Payload: {"pixels": [[row, col], ...], "rel_min": -10, "rel_max": 20, ...}."""
        return self._start_scan("noise_scan", params)

    @cscp_requestable(ORBIT, unpack_list=False)
    def qinj_scurve(self, params: dict) -> tuple[str, Any, dict]:
        """QInj S-curve edges in the background. Payload: {"pixels": [[row, col]], "qsels": [27], "method": "binary"}."""
        return self._start_scan("qinj_scurve", params)

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
        """BXs per second with trigger bits on all enabled channels (combineTriggerCount)"""
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
            return memo
        raise ValueError(f"FC mode must be one of {list(FC_MODES)} or 'custom <memo words>', got '{mode}'")

    def _apply_fc_mode(self, mode: str) -> None:
        memo = self._parse_fc_mode(mode)
        self._link.configure_fc(memo, self.prescale_factor)
        self._fc_mode = mode.strip()
        self._mark_change()
        self.log.info(f"Fast commands set to '{self._fc_mode}' ({memo})")

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
        return self.settle_windows <= delta < 0x8000

    def _busy(self) -> bool:
        return self._scan_thread is not None and self._scan_thread.is_alive()

    def _refuse_while_scanning(self) -> None:
        if self._busy():
            raise RuntimeError("A scan is running; stop_scan first")

    def _store_window(self, window: CounterWindow) -> None:
        with self._cache_lock:
            self._cache = window
            self._cache_time = time.monotonic()

    def _current_window(self, force: bool) -> CounterWindow | None:
        """The cached window, re-read from the board when stale and nothing else owns the readout."""
        with self._cache_lock:
            fresh = self._cache is not None and time.monotonic() - self._cache_time < CACHE_MAX_AGE_S
            cached = self._cache
        if fresh or not force or self._reader is None:
            return cached
        window = self._reader.read_window()
        self._store_window(window)
        return window

    def _metric_window(self) -> CounterWindow | None:
        # scans and RUN refresh the cache themselves; in plain ORBIT the metric reads the board
        idle_orbit = self.fsm.state == SatelliteState.ORBIT and not self._busy()
        try:
            return self._current_window(force=idle_orbit)
        except Exception as e:
            self.log.warning(f"Counter read for metrics failed: {e}")
            return None

    def _start_scan(self, kind: str, params: dict) -> tuple[str, Any, dict]:
        if self._engine is None:
            raise RuntimeError("Scans need the chip: set i2c_port (and chip_address) in the configuration")
        self._refuse_while_scanning()
        if not isinstance(params, dict):
            raise TypeError("scan parameters must be a dictionary")
        validate_scan_params(kind, params)
        self._scan_result = None
        self._engine.stop_event.clear()
        self._engine.status = {"state": "starting", "kind": kind}
        self._scan_thread = threading.Thread(target=self._scan_worker, args=(kind, dict(params)), name=kind, daemon=True)
        self._scan_thread.start()
        return f"{kind} started; poll get_scan_status / get_scan_result", None, {}

    def _scan_worker(self, kind: str, params: dict) -> None:
        try:
            run = self._engine.noise_scan if kind == "noise_scan" else self._engine.qinj_scurve
            record = run(params)
            self._scan_result = _summary(record)
            self.log.info(f"{kind} finished: {record['scan_dir']}")
        except ScanAborted as e:
            self.log.warning(f"{kind} aborted: {e}")
            self._scan_result = {"status": "aborted", "error": str(e), **self._engine.status}
        except Exception as e:
            self.log.error(f"{kind} failed: {type(e).__name__}: {e}")
            self._scan_result = {"status": "failed", "error": f"{type(e).__name__}: {e}", **self._engine.status}

    def _stop_scan(self, join: bool) -> None:
        if self._engine is not None:
            self._engine.stop_event.set()
        thread = self._scan_thread
        if join and thread is not None and thread is not threading.current_thread():
            # one point takes at most (settle_windows + 2) windows + 1 s, plus the restore
            thread.join(timeout=(self.settle_windows + 3) * self.firmware_window_s + 30.0)
            if thread.is_alive():
                self.log.critical("Scan thread did not stop; pixel registers may not be restored")
        if join:
            self._scan_thread = None

    def _record_run_window(self, window: CounterWindow) -> None:
        files = self._run_files
        if not files:
            return
        files["dat"].write(fpga_data_line(window, -1))
        files["dat"].flush()
        row = {k: v for k, v in window.to_dict().items() if k in _RUN_COLUMNS}
        row["fc_mode"] = self._fc_mode
        row["window_s"] = WINDOW_S
        files["csv"].writerow(row)
        files["csv_file"].flush()
        files["windows"] += 1

    def _close_run_files(self) -> None:
        for key in ("csv_file", "dat"):
            f = self._run_files.get(key)
            if f is not None:
                f.close()
        self._run_files = {}

    def _release(self) -> None:
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
    "data", "header", "trigbit", "combine", "led8", "ws_ready",
]


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    """Scan record without the per-point lists, small enough for a CSCP reply."""
    out = {k: v for k, v in record.items() if k != "results"}
    out["results"] = [{k: v for k, v in r.items() if k != "points"} for r in record.get("results", [])]
    return json.loads(json.dumps(out, default=str))
