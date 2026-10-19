"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocWaveform pixel and waveform-sampler mode keys (ws_pixel, qsel, qinj_en, offset, trigger_pixels, rfsel,
ws_sel1, ws_sel2): the launch sequence with the defaults, reconfigure in ORBIT, refusals; and the wrapper's
regOut1F / RFSel writes. Simulated KC705, a recording stand-in for the i2c_gui2 connection.
"""

import threading
import time

import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.network import get_loopback_interface_name
from constellation.core.protocol.cscp1 import SatelliteState

from ...EtrocCommon import new_wrapper_i2cGui2 as wrapper
from ...EtrocCounters.tests.fakes import FakeKC705
from ..EtrocWaveform import EtrocWaveform

OK = CSCP1Message.Type.SUCCESS
PIXEL_CALLS = ("batch_disable_all_chips", "batch_auto_calibrate_chip", "batch_enable_pixels")


class RecordingI2C:
    """i2c_connection stand-in that records every batch_* call with its arguments."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("batch_"):
            def record(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return {} if name == "batch_ws_read_data" else None
            return record
        raise AttributeError(name)

    def pixel_calls(self):
        return [c for c in self.calls if c[0] in PIXEL_CALLS]

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

    def _make(**extra):
        conns = []

        def open_i2c(*args, **kwargs):
            conns.append(RecordingI2C())
            return conns[-1]

        monkeypatch.setattr(wrapper, "i2c_connection", open_i2c)
        fake = FakeKC705(window_s=0.05).start()
        fake.on_status_read = lambda addr: setattr(fake, "ws_ready", 1)
        sat = EtrocWaveform("Test", "wspixels", 0, 0, 0, [get_loopback_interface_name()])
        thread = threading.Thread(target=sat.run_satellite, daemon=True)
        thread.start()
        ctx = zmq.Context()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 10000)
        sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
        tx = CommandTransmitter("test", sock)
        config = {
            "hostname": "127.0.0.1", "port": fake.port, "socket_timeout": 5.0, "clear_fifo": 0,
            "fast_command_memo": "Start QInj L1A Triggerbit", "active_channel": 0x0001, "ws_active_channel": 0x0091,
            "i2c_port": "/dev/fake", "output_path": str(tmp_path), "max_events": 1, **extra,
        }
        assert call(tx, "initialize", config).verb_type == OK
        wait_state(sat, SatelliteState.INIT)
        assert call(tx, "launch").verb_type == OK
        wait_state(sat, SatelliteState.ORBIT)
        made.append((sat, tx, fake, thread, sock, ctx))
        return sat, tx, conns[-1]

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


ALL_KEYS = {"ws_pixel": [0, 14], "qsel": 0x1E, "qinj_en": True, "offset": 10, "trigger_pixels": [], "rfsel": -1,
            "ws_sel1": 0, "ws_sel2": 0}


def test_launch_with_the_defaults_configures_pixel_0_14_as_before(make):
    sat, tx, i2c = make()
    assert i2c.pixel_calls() == [
        ("batch_disable_all_chips", (), {"power_mode": "high"}),
        ("batch_auto_calibrate_chip", (), {"pixel_list": [(0, 14)]}),
        ("batch_enable_pixels", (), {"pixel_list": [(0, 14)], "Qsel": 0x1E, "QInjEn": True, "offset": 10, "rfsel": None}),
    ]
    # the WS starts in bypass mode, powered from ws_start until ws_stop (regOut1F 0x0b)
    assert call(tx, "start", "r1").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    deadline = time.monotonic() + 10
    while sat.file_counter < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert call(tx, "stop").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT, 15)
    starts = [c for c in i2c.calls if c[0] == "batch_ws_start_sampling"]
    assert len(starts) >= 2 and all(c[2] == {"sel1": 0, "sel2": 0} for c in starts)


def test_reconfigure_pixel_keys_reruns_the_pixel_sequence(make):
    sat, tx, i2c = make(**ALL_KEYS)
    i2c.calls.clear()
    change = {"qsel": 31, "qinj_en": False, "trigger_pixels": [3, 11], "rfsel": 0, "offset": 20}
    rep = call(tx, "reconfigure", change)
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    assert i2c.pixel_calls() == [
        ("batch_disable_all_chips", (), {"power_mode": "high"}),
        ("batch_auto_calibrate_chip", (), {"pixel_list": [(0, 14), (3, 11)]}),
        ("batch_enable_pixels", (), {"pixel_list": [(0, 14)], "Qsel": 31, "QInjEn": False, "offset": 20, "rfsel": 0}),
        ("batch_enable_pixels", (), {"pixel_list": [(3, 11)], "Qsel": 31, "QInjEn": True, "offset": 20, "rfsel": 0}),
    ]
    summary = sat._ws_summary()["pixels"]
    assert summary["qinj_en"] is False and summary["trigger_pixels"] == [[3, 11]] and summary["qsel"] == 31

    # a non-pixel key does not touch the pixels
    i2c.calls.clear()
    assert call(tx, "reconfigure", {"ws_active_channel": 0x0081}).verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    assert i2c.pixel_calls() == []


def test_ws_mode_bits_reach_ws_start_sampling(make):
    sat, tx, i2c = make(**ALL_KEYS)
    assert call(tx, "reconfigure", {"ws_sel1": 1, "ws_sel2": 1}).verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)
    i2c.calls.clear()
    assert call(tx, "start", "r2").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    assert call(tx, "stop").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT, 15)
    starts = [c for c in i2c.calls if c[0] == "batch_ws_start_sampling"]
    assert starts and all(c[2] == {"sel1": 1, "sel2": 1} for c in starts)


@pytest.mark.parametrize("key, good, bad", [
    ("qsel", 31, 32), ("ws_pixel", [1, 14], [0, 16]), ("ws_pixel", [1, 14], [0]), ("trigger_pixels", [3, 11], [3]),
    ("trigger_pixels", [3, 11], [0, 14]), ("trigger_pixels", [3, 11], [3, 11, 3, 11]), ("rfsel", 0, 4),
    ("ws_sel2", 1, 2), ("offset", 20, 2000),
])
def test_invalid_pixel_values_are_refused_in_orbit(make, key, good, bad):
    sat, tx, i2c = make(**ALL_KEYS)
    rep = call(tx, "reconfigure", {key: good})
    assert rep.verb_type == OK, rep.verb_msg
    wait_state(sat, SatelliteState.ORBIT)
    i2c.calls.clear()
    rep = call(tx, "reconfigure", {key: bad})
    assert rep.verb_type != OK and key in rep.verb_msg
    time.sleep(0.2)
    assert sat.fsm.state == SatelliteState.ORBIT
    assert i2c.pixel_calls() == []
    assert getattr(sat, key) == good


class FakeChip:
    def __init__(self):
        self.regs = {}
        self.written = []
        self.decoded = {}

    def __setitem__(self, key, value):
        self.regs[key] = value

    def read_register(self, *key):
        pass

    def write_register(self, *key):
        self.written.append((key[-1], self.regs.get(key)))

    def read_decoded_value(self, *key):
        pass

    def set_decoded_value(self, space, block, name, value):
        self.decoded[name] = value

    def write_decoded_value(self, *key):
        pass

    def read_all_block(self, *key):
        pass

    def write_all_block(self, *key):
        pass


class _Iss:
    def close(self):
        pass


class _Helper:
    def __init__(self, port, clock, dummy_connect=False):
        self._iss = _Iss()


@pytest.fixture
def conn_and_chip(monkeypatch):
    monkeypatch.setattr(wrapper.i2c_gui2, "USB_ISS_Helper", _Helper)
    conn = wrapper.i2c_connection("/dev/fake", [0x60], [0x40], ["c"])
    chip = FakeChip()
    monkeypatch.setattr(conn, "_resolve_chip", lambda addr: chip)
    return conn, chip


@pytest.mark.parametrize("sel1, sel2, final", [(0, 0, 0x0B), (1, 0, 0x8B), (0, 1, 0x4B), (1, 1, 0xCB)])
def test_ws_start_sampling_writes_the_mode_bits(conn_and_chip, sel1, sel2, final):
    conn, chip = conn_and_chip
    conn.batch_ws_start_sampling(sel1=sel1, sel2=sel2)
    assert [v for name, v in chip.written if name == "regOut1F"] == [0x22, final]
    assert final & 0x04 == 0  # rd_en_I2C cleared at every start


def test_rfsel_is_written_only_when_given(conn_and_chip):
    conn, chip = conn_and_chip
    conn.config_single_pixel(0x60, 0, 14, Qsel=31, QInjEn=True)
    assert "RFSel" not in chip.decoded and chip.decoded["QSel"] == 31
    conn.config_single_pixel(0x60, 0, 14, Qsel=31, QInjEn=True, rfsel=0)
    assert chip.decoded["RFSel"] == 0
