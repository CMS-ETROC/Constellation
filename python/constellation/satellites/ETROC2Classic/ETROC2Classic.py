"""
SPDX-FileCopyrightText: 2024 DESY and the Constellation authors
SPDX-License-Identifier: CC-BY-4.0

Provides the class for the Mariner example satellite
"""

import random
import time
from typing import Any
import datetime
import os
import pathlib
import socket
from .command_interpret import *
import numpy as np

from constellation.core.cmdp import MetricsType
from constellation.core.commandmanager import cscp_requestable
from constellation.core.configuration import Configuration
from constellation.core.cscp import CSCPMessage
from constellation.core.fsm import SatelliteState
from constellation.core.monitoring import schedule_metric
from constellation.core.satellite import Satellite
from constellation.core.datasender import DataSender

class ETROC2Classic(DataSender):
    """Example for a Satellite class."""

    def print_all_config_params(self) -> None:
        self.log.debug("Printing All Config Params...")
        self.log.debug(f"hostname: {self.hostname}")
        self.log.debug(f"port: {self.port}")
        self.log.debug(f"polarity: {hex(self.polarity)}")
        self.log.debug(f"firmware: {self.firmware}")
        self.log.debug(f"timestamp: {hex(self.timestamp)}")
        self.log.debug(f"active_channel: {hex(self.active_channel)}")
        self.log.debug(f"prescale_factor: {self.prescale_factor}")
        self.log.debug(f"counter_duration: {hex(self.counter_duration)}")
        self.log.debug(f"triggerbit_delay: {hex(self.triggerbit_delay)}")
        self.log.debug(f"fc_delays: {hex(self.fc_delays)}")
        self.log.debug(f"data_delays_01: {hex(self.data_delays_01)}")
        self.log.debug(f"data_delays_23: {hex(self.data_delays_23)}")
        self.log.debug(f"num_fifo_read: {self.num_fifo_read}")
        self.log.debug(f"clear_fifo: {self.clear_fifo}")
        self.log.debug(f"reset_counter: {self.reset_counter}")
        self.log.debug(f"fast_command_memo: {self.fast_command_memo}")

    def configure_memo_FC(self, memo=None) -> None:
        if(memo==None):
            words = self.fast_command_memo.split(' ')
        else:
            words = memo.split(' ')
        QInj=False
        repeatedQInj = False
        L1A=False
        L1ARange=False
        BCR=False
        Triggerbit=False
        Initialize = False
        qinj_loop = 1
        uniform_mode = False
        if("QInj" in words):
            QInj=True
            matching_elements = [element for element in words if "repeatedQInj" in element]
            try:
                qinj_loop = int(matching_elements[0].split('=')[1])
                repeatedQInj = True
                self.log.info(f'Repeat charge injection by {qinj_loop}')
            except:
                qinj_loop = 1
                self.log.info('Only do single charge injection')

        if("L1A" in words): L1A=True
        if("L1ARange" in words):
            L1A=True
            L1ARange=True
        if("BCR" in words): BCR=True
        if("Triggerbit" in words): Triggerbit=True
        if("Start" in words): Initialize=True
        if('uniform' in words): uniform_mode=True

        if(Initialize):
            write_config_reg_decoded(self.connection_socket, "register_11", 0x0deb)
            time.sleep(0.01)

        # IDLE
        write_config_reg_decoded(self.connection_socket, "register_12", 0x0070 if Triggerbit else 0x0030)
        write_config_reg_decoded(self.connection_socket, "register_10", 0x000, self.prescale_factor)
        write_config_reg_decoded(self.connection_socket, "register_9", 0xdeb)
        write_pulse_reg_decoded(self.connection_socket, "fc_init")
        time.sleep(0.01)

        if(BCR):
            write_config_reg_decoded(self.connection_socket, "register_12", 0x0072 if Triggerbit else 0x0032)
            write_config_reg_decoded(self.connection_socket, "register_10", 0x000, self.prescale_factor)
            write_config_reg_decoded(self.connection_socket, "register_9", 0x000)
            write_pulse_reg_decoded(self.connection_socket, "fc_init")
            time.sleep(0.01)

        if(QInj):
            write_config_reg_decoded(self.connection_socket, "register_12", 0x0075 if Triggerbit else 0x0035)
            write_config_reg_decoded(self.connection_socket, "register_10", 0x005, self.prescale_factor)
            write_config_reg_decoded(self.connection_socket, "register_9", 0x005)
            write_pulse_reg_decoded(self.connection_socket, "fc_init")
            time.sleep(0.01)
            if(repeatedQInj):
                interval = (3000//16)//qinj_loop
                for i in range(qinj_loop):
                    write_config_reg_decoded(self.connection_socket, "register_12", 0x0075 if Triggerbit else 0x0035)
                    if not (uniform_mode):
                        write_config_reg_decoded(self.connection_socket, "register_10", 0x005 + i*0x010, self.prescale_factor)
                        write_config_reg_decoded(self.connection_socket, "register_9", 0x005 + i*0x010)
                        write_pulse_reg_decoded(self.connection_socket, "fc_init")
                        time.sleep(0.01)
                    else:
                        write_config_reg_decoded(self.connection_socket, "register_10", 0x005 + interval * i*0x010, self.prescale_factor)
                        write_config_reg_decoded(self.connection_socket, "register_9", 0x005 + interval * i*0x010)
                        write_pulse_reg_decoded(self.connection_socket, "fc_init")
                        time.sleep(0.01)

        if(L1A):
            write_config_reg_decoded(self.connection_socket, "register_12", 0x0076 if Triggerbit else 0x0036)
            write_config_reg_decoded(self.connection_socket, "register_10", 0x1fd, self.prescale_factor)
            write_config_reg_decoded(self.connection_socket, "register_9", 0x1fd)
            write_pulse_reg_decoded(self.connection_socket, "fc_init")
            time.sleep(0.01)
            if(L1ARange):
                interval = (3000//16)//qinj_loop
                for i in range(qinj_loop):
                    write_config_reg_decoded(self.connection_socket, "register_12", 0x0076 if Triggerbit else 0x0036)
                    if not (uniform_mode):
                        write_config_reg_decoded(self.connection_socket, "register_10", 0x1fd + i*0x010, self.prescale_factor)
                        write_config_reg_decoded(self.connection_socket, "register_9", 0x1fd + i*0x010)
                        write_pulse_reg_decoded(self.connection_socket, "fc_init")
                        time.sleep(0.01)
                    else:
                        write_config_reg_decoded(self.connection_socket, "register_10", 0x1fd + interval * i*0x010, self.prescale_factor)
                        write_config_reg_decoded(self.connection_socket, "register_9", 0x1fd + interval * i*0x010)
                        write_pulse_reg_decoded(self.connection_socket, "fc_init")
                        time.sleep(0.01)

        write_pulse_reg_decoded(self.connection_socket, "fc_signal_start")
        time.sleep(0.01)

    def do_initializing(self, config: Configuration) -> str:
        """Configure the Satellite and any associated hardware.

        The configuration is provided as Configuration object which works
        similar to a regular dictionary but tracks access to its keys. If a key
        does not exist or if one exists but is not used, the Satellite will
        automatically return an error or warning, respectively.

        """
        self.port = config.setdefault("port", 1024)
        self.hostname = config.setdefault("hostname", "192.168.2.3")
        self.firmware = config.setdefault("firmware", "0001")
        self.polarity = config.setdefault("polarity", 0x4023)
        self.timestamp = config.setdefault("timestamp", 0x0000)
        self.active_channel = config.setdefault("active_channel", 0x0001)
        self.prescale_factor = config.setdefault("prescale_factor", 2048)
        self.counter_duration = config.setdefault("counter_duration", 0x0000)
        self.triggerbit_delay = config.setdefault("triggerbit_delay", 0x1800)
        self.fc_delays = config.setdefault("fc_delays", 0x0000)
        self.data_delays_01 = config.setdefault("data_delays_01", 0x0000)
        self.data_delays_23 = config.setdefault("data_delays_23", 0x0000)
        self.num_fifo_read = config.setdefault("num_fifo_read", 65536)
        self.clear_fifo = config.setdefault("clear_fifo", 1)
        self.reset_counter = config.setdefault("reset_counter", 1)
        self.fast_command_memo = config.setdefault("fast_command_memo", "Start Triggerbit")
        self.connection_socket = None
        # TODO check for valid entries for all config keys
        if self.prescale_factor not in [2048, 4096, 8192, 16384]:
            raise ValueError(f"Prescale factor must be one of [2048, 4096, 8192, 16384], {self.prescale_factor} not supported")
        # if ' '.join(message.split(' ')[:1]) == 'memoFC':
        self.log.info(f"Configuration loaded and Defaults set")
        self.print_all_config_params()
        return "Initialized - Configuration loaded and Defaults set"

    def do_launching(self) -> str:
        try:
            self.connection_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except socket.error:
            raise RuntimeError("Failed to create socket for ETROC2Classic Satellite")
        try:
            self.connection_socket.connect((self.hostname, self.port))
        except socket.error:
            raise ConnectionError(f"Failed to connect to IP: {self.hostname}:{self.port}")
        write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        write_config_reg_decoded(self.connection_socket, "triggerbit_delay", self.triggerbit_delay)
        write_config_reg_decoded(self.connection_socket, "register_10", 0x000, prescale_factor = self.prescale_factor)
        write_config_reg_decoded(self.connection_socket, "polarity", self.polarity)
        write_config_reg_decoded(self.connection_socket, "counter_duration", self.counter_duration)
        write_config_reg_decoded(self.connection_socket, "fc_delays", self.fc_delays)
        write_config_reg_decoded(self.connection_socket, "data_delays_01", self.data_delays_01)
        write_config_reg_decoded(self.connection_socket, "data_delays_23", self.data_delays_23)
        if(self.clear_fifo):
            self.log.info("Clearing FIFO...")
            write_pulse_reg_decoded(self.connection_socket, "clear_fifo")
            time.sleep(2.1)
        self.configure_memo_FC()
        self.log.info(f"Socket connected, FPGA Registers and Fast Command configured")
        return f"Launched - Socket connected, FPGA Registers and Fast Command configured"

    def do_landing(self) -> str:
        self.configure_memo_FC(memo="Triggerbit")
        self.connection_socket.shutdown(socket.SHUT_RDWR)
        self.connection_socket.close()
        self.log.info(f"Socket shutdown and closed, Fast Command idling")
        return f"Landed - Socket shutdown and closed, Fast Command idling"

    def do_reconfigure(self, partial_config: Configuration) -> str:
        config_keys = partial_config.get_keys()
        if len(config_keys)==0:
            self.log.info(f"No Reconfiguration of ETROC2Satellite {self.get_name()} requested in config.")
            return f"No Reconfiguration requested in config."
        if "hostname" in config_keys:
            raise ValueError("Reconfiguring hostname is not possible")
        if "port" in config_keys:
            raise ValueError("Reconfiguring port is not possible")
        if "polarity" in config_keys:
            self.polarity = partial_config["polarity"]
            write_config_reg_decoded(self.connection_socket, "polarity", self.polarity)
        if "timestamp" in config_keys:
            self.timestamp = partial_config["timestamp"]
            write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        if "active_channel" in config_keys:
            self.active_channel = partial_config["active_channel"]
            write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        if "triggerbit_delay" in config_keys:
            self.triggerbit_delay = partial_config["triggerbit_delay"]
            write_config_reg_decoded(self.connection_socket, "triggerbit_delay", self.triggerbit_delay)
        if "counter_duration" in config_keys:
            self.counter_duration = partial_config["counter_duration"]
            write_config_reg_decoded(self.connection_socket, "counter_duration", self.counter_duration)
        if "fc_delays" in config_keys:
            self.fc_delays = partial_config["fc_delays"]
            write_config_reg_decoded(self.connection_socket, "fc_delays", self.fc_delays)
        if "data_delays_01" in config_keys:
            self.data_delays_01 = partial_config["data_delays_01"]
            write_config_reg_decoded(self.connection_socket, "data_delays_01", self.data_delays_01)
        if "data_delays_23" in config_keys:
            self.data_delays_23 = partial_config["data_delays_23"]
            write_config_reg_decoded(self.connection_socket, "data_delays_23", self.data_delays_23)
        if "prescale_factor" in config_keys:
            self.prescale_factor = partial_config["prescale_factor"]
            write_config_reg_decoded(self.connection_socket, "register_10", 0x000, prescale_factor = self.prescale_factor)
        if "fast_command_memo" in config_keys:
            self.fast_command_memo = partial_config["fast_command_memo"]
        self.configure_memo_FC()
        self.log.info(f"FPGA Registers and Fast Command reconfigured")
        self.print_all_config_params()
        return f"Reconfigured - FPGA Registers and Fast Command reconfigured"

    def do_starting(self, run_identifier: str) -> str:
        """
        move to data taking position
        """
        # Packacking the BOR Message
        self.run_start_time= time.strftime("%Y-%m-%d-%H%M%S", time.localtime())
        self.run_identifier = run_identifier
        tmp_BOR = {}
        tmp_BOR["start_time"] = self.run_start_time
        tmp_BOR["run_identifier"] = self.run_identifier
        # for reg in range(7,16):
        #     tmp_BOR[f"register_{reg}"] = read_config_reg(self.connection_socket, reg)
        self.BOR = tmp_BOR
        time.sleep(0.1)  # add sleep to make sure that everything has stopped

        # FPGA Presteps for DAQ
        if(self.reset_counter):
            write_pulse_reg_decoded(self.connection_socket, "reset_counter")
            time.sleep(0.1)
            self.log.info("Cleared Event Counter")
        # Start DAQ Session on FPGA
        self.log.info("Starting DAQ Session on FPGA...")
        modified_timestamp = format(self.timestamp, '016b')
        modified_timestamp = modified_timestamp[:-2] + '10'
        write_config_reg_decoded(self.connection_socket, "timestamp", int(modified_timestamp, base=2))
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle before Start Pulse: {format(read_status_reg(self.connection_socket, 5), '016b')}")
        write_pulse_reg_decoded(self.connection_socket, "start_DAQ")
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle after Start Pulse: {format(read_status_reg(self.connection_socket, 5), '016b')}")

        return f"Run {run_identifier} Session Started"

    #     self.EOR = {"start_of_loop_time": self.start_of_loop_time, "end_of_loop_time": self.end_of_loop_time}
    def do_stopping(self) -> str:
        """End the run. Add run metadata for end-of-run event"""
        time.sleep(0.1)
        # Stop DAQ Session on FPGA
        self.log.info("Stopping DAQ on FPGA...")
        modified_timestamp = format(self.timestamp, '016b')
        modified_timestamp = modified_timestamp[:-2] + '10'
        write_config_reg_decoded(self.connection_socket, "timestamp", int(modified_timestamp, base=2))
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle before Stop Pulse: {format(read_status_reg(self.connection_socket, 5), '016b')}")
        write_pulse_reg_decoded(self.connection_socket, "stop_DAQ")
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle after Stop Pulse: {format(read_status_reg(self.connection_socket, 5), '016b')}")

        # Packacking the EOR Message
        tmp_EOR = {}
        tmp_EOR["stop_time"] = time.strftime("%Y-%m-%d-%H%M%S", time.localtime())
        for reg in range(7,16):
            tmp_EOR[f"register_{reg}"] = read_config_reg(self.connection_socket, reg)
        self.EOR = tmp_EOR
        return f"Run {self.run_identifier} Stopped, EOR Sent"

    def do_run(self, payload: any) -> str:
        """Run the satellite. Collect data from buffers and send it."""
        self.log.debug("ETROC2Classic satellite running, publishing events...")
        while not self._state_thread_evt.is_set():
            # Main DAQ-loop
            mem_data = np.array(read_data_fifo(self.connection_socket, self.num_fifo_read))
            if mem_data.size == 0:
                self.log.debug("No data in buffer! Will try to read again")
                time.sleep(1.01)
                continue
            # Include data type as part of meta
            meta = {
                # "dtype": f'''{np.dtype('S4')}''',
                "dtype": f"{mem_data.dtype}",
            }
            # Format payload to serializable
            self.data_queue.put((mem_data.tobytes(), meta))
        return "Finished acquisition"

    @cscp_requestable
    def get_config_register(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Return the requested Config Register from the FPGA.
        """
        reg = request.payload
        return "FPGA is Ready", format(read_config_reg(self.connection_socket, reg), '016b'), {}
    def _get_config_register_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]

    @cscp_requestable
    def get_status_register(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Return the requested Status Register from the FPGA.
        """
        reg = request.payload
        return "FPGA is Ready", format(read_status_reg(self.connection_socket, reg), '016b'), {}
    def _get_status_register_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]

    @cscp_requestable
    def set_data_phase_delay(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the Data Phase Delay for DAQ using Reg 13
        """
        data_delay = request.payload
        timestamp = ((data_delay<<7)+(127)+(7<<13)) & (self.timestamp | (63<<7))
        self.timestamp = timestamp
        write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        return "FPGA Reg 13 Set, Data Delay Set", format(read_config_reg(self.connection_socket, 13), '016b'), {}
    def _set_data_phase_delay_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]

    @cscp_requestable
    def set_data_phase_channel_delay(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the Data Phase Delay for specified channel for DAQ using Reg 5 or 6
        """
        data_delay, channel = request.payload[0], request.payload[1]
        if(data_delay>39): data_delay=39
        if(data_delay<0):  data_delay=0
        if(channel>3): channel=3
        if(channel<0): channel=0
        if(channel<2):
            shift = 0 if channel == 0 else 8
            data_delays_01 = ((int(2**(16-shift-6) - 1)<<int(shift+6)) + (data_delay<<shift) + int(2**(shift) - 1)) & (self.data_delays_01 | (63<<shift))
            self.data_delays_01 = data_delays_01
            write_config_reg_decoded(self.connection_socket, "data_delays_01", self.data_delays_01)
            return "FPGA Reg 5 Set, Data Delay Set", format(read_config_reg(self.connection_socket, 5), '016b'), {}
        else:
            shift = 0 if channel == 2 else 8
            data_delays_23 = ((int(2**(16-shift-6) - 1)<<int(shift+6)) + (data_delay<<shift) + int(2**(shift) - 1)) & (self.data_delays_23 | (63<<shift))
            self.data_delays_23 = data_delays_23
            write_config_reg_decoded(self.connection_socket, "data_delays_23", self.data_delays_23)
            return "FPGA Reg 6 Set, Data Delay Set", format(read_config_reg(self.connection_socket, 6), '016b'), {}
    def _set_data_phase_channel_delay_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]

    @cscp_requestable
    def set_fc_phase_delay(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the Fast Command Phase Delay for DAQ using Reg 7
        """
        fc_delay = request.payload
        if(fc_delay>63): fc_delay=63
        if(fc_delay<0):  fc_delay=0
        counter_duration = ((fc_delay<<10)+1023) & (self.counter_duration | (63<<10))
        self.counter_duration = counter_duration
        write_config_reg_decoded(self.connection_socket, "counter_duration", self.counter_duration)
        return "FPGA Reg 7 Set, FC Phase Delay Set", format(read_config_reg(self.connection_socket, 7), '016b'), {}
    def _set_fc_phase_delay_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]

    @cscp_requestable
    def set_fc_phase_channel_delay(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the Fast Command Phase Delay for specified channel in Reg 4
        """
        fc_delay, channel = request.payload[0], request.payload[1]
        if(fc_delay>15): fc_delay=15
        if(fc_delay<0):  fc_delay=0
        if(channel>3): channel=3
        if(channel<0): channel=0
        fc_delays = ((int(2**(4*(3-channel)) - 1)<<int(4*(channel+1))) + (fc_delay<<int(4*channel)) + int(2**(4*channel) - 1)) & (self.fc_delays | (15<<int(4*channel)))
        self.fc_delays = fc_delays
        write_config_reg_decoded(self.connection_socket, "fc_delays", self.fc_delays)
        return "FPGA Reg 4 Set, FC Phase Delay Set", format(read_config_reg(self.connection_socket, 4), '016b'), {}
    def _set_fc_phase_channel_delay_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]

    @cscp_requestable
    def set_fc_bit_delay(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the Fast Command Bit Delay for DAQ using Reg 14
        """
        bit_delay = request.payload
        polarity = ((bit_delay<<10)+(1023)+(3<<14)) & (self.polarity | (15<<10))
        self.polarity = polarity
        write_config_reg_decoded(self.connection_socket, "polarity", self.polarity)
        return "FPGA Reg 14 Set, FC Bit Delay Set", format(read_config_reg(self.connection_socket, 14), '016b'), {}
    def _set_fc_bit_delay_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]

    # @schedule_metric("lm", MetricsType.LAST_VALUE, 10)
    # def brightness(self) -> int | None:
    #     if self.fsm.current_state_value in [
    #         SatelliteState.NEW,
    #         SatelliteState.ERROR,
    #         SatelliteState.DEAD,
    #         SatelliteState.initializing,
    #         SatelliteState.reconfiguring,
    #     ]:
    #         return None
    #     return self.device.get_current_brightness()
