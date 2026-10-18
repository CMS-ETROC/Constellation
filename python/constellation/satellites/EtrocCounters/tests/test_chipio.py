"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

I2CGui2ChipIO against a stand-in for the i2c_gui2 chip object (no USB-ISS).
"""

import pytest

from ..chipio import PIXEL_FIELDS, ChipWriteError, I2CGui2ChipIO


class StubChip:
    """Register cache + 'hardware' dict, with optional stuck fields that ignore writes."""

    def __init__(self, stuck=()):
        self.hw = {f: 0 for f in PIXEL_FIELDS} | {"ScanDone": 1, "ACC": 0}
        self.cache = dict(self.hw)
        self.stuck = set(stuck)
        self.broadcast = True
        self.row = self.col = None
        self.writes = []

    def read_all_block(self, chip, block):
        self.cache.update(self.hw)

    def write_all_block(self, chip, block):
        for k, v in self.cache.items():
            if k not in self.stuck:
                self.hw[k] = v

    def set_decoded_value(self, chip, block, name, value):
        self.cache[name] = value

    def get_decoded_value(self, chip, block, name):
        return self.cache[name]

    def write_decoded_value(self, chip, block, name):
        self.writes.append((name, self.cache[name]))
        if name not in self.stuck:
            self.hw[name] = self.cache[name]

    def read_decoded_value(self, chip, block, name):
        self.cache[name] = self.hw[name]


class StubConn:
    def __init__(self, chip, scan_done=1):
        self.chip = chip
        self.scan_done = scan_done

    def auto_cal_single_pixel(self, addr, row, col):
        self.chip.hw["ScanDone"] = self.scan_done
        self.chip.cache["ScanDone"] = self.scan_done
        return [512, 5, None]


def make(stuck=(), scan_done=1):
    io = I2CGui2ChipIO.__new__(I2CGui2ChipIO)
    io.chip_address = 0x60
    io.chip = StubChip(stuck)
    io.conn = StubConn(io.chip, scan_done)
    return io


def test_write_pixel_reads_back_and_selects_the_pixel():
    io = make()
    io.write_pixel(4, 9, {"QInjEn": 1, "QSel": 12})
    assert (io.chip.row, io.chip.col, io.chip.broadcast) == (4, 9, False)
    assert io.read_pixel(4, 9)["QSel"] == 12


def test_write_pixel_mismatch_raises():
    io = make(stuck={"QSel"})
    with pytest.raises(ChipWriteError, match="QSel"):
        io.write_pixel(0, 0, {"QSel": 7})


def test_set_dac_mismatch_raises():
    io = make(stuck={"DAC"})
    with pytest.raises(ChipWriteError):
        io.set_dac(0, 0, 300)
    with pytest.raises(ValueError):
        make().set_dac(0, 0, 1024)


def test_auto_calibration_requires_scan_done():
    assert make().auto_calibrate(1, 1) == (512, 5)
    with pytest.raises(ChipWriteError, match="ScanDone"):
        make(scan_done=0).auto_calibrate(1, 1)


def test_measure_acc_resets_starts_and_reads_every_value():
    io = make()
    io.chip.hw["ACC"] = 16384
    acc, done = io.measure_acc(3, 4, reads=3, wait_s=0)
    assert (io.chip.row, io.chip.col, io.chip.broadcast) == (3, 4, False)
    assert io.chip.writes == [("RSTn_THCal", 0), ("RSTn_THCal", 1), ("ScanStart_THCal", 0), ("ScanStart_THCal", 1)]
    assert acc == [16384] * 3 and done == [1] * 3


def test_measure_acc_stuck_scan_start_raises():
    with pytest.raises(ChipWriteError, match="ScanStart_THCal"):
        make(stuck={"ScanStart_THCal"}).measure_acc(0, 0, reads=1, wait_s=0)
