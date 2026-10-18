"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Pixel-level I2C access to one ETROC2 for the counter scans.

`ChipIO` is the small interface the scans use; `I2CGui2ChipIO` implements it with
the same i2c_gui2 wrapper EtrocWaveform uses (USB-ISS adapter). Every write is
read back and compared, so a failed I2C write stops the scan instead of being
measured silently.
"""

from typing import Protocol

# Pixel Config fields a scan may change; they are saved before and restored after a scan.
PIXEL_FIELDS = (
    "disDataReadout",
    "QInjEn",
    "disTrigPath",
    "Bypass_THCal",
    "DAC",
    "QSel",
    "enable_TDC",
    "L1Adelay",
    "CLKEn_THCal",
    "BufEn_THCal",
    "RSTn_THCal",
    "ScanStart_THCal",
    "TH_offset",
    "upperTOATrig",
    "lowerTOATrig",
    "upperTOTTrig",
    "lowerTOTTrig",
    "upperCalTrig",
    "lowerCalTrig",
    "upperTOA",
    "lowerTOA",
    "upperTOT",
    "lowerTOT",
    "upperCal",
    "lowerCal",
)

# TDC readout and trigger windows fully open (as EtrocWaveform's config_TDC_window_ranges_in_memory)
OPEN_WINDOWS = {
    "upperTOATrig": 0x3FF, "lowerTOATrig": 0x000,
    "upperTOTTrig": 0x1FF, "lowerTOTTrig": 0x000,
    "upperCalTrig": 0x3FF, "lowerCalTrig": 0x000,
    "upperTOA": 0x3FF, "lowerTOA": 0x000,
    "upperTOT": 0x1FF, "lowerTOT": 0x000,
    "upperCal": 0x3FF, "lowerCal": 0x000,
}

DAC_MAX = 0x3FF
# Keys of the wrapper's power_map (disable_all_pixels); an unknown key silently means "high"
POWER_MODES = ("high", "010", "101", "low")
QSEL_MAX = 0x1F


class ChipWriteError(RuntimeError):
    """An I2C write did not read back as written."""


class ChipIO(Protocol):
    def read_pixel(self, row: int, col: int) -> dict[str, int]:
        """Return the current values of PIXEL_FIELDS for one pixel."""

    def write_pixel(self, row: int, col: int, fields: dict[str, int]) -> None:
        """Write several Pixel Config fields of one pixel and verify them."""

    def set_dac(self, row: int, col: int, dac: int) -> None:
        """Write the pixel threshold DAC (Bypass_THCal must be 1) and verify it."""

    def auto_calibrate(self, row: int, col: int) -> tuple[int, int]:
        """Run the on-chip threshold calibration; return (baseline BL, noise width NW)."""

    def close(self) -> None:
        """Release the I2C adapter."""


class I2CGui2ChipIO:
    """ChipIO on top of EtrocCommon.new_wrapper_i2cGui2.i2c_connection."""

    BLOCK = ("ETROC2", "Pixel Config")

    def __init__(
        self, port: str, chip_address: int, ws_address: int | None = None, chip_name: str = "ETROC2", logger=None
    ):
        # i2c_gui2/pandas/tqdm are only imported when a chip is actually used
        from ..EtrocCommon.new_wrapper_i2cGui2 import i2c_connection

        self.chip_address = chip_address
        self.conn = i2c_connection(port, [chip_address], [ws_address], [chip_name], logger=logger)
        self.chip = self.conn.get_chip(chip_address)

    def _select(self, row: int, col: int) -> None:
        self.chip.broadcast = False
        self.chip.row, self.chip.col = row, col

    def read_pixel(self, row: int, col: int) -> dict[str, int]:
        self._select(row, col)
        self.chip.read_all_block(*self.BLOCK)
        return {f: int(self.chip.get_decoded_value(*self.BLOCK, f)) for f in PIXEL_FIELDS}

    def write_pixel(self, row: int, col: int, fields: dict[str, int]) -> None:
        self._select(row, col)
        # refresh the cache from the chip so untouched fields are written back unchanged
        self.chip.read_all_block(*self.BLOCK)
        for name, value in fields.items():
            self.chip.set_decoded_value(*self.BLOCK, name, int(value))
        self.chip.write_all_block(*self.BLOCK)
        self.chip.read_all_block(*self.BLOCK)
        bad = {n: (v, int(self.chip.get_decoded_value(*self.BLOCK, n))) for n, v in fields.items()
               if int(self.chip.get_decoded_value(*self.BLOCK, n)) != int(v)}
        if bad:
            raise ChipWriteError(f"Pixel ({row},{col}) on 0x{self.chip_address:02x}: read back differs (written, read) {bad}")

    def set_dac(self, row: int, col: int, dac: int) -> None:
        if not 0 <= dac <= DAC_MAX:
            raise ValueError(f"DAC {dac} outside 0-{DAC_MAX}")
        self._select(row, col)
        self.chip.set_decoded_value(*self.BLOCK, "DAC", int(dac))
        self.chip.write_decoded_value(*self.BLOCK, "DAC")
        self.chip.read_decoded_value(*self.BLOCK, "DAC")
        read = int(self.chip.get_decoded_value(*self.BLOCK, "DAC"))
        if read != dac:
            raise ChipWriteError(f"Pixel ({row},{col}) DAC written {dac}, read back {read}")

    def auto_calibrate(self, row: int, col: int) -> tuple[int, int]:
        bl, nw, _ = self.conn.auto_cal_single_pixel(self.chip_address, row, col)
        # the wrapper only logs a ScanDone timeout; its BL/NW are then not from this calibration
        if int(self.chip.get_decoded_value("ETROC2", "Pixel Status", "ScanDone")) != 1:
            raise ChipWriteError(f"Pixel ({row},{col}) auto-calibration did not finish (ScanDone = 0)")
        return int(bl), int(nw)

    def setup_chip(self, power_mode: str = "high") -> None:
        """PLL/FC calibration, peripherals, all pixels off, second PLL/FC calibration (as the bench needed)."""
        if power_mode not in POWER_MODES:
            raise ValueError(f"power_mode must be one of {POWER_MODES}, got '{power_mode}'")
        self.conn.batch_PLL_FC_calibration()
        self.conn.batch_set_chip_peripherals()
        self.conn.batch_disable_all_chips(power_mode=power_mode)
        self.conn.batch_PLL_FC_calibration()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.chip = None
        self.conn = None
