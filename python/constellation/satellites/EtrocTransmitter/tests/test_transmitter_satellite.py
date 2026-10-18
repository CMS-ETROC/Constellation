"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

EtrocTransmitter reconfigure and Ctrl-C reentry, over loopback CSCP against the simulated KC705.
"""

import logging
import os
import signal
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

from ...EtrocCounters.tests.fakes import FC_QINJ, FakeKC705, interrupt_main_loop, run_main_loop, start_task_pump
from ...EtrocCommon.commands import QUIESCE_IO_TIMEOUT_S
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


def _satellite(monkeypatch, start_thread=True, socket_timeout=5.0, extra_config=None):
    # the firmware waits (2.1 s after a FIFO reset) are not needed against the simulation
    fast_time = types.SimpleNamespace(**{n: getattr(time, n) for n in ("strftime", "localtime", "time", "monotonic")})
    fast_time.sleep = lambda s: None
    monkeypatch.setattr(tx_module, "time", fast_time)
    fake = FakeKC705(window_s=0.05).start()
    sat = EtrocTransmitter(0, "Test", "txlifecycle", 0, 0, 0, [get_loopback_interface_name()])
    if start_thread:
        thread = threading.Thread(target=sat.run_satellite, daemon=True)
    else:
        # the main loop is run later on the main thread (run_main_loop); until then a pump runs the tasks
        thread = start_task_pump(sat)
    if start_thread:
        thread.start()
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 5000)
    sock.connect(f"tcp://127.0.0.1:{sat.cmd_port}")
    tx = CommandTransmitter("test", sock)
    config = {
        "hostname": "127.0.0.1",
        "port": fake.port,
        "socket_timeout": socket_timeout,
        "polarity": 0x4023,
        "fast_command_memo": "Start QInj L1A Triggerbit",
        "num_fifo_read": 16,
        "_data": {"disable_transmission": True},
        **(extra_config or {}),
    }
    try:
        assert call(tx, "initialize", config).verb_type == OK
        wait_state(sat, SatelliteState.INIT)
        assert call(tx, "launch").verb_type == OK
        wait_state(sat, SatelliteState.ORBIT)
        yield sat, tx, fake, thread
    finally:
        fake.release()
        if not getattr(sat, "_reentered", False) and thread.is_alive() and sat.fsm.state == SatelliteState.ORBIT:
            call(tx, "land")
            wait_state(sat, SatelliteState.INIT)
        if not start_thread:
            thread.stop_evt.set()
        sat.reentry()
        thread.join(5.0)
        sock.close()
        ctx.term()
        fake.stop()


@pytest.fixture
def setup(monkeypatch):
    yield from _satellite(monkeypatch)


@pytest.fixture
def main_thread_setup(monkeypatch):
    """No run_satellite thread: the test calls run_satellite on the main thread, where Ctrl-C lands."""
    yield from _satellite(monkeypatch, start_thread=False)


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
        ({"num_fifo_read": 0}, ["num_fifo_read must be 1-65536"]),
        ({"num_fifo_read": 70000}, ["num_fifo_read must be 1-65536"]),
        ({"active_channel": -1}, ["active_channel must be a 16-bit register value"]),
        ({"triggerbit_delay": 0x1FFFF}, ["triggerbit_delay must be a 16-bit register value"]),
        ({"_data": {"disable_transmission": False}}, ["framework sections (_data)", "re-initialize"]),
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


# --------------------------------------------------------------- round 3: Ctrl-C escapes, stalls, false success
def unknown_not_claimed(texts):
    joined = " | ".join(texts)
    assert "UNKNOWN" in joined, joined
    low = joined.lower()
    assert "daq stopped" not in low and "fast commands idle" not in low, joined


def start_run(sat, tx, fake, reads=3):
    assert call(tx, "start", "run1").verb_type == OK
    wait_state(sat, SatelliteState.RUN)
    deadline = time.monotonic() + 5
    while fake.fifo_reads < reads:
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_second_ctrl_c_while_stopping_the_run_thread_is_ignored(main_thread_setup, caplog):
    sat, tx, fake, pump = main_thread_setup
    start_run(sat, tx, fake)
    stops = fake.pulses(STOP_DAQ_BIT)
    real_stop = sat._stop_state_thread
    calls = []

    def second_ctrl_c(timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real_stop(timeout)

    sat._stop_state_thread = second_ctrl_c
    interrupt_main_loop(sat)
    assert not run_main_loop(sat, pump), "KeyboardInterrupt escaped the shutdown"
    assert fake.pulses(STOP_DAQ_BIT) == stops + 1
    assert not fake.daq_running and not qinj_playing(fake)
    assert sat.connection_socket is None
    assert_run_thread_gone(sat)
    assert any("Ctrl-C ignored: shutdown in progress" in r.getMessage() for r in caplog.records)


def test_ctrl_c_in_the_main_loop_idle_sleep_still_runs_reentry(main_thread_setup, monkeypatch):
    sat, tx, fake, pump = main_thread_setup
    start_run(sat, tx, fake)
    from constellation.core import satellite as core_satellite

    raised = []

    def sleep(s):
        if not raised:
            raised.append(s)
            raise KeyboardInterrupt
        time.sleep(s)

    core_time = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith("_")})
    core_time.sleep = sleep
    monkeypatch.setattr(core_satellite, "time", core_time)
    stops = fake.pulses(STOP_DAQ_BIT)
    assert not run_main_loop(sat, pump), "KeyboardInterrupt escaped run_satellite"
    assert raised
    assert fake.pulses(STOP_DAQ_BIT) == stops + 1 and not fake.daq_running
    assert sat.connection_socket is None
    assert_run_thread_gone(sat)


def test_ctrl_c_with_a_stalled_board_reports_unknown_and_spares_the_shared_socket(setup, caplog):
    sat, tx, fake, thread = setup
    sat.REENTRY_JOIN_TIMEOUT_S = 1.0
    start_run(sat, tx, fake)
    fake.stall()  # the next FIFO read gets no answer, nor does anything after it
    deadline = time.monotonic() + 5
    while not fake.stalled:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    caplog.clear()
    t0 = time.monotonic()
    ctrl_c(sat, thread)
    elapsed = time.monotonic() - t0
    unknown_not_claimed([r.getMessage() for r in caplog.records])
    # nothing read on the link the run thread was blocked on
    assert fake.held_reads(1) == []
    bound = sat.REENTRY_JOIN_TIMEOUT_S + sat.RELEASE_JOIN_TIMEOUT_S + 3 * QUIESCE_IO_TIMEOUT_S
    assert elapsed < bound, f"reentry took {elapsed:.1f} s (bound {bound:.1f} s)"
    # the stop pulse still went out, on a fresh connection
    assert any(w == 0x000B0400 for _, w in fake.held)
    assert_run_thread_gone(sat)


def test_reconfigure_failure_on_a_stalled_board_reports_unknown_quickly(monkeypatch):
    gen = _satellite(monkeypatch, socket_timeout=6.0)
    sat, tx, fake, _ = next(gen)
    try:
        texts, durations = [], []
        real_fail, real_quiesce = sat.fail_gracefully, sat._quiesce

        def fail_gracefully():
            texts.append(real_fail())
            return texts[-1]

        def timed_quiesce(reason):
            t0 = time.monotonic()
            try:
                return real_quiesce(reason)
            finally:
                durations.append(time.monotonic() - t0)

        sat.fail_gracefully, sat._quiesce = fail_gracefully, timed_quiesce
        fake.stall(on_fifo_read=False)
        assert call(tx, "reconfigure", {"polarity": 0x4123}).verb_type == OK
        wait_state(sat, SatelliteState.ERROR, timeout=20.0)
        # the FSM enters ERROR before it runs fail_gracefully
        deadline = time.monotonic() + 10
        while not texts:
            assert time.monotonic() < deadline, "fail_gracefully did not run"
            time.sleep(0.05)
        unknown_not_claimed(texts)
        assert durations and max(durations) < 2 * QUIESCE_IO_TIMEOUT_S + 1.0, durations
    finally:
        next(gen, None)


class _SilentSocket:
    """A KC705 link that takes every write and never answers a read."""

    def __init__(self):
        self.words = []

    def sendall(self, data):
        self.words += [int.from_bytes(data[i:i + 4], "big") for i in range(0, len(data), 4)]

    def recv(self, n):
        import socket

        raise socket.timeout("timed out")

    def settimeout(self, t):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        pass


def test_stop_pulse_goes_out_before_any_read_on_a_silent_board():
    sat = EtrocTransmitter.__new__(EtrocTransmitter)
    sat.log = logging.getLogger("probe")
    link = _SilentSocket()
    sat.connection_socket, sat.timestamp, sat._daq_started = link, 0, True
    text = sat._quiesce("probe")
    assert 0x000B0400 in link.words, [hex(w) for w in link.words]
    unknown_not_claimed([text])
    assert sat.connection_socket is None


def test_reconfigure_readback_mismatch_fails_the_transition(setup):
    sat, tx, fake, _ = setup
    handle = fake._handle

    def drop_reg14_writes(word):
        if (word >> 16) == 0x20 + 14:
            return None  # the board does not take the polarity write
        return handle(word)

    fake._handle = drop_reg14_writes
    assert call(tx, "reconfigure", {"polarity": 0x4123}).verb_type == OK
    wait_state(sat, SatelliteState.ERROR)
    assert "did not take the value" in sat.fsm.status, sat.fsm.status


# --------------------------------------------------------------- round 4: fences, single stream, real SIGINT
class _FillerSocket(_SilentSocket):
    """A link whose every reply is FIFO filler: the late data of a FIFO read left pending on the stream."""

    def recv(self, n):
        return bytes.fromhex("55600001") * (n // 4) + b"\x00" * (n % 4)


def test_fence_rejects_stray_fifo_data():
    sat = EtrocTransmitter.__new__(EtrocTransmitter)
    sat.log = logging.getLogger("probe")
    link = _FillerSocket()
    sat.connection_socket, sat.timestamp, sat._daq_started = link, 0x10, True
    text = sat._quiesce("probe")
    assert 0x000B0400 in link.words
    unknown_not_claimed([text])


def test_ctrl_c_on_a_single_stream_stall_rejects_the_late_fifo_data(setup, caplog):
    sat, tx, fake, thread = setup
    sat.REENTRY_JOIN_TIMEOUT_S = 1.0
    start_run(sat, tx, fake)
    # the FIFO read's data arrives late, on the fresh connection, ahead of the stop sequence's replies
    fake.stall(single_stream=True)
    deadline = time.monotonic() + 5
    while not fake.stalled:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    caplog.clear()
    ctrl_c(sat, thread)
    unknown_not_claimed([r.getMessage() for r in caplog.records])
    assert fake.held_reads(1) == []
    assert fake.pulses(STOP_DAQ_BIT) >= 1 and not fake.daq_running
    assert_run_thread_gone(sat)


def test_ctrl_c_on_a_healthy_board_reports_the_verified_state(setup, caplog):
    sat, tx, fake, thread = setup
    start_run(sat, tx, fake)
    caplog.clear()
    ctrl_c(sat, thread)
    shutdown = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Shutdown (reentry)")]
    assert shutdown and "DAQ stopped (session_DAQ_on = 0)" in shutdown[0] and "fast commands idle" in shutdown[0]
    assert "UNKNOWN" not in shutdown[0]


@pytest.mark.parametrize(
    "stored, change, words",
    [
        # a typo key: the controller would refuse it as unused inside the transition (ERROR)
        ({}, {"triggerbit_dleay": 0x1DE7}, ["does not read ['triggerbit_dleay']"]),
        # stored at initialize but not a DEFAULT_CONFIG key: passed the pre-check, then ERROR ("not used")
        ({"allow_default_config": True}, {"allow_default_config": False}, ["'allow_default_config' cannot change"]),
    ],
)
def test_reconfigure_of_a_key_the_satellite_does_not_apply_is_refused(monkeypatch, stored, change, words):
    gen = _satellite(monkeypatch, extra_config=stored)
    sat, tx, fake, _ = next(gen)
    try:
        rep = call(tx, "reconfigure", change)
        assert rep.verb_type != OK
        assert all(w in rep.verb_msg for w in words), rep.verb_msg
        time.sleep(0.3)
        assert sat.fsm.state == SatelliteState.ORBIT
    finally:
        next(gen, None)


def test_fail_gracefully_from_another_thread_releases_the_blocked_run_thread(monkeypatch):
    gen = _satellite(monkeypatch, socket_timeout=30.0)
    sat, tx, fake, _ = next(gen)
    try:
        start_run(sat, tx, fake)
        fake.stall()
        deadline = time.monotonic() + 5
        while not fake.stalled:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        text = sat.fail_gracefully()  # the command thread's view: the run thread is blocked in recv
        unknown_not_claimed([text])
        # no register traffic on the stream the run thread was blocked on
        assert fake.held_reads(1) == []
        sat._state_thread_fut.result(timeout=5.0)
    finally:
        next(gen, None)


def _slow_step(sat, name, before):
    """Wrap sat.<name> so that `before()` runs and the step then takes 0.6 s (Ctrl-C lands inside it)."""
    real = getattr(sat, name)

    def slow(*args, **kwargs):
        before()
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            time.sleep(0.02)
        return real(*args, **kwargs)

    setattr(sat, name, slow)


def test_real_sigint_during_the_stop_sequence_is_ignored(main_thread_setup):
    sat, tx, fake, pump = main_thread_setup
    start_run(sat, tx, fake)
    _slow_step(sat, "_stop_daq", lambda: threading.Timer(0.1, os.kill, (os.getpid(), signal.SIGINT)).start())
    interrupt_main_loop(sat)
    assert not run_main_loop(sat, pump)
    assert not fake.daq_running and not qinj_playing(fake)
    assert sat.connection_socket is None


def test_ctrl_c_before_the_sigint_handler_is_installed_still_shuts_down(main_thread_setup, monkeypatch):
    sat, tx, fake, pump = main_thread_setup
    start_run(sat, tx, fake)
    from ...EtrocCommon import lifecycle

    calls = []

    def signal_once(*args):
        calls.append(args)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return signal.signal(*args)

    fake_signal = types.SimpleNamespace(**{n: getattr(signal, n) for n in dir(signal) if not n.startswith("__")})
    fake_signal.signal = signal_once
    monkeypatch.setattr(lifecycle, "signal", fake_signal)
    interrupt_main_loop(sat)
    assert not run_main_loop(sat, pump)
    assert not fake.daq_running and not qinj_playing(fake)
    assert sat.connection_socket is None
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_third_ctrl_c_during_the_shutdown_restores_the_default_action(main_thread_setup, caplog):
    sat, tx, fake, pump = main_thread_setup
    start_run(sat, tx, fake)
    seen = {}

    def press_three_times():
        def presses():
            for _ in range(3):
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(0.15)

        threading.Thread(target=presses, daemon=True).start()

    real = sat._stop_state_thread

    def stop_with_presses(timeout):
        press_three_times()
        deadline = time.monotonic() + 3
        while sat.__dict__.get("_ctrl_c_presses", 0) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        seen["action"] = signal.getsignal(signal.SIGINT)
        return real(timeout)

    sat._stop_state_thread = stop_with_presses
    interrupt_main_loop(sat)
    assert not run_main_loop(sat, pump)
    assert seen["action"] is signal.SIG_DFL
    ignored = [r for r in caplog.records if "press 3 times to abort the shutdown" in r.getMessage()]
    # Constellation logs error() at its CRITICAL level
    assert [r.levelno >= logging.ERROR for r in ignored] == [False, False, True]
    assert "waiting up to" in ignored[0].getMessage()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert not fake.daq_running and sat.connection_socket is None
