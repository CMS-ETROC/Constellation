"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from constellation.core.commandmanager import cscp_requestable
from constellation.core.configuration import Configuration
from constellation.core.protocol.cscp1 import SatelliteState
from constellation.core.satellite import Satellite

from ..EtrocCommon import ETROC_SATELLITES_VERSION, cmd_interpret, fast_command
from ..EtrocCommon.commands import KC705RegisterCommands
from ..EtrocCommon.config import load_typed_config
from ..EtrocCommon.lifecycle import EtrocLifecycle, run_step
from ..EtrocCommon.reconfigure import EtrocReconfigure
from ..EtrocCommon.satellite_types import REQUIRED_KEYS

_WAVEFORM_MEMO_WORDS = {"QInj", "L1A", "L1ARange", "BCR", "Triggerbit", "Start", "uniform"}
_REPEATED_RE = re.compile(r"^repeatedQInj(=\d+)?$")

# FC memo grammars. "fixed" (R-KC7-SW-20): repeatedQInj=N gives N injections, every address is checked
# against the orbit. "legacy": the ETROC2v2 arithmetic, kept to record the old behaviour on the bench
# (N+1 injections, uniform interval 0 above N = 187, addresses beyond the orbit never played).
FC_GRAMMARS = ("fixed", "legacy")
QINJ_BASE_BX = 0x005
L1A_BASE_BX = 0x1FD
REPEAT_STEP_BX = 0x010
UNIFORM_SPAN_STEPS = 3000 // 16
ORBIT_LAST_BX = fast_command.ORBIT_REG11
# FC memory address field: Reg10[11:0] / Reg9[11:0]
FC_ADDRESS_MASK = 0xFFF


def parse_waveform_memo(memo: str) -> dict[str, Any]:
    """Words of an EtrocWaveform memo as flags, plus the repeat count (None when no repeatedQInj word)."""
    words = memo.split()
    for word in words:
        if word not in _WAVEFORM_MEMO_WORDS and not _REPEATED_RE.match(word):
            raise ValueError(
                f"Unknown word '{word}' in fast_command_memo '{memo}'. "
                f"Allowed: {sorted(_WAVEFORM_MEMO_WORDS)} and repeatedQInj=N"
            )
    flags: dict[str, Any] = {w: w in words for w in _WAVEFORM_MEMO_WORDS}
    flags["repeatedQInj"] = any(_REPEATED_RE.match(w) for w in words)
    flags["repeat"] = None
    for word in words:
        if word.startswith("repeatedQInj="):
            flags["repeat"] = int(word.split("=", 1)[1])
            break
    return flags


def _legacy_addresses(base: int, loops: int, uniform: bool, do_loop: bool) -> list[int]:
    """The addresses ETROC2v2's _execute_fc_command writes: the base, then `loops` more if do_loop."""
    out = [base]
    if do_loop and loops > 0:
        interval = UNIFORM_SPAN_STEPS // loops if uniform else 1
        for i in range(1, loops + 1):
            out.append(base + interval * i * REPEAT_STEP_BX)
    return out


def fc_schedule(memo: str, grammar: str = "fixed", spacing_bx: int = REPEAT_STEP_BX) -> dict[str, Any]:
    """The FC memory writes a memo makes, without hardware.

    spacing_bx (the qinj_spacing_bx key, default 16): BX between successive injections of repeatedQInj=N (and
    their L1As with L1ARange) in the fixed grammar without 'uniform'. The legacy grammar and 'uniform' keep
    their own spacing; a spacing other than 16 is refused with the legacy grammar.

    Returns {"grammar", "memo", "start", "triggerbit", "bcr", "qinj_bx", "l1a_bx" (addresses written,
    in write order), "played_qinj_bx", "played_l1a_bx" (distinct addresses inside the orbit after the
    12-bit address field, i.e. the commands the firmware plays once per orbit)}.
    The fixed grammar raises ValueError for a repeat count < 1 or a schedule outside the orbit.
    """
    if grammar not in FC_GRAMMARS:
        raise ValueError(f"fc_grammar must be one of {FC_GRAMMARS}, got {grammar!r}")
    f = parse_waveform_memo(memo)
    if not 1 <= spacing_bx <= ORBIT_LAST_BX:
        raise ValueError(f"qinj_spacing_bx must be 1-{ORBIT_LAST_BX} BX, got {spacing_bx}")
    if grammar == "legacy" and spacing_bx != REPEAT_STEP_BX:
        raise ValueError(f"qinj_spacing_bx = {spacing_bx} needs the fixed grammar (legacy places injections 16 BX apart)")
    if grammar == "legacy":
        loops = f["repeat"] if f["repeat"] is not None else 1
        qinj = _legacy_addresses(QINJ_BASE_BX, loops, f["uniform"], f["repeatedQInj"]) if f["QInj"] else []
        l1a = _legacy_addresses(L1A_BASE_BX, loops, f["uniform"], f["L1ARange"]) if f["L1A"] else []
    else:
        n = f["repeat"] if f["repeat"] is not None else 1
        if n < 1:
            raise ValueError(f"repeatedQInj={n}: the repeat count must be at least 1")
        step = spacing_bx
        if f["uniform"]:
            interval = UNIFORM_SPAN_STEPS // n
            if interval < 1:
                raise ValueError(
                    f"repeatedQInj={n} with 'uniform': at most {UNIFORM_SPAN_STEPS} injections fit the "
                    f"uniform span of {UNIFORM_SPAN_STEPS} x 16 BX"
                )
            step = interval * REPEAT_STEP_BX
        repeats = n if f["repeatedQInj"] else 1
        qinj = [QINJ_BASE_BX + k * step for k in range(repeats)] if f["QInj"] else []
        l1a_repeats = repeats if f["L1ARange"] else 1
        l1a = [L1A_BASE_BX + k * step for k in range(l1a_repeats)] if f["L1A"] else []
        for name, addresses in (("QInj", qinj), ("L1A", l1a)):
            if addresses and addresses[-1] > ORBIT_LAST_BX:
                raise ValueError(
                    f"memo '{memo}': the last {name} lands at BX {addresses[-1]}, beyond the orbit "
                    f"(last BX {ORBIT_LAST_BX}); use fewer repeats or 'uniform'"
                )

    def played(addresses: list[int]) -> list[int]:
        return sorted({a & FC_ADDRESS_MASK for a in addresses if (a & FC_ADDRESS_MASK) <= ORBIT_LAST_BX})

    return {
        "grammar": grammar, "memo": memo, "start": f["Start"], "triggerbit": f["Triggerbit"], "bcr": f["BCR"],
        "spacing_bx": spacing_bx if grammar == "fixed" and not f["uniform"] else None, "qinj_bx": qinj, "l1a_bx": l1a, "played_qinj_bx": played(qinj), "played_l1a_bx": played(l1a),
    }


# do_run polls the WS-ready bit for up to ws_ready_timeout s (default) before an event is skipped
WS_READY_POLL_S = 5.0
# pause between two WS-ready reads (R-KC7-SW-21: the ETROC2v2 loop polled without a pause)
WS_POLL_INTERVAL_S = 0.01
# The pixel the WS samples: ETROC2 manual sec. 6.1, "pixel 224"; PixelID = Col[3:0],Row[3:0] (PixSta0), so
# 224 = 0xE0 = column 14, row 0. ws_pixel moves the injected pixel (the sampler stays on (0, 14)).
WS_PIXEL = [0, 14]
# Keys that rewrite the pixel configuration over I2C (launch, or reconfigure in ORBIT)
PIXEL_KEYS = ("ws_pixel", "qsel", "qinj_en", "offset", "trigger_pixels", "rfsel")
# Pixel Config fields read back from the chip when pixel_readback is on (run start and run stop)
READBACK_FIELDS = ("QInjEn", "QSel", "RFSel", "DAC", "TH_offset", "disDataReadout", "disTrigPath", "enable_TDC")


def pixel_pairs(flat: list[int]) -> list[tuple[int, int]]:
    """[r0, c0, r1, c1, ...] -> [(r0, c0), (r1, c1), ...] (TOML arrays of arrays do not survive the config)."""
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


# batch_ws_read_data: 1024 x (I2C write + read) per chip. About 20 s per chip per event according to
# notes/review-sw-kc705-constellation-i2cgui-waferprobe.md; not timed on the bench yet.
WS_READ_S_PER_CHIP = 20.0


class EtrocWaveform(KC705RegisterCommands, EtrocReconfigure, EtrocLifecycle, Satellite, version=ETROC_SATELLITES_VERSION):
    """Waveform-sampler readout: configures the ETROC2 over I2C and saves one CSV per trigger."""

    REQUIRED_KEYS = REQUIRED_KEYS["EtrocWaveform"]

    DEFAULT_CONFIG = {
        "hostname": "192.168.2.3",
        "port": 1024,
        "firmware": "0001",
        "polarity": 0x4023,
        "timestamp": 0x0000,
        "active_channel": 0x0001,
        "prescale_factor": 2048,
        "counter_duration": 0x0000,
        "triggerbit_delay": 0x1800,
        "fc_delays": 0x0000,
        "data_delays_01": 0x0000,
        "data_delays_23": 0x0000,
        "num_fifo_read": 65536,
        "clear_fifo": 1,
        "reset_counter": 1,
        "fast_command_memo": "Start Triggerbit",
        "active_boards": 1,
        "i2c_port": "/dev/ttyACM0",
        "chip_addresses": [0x60],
        "ws_addresses": [0x40],
        "chip_names": ["typeD_board"],
        "offset": 10,
        # Pixel configured at launch (and on reconfigure): [row, col], its QSel and QInjEn. The defaults are
        # what the launch hard-coded up to ETROC2v3 daafea22: (0, 14), QSel 0x1e, QInjEn 1, DAC = BL + offset.
        "ws_pixel": list(WS_PIXEL),
        "qsel": 0x1E,
        "qinj_en": True,
        # Extra pixels injected at qsel (flat [row, col, row, col, ...]): a trigger-bit source that does not
        # touch the WS pixel, for a qinj_en = false control with the same timing. Empty = none.
        "trigger_pixels": [],
        # Preamp feedback resistor RFSel of the configured pixels (0 = highest gain); -1 = not written
        "rfsel": -1,
        # Waveform-sampler mode bits written by ws_start_sampling (regOut1F[7], [6]; manual sec. 6.5):
        # ws_sel1 1 = VGA amplifier (0 = bypass), ws_sel2 1 = power on 400 ns after ws_start (0 = until ws_stop)
        "ws_sel1": 0,
        "ws_sel2": 0,
        # BX between the injections of repeatedQInj=N (fixed grammar, not 'uniform'); 16 = as before
        "qinj_spacing_bx": REPEAT_STEP_BX,
        # read the Pixel Config of ws_pixel and trigger_pixels back from the chip at each run start (before the
        # first capture) and run stop (after the last); recorded in ws_run_summary.json. Off = as before
        "pixel_readback": False,
        "ws_active_channel": 0x0091,
        "output_path": "./",
        "connect_timeout": 5.0,
        "socket_timeout": 10.0,
        # FC memo grammar, see FC_GRAMMARS
        "fc_grammar": "fixed",
        # stop capturing after this many events per run (0 = no limit); the run then idles until stop
        "max_events": 0,
        # seconds to wait for the WS-ready bit before an event is skipped
        "ws_ready_timeout": WS_READY_POLL_S,
    }

    # Keys `reconfigure` may change in ORBIT. The connection, the I2C adapter, the chip list and the output
    # directory need a landing. PIXEL_KEYS rerun the launch's pixel sequence over the open adapter (all off,
    # auto-calibration, enable); ws_sel1 / ws_sel2 take effect at the next ws_start_sampling (run start, event).
    RECONFIGURABLE_KEYS = (
        *KC705RegisterCommands.LAUNCH_REGISTERS,
        "prescale_factor", "fast_command_memo", "ws_active_channel",
        "num_fifo_read", "clear_fifo", "reset_counter", "firmware", "active_boards",
        "fc_grammar", "max_events", "ws_ready_timeout",
        *PIXEL_KEYS, "ws_sel1", "ws_sel2", "qinj_spacing_bx", "pixel_readback",
    )

    WS_READY_TIMEOUT_MAX_S = 60.0

    def _send_fc_sequence(self, reg12_val: int, reg10_val: int, reg9_val: int) -> None:
        """Fill FC memory addresses [reg10_val, reg9_val] with the command in reg12_val."""
        fast_command.send_fc_sequence(self.connection_socket, reg12_val, reg10_val, reg9_val, self.prescale_factor)

    def _check_memo(self, memo: str) -> None:
        """Validate a memo against the EtrocWaveform grammar in use (fc_grammar)."""
        spacing = getattr(self, "_spacing_for_check", None) or getattr(self, "qinj_spacing_bx", REPEAT_STEP_BX)
        fc_schedule(memo, getattr(self, "_grammar_for_check", None) or getattr(self, "fc_grammar", "fixed"), spacing)

    def configure_memo_FC(self, memo=None) -> None:
        """Program the FC memory from a memo in the fc_grammar in use, then start the playback."""
        memo_str = memo if memo is not None else self.fast_command_memo
        grammar = getattr(self, "fc_grammar", "fixed")
        sched = fc_schedule(memo_str, grammar, getattr(self, "qinj_spacing_bx", REPEAT_STEP_BX))
        base_reg12 = fast_command.BASE_REG12_TRIGGERBIT if sched["triggerbit"] else fast_command.BASE_REG12_NO_TRIGGERBIT
        if sched["qinj_bx"]:
            self.log.info(
                f"FC memo '{memo_str}' ({grammar} grammar): {len(sched['qinj_bx'])} QInj written, "
                f"{len(sched['played_qinj_bx'])} played per orbit; {len(sched['played_l1a_bx'])} L1A played"
            )

        if sched["start"]:
            cmd_interpret.write_config_reg_decoded(self.connection_socket, "register_11", ORBIT_LAST_BX)
            time.sleep(0.01)

        # IDLE over the whole orbit, then single commands on top
        self._send_fc_sequence(base_reg12 + fast_command.IDLE_CMD, 0x000, ORBIT_LAST_BX)
        if sched["bcr"]:
            self._send_fc_sequence(base_reg12 + fast_command.BCR_CMD, 0x000, 0x000)
        for bx in sched["qinj_bx"]:
            self._send_fc_sequence(base_reg12 + fast_command.QINJ_CMD, bx, bx)
        for bx in sched["l1a_bx"]:
            self._send_fc_sequence(base_reg12 + fast_command.L1A_CMD, bx, bx)

        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "fc_signal_start")
        time.sleep(0.01)
        self._fc_schedule = sched

    # ------------------------------------------------------------------ commands
    @cscp_requestable()
    def get_fc_schedule(self) -> tuple[str, Any, dict]:
        """The FC memory writes of the configured memo and grammar (computed, no hardware access)."""
        sched = fc_schedule(self.fast_command_memo, self.fc_grammar, self.qinj_spacing_bx)
        return (
            f"{len(sched['played_qinj_bx'])} QInj, {len(sched['played_l1a_bx'])} L1A per orbit ({self.fc_grammar})",
            sched, {},
        )

    @cscp_requestable([SatelliteState.ORBIT])
    def read_fpga_counters(self) -> tuple[str, Any, dict]:
        """The KC705 counters of the next complete 1 s window (header = L1As answered, data, trigger bits).

        Waits for two refresh increments (about 2-3 s, at most 5 s). Reg13 (status page / LED page)
        is restored to the configured value afterwards.
        """
        from ..EtrocCounters.counters import CounterReader, KC705Link

        link = KC705Link(self.hostname, self.port)
        link.sock = self.connection_socket
        try:
            window = CounterReader(link).wait_windows(2)
        except Exception:
            # a reply that arrives after a timeout would answer the next command on this shared stream
            self._discard_pending_replies()
            raise
        finally:
            cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        d = window.to_dict()
        return f"header {d['header']}, data {d['data']}, trigbit {d['trigbit']} (refresh {d['refresh']})", d, {}

    def _discard_pending_replies(self, quiet_s: float = 0.5) -> int:
        """Read and drop whatever the board still sends, until it is quiet for quiet_s; returns the bytes dropped."""
        sock = self.connection_socket
        old = sock.gettimeout()
        dropped = 0
        try:
            sock.settimeout(quiet_s)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                dropped += len(chunk)
        except (TimeoutError, OSError):
            pass
        finally:
            sock.settimeout(old)
        if dropped:
            self.log.warning(f"Dropped {dropped} bytes of late replies after a failed counter read")
        return dropped

    @cscp_requestable()
    def get_ws_status(self) -> tuple[str, Any, dict]:
        """Events captured and WS-ready timeouts of the current (or last) run."""
        status = self._ws_summary()
        return f"{status['events']} event(s), {status['ws_timeouts']} WS-ready timeout(s)", status, {}

    def do_initializing(self, config: Configuration) -> str:
        """Configure the Satellite and any associated hardware."""

        self._close_connection()
        self._close_i2c()
        self._ws_run_active = False
        effective = load_typed_config(self, config, self.DEFAULT_CONFIG)
        self._validate_settings(effective)

        self.log.info("Configuration loaded and Defaults set")
        return "Initialized - Configuration loaded and Defaults set"

    def do_launching(self) -> str:
        # The I2C stack (i2c_gui2, pandas, tqdm) is only needed from here on
        from ..EtrocCommon.new_wrapper_i2cGui2 import i2c_connection

        self._close_i2c()
        self.i2c_conn = i2c_connection(
            self.i2c_port, self.chip_addresses, self.ws_addresses, self.chip_names, logger=self.log
        )
        self.i2c_conn.batch_PLL_FC_calibration()
        self.i2c_conn.batch_set_chip_peripherals()
        self._configure_pixels()

        self._close_connection()
        self.connection_socket = cmd_interpret.open_socket(
            self.hostname, self.port, self.connect_timeout, self.socket_timeout or None
        )

        self._write_registers(self.LAUNCH_REGISTERS)
        self._write_prescale()

        if self.clear_fifo:
            self.log.info("Clearing FIFO...")
            cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "clear_fifo")
            time.sleep(2.1)

        self.configure_memo_FC()
        self.log.info("Socket connected, FPGA Registers and Fast Command configured")
        return "Launched - Socket connected, FPGA Registers and Fast Command configured"

    def _configure_pixels(self) -> str:
        """All pixels off, auto-calibrate the configured pixels, enable them at DAC = BL + offset.

        The WS pixel gets qsel / qinj_en; trigger_pixels get qsel with QInjEn 1. Returns a one-line summary.
        """
        ws = (self.ws_pixel[0], self.ws_pixel[1])
        trig = pixel_pairs(self.trigger_pixels)
        rfsel = None if self.rfsel < 0 else self.rfsel
        self.i2c_conn.batch_disable_all_chips(power_mode='high')
        self.i2c_conn.batch_auto_calibrate_chip(pixel_list=[ws, *trig])
        self.i2c_conn.batch_enable_pixels(
            pixel_list=[ws], Qsel=self.qsel, QInjEn=self.qinj_en, offset=self.offset, rfsel=rfsel
        )
        if trig:
            self.i2c_conn.batch_enable_pixels(pixel_list=trig, Qsel=self.qsel, QInjEn=True, offset=self.offset, rfsel=rfsel)
        self._pixel_state = {
            "ws_pixel": list(ws), "qsel": self.qsel, "qinj_en": bool(self.qinj_en), "offset": self.offset,
            "trigger_pixels": [list(p) for p in trig], "rfsel": self.rfsel, "baselines": self._baselines(),
        }
        text = (
            f"pixel ({ws[0]},{ws[1]}) QSel {self.qsel} QInjEn {int(bool(self.qinj_en))}, DAC = BL + {self.offset}"
            + (f", trigger pixels {trig} QInjEn 1" if trig else "")
            + (f", RFSel {self.rfsel}" if rfsel is not None else "")
        )
        self.log.info(f"Pixels configured: {text}; baselines {self._pixel_state['baselines']}")
        return text

    def _baselines(self) -> list[dict[str, Any]]:
        """Auto-calibrated baselines the wrapper holds (empty when it holds none)."""
        out: list[dict[str, Any]] = []
        bl = getattr(self.i2c_conn, "BL_df", None)
        if not isinstance(bl, dict):
            return out
        for addr, df in bl.items():
            try:
                for r in df.itertuples():
                    out.append({"chip": hex(addr), "row": int(r.row), "col": int(r.col),
                                "baseline": int(r.baseline), "noise_width": int(r.noise_width)})
            except Exception:  # noqa: BLE001 - a record, never a reason to fail the configuration
                continue
        return out

    def do_landing(self) -> str:
        self.configure_memo_FC(memo="Triggerbit")
        self._close_connection()
        self._close_i2c()

        self.log.info("Socket shutdown and closed, Fast Command idling")
        return "Landed - Socket shutdown and closed, Fast Command idling"

    def do_starting(self, run_identifier: str) -> str:
        """Move to data taking position"""
        # one index per event (R-KC7-SW-21: the ETROC2v2 counter advanced per chip and skipped indices)
        self.file_counter = 0
        self._ws_timeouts = 0
        self._wait_seconds: list[float] = []
        self._read_seconds: list[float] = []
        self.run_identifier = run_identifier

        # --- NEW: Clean, absolute directory logic ---
        # Directory structure becomes: output_path/run_identifier/
        self.run_dir = Path(self.output_path) / self.run_identifier
        self.log.info(f"Creating output directory: {self.run_dir}")

        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exception:
            raise RuntimeError(f"Failed to create directory {self.run_dir}: {exception}") from exception

        # Pure Bitwise Math for Timestamp
        modified_timestamp = self.timestamp & ~0x3
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", modified_timestamp)
        time.sleep(0.1)

        self._readback = {}
        self._read_back_pixels("start")
        self.i2c_conn.batch_ws_start_sampling(sel1=self.ws_sel1, sel2=self.ws_sel2)
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "ws_clear_pulse")
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "active_channel", self.ws_active_channel)
        self._ws_run_active = True
        self._events_csv = self.run_dir / "ws_events.csv"
        with open(self._events_csv, "w") as fh:
            fh.write("event,unix_time,ws_wait_s,read_s,chips\n")
        time.sleep(0.1)

        return f"Run {run_identifier} Session Started"

    def do_stopping(self, readback: bool = True) -> str:
        """End the run. Add run metadata for end-of-run event (readback: the pixel read-back, off when quiescing)"""
        time.sleep(0.1)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        if readback:
            self._read_back_pixels("stop")
        self._ws_run_active = False
        time.sleep(0.1)
        self._write_run_summary()
        return f"Run {self.run_identifier} Stopped"

    def _read_back_pixels(self, tag: str) -> None:
        """With pixel_readback: the READBACK_FIELDS of ws_pixel and trigger_pixels as the chip holds them.

        Stored under `tag` in the run summary's pixel_readback; a failed read is recorded, never raised (a record,
        not a reason to fail the run)."""
        if not getattr(self, "pixel_readback", False):
            return
        if not isinstance(getattr(self, "_readback", None), dict):
            self._readback = {}
        pixels = [tuple(self.ws_pixel), *pixel_pairs(self.trigger_pixels)]
        t0 = time.monotonic()
        try:
            values = self.i2c_conn.batch_read_pixel_config(pixel_list=pixels, fields=READBACK_FIELDS)
            entry: dict[str, Any] = {"values": values}
        except Exception as e:  # noqa: BLE001
            entry = {"error": f"{type(e).__name__}: {e}"}
            self.log.warning(f"Pixel read-back at run {tag} failed: {entry['error']}")
        entry.update({"unix": round(time.time(), 3), "seconds": round(time.monotonic() - t0, 3)})
        self._readback[tag] = entry
        if "values" in entry:
            self.log.info(f"Pixel read-back at run {tag}: {entry['values']}")

    def _ws_summary(self) -> dict[str, Any]:
        return {
            "run_identifier": getattr(self, "run_identifier", None),
            "events": getattr(self, "file_counter", 0),
            "ws_timeouts": getattr(self, "_ws_timeouts", 0),
            "max_events": getattr(self, "max_events", 0),
            "ws_ready_timeout": getattr(self, "ws_ready_timeout", WS_READY_POLL_S),
            "wait_seconds": list(getattr(self, "_wait_seconds", [])),
            "read_seconds": list(getattr(self, "_read_seconds", [])),
            "chips": list(getattr(self, "chip_names", [])),
            "fc_grammar": getattr(self, "fc_grammar", None),
            "fast_command_memo": getattr(self, "fast_command_memo", None),
            "ws_active_channel": getattr(self, "ws_active_channel", None),
            "triggerbit_delay": getattr(self, "triggerbit_delay", None),
            "ws_sel1": getattr(self, "ws_sel1", None),
            "ws_sel2": getattr(self, "ws_sel2", None),
            "pixels": getattr(self, "_pixel_state", None),
            "qinj_spacing_bx": getattr(self, "qinj_spacing_bx", None),
            "pixel_readback": getattr(self, "_readback", None) if getattr(self, "pixel_readback", False) else None,
        }

    def _write_run_summary(self) -> None:
        """ws_run_summary.json next to the CSVs: events, timeouts, per-event wait and read times, FC schedule."""
        run_dir = getattr(self, "run_dir", None)
        if run_dir is None:
            return
        summary = self._ws_summary()
        summary["fc_schedule"] = getattr(self, "_fc_schedule", None)
        summary["stop_unix"] = time.time()
        try:
            (Path(run_dir) / "ws_run_summary.json").write_text(json.dumps(summary, indent=1, default=str))
        except OSError as e:
            self.log.warning(f"Could not write the run summary in {run_dir}: {e}")

    def do_run(self) -> str:
        """Run the satellite. Collect data from buffers and save CSVs."""
        self.log.info("ETROC2Waveform satellite running, collecting waveforms...")
        limit_logged = False

        while not self.stop_requested():

            if self.max_events and self.file_counter >= self.max_events:
                if not limit_logged:
                    self.log.info(f"max_events = {self.max_events} captured; idling until the run is stopped")
                    limit_logged = True
                time.sleep(0.05)
                continue

            # Wait for FPGA trigger completion: poll with a pause, stop request checked on every pass
            before_check = time.monotonic()
            ws_ready = False

            while time.monotonic() - before_check < self.ws_ready_timeout and not self.stop_requested():
                if cmd_interpret.read_status_reg(self.connection_socket, 0) & 1:
                    ws_ready = True
                    time.sleep(0.1)
                    break
                time.sleep(WS_POLL_INTERVAL_S)
            if self.stop_requested():
                break
            waited = time.monotonic() - before_check

            # Check the flag after the loop finishes
            if not ws_ready:
                self._ws_timeouts += 1
                self.log.error(f"FPGA Timeout: No WS Ready within {self.ws_ready_timeout:g} s. Skipping event.")

                # Reset the hardware so it doesn't stay permanently stuck
                cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "ws_clear_pulse")
                cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")
                continue

            t_read = time.monotonic()
            dfs = self.i2c_conn.batch_ws_read_data()
            read_s = time.monotonic() - t_read
            event = self.file_counter

            for chip_name, df in dfs.items():

                # Example: WS_rawData_c1_0005.csv (one index per event, the same for every chip)
                filename = f"WS_rawData_{chip_name}_{event:04d}.csv"
                csv_path = self.run_dir / filename

                if csv_path.exists():
                    self.log.critical(f"File already exists: {csv_path}")
                    raise RuntimeError(f"File already exists: {csv_path}")

                df.to_csv(csv_path)

            self.file_counter += 1
            self._wait_seconds.append(round(waited, 3))
            self._read_seconds.append(round(read_s, 3))
            with open(self._events_csv, "a") as fh:
                fh.write(f"{event},{time.time():.3f},{waited:.3f},{read_s:.3f},{len(dfs)}\n")
            self.log.info(f"Event {event}: WS ready after {waited:.2f} s, read of {len(dfs)} chip(s) took {read_s:.1f} s")

            # Restart WS cycle
            self.i2c_conn.batch_ws_start_sampling(sel1=self.ws_sel1, sel2=self.ws_sel2)
            cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "ws_clear_pulse")
            cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")

        return "Finished acquisition"

    def _close_connection(self) -> None:
        """Close the KC705 TCP connection if one is open."""
        cmd_interpret.close_socket(getattr(self, "connection_socket", None))
        self.connection_socket = None

    def _close_i2c(self) -> None:
        """Release the USB-ISS adapter if it is open."""
        conn = getattr(self, "i2c_conn", None)
        self.i2c_conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception as e:
                self.log.warning(f"Closing the I2C adapter failed: {e}")

    # ------------------------------------------------------------- reconfigure
    def _validate_settings(self, values: dict[str, Any]) -> None:
        grammar = values.get("fc_grammar", getattr(self, "fc_grammar", "fixed"))
        if grammar not in FC_GRAMMARS:
            raise ValueError(f"fc_grammar must be one of {FC_GRAMMARS}, got {grammar!r}")
        # the memo is checked in the grammar it will be played with (a reconfigure may change both)
        self._grammar_for_check = grammar
        self._spacing_for_check = values.get("qinj_spacing_bx", getattr(self, "qinj_spacing_bx", REPEAT_STEP_BX))
        try:
            self._validate_kc705_settings(values)
            schedule_keys = ("fc_grammar" in values or "qinj_spacing_bx" in values)
            if schedule_keys and "fast_command_memo" not in values and hasattr(self, "fast_command_memo"):
                self._check_memo(self.fast_command_memo)
            elif "qinj_spacing_bx" in values and "fast_command_memo" not in values:
                fc_schedule("Start", grammar, self._spacing_for_check)  # range and grammar check at initialize
        finally:
            self._grammar_for_check = None
            self._spacing_for_check = None
        if "max_events" in values and values["max_events"] < 0:
            raise ValueError(f"max_events must be >= 0, got {values['max_events']}")
        if "ws_ready_timeout" in values and not 0 < values["ws_ready_timeout"] <= self.WS_READY_TIMEOUT_MAX_S:
            raise ValueError(
                f"ws_ready_timeout must be > 0 and <= {self.WS_READY_TIMEOUT_MAX_S} s, got {values['ws_ready_timeout']}"
            )
        self._validate_pixel_settings(values)

    def _validate_pixel_settings(self, values: dict[str, Any]) -> None:
        """ws_pixel, trigger_pixels, qsel, offset, rfsel, ws_sel1/2 (the keys present; the others as configured)."""

        def is_pixel(pair) -> bool:
            return len(pair) == 2 and all(isinstance(v, int) and 0 <= v <= 15 for v in pair)

        if "ws_pixel" in values and not is_pixel(values["ws_pixel"]):
            raise ValueError(f"ws_pixel must be [row, col] with 0-15 each, got {values['ws_pixel']}")
        if "trigger_pixels" in values:
            flat = values["trigger_pixels"]
            if len(flat) % 2 or not all(is_pixel(p) for p in pixel_pairs(flat)):
                raise ValueError(f"trigger_pixels must be a flat list [row, col, row, col, ...] with 0-15 each, got {flat}")
        ws = tuple(values.get("ws_pixel", getattr(self, "ws_pixel", WS_PIXEL)))
        trig = pixel_pairs(values.get("trigger_pixels", getattr(self, "trigger_pixels", [])))
        if ws in trig or len(set(trig)) != len(trig):
            raise ValueError(f"trigger_pixels {trig} must be distinct and must not contain ws_pixel {list(ws)}")
        if "qsel" in values and not 0 <= values["qsel"] <= 31:
            raise ValueError(f"qsel must be 0-31 (QSel[4:0]), got {values['qsel']}")
        if "offset" in values and not -1023 <= values["offset"] <= 1023:
            raise ValueError(f"offset (DAC = BL + offset) must be within +-1023, got {values['offset']}")
        if "rfsel" in values and values["rfsel"] not in (-1, 0, 1, 2, 3):
            raise ValueError(f"rfsel must be 0-3, or -1 to leave it unwritten, got {values['rfsel']}")
        for key in ("ws_sel1", "ws_sel2"):
            if key in values and values[key] not in (0, 1):
                raise ValueError(f"{key} must be 0 or 1, got {values[key]}")

    def _apply_reconfigure(self, plan: dict[str, Any]) -> list[str]:
        changes = self._reconfigure_kc705(plan)
        if any(key in plan for key in PIXEL_KEYS):
            changes.append("pixels reconfigured over I2C: " + self._configure_pixels())
        if (("fc_grammar" in plan or "qinj_spacing_bx" in plan)
                and "fast_command_memo" not in plan and "prescale_factor" not in plan):
            self.configure_memo_FC()
            changes.append(f"FC memory reprogrammed from '{self.fast_command_memo}' ({self.fc_grammar} grammar, "
                           f"injections {self.qinj_spacing_bx} BX apart)")
        return changes

    # ------------------------------------------------------- ERROR and Ctrl-C
    def _reentry_join_timeout(self) -> float:
        """An event in progress finishes its WS-ready poll and the I2C read of every chip."""
        chips = len(getattr(self, "chip_addresses", None) or [None])
        poll = getattr(self, "ws_ready_timeout", WS_READY_POLL_S)
        bound = poll + chips * WS_READ_S_PER_CHIP + 1.0
        self.log.info(
            f"Run-thread bound {bound:.0f} s = WS-ready poll {poll:.0f} s + {chips} chip(s) x "
            f"{WS_READ_S_PER_CHIP:.0f} s WS read + 1 s"
        )
        return bound

    def _verify_reg15_restored(self) -> str:
        reg15 = cmd_interpret.read_config_reg(self.connection_socket, cmd_interpret.CONFIG_REGISTERS["active_channel"])
        if reg15 != self.active_channel:
            raise RuntimeError(f"Reg15 reads 0x{reg15:04x}, expected active_channel 0x{self.active_channel:04x}")
        return f"Reg15 restored to active_channel 0x{reg15:04x}"

    def _quiesce(self, reason: str) -> str:
        """Restore Reg15 if a run set the WS channel, idle the fast commands, read back, close socket and I2C."""
        steps = []
        if getattr(self, "_ws_run_active", False):
            steps.append(("Reg15 restore", lambda: self.do_stopping(readback=False), self._verify_reg15_restored))
        text = self._kc705_quiesce(reason, steps)
        if self.__dict__.get("_run_thread_stuck"):
            return text + "; I2C adapter left open (the run thread may still be reading the waveform sampler)"
        if getattr(self, "i2c_conn", None) is None:
            return text
        closed = run_step(self.log, f"Releasing the I2C adapter ({reason})", self._close_i2c)
        return text + ("; I2C adapter closed" if closed else "; releasing the I2C adapter failed")

    def fail_gracefully(self) -> str:
        """Entering ERROR: restore Reg15, idle the fast commands, release the KC705 connection and the I2C adapter."""
        self._settle_run_thread(0.0)
        return "Failed gracefully: " + self._quiesce_locked("failure")
