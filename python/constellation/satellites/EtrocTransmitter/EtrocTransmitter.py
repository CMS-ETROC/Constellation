"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2
"""

import time
from typing import Any

from constellation.core.configuration import Configuration
from constellation.core.transmitter_satellite import TransmitterSatellite

from ..EtrocCommon import ETROC_SATELLITES_VERSION, cmd_interpret, fast_command
from ..EtrocCommon.commands import KC705RegisterCommands
from ..EtrocCommon.config import load_typed_config
from ..EtrocCommon.lifecycle import EtrocLifecycle, run_step
from ..EtrocCommon.reconfigure import EtrocReconfigure
from ..EtrocCommon.satellite_types import REQUIRED_KEYS

# I/O timeout for the stop/idle writes of a shutdown, so a dead board cannot hold it for socket_timeout
QUIESCE_IO_TIMEOUT_S = 2.0


class EtrocTransmitter(
    KC705RegisterCommands, EtrocReconfigure, EtrocLifecycle, TransmitterSatellite, version=ETROC_SATELLITES_VERSION
):
    """Reads the KC705 data FIFO over TCP and sends it as CDTP data records."""

    REQUIRED_KEYS = REQUIRED_KEYS["EtrocTransmitter"]

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
        "connect_timeout": 5.0,
        # A blocked TCP read (stalled board, KC7-FW-002) ends the run with an error after this many
        # seconds instead of hanging forever. 0 = wait forever (ETROC2v2 behaviour).
        "socket_timeout": 60.0,
    }

    # Keys `reconfigure` may change in ORBIT; the connection keys need a landing.
    RECONFIGURABLE_KEYS = (
        *KC705RegisterCommands.LAUNCH_REGISTERS,
        "prescale_factor", "fast_command_memo", "num_fifo_read", "clear_fifo", "reset_counter", "firmware",
    )

    def configure_memo_FC(self, memo=None) -> None:
        """Program the FC memory from a memo string (grammar in EtrocCommon.fast_command)."""
        memo_str = memo if memo is not None else self.fast_command_memo
        fast_command.configure_memo_fc(self.connection_socket, memo_str, self.prescale_factor)

    def do_initializing(self, config: Configuration) -> str:
        """Configure the Satellite and any associated hardware."""

        self._close_connection()
        self._daq_started = False
        effective = load_typed_config(self, config, self.DEFAULT_CONFIG)
        # Reject a bad prescale factor or a malformed memo before any hardware access
        self._validate_settings(effective)

        self.log.info("Configuration loaded and Defaults set")
        return "Initialized - Configuration loaded and Defaults set"

    def do_launching(self) -> str:
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

        self.log.info("Socket shutdown and closed, Fast Command idling")
        return "Landed - Socket shutdown and closed, Fast Command idling"

    def do_starting(self, run_identifier: str) -> str:
        """
        move to data taking position
        """
        time.sleep(0.1)  # add sleep to make sure that everything has stopped

        if self.clear_fifo:
            # Words left in the FIFO after the previous stop_DAQ would open this run. fifo_reset
            # drives the global reset, which also stops the FC playback: reprogram it afterwards.
            self.log.info("Clearing FIFO before the run...")
            cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "clear_fifo")
            time.sleep(2.1)
            self.configure_memo_FC()

        # FPGA Presteps for DAQ
        if self.reset_counter:
            cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "reset_counter")
            time.sleep(0.1)
            self.log.info("Pulsed clrError (pulse register bit 5, 'reset_counter')")

        # User tags of the begin-of-run message (the framework adds the configuration). The
        # registers are read back from the board, so commands sent in ORBIT are recorded too.
        self.bor = {
            "start_time": time.strftime("%Y-%m-%d-%H%M%S", time.localtime()),
            "run_identifier": run_identifier,
            "fast_command_memo": self.fast_command_memo,
            "kc705_readback": self._read_back_registers(),
        }

        # Start DAQ Session on FPGA
        self.log.info("Starting DAQ Session on FPGA...")

        # Bitwise logic: Clear the bottom 2 bits (~0b11) and set them to '10' (0b10)
        self.timestamp = (self.timestamp & ~0b11) | 0b10
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        time.sleep(0.1)

        self.log.debug(f"Status of DAQ Toggle before Start Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "start_DAQ")
        self._daq_started = True
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle after Start Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")

        return f"Run {run_identifier} Session Started"

    def do_stopping(self) -> str:
        """End the run. Add run metadata for end-of-run event"""
        time.sleep(0.1)
        # Stop DAQ Session on FPGA
        self.log.info("Stopping DAQ on FPGA...")
        self._stop_daq()

        # User tags of the end-of-run message
        self.eor = {
            "stop_time": time.strftime("%Y-%m-%d-%H%M%S", time.localtime()),
            "register_7": cmd_interpret.read_config_reg(self.connection_socket, 7),
            "register_16": cmd_interpret.read_config_reg(self.connection_socket, 16),
        }
        return f"Run {self.run_identifier} Stopped, EOR Sent"

    def do_run(self) -> str:
        """Run the satellite. Collect data from buffers and send it."""
        # 1. Use the framework's built-in stop request flag
        while not self.stop_requested():

            # 2. Check if rate limited
            if not self.can_send_record():
                time.sleep(0.001)
                continue

            # 3. Read the data FIFO
            try:
                raw_bytes = cmd_interpret.read_data_fifo(self.connection_socket, self.num_fifo_read)
            except TimeoutError as e:
                raise RuntimeError(
                    f"KC705 data FIFO read of {self.num_fifo_read} words got no complete reply within "
                    f"{self.socket_timeout} s: board stalled or link down (no fillers arriving)"
                ) from e

            # 4. Handle an empty buffer
            if not raw_bytes:
                self.log.debug("No data in buffer! Will try to read again")
                time.sleep(0.1)
                continue

            data_record = self.new_data_record()
            data_record.add_block(raw_bytes)
            self.send_data_record(data_record)

        return "Finished acquisition"

    def _read_back_registers(self) -> dict[str, str]:
        """Config registers 4-8 and 13-15 as read from the board, as hex strings."""
        regs = {}
        for name, reg in cmd_interpret.CONFIG_REGISTERS.items():
            if 4 <= reg <= 8 or 13 <= reg <= 15:
                regs[f"reg{reg}_{name}"] = f"0x{cmd_interpret.read_config_reg(self.connection_socket, reg):04x}"
        return regs

    def _stop_daq(self) -> None:
        """The stop sequence of do_stopping: timestamp register, then the stop_DAQ pulse."""
        # Bitwise logic: Clear the bottom 2 bits (~0b11) and set them to '10' (0b10)
        self.timestamp = (self.timestamp & ~0b11) | 0b10
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        time.sleep(0.1)

        self.log.debug(f"Status of DAQ Toggle before Stop Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "stop_DAQ")
        self._daq_started = False
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle after Stop Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")

    def _close_connection(self) -> None:
        """Close the KC705 TCP connection if one is open."""
        cmd_interpret.close_socket(getattr(self, "connection_socket", None))
        self.connection_socket = None

    # ------------------------------------------------------------- reconfigure
    def _validate_settings(self, values: dict[str, Any]) -> None:
        self._validate_kc705_settings(values)

    def _apply_reconfigure(self, plan: dict[str, Any]) -> list[str]:
        return self._reconfigure_kc705(plan)

    # ------------------------------------------------------- ERROR and Ctrl-C
    def _quiesce(self, reason: str) -> str:
        """Stop the DAQ if a run started it, idle the fast commands, close the socket (idempotent)."""
        sock = getattr(self, "connection_socket", None)
        if sock is None:
            return "KC705 connection already closed"
        run_step(self.log, f"Shortening the KC705 I/O timeout ({reason})", sock.settimeout, QUIESCE_IO_TIMEOUT_S)
        if getattr(self, "_daq_started", False):
            if run_step(self.log, f"Stopping the DAQ ({reason})", self._stop_daq):
                self.log.info(f"DAQ stopped on {reason}")
        if run_step(self.log, f"Idling the fast commands ({reason})", self.configure_memo_FC, "Triggerbit"):
            self.log.info(f"Fast commands idle on {reason}")
        run_step(self.log, f"Closing the KC705 connection ({reason})", self._close_connection)
        return "DAQ stopped, fast commands idle, KC705 connection closed"

    def _stop_transport_for_reentry(self) -> None:
        """Stop the CDTP push thread (non-daemon) without waiting for the queue to drain; no EOR is sent."""
        dtm = getattr(self, "_dtm", None)
        thread = getattr(dtm, "_push_thread", None)
        if thread is None or not thread.is_alive():
            return
        dtm._stopevt.set()
        thread.join(timeout=self.REENTRY_JOIN_TIMEOUT_S)
        if thread.is_alive():
            self.log.error("CDTP push thread still sending after reentry; it ends at its data_timeout")

    def fail_gracefully(self) -> str:
        """Entering ERROR: stop the DAQ, idle the fast commands and release the connection for a later initialize."""
        return "Failed gracefully: " + self._quiesce_locked("failure")
