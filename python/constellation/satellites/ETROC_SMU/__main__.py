# SPDX-FileCopyrightText: 2025 Yifan Wang
# SPDX-License-Identifier: CC-BY-4.0
from constellation.core.base import setup_cli_logging, EPILOG
from constellation.core.datasender import DataSenderArgumentParser
from .ETROC_SMU import ETROC_SMU

def main(args=None):
    parser = DataSenderArgumentParser(description="ETROC DAQ Satellite", epilog=EPILOG)
    args = vars(parser.parse_args(args))
    setup_cli_logging(args["name"], args.pop("log_level"))
    s = ETROC_SMU(**args)
    s.run_satellite()

if __name__ == "__main__":
    main()
