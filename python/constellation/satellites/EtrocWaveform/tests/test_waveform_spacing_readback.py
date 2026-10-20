"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocWaveform opt-in keys for the waveform-sampler studies: qinj_spacing_bx (BX between the injections of
repeatedQInj=N in the fixed grammar; default 16 = as before) and pixel_readback (the chip's Pixel Config of the
configured pixels read back at run start and run stop, into ws_run_summary.json; default off = as before).
Simulated KC705, a recording stand-in for the i2c_gui2 connection.
"""

import json
import threading
import time
from pathlib import Path

import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from ...EtrocCommon import new_wrapper_i2cGui2 as wrapper
from ...EtrocCounters.tests.fakes import FakeKC705
from ..EtrocWaveform import ORBIT_LAST_BX, READBACK_FIELDS, EtrocWaveform, fc_schedule

OK = CSCP1Message.Type.SUCCESS
QINJ = 0x5
DENSE = "Start QInj L1A Triggerbit repeatedQInj=200"


class ReadbackI2C:
    """i2c_connection stand-in: records batch_* calls; batch_read_pixel_config answers QInjEn per pixel."""

    fail = False

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.qinj = {}

    def __getattr__(self, name):
        if name.startswith("batch_"):
            def record(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                if name == "batch_enable_pixels":
                    for p in kwargs["pixel_list"]:
                        self.qinj[tuple(p)] = int(bool(kwargs["QInjEn"]))
                return {} if name == "batch_ws_read_data" else None
            return record
        raise AttributeError(name)

    def batch_read_pixel_config(self, pixel_list, fields):
        self.calls.append(("batch_read_pixel_config", (), {"pixel_list": pixel_list, "fields": fields}))
        if self.fail:
            raise OSError("USB-ISS read timed out")
        return {"0x60": {f"{r},{c}": {f: (self.qinj.get((r, c), 0) if f == "QInjEn" else 0) for f in fields}
                         for r, c in pixel_list}}

    def names(self):
        return [c[0] for c in self.calls]

    def close(self):
        pass


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
def make(tmp_path, monkeypatch):
    made = []

    def _make(i2c_class=ReadbackI2C, **extra):
        conns = []

        def open_i2c(*args, **kwargs):
            conns.append(i2c_class())
            return conns[-1]

        monkeypatch.setattr(wrapper, "i2c_connection", open_i2c)
        fake = FakeKC705(window_s=0.05).start()
        fake.on_status_read = lambda addr: setattr(fake, "ws_ready", 1)
        sat = EtrocWaveform("Test", "wsp10", 0, 0, 0, [get_loopback_interface_name()])
        thread = threading.Thread(target=sat.run_satellite, daemon=True)
        thread.start()
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 10000)
        sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
        tx = CommandTransmitter("test", sock)
        config = {
            "hostname": "127.0.0.1", "port": fake.port, "socket_timeout": 5.0, "clear_fifo": 0,
            "fast_command_memo": DENSE, "active_channel": 0x0001, "ws_active_channel": 0x00A1,
            "i2c_port": "/dev/fake", "output_path": str(tmp_path), "max_events": 1, **extra,
        }
        assert call(tx, "initialize", config).verb_type == OK
        wait_state(sat, SatelliteState.INIT)
        assert call(tx, "launch").verb_type == OK
        wait_state(sat, SatelliteState.ORBIT)
        made.append((sat, tx, fake, thread, sock, ctx))
        return sat, tx, fake, conns[-1]

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


def played_qinj(fake):
    return [bx for bx, cmd in enumerate(fake.pattern or []) if cmd == QINJ]


def train_becomes(sat, fake, expected, timeout=10.0):
    """The reconfigure transition runs after the reply: wait until ORBIT with the expected train playing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sat.fsm.state == SatelliteState.ORBIT and played_qinj(fake) == expected:
            return True
        time.sleep(0.02)
    return played_qinj(fake) == expected


def one_run(sat, tx, tmp_path, run_id):
    assert call(tx, "start", run_id).verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    deadline = time.monotonic() + 10
    while sat.file_counter < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert call(tx, "stop").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    return json.loads((Path(tmp_path) / run_id / "ws_run_summary.json").read_text())


# ------------------------------------------------------------------ the schedule
def test_spacing_17_places_the_injections_17_bx_apart_and_16_is_unchanged():
    assert fc_schedule(DENSE, "fixed", 17)["qinj_bx"] == [5 + 17 * k for k in range(200)]
    assert fc_schedule(DENSE, "fixed")["qinj_bx"] == [5 + 16 * k for k in range(200)]
    assert fc_schedule(DENSE, "fixed", 16) == fc_schedule(DENSE, "fixed")
    two = fc_schedule("Start QInj L1A L1ARange Triggerbit repeatedQInj=2", "fixed", 12)
    assert two["qinj_bx"] == [5, 17] and two["l1a_bx"] == [509, 521]


def test_spacing_refusals():
    with pytest.raises(ValueError, match="beyond the orbit"):
        fc_schedule(DENSE, "fixed", 20)  # 5 + 199 x 20 = 3985
    assert fc_schedule(DENSE.replace("200", "178"), "fixed", 20)["qinj_bx"][-1] == 5 + 177 * 20 <= ORBIT_LAST_BX
    with pytest.raises(ValueError, match="fixed grammar"):
        fc_schedule(DENSE, "legacy", 17)
    for bad in (0, ORBIT_LAST_BX + 1):
        with pytest.raises(ValueError, match="qinj_spacing_bx"):
            fc_schedule(DENSE, "fixed", bad)


# ------------------------------------------------------------------ the satellite
def test_default_spacing_programs_the_16_bx_train_as_before(make):
    sat, tx, fake, i2c = make()
    assert played_qinj(fake) == [5 + 16 * k for k in range(200)]


def test_reconfigure_spacing_reprograms_the_train(make):
    sat, tx, fake, i2c = make()
    rep = call(tx, "reconfigure", {"qinj_spacing_bx": 17})
    assert rep.verb_type == OK, rep.verb_msg
    assert train_becomes(sat, fake, [5 + 17 * k for k in range(200)])
    sched = call(tx, "get_fc_schedule")
    assert sched.payload["spacing_bx"] == 17
    # a spacing whose train leaves the orbit is refused; the satellite stays in ORBIT with the old train
    rep = call(tx, "reconfigure", {"qinj_spacing_bx": 20})
    assert rep.verb_type != OK and "beyond the orbit" in rep.verb_msg
    assert sat.fsm.state == SatelliteState.ORBIT and sat.qinj_spacing_bx == 17
    assert played_qinj(fake) == [5 + 17 * k for k in range(200)]
    # memo and spacing together
    rep = call(tx, "reconfigure", {"qinj_spacing_bx": 20, "fast_command_memo": DENSE.replace("200", "178")})
    assert rep.verb_type == OK, rep.verb_msg
    assert train_becomes(sat, fake, [5 + 20 * k for k in range(178)])


def test_pixel_readback_is_off_by_default(make, tmp_path):
    sat, tx, fake, i2c = make()
    summary = one_run(sat, tx, tmp_path, "r0")
    assert "batch_read_pixel_config" not in i2c.names()
    assert summary["pixel_readback"] is None and summary["qinj_spacing_bx"] == 16


def test_pixel_readback_at_run_start_and_stop(make, tmp_path):
    sat, tx, fake, i2c = make(pixel_readback=True, qinj_en=False, trigger_pixels=[1, 13])
    summary = one_run(sat, tx, tmp_path, "r1")
    names = i2c.names()
    reads = [i for i, n in enumerate(names) if n == "batch_read_pixel_config"]
    starts = [i for i, n in enumerate(names) if n == "batch_ws_start_sampling"]
    assert len(reads) == 2
    assert reads[0] < starts[0]  # before the first capture is armed
    assert reads[1] > starts[-1]  # after the last re-arm
    rb = summary["pixel_readback"]
    assert set(rb) == {"start", "stop"}
    for tag in ("start", "stop"):
        values = rb[tag]["values"]["0x60"]
        assert values["0,14"]["QInjEn"] == 0 and values["1,13"]["QInjEn"] == 1
        assert set(values["0,14"]) == set(READBACK_FIELDS)
    assert i2c.calls[reads[0]][2]["pixel_list"] == [(0, 14), (1, 13)]


def test_pixel_readback_failure_is_recorded_not_raised(make, tmp_path):
    class Failing(ReadbackI2C):
        fail = True

    sat, tx, fake, i2c = make(i2c_class=Failing, pixel_readback=True)
    summary = one_run(sat, tx, tmp_path, "r2")
    assert summary["events"] == 1
    assert "timed out" in summary["pixel_readback"]["start"]["error"]
    assert "timed out" in summary["pixel_readback"]["stop"]["error"]
