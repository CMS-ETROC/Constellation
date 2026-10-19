"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

R-KC7-SW-16: at a stop, the transmitter idles the fast commands and reads the FIFO until a read
holds fillers only, so the last event keeps its trailer. Against a simulated KC705 whose FIFO
holds events; the run thread is stopped at a read that ends inside an event.
"""

import threading
import time
import types

import numpy as np
import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from .. import EtrocTransmitter as tx_module
from ..EtrocTransmitter import EtrocTransmitter, fillers_only
from .event_fake import EventFifoKC705, ends_inside_event, event_words

OK = CSCP1Message.Type.SUCCESS
# 16-word reads against 7-word events with no gaps: read k ends inside an event unless 16k % 7 == 0
NUM_FIFO_READ = 16
STOP_AT_READ = 5


def wait_state(sat, state, timeout=10.0):
    deadline = time.monotonic() + timeout
    while sat.fsm.state != state:
        if time.monotonic() > deadline:
            raise AssertionError(f"never reached {state.name}; now {sat.fsm.state.name}: {sat.fsm.status}")
        time.sleep(0.01)


def call(tx, cmd, payload=None):
    tx.send_request(cmd, payload)
    return tx.get_message()


@pytest.fixture
def make(monkeypatch):
    made = []

    def _make(**extra):
        fast_time = types.SimpleNamespace(**{n: getattr(time, n) for n in ("strftime", "localtime", "time", "monotonic")})
        fast_time.sleep = lambda s: None
        monkeypatch.setattr(tx_module, "time", fast_time)
        fake = EventFifoKC705(window_s=0.05, gap=(0, 0)).start()
        sat = EtrocTransmitter(0, "Test", "txdrain", 0, 0, 0, [get_loopback_interface_name()])
        thread = threading.Thread(target=sat.run_satellite, daemon=True)
        thread.start()
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 5000)
        sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
        tx = CommandTransmitter("test", sock)
        config = {
            "hostname": "127.0.0.1", "port": fake.port, "socket_timeout": 5.0, "polarity": 0x4023,
            "fast_command_memo": "Start QInj L1A Triggerbit", "num_fifo_read": NUM_FIFO_READ,
            "_data": {"disable_transmission": True}, **extra,
        }
        assert call(tx, "initialize", config).verb_type == OK
        wait_state(sat, SatelliteState.INIT)
        assert call(tx, "launch").verb_type == OK
        wait_state(sat, SatelliteState.ORBIT)
        made.append((sat, tx, fake, thread, sock, ctx))
        return sat, tx, fake

    yield _make
    for sat, tx, fake, thread, sock, ctx in made:
        if thread.is_alive() and sat.fsm.state == SatelliteState.ORBIT:
            call(tx, "land")
            wait_state(sat, SatelliteState.INIT)
        sat.reentry()
        thread.join(5.0)
        sock.close()
        ctx.term()
        fake.stop()


def run_and_stop_at(sat, tx, fake, run_id, stop_at=STOP_AT_READ):
    """One run whose acquisition loop is told to stop right after its `stop_at`-th FIFO read; returns the words sent."""
    blocks = []
    sat.send_data_record = lambda record: blocks.extend(record.blocks)
    with fake.lock:
        base = fake.fifo_reads

    def on_read(n):
        if n - base >= stop_at:
            sat._state_thread_evt.set()

    fake.on_fifo_read = on_read
    previous = sat._state_thread_fut
    assert call(tx, "start", run_id).verb_type == OK
    deadline = time.monotonic() + 5
    while sat._state_thread_fut is previous or sat._state_thread_fut is None or not sat._state_thread_fut.done():
        assert time.monotonic() < deadline, "run thread did not finish after the stop request"
        time.sleep(0.01)
    fake.on_fifo_read = None
    assert call(tx, "stop").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    return [int(w) for b in blocks for w in np.frombuffer(b, dtype=">u4")]


def test_event_fake_builds_events_the_walker_reads():
    words = event_words([(0, 14)]) + event_words([(0, 14), (1, 13)])
    assert not ends_inside_event(words)
    assert ends_inside_event(words[:-1]) and ends_inside_event(words[:3])
    filler = np.asarray([0x556 << 20] * 4, dtype=">u4").tobytes()
    assert fillers_only(filler)
    assert not fillers_only(filler + np.asarray(words[:1], dtype=">u4").tobytes())
    assert not fillers_only(b"")


def test_without_the_drain_the_last_event_loses_its_trailer(make):
    """Positive control of the stop-order test: the old order cuts the stream inside an event."""
    sat, tx, fake = make(drain_on_stop=0)
    words = run_and_stop_at(sat, tx, fake, "run_nodrain")
    assert len(words) == STOP_AT_READ * NUM_FIFO_READ
    assert ends_inside_event(words)
    assert sat.eor["drain"] == {"enabled": False, "ran": False}


def test_drain_idles_the_fc_and_reads_the_last_event_to_its_trailer(make):
    sat, tx, fake = make()
    assert sat.drain_on_stop == 1  # the default
    words = run_and_stop_at(sat, tx, fake, "run_drain")
    assert len(words) > STOP_AT_READ * NUM_FIFO_READ
    assert not ends_inside_event(words)
    assert fillers_only(np.asarray(words[-NUM_FIFO_READ:], dtype=">u4").tobytes())
    drain = sat.eor["drain"]
    assert drain["ran"] and drain["clean"] and drain["reason"] == "fillers only"
    assert sat.eor["fc_idle_unix"] <= sat.eor["daq_stop_unix"]
    with fake.lock:
        assert fake.pattern is not None and 0x5 not in fake.pattern  # FC idle, no QInj
        assert not fake.daq_running


def test_next_start_replays_the_memo_the_drain_idled(make):
    sat, tx, fake = make(clear_fifo=0)
    run_and_stop_at(sat, tx, fake, "run_a")
    with fake.lock:
        assert 0x5 not in fake.pattern
    words = run_and_stop_at(sat, tx, fake, "run_b")
    with fake.lock:
        assert 0x5 not in fake.pattern
    # events came out of the second run: the memo was replayed at its start
    assert any(w >> 4 == 0xC3A3C3A for w in words)


def test_drain_ends_at_its_timeout_when_the_fifo_never_runs_dry(make, monkeypatch):
    sat, tx, fake = make(drain_timeout=0.3)
    monkeypatch.setattr(fake, "_l1a_playing", lambda: True)  # events keep coming after the FC idle
    run_and_stop_at(sat, tx, fake, "run_endless")
    drain = sat.eor["drain"]
    assert drain["ran"] and not drain["clean"] and drain["reason"] == "timeout"
    assert drain["seconds"] < 2.0


@pytest.mark.parametrize("change", [{"drain_timeout": 0.0}, {"drain_timeout": 100.0}, {"drain_on_stop": 2}])
def test_bad_drain_settings_are_refused_in_orbit(make, change):
    sat, tx, fake = make()
    rep = call(tx, "reconfigure", change)
    assert rep.verb_type != OK
    time.sleep(0.1)
    assert sat.fsm.state == SatelliteState.ORBIT


def test_drain_can_be_switched_off_in_orbit(make):
    sat, tx, fake = make()
    rep = call(tx, "reconfigure", {"drain_on_stop": 0})
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    words = run_and_stop_at(sat, tx, fake, "run_off")
    assert ends_inside_event(words)
