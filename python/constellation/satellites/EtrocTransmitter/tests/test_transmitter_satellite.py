"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocTransmitter reconfigure and Ctrl-C reentry, over loopback CSCP against the simulated KC705.
"""

import logging
import threading
import time
import types

import pytest
import zmq

from constellation.core.configuration import Configuration
from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from ...EtrocCounters.tests.fakes import FC_QINJ, FakeKC705
from .. import EtrocTransmitter as tx_module
from ..EtrocTransmitter import EtrocTransmitter

OK = CSCP1Message.Type.SUCCESS
STOP_DAQ_BIT = 0x0400


def wait_state(sat, state, timeout=10.0):
    deadline = time.monotonic() + timeout
    while sat.fsm.state != state:
        if time.monotonic() > deadline:
            raise AssertionError(f"never reached {state.name}; now {sat.fsm.state.name}: {sat.fsm.status}")
        time.sleep(0.01)


def call(tx, cmd, payload=None):
    tx.send_request(cmd, payload)
    return tx.get_message()


def ctrl_c(sat, thread, timeout=20.0):
    """Raise KeyboardInterrupt in the satellite main loop, as Ctrl-C does, and wait for run_satellite to return."""
    get = sat.task_queue.get

    def interrupted(*args, **kwargs):
        sat.task_queue.get = get
        raise KeyboardInterrupt

    sat.task_queue.get = interrupted
    thread.join(timeout)
    assert not thread.is_alive(), "run_satellite did not return after KeyboardInterrupt"


def assert_run_thread_gone(sat):
    assert sat._state_thread_fut is not None and sat._state_thread_fut.done()
    for worker in list(sat._state_thread_exc._threads):
        worker.join(2.0)
        assert not worker.is_alive()


def qinj_playing(fake):
    with fake.lock:
        return bool(fake.pattern) and FC_QINJ in fake.pattern


def failed_steps(caplog):
    return [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]


@pytest.fixture
def setup(monkeypatch):
    # the firmware waits (2.1 s after a FIFO reset) are not needed against the simulation
    fast_time = types.SimpleNamespace(**{n: getattr(time, n) for n in ("strftime", "localtime", "time", "monotonic")})
    fast_time.sleep = lambda s: None
    monkeypatch.setattr(tx_module, "time", fast_time)
    fake = FakeKC705(window_s=0.05).start()
    sat = EtrocTransmitter(0, "Test", "txlifecycle", 0, 0, 0, [get_loopback_interface_name()])
    thread = threading.Thread(target=sat.run_satellite, daemon=True)
    thread.start()
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 5000)
    sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
    tx = CommandTransmitter("test", sock)
    config = {
        "hostname": "127.0.0.1",
        "port": fake.port,
        "socket_timeout": 5.0,
        "polarity": 0x4023,
        "fast_command_memo": "Start QInj L1A Triggerbit",
        "num_fifo_read": 16,
        "_data": {"disable_transmission": True},
    }
    assert call(tx, "initialize", config).verb_type == OK
    wait_state(sat, SatelliteState.INIT)
    assert call(tx, "launch").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    yield sat, tx, fake, thread
    if thread.is_alive() and sat.fsm.state == SatelliteState.ORBIT:
        call(tx, "land")
        wait_state(sat, SatelliteState.INIT)
    sat.reentry()
    thread.join(5.0)
    sock.close()
    ctx.term()
    fake.stop()


def test_reconfigure_writes_exactly_the_changed_register_and_updates_the_config(setup):
    sat, tx, fake, _ = setup
    with fake.lock:
        mark = len(fake.received)
    rep = call(tx, "reconfigure", {"polarity": 0x4123})
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    with fake.lock:
        new = fake.received[mark:]
    assert [m for m in new if m[0] in ("write_config", "pulse")] == [("write_config", 14, 0x4123)]
    assert "polarity = 0x4123 (Reg14 read back 0x4123)" in sat.fsm.status
    # the stored configuration is what the BOR carries
    assert sat._config._dictionary["polarity"] == 0x4123
    sat.do_starting("run_after_reconfigure")
    assert sat.bor["kc705_readback"]["reg14_polarity"] == "0x4123"


def test_reconfigure_memo_reprograms_the_fast_commands(setup):
    sat, tx, fake, _ = setup
    assert qinj_playing(fake)
    assert call(tx, "reconfigure", {"fast_command_memo": "Start Triggerbit"}).verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    assert not qinj_playing(fake)
    assert sat._config._dictionary["fast_command_memo"] == "Start Triggerbit"


@pytest.mark.parametrize(
    "change, words",
    [
        ({"hostname": "10.0.0.1"}, ["'hostname'", "land and re-initialize to change it"]),
        ({"socket_timeout": 1.0, "polarity": 0x4123}, ["'socket_timeout'", "land and re-initialize"]),
        ({"polarty": 0x4123}, ["polarty"]),
        ({"prescale_factor": 1000}, ["1000"]),
        ({"fast_command_memo": "Start Trigerbit"}, ["Trigerbit"]),
    ],
)
def test_refused_reconfigure_leaves_the_satellite_in_orbit(setup, change, words):
    sat, tx, fake, _ = setup
    with fake.lock:
        mark = len(fake.received)
    rep = call(tx, "reconfigure", change)
    assert rep.verb_type != OK
    assert all(w in rep.verb_msg for w in words), rep.verb_msg
    time.sleep(0.2)
    assert sat.fsm.state == SatelliteState.ORBIT
    with fake.lock:
        assert fake.received[mark:] == []
    assert sat._config._dictionary["polarity"] == 0x4023


def test_do_reconfigure_names_a_key_that_needs_a_landing(setup):
    sat, _, _, _ = setup
    with pytest.raises(ValueError, match="'port' cannot change in ORBIT; land and re-initialize to change it"):
        sat.do_reconfigure(Configuration({"port": 1025}))


def test_reconfigure_outside_orbit_is_refused(setup):
    sat, tx, _, _ = setup
    call(tx, "land")
    wait_state(sat, SatelliteState.INIT)
    rep = call(tx, "reconfigure", {"polarity": 0x4123})
    assert rep.verb_type != OK
    assert "not allowed in INIT" in rep.verb_msg
    assert sat.fsm.state == SatelliteState.INIT


def test_ctrl_c_in_run_stops_the_daq_idles_fc_and_releases_everything(setup, caplog):
    sat, tx, fake, thread = setup
    assert call(tx, "start", "run1").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    deadline = time.monotonic() + 5
    while fake.fifo_reads < 3:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert fake.daq_running and qinj_playing(fake)
    stops = fake.pulses(STOP_DAQ_BIT)

    ctrl_c(sat, thread)

    assert fake.pulses(STOP_DAQ_BIT) == stops + 1
    assert not fake.daq_running
    assert not qinj_playing(fake)
    assert sat.connection_socket is None
    assert_run_thread_gone(sat)
    assert not sat._dtm._push_thread.is_alive()
    reads = fake.fifo_reads
    time.sleep(0.2)
    assert fake.fifo_reads == reads

    caplog.clear()
    with fake.lock:
        mark = len(fake.received)
    sat.reentry()
    with fake.lock:
        assert fake.received[mark:] == []
    assert failed_steps(caplog) == []


def test_reentry_after_landing_touches_nothing(setup, caplog):
    sat, tx, fake, _ = setup
    call(tx, "land")
    wait_state(sat, SatelliteState.INIT)
    caplog.clear()
    with fake.lock:
        mark = len(fake.received)
    sat.reentry()
    with fake.lock:
        assert fake.received[mark:] == []
    assert failed_steps(caplog) == []
