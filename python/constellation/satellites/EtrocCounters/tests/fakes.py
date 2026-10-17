"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Simulated KC705 (TCP register protocol of cmd_interpret, SEU2025 status layout) and a
simulated ETROC2 pixel matrix behind the ChipIO interface.

The status register is assembled here from the firmware concatenation order
({combine, trig, refresh, header, data, 8'd0, LED8, 8'd0, 8'd0, 7'd0, ws_ready},
MSB first), independently of the decoder under test.
"""

import math
import socket
import struct
import threading
import time

from ..chipio import OPEN_WINDOWS, PIXEL_FIELDS, ChipWriteError

CLK40_PER_WINDOW = 40_000_001
GAIN_DAC_PER_FC = 6
NOISE_PEAK = 1_000_000

FC_IDLE, FC_QINJ, FC_L1A = 0x0, 0x5, 0x6


def concat(fields: list[tuple[int, int]]) -> int:
    """Verilog-style {a, b, c}: fields given MSB first as (width, value)."""
    value = 0
    for width, v in fields:
        value = (value << width) | (v & ((1 << width) - 1))
    return value


def pixel_counts(fields: dict[str, int], bl: int, nw: int, injections: int, headers: int) -> tuple[int, int]:
    """(trigger bits, data hits) of one pixel in one 1 s window.

    Noise: Gaussian in DAC around BL with sigma NW/2 (peak NOISE_PEAK per window).
    QInj: every injection fires while BL - NW <= DAC <= BL + GAIN * (QSel + 1).
    """
    if not fields["Bypass_THCal"]:
        return 0, 0
    dac = fields["DAC"]
    sigma = max(nw / 2.0, 1.0)
    noise = int(NOISE_PEAK * math.exp(-0.5 * ((dac - bl) / sigma) ** 2))
    fired = 0
    if fields["QInjEn"] and bl - nw <= dac <= bl + GAIN_DAC_PER_FC * (fields["QSel"] + 1):
        fired = injections
    trig = 0 if fields["disTrigPath"] else noise + fired
    data = 0 if fields["disDataReadout"] or headers == 0 else fired
    return trig, data


class FakeChip:
    """In-memory ETROC2 pixel matrix implementing ChipIO."""

    DEFAULTS = {
        "disDataReadout": 1, "QInjEn": 0, "disTrigPath": 1, "Bypass_THCal": 0, "DAC": 0, "QSel": 0,
        "enable_TDC": 0, "L1Adelay": 0, "CLKEn_THCal": 0, "BufEn_THCal": 0,
        **{k: 0 for k in OPEN_WINDOWS},
    }

    def __init__(self, baselines: dict[tuple[int, int], tuple[int, int]], fail_on_set_dac_call: int | None = None):
        self.baselines = baselines
        self.pixels = {(r, c): dict(self.DEFAULTS) for r in range(16) for c in range(16)}
        self.lock = threading.RLock()
        self.version = 0
        self.set_dac_calls = 0
        self.fail_on_set_dac_call = fail_on_set_dac_call
        self.closed = False

    def snapshot(self) -> dict[tuple[int, int], dict[str, int]]:
        with self.lock:
            return {p: dict(f) for p, f in self.pixels.items()}

    def read_pixel(self, row, col):
        with self.lock:
            return {k: self.pixels[(row, col)][k] for k in PIXEL_FIELDS}

    def write_pixel(self, row, col, fields):
        with self.lock:
            for k, v in fields.items():
                if k not in self.pixels[(row, col)]:
                    raise ChipWriteError(f"unknown field {k}")
                self.pixels[(row, col)][k] = int(v)
            self.version += 1

    def set_dac(self, row, col, dac):
        with self.lock:
            self.set_dac_calls += 1
            if self.fail_on_set_dac_call is not None and self.set_dac_calls >= self.fail_on_set_dac_call:
                raise ChipWriteError("simulated I2C failure")
            self.pixels[(row, col)]["DAC"] = int(dac)
            self.version += 1

    def auto_calibrate(self, row, col):
        with self.lock:
            # the on-chip calibration leaves its own register changes behind
            self.pixels[(row, col)].update({"CLKEn_THCal": 1, "BufEn_THCal": 1, "Bypass_THCal": 0})
            self.version += 1
            return self.baselines[(row, col)]

    def close(self):
        self.closed = True

    def counts(self, injections: int, headers: int) -> tuple[int, int]:
        trig = data = 0
        with self.lock:
            for p, f in self.pixels.items():
                bl, nw = self.baselines.get(p, (500, 4))
                t, d = pixel_counts(f, bl, nw, injections, headers)
                trig += t
                data += d
        return trig, data


class FakeKC705:
    """KC705 register server: one client at a time, 1 s windows compressed to `window_s`."""

    def __init__(
        self,
        chip: FakeChip | None = None,
        window_s: float = 0.05,
        aligned_channels: tuple[int, ...] = (0,),
        reg8: int = 0x0400,
        reg13: int = 0x1F80,
        reversed_words: bool = False,
        l1a_reaches_chip: bool = True,
    ):
        self.chip = chip
        self.window_s = window_s
        self.aligned_channels = set(aligned_channels)
        self.reversed_words = reversed_words
        self.l1a_reaches_chip = l1a_reaches_chip
        self.config = [0] * 64
        self.config[8] = reg8
        self.config[13] = reg13
        self.memory = [0] * 4096
        self.pattern: list[int] | None = None
        self.fc_version = 0
        self.received: list[tuple[str, int, int]] = []
        self.refresh = [0, 0, 0, 0]
        self.latched = [dict(data=0, header=0, trig=0, combine=0) for _ in range(4)]
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(0.1)
        self.port = self._server.getsockname()[1]
        self._threads = [
            threading.Thread(target=self._serve, daemon=True),
            threading.Thread(target=self._tick, daemon=True),
        ]
        self._window_key = None
        self._window_counts = None

    # --- lifecycle --------------------------------------------------------
    def start(self) -> "FakeKC705":
        self._window_key = self._state_key()
        self._window_counts = self._counts()
        for t in self._threads:
            t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self._server.close()

    # --- physics ----------------------------------------------------------
    def _rates(self) -> tuple[int, int]:
        """(injections, L1As) per 1 s window from the playing FC pattern."""
        if not self.pattern:
            return 0, 0
        orbits = CLK40_PER_WINDOW / len(self.pattern)
        injections = int(self.pattern.count(FC_QINJ) * orbits)
        headers = int(self.pattern.count(FC_L1A) * orbits) if self.l1a_reaches_chip else 0
        return injections, headers

    def _counts(self) -> dict[str, int]:
        injections, headers = self._rates()
        trig, data = self.chip.counts(injections, headers) if self.chip is not None else (0, 0)
        return dict(data=data, header=headers, trig=trig, combine=trig)

    def _state_key(self):
        return (self.chip.version if self.chip is not None else 0, self.fc_version)

    def _tick(self) -> None:
        while not self._stop.wait(self.window_s):
            with self.lock:
                key = self._state_key()
                # a window during which the DAC or the FC pattern changed is reported with the
                # values that held when it started (a partial window)
                counts = self._counts() if key == self._window_key else self._window_counts
                for ch in range(4):
                    if ch in self.aligned_channels:
                        self.refresh[ch] = (self.refresh[ch] + 1) & 0xFFFF
                        self.latched[ch] = dict(counts)
                    else:
                        self.refresh[ch] = 0
                        self.latched[ch] = dict(data=0, header=0, trig=0, combine=0)
                self._window_key = key
                self._window_counts = self._counts()

    # --- status pages -----------------------------------------------------
    def _status_value(self) -> int:
        reg13 = self.config[13]
        page = reg13 & 0b11
        led_pages = (reg13 >> 2) & 0b111
        ch = led_pages & 0b11
        if page == 0:
            aligned = ch in self.aligned_channels
            led8 = concat([(2, ch), (1, 1), (1, 1), (1, int(aligned)), (1, 0), (1, int(aligned)), (1, 0)])
            if led_pages & 0b100:
                led8 = 0
            c = self.latched[ch]
            return concat([
                (16, c["combine"]), (32, c["trig"]), (16, self.refresh[ch]), (32, c["header"]),
                (32, c["data"]), (8, 0), (8, led8), (8, 0), (8, 0), (7, 0), (1, 1),
            ])
        if page == 2:
            return concat([(16, 0x3C5C)] * 5 + [(14, 0x0F17), (1, 0), (1, 1), (80, 0x0123_4567_89AB_CDEF_0F0F)])
        return 0

    def _status_word(self, addr: int) -> int:
        n = 10 - addr if self.reversed_words else addr
        return (self._status_value() >> (16 * n)) & 0xFFFF

    # --- protocol ---------------------------------------------------------
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            conn.settimeout(0.1)
            buf = b""
            with conn:
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except (TimeoutError, socket.timeout):
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= 4:
                        (word,) = struct.unpack(">I", buf[:4])
                        buf = buf[4:]
                        reply = self._handle(word)
                        if reply is not None:
                            conn.sendall(struct.pack(">I", reply))

    def _handle(self, word: int) -> int | None:
        upper, low = word >> 16, word & 0xFFFF
        with self.lock:
            if upper & 0x8000:
                sub = upper & 0x7FFF
                if sub >= 0x20:
                    addr = sub - 0x20
                    self.received.append(("read_config", addr, 0))
                    return self.config[addr]
                self.received.append(("read_status", sub, 0))
                return self._status_word(sub)
            if 0x20 <= upper < 0x40:
                addr = upper - 0x20
                self.received.append(("write_config", addr, low))
                self.config[addr] = low
                return None
            if upper == 0x0B:
                self.received.append(("pulse", 0, low))
                self._pulse(low)
                return None
            self.received.append(("other", upper, low))
            return None

    def _pulse(self, bits: int) -> None:
        if bits & 0x0002:  # fifo_reset: global reset, stops FC playback and zeroes the counters
            self.pattern = None
            self.fc_version += 1
            self.refresh = [0, 0, 0, 0]
        if bits & 0x0010:  # fc_init: fill [Reg10[11:0], Reg9[11:0]] with Reg12[3:0]
            lo, hi = self.config[10] & 0xFFF, self.config[9] & 0xFFF
            for a in range(lo, hi + 1):
                self.memory[a] = self.config[12] & 0xF
        if bits & 0x0004:  # fc_signal_start: play memory[0 .. Reg11]
            self.pattern = list(self.memory[: self.config[11] + 1])
            self.fc_version += 1

    # --- inspection -------------------------------------------------------
    def writes_to(self, reg: int) -> list[int]:
        return [v for kind, a, v in self.received if kind == "write_config" and a == reg]

    def pulses(self, mask: int) -> int:
        return sum(1 for kind, _, v in self.received if kind == "pulse" and v & mask)

    def wait_refresh(self, ch: int, n: int, timeout: float = 5.0) -> None:
        start = self.refresh[ch]
        deadline = time.monotonic() + timeout
        while (self.refresh[ch] - start) & 0xFFFF < n:
            if time.monotonic() > deadline:
                raise TimeoutError("fake refresh did not advance")
            time.sleep(self.window_s / 5)
