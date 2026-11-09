import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from potehinsonnet.storage import DataDir

from mr_windows_shield.chart import PERIODS, render_chart
from mr_windows_shield.index import IndexBook, day_id
from mr_windows_shield.models import DayObservation, Report, TestResult, dorkin_index, level_for_index

GUILD = 1
DAY1 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)        # 12:00 МСК, 09.11


class Clock:
    def __init__(self):
        self.value = DAY1

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def book(tmp_path, clock):
    return IndexBook(DataDir(tmp_path), now=clock)


def add_test(book, at, answers=None, completed=True):
    answers = answers or {}
    book.tests.put(TestResult(id=uuid.uuid4().hex, guild_id=GUILD, user_id=7, user_name="Д", answers=answers,
                              index=dorkin_index(answers) if completed else None, completed=completed, created_at=at))


def add_report(book, at, level):
    book.reports.put(Report(id=uuid.uuid4().hex, guild_id=GUILD, channel_id=1, user_id=2, user_name="В",
                            level=level, created_at=at))


ALL = {"airfryer": 5, "women": 5, "warthunder": 5, "autism": 5}


@pytest.mark.parametrize("answers, expected", [
    ({"airfryer": 1, "women": 1, "warthunder": 1, "autism": 5}, 0),      # без желаний аутизм ничего не даёт
    (ALL, 100),
    ({"airfryer": 5, "women": 5, "warthunder": 5, "autism": 1}, 50),     # аутизм 1 — база как есть
    ({"airfryer": 3, "women": 3, "warthunder": 3, "autism": 3}, 37.5),
])
def test_dorkin_index_formula(answers, expected):
    assert dorkin_index(answers) == expected


@pytest.mark.parametrize("index, level", [(0, 1), (19.9, 1), (20, 2), (59, 3), (79.9, 4), (80, 5), (100, 5)])
def test_level_for_index(index, level):
    assert level_for_index(index) == level


def test_no_index_before_first_test(book):
    add_report(book, DAY1, 5)
    assert book.today(GUILD) is None


def test_daily_sums_base_reports_observation_missed(book):
    add_test(book, DAY1, {"airfryer": 3, "women": 3, "warthunder": 3, "autism": 3})     # 37.5
    add_report(book, DAY1, 5)        # +4
    add_report(book, DAY1, 4)        # +2
    add_test(book, DAY1 + timedelta(hours=1), completed=False)       # +5
    book.observations.put(DayObservation(id=day_id(GUILD, date(2026, 11, 9)), guild_id=GUILD,
                                         day=date(2026, 11, 9), points=23))                # 23 // 5 = +4

    today = book.today(GUILD)
    assert (today.base, today.base_from_test, today.reports, today.observation, today.missed) == (37.5, True, 6, 4, 5)
    assert today.daily == 52.5


def test_caps_on_reports_and_observation(book):
    add_test(book, DAY1, ALL)                                        # 100
    for _ in range(10):
        add_report(book, DAY1, 1)                                    # −4 × 10 → −10
    book.observations.put(DayObservation(id=day_id(GUILD, date(2026, 11, 9)), guild_id=GUILD,
                                         day=date(2026, 11, 9), points=500))
    today = book.today(GUILD)
    assert (today.reports, today.observation, today.daily) == (-10, 15, 100)     # 100 − 10 + 15 → обрезано до 100


def test_several_tests_a_day_are_averaged(book):
    add_test(book, DAY1, ALL)                                        # 100
    add_test(book, DAY1 + timedelta(hours=6), {"airfryer": 1, "women": 1, "warthunder": 1, "autism": 1})   # 0
    assert book.today(GUILD).base == 50


def test_overall_starts_from_first_day_then_moves_slowly(book, clock):
    add_test(book, DAY1, ALL)                                        # день 1: 100
    clock.value = DAY1 + timedelta(days=1)
    book.close_days(GUILD)
    assert book.state(GUILD).overall == 100

    add_test(book, clock.value, {"airfryer": 1, "women": 1, "warthunder": 1, "autism": 1})   # день 2: 0
    clock.value += timedelta(days=1)
    book.close_days(GUILD)
    assert book.state(GUILD).overall == 80                           # 0.8 × 100 + 0.2 × 0
    assert [d.overall for d in book.history(GUILD)] == [100, 80, None]   # закрытые дни и сегодняшний


def test_day_without_test_keeps_overall_and_reports_move_it(book, clock):
    add_test(book, DAY1, {"airfryer": 5, "women": 5, "warthunder": 5, "autism": 1})    # 50
    clock.value = DAY1 + timedelta(days=1)
    book.close_days(GUILD)
    add_report(book, clock.value, 5)                                 # день 2 без теста: основа = общий 50, +4
    today = book.today(GUILD)
    assert (today.base, today.base_from_test, today.daily) == (50, False, 54)

    clock.value += timedelta(days=3)                                 # пропущенные дни закрываются разом
    book.close_days(GUILD)
    assert book.state(GUILD).overall == pytest.approx(50.8)          # 0.8 × 50 + 0.2 × 54; пустые дни не двигают
    assert book.state(GUILD).closed_day == date(2026, 11, 12)


def test_chart_with_everything_renders(book, clock):
    add_test(book, DAY1, ALL)
    add_test(book, DAY1 + timedelta(hours=3), completed=False)
    add_report(book, DAY1, 3)
    clock.value += timedelta(days=2)
    add_test(book, clock.value, {"airfryer": 2, "women": 4, "warthunder": 3, "autism": 2})
    book.close_days(GUILD)
    for period in PERIODS.values():
        assert render_chart(book, GUILD, period, clock()).startswith(b"\x89PNG")
