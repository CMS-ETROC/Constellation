"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

The controller-side preflight (check_toml) and the satellites' guard against a
configuration merged from [_default] alone.
"""

from pathlib import Path

import pytest

from constellation.core.configuration import Configuration

from ...EtrocCounters.EtrocCounters import EtrocCounters
from ...EtrocReceiver.EtrocReceiver import EtrocReceiver
from ...EtrocTransmitter.EtrocTransmitter import EtrocTransmitter
from ...EtrocWaveform.EtrocWaveform import EtrocWaveform
from .. import check_toml
from ..config import load_typed_config
from ..convert_toml import convert
from ..satellite_types import CONFIG_KEYS, REQUIRED_KEYS

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


# the converted file without the 0.x key the 1.x receiver does not implement
BEAM_TOML_1X = convert(BEAM_TOML_0X).replace("compressed_binary = 1\n", "")
CASES = Path(__file__).parent / "toml_cases"


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_converted_sidecar_file_passes_once_compressed_binary_is_dropped():
    problems = check_toml.check(convert(BEAM_TOML_0X)).errors
    assert len(problems) == 1 and "`compressed_binary` is not supported in 1.x" in problems[0], problems
    report = check_toml.check(BEAM_TOML_1X)
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
    text = BEAM_TOML_1X.replace("[EtrocTransmitter.One]", "[EtrocTransmiter.One]")
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
        '[_default]\nfc_delays = 1\ntriggerbit_delay = 1\n\n'
        '[EtrocTransmitter._default]\nfc_delays = 2\ncounter_duration = 2\n\n'
        '[EtrocTransmitter.One]\ncounter_duration = 3\nfast_command_memo = "Start Triggerbit"\n'
        'polarity = 1\nactive_channel = 1\ntimestamp = 1\n'
    )
    report = check_toml.check(text)
    assert report.ok, report.errors
    (_, merged, _), = report.sections
    assert (merged["triggerbit_delay"], merged["fc_delays"], merged["counter_duration"]) == (1, 2, 3)


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
    text = BEAM_TOML_1X.replace("[EtrocReceiver.One]", "[etrocreceiver.One]") + "\n[Sputnik.One]\n"
    report = check_toml.check(text, allow_types=("Sputnik",))
    assert report.ok, report.errors
    assert any("only in case" in n for n in report.notes)


def test_main_exit_status(tmp_path, capsys):
    good = tmp_path / "good.toml"
    good.write_text(BEAM_TOML_1X)
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


# --------------------------------------------------------------- round 3: defaults, --satellites, unknown keys
def test_required_key_in_default_is_a_problem():
    report = check_toml.check((CASES / "e1_default_noreceiver.toml").read_text())
    assert any("[_default] holds `output_path`" in e for e in report.errors), report.errors
    text = '[EtrocTransmitter._default]\npolarity = 1\n\n[EtrocTransmitter.One]\nfast_command_memo = "Start Triggerbit"\n'
    assert any("[EtrocTransmitter._default] holds `polarity`" in e for e in check_toml.check(text).errors)


def test_expected_satellite_without_a_section_is_reported(tmp_path, capsys):
    path = CASES / "e2_misnamed.toml"
    report = check_toml.check(path.read_text(), satellites=("EtrocReceiver.One",))
    assert report.instances == ["EtrocTransmitter.One", "EtrocReceiver.Main"]
    assert any(
        "[EtrocReceiver.One] has no section" in e and "EtrocTransmitter.One, EtrocReceiver.Main" in e
        for e in report.errors
    ), report.errors
    assert check_toml.main([str(path), "--satellites", "EtrocTransmitter.One,EtrocReceiver.One"]) == 1
    out = capsys.readouterr().out
    assert "ETROC satellite sections found: EtrocTransmitter.One, EtrocReceiver.Main" in out


def test_typo_keys_get_the_likely_spelling():
    errors = check_toml.check((CASES / "e9_typo_key.toml").read_text()).errors
    assert any("unknown key `triggerbit_dleay`" in e and "did you mean `triggerbit_delay`" in e for e in errors), errors
    assert any("unknown key `prescale_factr`" in e and "did you mean `prescale_factor`" in e for e in errors), errors


def test_converted_team_file_fails_only_on_compressed_binary():
    errors = check_toml.check((CASES / "conv_beam.toml").read_text()).errors
    assert errors == [
        "[EtrocReceiver.One]: `compressed_binary` is not supported in 1.x "
        "(the 1.x receiver writes no compressed binary output; drop the key)"
    ]


def test_convert_keeps_keys_of_parent_tables_as_default_sections():
    converted = convert((CASES / "e16_old_typekeys.toml").read_text())
    assert "[EtrocTransmitter._default]" in converted and "[EtrocReceiver._default]" not in converted
    report = check_toml.check(converted)
    assert report.ok, report.errors
    merged = {name: m for name, m, _ in report.sections}
    assert merged["EtrocTransmitter.One"]["triggerbit_delay"] == 0x1DE6
    assert "[_default]\nhostname" in convert('[satellites]\nhostname = "192.168.2.3"\n')


@pytest.mark.parametrize(
    "header", ['[[satellites.ETROC2Classic]]', '["satellites".ETROC2Classic.One]', "[satellites . ETROC2Classic.One]"]
)
def test_convert_refuses_a_header_it_cannot_map(header, tmp_path, capsys):
    with pytest.raises(ValueError, match="cannot convert"):
        convert(f"{header}\npolarity = 1\n")
    path = tmp_path / "old.toml"
    path.write_text(f"{header}\npolarity = 1\n")
    from ..convert_toml import main as convert_main

    assert convert_main([str(path)]) == 1
    assert "cannot convert" in capsys.readouterr().err


def test_preflight_follows_the_initialize_guard():
    partial = '[EtrocTransmitter.One]\nfast_command_memo = "Start Triggerbit"\n'
    report = check_toml.check(partial)
    assert report.ok, report.errors
    assert any("not set; the satellite uses its defaults" in n for n in report.notes)
    deliberate = '[EtrocTransmitter.One]\nallow_default_config = true\nhostname = "192.168.2.3"\n'
    assert check_toml.check(deliberate).ok


@pytest.mark.parametrize(
    "text, words",
    [
        ('[EtrocTransmitter.One-A]\nfast_command_memo = "Start Triggerbit"\n', ["not a valid satellite name"]),
        ('[_default]\n_data = {bor_timeout = 5}\n[EtrocTransmitter.One]\nfast_command_memo = "Start Triggerbit"\n'
         '_data = 3\n', ["[EtrocTransmitter.One]: the controller cannot merge"]),
        ('[_default]\ncompressed_binary = 1\n[EtrocReceiver.One]\noutput_path = "/d"\n', ["not supported in 1.x"]),
    ],
)
def test_round3_reported_problems(text, words):
    report = check_toml.check(text)
    assert any(all(w in e for w in words) for e in report.errors), report.errors


def test_environment_references_are_checked_as_written():
    text = '[EtrocReceiver.One]\noutput_path = "$_{ETROC_PREFLIGHT_UNSET_VAR}/data"\ntranslate = 0\nskip_fillers = 1\n'
    report = check_toml.check(text)
    assert report.ok, report.errors
    assert any("ETROC_PREFLIGHT_UNSET_VAR" in n for n in report.notes)


@pytest.mark.parametrize("cls", [EtrocTransmitter, EtrocReceiver, EtrocWaveform, EtrocCounters])
def test_preflight_key_lists_match_the_satellites(cls):
    assert sorted(CONFIG_KEYS[cls.__name__]) == sorted(cls.DEFAULT_CONFIG)


def test_kc705_registers_is_a_counters_key_only():
    text = (
        '[EtrocTransmitter.One]\nfast_command_memo = "Start Triggerbit"\npolarity = 1\nactive_channel = 1\n'
        'timestamp = 1\n\n[EtrocTransmitter.One.kc705_registers]\ntriggerbit_delay = 1\n'
    )
    assert any("unknown key `kc705_registers`" in e for e in check_toml.check(text).errors)
    counters = '[EtrocCounters.C]\noutput_path = "/d"\nchannel = 0\nfc_mode_on_launch = "idle"\n\n[EtrocCounters.C.kc705_registers]\ntriggerbit_delay = 1\n'
    assert check_toml.check(counters).ok
