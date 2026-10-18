"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2
"""

import re
import time
from pathlib import Path
from typing import Any

from constellation.core.configuration import Configuration
from constellation.core.satellite import Satellite

from ..EtrocCommon import ETROC_SATELLITES_VERSION, cmd_interpret, fast_command
from ..EtrocCommon.commands import KC705RegisterCommands
from ..EtrocCommon.config import load_typed_config
from ..EtrocCommon.lifecycle import EtrocLifecycle, run_step
from ..EtrocCommon.reconfigure import EtrocReconfigure
from ..EtrocCommon.satellite_types import REQUIRED_KEYS

_WAVEFORM_MEMO_WORDS = {"QInj", "L1A", "L1ARange", "BCR", "Triggerbit", "Start", "uniform"}
_REPEATED_RE = re.compile(r"^repeatedQInj(=\d+)?$")


# I/O timeout for the stop/idle writes of a shutdown, so a dead board cannot hold it for socket_timeout
QUIESCE_IO_TIMEOUT_S = 2.0


class EtrocWaveform(KC705RegisterCommands, EtrocReconfigure, EtrocLifecycle, Satellite, version=ETROC_SATELLITES_VERSION):
    """Waveform-sampler readout: configures the ETROC2 over I2C and saves one CSV per trigger."""

    REQUIRED_KEYS = REQUIRED_KEYS["EtrocWaveform"]
    # do_run polls the WS-ready bit for up to 5 s before it checks stop_requested() again
    REENTRY_JOIN_TIMEOUT_S = 8.0

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
    }

    # Keys `reconfigure` may change in ORBIT. The connection, the I2C adapter and chips (set up at
    # launch: calibration, pixel offset) and the output directory need a landing.
    RECONFIGURABLE_KEYS = (
        *KC705RegisterCommands.LAUNCH_REGISTERS,
        "prescale_factor", "fast_command_memo", "ws_active_channel",
        "num_fifo_read", "clear_fifo", "reset_counter", "firmware", "active_boards",
    )

    def _send_fc_sequence(self, reg12_val: int, reg10_val: int, reg9_val: int) -> None:
        """Fill FC memory addresses [reg10_val, reg9_val] with the command in reg12_val."""
        fast_command.send_fc_sequence(self.connection_socket, reg12_val, reg10_val, reg9_val, self.prescale_factor)

    def _check_memo(self, memo: str) -> None:
        """Validate a memo against the EtrocWaveform grammar."""
        for word in memo.split():
            if word not in _WAVEFORM_MEMO_WORDS and not _REPEATED_RE.match(word):
                raise ValueError(
                    f"Unknown word '{word}' in fast_command_memo '{memo}'. "
                    f"Allowed: {sorted(_WAVEFORM_MEMO_WORDS)} and repeatedQInj=N"
                )

    def _execute_fc_command(self, base_reg12: int, cmd_offset: int, base_val: int, loops: int, uniform: bool, do_loop: bool) -> None:
        """Helper to handle the initial command and its optional repeating loop."""
        reg12_val = base_reg12 + cmd_offset

        # 1. Send the initial base command
        self._send_fc_sequence(reg12_val, base_val, base_val)

        # 2. Execute the loop if requested
        if do_loop and loops > 0:
            interval = (3000 // 16) // loops if uniform else 1
            for i in range(1, loops + 1):
                step_val = base_val + (interval * i * 0x010) if uniform else base_val + (i * 0x010)
                self._send_fc_sequence(reg12_val, step_val, step_val)

    def configure_memo_FC(self, memo=None) -> None:
        memo_str = memo if memo is not None else self.fast_command_memo
        words = memo_str.split(' ')

        # 1. Parse all flags into a clean dictionary
        flags = {
            "QInj": "QInj" in words,
            "repeatedQInj": any("repeatedQInj" in w for w in words),
            "L1A": "L1A" in words,
            "L1ARange": "L1ARange" in words,
            "BCR": "BCR" in words,
            "Triggerbit": "Triggerbit" in words,
            "Start": "Start" in words,
            "uniform": "uniform" in words,
        }

        # 2. Extract loop count safely
        qinj_loop = 1
        for word in words:
            if "repeatedQInj=" in word:
                try:
                    qinj_loop = int(word.split('=')[1])
                    self.log.info(f'Repeat charge injection by {qinj_loop}')
                except ValueError:
                    self.log.info('Invalid repeat value. Defaulting to single charge injection.')
                break
        else:
            if flags["QInj"]:
                self.log.info('Only do single charge injection')

        # 3. Determine base register 12 value
        base_reg12 = 0x0070 if flags["Triggerbit"] else 0x0030

        # --- Hardware Execution Sequence ---

        if flags["Start"]:
            cmd_interpret.write_config_reg_decoded(self.connection_socket, "register_11", 0x0deb)
            time.sleep(0.01)

        # IDLE (Always executed based on original code flow)
        self._send_fc_sequence(base_reg12, 0x000, 0x0deb)

        if flags["BCR"]:
            self._send_fc_sequence(base_reg12 + 0x2, 0x000, 0x000)

        if flags["QInj"]:
            self._execute_fc_command(base_reg12, 0x5, 0x005, qinj_loop, flags["uniform"], flags["repeatedQInj"])

        if flags["L1A"]:
            self._execute_fc_command(base_reg12, 0x6, 0x1fd, qinj_loop, flags["uniform"], flags["L1ARange"])

        # Final signal start
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "fc_signal_start")
        time.sleep(0.01)

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
        self.file_counter = 0
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
        time.sleep(0.1)

        return f"Run {run_identifier} Session Started"

    def do_stopping(self) -> str:
        """End the run. Add run metadata for end-of-run event"""
        time.sleep(0.1)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        self._ws_run_active = False
        time.sleep(0.1)
        return f"Run {self.run_identifier} Stopped"

    def do_run(self) -> str:
        """Run the satellite. Collect data from buffers and save CSVs."""
        self.log.info("ETROC2Waveform satellite running, collecting waveforms...")

        while not self.stop_requested():

            # Wait for FPGA trigger completion
            before_check = time.monotonic()
            ws_ready = False

            while (time.monotonic() - before_check < 5):
                if cmd_interpret.read_status_reg(self.connection_socket, 0) & 1:
                    ws_ready = True
                    time.sleep(0.1)
                    break

            # Check the flag after the loop finishes
            if not ws_ready:
                self.log.error("FPGA Timeout: No WS Ready within 5s. Skipping event.")

                # Reset the hardware so it doesn't stay permanently stuck
                cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "ws_clear_pulse")
                cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")
                continue

            dfs = self.i2c_conn.batch_ws_read_data()

            for chip_name, df in dfs.items():

                # Example: WS_rawData_c1_0005.csv
                filename = f"WS_rawData_{chip_name}_{self.file_counter:04d}.csv"
                csv_path = self.run_dir / filename

                if csv_path.exists():
                    self.log.critical(f"File already exists: {csv_path}")
                    raise RuntimeError(f"File already exists: {csv_path}")

                df.to_csv(csv_path)
                self.file_counter += 1

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
        self._validate_kc705_settings(values)

    def _apply_reconfigure(self, plan: dict[str, Any]) -> list[str]:
        return self._reconfigure_kc705(plan)

    # ------------------------------------------------------- ERROR and Ctrl-C
    def _quiesce(self, reason: str) -> str:
        """Restore Reg15 if a run set the WS channel, idle the fast commands, close socket and I2C (idempotent)."""
        sock = getattr(self, "connection_socket", None)
        if sock is not None:
            run_step(self.log, f"Shortening the KC705 I/O timeout ({reason})", sock.settimeout, QUIESCE_IO_TIMEOUT_S)
            if getattr(self, "_ws_run_active", False):
                run_step(self.log, f"Restoring active_channel after the run ({reason})", self.do_stopping)
            run_step(self.log, f"Idling the fast commands ({reason})", self.configure_memo_FC, "Triggerbit")
            run_step(self.log, f"Closing the KC705 connection ({reason})", self._close_connection)
        run_step(self.log, f"Releasing the I2C adapter ({reason})", self._close_i2c)
        return "fast commands idle, KC705 connection and I2C adapter closed"

    def fail_gracefully(self) -> str:
        """Entering ERROR: idle the fast commands, release the KC705 connection and the I2C adapter."""
        return "Failed gracefully: " + self._quiesce_locked("failure")
