"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

R-KC7-SW-20: the EtrocWaveform FC memo arithmetic, fixed and legacy (no hardware).
"""

import pytest

from ..EtrocWaveform import ORBIT_LAST_BX, fc_schedule


@pytest.mark.parametrize("n", [1, 10, 100])
def test_fixed_grammar_injects_n_times_and_legacy_n_plus_one(n):
    memo = f"Start QInj L1A L1ARange repeatedQInj={n} Triggerbit"
    fixed, legacy = fc_schedule(memo, "fixed"), fc_schedule(memo, "legacy")
    assert len(fixed["played_qinj_bx"]) == n and len(fixed["played_l1a_bx"]) == n
    assert len(legacy["played_qinj_bx"]) == n + 1 and len(legacy["played_l1a_bx"]) == n + 1
    # every L1A pairs with its injection 504 BX earlier (the pixel L1Adelay 501 + 3)
    assert [l - q for q, l in zip(fixed["qinj_bx"], fixed["l1a_bx"])] == [504] * n


def test_uniform_above_187_collapses_in_legacy_and_is_refused_in_fixed():
    memo = "Start QInj L1A L1ARange repeatedQInj=200 uniform Triggerbit"
    legacy = fc_schedule(memo, "legacy")
    assert len(legacy["qinj_bx"]) == 201 and legacy["played_qinj_bx"] == [0x005]
    with pytest.raises(ValueError, match="at most 187"):
        fc_schedule(memo, "fixed")
    assert len(fc_schedule(memo.replace("200", "187"), "fixed")["played_qinj_bx"]) == 187


def test_fixed_refuses_a_schedule_beyond_the_orbit_legacy_loses_the_entries():
    memo = "Start QInj L1A L1ARange repeatedQInj=250 Triggerbit"
    with pytest.raises(ValueError, match="beyond the orbit"):
        fc_schedule(memo, "fixed")
    legacy = fc_schedule(memo, "legacy")
    assert max(legacy["qinj_bx"]) > ORBIT_LAST_BX
    assert len(legacy["played_l1a_bx"]) < 251
    assert all(bx <= ORBIT_LAST_BX for bx in legacy["played_qinj_bx"] + legacy["played_l1a_bx"])


@pytest.mark.parametrize("memo", [
    "Start QInj L1A L1ARange repeatedQInj=191 Triggerbit",
    "Start QInj L1A L1ARange repeatedQInj=37 uniform Triggerbit",
    "Start QInj L1A Triggerbit BCR",
])
def test_fixed_injections_and_l1as_never_share_an_address(memo):
    s = fc_schedule(memo, "fixed")
    assert not set(s["qinj_bx"]) & set(s["l1a_bx"])
    assert all(bx <= ORBIT_LAST_BX for bx in s["qinj_bx"] + s["l1a_bx"])


def test_bare_repeat_word_and_plain_memo():
    assert len(fc_schedule("Start QInj L1A Triggerbit repeatedQInj", "fixed")["qinj_bx"]) == 1
    assert len(fc_schedule("Start QInj L1A Triggerbit repeatedQInj", "legacy")["qinj_bx"]) == 2
    plain = fc_schedule("Start QInj L1A Triggerbit", "fixed")
    assert plain == {**plain, "qinj_bx": [0x005], "l1a_bx": [0x1FD]}
    assert fc_schedule("Start Triggerbit", "fixed")["played_qinj_bx"] == []


@pytest.mark.parametrize("memo,grammar", [
    ("Start QInj repeatedQInj=0", "fixed"), ("Start QInj Triggr", "fixed"), ("Start QInj", "newest"),
])
def test_bad_memos_and_grammars_are_refused(memo, grammar):
    with pytest.raises(ValueError):
        fc_schedule(memo, grammar)
