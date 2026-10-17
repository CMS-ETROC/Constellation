"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Counter scans: trigger-bit noise scan around the auto-calibrated baseline, and the
fast QInj S-curve edges (binary search or full sweep), as done in 2023-24 with
i2c_gui `scripts/TIDboard_noisePeak.py` and ETROC-DAQ `Save_FPGA_data`.

Outputs, under `<output_path>/<YYYY-MM-DD>_Array_Test_Results/`:
- one directory per pixel (and charge) with the 7-column `FPGA_Data.dat` the old
  analysis parsers read: refresh,en_L1A,window_s,data,header,trigbit,DAC
  (old directory names: <chip>_VRef_SCurve_NoiseOnly_Pixel_C<c>_R<r>,
  <chip>_VRef_SCurve_BinarySearch_TurnOn_Pixel_C<c>_R<r>,
  <chip>_VRef_SCurve_BinarySearch_TurnOff_Pixel_C<c>_R<r>_QInj_<qsel>);
- one `<chip>_<scan>_<HHMMSS>_scan/` directory with `counters.csv` (every measured
  window with pixel, charge, channel, settings, timestamps) and `scan.json`
  (parameters, per-pixel results, status).
"""

import csv
import datetime
import json
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .chipio import DAC_MAX, OPEN_WINDOWS, QSEL_MAX, ChipIO
from .counters import WINDOW_S, CounterReader, CounterWindow

CSV_COLUMNS = [
    "scan_id", "scan_type", "phase", "timestamp", "unix_time", "chip_name", "chip_address", "channel",
    "row", "col", "qsel", "charge_fC", "dac", "baseline", "noise_width", "fc_mode",
    "refresh", "en_L1A", "window_s", "data", "header", "trigbit", "combine", "led8",
]


def fpga_data_line(window: CounterWindow, dac: int) -> str:
    """The 7-column FPGA_Data.dat line of the 2023-24 DAQ: refresh,en_L1A,window_s,data,header,trigbit,DAC.

    window_s is the firmware counting window (fixed 1 s on SEU2025).
    """
    return f"{window.refresh},{window.en_l1a},{round(WINDOW_S)},{window.data},{window.header},{window.trigbit},{dac}\n"


def parse_pixels(pixels: Any) -> list[tuple[int, int]]:
    """Accept [[row, col], ...] (or a single [row, col]) and validate the 16x16 range."""
    if not isinstance(pixels, list) or not pixels:
        raise ValueError("'pixels' must be a non-empty list of [row, col] pairs")
    if all(isinstance(x, int) for x in pixels) and len(pixels) == 2:
        pixels = [pixels]
    out = []
    for p in pixels:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(x, int) for x in p)):
            raise ValueError(f"pixel {p!r} is not a [row, col] pair of integers")
        row, col = p
        if not (0 <= row <= 15 and 0 <= col <= 15):
            raise ValueError(f"pixel {p!r} outside the 16x16 matrix")
        out.append((row, col))
    return out


NOISE_SCAN_KEYS = {"pixels", "rel_min", "rel_max", "step", "dac_min", "dac_max", "data_readout", "tag"}
QINJ_SCURVE_KEYS = {
    "pixels", "qsels", "method", "turn_on", "turn_off", "turnoff_counter", "turnoff_dac_max",
    "dac_min", "dac_max", "step", "tag",
}


def validate_scan_params(kind: str, params: dict[str, Any]) -> None:
    """Reject unknown keys and malformed values before a scan thread is started."""
    allowed = {"noise_scan": NOISE_SCAN_KEYS, "qinj_scurve": QINJ_SCURVE_KEYS}[kind]
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"{kind}: unknown parameters {unknown}; allowed {sorted(allowed)}")
    parse_pixels(params.get("pixels"))
    for key in ("rel_min", "rel_max", "step", "dac_min", "dac_max", "turnoff_dac_max"):
        if key in params and not (isinstance(params[key], int) and not isinstance(params[key], bool)):
            raise ValueError(f"{kind}: '{key}' must be an integer")
    if params.get("step", 1) < 1:
        raise ValueError(f"{kind}: step must be >= 1")
    for key in ("dac_min", "dac_max", "turnoff_dac_max"):
        if key in params and not 0 <= params[key] <= DAC_MAX:
            raise ValueError(f"{kind}: '{key}' outside 0-{DAC_MAX}")
    if kind == "noise_scan":
        if ("dac_min" in params) != ("dac_max" in params):
            raise ValueError("noise_scan: give both dac_min and dac_max, or neither")
        if params.get("rel_min", -10) > params.get("rel_max", 20):
            raise ValueError("noise_scan: rel_min > rel_max")
        return
    qsels = params.get("qsels", [27])
    qsels = [qsels] if isinstance(qsels, int) else qsels
    if not isinstance(qsels, list) or not qsels:
        raise ValueError("qinj_scurve: 'qsels' must be a non-empty list of QSel values")
    for q in qsels:
        if not (isinstance(q, int) and 0 <= q <= QSEL_MAX):
            raise ValueError(f"qinj_scurve: QSel {q!r} outside 0-{QSEL_MAX}")
    if params.get("method", "binary") not in ("binary", "sweep"):
        raise ValueError("qinj_scurve: method must be 'binary' or 'sweep'")
    if params.get("turnoff_counter", "data") not in ("data", "trigbit"):
        raise ValueError("qinj_scurve: turnoff_counter must be 'data' or 'trigbit'")


class ScanAborted(RuntimeError):
    """The scan was stopped by stop_scan or by a state change."""


class ScanEngine:
    """Runs one scan at a time; every pixel's registers are restored afterwards, also on errors."""

    def __init__(
        self,
        reader: CounterReader,
        chip: ChipIO,
        set_fc_mode: Callable[[str], None],
        get_fc_mode: Callable[[], str],
        output_path: Path,
        chip_name: str,
        chip_address: int,
        log: Any,
        settle_windows: int = 2,
        l1a_delay: int = 0x1F5,
        on_window: Callable[[CounterWindow], None] | None = None,
    ):
        self.reader = reader
        self.chip = chip
        self.set_fc_mode = set_fc_mode
        self.get_fc_mode = get_fc_mode
        self.output_path = Path(output_path)
        self.chip_name = chip_name
        self.chip_address = chip_address
        self.log = log
        self.settle_windows = settle_windows
        self.l1a_delay = l1a_delay
        self.on_window = on_window
        self.stop_event = threading.Event()
        self.status: dict[str, Any] = {"state": "idle"}
        self._csv_writer = None
        self._csv_file = None

    # ------------------------------------------------------------------ helpers
    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise ScanAborted("scan stopped on request")

    def _day_dir(self) -> Path:
        d = self.output_path / f"{datetime.date.today().isoformat()}_Array_Test_Results"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _pixel_file(self, name: str) -> Path:
        """FPGA_Data.dat of a pixel directory; a repeated name gets a _rN suffix instead of appending to old data."""
        base = self._day_dir() / name
        candidate, n = base, 1
        while (candidate / "FPGA_Data.dat").exists():
            n += 1
            candidate = base.with_name(f"{base.name}_r{n}")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate / "FPGA_Data.dat"

    @contextmanager
    def _scan_files(self, scan_type: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        stamp = time.strftime("%H%M%S")
        scan_id = f"{self.chip_name}_{scan_type}_{stamp}"
        scan_dir = self._day_dir() / f"{scan_id}_scan"
        scan_dir.mkdir(parents=True, exist_ok=True)
        self._csv_file = open(scan_dir / "counters.csv", "w", newline="")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=CSV_COLUMNS)
        self._csv_writer.writeheader()
        record: dict[str, Any] = {
            "scan_id": scan_id,
            "scan_type": scan_type,
            "chip_name": self.chip_name,
            "chip_address": f"0x{self.chip_address:02x}",
            "channel": self.reader.get_channel(),
            "parameters": params,
            "start_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scan_dir": str(scan_dir),
            "pixel_files": [],
            "results": [],
            "status": "running",
        }
        self.status = {"state": "running", "scan_id": scan_id, "scan_dir": str(scan_dir), "points": 0}
        try:
            yield record
            record["status"] = "finished"
        except ScanAborted as e:
            record["status"] = "aborted"
            record["error"] = str(e)
            raise
        except Exception as e:
            record["status"] = "failed"
            record["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            record["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
            with open(scan_dir / "scan.json", "w") as f:
                json.dump(record, f, indent=2, default=str)
            self.status = {
                "state": record["status"], "scan_id": scan_id, "scan_dir": str(scan_dir),
                "points": self.status.get("points", 0), "error": record.get("error"),
            }

    @contextmanager
    def _pixel_session(self, row: int, col: int) -> Iterator[dict[str, int]]:
        """Save the pixel's Pixel Config fields and write them back afterwards, whatever happens."""
        saved = self.chip.read_pixel(row, col)
        try:
            yield saved
        except BaseException:
            # the scan error is the one reported; a restore failure is logged next to it
            self._restore_pixel(row, col, saved, reraise=False)
            raise
        self._restore_pixel(row, col, saved, reraise=True)

    def _restore_pixel(self, row: int, col: int, saved: dict[str, int], reraise: bool) -> None:
        try:
            self.chip.write_pixel(row, col, saved)
            self.log.info(f"Restored pixel ({row},{col}) registers")
        except Exception as e:
            self.log.critical(f"FAILED to restore pixel ({row},{col}) registers {saved}: {e}")
            if reraise:
                raise

    @contextmanager
    def _fc_session(self, mode: str) -> Iterator[None]:
        previous = self.get_fc_mode()
        if mode != previous:
            self.set_fc_mode(mode)
        try:
            yield
        finally:
            if self.get_fc_mode() != previous:
                try:
                    self.set_fc_mode(previous)
                except Exception as e:
                    self.log.critical(f"FAILED to restore fast-command mode '{previous}': {e}")

    def _measure(self, row: int, col: int, dac: int, record: dict[str, Any], meta: dict[str, Any], dat) -> CounterWindow:
        self._check_stop()
        self.chip.set_dac(row, col, dac)
        try:
            window = self.reader.wait_windows(self.settle_windows, self.stop_event)
        except InterruptedError as e:
            raise ScanAborted("scan stopped on request") from e
        dat.write(fpga_data_line(window, dac))
        dat.flush()
        self._csv_writer.writerow({
            "scan_id": record["scan_id"], "scan_type": record["scan_type"], "phase": meta.get("phase", ""),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(window.unix_time)),
            "unix_time": f"{window.unix_time:.3f}", "chip_name": self.chip_name,
            "chip_address": f"0x{self.chip_address:02x}", "channel": window.channel, "row": row, "col": col,
            "qsel": meta.get("qsel", ""), "charge_fC": meta.get("charge_fC", ""), "dac": dac,
            "baseline": meta.get("baseline", ""), "noise_width": meta.get("noise_width", ""),
            "fc_mode": self.get_fc_mode(), "refresh": window.refresh, "en_L1A": window.en_l1a,
            "window_s": WINDOW_S, "data": window.data, "header": window.header,
            "trigbit": window.trigbit, "combine": window.combine, "led8": window.led8,
        })
        self._csv_file.flush()
        self.status["points"] = self.status.get("points", 0) + 1
        self.status["last"] = {"row": row, "col": col, "dac": dac, "trigbit": window.trigbit,
                               "data": window.data, "header": window.header}
        if self.on_window is not None:
            self.on_window(window)
        return window

    def _baseline(self, row: int, col: int, autocal: bool) -> tuple[int, int]:
        if not autocal:
            return -1, -1
        bl, nw = self.chip.auto_calibrate(row, col)
        self.log.info(f"Pixel ({row},{col}) auto-calibration: BL {bl}, NW {nw}")
        return bl, nw

    # ---------------------------------------------------------------- noise scan
    def noise_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        """Trigger-bit count per window vs DAC with no charge injection and IDLE fast commands.

        params: pixels [[row, col], ...] (required); rel_min (-10), rel_max (20), step (1)
        relative to the auto-cal baseline, or dac_min/dac_max absolute; data_readout (False)
        enables the pixel's data readout as well; tag ("") is appended to directory names.
        """
        validate_scan_params("noise_scan", params)
        pixels = parse_pixels(params.get("pixels"))
        rel_min = int(params.get("rel_min", -10))
        rel_max = int(params.get("rel_max", 20))
        step = int(params.get("step", 1))
        if step < 1:
            raise ValueError("step must be >= 1")
        dac_min = params.get("dac_min")
        dac_max = params.get("dac_max")
        data_readout = bool(params.get("data_readout", False))
        tag = str(params.get("tag", ""))
        if (dac_min is None) != (dac_max is None):
            raise ValueError("give both dac_min and dac_max, or neither")
        self.stop_event.clear()

        with self._scan_files("NoiseScan", dict(params)) as record, self._fc_session("idle"):
            for row, col in pixels:
                with self._pixel_session(row, col):
                    bl, nw = self._baseline(row, col, autocal=dac_min is None)
                    if dac_min is None:
                        dacs = [d for d in range(bl + rel_min, bl + rel_max + 1, step) if 0 <= d <= DAC_MAX]
                    else:
                        dacs = list(range(int(dac_min), int(dac_max) + 1, step))
                    if not dacs:
                        raise ValueError(f"empty DAC range for pixel ({row},{col})")
                    self.chip.write_pixel(row, col, {
                        "disDataReadout": 0 if data_readout else 1, "QInjEn": 0, "disTrigPath": 0,
                        "Bypass_THCal": 1, "enable_TDC": 1, "DAC": dacs[0], **OPEN_WINDOWS,
                    })
                    dat_path = self._pixel_file(f"{self.chip_name}_VRef_SCurve_NoiseOnly_Pixel_C{col}_R{row}{tag}")
                    record["pixel_files"].append(str(dat_path))
                    meta = {"phase": "noise", "baseline": bl, "noise_width": nw}
                    points = []
                    with open(dat_path, "w") as dat:
                        for dac in dacs:
                            w = self._measure(row, col, dac, record, meta, dat)
                            points.append((dac, w.trigbit))
                    record["results"].append(summarise_noise(row, col, bl, nw, points))
        return record

    # -------------------------------------------------------------- QInj S-curve
    def qinj_scurve(self, params: dict[str, Any]) -> dict[str, Any]:
        """S-curve edges with one QInj + one L1A per orbit (~11.2 k per window).

        params: pixels (required); qsels [27] (QSel register values, charge = QSel+1 fC);
        method "binary" (default) or "sweep"; turn_on (True) and turn_off (True) for the
        binary search; turnoff_counter "data" (default, as pixel_turnoff_points) or
        "trigbit"; turnoff_dac_max (1020); dac_min/dac_max/step for "sweep" (default
        BL-10 .. BL+150, step 2); tag ("").
        """
        validate_scan_params("qinj_scurve", params)
        pixels = parse_pixels(params.get("pixels"))
        qsels = params.get("qsels", [27])
        if isinstance(qsels, int):
            qsels = [qsels]
        for q in qsels:
            if not (isinstance(q, int) and 0 <= q <= QSEL_MAX):
                raise ValueError(f"QSel {q!r} outside 0-{QSEL_MAX}")
        method = str(params.get("method", "binary"))
        if method not in ("binary", "sweep"):
            raise ValueError("method must be 'binary' or 'sweep'")
        counter = str(params.get("turnoff_counter", "data"))
        if counter not in ("data", "trigbit"):
            raise ValueError("turnoff_counter must be 'data' or 'trigbit'")
        do_on = bool(params.get("turn_on", True))
        do_off = bool(params.get("turn_off", True))
        turnoff_max = int(params.get("turnoff_dac_max", 1020))
        tag = str(params.get("tag", ""))
        self.stop_event.clear()

        with self._scan_files(f"QInjSCurve_{method}", dict(params)) as record, self._fc_session("qinj_l1a"):
            for row, col in pixels:
                with self._pixel_session(row, col):
                    bl, nw = self._baseline(row, col, autocal=True)
                    base_fields = {"disDataReadout": 0, "QInjEn": 1, "disTrigPath": 0, "Bypass_THCal": 1,
                                   "enable_TDC": 1, "L1Adelay": self.l1a_delay, **OPEN_WINDOWS}
                    for qsel in qsels:
                        self.chip.write_pixel(row, col, {**base_fields, "QSel": qsel, "DAC": DAC_MAX})
                        meta = {"qsel": qsel, "charge_fC": qsel + 1, "baseline": bl, "noise_width": nw}
                        result: dict[str, Any] = {"row": row, "col": col, "qsel": qsel, "charge_fC": qsel + 1,
                                                  "baseline": bl, "noise_width": nw, "method": method}
                        if method == "sweep":
                            lo = int(params.get("dac_min", bl - 10))
                            hi = int(params.get("dac_max", bl + 150))
                            st = int(params.get("step", 2))
                            dat_path = self._pixel_file(
                                f"{self.chip_name}_VRef_SCurve_Sweep_Pixel_C{col}_R{row}_QInj_{qsel}{tag}")
                            record["pixel_files"].append(str(dat_path))
                            points = []
                            with open(dat_path, "w") as dat:
                                for dac in range(max(lo, 0), min(hi, DAC_MAX) + 1, st):
                                    w = self._measure(row, col, dac, record, {**meta, "phase": "sweep"}, dat)
                                    points.append((dac, w.trigbit, w.data, w.header))
                            result.update(summarise_sweep(points, counter))
                        else:
                            if do_on:
                                dat_path = self._pixel_file(
                                    f"{self.chip_name}_VRef_SCurve_BinarySearch_TurnOn_Pixel_C{col}_R{row}{tag}"
                                    + (f"_QInj_{qsel}" if len(qsels) > 1 else ""))
                                record["pixel_files"].append(str(dat_path))
                                with open(dat_path, "w") as dat:
                                    result["turn_on"] = self._bisect_turn_on(
                                        row, col, max(bl + 3 * nw, 1), record, {**meta, "phase": "turn_on"}, dat)
                            if do_off:
                                dat_path = self._pixel_file(
                                    f"{self.chip_name}_VRef_SCurve_BinarySearch_TurnOff_Pixel_C{col}_R{row}_QInj_{qsel}{tag}")
                                record["pixel_files"].append(str(dat_path))
                                with open(dat_path, "w") as dat:
                                    result["turn_off"] = self._bisect_turn_off(
                                        row, col, max(bl, 0), turnoff_max, counter, record,
                                        {**meta, "phase": "turn_off"}, dat)
                        record["results"].append(result)
        return record

    def _bisect_turn_on(self, row, col, upper, record, meta, dat) -> dict[str, Any]:
        """Lowest DAC in [0, upper] with trigger bits > 0 (pixel_turnon_points)."""
        a, b = 0, int(upper)
        found = False
        while b - a > 1:
            dac = (a + b) // 2
            w = self._measure(row, col, dac, record, meta, dat)
            if w.trigbit > 0:
                b, found = dac, True
            else:
                a = dac
        # final point at the result, so the last FPGA_Data.dat line carries the answer
        w = self._measure(row, col, b, record, meta, dat)
        return {"dac": b, "found": found or w.trigbit > 0, "trigbit_at_dac": w.trigbit, "header_at_dac": w.header}

    def _bisect_turn_off(self, row, col, lower, upper, counter, record, meta, dat) -> dict[str, Any]:
        """Highest DAC in [lower, upper] with counter >= header_max/2 (pixel_turnoff_points).

        header_max is the header count of the first point (L1As per window).
        """
        a, b = int(lower), int(upper)
        header_max = None
        while b - a > 1:
            dac = (a + b) // 2
            w = self._measure(row, col, dac, record, meta, dat)
            if header_max is None:
                header_max = w.header
                if header_max == 0:
                    raise RuntimeError(
                        "header count is 0 under qinj_l1a: no L1A reached the chip or the chip does not answer "
                        "(check FC alignment, chip configuration and the selected channel)")
            value = w.data if counter == "data" else w.trigbit
            if value >= header_max / 2.0:
                a = dac
            else:
                b = dac
        w = self._measure(row, col, a, record, meta, dat)
        value = w.data if counter == "data" else w.trigbit
        return {"dac": a, "header_max": header_max if header_max is not None else w.header,
                "counter": counter, "value_at_dac": value, "header_at_dac": w.header,
                "efficiency_at_dac": value / w.header if w.header else math.nan}


def summarise_noise(row: int, col: int, bl: int, nw: int, points: list[tuple[int, int]]) -> dict[str, Any]:
    """Peak of the trigger-bit count vs DAC and its comparison with the auto-cal BL/NW."""
    dacs = [p[0] for p in points]
    counts = [p[1] for p in points]
    peak_count = max(counts)
    peak_dac = dacs[counts.index(peak_count)]
    above_half = [d for d, c in points if peak_count > 0 and c >= peak_count / 2.0]
    fwhm = (max(above_half) - min(above_half)) if above_half else 0
    result = {
        "row": row, "col": col, "baseline": bl, "noise_width": nw,
        "peak_dac": peak_dac, "peak_trigbit": peak_count,
        "fwhm_dac": fwhm, "dac_min": dacs[0], "dac_max": dacs[-1], "n_points": len(points),
        "points": [{"dac": d, "trigbit": c} for d, c in points],
    }
    if bl >= 0:
        result["peak_minus_baseline"] = peak_dac - bl
        result["peak_within_noise_width"] = abs(peak_dac - bl) <= max(nw, 0)
    if peak_count == 0:
        result["warning"] = "no trigger bits at any DAC: trigger path, link or channel selection"
    return result


def summarise_sweep(points: list[tuple[int, int, int, int]], counter: str) -> dict[str, Any]:
    """Turn-on (first DAC with trigger bits) and turn-off (last DAC with counter >= header/2) of a full sweep."""
    turn_on = next((d for d, t, _, _ in points if t > 0), None)
    passing = [d for d, t, data, h in points if h > 0 and (data if counter == "data" else t) >= h / 2.0]
    return {
        "turn_on": {"dac": turn_on, "found": turn_on is not None},
        "turn_off": {"dac": max(passing) if passing else None, "counter": counter, "found": bool(passing)},
        "points": [{"dac": d, "trigbit": t, "data": data, "header": h} for d, t, data, h in points],
    }
