#!/usr/bin/env python3
"""
SPDX-FileCopyrightText: 2024 DESY and the Constellation authors
SPDX-License-Identifier: CC-BY-4.0

This module provides the class for a Constellation Satellite.
"""

from constellation.core.satellite import Satellite
import time
import logging
from constellation.core.configuration import ConfigError, Configuration


class Example_Device1:

    def set_config(self, config: Configuration):
        self.voltage = config["voltage"]
        self.ampere = config["ampere"]
        self.sample_period = config.setdefault("sample_period", default=0.1)


class Example_Satellite(Satellite):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = Example_Device1()

    def do_initializing(self, payload: dict) -> str:
        super().do_initializing(payload=payload)
        try:
            self.device.set_config(self.config)
        except KeyError as e:
            self.log.error("Configuration has no attribute %s", e)
            raise ConfigError

        return "Initialized"

    def do_run(self, payload: any) -> str:
        while not self._state_thread_evt.is_set():
            time.sleep(self.device.sample_period)
            print(f"New sample at {self.device.voltage}")
        return "Finished acquisition."


# -------------------------------------------------------------------------


def main(args=None):
    """Start the base Satellite server."""
    import argparse
    import coloredlogs

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--cmd-port", type=int, default=23999)
    parser.add_argument("--mon-port", type=int, default=55556)
    parser.add_argument("--hb-port", type=int, default=61234)
    parser.add_argument("--interface", type=str, default="*")
    parser.add_argument("--name", type=str, default="satellite_demo")
    parser.add_argument("--group", type=str, default="constellation")
    args = parser.parse_args(args)

    # set up logging
    logger = logging.getLogger(args.name)
    coloredlogs.install(level=args.log_level.upper(), logger=logger)

    logger.info("Starting up satellite!")
    # start server with remaining args
    s = Satellite(
        name=args.name,
        group=args.group,
        cmd_port=args.cmd_port,
        hb_port=args.hb_port,
        mon_port=args.mon_port,
        interface=args.interface,
    )
    s.run_satellite()


if __name__ == "__main__":
    main()
