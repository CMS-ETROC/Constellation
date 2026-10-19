"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Counter scans: trigger-bit noise scan around the auto-calibrated baseline, and the
fast QInj S-curve edges (binary search or full sweep), as done in 2023-24 with
i2c_gui `scripts/TIDboard_noisePeak.py` and ETROC-DAQ `Save_FPGA_data`; and the ACC
scan (measurement plan A1): the pixel's own discriminator duty cycle (threshold-
calibration accumulator, bypass mode) against the trigger-bit and data counters.

Outputs, under `<output_path>/<YYYY-MM-DD>_Array_Test_Results/`:
- one directory per pixel (and charge) with the 7-column `FPGA_Data.dat` the old
  analysis parsers read: refresh,en_L1A,window_s,data,header,trigbit,DAC
  (old directory names: <chip>_VRef_SCurve_NoiseOnly_Pixel_C<c>_R<r>,
  <chip>_VRef_SCurve_BinarySearch_TurnOn_Pixel_C<c>_R<r>,
  <chip>_VRef_SCurve_BinarySearch_TurnOff_Pixel_C<c>_R<r>_QInj_<qsel>);
- one `<chip>_<scan>_<HHMMSS>_scan/` directory (`..._<HHMMSS>_rN_scan/` for a scan started in
  the same second as the previous one) with `counters.csv` (every measured
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
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .chipio import ACC_WINDOW_CLK40, DAC_MAX, OPEN_WINDOWS, QSEL_MAX, ChipIO
from .counters import WINDOW_CLK40, WINDOW_S, CounterReader, CounterWindow

# L1Adelay used on the bench with the qinj_l1a pattern (KC7-SW-008: the pattern spaces L1A 0x1f8 BX after QInj)
QINJ_L1A_SPACING_DEFAULT_DELAY = 0x1F5

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
    "dac_min", "dac_max", "step", "tag", "fc_mode", "together", "trig_path",
}
ACC_SCAN_KEYS = {
    "pixels", "qsel", "together", "rel_min", "rel_max", "fine_range", "fine_step", "coarse_step", "center",
    "dac_min", "dac_max", "acc_reads", "windows", "tag",
}
ACC_SCAN_DEFAULTS = {
    "rel_min": -16, "rel_max": 24, "fine_range": 8, "fine_step": 1, "coarse_step": 4, "acc_reads": 3, "windows": 3,
}
# upper end of the turn-off bisection that centres an ACC scan with charge injection (as qinj_scurve)
ACC_TURNOFF_DAC_MAX = 1020


def validate_scan_params(kind: str, params: dict[str, Any]) -> None:
    """Reject unknown keys and malformed values before a scan thread is started."""
    allowed = {"noise_scan": NOISE_SCAN_KEYS, "qinj_scurve": QINJ_SCURVE_KEYS, "acc_scan": ACC_SCAN_KEYS}[kind]
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"{kind}: unknown parameters {unknown}; allowed {sorted(allowed)}")
    parse_pixels(params.get("pixels"))
    if kind == "acc_scan":
        _validate_acc_params(params)
        return
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
    if "fc_mode" in params and not (isinstance(params["fc_mode"], str) and params["fc_mode"].strip()):
        raise ValueError("qinj_scurve: 'fc_mode' must be a non-empty string (an FC mode with QInj and L1A)")
    for key in ("together", "trig_path"):
        if key in params and not isinstance(params[key], bool):
            raise ValueError(f"qinj_scurve: '{key}' must be true or false")
    sweep = params.get("method", "binary") == "sweep"
    if params.get("together", False) and not (sweep and "dac_min" in params and "dac_max" in params):
        raise ValueError("qinj_scurve: 'together' needs method 'sweep' with absolute dac_min and dac_max")
    if params.get("trig_path", True) is False and not sweep:
        raise ValueError("qinj_scurve: 'trig_path': false needs method 'sweep' (the turn-on search uses the trigger bits)")
    if sweep and params.get("dac_min", 0) > params.get("dac_max", DAC_MAX):
        raise ValueError("qinj_scurve: dac_min > dac_max")


def _validate_acc_params(params: dict[str, Any]) -> None:
    for key in ("rel_min", "rel_max", "fine_range", "fine_step", "coarse_step", "center", "dac_min", "dac_max",
                "acc_reads", "windows"):
        if key in params and not (isinstance(params[key], int) and not isinstance(params[key], bool)):
            raise ValueError(f"acc_scan: '{key}' must be an integer")
    for key in ("fine_step", "coarse_step", "acc_reads", "windows"):
        if params.get(key, 1) < 1:
            raise ValueError(f"acc_scan: '{key}' must be >= 1")
    if params.get("fine_range", 0) < 0:
        raise ValueError("acc_scan: 'fine_range' must be >= 0")
    for key in ("center", "dac_min", "dac_max"):
        if key in params and not 0 <= params[key] <= DAC_MAX:
            raise ValueError(f"acc_scan: '{key}' outside 0-{DAC_MAX}")
    if ("dac_min" in params) != ("dac_max" in params):
        raise ValueError("acc_scan: give both dac_min and dac_max, or neither")
    if params.get("dac_min", 0) > params.get("dac_max", DAC_MAX):
        raise ValueError("acc_scan: dac_min > dac_max")
    if params.get("rel_min", -16) > params.get("rel_max", 24):
        raise ValueError("acc_scan: rel_min > rel_max")
    qsel = params.get("qsel")
    if qsel is not None and not (isinstance(qsel, int) and not isinstance(qsel, bool) and 0 <= qsel <= QSEL_MAX):
        raise ValueError(f"acc_scan: qsel must be null (noise) or an integer 0-{QSEL_MAX}")
    if not isinstance(params.get("together", False), bool):
        raise ValueError("acc_scan: 'together' must be true or false")


def fine_coarse_grid(center: int, lo: int, hi: int, fine_range: int, fine_step: int, coarse_step: int) -> list[int]:
    """DACs in [lo, hi]: every fine_step within center +- fine_range (counted from center), every coarse_step
    outside it (counted outwards from the outermost fine points), and lo and hi themselves. Not clipped."""
    if lo > hi:
        return []
    f_lo, f_hi = max(center - fine_range, lo), min(center + fine_range, hi)
    if f_lo > f_hi:
        # the fine region lies outside [lo, hi]: coarse steps throughout
        return sorted(set(range(lo, hi + 1, coarse_step)) | {hi})
    fine = {d for d in range(center, f_hi + 1, fine_step)} | {d for d in range(center, f_lo - 1, -fine_step)}
    fine = {d for d in fine if f_lo <= d <= f_hi} or {f_lo}
    below = set(range(min(fine) - coarse_step, lo - 1, -coarse_step))
    above = set(range(max(fine) + coarse_step, hi + 1, coarse_step))
    return sorted(fine | below | above | {lo, hi})


def acc_dac_grid(center: int, lo: int, hi: int, fine_range: int, fine_step: int, coarse_step: int) -> list[int]:
    """fine_coarse_grid with [lo, hi] clipped to the DAC range 0-DAC_MAX first (so 0 or DAC_MAX is an end point)."""
    return fine_coarse_grid(center, max(lo, 0), min(hi, DAC_MAX), fine_range, fine_step, coarse_step)


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
        base_id = scan_id = f"{self.chip_name}_{scan_type}_{stamp}"
        scan_dir = self._day_dir() / f"{scan_id}_scan"
        n = 1
        while scan_dir.exists():
            # a scan started in the same second as the previous one gets its own directory (_rN), never its files
            n += 1
            scan_id = f"{base_id}_r{n}"
            scan_dir = self._day_dir() / f"{scan_id}_scan"
        scan_dir.mkdir(parents=True)
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
        """Play `mode` inside the block and the previous pattern afterwards, whatever happens. A change that
        fails while the FC memory is being written (its name not yet taken) is re-programmed as well."""
        previous = self.get_fc_mode()
        changed = False
        try:
            if mode != previous:
                changed = True
                self.set_fc_mode(mode)
            yield
        finally:
            if changed or self.get_fc_mode() != previous:
                self._restore_fc(previous)

    def _restore_fc(self, previous: str) -> None:
        try:
            self.set_fc_mode(previous)
            return
        except Exception as e:
            self.log.critical(f"FAILED to restore fast-command mode '{previous}': {e}; trying idle")
        try:
            self.set_fc_mode("idle")
        except Exception as e:
            self.log.critical(f"FAILED to set the fast commands idle: {e}")

    def _measure(self, row: int, col: int, dac: int, record: dict[str, Any], meta: dict[str, Any], dat) -> CounterWindow:
        self._check_stop()
        self.chip.set_dac(row, col, dac)
        window = self._wait_windows(self.settle_windows)
        dat.write(fpga_data_line(window, dac))
        dat.flush()
        self._record_window(row, col, dac, window, record, meta)
        return window

    def _wait_windows(self, increments: int, start_refresh: int | None = None) -> CounterWindow:
        try:
            return self.reader.wait_windows(increments, self.stop_event, start_refresh=start_refresh)
        except InterruptedError as e:
            raise ScanAborted("scan stopped on request") from e

    def _record_window(self, row, col, dac, window: CounterWindow, record: dict[str, Any], meta: dict[str, Any]) -> None:
        """One counters.csv row, the status counters and the on_window callback."""
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
        self.status["last"] = {"row": row, "col": col, "dac": dac, "phase": meta.get("phase", ""),
                               "trigbit": window.trigbit, "data": window.data, "header": window.header}
        if self.on_window is not None:
            self.on_window(window)

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
        BL-10 .. BL+150, step 2); tag ("");
        fc_mode ("qinj_l1a"): the FC pattern during the injection (a custom memo with QInj and L1A, e.g.
        "custom Start QInj L1A Triggerbit multiple=64" for the rate ladder or "... l1a_offset=K" for the
        FC-to-L1A pairing); the auto-calibration still runs with idle FC and the previous pattern is
        restored afterwards; together (False, sweep with absolute dac_min/dac_max only): all pixels
        enabled at once at the same DAC; trig_path (True; False = disTrigPath 1, sweep only).
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
        fc_mode = str(params.get("fc_mode", "qinj_l1a")).strip()
        together = bool(params.get("together", False))
        trig_path = bool(params.get("trig_path", True))

        if self.l1a_delay != QINJ_L1A_SPACING_DEFAULT_DELAY:
            self.log.warning(
                f"l1a_delay 0x{self.l1a_delay:03x} differs from 0x{QINJ_L1A_SPACING_DEFAULT_DELAY:03x}, the value used "
                f"with this FC pattern on the bench (QInj at BX 0x005, L1A at BX 0x1fd); headers may carry no hits"
            )

        with self._scan_files(f"QInjSCurve_{method}", dict(params)) as record:
            # the on-chip calibration runs with IDLE fast commands, before charge is injected
            baselines = {}
            with self._fc_session("idle"):
                for row, col in pixels:
                    self._check_stop()
                    with self._pixel_session(row, col):
                        baselines[(row, col)] = self._baseline(row, col, autocal=True)
            base_fields = {"disDataReadout": 0, "QInjEn": 1, "disTrigPath": 0 if trig_path else 1, "Bypass_THCal": 1,
                           "enable_TDC": 1, "L1Adelay": self.l1a_delay, **OPEN_WINDOWS}
            with self._fc_session(fc_mode):
                if together:
                    for qsel in qsels:
                        record["results"].append(
                            self._sweep_together(pixels, baselines, base_fields, qsel, params, counter, tag, record))
                    return record
                for row, col in pixels:
                    with self._pixel_session(row, col):
                        bl, nw = baselines[(row, col)]
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

    def _sweep_together(self, pixels, baselines, base_fields, qsel, params, counter, tag, record) -> dict[str, Any]:
        """All listed pixels injected and read out at once, every one at the same DAC of the sweep (measurement
        plan A2: hits under the L1A rate ladder for 1, 4, 16 pixels). Rows and columns are written as 'r1;r2;...'."""
        lo, hi, st = int(params["dac_min"]), int(params["dac_max"]), int(params.get("step", 2))
        rows = ";".join(str(r) for r, _ in pixels)
        cols = ";".join(str(c) for _, c in pixels)
        meta = {"qsel": qsel, "charge_fC": qsel + 1, "phase": "sweep_together",
                "baseline": ";".join(str(baselines[p][0]) for p in pixels),
                "noise_width": ";".join(str(baselines[p][1]) for p in pixels)}
        points = []
        with ExitStack() as stack:
            for row, col in pixels:
                stack.enter_context(self._pixel_session(row, col))
            for row, col in pixels:
                self.chip.write_pixel(row, col, {**base_fields, "QSel": qsel, "DAC": DAC_MAX})
            dat_path = self._pixel_file(
                f"{self.chip_name}_VRef_SCurve_Sweep_Together_{len(pixels)}px_QInj_{qsel}{tag}")
            record["pixel_files"].append(str(dat_path))
            with open(dat_path, "w") as dat:
                for dac in range(max(lo, 0), min(hi, DAC_MAX) + 1, st):
                    self._check_stop()
                    for row, col in pixels:
                        self.chip.set_dac(row, col, dac)
                    w = self._wait_windows(self.settle_windows)
                    dat.write(fpga_data_line(w, dac))
                    dat.flush()
                    self._record_window(rows, cols, dac, w, record, meta)
                    points.append((dac, w.trigbit, w.data, w.header))
        result = {"pixels": [list(p) for p in pixels], "together": True, "n_pixels": len(pixels), "qsel": qsel,
                  "charge_fC": qsel + 1, "baselines": [baselines[p][0] for p in pixels],
                  "noise_widths": [baselines[p][1] for p in pixels], "method": "sweep"}
        result.update(summarise_sweep(points, counter))
        # the group's turn-off (counter >= header/2) is not a per-pixel efficiency; data / header / pixels is
        result["hits_per_l1a_per_pixel"] = [
            (data / h / len(pixels)) if h else None for _, _, data, h in points]
        return result

    # ------------------------------------------------------------------ ACC scan
    def acc_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        """Measurement plan A1: per DAC, the pixel's discriminator duty cycle from its own threshold-calibration
        accumulator (C1 = ACC/32768, bypass mode) against the trigger-bit counter (C2 = trigbit per clk40 of
        the 1 s window) and the data counter under L1A (C3 = data/header).

        params: pixels (required); qsel (null: noise, QInjEn 0; an int: QInjEn 1, QSel, L1Adelay = l1a_delay);
        rel_min (-16) .. rel_max (24) around `center`, or dac_min/dac_max absolute; fine_step (1) within
        center +- fine_range (8), coarse_step (4) outside; center (default: the auto-cal BL for noise, the
        data-counter turn-off found by bisection for qsel); acc_reads (3); windows (3); together (false):
        afterwards all listed pixels enabled at once (the OR term); tag ("").

        Noise, per DAC: DAC; RSTn_THCal pulse; ScanStart_THCal rising edge; 2 ms; ScanDone and ACC
        acc_reads times; `windows` settled windows with FC idle (C2); FC qinj_l1a and `windows` settled
        windows (C3); FC idle. With qsel the charge must be injected while ACC and the trigger bits are
        counted, so the FC stays qinj_l1a for the whole scan and the same windows give C2 and C3.
        """
        validate_scan_params("acc_scan", params)
        pixels = parse_pixels(params.get("pixels"))
        opt = {**ACC_SCAN_DEFAULTS, **{k: v for k, v in params.items() if k in ACC_SCAN_DEFAULTS}}
        qsel = params.get("qsel")
        together = bool(params.get("together", False))
        tag = str(params.get("tag", ""))
        absolute = "dac_min" in params
        if qsel is not None and self.l1a_delay != QINJ_L1A_SPACING_DEFAULT_DELAY:
            self.log.warning(
                f"l1a_delay 0x{self.l1a_delay:03x} differs from 0x{QINJ_L1A_SPACING_DEFAULT_DELAY:03x}: "
                f"C3 headers may carry no hits")

        fields = {"disDataReadout": 0, "QInjEn": 0 if qsel is None else 1, "disTrigPath": 0, "Bypass_THCal": 1,
                  "CLKEn_THCal": 1, "BufEn_THCal": 1, "enable_TDC": 1, **OPEN_WINDOWS}
        if qsel is not None:
            fields.update({"QSel": qsel, "L1Adelay": self.l1a_delay})
        meta_base = {"qsel": "" if qsel is None else qsel, "charge_fC": "" if qsel is None else qsel + 1}
        scan_type = "ACCScan_Noise" if qsel is None else f"ACCScan_QInj_{qsel}"

        with self._scan_files(scan_type + tag, dict(params)) as record:
            record["center_search_files"] = []
            calibration = {}
            with self._fc_session("idle"):
                for row, col in pixels:
                    self._check_stop()
                    with self._pixel_session(row, col):
                        calibration[(row, col)] = self._baseline(row, col, autocal=True)
            centers = {}
            with self._fc_session("idle" if qsel is None else "qinj_l1a"):
                for row, col in pixels:
                    bl, nw = calibration[(row, col)]
                    meta = {**meta_base, "baseline": bl, "noise_width": nw}
                    with self._pixel_session(row, col):
                        self.chip.write_pixel(row, col, {**fields, "DAC": DAC_MAX})
                        center = self._acc_center(row, col, bl, qsel, params, record, meta)
                        centers[(row, col)] = center
                        lo, hi = (params["dac_min"], params["dac_max"]) if absolute else \
                            (center + opt["rel_min"], center + opt["rel_max"])
                        dacs = acc_dac_grid(center, lo, hi, opt["fine_range"], opt["fine_step"], opt["coarse_step"])
                        if not dacs:
                            raise ValueError(f"empty DAC grid for pixel ({row},{col})")
                        points = [self._acc_point([(row, col)], [dac], qsel, opt, record, meta) for dac in dacs]
                    record["results"].append(summarise_acc(row, col, bl, nw, center, qsel, points))
                if together and len(pixels) > 1:
                    record["results"].append(self._acc_together(pixels, calibration, centers, fields, qsel, opt,
                                                                 params, absolute, record, meta_base))
        return record

    def _acc_center(self, row, col, bl, qsel, params, record, meta) -> int:
        """Centre of the fine region: `center` if given, else BL (noise) or the data-counter turn-off (qsel)."""
        if "center" in params:
            return int(params["center"])
        if qsel is None:
            return bl
        path = Path(record["scan_dir"]) / f"center_search_R{row}_C{col}_QInj_{qsel}.dat"
        record["center_search_files"].append(str(path))
        with open(path, "w") as dat:
            found = self._bisect_turn_off(row, col, max(bl, 0), ACC_TURNOFF_DAC_MAX, "data", record,
                                          {**meta, "phase": "center_search"}, dat)
        self.log.info(f"Pixel ({row},{col}) QSel {qsel}: turn-off (data counter) at DAC {found['dac']}")
        return int(found["dac"])

    def _acc_together(self, pixels, calibration, centers, fields, qsel, opt, params, absolute, record, meta_base):
        """All listed pixels enabled at once; each at its own centre + offset (or one absolute DAC)."""
        with ExitStack() as stack:
            for row, col in pixels:
                stack.enter_context(self._pixel_session(row, col))
            for row, col in pixels:
                self.chip.write_pixel(row, col, {**fields, "DAC": DAC_MAX})
            if absolute:
                mean_center = round(sum(centers.values()) / len(centers))
                grid = [[d] * len(pixels) for d in acc_dac_grid(
                    mean_center, params["dac_min"], params["dac_max"], opt["fine_range"], opt["fine_step"],
                    opt["coarse_step"])]
                offsets = [None] * len(grid)
            else:
                offsets, grid = [], []
                for off in fine_coarse_grid(0, opt["rel_min"], opt["rel_max"], opt["fine_range"], opt["fine_step"],
                                            opt["coarse_step"]):
                    dacs = [centers[p] + off for p in pixels]
                    if all(0 <= d <= DAC_MAX for d in dacs):
                        offsets.append(off)
                        grid.append(dacs)
            meta = {**meta_base, "baseline": ";".join(str(calibration[p][0]) for p in pixels),
                    "noise_width": ";".join(str(calibration[p][1]) for p in pixels)}
            points = []
            for off, dacs in zip(offsets, grid):
                point = self._acc_point(pixels, dacs, qsel, opt, record, meta)
                point["rel"] = off
                c1 = point["c1"]
                none_high = 1.0
                for c in c1:
                    none_high *= 1.0 - c
                # expected OR of independent pixels, for comparison with C2
                point["c1_or_indep"] = 1.0 - none_high
                points.append(point)
        return {"pixels": [list(p) for p in pixels], "together": True, "qsel": qsel,
                "baselines": [calibration[p][0] for p in pixels], "noise_widths": [calibration[p][1] for p in pixels],
                "centers": [centers[p] for p in pixels], "n_points": len(points), "points": points,
                "flagged_points": [p["dac"] for p in points if p.get("flag")]}

    def _acc_point(self, group, dacs, qsel, opt, record, meta) -> dict[str, Any]:
        """One DAC step of the A1 procedure for one pixel (or a group enabled together)."""
        self._check_stop()
        for (row, col), dac in zip(group, dacs):
            self.chip.set_dac(row, col, dac)
        accs, dones = [], []
        for row, col in group:
            acc, done = self.chip.measure_acc(row, col, opt["acc_reads"])
            accs.append(acc)
            dones.append(done)
        if len(group) == 1:
            row, col = group[0]
            csv_dac = dacs[0]
        else:
            row, col = ";".join(str(p[0]) for p in group), ";".join(str(p[1]) for p in group)
            csv_dac = ";".join(str(d) for d in dacs)
        if qsel is None:
            c2_windows = self._acc_windows(opt["windows"], row, col, csv_dac, record, {**meta, "phase": "c2_idle"})
            with self._fc_session("qinj_l1a"):  # back to the idle of the enclosing session, also on errors
                c3_windows = self._acc_windows(opt["windows"], row, col, csv_dac, record,
                                               {**meta, "phase": "c3_qinj_l1a"})
        else:
            c2_windows = c3_windows = self._acc_windows(opt["windows"], row, col, csv_dac, record,
                                                        {**meta, "phase": "c2c3_qinj_l1a"})
        trig = [w.trigbit for w in c2_windows]
        data = [w.data for w in c3_windows]
        header = [w.header for w in c3_windows]
        c1 = [sum(a) / len(a) / ACC_WINDOW_CLK40 for a in accs]
        point = {
            "dac": dacs[0] if len(group) == 1 else list(dacs),
            "acc": accs[0] if len(group) == 1 else accs,
            "scan_done": dones[0] if len(group) == 1 else dones,
            "c1": c1[0] if len(group) == 1 else c1,
            "c2": trig,
            "c2_duty": sum(trig) / len(trig) / WINDOW_CLK40,
            "c3": {"data": data, "header": header},
            "c3_eff": sum(data) / sum(header) if sum(header) else None,
        }
        if any(v != 1 for done in dones for v in done):
            point["flag"] = "scan_done_not_1"
            self.log.warning(f"ACC scan pixel(s) {group} DAC {dacs}: ScanDone read {dones}, point flagged")
        self.status["acc_points"] = self.status.get("acc_points", 0) + 1
        return point

    def _acc_windows(self, n, row, col, dac, record, meta) -> list[CounterWindow]:
        """`n` consecutive windows, the first one settled (settle_windows after the last change)."""
        windows = [self._wait_windows(self.settle_windows)]
        while len(windows) < n:
            windows.append(self._wait_windows(1, start_refresh=windows[-1].refresh))
        for w in windows:
            self._record_window(row, col, dac, w, record, meta)
        return windows

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


def level_crossing(points: list[tuple[int, float]], level: float, center: float) -> float | None:
    """DAC where the curve crosses `level` (linear interpolation between neighbouring points), the crossing
    nearest `center` if there are several; None if it never crosses."""
    found = []
    for (d0, v0), (d1, v1) in zip(points, points[1:]):
        if v0 == level:
            found.append(float(d0))
        elif (v0 - level) * (v1 - level) < 0:
            found.append(d0 + (level - v0) * (d1 - d0) / (v1 - v0))
    if points and points[-1][1] == level:
        found.append(float(points[-1][0]))
    return min(found, key=lambda x: abs(x - center)) if found else None


def summarise_acc(row: int, col: int, bl: int, nw: int, center: int, qsel: int | None,
                  points: list[dict[str, Any]]) -> dict[str, Any]:
    """Derived quantities of one pixel's three curves.

    `<curve>_bl` is the DAC where the curve crosses 0.5, `<curve>_nw` half the DAC distance between its
    0.16 and 0.84 crossings (one sigma for a Gaussian-noise S-curve), for curve = acc (C1), trig (C2) and
    data (C3). Noise: absolute levels. With qsel the curves are divided by their maximum first (C1 and C2
    then reach only the pulse duty cycle, about pulse BX per orbit), so the crossings are the 50 % turn-off.
    `acc_bl_vs_cal` = acc_bl - auto-cal BL is the positive control of plan A1 (a): within 1 LSB in noise mode.
    `acc_nw_transition` is the manual's NW definition (DAC codes with 0 < ACC < 32768), meaningful on a
    fine grid.
    """
    dacs = [p["dac"] for p in points]
    result: dict[str, Any] = {
        "row": row, "col": col, "qsel": qsel, "charge_fC": None if qsel is None else qsel + 1,
        "baseline": bl, "noise_width": nw, "center": center, "dac_min": dacs[0], "dac_max": dacs[-1],
        "n_points": len(points), "normalisation": "absolute" if qsel is None else "plateau",
    }
    curves = {"acc": [p["c1"] for p in points], "trig": [p["c2_duty"] for p in points],
              "data": [p["c3_eff"] for p in points]}
    for name, values in curves.items():
        pts = [(d, v) for d, v in zip(dacs, values) if v is not None]
        if qsel is not None:
            plateau = max((v for _, v in pts), default=0.0)
            result[f"{name}_plateau"] = plateau
            pts = [(d, v / plateau) for d, v in pts] if plateau > 0 else []
        bl50 = level_crossing(pts, 0.5, center)
        lo, hi = level_crossing(pts, 0.16, center), level_crossing(pts, 0.84, center)
        result[f"{name}_bl"] = bl50
        result[f"{name}_nw"] = abs(hi - lo) / 2.0 if lo is not None and hi is not None else None
    acc_bl = result["acc_bl"]
    result["acc_bl_vs_cal"] = acc_bl - bl if acc_bl is not None else None
    if qsel is None:
        result["positive_control"] = None if acc_bl is None else ("pass" if abs(acc_bl - bl) <= 1 else "fail")
    mean_acc = [sum(p["acc"]) / len(p["acc"]) for p in points]
    transition = [d for d, a in zip(dacs, mean_acc) if 0 < a < ACC_WINDOW_CLK40]
    result["acc_nw_transition"] = len(transition)
    result["flagged_points"] = [p["dac"] for p in points if p.get("flag")]
    if result["flagged_points"]:
        result["warning"] = "ScanDone was not 1 at the flagged points: their ACC is not a completed window"
    result["points"] = points
    return result
