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
"""

import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any


def run_step(log: Any, what: str, fn: Callable[..., Any], *args: Any) -> bool:
    """Run one shutdown step; log and swallow its exception so the next step still runs."""
    try:
        fn(*args)
    except Exception as e:
        log.warning(f"{what} failed: {type(e).__name__}: {e}")
        return False
    return True


class EtrocLifecycle:
    """Mixin (before the Satellite base): reentry that stops the run thread and quiesces the hardware.

    The satellite provides `_quiesce(reason) -> str` (idempotent, each step in its own
    try/except) and may override `_stop_transport_for_reentry()` for CDTP threads.
    """

    # do_run loops poll stop_requested() at least this often
    REENTRY_JOIN_TIMEOUT_S = 5.0

    def _quiesce(self, reason: str) -> str:
        raise NotImplementedError

    def _quiesce_locked(self, reason: str) -> str:
        """`_quiesce` under a lock: reentry and the ERROR path can run it from different threads."""
        lock = self.__dict__.setdefault("_quiesce_lock", threading.Lock())
        with lock:
            return self._quiesce(reason)

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
        try:
            fut.result(timeout=timeout)
        except FutureTimeoutError:
            self.log.error(f"Run thread did not stop within {timeout} s; releasing the hardware anyway")
            return False
        except Exception as e:
            self.log.warning(f"Run thread ended with {type(e).__name__}: {e}")
        return True

    def _stop_transport_for_reentry(self) -> None:
        """Stop CDTP sender/receiver threads (non-daemon) before the hardware is released."""

    def reentry(self) -> None:
        log = self.log
        log.warning("Reentry: stopping the run thread and releasing the hardware")
        run_step(log, "Stopping the run thread", self._stop_state_thread, self.REENTRY_JOIN_TIMEOUT_S)
        run_step(log, "Stopping the data transport", self._stop_transport_for_reentry)
        run_step(log, "Releasing the hardware", self._quiesce_locked, "reentry")
        executor = getattr(self, "_state_thread_exc", None)
        if executor is not None:
            # lets the idle worker exit, so interpreter shutdown does not wait on it
            run_step(log, "Shutting down the state-thread executor", lambda: executor.shutdown(wait=False, cancel_futures=True))
        super().reentry()  # type: ignore[misc]
