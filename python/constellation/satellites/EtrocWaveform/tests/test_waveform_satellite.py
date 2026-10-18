"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocWaveform reconfigure and Ctrl-C reentry over loopback CSCP, against the simulated KC705
and a stand-in for the i2c_gui2 connection (no USB-ISS).
"""

import threading
import time

import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from ...EtrocCommon import new_wrapper_i2cGui2
from ...EtrocCounters.tests.fakes import FC_QINJ, FakeKC705
from ..EtrocWaveform import EtrocWaveform

OK = CSCP1Message.Type.SUCCESS
ACTIVE_CHANNEL = 0x0001
WS_ACTIVE_CHANNEL = 0x0091


class FakeI2C:
    """i2c_connection stand-in: every batch_* call succeeds, the WS read returns no chips."""

    def __init__(self, *args, **kwargs):
        self.closed = False

    def __getattr__(self, name):
        if name.startswith("batch_"):
            return lambda *args, **kwargs: {} if name == "batch_ws_read_data" else None
        raise AttributeError(name)

    def close(self):
        self.closed = True


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


def qinj_playing(fake):
    with fake.lock:
        return bool(fake.pattern) and FC_QINJ in fake.pattern


@pytest.fixture
def setup(tmp_path, monkeypatch):
    connections = []

    def open_i2c(*args, **kwargs):
        connections.append(FakeI2C())
        return connections[-1]

    monkeypatch.setattr(new_wrapper_i2cGui2, "i2c_connection", open_i2c)
    fake = FakeKC705(window_s=0.05).start()
    sat = EtrocWaveform("Test", "wslifecycle", 0, 0, 0, [get_loopback_interface_name()])
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
        "clear_fifo": 0,
        "fast_command_memo": "Start QInj L1A Triggerbit",
        "active_channel": ACTIVE_CHANNEL,
        "ws_active_channel": WS_ACTIVE_CHANNEL,
        "i2c_port": "/dev/fake",
        "output_path": str(tmp_path),
    }
    assert call(tx, "initialize", config).verb_type == OK
    wait_state(sat, SatelliteState.INIT)
    assert call(tx, "launch").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    yield sat, tx, fake, thread, connections
    if thread.is_alive() and sat.fsm.state == SatelliteState.ORBIT:
        call(tx, "land")
        wait_state(sat, SatelliteState.INIT)
    sat.reentry()
    thread.join(5.0)
    sock.close()
    ctx.term()
    fake.stop()


def test_reconfigure_writes_the_register_and_keeps_the_chip(setup):
    sat, tx, fake, _, connections = setup
    with fake.lock:
        mark = len(fake.received)
    rep = call(tx, "reconfigure", {"triggerbit_delay": 0x1de6, "ws_active_channel": 0x0081})
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    with fake.lock:
        writes = [m for m in fake.received[mark:] if m[0] in ("write_config", "pulse")]
    assert writes == [("write_config", 8, 0x1de6)]
    assert sat.ws_active_channel == 0x0081
    assert sat._config._dictionary["triggerbit_delay"] == 0x1de6
    assert len(connections) == 1 and not connections[0].closed

    rep = call(tx, "reconfigure", {"i2c_port": "/dev/ttyACM1"})
    assert rep.verb_type != OK and "'i2c_port'" in rep.verb_msg
    time.sleep(0.2)
    assert sat.fsm.state == SatelliteState.ORBIT


def test_ctrl_c_in_run_restores_registers_and_releases_i2c(setup, caplog):
    sat, tx, fake, thread, connections = setup
    assert call(tx, "start", "run_ctrl_c").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    time.sleep(0.3)
    with fake.lock:
        assert fake.config[15] == WS_ACTIVE_CHANNEL
    assert qinj_playing(fake)

    ctrl_c(sat, thread)

    # do_run returned on stop_requested(), it did not die on the released socket
    assert sat.fsm.state != SatelliteState.ERROR
    with fake.lock:
        assert fake.config[15] == ACTIVE_CHANNEL
    assert not qinj_playing(fake)
    assert sat.connection_socket is None
    assert sat.i2c_conn is None and connections[0].closed
    assert sat._state_thread_fut.done()
    for worker in list(sat._state_thread_exc._threads):
        worker.join(2.0)
        assert not worker.is_alive()

    caplog.clear()
    sat.reentry()
    assert [r.getMessage() for r in caplog.records if "failed" in r.getMessage()] == []
