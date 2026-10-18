"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

The controller-side preflight (check_toml) and the satellites' guard against a
configuration merged from [_default] alone.
"""

import pytest

from constellation.core.configuration import Configuration

from ...EtrocCounters.EtrocCounters import EtrocCounters
from ...EtrocReceiver.EtrocReceiver import EtrocReceiver
from ...EtrocTransmitter.EtrocTransmitter import EtrocTransmitter
from ...EtrocWaveform.EtrocWaveform import EtrocWaveform
from .. import check_toml
from ..config import load_typed_config
from ..convert_toml import convert
from ..satellite_types import REQUIRED_KEYS

# sidecar/etroc3/constellation_tomls/beam.toml (DESY May 2025), as the team wrote it for 0.x
BEAM_TOML_0X = """\
[satellites.ETROC2Classic]

[satellites.ETROC2Classic.One]
fast_command_memo = "Start Triggerbit"
polarity = 0x4123
active_channel = 0x1
timestamp = 0x10
fc_delays = 0b0000000010101010

triggerbit_delay = 0x1de6

[satellites.ETROC2Receiver]

[satellites.ETROC2Receiver.One]
output_path = "/home/daq/ETROC2/ETROC-Data/DESY_May2025/beam"
translate = 0
compressed_binary = 1
skip_fillers = 1
"""


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_converted_sidecar_file_passes():
    report = check_toml.check(convert(BEAM_TOML_0X))
    assert report.ok, report.errors
    assert report.notes == []
    sections = {name: (merged, missing) for name, merged, missing in report.sections}
    assert set(sections) == {"EtrocTransmitter.One", "EtrocReceiver.One"}
    assert sections["EtrocTransmitter.One"][0]["polarity"] == 0x4123
    assert all(missing == [] for _, missing in sections.values())


def test_old_grammar_is_reported_with_the_new_header():
    report = check_toml.check(BEAM_TOML_0X)
    assert not report.ok
    assert any("[satellites.ETROC2Classic.One] is the 0.x grammar" in e and "[EtrocTransmitter.One]" in e
               for e in report.errors)
    assert any("[EtrocReceiver.One]" in e for e in report.errors)


def test_misspelled_type_fails_with_the_suggestion():
    text = convert(BEAM_TOML_0X).replace("[EtrocTransmitter.One]", "[EtrocTransmiter.One]")
    report = check_toml.check(text)
    assert not report.ok
    assert any("[EtrocTransmiter.One] matches no satellite type; did you mean [EtrocTransmitter.One]?" in e
               for e in report.errors)


def test_default_only_merge_reports_the_missing_required_keys():
    text = '[_default]\nhostname = "192.168.2.3"\n\n[EtrocTransmitter.One]\n'
    report = check_toml.check(text)
    assert not report.ok
    (name, merged, missing), = report.sections
    assert name == "EtrocTransmitter.One" and merged == {"hostname": "192.168.2.3"}
    assert missing == list(REQUIRED_KEYS["EtrocTransmitter"])


def test_type_default_and_global_default_merge_in_documented_order():
    text = (
        '[_default]\npolarity = 1\ntimestamp = 1\n\n'
        '[EtrocTransmitter._default]\npolarity = 2\nactive_channel = 2\n\n'
        '[EtrocTransmitter.One]\nactive_channel = 3\nfast_command_memo = "Start Triggerbit"\n'
    )
    report = check_toml.check(text)
    assert report.ok, report.errors
    (_, merged, _), = report.sections
    assert (merged["timestamp"], merged["polarity"], merged["active_channel"]) == (1, 2, 3)


@pytest.mark.parametrize(
    "text, words",
    [
        ('[ETROC2Classic.One]\npolarity = 1\n', ["0.x type name", "[EtrocTransmitter.One]"]),
        ('[Sputnik.One]\ninterval = 1\n', ["unknown top-level table [Sputnik]", "--allow-type Sputnik"]),
        ('[_autonomy]\nrole = "ESSENTIAL"\n', ["[_default._autonomy]"]),
        ('[EtrocTransmitter]\npolarity = 1\n', ["[EtrocTransmitter._default]"]),
        ('[EtrocTransmitter.One\n', ["not valid TOML"]),
    ],
)
def test_reported_problems(text, words):
    report = check_toml.check(text)
    assert not report.ok
    assert any(all(w in e for w in words) for e in report.errors), report.errors


def test_case_only_difference_is_a_note_and_other_types_can_be_allowed():
    text = convert(BEAM_TOML_0X).replace("[EtrocReceiver.One]", "[etrocreceiver.One]") + "\n[Sputnik.One]\n"
    report = check_toml.check(text, allow_types=("Sputnik",))
    assert report.ok, report.errors
    assert any("only in case" in n for n in report.notes)


def test_main_exit_status(tmp_path, capsys):
    good = tmp_path / "good.toml"
    good.write_text(convert(BEAM_TOML_0X))
    bad = tmp_path / "bad.toml"
    bad.write_text(BEAM_TOML_0X)
    assert check_toml.main([str(good)]) == 0
    assert "OK" in capsys.readouterr().out
    assert check_toml.main([str(bad)]) == 1
    assert "Problems (2)" in capsys.readouterr().out


@pytest.mark.parametrize("cls", [EtrocTransmitter, EtrocReceiver, EtrocWaveform, EtrocCounters])
def test_satellite_refuses_a_default_only_configuration(cls):
    sat = cls.__new__(cls)
    sat.name = f"{cls.__name__}.One"
    sat.log = _Log()
    default_only = Configuration({"hostname": "192.168.2.3", "_autonomy": {"role": "ESSENTIAL"}})
    with pytest.raises(RuntimeError, match=rf"no \[{cls.__name__}\.One\] section reached this satellite \(none of"):
        load_typed_config(sat, default_only, cls.DEFAULT_CONFIG)
    # one required key is enough; allow_default_config bypasses the check
    key = REQUIRED_KEYS[cls.__name__][0]
    load_typed_config(sat, Configuration({key: cls.DEFAULT_CONFIG[key]}), cls.DEFAULT_CONFIG)
    load_typed_config(sat, Configuration({"hostname": "h", "allow_default_config": True}), cls.DEFAULT_CONFIG)
