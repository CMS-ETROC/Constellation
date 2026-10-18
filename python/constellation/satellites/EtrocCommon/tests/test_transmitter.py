"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocTransmitter on loopback CSCP against the simulated KC705.
"""

import threading
import time
import types

import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from ...EtrocCounters.tests.fakes import FakeKC705
from ...EtrocTransmitter import EtrocTransmitter as tx_module
from ...EtrocTransmitter.EtrocTransmitter import EtrocTransmitter

OK = CSCP1Message.Type.SUCCESS
CLEAR_FIFO_BIT = 0x0002


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
def setup(monkeypatch):
    # the firmware waits (2.1 s after a FIFO reset) are not needed against the simulation
    fast_time = types.SimpleNamespace(**{n: getattr(time, n) for n in ("strftime", "localtime", "time", "monotonic")})
    fast_time.sleep = lambda s: None
    monkeypatch.setattr(tx_module, "time", fast_time)
    fake = FakeKC705(window_s=0.05).start()
    sat = EtrocTransmitter(0, "Test", "txtest", 0, 0, 0, [get_loopback_interface_name()])
    thread = threading.Thread(target=sat.run_satellite, daemon=True)
    thread.start()
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 5000)
    sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
    tx = CommandTransmitter("test", sock)
    config = {"hostname": "127.0.0.1", "port": fake.port, "socket_timeout": 5.0, "fc_delays": 0x0123}
    assert call(tx, "initialize", config).verb_type == OK
    wait_state(sat, SatelliteState.INIT)
    assert call(tx, "launch").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    yield sat, tx, fake
    if sat.fsm.state == SatelliteState.ORBIT:
        call(tx, "land")
        wait_state(sat, SatelliteState.INIT)
    sat.reentry()
    sock.close()
    ctx.term()
    fake.stop()


def test_register_commands_take_list_payloads(setup):
    sat, tx, fake = setup
    assert call(tx, "set_ledpage", [2]).verb_type == OK
    assert call(tx, "set_ledpage", 2).verb_type != OK  # a bare scalar is not a payload list
    with fake.lock:
        assert (fake.config[13] >> 2) & 0x7 == 2  # LED_Pages
    assert call(tx, "get_config_register", [13]).verb_type == OK
    assert call(tx, "set_data_phase_channel_delay", [5, 1]).verb_type == OK
    assert call(tx, "set_fast_command_memo", ["Start Triggerbit"]).verb_type == OK


def test_start_clears_the_fifo_restarts_fast_commands_and_records_registers(setup):
    sat, tx, fake = setup
    with fake.lock:
        fake.config[7] = 0x0042  # changed on the board after launch
        fifo_resets = fake.pulses(CLEAR_FIFO_BIT)
    sat.do_starting("run7")
    with fake.lock:
        assert fake.pulses(CLEAR_FIFO_BIT) == fifo_resets + 1
        assert fake.pattern is not None  # FC playback restarted after the global reset
        received = list(fake.received)
    last_reset = max(i for i, (kind, _, v) in enumerate(received) if kind == "pulse" and v & CLEAR_FIFO_BIT)
    assert any(kind == "write_config" for kind, _, _ in received[last_reset + 1:])

    readback = sat.bor["kc705_readback"]
    assert readback["reg4_fc_delays"] == "0x0123"
    assert readback["reg7_counter_duration"] == "0x0042"
    assert {f"reg{r}" for r in (4, 5, 6, 7, 13, 14, 15)} <= {k.split("_")[0] for k in readback}
    assert sat.bor["run_identifier"] == "run7"
