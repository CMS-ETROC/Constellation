"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocReceiver file handling, driven through its hooks without a data transport.
"""

import time
import types

import numpy as np
import pytest

from constellation.core.configuration import Configuration

from ...EtrocReceiver.EtrocReceiver import EtrocReceiver

FIFO_FILLER = 0x556


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


def record(words):
    return types.SimpleNamespace(blocks=[np.asarray(words, dtype=">u4").tobytes()])


def fillers(n, start=0):
    return [(FIFO_FILLER << 20) | ((start + i) & 0xFFFFF) for i in range(n)]


@pytest.fixture
def make(tmp_path):
    def _make(**config):
        rx = EtrocReceiver.__new__(EtrocReceiver)
        rx.log = _Log()
        rx.name = "rx"
        rx.do_initializing(Configuration({"output_path": str(tmp_path), **config}))
        return rx
    return _make


def test_rate_and_stop_before_any_run(make):
    rx = make()
    msg, rate, _ = rx.get_data_rate()
    assert rate == 0.0 and "No run" in msg
    rx.do_stopping()


def test_second_sender_is_refused(make):
    rx = make()
    rx.do_starting("run1")
    rx.receive_bor("EtrocTransmitter.a", {}, {})
    rx.receive_data("EtrocTransmitter.a", record(fillers(4)))
    with pytest.raises(RuntimeError, match="receive_from"):
        rx.receive_data("EtrocTransmitter.b", record(fillers(4)))
    rx.do_stopping()


def test_failure_closes_the_file_and_leaves_a_marker(make, tmp_path):
    rx = make()
    rx.do_starting("run2")
    rx.receive_data("EtrocTransmitter.a", record(fillers(10)))
    rx.fail_gracefully()
    assert rx.outfile is None
    assert len((tmp_path / "run2" / "file_0.nem").read_text().splitlines()) == 10
    assert "failure" in (tmp_path / "run2" / "RUN_NOT_STOPPED_CLEANLY.txt").read_text()


def test_reinitialize_after_a_failed_run_closes_the_file(make, tmp_path):
    rx = make()
    rx.do_starting("run3")
    rx.receive_data("EtrocTransmitter.a", record(fillers(3)))
    rx.do_initializing(Configuration({"output_path": str(tmp_path)}))
    assert rx.start_time is None
    assert len((tmp_path / "run3" / "file_0.nem").read_text().splitlines()) == 3
    assert (tmp_path / "run3" / "RUN_NOT_STOPPED_CLEANLY.txt").exists()


def test_data_is_flushed_every_flush_interval(make, tmp_path):
    rx = make(flush_interval=0.01)
    rx.do_starting("run4")
    time.sleep(0.02)
    rx.receive_data("EtrocTransmitter.a", record(fillers(5)))
    assert len((tmp_path / "run4" / "file_0.nem").read_text().splitlines()) == 5
    rx.do_stopping()


def test_filler_only_stream_rotates_by_line_count(make, tmp_path):
    rx = make(translate=1, skip_fillers=0)
    rx.file_size_limit = 100
    rx.do_starting("run5")
    for i in range(5):
        rx.receive_data("EtrocTransmitter.a", record(fillers(60, start=60 * i)))
    rx.do_stopping()
    files = sorted((tmp_path / "run5").glob("file_*.nem"))
    assert len(files) >= 3
    assert sum(len(f.read_text().splitlines()) for f in files) == 300
    assert all(len(f.read_text().splitlines()) <= 120 for f in files)
