"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

Importing the I2C wrapper must not take Constellation's TRACE level or ConstellationLogger.trace away,
not even for the duration of the i2c_gui2 import: other threads (a controller's heartbeat checker, a
satellite's heartbeat sender) log at TRACE meanwhile.
"""

import importlib
import importlib.util
import logging
import subprocess
import sys
import textwrap

import pytest

from constellation.core.logging import ConstellationLogger

WRAPPER = "constellation.satellites.EtrocCommon.new_wrapper_i2cGui2"

# Same shape as i2c_gui2 0.1.14: __init__ imports addLoggingLevel from .functions and registers
# TRACE (8) and DETAILED_TRACE (5) with it; addLoggingLevel raises if a name already exists.
FAKE_FUNCTIONS = '''
import logging


def addLoggingLevel(levelName, levelNum, methodName=None):
    if not methodName:
        methodName = levelName.lower()
    if hasattr(logging, levelName):
        raise AttributeError('{} already defined in logging module'.format(levelName))
    if hasattr(logging, methodName):
        raise AttributeError('{} already defined in logging module'.format(methodName))
    if hasattr(logging.getLoggerClass(), methodName):
        raise AttributeError('{} already defined in logger class'.format(methodName))

    def logForLevel(self, message, *args, **kwargs):
        if self.isEnabledFor(levelNum):
            self._log(levelNum, message, args, **kwargs)

    def logToRoot(message, *args, **kwargs):
        logging.log(levelNum, message, *args, **kwargs)

    logging.addLevelName(levelNum, levelName)
    setattr(logging, levelName, levelNum)
    setattr(logging.getLoggerClass(), methodName, logForLevel)
    setattr(logging, methodName, logToRoot)
'''

FAKE_INIT = '''
import logging

from constellation.core.logging import ConstellationLogger

from .functions import addLoggingLevel

# what a concurrent thread would see while the package is being imported
OBSERVED = {"class_trace": "trace" in vars(ConstellationLogger), "TRACE": getattr(logging, "TRACE", None)}

addLoggingLevel('TRACE', 8)
addLoggingLevel('DETAILED_TRACE', 5)


class ETROC2_Chip:  # names the wrapper uses at import (annotations)
    pass


class USB_ISS_Helper:
    pass
'''


@pytest.fixture
def fresh_import(tmp_path, monkeypatch):
    """A fake i2c_gui2 on sys.path, the wrapper and i2c_gui2 unimported, logging state restored after."""
    pkg = tmp_path / "i2c_gui2"
    pkg.mkdir()
    (pkg / "functions.py").write_text(FAKE_FUNCTIONS)
    (pkg / "__init__.py").write_text(FAKE_INIT)
    monkeypatch.syspath_prepend(str(tmp_path))
    names = [n for n in sys.modules if n == WRAPPER or n == "i2c_gui2" or n.startswith("i2c_gui2.")]
    saved = {n: sys.modules.pop(n) for n in names}
    monkeypatch.setattr(logging, "_levelToName", dict(logging._levelToName))
    monkeypatch.setattr(logging, "_nameToLevel", dict(logging._nameToLevel))
    monkeypatch.setattr(logging, "_loggerClass", ConstellationLogger)
    monkeypatch.setattr(logging, "TRACE", logging.DEBUG - 5, raising=False)
    for owner in (logging, logging.Logger, ConstellationLogger):
        for attr in ("DETAILED_TRACE", "detailed_trace"):
            monkeypatch.delattr(owner, attr, raising=False)
    importlib.invalidate_caches()
    yield
    for n in [n for n in sys.modules if n == WRAPPER or n == "i2c_gui2" or n.startswith("i2c_gui2.")]:
        del sys.modules[n]
    sys.modules.update(saved)


def test_import_keeps_trace_while_i2c_gui2_loads(fresh_import):
    original_trace = vars(ConstellationLogger)["trace"]
    importlib.import_module(WRAPPER)
    fake = sys.modules["i2c_gui2"]
    assert fake.__file__.startswith(str(sys.path[0]))  # the fake, not an installed i2c_gui2
    assert fake.OBSERVED == {"class_trace": True, "TRACE": 5}
    assert vars(ConstellationLogger)["trace"] is original_trace
    assert logging.TRACE == 5
    assert logging.getLevelName(5) == "TRACE"
    assert logging.getLevelName("TRACE") == 5
    log = logging.getLogger("wrapper-import-test")
    assert isinstance(log, ConstellationLogger)
    log.trace("constellation trace still works")
    log.detailed_trace("i2c_gui2's level still works")


@pytest.mark.skipif(importlib.util.find_spec("i2c_gui2") is None, reason="i2c_gui2 not installed")
def test_trace_from_another_thread_survives_the_real_import():
    code = textwrap.dedent(
        f"""
        import logging, sys, threading
        from constellation.core.logging import ConstellationLogger
        logging.setLoggerClass(ConstellationLogger)
        log = logging.getLogger("probe")
        errors, stop = [], threading.Event()
        def hammer():
            while not stop.is_set():
                try:
                    log.trace("x")
                except Exception as e:
                    errors.append(repr(e))
                    return
        t = threading.Thread(target=hammer)
        t.start()
        import {WRAPPER}
        stop.set()
        t.join()
        print(errors)
        sys.exit(1 if errors else 0)
        """
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
