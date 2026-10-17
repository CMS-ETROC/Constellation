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


class EtrocTransmitter(KC705RegisterCommands, TransmitterSatellite, version=ETROC_SATELLITES_VERSION):
    """Reads the KC705 data FIFO over TCP and sends it as CDTP data records."""

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

    def configure_memo_FC(self, memo=None) -> None:
        """Program the FC memory from a memo string (grammar in EtrocCommon.fast_command)."""
        memo_str = memo if memo is not None else self.fast_command_memo
        fast_command.configure_memo_fc(self.connection_socket, memo_str, self.prescale_factor)

    def do_initializing(self, config: Configuration) -> str:
        """Configure the Satellite and any associated hardware."""

        self._close_connection()
        load_typed_config(self, config, self.DEFAULT_CONFIG)

        if self.prescale_factor not in [2048, 4096, 8192, 16384]:
            raise ValueError(f"Prescale factor must be one of [2048, 4096, 8192, 16384], {self.prescale_factor} not supported")
        # Reject a malformed memo before any hardware access
        fast_command.parse_memo(self.fast_command_memo)

        self.log.info("Configuration loaded and Defaults set")
        return "Initialized - Configuration loaded and Defaults set"

    def do_launching(self) -> str:
        self._close_connection()
        self.connection_socket = cmd_interpret.open_socket(
            self.hostname, self.port, self.connect_timeout, self.socket_timeout or None
        )

        # 2. Cleanly map and write standard registers
        initial_registers = {
            "active_channel": self.active_channel,
            "timestamp": self.timestamp,
            "triggerbit_delay": self.triggerbit_delay,
            "polarity": self.polarity,
            "counter_duration": self.counter_duration,
            "fc_delays": self.fc_delays,
            "data_delays_01": self.data_delays_01,
            "data_delays_23": self.data_delays_23
        }

        for reg_name, value in initial_registers.items():
            cmd_interpret.write_config_reg_decoded(self.connection_socket, reg_name, value)

        # Write special-case register
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "register_10", 0x000, self.prescale_factor)

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
        # User tags of the begin-of-run message (the framework adds the configuration)
        self.bor = {
            "start_time": time.strftime("%Y-%m-%d-%H%M%S", time.localtime()),
            "run_identifier": run_identifier,
            "fast_command_memo": self.fast_command_memo,
        }
        time.sleep(0.1)  # add sleep to make sure that everything has stopped

        # FPGA Presteps for DAQ
        if self.reset_counter:
            cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "reset_counter")
            time.sleep(0.1)
            self.log.info("Cleared Event Counter")

        # Start DAQ Session on FPGA
        self.log.info("Starting DAQ Session on FPGA...")

        # Bitwise logic: Clear the bottom 2 bits (~0b11) and set them to '10' (0b10)
        self.timestamp = (self.timestamp & ~0b11) | 0b10
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        time.sleep(0.1)

        self.log.debug(f"Status of DAQ Toggle before Start Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "start_DAQ")
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle after Start Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")

        return f"Run {run_identifier} Session Started"

    def do_stopping(self) -> str:
        """End the run. Add run metadata for end-of-run event"""
        time.sleep(0.1)
        # Stop DAQ Session on FPGA
        self.log.info("Stopping DAQ on FPGA...")

        # Bitwise logic: Clear the bottom 2 bits (~0b11) and set them to '10' (0b10)
        self.timestamp = (self.timestamp & ~0b11) | 0b10
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        time.sleep(0.1)

        self.log.debug(f"Status of DAQ Toggle before Stop Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")
        cmd_interpret.write_pulse_reg_decoded(self.connection_socket, "stop_DAQ")
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle after Stop Pulse: {format(cmd_interpret.read_status_reg(self.connection_socket, 5), '016b')}")

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

    def _close_connection(self) -> None:
        """Close the KC705 TCP connection if one is open."""
        cmd_interpret.close_socket(getattr(self, "connection_socket", None))
        self.connection_socket = None

    def fail_gracefully(self) -> str:
        """Release the KC705 connection when entering ERROR so a later initialize can reconnect."""
        self._close_connection()
        return "Failed gracefully, KC705 connection closed"
