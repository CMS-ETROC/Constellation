"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocReceiver reconfigure over loopback CSCP. No transmitter runs: the
test feeds records through receive_data, as the CDTP pull thread would.
"""

import threading
import time

import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from ..EtrocReceiver import EtrocReceiver

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


@pytest.fixture
def setup(tmp_path):
    sat = EtrocReceiver("Test", "rxlifecycle", 0, 0, 0, [get_loopback_interface_name()])
    thread = threading.Thread(target=sat.run_satellite, daemon=True)
    thread.start()
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 5000)
    sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
    tx = CommandTransmitter("test", sock)
    # a long flush interval keeps the written lines in the file buffer until the file is closed
    config = {"output_path": str(tmp_path), "translate": 1, "skip_fillers": 0, "flush_interval": 1000.0}
    assert call(tx, "initialize", config).verb_type == OK
    wait_state(sat, SatelliteState.INIT)
    assert call(tx, "launch").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    yield sat, tx, thread
    if thread.is_alive() and sat.fsm.state == SatelliteState.ORBIT:
        call(tx, "land")
        wait_state(sat, SatelliteState.INIT)
    sat.reentry()
    thread.join(5.0)
    sock.close()
    ctx.term()


def test_reconfigure_switches_to_raw_files(setup):
    sat, tx, _ = setup
    rep = call(tx, "reconfigure", {"translate": 0, "flush_interval": 5})  # integer for a float key
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    assert sat.translate == 0 and sat.file_size_limit == 20 * 10**6
    assert sat._config._dictionary["translate"] == 0
    assert sat._config._dictionary["flush_interval"] == 5.0


def test_output_path_needs_a_landing(setup, tmp_path):
    sat, tx, _ = setup
    rep = call(tx, "reconfigure", {"output_path": str(tmp_path / "other")})
    assert rep.verb_type != OK
    assert "'output_path'" in rep.verb_msg and "land and re-initialize" in rep.verb_msg
    time.sleep(0.2)
    assert sat.fsm.state == SatelliteState.ORBIT
