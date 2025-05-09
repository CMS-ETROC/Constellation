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
from .ws_testing import *
import numpy as np

from constellation.core.cmdp import MetricsType
from constellation.core.commandmanager import cscp_requestable
from constellation.core.configuration import Configuration
from constellation.core.cscp import CSCPMessage
from constellation.core.fsm import SatelliteState
from constellation.core.monitoring import schedule_metric
from constellation.core.satellite import Satellite

class ETROC2Waveform(Satellite):
    """Example for a Satellite class."""

    def print_all_config_params(self) -> None:
        self.log.info("Printing All Config Params...")
        self.log.info(f"hostname: {self.hostname}")
        self.log.info(f"port: {self.port}")
        self.log.info(f"polarity: {hex(self.polarity)}")
        self.log.info(f"firmware: {self.firmware}")
        self.log.info(f"timestamp: {hex(self.timestamp)}")
        self.log.info(f"active_channel: {hex(self.active_channel)}")
        self.log.info(f"prescale_factor: {self.prescale_factor}")
        self.log.info(f"counter_duration: {hex(self.counter_duration)}")
        self.log.info(f"triggerbit_delay: {hex(self.triggerbit_delay)}")
        self.log.info(f"fc_delays: {hex(self.fc_delays)}")
        self.log.info(f"data_delays_01: {hex(self.data_delays_01)}")
        self.log.info(f"data_delays_23: {hex(self.data_delays_23)}")
        self.log.info(f"num_fifo_read: {self.num_fifo_read}")
        self.log.info(f"clear_fifo: {self.clear_fifo}")
        self.log.info(f"reset_counter: {self.reset_counter}")
        self.log.info(f"i2c_port: {self.i2c_port}")
        self.log.info(f"chip_addresses: {self.chip_addresses}")
        self.log.info(f"ws_addresses: {self.ws_addresses}")
        self.log.info(f"chip_names: {self.chip_names}")
        self.log.info(f"ws_active_channel: {hex(self.ws_active_channel)}")
        self.log.info(f"output_path: {self.output_path}")
        self.log.info(f"save_html: {self.save_html}")

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
        self.active_boards = config.setdefault("active_boards", 1)
        self.i2c_port = config.setdefault("i2c_port", "/dev/ttyACM0")
        self.chip_addresses = config.setdefault("chip_addresses", [0x60])
        self.ws_addresses = config.setdefault("ws_addresses", [0x40])
        self.chip_names = config.setdefault("chip_names", ["c1","c2","c3","c4"])
        self.ws_active_channel = config.setdefault("ws_active_channel", 0x0091)
        self.output_path = self.config.setdefault("output_path", "waveforms")
        self.file_name_pattern = self.config.setdefault("file_name_pattern", "{run_identifier}/waveform_{date}")
        self.save_html = config.setdefault("save_html", 1)
        self.file_counter = 0

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
            self.i2c_conn = i2c_connection(self.i2c_port,self.chip_addresses,self.ws_addresses,self.chip_names)
        except socket.error:
            raise RuntimeError("Failed to create I2C GUI Conn Object for ETROC2Waveform Satellite")
        self.i2c_conn.start_ws_sampling()
        try:
            self.connection_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except socket.error:
            raise RuntimeError("Failed to create socket for ETROC2Waveform Satellite")
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
        # write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")
        self.log.info(f"I2C Obj made, WS Config, Socket connected, FPGA Registers, Fast Command configured")
        return f"Launched - Socket connected, FPGA Registers and Fast Command configured"

    def do_landing(self) -> str:
        self.i2c_conn.stop_ws_sampling()
        self.configure_memo_FC(memo="Triggerbit")
        self.connection_socket.shutdown(socket.SHUT_RDWR)
        self.connection_socket.close()
        self.log.info(f"Socket shutdown and closed, Fast Command idling")
        del self.i2c_conn
        return f"Landed - Socket shutdown and closed, Fast Command idling, I2C Obj deleted"

    def do_reconfigure(self, partial_config: Configuration) -> str:
        config_keys = partial_config.get_keys()
        if len(config_keys)==0:
            self.log.info(f"No Reconfiguration of ETROC2Satellite {self.get_name()} requested in config.")
            return f"No Reconfiguration requested in config."
        if "hostname" in config_keys:
            raise ValueError("Reconfiguring hostname is not possible")
        if "port" in config_keys:
            raise ValueError("Reconfiguring port is not possible")
        if "chip_names" in config_keys:
            raise ValueError("Reconfiguring chip_names is not possible")
        if "i2c_port" in config_keys:
            raise ValueError("Reconfiguring i2c_port is not possible")
        if "chip_addresses" in config_keys:
            raise ValueError("Reconfiguring chip_addresses is not possible")
        if "ws_addresses" in config_keys:
            raise ValueError("Reconfiguring ws_addresses is not possible")
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
        self.file_counter = 0
        filename_list = self.file_name_pattern.format(
                            run_identifier=self.run_identifier,
                            date=self.file_counter,
                        ).split('/')
        self.directory = pathlib.Path(self.output_path) / pathlib.Path(filename_list[0]) 
        self.log.info(f"Creating files in {self.output_path}/{filename_list[0]}...")
        try:
            os.makedirs(self.directory, exist_ok=True)
        except Exception as exception:
            raise RuntimeError(
                f"unable to create directory {self.directory}: \
                {type(exception)} {str(exception)}"
            ) from exception

        self.i2c_conn.start_ws_sampling()
        write_config_reg_decoded(self.connection_socket, "active_channel", self.ws_active_channel)
        time.sleep(0.1)
        # if(self.clear_fifo):
        #     self.log.info("Clearing FIFO...")
        #     write_pulse_reg_decoded(self.connection_socket, "clear_fifo")
        #     time.sleep(2.1)

        # self.configure_memo_FC()

        # write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")

        return f"Run {run_identifier} Session Started"

    #     self.EOR = {"start_of_loop_time": self.start_of_loop_time, "end_of_loop_time": self.end_of_loop_time}
    def do_stopping(self) -> str:
        """End the run. Add run metadata for end-of-run event"""
        time.sleep(0.1)
        write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        # write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")
        time.sleep(0.1)
        return f"Run {self.run_identifier} Stopped"

    def do_run(self, payload: any) -> str:
        """Run the satellite. Collect data from buffers and send it."""
        self.log.info("ETROC2Waveform satellite running, collecting waveforms...")
        while not self._state_thread_evt.is_set():
            self.file_counter += 1
            filename_list = self.file_name_pattern.format(
                                run_identifier=self.run_identifier,
                                date=self.file_counter,
                            ).split('/')
            basefilename = filename_list[1]
            for chip_address,ws_address,chip_name in zip(self.chip_addresses,self.ws_addresses,self.chip_names):
                df = self.i2c_conn.read_chip_ws(chip_address,ws_address)
                filename = pathlib.Path(basefilename + f"_rawData_{chip_name}.csv")
                if os.path.isfile(self.directory / filename):
                    self.log.critical("file already exists: %s", self.directory / filename)
                    raise RuntimeError(f"file already exists: {self.directory / filename}")
                df.to_csv(self.directory /filename)
                if(self.save_html):
                    fig_dout = px.line(
                        df,
                        x="Time [ns]",
                        y="Dout",
                        labels = {
                            "Time [ns]": "Time [ns]",
                            "Dout": "",
                        },
                        title = "Waveform (Dout) from the board {}".format(chip_name),
                        markers=True
                    )
                    fig_dout.write_html(
                        str(self.directory / pathlib.Path(basefilename + f"_Dout_{chip_name}.html")),
                        full_html = False,
                        include_plotlyjs = 'cdn',
                    )
            self.i2c_conn.start_ws_sampling()
            write_pulse_reg_decoded(self.connection_socket, "clear_ws_block")
            # time.sleep(1)
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
        if(fc_delay>31): fc_delay=31
        if(fc_delay<0):  fc_delay=0
        if(channel>3): channel=3
        if(channel<0): channel=0
        fc_delays = ((int(2**(4*(3-channel)) - 1)<<int(4*(channel+1))) + ((fc_delay&0xf)<<int(4*channel)) + int(2**(4*channel) - 1)) & (self.fc_delays | (15<<int(4*channel)))
        self.fc_delays = fc_delays
        write_config_reg_decoded(self.connection_socket, "fc_delays", self.fc_delays)
        msb = fc_delay>>4
        shift = -1 
        if channel == 0: shift = 6
        elif channel == 1: shift = 7
        elif channel == 2: shift = 14
        elif channel == 3: shift = 15
        data_delays_01 = (msb<<shift) | (self.data_delays_01 & ((~(1<<shift))&0xffff))
        self.data_delays_01 = data_delays_01
        write_config_reg_decoded(self.connection_socket, "data_delays_01", self.data_delays_01)
        return "FPGA Reg 4 and 5 Set, FC Phase Delay Set", [format(read_config_reg(self.connection_socket, 4), '016b'), format(read_config_reg(self.connection_socket, 5), '016b')], {}
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
    
    @cscp_requestable
    def set_ledpage(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the ledpage for DAQ using Reg 13
        """
        ledpage = request.payload
        if(ledpage>5): ledpage=5
        if(ledpage<0): ledpage=0
        timestamp = ((2047<<5) + (ledpage<<2) + (3)) & (self.timestamp | (7<<2))
        self.timestamp = timestamp
        write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        return "FPGA Reg 13 Set, Led Page Set", format(read_config_reg(self.connection_socket, 13), '016b'), {}
    def _set_timestamp_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]
    
    @cscp_requestable
    def set_active_channel(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the active_channel for DAQ using Reg 15
        """
        self.active_channel = request.payload
        write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        return "FPGA Reg 15 Set, active_channel Set", format(read_config_reg(self.connection_socket, 15), '016b'), {}
    def _set_active_channel_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]
    
    @cscp_requestable
    def set_fast_command_memo(self, request: CSCPMessage) -> tuple[str, Any, dict]:
        """
        Set the Fast Command Memo
        """
        self.fast_command_memo = request.payload
        self.configure_memo_FC()
        return "Fast Command Configured", self.fast_command_memo, {}
    def _set_fast_command_memo_is_allowed(self, request: CSCPMessage) -> bool:
        """Allow in the state ORBIT only, when the socket is connected to the FPGA"""
        return self.fsm.current_state.id in ["ORBIT"]