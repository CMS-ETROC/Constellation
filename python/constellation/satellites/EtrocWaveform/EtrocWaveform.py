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


def fc_schedule(memo: str, grammar: str = "fixed") -> dict[str, Any]:
    """The FC memory writes a memo makes, without hardware.

    Returns {"grammar", "memo", "start", "triggerbit", "bcr", "qinj_bx", "l1a_bx" (addresses written,
    in write order), "played_qinj_bx", "played_l1a_bx" (distinct addresses inside the orbit after the
    12-bit address field, i.e. the commands the firmware plays once per orbit)}.
    The fixed grammar raises ValueError for a repeat count < 1 or a schedule outside the orbit.
    """
    if grammar not in FC_GRAMMARS:
        raise ValueError(f"fc_grammar must be one of {FC_GRAMMARS}, got {grammar!r}")
    f = parse_waveform_memo(memo)
    if grammar == "legacy":
        loops = f["repeat"] if f["repeat"] is not None else 1
        qinj = _legacy_addresses(QINJ_BASE_BX, loops, f["uniform"], f["repeatedQInj"]) if f["QInj"] else []
        l1a = _legacy_addresses(L1A_BASE_BX, loops, f["uniform"], f["L1ARange"]) if f["L1A"] else []
    else:
        n = f["repeat"] if f["repeat"] is not None else 1
        if n < 1:
            raise ValueError(f"repeatedQInj={n}: the repeat count must be at least 1")
        step = REPEAT_STEP_BX
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
        "qinj_bx": qinj, "l1a_bx": l1a, "played_qinj_bx": played(qinj), "played_l1a_bx": played(l1a),
    }


# do_run polls the WS-ready bit for up to ws_ready_timeout s (default) before an event is skipped
WS_READY_POLL_S = 5.0
# pause between two WS-ready reads (R-KC7-SW-21: the ETROC2v2 loop polled without a pause)
WS_POLL_INTERVAL_S = 0.01
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

    # Keys `reconfigure` may change in ORBIT. The connection, the I2C adapter and chips (set up at
    # launch: calibration, pixel offset) and the output directory need a landing.
    RECONFIGURABLE_KEYS = (
        *KC705RegisterCommands.LAUNCH_REGISTERS,
        "prescale_factor", "fast_command_memo", "ws_active_channel",
        "num_fifo_read", "clear_fifo", "reset_counter", "firmware", "active_boards",
        "fc_grammar", "max_events", "ws_ready_timeout",
    )

    WS_READY_TIMEOUT_MAX_S = 60.0

    def _send_fc_sequence(self, reg12_val: int, reg10_val: int, reg9_val: int) -> None:
        """Fill FC memory addresses [reg10_val, reg9_val] with the command in reg12_val."""
        fast_command.send_fc_sequence(self.connection_socket, reg12_val, reg10_val, reg9_val, self.prescale_factor)

    def _check_memo(self, memo: str) -> None:
        """Validate a memo against the EtrocWaveform grammar in use (fc_grammar)."""
        fc_schedule(memo, getattr(self, "_grammar_for_check", None) or getattr(self, "fc_grammar", "fixed"))

    def configure_memo_FC(self, memo=None) -> None:
        """Program the FC memory from a memo in the fc_grammar in use, then start the playback."""
        memo_str = memo if memo is not None else self.fast_command_memo
        grammar = getattr(self, "fc_grammar", "fixed")
        sched = fc_schedule(memo_str, grammar)
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
        sched = fc_schedule(self.fast_command_memo, self.fc_grammar)
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
        self.i2c_conn.batch_disable_all_chips(power_mode='high')
        self.i2c_conn.batch_auto_calibrate_chip(pixel_list=[(0, 14)])
        self.i2c_conn.batch_enable_pixels(pixel_list=[(0, 14)], offset=self.offset)

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

        self.i2c_conn.batch_ws_start_sampling()
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "ws_clear_pulse")
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "active_channel", self.ws_active_channel)
        self._ws_run_active = True
        self._events_csv = self.run_dir / "ws_events.csv"
        with open(self._events_csv, "w") as fh:
            fh.write("event,unix_time,ws_wait_s,read_s,chips\n")
        time.sleep(0.1)

        return f"Run {run_identifier} Session Started"

    def do_stopping(self) -> str:
        """End the run. Add run metadata for end-of-run event"""
        time.sleep(0.1)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        self._ws_run_active = False
        time.sleep(0.1)
        self._write_run_summary()
        return f"Run {self.run_identifier} Stopped"

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
            self.i2c_conn.batch_ws_start_sampling()
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
        try:
            self._validate_kc705_settings(values)
            if "fc_grammar" in values and "fast_command_memo" not in values and hasattr(self, "fast_command_memo"):
                self._check_memo(self.fast_command_memo)
        finally:
            self._grammar_for_check = None
        if "max_events" in values and values["max_events"] < 0:
            raise ValueError(f"max_events must be >= 0, got {values['max_events']}")
        if "ws_ready_timeout" in values and not 0 < values["ws_ready_timeout"] <= self.WS_READY_TIMEOUT_MAX_S:
            raise ValueError(
                f"ws_ready_timeout must be > 0 and <= {self.WS_READY_TIMEOUT_MAX_S} s, got {values['ws_ready_timeout']}"
            )

    def _apply_reconfigure(self, plan: dict[str, Any]) -> list[str]:
        changes = self._reconfigure_kc705(plan)
        if "fc_grammar" in plan and "fast_command_memo" not in plan and "prescale_factor" not in plan:
            self.configure_memo_FC()
            changes.append(f"FC memory reprogrammed from '{self.fast_command_memo}' ({self.fc_grammar} grammar)")
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
            steps.append(("Reg15 restore", self.do_stopping, self._verify_reg15_restored))
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
