"""Tests for scout/seasons.py: every rule in the RULES table, the ambiguous 'Winter', and student housing."""
from datetime import date

import pytest

from scout import seasons
from scout.seasons import interpret, to_deadline

# (phrase, expected deadline, the rule that must match). Together these cover every rule in RULES.
CASES = [
    ("Opening Fall 2026", date(2026, 12, 31), "season_year"),
    ("Opening Summer 2026", date(2026, 9, 21), "season_year"),
    ("Opening Spring 2026", date(2026, 6, 20), "season_year"),
    ("Opening Winter 2026", date(2027, 3, 20), "season_year"),             # ambiguous: see its own test
    ("Opening Autumn 2026", date(2026, 12, 31), "season_year"),
    ("Opening in Summer ’26", date(2026, 9, 21), "season_year"),           # two-digit year
    ("Q3 2026", date(2026, 9, 30), "quarter_year"),
    ("Third quarter of 2026", date(2026, 9, 30), "quarter_year"),
    ("Q1 2027", date(2027, 3, 31), "quarter_year"),
    ("early 2026", date(2026, 4, 30), "early_mid_late"),
    ("mid-2026", date(2026, 8, 31), "early_mid_late"),
    ("late 2026", date(2026, 12, 31), "early_mid_late"),
    ("end of 2026", date(2026, 12, 31), "early_mid_late"),
    ("August 2026", date(2026, 8, 31), "month_year"),
    ("Opening Feb 2028", date(2028, 2, 29), "month_year"),                  # leap year
    ("Opening 8/2026", date(2026, 8, 31), "month_year_numeric"),
    ("Grand opening March 3, 2027", date(2027, 3, 3), "exact_date_words"),
    ("Opening 11/15/2026", date(2026, 11, 15), "exact_date_numeric"),
    ("First half of 2027", date(2027, 6, 30), "half_year"),
    ("H2 2026", date(2026, 12, 31), "half_year"),
    ("Delivering 2027", date(2027, 12, 31), "year_only"),
    ("Now leasing for the 2026-27 school year", date(2026, 12, 31), "academic_year"),
    ("Academic year 2026-27", date(2026, 12, 31), "academic_year"),
]


@pytest.mark.parametrize("phrase,deadline,rule", CASES)
def test_each_phrase_gives_the_latest_reasonable_deadline(phrase, deadline, rule):
    o = interpret(phrase)
    assert (o.deadline, o.rule) == (deadline, rule)
    assert to_deadline(phrase)[0] == deadline and o.note


def test_every_rule_in_the_table_is_tested():
    assert {name for name, _, _ in seasons.RULES} == {rule for _, _, rule in CASES}


def test_winter_is_flagged_as_ambiguous():
    deadline, note = to_deadline("Opening Winter 2026")
    assert deadline == date(2027, 3, 20)
    assert "ambiguous" in note and "early 2026" in note and "March 20, 2027" in note


def test_fall_note_mentions_the_student_move_in():
    assert "August" in to_deadline("Opening Fall 2026")[1]


def test_the_phrase_can_sit_inside_a_sentence_and_the_most_specific_rule_wins():
    assert interpret("Opening Winter 2025, now pre-leasing!").deadline == date(2026, 3, 20)
    assert interpret("Grand opening March 3, 2027 - reserve now").rule == "exact_date_words"


# ---- student housing lease-up -------------------------------------------------------------------
def test_student_housing_lease_up_is_august_31_of_the_year_in_the_phrase():
    assert interpret("Opening Winter 2025", student_housing=True).lease_up_date == date(2025, 8, 31)
    assert interpret("Opening Fall 2026", student_housing=True).lease_up_date == date(2026, 8, 31)
    assert interpret("Opening Spring 2027", student_housing=True).lease_up_date == date(2027, 8, 31)
    assert interpret("Opening Fall 2026").lease_up_date is None                       # not student housing


def test_academic_year_always_sets_lease_up_and_uses_the_first_year():
    for phrase in ("Academic year 2026-27", "2026-27 academic year", "2026/2027 school year"):
        o = interpret(phrase)
        assert (o.deadline, o.lease_up_date, o.year) == (date(2026, 12, 31), date(2026, 8, 31), 2026)


# ---- no date ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("phrase", ["Coming soon", "Now pre-leasing", "Opening soon", "Stay tuned"])
def test_coming_soon_has_no_deadline_so_it_is_never_stale_on_its_own(phrase):
    o = interpret(phrase, student_housing=True)
    assert o.deadline is None and o.lease_up_date is None and "cannot be called late" in o.note


@pytest.mark.parametrize("phrase", ["", None, "Opening Spring", "Welcome to our building", "February 30, 2026"])
def test_phrases_without_a_usable_date_do_not_crash(phrase):
    o = interpret(phrase)
    assert o.deadline is None and o.note


def test_to_deadline_accepts_a_reference_date():
    assert to_deadline("Opening Fall 2026", date(2026, 11, 8))[0] == date(2026, 12, 31)
