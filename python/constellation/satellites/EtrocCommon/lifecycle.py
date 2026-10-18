"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Orderly shutdown of the ETROC satellites on Ctrl-C (reentry) and on ERROR.

On Ctrl-C the satellite main loop (`Satellite.run_satellite`) breaks and calls `reentry()`.
The framework's reentry stops only the communication threads; the FSM state thread that runs
`do_run` is a non-daemon ThreadPoolExecutor worker whose stop event nobody sets, so the
process hangs at exit in `concurrent.futures` while the run loop keeps talking to the board.
`EtrocLifecycle.reentry` stops that thread first, then releases the hardware through the
satellite's `_quiesce()`, the same helper `fail_gracefully()` uses.

A second Ctrl-C must not cut the shutdown short. KeyboardInterrupt is a BaseException, so
`run_step` catches it explicitly; reentry ignores SIGINT while it runs (main thread only),
installing that handler before anything else; and `run_satellite` runs reentry whenever its
steps have not completed, also when a KeyboardInterrupt escapes the framework's loop or hits
reentry before the handler is in place. Each ignored Ctrl-C logs what the shutdown is waiting
for; the third restores the default SIGINT action, so a fourth ends the process and leaves the
hardware as it is (the operator's way out of a long bounded wait).

A run thread that does not stop within the bound is usually blocked in a read on the board's
single TCP stream. Its link is then closed (the shutdown wakes the blocked recv), the thread
gets a second, short wait, and the stop sequence goes out on a fresh connection. If the
thread still runs, the satellite skips all register traffic and reports the hardware state
as UNKNOWN instead of sharing the stream with it.
"""

import signal
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

CTRL_C_IGNORED = "Ctrl-C ignored: shutdown in progress"
# presses after which the default SIGINT action is restored (the next one ends the process)
CTRL_C_ABORT_PRESSES = 3


def run_step(log: Any, what: str, fn: Callable[..., Any], *args: Any) -> bool:
    """Run one shutdown step; log and swallow its exception (and Ctrl-C) so the next step still runs."""
    try:
        fn(*args)
    except KeyboardInterrupt:
        log.warning(f"{CTRL_C_IGNORED} ({what} interrupted; continuing with the next step)")
        return False
    except Exception as e:
        log.warning(f"{what} failed: {type(e).__name__}: {e}")
        return False
    return True


def shutdown_text(verified: list[str], answered: bool, failed: list[str], tail: list[str]) -> str:
    """Status of a shutdown: a hardware state is claimed only when a read after the writes answered."""
    parts = list(verified) if answered else ["DAQ/FC state UNKNOWN: board did not answer"]
    if failed:
        parts.append("failed: " + ", ".join(failed))
    return "; ".join(parts + tail)


def shutdown_is_clean(text: str) -> bool:
    return "UNKNOWN" not in text and "failed" not in text


class EtrocLifecycle:
    """Mixin (before the Satellite base): reentry that stops the run thread and quiesces the hardware.

    The satellite provides `_quiesce(reason) -> str` (idempotent, each step in its own
    try/except, the status built from what was verified) and may override
    `_stop_transport_for_reentry()` for CDTP threads and `_release_run_thread_io()` to close
    the link a blocked run thread is using.
    """

    # do_run loops poll stop_requested() at least this often
    REENTRY_JOIN_TIMEOUT_S = 5.0
    # second wait, after the link the run thread was blocked on has been closed
    RELEASE_JOIN_TIMEOUT_S = 3.0

    def _quiesce(self, reason: str) -> str:
        raise NotImplementedError

    def _quiesce_locked(self, reason: str) -> str:
        """`_quiesce` under a lock (reentry and the ERROR path can run it from different threads); logs the result."""
        lock = self.__dict__.setdefault("_quiesce_lock", threading.Lock())
        with lock:
            text = self._quiesce(reason)
        if shutdown_is_clean(text):
            self.log.info(f"Shutdown ({reason}): {text}")
        else:
            self.log.warning(f"Shutdown ({reason}): {text}")
        return text

    def _reentry_join_timeout(self) -> float:
        return self.REENTRY_JOIN_TIMEOUT_S

    def _release_run_thread_io(self) -> None:
        """Close the hardware link a blocked run thread is using; the shutdown then opens a fresh one."""

    def _on_run_thread(self) -> bool:
        return threading.current_thread() in getattr(getattr(self, "_state_thread_exc", None), "_threads", ())

    def _stop_state_thread(self, timeout: float) -> bool:
        """Set the FSM state-thread event (stop_requested) and wait for do_run to return.

        Returns False if the thread is still running after `timeout` seconds.
        """
        evt = getattr(self, "_state_thread_evt", None)
        fut = getattr(self, "_state_thread_fut", None)
        if evt is not None:
            evt.set()
        if fut is None or fut.done():
            return True
        self._log_wait(timeout, "the run thread to stop")
        try:
            fut.result(timeout=timeout)
        except FutureTimeoutError:
            self.log.error(f"Run thread did not stop within {timeout:.1f} s")
            return False
        except Exception as e:
            self.log.warning(f"Run thread ended with {type(e).__name__}: {e}")
        return True

    def _settle_run_thread(self, timeout: float) -> None:
        """Stop the run thread; if it hangs, close its link and wait once more. Sets `_run_thread_stuck`."""
        self._run_thread_stuck = False
        self._current_wait = f"waiting up to {timeout + self.RELEASE_JOIN_TIMEOUT_S:.1f} s for the run thread to stop"
        if self._on_run_thread():
            # the failure path of do_run itself: the run loop is this thread and no longer uses the link
            return
        result: dict[str, bool] = {}

        def stop(bound: float) -> None:
            result["stopped"] = self._stop_state_thread(bound)

        run_step(self.log, "Stopping the run thread", stop, timeout)
        if result.get("stopped", False):
            return
        run_step(self.log, "Closing the link the run thread is blocked on", self._release_run_thread_io)
        result.clear()
        run_step(self.log, "Waiting for the run thread after closing its link", stop, self.RELEASE_JOIN_TIMEOUT_S)
        if not result.get("stopped", False):
            self._run_thread_stuck = True
            self.log.critical("Run thread still running: the shutdown skips all register traffic")

    def _stop_transport_for_reentry(self) -> None:
        """Stop CDTP sender/receiver threads (non-daemon) before the hardware is released."""

    def _log_wait(self, bound: float, what: str, level: str = "warning") -> None:
        """Log a bounded wait before it starts; an ignored Ctrl-C names the wait in progress."""
        text = f"waiting up to {bound:.1f} s for {what}"
        self._current_wait = text
        getattr(self.log, level)(text[0].upper() + text[1:])

    def _ignore_sigint(self, signum: int, frame: Any) -> None:
        presses = self.__dict__.get("_ctrl_c_presses", 0) + 1
        self._ctrl_c_presses = presses
        wait = self.__dict__.get("_current_wait", "running the shutdown steps")
        text = (
            f"{CTRL_C_IGNORED}, {wait}; press {CTRL_C_ABORT_PRESSES} times to abort the shutdown with the "
            f"hardware as is ({presses}/{CTRL_C_ABORT_PRESSES})"
        )
        if presses < CTRL_C_ABORT_PRESSES:
            self.log.warning(text)
            return
        self.log.error(text + ": the next Ctrl-C ends the process; the KC705/I2C state is then unknown")
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    def run_satellite(self) -> None:
        """The framework loop; reentry runs whenever its steps have not completed (a KeyboardInterrupt that
        escapes the loop, e.g. during its idle sleep, or that hits reentry before its SIGINT handler)."""
        for _ in range(3):
            try:
                if not self.__dict__.get("_reentered", False):
                    super().run_satellite()  # type: ignore[misc]
                elif not self.__dict__.get("_reentry_done", False):
                    self.reentry()
            except KeyboardInterrupt:
                pass
            if self.__dict__.get("_reentry_done", False):
                return
            self._reentered = True  # the framework loop is left: from here on only reentry runs
        self.log.critical("Shutdown did not complete after repeated Ctrl-C")

    def reentry(self) -> None:
        previous = None
        if threading.current_thread() is threading.main_thread():
            # the handler first: a Ctrl-C from here on is logged, not raised
            try:
                previous = signal.signal(signal.SIGINT, self._ignore_sigint)
            except (ValueError, OSError) as e:
                self.log.warning(f"Could not ignore Ctrl-C during the shutdown: {e}")
        self._reentered = True
        self._ctrl_c_presses = 0
        try:
            self._reentry_steps()
            self._reentry_done = True
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def _reentry_steps(self) -> None:
        log = self.log
        log.warning("Reentry: stopping the run thread and releasing the hardware")
        run_step(log, "Stopping the run thread", self._settle_run_thread, self._reentry_join_timeout())
        run_step(log, "Stopping the data transport", self._stop_transport_for_reentry)
        run_step(log, "Releasing the hardware", self._quiesce_locked, "reentry")
        executor = getattr(self, "_state_thread_exc", None)
        if executor is not None:
            # lets the idle worker exit, so interpreter shutdown does not wait on it
            run_step(log, "Shutting down the state-thread executor", lambda: executor.shutdown(wait=False, cancel_futures=True))
        run_step(log, "Stopping the communication threads", super().reentry)  # type: ignore[misc]
