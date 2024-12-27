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
from .daq_helpers import *
import numpy as np

from constellation.core.cmdp import MetricsType
from constellation.core.commandmanager import cscp_requestable
from constellation.core.configuration import Configuration
from constellation.core.cscp import CSCPMessage
from constellation.core.fsm import SatelliteState
from constellation.core.monitoring import schedule_metric
from constellation.core.satellite import Satellite

class ETROC2Classic(Satellite):
    """Example for a Satellite class."""

    def print_all_config_params(self) -> None:
        self.log.debug("Printing All Config Params...")
        self.log.debug(f"hostname: {self.hostname}")
        self.log.debug(f"port: {self.port}")
        self.log.debug(f"polarity: {self.polarity}")
        self.log.debug(f"firmware: {self.firmware}")
        self.log.debug(f"timestamp: {self.timestamp}")
        self.log.debug(f"active_channel: {self.active_channel}")
        self.log.debug(f"prescale_factor: {self.prescale_factor}")
        self.log.debug(f"counter_duration: {self.counter_duration}")
        self.log.debug(f"trigger_bit_delay: {self.trigger_bit_delay}")
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
            register_11(self.connection_socket, 0x0deb)
            time.sleep(0.01)

        # IDLE
        register_12(self.connection_socket, 0x0070 if Triggerbit else 0x0030)
        register_10(self.connection_socket, self.prescale_factor, 0x000)
        register_9(self.connection_socket, 0xdeb)
        fc_init_pulse(self.connection_socket)
        time.sleep(0.01)

        if(BCR):
            # BCR
            register_12(self.connection_socket, 0x0072 if Triggerbit else 0x0032)
            register_10(self.connection_socket, self.prescale_factor, 0x000)
            register_9(self.connection_socket, 0x000)
            fc_init_pulse(self.connection_socket)
            time.sleep(0.01)

        # QInj FC
        if(QInj):
            register_12(self.connection_socket, 0x0075 if Triggerbit else 0x0035)
            register_10(self.connection_socket, self.prescale_factor, 0x005)
            register_9(self.connection_socket, 0x005)
            fc_init_pulse(self.connection_socket)
            time.sleep(0.01)
            if(repeatedQInj):
                interval = (3000//16)//qinj_loop
                for i in range(qinj_loop):
                    register_12(self.connection_socket, 0x0075 if Triggerbit else 0x0035)
                    if not (uniform_mode):
                        register_10(self.connection_socket, self.prescale_factor, 0x005 + i*0x010)
                        register_9(self.connection_socket, 0x005 + i*0x010)
                        fc_init_pulse(self.connection_socket)
                        time.sleep(0.01)
                    else:
                        register_10(self.connection_socket, self.prescale_factor, 0x005 + interval * i*0x010)
                        register_9(self.connection_socket, 0x005 + interval * i*0x010)
                        fc_init_pulse(self.connection_socket)
                        time.sleep(0.01)
                        pass

        ### Send L1A
        if(L1A):
            register_12(self.connection_socket, 0x0076 if Triggerbit else 0x0036)
            register_10(self.connection_socket, self.prescale_factor, 0x1fd)
            register_9(self.connection_socket, 0x1fd)
            fc_init_pulse(self.connection_socket)
            time.sleep(0.01)

            ### Send L1A Range
            if(L1ARange):
                interval = (3000//16)//qinj_loop
                for i in range(qinj_loop):
                    register_12(self.connection_socket, 0x0076 if Triggerbit else 0x0036)
                    if not (uniform_mode):
                        register_10(self.connection_socket, self.prescale_factor, 0x1fd + i*0x010)
                        register_9(self.connection_socket, 0x1fd + i*0x010)
                        fc_init_pulse(self.connection_socket)
                        time.sleep(0.01)
                    else:
                        register_10(self.connection_socket, self.prescale_factor, 0x1fd + interval * i*0x010)
                        register_9(self.connection_socket, 0x1fd + interval * i*0x010)
                        fc_init_pulse(self.connection_socket)
                        time.sleep(0.01)


        fc_signal_start(self.connection_socket)
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
        self.trigger_bit_delay = config.setdefault("trigger_bit_delay", 0x1800)
        self.num_fifo_read = config.setdefault("num_fifo_read", 65536)
        self.clear_fifo = config.setdefault("clear_fifo", True) 
        self.reset_counter = config.setdefault("reset_counter", True) 
        self.fast_command_memo = config.setdefault("fast_command_memo", "Start Triggerbit") 
        self.connection_socket = None
        # TODO check for valid entries for all config keys
        if self.prescale_factor not in [2048, 4096, 8192, 16384]:
            raise ValueError(f"Prescale factor must be one of [2048, 4096, 8192, 16384], {self.prescale_factor} not supported")
        # if ' '.join(message.split(' ')[:1]) == 'memoFC':
        self.log.info(f"Configuration loaded and Defaults set")
        self.print_all_config_params()
        return "Initialized"
    
    def do_launching(self) -> str:
        try:
            self.connection_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except socket.error:
            raise RuntimeError("Failed to create socket for ETROC2Classic Satellite")
        try:
            self.connection_socket.connect((self.hostname, self.port))
        except socket.error:
            raise ConnectionError(f"Failed to connect to IP: {self.hostname}:{self.port}")
        # print("Setting firmware...")
        active_channels(self.connection_socket, key = self.active_channel)
        timestamp(self.connection_socket, key = self.timestamp)
        triggerBitDelay(self.connection_socket, key = self.trigger_bit_delay)
        register_10(self.connection_socket, prescale_factor = self.prescale_factor)
        Enable_FPGA_Descramblber(self.connection_socket, val = self.polarity)
        # if(options.counter_duration):
        #     print("Setting Counter Duration and Channel Delays...")
        counterDuration(self.connection_socket, key = self.counter_duration)
        if(self.clear_fifo):
            self.log.info("Clearing FIFO...")
            software_clear_fifo(self.connection_socket)
            time.sleep(2.1)
            self.log.info("Waited for 2.1 secs")
        self.configure_memo_FC()
        self.log.info(f"Socket connected, FPGA Registers and Fast Command configured")
        return f"Launched"

    def do_landing(self) -> str:
        self.configure_memo_FC(memo="Triggerbit")
        self.connection_socket.shutdown(socket.SHUT_RDWR)
        self.connection_socket.close()
        self.log.info(f"Socket shutdown and closed, Fast Command idling")
        return f"Landed"

    def do_reconfigure(self, partial_config: Configuration) -> str:
        config_keys = partial_config.get_keys()
        if "hostname" in config_keys:
            raise ValueError("Reconfiguring hostname is not possible")
        if "port" in config_keys:
            raise ValueError("Reconfiguring port is not possible")
        if "polarity" in config_keys:
            self.polarity = partial_config["polarity"]
            Enable_FPGA_Descramblber(self.connection_socket, val = self.polarity)
        if "timestamp" in config_keys:
            self.timestamp = partial_config["timestamp"]
            timestamp(self.connection_socket, key = self.timestamp)
        if "active_channels" in config_keys:
            self.active_channels = partial_config["active_channels"]
            active_channels(self.connection_socket, key = self.active_channels)
        if "trigger_bit_delay" in config_keys:
            self.trigger_bit_delay = partial_config["trigger_bit_delay"]
            triggerBitDelay(self.connection_socket, key = self.trigger_bit_delay)
        if "counter_duration" in config_keys:
            self.counter_duration = partial_config["counter_duration"]
            counterDuration(self.connection_socket, key = self.counter_duration)
        if "prescale_factor" in config_keys:
            self.prescale_factor = partial_config["prescale_factor"]
            register_10(self.connection_socket, prescale_factor = self.prescale_factor)
        if "fast_command_memo" in config_keys:
            self.fast_command_memo = partial_config["fast_command_memo"]
        self.configure_memo_FC()
        self.log.info(f"FPGA Registers and Fast Command reconfigured")
        self.print_all_config_params()
        return f"Reconfigured"
    
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
            software_clear_error(self.connection_socket)
            time.sleep(0.1)
            self.log.info("Cleared Event Counter")
        # Start DAQ Session on FPGA
        self.log.info("Starting DAQ Session on FPGA...")
        modified_timestamp = format(self.timestamp, '016b')
        modified_timestamp = modified_timestamp[:-2] + '10'
        timestamp(self.connection_socket, key = int(modified_timestamp, base=2))
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle before Start Pulse: {format(read_status_reg(self.connection_socket, 5), '016b')}")
        start_DAQ_pulse(self.connection_socket)
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
        timestamp(self.connection_socket, key = int(modified_timestamp, base=2))
        time.sleep(0.1)
        self.log.debug(f"Status of DAQ Toggle before Stop Pulse: {format(read_status_reg(self.connection_socket, 5), '016b')}")
        stop_DAQ_pulse(self.connection_socket)
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
            self.log.debug(f"Top of the data block: {mem_data[0]}")
            # Include data type as part of meta
            meta = {
                "dtype": f"{mem_data.dtype}",
            }
            # Format payload to serializable
            # self.data_queue.put((mem_data.tobytes(), meta))
        return "Finished acquisition"
    
    @cscp_requestable
    def get_config_register(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Return the requested Config Register from the FPGA.
        """
        # we cannot perform this command when not ready:
        # if self.fsm.current_state_value in [
        #     SatelliteState.NEW,
        #     SatelliteState.ERROR,
        #     SatelliteState.DEAD,
        #     SatelliteState.initializing,
        #     SatelliteState.reconfiguring,
        # ]:
        #     return "FPGA not ready", None, {}
        paramList = request.payload
        reg = paramList[0]
        return "FPGA is Ready", format(read_config_reg(self.connection_socket, reg), '016b'), {}
    def _get_config_register_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]
    
    @cscp_requestable
    def get_status_register(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Return the requested Status Register from the FPGA.
        """
        paramList = request.payload
        reg = paramList[0]
        return "FPGA is Ready", format(read_status_reg(self.connection_socket, reg), '016b'), {}
    def _get_status_register_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]
        
    # def reentry(self) -> None:
    #     if hasattr(self, "device"):
    #         self.device.release()
    #     super().reentry()

    # def do_interrupting(self) -> str:
    #     """Power down but do not disconnect (e.g. keep monitoring)."""
    #     self._power_down()
    #     return "Interrupted and stopped HV."

    # def fail_gracefully(self):
    #     """Kill HV and disconnect."""
    #     if getattr(self, "caen", None):
    #         self._power_down()
    #         self.caen.disconnect()
    #     return "Powered down and disconnected from crate."

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
