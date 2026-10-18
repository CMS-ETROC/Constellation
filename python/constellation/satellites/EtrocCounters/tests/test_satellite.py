"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocCounters driven over CSCP (real ZeroMQ on loopback) against the simulated KC705.
"""

import json
import threading
import time

import pytest
import zmq

from constellation.core.cscp import CommandTransmitter
from constellation.core.message.cscp1 import CSCP1Message
from constellation.core.protocol.cscp1 import SatelliteState
from constellation.core.network import get_loopback_interface_name

from .. import chipio
from ..EtrocCounters import EtrocCounters
from .fakes import FakeChip, FakeKC705

WINDOW = 0.03
PIXEL = (2, 5)
OK = CSCP1Message.Type.SUCCESS


def wait_state(sat, state, timeout=10.0):
    deadline = time.monotonic() + timeout
    while sat.fsm.state != state:
        if time.monotonic() > deadline:
            raise AssertionError(f"never reached {state.name}; now {sat.fsm.state.name}: {sat.fsm.status}")
        time.sleep(0.01)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    chip = FakeChip({PIXEL: (480, 4)})

    def open_chip(*args, **kwargs):
        chip.closed = False
        return chip

    monkeypatch.setattr(chipio, "I2CGui2ChipIO", open_chip)
    fake = FakeKC705(chip=chip, window_s=WINDOW).start()
    sat = EtrocCounters("Test", "countertest", 0, 0, 0, [get_loopback_interface_name()])
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
        "firmware_window_s": WINDOW,
        "output_path": str(tmp_path),
        "i2c_port": "/dev/fake",
        "chip_name": "ET2p03_sat",
        "kc705_registers": {"triggerbit_delay": 0x1800},
    }
    yield sat, tx, fake, chip, config
    if sat.fsm.state == SatelliteState.RUN:
        call(tx, "stop")
        wait_state(sat, SatelliteState.ORBIT)
    if sat.fsm.state == SatelliteState.ORBIT:
        call(tx, "land")
        wait_state(sat, SatelliteState.INIT)
    sat.reentry()
    sock.close()
    ctx.term()
    fake.stop()


def call(tx, cmd, payload=None):
    """Send one CSCP request and return the reply, also when it is not SUCCESS."""
    tx.send_request(cmd, payload)
    return tx.get_message()


def launch(sat, tx, config):
    assert call(tx, "initialize", config).verb_type == OK
    wait_state(sat, SatelliteState.INIT)
    assert call(tx, "launch").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)


def wait_scan_done(tx, timeout=20.0):
    deadline = time.monotonic() + timeout
    while call(tx, "get_scan_status").verb_msg in ("starting", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return call(tx, "get_scan_result").payload


def wait_settled(tx, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        rep = call(tx, "read_counters")
        if rep.payload is not None and rep.payload["settled"]:
            return rep.payload
        assert time.monotonic() < deadline, "never settled"
        time.sleep(WINDOW / 2)


def no_qinj_playing(fake):
    return not fake.pattern or 0x5 not in fake.pattern


def test_launch_read_and_scan_over_cscp(setup):
    sat, tx, fake, chip, config = setup
    initial = chip.snapshot()
    launch(sat, tx, config)
    assert fake.writes_to(8) == [0x1800]

    rep = call(tx, "read_counters")
    assert rep.verb_type == OK
    assert rep.payload["channel"] == 0
    assert rep.payload["header"] == 0
    assert set(rep.payload["led_bits"]) >= {"monitor_sel", "trig_synched", "synched"}

    assert call(tx, "set_fc_mode", ["qinj_l1a"]).verb_type == OK
    assert wait_settled(tx)["header"] == 11223
    assert call(tx, "set_fc_mode", ["custom Start QInj L1A Triggerbit"]).verb_type == OK
    assert call(tx, "set_fc_mode", ["custom Start QInj L1A"]).verb_type != OK  # no Triggerbit
    assert call(tx, "set_fc_mode", ["sometimes"]).verb_type != OK
    assert call(tx, "set_fc_mode", ["idle"]).verb_type == OK

    rep = call(tx, "noise_scan", [{"pixels": [list(PIXEL)], "rel_min": -2, "rel_max": 2}])
    assert rep.verb_type == OK, rep.verb_msg
    assert call(tx, "set_channel", [1]).verb_type != OK  # refused while a scan runs
    assert call(tx, "start", "refused_run").verb_type != OK  # refused while a scan runs
    result = wait_scan_done(tx)
    assert result["status"] == "finished"
    assert result["results"][0]["peak_dac"] == 480
    assert chip.snapshot() == initial
    assert sat.fsm.state == SatelliteState.ORBIT

    assert call(tx, "check_status_layout").verb_msg == "Layout OK"
    assert call(tx, "noise_scan", [{"pixels": [[0, 0]], "bogus": 1}]).verb_type != OK
    assert fake.writes_to(7) == []
    assert fake.pulses(0x0002) == 0


def test_single_value_commands_take_a_list_payload(setup):
    sat, tx, _, _, config = setup
    launch(sat, tx, config)
    rep = call(tx, "set_channel", [2])
    assert rep.verb_type == OK and rep.payload == 2
    assert call(tx, "set_channel", 2).verb_type != OK  # a bare scalar is not a payload list
    assert call(tx, "set_channel", [0]).verb_type == OK


def test_scan_parameters_as_json_string(setup):
    sat, tx, _, _, config = setup
    launch(sat, tx, config)
    rep = call(tx, "noise_scan", ['{"pixels": [[2, 5]], "dac_min": 479, "dac_max": 481}'])
    assert rep.verb_type == OK, rep.verb_msg
    assert wait_scan_done(tx)["results"][0]["peak_dac"] == 480
    assert call(tx, "noise_scan", ["{not json"]).verb_type != OK


def test_metrics_read_only_the_cache(setup):
    sat, tx, fake, _, config = setup
    launch(sat, tx, config)
    wait_settled(tx)
    sat._stop_poller()
    time.sleep(2 * WINDOW)  # cache older than one window, younger than the metric age limit
    with fake.lock:
        before = len(fake.received)
    values = [sat.REFRESH(), sat.DATA_RATE(), sat.HEADER_RATE(), sat.TRIGBIT_RATE(),
              sat.COINCIDENCE_RATE(), sat.CHANNEL()]
    with fake.lock:
        assert len(fake.received) == before
    assert values[-1] == 0
    assert values[0] is not None


def test_settled_recovers_after_counter_reset(setup):
    sat, tx, fake, _, config = setup
    launch(sat, tx, config)
    with fake.lock:
        fake.refresh = [1000, 1000, 1000, 1000]
    assert call(tx, "set_fc_mode", ["qinj_l1a"]).verb_type == OK  # settle reference near 1000
    wait_settled(tx)
    with fake.lock:
        fake.refresh = [0, 0, 0, 0]  # what a re-alignment does to every counter
    assert wait_settled(tx)["refresh"] < 20


def test_fail_gracefully_mid_scan_restores_and_idles(setup):
    sat, tx, fake, chip, config = setup
    initial = chip.snapshot()
    launch(sat, tx, config)
    rep = call(tx, "qinj_scurve", [{"pixels": [list(PIXEL)], "qsels": [9], "method": "sweep",
                                     "dac_min": 470, "dac_max": 560, "step": 1}])
    assert rep.verb_type == OK
    deadline = time.monotonic() + 10
    while (call(tx, "get_scan_status").payload or {}).get("points", 0) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert fake.pattern and 0x5 in fake.pattern  # QInj playing mid-scan
    scan_thread = sat._scan_thread
    chip.write_delay = 0.2  # the register restore is still running when fail_gracefully is called
    sat.fail_gracefully()
    assert not scan_thread.is_alive()
    assert sat._scan_result["status"] == "aborted"
    assert chip.snapshot() == initial
    assert no_qinj_playing(fake)
    assert sat._link is None


def test_run_records_every_window_and_survives_a_stall(setup, tmp_path):
    sat, tx, fake, _, config = setup
    launch(sat, tx, config)
    assert call(tx, "start", "run1").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    time.sleep(10 * WINDOW)
    assert call(tx, "read_counters").verb_type == OK
    assert call(tx, "noise_scan", [{"pixels": [list(PIXEL)]}]).verb_type != OK  # ORBIT only
    with fake.lock:
        fake.aligned_channels = set()  # link lost: refresh held at 0
    time.sleep(1.5)
    with fake.lock:
        fake.aligned_channels = {0}
    time.sleep(10 * WINDOW)
    assert sat.fsm.state == SatelliteState.RUN
    assert call(tx, "stop").verb_type == OK
    wait_state(sat, SatelliteState.ORBIT)

    run_dir = tmp_path / "run1"
    lines = (run_dir / "FPGA_Data.dat").read_text().splitlines()
    assert len(lines) >= 3
    assert all(len(line.split(",")) == 7 and line.endswith(",-1") for line in lines)
    rows = (run_dir / "counters_run.csv").read_text().splitlines()
    header = rows[0].split(",")
    stalled = [r.split(",")[header.index("stalled")] for r in rows[1:]]
    assert "1" in stalled
    info = json.loads((run_dir / "run.json").read_text())
    assert info["windows"] == len(lines)
    assert info["stalled"] >= 1
    assert {"start_time", "stop_time", "configuration", "channel", "fc_mode"} <= set(info)


def test_empty_configuration_is_refused(setup):
    sat, tx, _, _, _ = setup
    call(tx, "initialize", {})
    wait_state(sat, SatelliteState.ERROR)
    assert "[EtrocCounters.<name>]" in sat.fsm.status


@pytest.mark.parametrize(
    "override",
    [
        {"kc705_registers": {"counter_duration": 0x0005}},
        {"settle_windows": 1},
        {"channel": 4},
        {"fc_mode_on_launch": "fast"},
        {"fc_mode_on_launch": "custom Start QInj"},
        {"socket_timeout": 0},
        {"connect_timeout": 0.0},
        {"power_mode": "hihg"},
    ],
)
def test_unsafe_configuration_is_refused(setup, override):
    sat, tx, _, _, config = setup
    call(tx, "initialize", {**config, **override})
    wait_state(sat, SatelliteState.ERROR)


def test_integer_timeouts_are_accepted(setup):
    sat, tx, _, _, config = setup
    call(tx, "initialize", {**config, "socket_timeout": 3, "connect_timeout": 2})
    wait_state(sat, SatelliteState.INIT)
    assert sat.socket_timeout == 3.0 and isinstance(sat.socket_timeout, float)


def test_unreachable_board_fails_launch_with_hint(setup):
    sat, tx, _, _, config = setup
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()
    call(tx, "initialize", {**config, "port": closed_port})
    wait_state(sat, SatelliteState.INIT)
    call(tx, "launch")
    wait_state(sat, SatelliteState.ERROR)
    assert "ONE TCP client" in sat.fsm.status
