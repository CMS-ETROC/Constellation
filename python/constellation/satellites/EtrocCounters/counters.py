"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

KC705 per-channel counters ("FPGA scalars") of the ETROC2TestFirmware SEU2025 build.

Firmware facts this module relies on (SEU2025 d1248d5):
- Status page 0 (`kc705_mig.v`, `assign status_reg`):
  {combineTriggerCount[15:0], trigCounter[31:0], refreshCounter[15:0], headerCounter[31:0],
   dataCounter[31:0], 8'd0, LED8Bit[7:0], 8'd0, 8'd0, 7'd0, ws_ready}
  zero-extended to 176 bits; status word n = status_reg[16n+15:16n]
  (`src/control_interface.vhd`, STATUS_REG read).
- Status page = config Reg13[1:0]; LED_Pages = Reg13[4:2]; the channel whose counters
  are shown is monitorSel = LED_Pages[1:0] = Reg13[3:2].
- Window fixed at 1 s (40,000,001 clk40 cycles, `src/ETROCBoard.v`); the counters are
  latched at the end of each window, refreshCounter counts completed windows, and
  everything is zeroed while the link is not aligned (RC_aligned & trigSynched) and by
  the clear_fifo pulse.
- Status page 2 carries fixed patterns: words 6-10 = 0x3C5C and word 5 = {14'h0F17,
  session_DAQ_on, hist_fill_done}. They are used to check the word addressing on hardware.
"""

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..EtrocCommon import cmd_interpret, fast_command

WINDOW_S = 1.0
WINDOW_CLK40 = 40_000_001
ORBIT_BX = 3564
# One QInj + one L1A per orbit in "qinj_l1a" mode
EXPECTED_L1A_PER_WINDOW = WINDOW_CLK40 / ORBIT_BX

REG13 = 13
REG8 = 8
STATUS_PAGE_MASK = 0b11
LED_PAGES_SHIFT = 2
LED_PAGES_MASK = 0b111 << LED_PAGES_SHIFT
PAGE_COUNTERS = 0b00
PAGE_FIXED_PATTERN = 0b10
PAGE2_PATTERN_WORD = 0x3C5C
PAGE2_WORD5_CONSTANT = 0x0F17

FC_MODES = {
    "idle": "Start Triggerbit",
    "qinj_l1a": "Start QInj L1A Triggerbit",
}

# Config registers EtrocCounters may write at launch (never Reg7: on SEU2025 it holds
# the FC delay line and trigger-bit delays, not a counter window).
WRITABLE_REGISTERS = ("fc_delays", "data_delays_01", "data_delays_23", "triggerbit_delay", "polarity", "active_channel")


class CounterTimeoutError(RuntimeError):
    """The refresh counter did not advance as expected."""


def _bits(value: int, lo: int, width: int) -> int:
    return (value >> lo) & ((1 << width) - 1)


def words_to_int(words: list[int]) -> int:
    """Assemble status words 0-10 into the 176-bit status register value."""
    value = 0
    for n, w in enumerate(words):
        value |= (w & 0xFFFF) << (16 * n)
    return value


def decode_page0(words: list[int]) -> dict[str, int]:
    """Decode the 11 status words of page 0 (SEU2025 layout)."""
    if len(words) != cmd_interpret.STATUS_REGISTER_COUNT:
        raise ValueError(f"Expected {cmd_interpret.STATUS_REGISTER_COUNT} status words, got {len(words)}")
    v = words_to_int(words)
    return {
        "ws_ready": _bits(v, 0, 1),
        "led8": _bits(v, 24, 8),
        "data": _bits(v, 40, 32),
        "header": _bits(v, 72, 32),
        "refresh": _bits(v, 104, 16),
        "trigbit": _bits(v, 120, 32),
        "combine": _bits(v, 152, 16),
    }


def refresh_from_words(w6: int, w7: int) -> int:
    """refreshCounter from status words 6 ({refresh[7:0], header[31:24]}) and 7 ({trig[7:0], refresh[15:8]})."""
    return ((w6 >> 8) & 0xFF) | ((w7 & 0xFF) << 8)


def decode_led8(led8: int) -> dict[str, int]:
    """LED8Bit when LED_Pages[2] = 0: {monitorSel[1:0], isDataEx, isHeadEx, trigSynched, trigError, synched, dataError}.

    Bits 7, 5, 3 and 1 are XOR-ed with a flashing align-error indicator, so they toggle
    while a channel has an alignment error; bits 6, 4, 2, 0 are not modulated.
    """
    return {
        "monitor_sel": (led8 >> 6) & 0b11,
        "is_data_ex": (led8 >> 5) & 1,
        "is_head_ex": (led8 >> 4) & 1,
        "trig_synched": (led8 >> 3) & 1,
        "trig_error": (led8 >> 2) & 1,
        "synched": (led8 >> 1) & 1,
        "data_error": led8 & 1,
    }


def refresh_delta(new: int, old: int) -> int:
    """Increments of the 16-bit refreshCounter from old to new (wraps after 65536 s)."""
    return (new - old) & 0xFFFF


@dataclass
class CounterWindow:
    """One completed 1 s counting window as latched by the firmware."""

    refresh: int
    data: int
    header: int
    trigbit: int
    combine: int
    channel: int
    en_l1a: int
    led8: int
    ws_ready: int
    unix_time: float
    status_words: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status_words"] = [f"0x{w:04x}" for w in self.status_words]
        d["led_bits"] = decode_led8(self.led8)
        d["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.unix_time))
        return d


class KC705Link:
    """Thread-safe register access to the KC705 over its single TCP stream."""

    def __init__(self, hostname: str, port: int, connect_timeout: float = 5.0, io_timeout: float | None = 5.0):
        self.hostname = hostname
        self.port = port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self.lock = threading.RLock()
        self.sock = None

    def open(self) -> None:
        with self.lock:
            self.close()
            self.sock = cmd_interpret.open_socket(self.hostname, self.port, self.connect_timeout, self.io_timeout)

    def close(self) -> None:
        with self.lock:
            cmd_interpret.close_socket(self.sock)
            self.sock = None

    @property
    def is_open(self) -> bool:
        return self.sock is not None

    def _require(self):
        if self.sock is None:
            raise RuntimeError("KC705 link is not open")
        return self.sock

    def read_config(self, reg: int) -> int:
        with self.lock:
            return cmd_interpret.read_config_reg(self._require(), reg)

    def write_config(self, reg: int, value: int) -> None:
        if reg == 7:
            raise ValueError("Refusing to write config Reg7: on SEU2025 it is the FC delay / trigger-bit delay, not a counter window")
        with self.lock:
            cmd_interpret.write_config_reg(self._require(), reg, value)

    def write_named_config(self, name: str, value: int) -> None:
        if name not in WRITABLE_REGISTERS:
            raise ValueError(f"EtrocCounters may only write {WRITABLE_REGISTERS}, not '{name}'")
        self.write_config(cmd_interpret.CONFIG_REGISTERS[name], value)

    def read_status(self, addr: int) -> int:
        with self.lock:
            return cmd_interpret.read_status_reg(self._require(), addr)

    def read_status_words(self) -> list[int]:
        with self.lock:
            sock = self._require()
            return [cmd_interpret.read_status_reg(sock, n) for n in range(cmd_interpret.STATUS_REGISTER_COUNT)]

    def configure_fc(self, memo: str, prescale_factor: int = 2048) -> None:
        with self.lock:
            fast_command.configure_memo_fc(self._require(), memo, prescale_factor)


class CounterReader:
    """Selects the counter page/channel and reads complete, settled windows."""

    def __init__(self, link: KC705Link, window_s: float = WINDOW_S, poll_s: float = 0.1):
        self.link = link
        self.window_s = window_s
        self.poll_s = poll_s

    # --- Reg13 handling ---------------------------------------------------
    def _update_reg13(self, clear_mask: int, set_bits: int) -> int:
        with self.link.lock:
            reg13 = self.link.read_config(REG13)
            new = (reg13 & ~clear_mask & 0xFFFF) | set_bits
            if new != reg13:
                self.link.write_config(REG13, new)
            return new

    def select_page(self, page: int) -> None:
        self._update_reg13(STATUS_PAGE_MASK, page & STATUS_PAGE_MASK)

    def set_channel(self, channel: int) -> None:
        """Show the counters of `channel` (0-3): Reg13[4:2] = 0b0cc, status page 0."""
        if not 0 <= channel <= 3:
            raise ValueError(f"channel must be 0-3, got {channel}")
        self._update_reg13(LED_PAGES_MASK | STATUS_PAGE_MASK, (channel << LED_PAGES_SHIFT) | PAGE_COUNTERS)

    def get_channel(self) -> int:
        return (self.link.read_config(REG13) >> LED_PAGES_SHIFT) & 0b11

    def en_l1a(self) -> int:
        """Reg8 bit 10 (enableL1Trig: FPGA issues L1A on received trigger bits); column 2 of FPGA_Data.dat."""
        return (self.link.read_config(REG8) >> 10) & 1

    # --- layout self-test -----------------------------------------------
    def check_status_layout(self) -> dict[str, Any]:
        """Read the fixed patterns of status page 2 to confirm word n = status_reg[16n+15:16n]."""
        with self.link.lock:
            self.select_page(PAGE_FIXED_PATTERN)
            try:
                words = self.link.read_status_words()
            finally:
                self.select_page(PAGE_COUNTERS)
        ok_pattern = all(w == PAGE2_PATTERN_WORD for w in words[6:11])
        ok_word5 = (words[5] >> 2) == PAGE2_WORD5_CONSTANT
        return {
            "ok": ok_pattern and ok_word5,
            "words": [f"0x{w:04x}" for w in words],
            "expected": "words 6-10 = 0x3c5c, word 5 >> 2 = 0x0f17",
        }

    # --- reading -----------------------------------------------------------
    def read_refresh(self) -> int:
        with self.link.lock:
            return refresh_from_words(self.link.read_status(6), self.link.read_status(7))

    def read_window(self) -> CounterWindow:
        """Read the last completed window of the selected channel (page 0 is selected first).

        The 11 words are read one by one; if the refresh counter changed while reading,
        the read is repeated so that all values belong to the same window.
        """
        with self.link.lock:
            self.select_page(PAGE_COUNTERS)
            reg13 = self.link.read_config(REG13)
            en_l1a = self.en_l1a()
            for _ in range(3):
                words = self.link.read_status_words()
                again = refresh_from_words(self.link.read_status(6), self.link.read_status(7))
                fields = decode_page0(words)
                if again == fields["refresh"]:
                    break
            else:
                raise CounterTimeoutError("refresh counter changed during three consecutive reads")
        return CounterWindow(
            refresh=fields["refresh"],
            data=fields["data"],
            header=fields["header"],
            trigbit=fields["trigbit"],
            combine=fields["combine"],
            channel=(reg13 >> LED_PAGES_SHIFT) & 0b11,
            en_l1a=en_l1a,
            led8=fields["led8"],
            ws_ready=fields["ws_ready"],
            unix_time=time.time(),
            status_words=words,
        )

    def wait_windows(self, increments: int = 2, stop_event: threading.Event | None = None) -> CounterWindow:
        """Wait until `increments` windows have completed after now, then read the latest one.

        Call this AFTER changing a DAC or the FC pattern: with increments = 2 the first
        window may still be partial, the second one started after the change.
        A refresh counter that goes backwards means the counters were reset (link lost
        alignment or clear_fifo); the wait then starts over once.
        """
        self.select_page(PAGE_COUNTERS)
        start = self.read_refresh()
        deadline = time.monotonic() + (increments + 2) * self.window_s + 1.0
        restarted = False
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("stop requested")
            current = self.read_refresh()
            delta = refresh_delta(current, start)
            if delta >= increments and delta < 0x8000:
                return self.read_window()
            if delta >= 0x8000:
                if restarted:
                    raise CounterTimeoutError("refresh counter went backwards twice: counters keep being reset (link alignment lost?)")
                restarted = True
                start = current
                deadline = time.monotonic() + (increments + 2) * self.window_s + 1.0
            if time.monotonic() > deadline:
                raise CounterTimeoutError(
                    f"refresh counter did not advance by {increments} within {(increments + 2) * self.window_s + 1.0:.1f} s "
                    f"(stuck at {current}): the link of the selected channel is not aligned "
                    f"(RC_aligned & trigSynched) or the board is held in reset"
                )
            time.sleep(self.poll_s)
