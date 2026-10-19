"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

A simulated KC705 whose data FIFO holds events: the stop-order tests (R-KC7-SW-16) and the bench
simulation need FIFO reads that end inside an event.

Event layout (KC705 eventBuilder, as EtrocReceiver decodes it): header word 1 = 0xc3a3c3a in the top
28 bits + the active-channel bits; header word 2 = 0x1 in the top 4 bits, the L1A counter in bits
12-27 and the number of 40-bit ETROC2 frames in bits 2-11; the frames packed MSB first into 32-bit
words (zero padded); trailer = 0b001011 in the top 6 bits, hits in bits 14-25, overflowCount in
bits 11-13, HammingCount in bits 8-10.
"""

import collections
import random

from ...EtrocCounters.tests.fakes import FC_L1A, FC_QINJ, FIFO_FILLER, FakeKC705

EVENT_HEADER_PREFIX = 0xC3A3C3A
EVENT_TRAILER_PREFIX = 0b001011
FRAME_HEADER_PREFIX = 0x3C5C << 2  # top 18 bits of an ETROC2 frame header
CHIP_ID = 0x1ABCD


def pack_frames(frames: list[int]) -> list[int]:
    """40-bit frames, MSB first, into 32-bit words (the last one zero padded)."""
    value, nbits = 0, 0
    for f in frames:
        value = (value << 40) | (f & ((1 << 40) - 1))
        nbits += 40
    nwords = -(-nbits // 32)
    value <<= nwords * 32 - nbits
    return [(value >> (32 * (nwords - 1 - i))) & 0xFFFFFFFF for i in range(nwords)]


def event_words(hits: list[tuple[int, int]], l1a_counter: int = 0, bcid: int = 0, status: int = 0,
                overflow: int = 0) -> list[int]:
    """One single-chip event on channel 0 with one data frame per (row, col) hit."""
    frames = [(FRAME_HEADER_PREFIX << 22) | ((l1a_counter & 0xFF) << 14) | (bcid & 0xFFF)]
    for row, col in hits:
        frames.append((1 << 39) | ((col & 0xF) << 33) | ((row & 0xF) << 29) | (200 << 19) | (100 << 10) | 180)
    frames.append(((CHIP_ID & 0x1FFFF) << 22) | ((status & 0x3F) << 16) | ((len(hits) & 0xFF) << 8))
    head = [
        (EVENT_HEADER_PREFIX << 4) | 0b0001,
        (0x1 << 28) | ((l1a_counter & 0xFFFF) << 12) | ((len(frames) & 0x3FF) << 2),
    ]
    trailer = (EVENT_TRAILER_PREFIX << 26) | ((len(hits) & 0xFFF) << 14) | ((overflow & 0x7) << 11)
    return head + pack_frames(frames) + [trailer]


def ends_inside_event(words: list[int]) -> bool:
    """Walk a word stream with the event lengths of the headers; True if it stops before a trailer."""
    i, n = 0, len(words)
    while i < n:
        if words[i] >> 4 == EVENT_HEADER_PREFIX:
            if i + 1 >= n:
                return True
            nframes = (words[i + 1] >> 2) & 0x3FF
            end = i + 2 + -(-40 * nframes // 32)  # index of the trailer
            if end >= n:
                return True
            i = end + 1
        else:
            i += 1
    return False


class EventFifoKC705(FakeKC705):
    """FakeKC705 whose FIFO reads return events while the DAQ runs and the FC plays L1As.

    The stream is built lazily, one read at a time: a gap of fillers, then an event, until the read is
    full; the rest of the last event stays queued for the next read, as on the board. A FIFO reset
    empties the queue; idling the FC stops new events, the queued words still come out.
    `on_fifo_read(n)` (optional) is called with the running read count of the run.
    """

    def __init__(self, *args, hits=((0, 14),), gap=(0, 4), seed=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.hits = list(hits)
        self.gap = gap
        self.rng = random.Random(seed)
        self.queue = collections.deque()
        self.events_generated = 0
        self.on_fifo_read = None

    def _l1a_playing(self) -> bool:
        return bool(self.pattern) and FC_L1A in self.pattern and FC_QINJ in self.pattern

    def _fifo_words(self, n: int) -> list[int]:
        filler = FIFO_FILLER << 20
        while len(self.queue) < n:
            if self.daq_running and self._l1a_playing():
                self.queue.extend([filler] * self.rng.randint(*self.gap))
                self.queue.extend(event_words(self.hits, l1a_counter=self.events_generated))
                self.events_generated += 1
            else:
                self.queue.extend([filler] * (n - len(self.queue)))
        return [self.queue.popleft() for _ in range(n)]

    def _handle(self, word: int):
        upper, low = word >> 16, word & 0xFFFF
        if upper == 0x19 and not self.stall_on_fifo_read:
            with self.lock:
                self.fifo_reads += 1
                reads = self.fifo_reads
                words = self._fifo_words(low + 1)
            if self.on_fifo_read is not None:
                self.on_fifo_read(reads)
            return words
        return super()._handle(word)

    def _pulse(self, bits: int) -> None:
        super()._pulse(bits)
        if bits & 0x0002:  # fifo_reset
            self.queue.clear()
