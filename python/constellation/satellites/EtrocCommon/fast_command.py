"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Fast-command (FC) memory programming for the KC705 ETROC2 test firmware.

The FC memory holds one command per bunch crossing (BX) and is played back
in a loop of Reg11 + 1 BX (0x0deb -> 3564 BX = one LHC orbit). One memory
range is filled per "sequence": Reg12 low byte = command (+ trigger-bit
format bits), Reg10[11:0] = first address, Reg9[11:0] = last address, then
the fc_init pulse writes it. fc_signal_start starts the playback.

Reg12 values used here (0x70 base = trigDataSize 01, period 1):
  0x70 IDLE, 0x72 BCR, 0x75 QInj, 0x76 L1A.

Each L1A is placed QINJ_L1A_SPACING (504) BX after its QInj; the memo word
`l1a_offset=K` moves every L1A by K BX (504 + K), for the FC-to-L1A delay
pairing measurement (R-KC7-SW-22, KC7-SW-008). A placement that leaves the
orbit or lands on another command's address is refused.
"""

import re
import time

from . import cmd_interpret

ORBIT_REG11 = 0x0DEB
IDLE_CMD = 0x0
BCR_CMD = 0x2
QINJ_CMD = 0x5
L1A_CMD = 0x6
BASE_REG12_TRIGGERBIT = 0x0070
BASE_REG12_NO_TRIGGERBIT = 0x0030

# Bunch layout used by EtrocTransmitter for "multiple=N" (non-overlapping QInj/L1A pairs).
_BUNCH_STARTS = ((0x005, 0x1FD), (0x3F5, 0x5ED), (0x7E5, 0x9DD))
MAX_MULTIPLE = 378
# BX from a QInj to its L1A in every bunch above (0x1FD - 0x005)
QINJ_L1A_SPACING = 0x1F8
BCR_ADDRESS = 0x000

_MEMO_WORDS = {"QInj", "L1A", "BCR", "Triggerbit", "Start"}
_MULTIPLE_RE = re.compile(r"^multiple=(\d+)$")
_L1A_OFFSET_RE = re.compile(r"^l1a_offset=([+-]?\d+)$")


def parse_memo(memo: str) -> dict:
    """Parse an EtrocTransmitter fast-command memo string.

    Grammar: space-separated words from {Start, Triggerbit, BCR, QInj, L1A,
    multiple=N, l1a_offset=K}. Unknown words raise ValueError (a typo used to be
    ignored silently and changed the trigger pattern). `l1a_offset=K` (K may be
    negative) needs QInj and L1A, and every resulting L1A must fall inside the
    orbit on an address no other command uses; otherwise ValueError.
    """
    words = memo.split()
    flags = {w: False for w in _MEMO_WORDS}
    loops = 1
    offset = None
    for w in words:
        if w in _MEMO_WORDS:
            flags[w] = True
            continue
        m = _MULTIPLE_RE.match(w)
        if m:
            loops = int(m.group(1))
            continue
        m = _L1A_OFFSET_RE.match(w)
        if m:
            offset = int(m.group(1))
            continue
        raise ValueError(
            f"Unknown word '{w}' in fast_command_memo '{memo}'. "
            f"Allowed: {sorted(_MEMO_WORDS)}, multiple=N and l1a_offset=K"
        )
    flags["loops"] = min(max(loops, 1), MAX_MULTIPLE)
    flags["l1a_offset"] = 0 if offset is None else offset
    if offset is not None:
        if not (flags["QInj"] and flags["L1A"]):
            raise ValueError(f"l1a_offset in fast_command_memo '{memo}' needs both QInj and L1A (it moves the L1A of each QInj)")
        pairs = qinj_l1a_addresses(flags["loops"], offset)
        if flags["BCR"] and any(l1a == BCR_ADDRESS for _, l1a in pairs):
            raise ValueError(f"l1a_offset={offset} puts an L1A on BX {BCR_ADDRESS}, the BCR address ('{memo}')")
    return flags


def send_fc_sequence(sock, reg12_val: int, reg10_val: int, reg9_val: int, prescale_factor: int = 2048) -> None:
    """Fill FC memory addresses [reg10_val, reg9_val] with the command in reg12_val."""
    cmd_interpret.write_config_reg_decoded(sock, "register_12", reg12_val)
    cmd_interpret.write_config_reg_decoded(sock, "register_10", reg10_val, prescale_factor)
    cmd_interpret.write_config_reg_decoded(sock, "register_9", reg9_val)
    cmd_interpret.write_pulse_reg_decoded(sock, "fc_init")
    time.sleep(0.01)


def qinj_l1a_addresses(loops: int, l1a_offset: int = 0) -> list[tuple[int, int]]:
    """(QInj BX, L1A BX) pairs for `loops` injections per orbit, as EtrocTransmitter places them.

    Each L1A sits QINJ_L1A_SPACING + l1a_offset BX after its QInj. Raises ValueError if an L1A
    leaves the orbit (BX 0 .. ORBIT_REG11) or shares an address with a QInj or another L1A.
    """
    pairs = []
    for i in range(min(max(loops, 1), MAX_MULTIPLE)):
        q0, l0 = _BUNCH_STARTS[i % 3]
        step = (i // 3) * 4
        pairs.append((q0 + step, l0 + step + l1a_offset))
    l1as = [l1a for _, l1a in pairs]
    outside = [a for a in l1as if not 0 <= a <= ORBIT_REG11]
    if outside:
        raise ValueError(
            f"l1a_offset={l1a_offset} puts L1As outside the orbit (BX 0-{ORBIT_REG11}), e.g. BX {outside[0]}")
    clash = sorted(set(l1as) & {q for q, _ in pairs})
    if clash or len(set(l1as)) != len(l1as):
        raise ValueError(
            f"l1a_offset={l1a_offset} with {len(pairs)} QInj per orbit puts an L1A on a QInj address "
            f"(e.g. BX {clash[0] if clash else '?'}); use a smaller |offset| or fewer injections")
    return pairs


def configure_memo_fc(sock, memo: str, prescale_factor: int = 2048) -> dict:
    """Program and start the FC memory from a memo string; returns the parsed flags."""
    flags = parse_memo(memo)
    base_reg12 = BASE_REG12_TRIGGERBIT if flags["Triggerbit"] else BASE_REG12_NO_TRIGGERBIT

    if flags["Start"]:
        cmd_interpret.write_config_reg_decoded(sock, "register_11", ORBIT_REG11)
        time.sleep(0.01)

    # IDLE over the whole orbit, then single commands on top
    send_fc_sequence(sock, base_reg12 + IDLE_CMD, 0x000, ORBIT_REG11, prescale_factor)

    if flags["BCR"]:
        send_fc_sequence(sock, base_reg12 + BCR_CMD, 0x000, 0x000, prescale_factor)

    # L1A is placed only together with QInj (unchanged from the ETROC2v2 transmitter)
    if flags["QInj"]:
        for qinj_bx, l1a_bx in qinj_l1a_addresses(flags["loops"], flags["l1a_offset"]):
            send_fc_sequence(sock, base_reg12 + QINJ_CMD, qinj_bx, qinj_bx, prescale_factor)
            if flags["L1A"]:
                send_fc_sequence(sock, base_reg12 + L1A_CMD, l1a_bx, l1a_bx, prescale_factor)

    cmd_interpret.write_pulse_reg_decoded(sock, "fc_signal_start")
    time.sleep(0.01)
    return flags
