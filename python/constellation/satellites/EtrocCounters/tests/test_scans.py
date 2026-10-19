"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2
"""

import csv
import json
import logging
import threading

import pytest

from ..chipio import ChipWriteError
from ..counters import FC_MODES, CounterReader, KC705Link
from ..scans import ScanAborted, ScanEngine, acc_dac_grid, level_crossing, validate_scan_params
from .fakes import GAIN_DAC_PER_FC, FakeChip, FakeKC705, pixel_counts

WINDOW = 0.03
BL, NW = 500, 4
PIXEL = (3, 7)


class Rig:
    """Fake board + chip + engine, with the FC mode handled like the satellite does."""

    def __init__(self, tmp_path, fail_on_set_dac_call=None, l1a_reaches_chip=True, latency_offset=None):
        self.chip = FakeChip({PIXEL: (BL, NW)}, fail_on_set_dac_call=fail_on_set_dac_call)
        self.fake = FakeKC705(chip=self.chip, window_s=WINDOW, l1a_reaches_chip=l1a_reaches_chip,
                              latency_offset=latency_offset).start()
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
        # named modes, or "custom <memo>" as the satellite passes them
        self.link.configure_fc(FC_MODES[mode] if mode in FC_MODES else mode.removeprefix("custom "))
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
        ("acc_scan", {"pixels": [[0, 0]], "qsel": 32}),
        ("acc_scan", {"pixels": [[0, 0]], "qsels": [3]}),
        ("acc_scan", {"pixels": [[0, 0]], "dac_max": 400}),
        ("acc_scan", {"pixels": [[0, 0]], "fine_step": 0}),
        ("acc_scan", {"pixels": [[0, 0]], "windows": 0}),
        ("acc_scan", {"pixels": [[0, 0]], "together": 1}),
        ("acc_scan", {"pixels": [[0, 0]], "rel_min": 5, "rel_max": -5}),
    ],
)
def test_bad_scan_parameters_are_rejected(kind, params):
    with pytest.raises(ValueError):
        validate_scan_params(kind, params)


def test_stop_requested_before_the_first_point_is_honoured(rig):
    rig.engine.stop_event.set()
    with pytest.raises(ScanAborted):
        rig.engine.noise_scan({"pixels": [list(PIXEL)], "rel_min": -2, "rel_max": 2})
    assert rig.engine.status["state"] == "aborted"
    rig.assert_clean()


def test_auto_calibration_runs_without_charge_injection(rig):
    rig.set_fc_mode("qinj_l1a")
    rig.engine.qinj_scurve({"pixels": [list(PIXEL)], "qsels": [3]})
    assert rig.chip.autocal_fc_qinj == [False]
    assert rig.fc_mode == "qinj_l1a"
    rig.set_fc_mode("idle")
    rig.assert_clean()


# ------------------------------------------------------------------- acc_scan
ACC_SMALL = {"rel_min": -6, "rel_max": 8, "fine_range": 3, "coarse_step": 3, "acc_reads": 2, "windows": 2}


def test_acc_scan_positive_control_and_c2_tracks_c1(rig):
    rig.chip.level_model = True
    record = rig.engine.acc_scan({"pixels": [list(PIXEL)], **ACC_SMALL})
    (res,) = record["results"]
    assert res["baseline"] == BL and res["center"] == BL
    assert abs(res["acc_bl"] - BL) <= 1
    assert res["acc_bl_vs_cal"] == res["acc_bl"] - BL
    assert res["positive_control"] == "pass"
    assert res["flagged_points"] == []
    for point in res["points"]:
        assert len(point["acc"]) == 2 and point["scan_done"] == [1, 1]
        assert len(point["c2"]) == 2 and len(point["c3"]["header"]) == 2
        assert abs(point["c2_duty"] - point["c1"]) < 0.05
        assert abs(point["c3_eff"] - point["c1"]) < 0.05
        assert set(point["c3"]["header"]) == {11223}
    assert abs(res["trig_bl"] - BL) <= 1 and abs(res["data_bl"] - BL) <= 1
    # one ACC window per DAC, in grid order
    assert [d for _, _, d in rig.chip.acc_calls] == [p["dac"] for p in res["points"]]

    scan = json.load(open(f"{record['scan_dir']}/scan.json"))
    assert scan["status"] == "finished"
    rows = list(csv.DictReader(open(f"{record['scan_dir']}/counters.csv")))
    n = len(res["points"])
    assert len(rows) == 2 * 2 * n
    assert [r["phase"] for r in rows[:4]] == ["c2_idle", "c2_idle", "c3_qinj_l1a", "c3_qinj_l1a"]
    assert {r["fc_mode"] for r in rows if r["phase"] == "c2_idle"} == {"idle"}
    assert {r["header"] for r in rows if r["phase"] == "c2_idle"} == {"0"}
    assert [int(r["dac"]) for r in rows[::4]] == [p["dac"] for p in res["points"]]
    rig.assert_clean()


def test_acc_scan_scan_done_stuck_flags_points_and_finishes(rig):
    rig.chip.level_model = True
    rig.chip.scan_done_stuck = True
    record = rig.engine.acc_scan({"pixels": [list(PIXEL)], "dac_min": BL - 1, "dac_max": BL + 1, "windows": 1})
    (res,) = record["results"]
    assert record["status"] == "finished"
    assert res["flagged_points"] == [BL - 1, BL, BL + 1]
    assert all(p["flag"] == "scan_done_not_1" for p in res["points"])
    assert "warning" in res
    rig.assert_clean()


def test_acc_scan_stop_during_c3_restores_registers_and_fc_mode(rig):
    rig.chip.level_model = True
    errors = []

    def run():
        try:
            rig.engine.acc_scan({"pixels": [list(PIXEL)], "windows": 3})
        except Exception as e:
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    while (rig.engine.status.get("last") or {}).get("phase") != "c3_qinj_l1a":
        assert t.is_alive()
        t.join(0.005)
    assert rig.fc_mode == "qinj_l1a"
    rig.engine.stop_event.set()
    t.join(10)
    assert not t.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ScanAborted)
    assert rig.engine.status["state"] == "aborted"
    rig.assert_clean()


def test_acc_scan_with_charge_keeps_qinj_and_centres_on_turn_off(rig):
    qsel = 4
    record = rig.engine.acc_scan({"pixels": [list(PIXEL)], "qsel": qsel, "rel_min": -2, "rel_max": 2,
                                  "fine_range": 1, "windows": 1, "acc_reads": 1})
    (res,) = record["results"]
    assert res["center"] == BL + GAIN_DAC_PER_FC * (qsel + 1)
    assert res["normalisation"] == "plateau"
    assert len(record["center_search_files"]) == 1
    rows = list(csv.DictReader(open(f"{record['scan_dir']}/counters.csv")))
    assert {r["phase"] for r in rows} == {"center_search", "c2c3_qinj_l1a"}
    assert {r["fc_mode"] for r in rows} == {"qinj_l1a"}
    assert rig.chip.autocal_fc_qinj == [False]
    rig.assert_clean()


def test_acc_scan_together_enables_all_listed_pixels(tmp_path):
    rig = Rig(tmp_path)
    try:
        other = (4, 7)
        rig.chip.baselines[other] = (510, 4)
        rig.chip.level_model = True
        seen = []
        measure = rig.chip.measure_acc

        def spy(row, col, reads, wait_s=0.0):
            enabled = sorted(p for p, f in rig.chip.pixels.items() if f["Bypass_THCal"] and not f["disTrigPath"])
            seen.append(((row, col), enabled))
            return measure(row, col, reads, wait_s)

        rig.chip.measure_acc = spy
        record = rig.engine.acc_scan({"pixels": [list(PIXEL), list(other)], "together": True,
                                      "rel_min": -1, "rel_max": 1, "fine_range": 1, "windows": 1, "acc_reads": 1})
        single_a, single_b, group = record["results"]
        assert single_a["baseline"] == BL and single_b["baseline"] == 510
        assert group["together"] and group["centers"] == [BL, 510]
        assert [p["rel"] for p in group["points"]] == [-1, 0, 1]
        assert group["points"][1]["dac"] == [BL, 510]
        assert all(len(p["c1"]) == 2 and 0 <= p["c1_or_indep"] <= 1 for p in group["points"])
        # single-pixel curves: only that pixel enabled; together: both
        assert {tuple(e) for _, e in seen[:3]} == {(PIXEL,)}
        assert {tuple(e) for _, e in seen[3:6]} == {(other,)}
        assert {tuple(e) for _, e in seen[6:]} == {(PIXEL, other)}
        rig.assert_clean()
    finally:
        rig.close()


def test_acc_dac_grid_fine_inside_coarse_outside():
    grid = acc_dac_grid(383, 383 - 16, 383 + 24, 8, 1, 4)
    assert grid == [367, 371] + list(range(375, 392)) + [395, 399, 403, 407]
    # coarse steps only, when the fine region is outside an absolute range
    assert acc_dac_grid(383, 400, 410, 8, 1, 4) == [400, 404, 408, 410]
    # absolute range around the centre, end points kept, clipped to the DAC range
    assert acc_dac_grid(5, 0, 30, 2, 1, 10) == [0, 3, 4, 5, 6, 7, 17, 27, 30]
    assert acc_dac_grid(1020, 1000, 1030, 2, 2, 4)[-3:] == [1020, 1022, 1023]


def test_acc_scan_absolute_override_sets_the_grid(rig):
    rig.chip.level_model = True
    record = rig.engine.acc_scan({"pixels": [list(PIXEL)], "dac_min": 490, "dac_max": 510, "fine_range": 2,
                                  "coarse_step": 5, "windows": 1, "acc_reads": 1})
    (res,) = record["results"]
    assert [p["dac"] for p in res["points"]] == [490, 493, 498, 499, 500, 501, 502, 507, 510]
    assert res["baseline"] == BL
    rig.assert_clean()


def test_level_crossing_interpolates_nearest_the_centre():
    falling = [(0, 1.0), (2, 0.6), (4, 0.2), (6, 0.0)]
    assert level_crossing(falling, 0.5, 3) == pytest.approx(2.5)
    assert level_crossing(falling, 0.2, 3) == 4.0
    peak = [(0, 0.0), (1, 1.0), (2, 0.0)]
    assert level_crossing(peak, 0.5, 1.8) == pytest.approx(1.5)
    assert level_crossing([(0, 0.1), (1, 0.2)], 0.5, 0) is None


# ------------------------------------------------- qinj_scurve fc_mode / together / trig_path (plan A2, A3, A5)
ORBITS = 40_000_001 / 3564


def test_qinj_sweep_under_a_rate_pattern_counts_n_hits_per_orbit(rig):
    mode = "custom Start QInj L1A Triggerbit multiple=4"
    record = rig.engine.qinj_scurve({"pixels": [list(PIXEL)], "qsels": [9], "method": "sweep",
                                     "dac_min": BL + 20, "dac_max": BL + 22, "step": 1, "fc_mode": mode})
    points = record["results"][0]["points"]
    assert [p["header"] for p in points] == [int(4 * ORBITS)] * 3
    assert [p["data"] for p in points] == [int(4 * ORBITS)] * 3
    rows = list(csv.DictReader(open(f"{record['scan_dir']}/counters.csv")))
    assert {r["fc_mode"] for r in rows} == {mode}
    assert rig.chip.autocal_fc_qinj == [False]  # the calibration still ran with idle FC
    rig.assert_clean()


def test_qinj_sweep_together_enables_all_pixels_at_one_dac(tmp_path):
    rig = Rig(tmp_path)
    try:
        other = (4, 7)
        rig.chip.baselines[other] = (510, 4)
        seen = []
        set_dac = rig.chip.set_dac

        def spy(row, col, dac):
            set_dac(row, col, dac)
            seen.append(sorted(p for p, f in rig.chip.pixels.items() if f["QInjEn"]))

        rig.chip.set_dac = spy
        record = rig.engine.qinj_scurve({"pixels": [list(PIXEL), list(other)], "qsels": [9], "method": "sweep",
                                         "dac_min": 530, "dac_max": 532, "step": 2, "together": True,
                                         "fc_mode": "custom Start QInj L1A Triggerbit multiple=2"})
        (res,) = record["results"]
        assert res["together"] and res["n_pixels"] == 2 and res["baselines"] == [BL, 510]
        assert [p["dac"] for p in res["points"]] == [530, 532]
        assert [p["data"] for p in res["points"]] == [2 * int(2 * ORBITS)] * 2
        assert res["hits_per_l1a_per_pixel"] == [1.0, 1.0]
        assert {tuple(e) for e in seen} == {(PIXEL, other)}
        assert "Sweep_Together_2px_QInj_9" in record["pixel_files"][0]
        rows = list(csv.DictReader(open(f"{record['scan_dir']}/counters.csv")))
        assert {(r["row"], r["col"]) for r in rows} == {(f"{PIXEL[0]};4", f"{PIXEL[1]};7")}
        rig.assert_clean()
    finally:
        rig.close()


def test_qinj_sweep_with_trigger_path_off_reads_hits_without_trigger_bits(rig):
    common = {"pixels": [list(PIXEL)], "qsels": [9], "method": "sweep", "dac_min": BL + 20, "dac_max": BL + 20}
    on = rig.engine.qinj_scurve(dict(common))["results"][0]["points"][0]
    off = rig.engine.qinj_scurve({**common, "trig_path": False})["results"][0]["points"][0]
    assert on["trigbit"] == int(ORBITS) and on["data"] == on["header"] == int(ORBITS)
    assert off["trigbit"] == 0 and off["data"] == off["header"] == int(ORBITS)
    rig.assert_clean()


def test_l1a_offset_and_l1a_delay_move_the_readout_window_together(tmp_path):
    rig = Rig(tmp_path, latency_offset=3)  # in time when L1A - QInj = L1Adelay + 3, i.e. 504 = 501 + 3
    try:
        def point(offset):
            mode = f"custom Start QInj L1A Triggerbit l1a_offset={offset}"
            rec = rig.engine.qinj_scurve({"pixels": [list(PIXEL)], "qsels": [9], "method": "sweep",
                                          "dac_min": BL + 20, "dac_max": BL + 20, "fc_mode": mode})
            return rec["results"][0]["points"][0]

        assert point(0)["data"] == int(ORBITS)
        late = point(1)
        assert late["data"] == 0 and late["trigbit"] == int(ORBITS) and late["header"] == int(ORBITS)
        rig.engine.l1a_delay = 0x1F5 + 1
        assert point(1)["data"] == int(ORBITS)  # K* = 501 - d*: one BX later is matched by one more delay
        assert point(0)["data"] == 0
        rig.assert_clean()
    finally:
        rig.close()


@pytest.mark.parametrize(
    "params",
    [
        {"pixels": [[0, 0]], "fc_mode": ""},
        {"pixels": [[0, 0]], "fc_mode": 3},
        {"pixels": [[0, 0]], "together": 1},
        {"pixels": [[0, 0]], "together": True},
        {"pixels": [[0, 0]], "together": True, "method": "sweep"},
        {"pixels": [[0, 0]], "trig_path": False},
        {"pixels": [[0, 0]], "trig_path": "no", "method": "sweep"},
        {"pixels": [[0, 0]], "method": "sweep", "dac_min": 600, "dac_max": 500},
    ],
)
def test_bad_qinj_pattern_parameters_are_rejected(params):
    with pytest.raises(ValueError):
        validate_scan_params("qinj_scurve", params)


def test_together_sweep_stopped_mid_scan_restores_every_pixel_and_the_fc(tmp_path):
    rig = Rig(tmp_path)
    try:
        other = (4, 7)
        rig.chip.baselines[other] = (510, 4)
        set_dac = rig.chip.set_dac
        calls = []

        def stop_on_third(row, col, dac):
            set_dac(row, col, dac)
            calls.append(dac)
            if len(calls) == 3:
                rig.engine.stop_event.set()

        rig.chip.set_dac = stop_on_third
        with pytest.raises(ScanAborted):
            rig.engine.qinj_scurve({"pixels": [list(PIXEL), list(other)], "qsels": [9], "method": "sweep",
                                    "dac_min": 530, "dac_max": 540, "step": 1, "together": True,
                                    "fc_mode": "custom Start QInj L1A Triggerbit multiple=2"})
        rig.assert_clean()
    finally:
        rig.close()


def test_scans_started_in_the_same_second_keep_separate_records(rig, monkeypatch):
    from .. import scans

    strftime = scans.time.strftime
    monkeypatch.setattr(scans.time, "strftime", lambda fmt, *a: "120000" if fmt == "%H%M%S" else strftime(fmt, *a))
    first = rig.engine.noise_scan({"pixels": [list(PIXEL)], "dac_min": BL, "dac_max": BL})
    second = rig.engine.noise_scan({"pixels": [list(PIXEL)], "dac_min": BL - 2, "dac_max": BL - 2})
    assert first["scan_dir"] != second["scan_dir"]
    assert second["scan_id"].endswith("_120000_r2")
    assert json.load(open(f"{first['scan_dir']}/scan.json"))["results"][0]["dac_min"] == BL
    assert json.load(open(f"{second['scan_dir']}/scan.json"))["results"][0]["dac_min"] == BL - 2
