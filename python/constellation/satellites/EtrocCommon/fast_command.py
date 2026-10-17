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

_MEMO_WORDS = {"QInj", "L1A", "BCR", "Triggerbit", "Start"}
_MULTIPLE_RE = re.compile(r"^multiple=(\d+)$")


def parse_memo(memo: str) -> dict:
    """Parse an EtrocTransmitter fast-command memo string.

    Grammar: space-separated words from {Start, Triggerbit, BCR, QInj, L1A,
    multiple=N}. Unknown words raise ValueError (a typo used to be ignored
    silently and changed the trigger pattern).
    """
    words = memo.split()
    flags = {w: False for w in _MEMO_WORDS}
    loops = 1
    for w in words:
        if w in _MEMO_WORDS:
            flags[w] = True
            continue
        m = _MULTIPLE_RE.match(w)
        if m:
            loops = int(m.group(1))
            continue
        raise ValueError(
            f"Unknown word '{w}' in fast_command_memo '{memo}'. "
            f"Allowed: {sorted(_MEMO_WORDS)} and multiple=N"
        )
    flags["loops"] = min(max(loops, 1), MAX_MULTIPLE)
    return flags


def send_fc_sequence(sock, reg12_val: int, reg10_val: int, reg9_val: int, prescale_factor: int = 2048) -> None:
    """Fill FC memory addresses [reg10_val, reg9_val] with the command in reg12_val."""
    cmd_interpret.write_config_reg_decoded(sock, "register_12", reg12_val)
    cmd_interpret.write_config_reg_decoded(sock, "register_10", reg10_val, prescale_factor)
    cmd_interpret.write_config_reg_decoded(sock, "register_9", reg9_val)
    cmd_interpret.write_pulse_reg_decoded(sock, "fc_init")
    time.sleep(0.01)


def qinj_l1a_addresses(loops: int) -> list[tuple[int, int]]:
    """(QInj BX, L1A BX) pairs for `loops` injections per orbit, as EtrocTransmitter places them."""
    pairs = []
    for i in range(min(max(loops, 1), MAX_MULTIPLE)):
        q0, l0 = _BUNCH_STARTS[i % 3]
        step = (i // 3) * 4
        pairs.append((q0 + step, l0 + step))
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
        for qinj_bx, l1a_bx in qinj_l1a_addresses(flags["loops"]):
            send_fc_sequence(sock, base_reg12 + QINJ_CMD, qinj_bx, qinj_bx, prescale_factor)
            if flags["L1A"]:
                send_fc_sequence(sock, base_reg12 + L1A_CMD, l1a_bx, l1a_bx, prescale_factor)

    cmd_interpret.write_pulse_reg_decoded(sock, "fc_signal_start")
    time.sleep(0.01)
    return flags
