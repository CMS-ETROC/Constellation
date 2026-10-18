"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocTransmitter reconfigure over loopback CSCP against the simulated KC705.
"""

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


def wait_state(sat, state, timeout=10.0):
    deadline = time.monotonic() + timeout
    while sat.fsm.state != state:
        if time.monotonic() > deadline:
            raise AssertionError(f"never reached {state.name}; now {sat.fsm.state.name}: {sat.fsm.status}")
        time.sleep(0.01)


def call(tx, cmd, payload=None):
    tx.send_request(cmd, payload)
    return tx.get_message()


def qinj_playing(fake):
    with fake.lock:
        return bool(fake.pattern) and FC_QINJ in fake.pattern


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
