"""Дневной и общий индекс Доркина.

дневной = основа (среднее тестов за день, иначе общий индекс на начало дня)
        + репорты    (−4…+4 за каждый, всего не больше ±10 за день)
        + наблюдение (+1 за каждые 5 очков, не больше +15 за день)
        + активность (войс, онлайн, ночь, «не беспокоить» — см. activity.py; вместе с наблюдением не больше +25)
        + ломка      (день без War Thunder: +3 × дней подряд, до +15; долгая игра −3)
        + уклонения  (+5 за каждое)
        → 0–100
общий   = первый дневной; дальше при закрытии каждого дня (00:00 МСК): 0.8 × общий + 0.2 × дневной

Пока не было ни одного теста, основы нет и индексы не считаются.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel
from potehinsonnet.storage import DataDir

from mr_windows_shield.activity import ActivityDay, activity_points, withdrawal
from mr_windows_shield.models import (
    DayIndex, DayObservation, LEVELS, Report, TestResult, day_start, msk_day,
)

REPORT_STEP = 2                 # (степень − середина шкалы) × 2: −4 … +4
REPORTS_CAP = 10
OBSERVATION_POINTS_PER_UNIT = 5
OBSERVATION_CAP = 15
WATCH_CAP = 25                  # наблюдение + активность за день
MISSED_PENALTY = 5
OVERALL_KEEP = 0.8              # доля старого общего индекса при закрытии дня


class IndexState(BaseModel):
    guild_id: int
    overall: float | None = None
    closed_day: date | None = None  # последний закрытый день


def day_id(guild_id: int, day: date) -> str:
    return f"{guild_id}:{day.isoformat()}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class IndexBook:
    def __init__(self, data: DataDir, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.reports = data.collection("reports.json", Report, key="id")
        self.tests = data.collection("tests.json", TestResult, key="id")
        self.observations = data.collection("observations.json", DayObservation, key="id")
        self.days = data.collection("day_index.json", DayIndex, key="id")
        self.states = data.collection("index_state.json", IndexState, key="guild_id")
        self.activity = data.collection("activity.json", ActivityDay, key="id")
        self.now = now

    # --- чтение -------------------------------------------------------------------------------

    def state(self, guild_id: int) -> IndexState:
        return self.states.get(guild_id) or IndexState(guild_id=guild_id)

    def _in_day(self, items, guild_id: int, day: date):
        start, end = day_start(day), day_start(day + timedelta(days=1))
        return [i for i in items if i.guild_id == guild_id and start <= i.created_at < end]

    def compute(self, guild_id: int, day: date, overall: float | None) -> DayIndex | None:
        """Дневной индекс за day при общем индексе overall на начало дня; None — основы нет."""
        tests = self._in_day(self.tests.all(), guild_id, day)
        done = [t.index for t in tests if t.completed and t.index is not None]
        if done:
            base, from_test = sum(done) / len(done), True
        elif overall is not None:
            base, from_test = overall, False
        else:
            return None

        middle = (len(LEVELS) + 1) / 2
        reports = clamp(sum((r.level - middle) * REPORT_STEP for r in self._in_day(self.reports.all(), guild_id, day)),
                        -REPORTS_CAP, REPORTS_CAP)
        seen = self.observations.get(day_id(guild_id, day))
        observation = min(OBSERVATION_CAP, (seen.points if seen else 0) // OBSERVATION_POINTS_PER_UNIT)
        points = activity_points(self.activity.get(day_id(guild_id, day)))
        activity = min(WATCH_CAP - observation, sum(points.values()))
        lack, streak = withdrawal(self.activity_days(guild_id), day)
        missed = MISSED_PENALTY * sum(1 for t in tests if not t.completed)
        daily = clamp(base + reports + observation + activity + lack + missed, 0, 100)
        return DayIndex(id=day_id(guild_id, day), guild_id=guild_id, day=day, base=round(base, 1),
                        base_from_test=from_test, reports=reports, observation=observation, missed=missed,
                        activity=activity, activity_reasons=points, withdrawal=lack, withdrawal_streak=streak,
                        daily=round(daily, 1))

    def activity_days(self, guild_id: int) -> dict[date, ActivityDay]:
        return {a.day: a for a in self.activity.all() if a.guild_id == guild_id}

    def today(self, guild_id: int) -> DayIndex | None:
        """Дневной индекс за сегодня «вживую»."""
        return self.compute(guild_id, msk_day(self.now()), self.state(guild_id).overall)

    def history(self, guild_id: int) -> list[DayIndex]:
        """Закрытые дни и сегодняшний, по порядку."""
        days = sorted((d for d in self.days.all() if d.guild_id == guild_id), key=lambda d: d.day)
        if today := self.today(guild_id):
            days.append(today)
        return days

    # --- закрытие дней --------------------------------------------------------------------------

    def _first_day(self, guild_id: int) -> date | None:
        moments = [t.created_at for t in self.tests.all() if t.guild_id == guild_id and t.completed]
        return msk_day(min(moments)) if moments else None

    def close_days(self, guild_id: int) -> None:
        """Закрыть все прошедшие дни (00:00 МСК): записать дневной и пересчитать общий."""
        state = self.state(guild_id)
        today = msk_day(self.now())
        day = state.closed_day + timedelta(days=1) if state.closed_day else self._first_day(guild_id)
        if day is None:
            return
        changed = False
        while day < today:
            result = self.compute(guild_id, day, state.overall)
            if result is not None:
                state.overall = round(result.daily if state.overall is None
                                      else OVERALL_KEEP * state.overall + (1 - OVERALL_KEEP) * result.daily, 1)
                result.overall = state.overall
                self.days.put(result)
            state.closed_day = day
            changed = True
            day += timedelta(days=1)
        if changed:
            self.states.put(state)
