"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2
"""

import json
import logging
import threading

import pytest

from ..chipio import ChipWriteError
from ..counters import FC_MODES, CounterReader, KC705Link
from ..scans import ScanAborted, ScanEngine, validate_scan_params
from .fakes import GAIN_DAC_PER_FC, FakeChip, FakeKC705, pixel_counts

WINDOW = 0.03
BL, NW = 500, 4
PIXEL = (3, 7)


class Rig:
    """Fake board + chip + engine, with the FC mode handled like the satellite does."""

    def __init__(self, tmp_path, fail_on_set_dac_call=None, l1a_reaches_chip=True):
        self.chip = FakeChip({PIXEL: (BL, NW)}, fail_on_set_dac_call=fail_on_set_dac_call)
        self.fake = FakeKC705(chip=self.chip, window_s=WINDOW, l1a_reaches_chip=l1a_reaches_chip).start()
        self.link = KC705Link("127.0.0.1", self.fake.port, 2.0, 2.0)
        self.link.open()
        self.reader = CounterReader(self.link, window_s=WINDOW, poll_s=WINDOW / 5)
        self.reader.set_channel(0)
        self.fc_mode = "idle"
        self.link.configure_fc(FC_MODES["idle"])
        self.engine = ScanEngine(
            reader=self.reader,
            chip=self.chip,
            set_fc_mode=self.set_fc_mode,
            get_fc_mode=lambda: self.fc_mode,
            output_path=tmp_path,
            chip_name="ET2p03_test",
            chip_address=0x60,
            log=logging.getLogger("test"),
            settle_windows=2,
        )
        self.initial = self.chip.snapshot()

    def set_fc_mode(self, mode):
        self.link.configure_fc(FC_MODES[mode])
        self.fc_mode = mode

    def close(self):
        self.link.close()
        self.fake.stop()

    def assert_clean(self):
        assert self.chip.snapshot() == self.initial
        assert self.fc_mode == "idle"
        assert self.fake.writes_to(7) == []
        assert self.fake.pulses(0x0002) == 0


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def model_trigbits(dac, qsel, injections, headers, qinj=True):
    fields = {"Bypass_THCal": 1, "DAC": dac, "QInjEn": int(qinj), "QSel": qsel, "disTrigPath": 0, "disDataReadout": 0}
    return pixel_counts(fields, BL, NW, injections, headers)


def read_dat(path):
    return [list(map(int, line.split(","))) for line in open(path).read().splitlines()]


def test_noise_scan_finds_peak_at_baseline_and_restores(rig, tmp_path):
    record = rig.engine.noise_scan({"pixels": [list(PIXEL)], "rel_min": -6, "rel_max": 6, "step": 2})
    res = record["results"][0]
    assert res["peak_dac"] == BL
    assert res["peak_within_noise_width"]
    assert [p["dac"] for p in res["points"]] == list(range(BL - 6, BL + 7, 2))
    assert [p["trigbit"] for p in res["points"]] == [model_trigbits(d, 0, 0, 0, qinj=False)[0] for d in range(BL - 6, BL + 7, 2)]

    (dat,) = record["pixel_files"]
    assert dat.endswith(f"ET2p03_test_VRef_SCurve_NoiseOnly_Pixel_C{PIXEL[1]}_R{PIXEL[0]}/FPGA_Data.dat")
    lines = read_dat(dat)
    assert [len(line) for line in lines] == [7] * 7
    assert [line[6] for line in lines] == list(range(BL - 6, BL + 7, 2))
    assert {line[1] for line in lines} == {1}
    assert {line[2] for line in lines} == {1}
    assert {line[4] for line in lines} == {0}
    refreshes = [line[0] for line in lines]
    assert all(b - a >= 2 for a, b in zip(refreshes, refreshes[1:]))

    scan = json.load(open(f"{record['scan_dir']}/scan.json"))
    assert scan["status"] == "finished"
    csv_rows = open(f"{record['scan_dir']}/counters.csv").read().splitlines()
    assert len(csv_rows) == 1 + 7
    rig.assert_clean()


def test_noise_scan_absolute_range_skips_autocal(rig):
    record = rig.engine.noise_scan({"pixels": [list(PIXEL)], "dac_min": 498, "dac_max": 502})
    assert record["results"][0]["baseline"] == -1
    assert record["results"][0]["peak_dac"] == BL
    rig.assert_clean()


def test_qinj_binary_search_matches_model(rig):
    qsel = 9
    record = rig.engine.qinj_scurve({"pixels": [list(PIXEL)], "qsels": [qsel]})
    res = record["results"][0]
    injections = headers = 11223

    upper = BL + 3 * NW
    expected_on = min(d for d in range(0, upper + 1) if model_trigbits(d, qsel, injections, headers)[0] > 0)
    assert res["turn_on"]["dac"] == expected_on
    assert res["turn_off"]["dac"] == BL + GAIN_DAC_PER_FC * (qsel + 1)
    assert res["turn_off"]["header_max"] == 11223
    assert res["charge_fC"] == qsel + 1

    on_file, off_file = record["pixel_files"]
    assert "BinarySearch_TurnOn" in on_file
    assert off_file.endswith(f"TurnOff_Pixel_C{PIXEL[1]}_R{PIXEL[0]}_QInj_{qsel}/FPGA_Data.dat")
    assert read_dat(off_file)[-1][6] == res["turn_off"]["dac"]
    assert {line[4] for line in read_dat(off_file)} == {11223}
    rig.assert_clean()


def test_qinj_sweep_gives_same_edges(rig):
    qsel = 4
    record = rig.engine.qinj_scurve(
        {"pixels": [list(PIXEL)], "qsels": [qsel], "method": "sweep", "dac_min": 480, "dac_max": 540, "step": 1}
    )
    res = record["results"][0]
    injections = 11223
    expected_on = min(d for d in range(480, 541) if model_trigbits(d, qsel, injections, injections)[0] > 0)
    assert res["turn_on"]["dac"] == expected_on
    assert res["turn_off"]["dac"] == BL + GAIN_DAC_PER_FC * (qsel + 1)
    rig.assert_clean()


def test_registers_restored_after_i2c_failure(tmp_path):
    rig = Rig(tmp_path, fail_on_set_dac_call=3)
    try:
        with pytest.raises(ChipWriteError):
            rig.engine.qinj_scurve({"pixels": [list(PIXEL)], "qsels": [5]})
        rig.chip.fail_on_set_dac_call = None
        rig.assert_clean()
        scan_dir = rig.engine.status["scan_dir"]
        assert json.load(open(f"{scan_dir}/scan.json"))["status"] == "failed"
    finally:
        rig.close()


def test_missing_headers_abort_turn_off_and_restore(tmp_path):
    rig = Rig(tmp_path, l1a_reaches_chip=False)
    try:
        with pytest.raises(RuntimeError, match="header count is 0"):
            rig.engine.qinj_scurve({"pixels": [list(PIXEL)], "turn_on": False})
        rig.assert_clean()
    finally:
        rig.close()


def test_stop_request_aborts_and_restores(rig):
    errors = []

    def run():
        try:
            rig.engine.noise_scan({"pixels": [list(PIXEL)], "rel_min": -20, "rel_max": 20})
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    while rig.engine.status.get("points", 0) < 1:
        t.join(0.01)
    rig.engine.stop_event.set()
    t.join(10)
    assert not t.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ScanAborted)
    assert rig.engine.status["state"] == "aborted"
    rig.assert_clean()


def test_repeated_scan_does_not_overwrite_pixel_file(rig):
    first = rig.engine.noise_scan({"pixels": [list(PIXEL)], "dac_min": 500, "dac_max": 500})
    second = rig.engine.noise_scan({"pixels": [list(PIXEL)], "dac_min": 500, "dac_max": 500})
    assert first["pixel_files"][0] != second["pixel_files"][0]
    assert "_r2/" in second["pixel_files"][0]


@pytest.mark.parametrize(
    "kind,params",
    [
        ("noise_scan", {"pixels": [[16, 0]]}),
        ("noise_scan", {"pixels": [[0, 0]], "dac_min": 10}),
        ("noise_scan", {"pixels": [[0, 0]], "bogus": 1}),
        ("noise_scan", {"pixels": [[0, 0]], "step": 0}),
        ("qinj_scurve", {"pixels": [[0, 0]], "qsels": [32]}),
        ("qinj_scurve", {"pixels": [[0, 0]], "method": "linear"}),
        ("qinj_scurve", {"pixels": []}),
    ],
)
def test_bad_scan_parameters_are_rejected(kind, params):
    with pytest.raises(ValueError):
        validate_scan_params(kind, params)
