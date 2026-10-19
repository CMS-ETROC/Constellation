"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2
"""

import socket

import pytest

from ...EtrocCommon import cmd_interpret
from ..counters import (
    EXPECTED_L1A_PER_WINDOW,
    CounterReader,
    CounterTimeoutError,
    KC705Link,
    decode_led8,
    decode_page0,
    refresh_delta,
    refresh_from_words,
)
from .fakes import FakeChip, FakeKC705

WINDOW = 0.05

# combine 0xBEEF, trig 0x12345678, refresh 0x9ABC, header 11223, data 0xDEADBEEF, LED8 0xA5, ws_ready 1
PAGE0_WORDS = [0x0001, 0xA500, 0xEF00, 0xADBE, 0xD7DE, 0x002B, 0xBC00, 0x789A, 0x3456, 0xEF12, 0x00BE]


@pytest.fixture
def board():
    fake = FakeKC705(chip=FakeChip({(0, 0): (500, 4)}), window_s=WINDOW).start()
    link = KC705Link("127.0.0.1", fake.port, connect_timeout=2.0, io_timeout=2.0)
    link.open()
    yield fake, link, CounterReader(link, window_s=WINDOW, poll_s=WINDOW / 5)
    link.close()
    fake.stop()


def test_decode_page0_literal_words():
    assert decode_page0(PAGE0_WORDS) == {
        "ws_ready": 1,
        "led8": 0xA5,
        "data": 0xDEADBEEF,
        "header": 11223,
        "refresh": 0x9ABC,
        "trigbit": 0x12345678,
        "combine": 0xBEEF,
    }
    assert refresh_from_words(PAGE0_WORDS[6], PAGE0_WORDS[7]) == 0x9ABC


def test_decode_page0_needs_eleven_words():
    with pytest.raises(ValueError):
        decode_page0(PAGE0_WORDS[:10])


def test_led8_fields():
    assert decode_led8(0b10_1_0_1_0_1_0) == {
        "monitor_sel": 2, "is_data_ex": 1, "is_head_ex": 0, "trig_synched": 1,
        "trig_error": 0, "synched": 1, "data_error": 0,
    }


def test_refresh_delta_wraps():
    assert refresh_delta(3, 0xFFFE) == 5
    assert refresh_delta(10, 8) == 2


def test_expected_l1a_rate_is_one_per_orbit():
    assert int(EXPECTED_L1A_PER_WINDOW) == 11223


def test_reg7_is_never_written(board):
    fake, link, _ = board
    with pytest.raises(ValueError):
        link.write_config(7, 0x1234)
    with pytest.raises(ValueError):
        link.write_named_config("counter_duration", 0x1234)
    assert fake.writes_to(7) == []


def test_set_channel_changes_only_led_and_status_page_bits(board):
    fake, link, reader = board
    fake.config[13] = 0x1F83
    reader.set_channel(2)
    assert reader.get_channel() == 2
    assert fake.config[13] == 0x1F88
    with pytest.raises(ValueError):
        reader.set_channel(4)


def test_layout_check_passes_on_firmware_addressing(board):
    _, link, reader = board
    result = reader.check_status_layout()
    assert result["ok"], result
    assert link.read_config(13) & 0b11 == 0


def test_layout_check_detects_wrong_word_order():
    fake = FakeKC705(window_s=WINDOW, reversed_words=True).start()
    link = KC705Link("127.0.0.1", fake.port, 2.0, 2.0)
    link.open()
    try:
        assert not CounterReader(link, window_s=WINDOW).check_status_layout()["ok"]
    finally:
        link.close()
        fake.stop()


def test_header_count_follows_fc_pattern(board):
    fake, link, reader = board
    reader.set_channel(0)
    link.configure_fc("Start QInj L1A Triggerbit")
    w = reader.wait_windows(2)
    assert w.header == 11223
    assert w.channel == 0
    assert w.en_l1a == 1
    assert decode_led8(w.led8)["monitor_sel"] == 0

    link.configure_fc("Start Triggerbit")
    w2 = reader.wait_windows(2)
    assert w2.header == 0
    assert refresh_delta(w2.refresh, w.refresh) >= 2
    assert fake.pulses(0x0002) == 0


def test_wait_windows_reports_unaligned_link(board):
    _, _, reader = board
    reader.set_channel(1)
    with pytest.raises(CounterTimeoutError, match="not aligned"):
        reader.wait_windows(2)


def test_wait_windows_honours_stop_event(board):
    import threading

    _, _, reader = board
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        reader.wait_windows(2, stop)


def test_closed_port_gives_single_client_hint():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ConnectionError, match="ONE TCP client"):
        cmd_interpret.open_socket("127.0.0.1", port, connect_timeout=1.0)


def test_handshake_timeout_is_bounded(monkeypatch):
    def no_answer(address, timeout):
        assert timeout == 0.5
        raise TimeoutError("timed out")

    monkeypatch.setattr(cmd_interpret.socket, "create_connection", no_answer)
    with pytest.raises(ConnectionError, match="did not complete the TCP handshake within 0.5 s"):
        cmd_interpret.open_socket("192.168.2.3", 1024, connect_timeout=0.5)


def test_window_latched_during_the_read_is_not_torn():
    fake = FakeKC705(window_s=60.0).start()  # no automatic latch during the test
    link = KC705Link("127.0.0.1", fake.port, 2.0, 2.0)
    link.open()
    try:
        old = dict(data=0x11111111, header=0x11111111, trig=0x11111111, combine=0x1111)
        new = dict(data=0x22222222, header=0x22222222, trig=0x22222222, combine=0x2222)
        with fake.lock:
            fake.refresh[0] = 7
            fake.latched[0] = old
        latched = []

        def latch_between_word2_and_word3(addr):
            if addr == 3 and not latched:
                fake.refresh[0] = 8
                fake.latched[0] = new
                latched.append(True)

        fake.on_status_read = latch_between_word2_and_word3
        w = CounterReader(link, window_s=60.0).read_window()
        assert latched
        assert (w.refresh, w.data, w.header, w.trigbit, w.combine) == (8, 0x22222222, 0x22222222, 0x22222222, 0x2222)
    finally:
        link.close()
        fake.stop()


# ------------------------------------------------------------- l1a_offset memo word (plan A3 reverse)
def test_l1a_offset_moves_every_l1a_and_leaves_the_qinj():
    from ...EtrocCommon import fast_command as fc

    assert fc.qinj_l1a_addresses(3) == [(0x005, 0x1FD), (0x3F5, 0x5ED), (0x7E5, 0x9DD)]
    assert fc.qinj_l1a_addresses(3, -2) == [(0x005, 0x1FB), (0x3F5, 0x5EB), (0x7E5, 0x9DB)]
    assert fc.qinj_l1a_addresses(4, 7)[3] == (0x009, 0x009 + 504 + 7)
    flags = fc.parse_memo("Start QInj L1A Triggerbit l1a_offset=-12")
    assert flags["l1a_offset"] == -12 and flags["loops"] == 1
    assert fc.parse_memo("Start QInj L1A Triggerbit multiple=8")["l1a_offset"] == 0


@pytest.mark.parametrize(
    "memo, word",
    [
        ("Start QInj Triggerbit l1a_offset=2", "needs both QInj and L1A"),
        ("Start L1A Triggerbit l1a_offset=2", "needs both QInj and L1A"),
        ("Start QInj L1A Triggerbit l1a_offset=3100", "outside the orbit"),
        ("Start QInj L1A Triggerbit l1a_offset=-515", "outside the orbit"),
        ("Start QInj L1A Triggerbit l1a_offset=-504", "QInj address"),
        ("Start QInj L1A Triggerbit multiple=378 l1a_offset=-4", "QInj address"),
        ("Start BCR QInj L1A Triggerbit l1a_offset=-509", "BCR address"),
        ("Start QInj L1A Triggerbit l1a_offset=two", "Unknown word"),
    ],
)
def test_l1a_offset_refusals(memo, word):
    from ...EtrocCommon import fast_command as fc

    with pytest.raises(ValueError, match=word):
        fc.parse_memo(memo)


def test_l1a_offset_programs_the_l1a_k_bx_later(board):
    fake, link, _ = board
    link.configure_fc("Start QInj L1A Triggerbit l1a_offset=3")
    assert fake.pattern[0x005] == 0x5
    assert fake.pattern[0x1FD + 3] == 0x6
    assert fake.pattern[0x1FD] == 0x0
    assert fake.pattern.count(0x6) == 1 and fake.pattern.count(0x5) == 1
    link.configure_fc("Start QInj L1A Triggerbit multiple=6 l1a_offset=-3")
    l1as = [i for i, c in enumerate(fake.pattern) if c == 0x6]
    qinj = [i for i, c in enumerate(fake.pattern) if c == 0x5]
    assert len(qinj) == 6 and sorted(q + 504 - 3 for q in qinj) == l1as


def test_memo_without_triggerbit_programs_trigdatasize_zero(board):
    fake, link, _ = board
    mark = len(fake.received)
    link.configure_fc("Start QInj L1A")
    reg12 = [v for kind, a, v in fake.received[mark:] if kind == "write_config" and a == 12]
    assert reg12 and all((v >> 6) & 0b11 == 0 for v in reg12)
    link.configure_fc("Start QInj L1A Triggerbit")
    assert all((v >> 6) & 0b11 == 1 for v in fake.writes_to(12)[len(reg12):])
