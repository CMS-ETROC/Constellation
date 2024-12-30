"""
SPDX-FileCopyrightText: 2024 DESY and the Constellation authors
SPDX-License-Identifier: CC-BY-4.0

Provides the class for the Mariner example satellite
"""

import random
import time
import os
import socket
from .daq_helpers import *
import numpy as np
from numpy.typing import NDArray
import io
import struct

from constellation.core.configuration import Configuration
from constellation.core.monitoring import schedule_metric
from constellation.core.datareceiver import DataReceiver

import datetime
import pathlib
import sys
import threading

import zmq
from uuid import UUID
from functools import partial
from typing import Any, Tuple

from constellation.core.broadcastmanager import chirp_callback, DiscoveredService
from constellation.core.cdtp import CDTPMessage, CDTPMessageIdentifier, DataTransmitter
from constellation.core.cmdp import MetricsType
from constellation.core.chirp import CHIRPServiceIdentifier
from constellation.core.commandmanager import cscp_requestable
from constellation.core.cscp import CSCPMessage
from constellation.core.fsm import SatelliteState
from constellation.core.satellite import Satellite

class ETROC2Receiver(DataReceiver):
    """Satellite which receives data via ZMQ and writes to ETROC2Classic nem type."""

    def do_initializing(self, config: dict[str, Any]) -> str:
        """Initialize and configure the satellite."""
        # what directory to store files in?
        self.output_path = self.config.setdefault("_output_path", "data")
        # Do you want to translate the received data and store that instead?
        self.translate = self.config.setdefault("translate", 1)
        self.compressed_binary = self.config.setdefault("compressed_binary", 1)
        # what pattern to use for the file names?
        if(self.translate):
            self.file_name_pattern = self.config.setdefault("_file_name_pattern", "run_{run_identifier}_{date}.nem")
        else:
            self.file_name_pattern = self.config.setdefault("_file_name_pattern", "run_{run_identifier}_{date}.bin")
        # Do you want to skip fillers in the translated files?
        self.skip_fillers = self.config.setdefault("skip_fillers", 0)
        self._configure_monitoring(2.0)
        # how often will the file be flushed? Negative values for 'at the end of the run'
        self.flush_interval = self.config.setdefault("flush_interval", 10.0)
        # Defaults
        self.frame_trailers = self.config.setdefault("frame_trailers", {0:0x17f0f,1:0x17f0f,2:0x17f0f,3:0x17f0f})
        self.fixed_patterns = {
            "clk2_filler":   0x553,     # first 12 bits
            "fifo_filler":   0x556,     # first 12 bits
            "time_filler":   0x559,     # first 12 bits
            "event_header":  0xc3a3c3a, # first 28 bits
            "firmware_key":  0x1,       # first 4  bits
            "event_trailer": 0xb,       # first 6  bits
            "frame_header":  0x3c5c,    # first 16 bits + '00'
            "frame_data":    0x1,
        }
        self.fixed_pattern_sizes = {
            "clk2_filler":   12,     # first 12 bits
            "fifo_filler":   12,     # first 12 bits
            "time_filler":   12,     # first 12 bits
            "event_header":  28,     # first 28 bits
            "firmware_key":  4,      # first 4 bits
            "event_trailer": 6,      # first 6  bits
            "frame_header":  18,     # first 16 bits + '00'
            "frame_trailer": 18,     # first 18 bits
            "frame_data":    1,      # first 1 bit
        }
        self.file_counter = 0
        self.translate_int = np.uint64(0)
        self.active_channels = []
        self.active_channel  = -1
        self.active_channels_extend = self.active_channels.extend
        self.active_channels_pop = self.active_channels.pop
        self.active_channels_clear = self.active_channels.clear
        self.translate_state = [False, "", ""] # [in_event, previous_state, previous_filler]
        self.event_stats     = [-1, -1, -1]    # [40bit_state, num_32bit_words, current_word]
        self.buffer_shifts = {
            1:24,
            2:16,
            3:8,
            4:0
        }
        return "Configured ETROC2Receiver"
    
    def _reset_params(self) -> None:
        self.translate_int = np.uint64(0)
        self.active_channels_clear()
        self.active_channel  = -1
        self.translate_state[0] = False
        self.event_stats[0] = -1
        self.event_stats[1] = -1
        self.event_stats[2] = -1
    
    def do_run(self, run_identifier: str) -> str:
        """Handle the data enqueued by the ZMQ Poller."""
        self.last_flush = datetime.datetime.now()
        return super().do_run(run_identifier)

    def _write_EOR(self, outfile: io.IOBase, item: CDTPMessage) -> None:
        """Write data to file"""
        # grp = outfile[item.name].create_group("EOR")
        # grp.update(item.payload)
        self.log.info(
            "Wrote EOR packet from %s on run %s",
            item.name,
            self.run_identifier,
        )

    def _write_BOR(self, outfile: io.IOBase, item: CDTPMessage) -> None:
        """Write BOR to file"""
        # if item.name not in outfile.keys():
            # grp = outfile.create_group(item.name).create_group("BOR")
            # grp.update(item.payload)
        self.log.info(
            "Wrote BOR packet from %s on run %s",
            item.name,
            self.run_identifier,
        )

    def _write_data(self, outfile: io.IOBase, item: CDTPMessage) -> None:
        """
        Write data into nem file
        """
        if item.name not in self.active_satellites:
            self.log.warning(
                "%s sent data but is no longer assumed active (EOR received)",
                item.name,
            )

        # title = f"data_{self.run_identifier}_{item.sequence_number:09}"

        if isinstance(item.payload, bytes):
            # interpret bytes as array of uint8 if nothing else was specified in the meta
            payload = np.frombuffer(item.payload, dtype=item.meta.get("dtype", np.uint8))
        elif isinstance(item.payload, list):
            payload = np.array(item.payload)
        elif item.payload is None:
            # empty payload -> empty array of bytes
            payload = np.array([], dtype=np.uint8)
        else:
            raise TypeError(f"Cannot write payload of type '{type(item.payload)}'")

        if(not self.translate):
            if(self.compressed_binary):
                outfile.write(b''.join(payload))
            else:
                binary_text =  map(lambda x: format(struct.unpack("I",x)[0], '032b'), payload)
                outfile.write("\n".join(list(binary_text)))
        else:
            self._translate_and_write(outfile, payload)

        # time to flush data to file?
        if self.flush_interval > 0 and (datetime.datetime.now() - self.last_flush).total_seconds() > self.flush_interval:
            outfile.flush()
            self.last_flush = datetime.datetime.now()

    def _translate_and_write(self, outfile: io.IOBase, payload:  NDArray) -> None:
        for line_bin in payload:
            self.log.debug(f"{line_bin}")
            line_int = np.uint64(struct.unpack("I",line_bin)[0])
            # Currently outside of an event
            if(self.translate_state[0] == False):
                # FIFO or fixed TIME Filler
                if(line_int>>32-self.fixed_pattern_sizes["fifo_filler"] == self.fixed_patterns["fifo_filler"] or line_int>>32-self.fixed_pattern_sizes["time_filler"]== self.fixed_patterns["time_filler"]):
                    binary_text = format(line_int, '032b')[self.fixed_pattern_sizes["fifo_filler"]:]
                    filler_type = "FIFO" if (line_int>>32-self.fixed_pattern_sizes["fifo_filler"] == self.fixed_patterns["fifo_filler"]) else "CLOCK"
                    if(self.translate_state[2] != binary_text):
                        self.log.info(f"Link Status: {binary_text[0:4]} {binary_text[4:12]}, Reset Counter: {int(binary_text[12:],2)}")
                        self.translate_state[2] = binary_text
                    if(not self.skip_fillers):
                        outfile.write(f"{filler_type} {binary_text[0:4]} {binary_text[4:12]} {int(binary_text[12:],2)}\n")
                    self.translate_state[1] = "FILLER"
                # CLOCK2 Filler
                elif(line_int>>32-self.fixed_pattern_sizes["clk2_filler"] == self.fixed_patterns["clk2_filler"]):
                    binary_text = format(line_int, '032b')[self.fixed_pattern_sizes["clk2_filler"]:]
                    if(not self.skip_fillers):
                        outfile.write(f"CLOCK2 {binary_text}\n")
                    self.translate_state[1] = "FILLER"
                # Event Header, forces transition into event state
                elif(line_int>>32-self.fixed_pattern_sizes["event_header"] == self.fixed_patterns["event_header"]):
                    # self._reset_params()
                    self.translate_state[0] = True
                    self.translate_state[1] = "HEADER_1"
                    binary_text = format(line_int & 0xF, '04b')
                    self.active_channels_extend([key for key,val in enumerate(binary_text[::-1]) if val=='1'][::-1])
            # Currently inside of an event
            else:
                # Upon first entry, check if HEADER_2 found, else bail out
                if(self.translate_state[1] == "HEADER_1"):
                    if(line_int>>32-self.fixed_pattern_sizes["firmware_key"] == self.fixed_patterns["firmware_key"]):
                        self.translate_state[1] = "HEADER_2"
                        num_words = (line_int>>2) & 0x3FF
                        self.event_stats[1] = -(40*num_words//(-32)) # div ceil -(x//(-y))
                        self.event_stats[2] += 1
                        # outfile.write(f"EH {event_num} {event_type} {num_words}\n")
                        outfile.write(f"EH {(line_int>>12)&0xFFFF} {line_int & 0x3} {num_words} {self.event_stats[1]}\n")
                    else:
                        self._reset_params()
                        outfile.write(f"BROKEN EVENT HEADER!\n")
                # Translate ETROC2 Frames after HEADER_2
                elif(self.translate_state[1] == "HEADER_2"):                    
                    self.event_stats[2] += 1
                    self.translate_int = ((self.translate_int << 32) + np.uint64(line_int) )
                    self.event_stats[0] = (self.event_stats[0]+1)%5
                    if(self.event_stats[0]>0):
                        to_be_translated = self.translate_int >> self.buffer_shifts[self.event_stats[0]]
                        self.translate_int = (self.translate_int & ((1<<self.buffer_shifts[self.event_stats[0]]) -1))
                        # HEADER "H {channel} {L1Counter} {Type} {BCID}"
                        if(to_be_translated>>40-self.fixed_pattern_sizes["frame_header"] == self.fixed_patterns["frame_header"]<<2):
                            try:
                                self.active_channel=self.active_channels_pop()
                            except IndexError:
                                self.active_channel=-1
                            outfile.write(f"H {self.active_channel} {(to_be_translated>>14) & 0xFF} {(to_be_translated>>12) & 0x3} {to_be_translated & 0xFFF}\n")
                        # DATA "D {channel} {EA} {ROW} {COL} {TOA} {TOT} {CAL}"
                        elif(to_be_translated>>40-self.fixed_pattern_sizes["frame_data"] == self.fixed_patterns["frame_data"]):
                            outfile.write(f"D {self.active_channel} {(to_be_translated >> 37) & 0x3} {(to_be_translated >> 29) & 0xF} {(to_be_translated >> 33) & 0xF} {(to_be_translated >> 19) & 0x3FF} {(to_be_translated >> 10) & 0x1FF} {to_be_translated & 0x3FF}\n")
                        # TRAILER "T {channel} {Status} {Hits} {CRC}"
                        elif(to_be_translated>>40-self.fixed_pattern_sizes["frame_trailer"] == self.frame_trailers[self.active_channel]):
                            outfile.write(f"T {self.active_channel} {(to_be_translated >> 16) & 0x3F} {(to_be_translated >> 8) & 0xFF} {to_be_translated & 0xFF}\n")
                        else:
                            outfile.write(f"UNKNOWN 40 bit word!\n")
                    if(self.event_stats[2] == self.event_stats[1]):
                        self.translate_state[1] = "ETROC2"
                # Translate Event Trailer after ETROC2 Frames
                elif(self.translate_state[1] == "ETROC2"):
                    if(line_int>>32-self.fixed_pattern_sizes["event_trailer"] == self.fixed_patterns["event_trailer"]):
                        self.translate_state[1] = "TRAILER"
                        # outfile.write(f"ET {num_hits} {overflow_count} {hamming_count} {crc}\n")
                        outfile.write(f"ET {(line_int >> 14) & 0xFFF} {(line_int >> 11) & 0x7} {(line_int >> 8) & 0x7} {line_int & 0xFF}\n")
                    else:
                        outfile.write(f"BROKEN EVENT NO EVENT TRAILER FOUND!\n")
                    self._reset_params()
                else:
                    self._reset_params()
                    outfile.write(f"BROKEN EVENT... How did we get here?...\n")


    def _open_file(self, filename: pathlib.Path) -> io.IOBase:
        """Open the nem file and return the file object."""
        file = None
        if os.path.isfile(filename):
            self.log.critical("file already exists: %s", filename)
            raise RuntimeError(f"file already exists: {filename}")

        self.log.info("Creating file %s", filename)
        # Create directory path.
        directory = pathlib.Path(self.output_path)  # os.path.dirname(filename)
        try:
            os.makedirs(directory)
        except (FileExistsError, FileNotFoundError):
            self.log.info("Directory %s already exists", directory)
            pass
        except Exception as exception:
            raise RuntimeError(
                f"unable to create directory {directory}: \
                {type(exception)} {str(exception)}"
            ) from exception
        try:
            if(self.translate or (not self.compressed_binary)):
                file = open(directory / filename, "w")
            else:
                file = open(directory / filename, "wb")
        except Exception as exception:
            self.log.critical("Unable to open %s: %s", filename, str(exception))
            raise RuntimeError(
                f"Unable to open {filename}: {str(exception)}",
            ) from exception
        return file

    def _close_file(self, outfile: io.IOBase) -> None:
        """Close the filehandler"""
        outfile.close()
