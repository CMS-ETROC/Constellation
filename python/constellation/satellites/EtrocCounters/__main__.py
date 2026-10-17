"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2
"""

from constellation.core.logging import setup_cli_logging
from constellation.core.satellite import SatelliteArgumentParser

from ..EtrocCommon import ETROC_SATELLITES_VERSION
from .EtrocCounters import EtrocCounters


def main(args=None):
    """EtrocCounters satellite"""

    parser = SatelliteArgumentParser(description=main.__doc__, version=ETROC_SATELLITES_VERSION)
    args = vars(parser.parse_args(args))

    setup_cli_logging(args.pop("level"))

    s = EtrocCounters(**args)
    s.run_satellite()


if __name__ == "__main__":
    main()
