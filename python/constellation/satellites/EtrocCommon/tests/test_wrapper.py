"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

i2c_connection logging and release, with the USB-ISS helper replaced by a stand-in.
"""

import logging

import pytest

from .. import new_wrapper_i2cGui2 as wrapper


class FakeIss:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeHelper:
    def __init__(self, port, clock, dummy_connect=False):
        self._iss = FakeIss()


@pytest.fixture(autouse=True)
def no_usb(monkeypatch):
    monkeypatch.setattr(wrapper.i2c_gui2, "USB_ISS_Helper", FakeHelper)


def make(**kwargs):
    return wrapper.i2c_connection("/dev/fake", [0x60], [0x40], ["ET2p03"], **kwargs)


def test_close_releases_the_adapter():
    conn = make()
    iss = conn.conn._iss
    conn.close()
    assert iss.closed
    assert conn.conn is None
    conn.close()  # a second close is harmless


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_records_reach_the_satellite_logger_once():
    parent = logging.getLogger("ETROCWAVEFORM_TEST")
    parent.propagate = False
    collect = _Collect()
    parent.addHandler(collect)
    try:
        conn = make(logger=parent)
        conn.logger.warning("from the wrapper")
        assert [r.getMessage() for r in collect.records] == ["from the wrapper"]
        assert conn.logger.handlers == []
    finally:
        parent.removeHandler(collect)
