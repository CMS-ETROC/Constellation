"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocWaveform R-KC7-SW-20/21 against the simulated KC705: the FC grammar on the played FC memory,
the FPGA counters in ORBIT, one file index per event, max_events, and a stop during the WS-ready poll.
"""

import json
import threading
import time

import pandas as pd
import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from ...EtrocCommon import new_wrapper_i2cGui2
from ...EtrocCounters.tests.fakes import FC_L1A, FC_QINJ, FakeKC705
from ..EtrocWaveform import EtrocWaveform
from .test_waveform_satellite import OK, call, wait_state

ORBITS_PER_WINDOW = 40_000_001 / 3564


class TwoChipI2C:
    """i2c_connection stand-in with two chips; the WS read returns one small frame per chip."""

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.reads = 0

    def __getattr__(self, name):
        if name.startswith("batch_"):
            return lambda *args, **kwargs: None
        raise AttributeError(name)

    def batch_ws_read_data(self):
        self.reads += 1
        return {name: pd.DataFrame({"Dout": [0.0, 1.0]}) for name in ("c1", "c2")}

    def close(self):
        self.closed = True


@pytest.fixture
def make(tmp_path, monkeypatch):
    made = []

    def _make(always_ready=True, **extra):
        monkeypatch.setattr(new_wrapper_i2cGui2, "i2c_connection", TwoChipI2C)
        fake = FakeKC705(window_s=0.05).start()
        if always_ready:
            fake.on_status_read = lambda addr: setattr(fake, "ws_ready", 1)
        sat = EtrocWaveform("Test", "wsrun", 0, 0, 0, [get_loopback_interface_name()])
        thread = threading.Thread(target=sat.run_satellite, daemon=True)
        thread.start()
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 10000)
        sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
        tx = CommandTransmitter("test", sock)
        config = {
            "hostname": "127.0.0.1", "port": fake.port, "socket_timeout": 5.0, "clear_fifo": 0,
            "fast_command_memo": "Start QInj L1A Triggerbit", "fc_grammar": "fixed", "max_events": 0,
            "ws_ready_timeout": 5.0, "i2c_port": "/dev/fake", "chip_addresses": [0x60, 0x61],
            "ws_addresses": [0x40, 0x41], "chip_names": ["c1", "c2"], "output_path": str(tmp_path), **extra,
        }
        assert call(tx, "initialize", config).verb_type == OK
        wait_state(sat, SatelliteState.INIT)
        assert call(tx, "launch").verb_type == OK
        wait_state(sat, SatelliteState.ORBIT)
        made.append((sat, tx, fake, thread, sock, ctx))
        return sat, tx, fake

    yield _make
    for sat, tx, fake, thread, sock, ctx in made:
        if thread.is_alive() and sat.fsm.state == SatelliteState.RUN:
            call(tx, "stop")
            wait_state(sat, SatelliteState.ORBIT, 15)
        if thread.is_alive() and sat.fsm.state == SatelliteState.ORBIT:
            call(tx, "land")
            wait_state(sat, SatelliteState.INIT)
        sat.reentry()
        thread.join(5.0)
        sock.close()
        ctx.term()
        fake.stop()


def played(fake, cmd):
    with fake.lock:
        return fake.pattern.count(cmd)


def test_grammar_switch_in_orbit_reprograms_n_versus_n_plus_one(make):
    sat, tx, fake = make()
    memo = "Start QInj L1A L1ARange repeatedQInj=10 Triggerbit"
    rep = call(tx, "reconfigure", {"fast_command_memo": memo})
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    assert played(fake, FC_QINJ) == 10 and played(fake, FC_L1A) == 10
    rep = call(tx, "reconfigure", {"fc_grammar": "legacy"})
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    assert played(fake, FC_QINJ) == 11 and played(fake, FC_L1A) == 11
    rep = call(tx, "get_fc_schedule")
    assert rep.verb_type == OK and len(rep.payload["played_l1a_bx"]) == 11


def test_counters_in_orbit_count_the_played_l1as(make):
    sat, tx, fake = make()
    assert call(tx, "reconfigure", {"fast_command_memo": "Start QInj L1A L1ARange repeatedQInj=4 Triggerbit"}).verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    rep = call(tx, "read_fpga_counters")
    assert rep.verb_type == OK, rep.verb_msg
    assert rep.payload["header"] == int(4 * ORBITS_PER_WINDOW)
    with fake.lock:
        assert fake.config[13] == sat.timestamp  # Reg13 restored


def test_uniform_200_is_refused_in_fixed_and_the_satellite_stays_in_orbit(make):
    sat, tx, fake = make()
    rep = call(tx, "reconfigure", {"fast_command_memo": "Start QInj L1A L1ARange repeatedQInj=200 uniform Triggerbit"})
    assert rep.verb_type != OK and "187" in rep.verb_msg
    time.sleep(0.1)
    assert sat.fsm.state == SatelliteState.ORBIT
    # the same memo is accepted together with the legacy grammar, and plays one injection
    rep = call(tx, "reconfigure", {"fc_grammar": "legacy",
                                   "fast_command_memo": "Start QInj L1A L1ARange repeatedQInj=200 uniform Triggerbit"})
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    assert played(fake, FC_QINJ) == 1


def test_one_file_index_per_event_and_max_events(make, tmp_path):
    sat, tx, fake = make(max_events=3)
    assert call(tx, "start", "ws1").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    deadline = time.monotonic() + 10
    while call(tx, "get_ws_status").payload["events"] < 3:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    time.sleep(0.3)
    assert sat.i2c_conn.reads == 3  # the limit holds: no fourth capture
    assert call(tx, "stop").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    names = sorted(p.name for p in (tmp_path / "ws1").glob("WS_rawData_*.csv"))
    assert names == [f"WS_rawData_{c}_{i:04d}.csv" for c in ("c1", "c2") for i in range(3)]
    rows = (tmp_path / "ws1" / "ws_events.csv").read_text().splitlines()
    assert rows[0].startswith("event,") and [r.split(",")[0] for r in rows[1:]] == ["0", "1", "2"]
    summary = json.loads((tmp_path / "ws1" / "ws_run_summary.json").read_text())
    assert summary["events"] == 3 and len(summary["read_seconds"]) == 3 and summary["fc_grammar"] == "fixed"


def test_stop_during_the_ws_ready_poll_is_prompt(make):
    sat, tx, fake = make(always_ready=False, ws_ready_timeout=30.0)
    assert call(tx, "start", "ws_nopulse").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    time.sleep(0.5)
    with fake.lock:
        fake.ws_ready = 0
    t0 = time.monotonic()
    assert call(tx, "stop").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT, 5)
    assert time.monotonic() - t0 < 2.0
    assert call(tx, "get_ws_status").payload["events"] == 0
