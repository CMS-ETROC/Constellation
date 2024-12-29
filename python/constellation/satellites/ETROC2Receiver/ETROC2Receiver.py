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
import io
import queue

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
        # what pattern to use for the file names?
        self.file_name_pattern = self.config.setdefault("_file_name_pattern", "run_{run_identifier}_{date}.nem")
        # what directory to store files in?
        self.output_path = self.config.setdefault("_output_path", "data")
        self._configure_monitoring(2.0)
        # how often will the file be flushed? Negative values for 'at the end of
        # the run'
        self.flush_interval = self.config.setdefault("flush_interval", 10.0)
        self.file_counter = 0
        self.translate_queue = queue.Queue()
        return "Configured ETROC2Receiver"
    
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

        title = f"data_{self.run_identifier}_{item.sequence_number:09}"
        self.log.info(title)
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

        binary_text =  map(lambda x: format(int(x), '032b'), payload)
        outfile.write("\n".join(list(binary_text)))

        # time to flush data to file?
        if self.flush_interval > 0 and (datetime.datetime.now() - self.last_flush).total_seconds() > self.flush_interval:
            outfile.flush()
            self.last_flush = datetime.datetime.now()

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
            file = open(directory / filename, "w")
        except Exception as exception:
            self.log.critical("Unable to open %s: %s", filename, str(exception))
            raise RuntimeError(
                f"Unable to open {filename}: {str(exception)}",
            ) from exception
        return file

    def _close_file(self, outfile: io.IOBase) -> None:
        """Close the filehandler"""
        outfile.close()
