"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocReceiver reconfigure and Ctrl-C reentry over loopback CSCP. No transmitter runs: the
test feeds records through receive_data, as the CDTP pull thread would.
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

from ..EtrocReceiver import EtrocReceiver

OK = CSCP1Message.Type.SUCCESS
FIFO_FILLER = 0x556


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


def record(n):
    words = [(FIFO_FILLER << 20) | i for i in range(n)]
    return types.SimpleNamespace(blocks=[np.asarray(words, dtype=">u4").tobytes()])


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


def test_reconfigure_of_a_framework_section_is_refused_in_orbit(setup):
    sat, tx, _ = setup
    rep = call(tx, "reconfigure", {"_data": {"receive_from": ["Other"]}})
    assert rep.verb_type != OK
    assert "framework sections (_data)" in rep.verb_msg and "re-initialize" in rep.verb_msg
    time.sleep(0.2)
    assert sat.fsm.state == SatelliteState.ORBIT


def test_ctrl_c_in_run_closes_the_file_with_the_last_lines(setup, tmp_path, caplog):
    sat, tx, thread = setup
    assert call(tx, "start", "run_ctrl_c").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    sat.receive_data("EtrocTransmitter.a", record(7))
    data_file = tmp_path / "run_ctrl_c" / "file_0.nem"
    assert data_file.read_text() == ""  # still in the file buffer

    ctrl_c(sat, thread)

    assert sat.outfile is None
    assert len(data_file.read_text().splitlines()) == 7
    assert "reentry" in (tmp_path / "run_ctrl_c" / "RUN_NOT_STOPPED_CLEANLY.txt").read_text()
    assert not sat._drc._pull_thread.is_alive()
    assert sat._state_thread_fut.done()
    for worker in list(sat._state_thread_exc._threads):
        worker.join(2.0)
        assert not worker.is_alive()

    caplog.clear()
    sat.reentry()
    assert [r.getMessage() for r in caplog.records if "failed" in r.getMessage()] == []
