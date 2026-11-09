from datetime import date, datetime, timedelta, timezone

import pytest
from potehinsonnet.net import NatsRequestError
from potehinsonnet.storage import DataDir

from mr_windows_shield.activity import ActivityDay, ActivityTracker, activity_points, withdrawal
from mr_windows_shield.index import IndexBook, day_id
from mr_windows_shield.models import TestResult
from mr_windows_shield.survey import Surveys

GUILD, DORKIN = 1, 77
DAY = date(2026, 11, 9)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)      # 15:00 МСК

    def __call__(self):
        return self.value


class FakeNats:
    def __init__(self):
        self.state = {"status": "online", "activities": [], "presence_known": True, "voice_channel_id": None}
        self.requests = 0

    async def request(self, subject, payload, timeout=10.0):
        self.requests += 1
        if isinstance(self.state, Exception):
            raise self.state
        return dict(self.state, guild_id=payload["guild_id"], user={"id": payload["user_id"]})


@pytest.fixture
def setup(tmp_path):
    clock, nats = Clock(), FakeNats()
    book = IndexBook(DataDir(tmp_path), now=clock)
    surveys = Surveys(DataDir(tmp_path), None, book, now=clock)
    surveys.configure(GUILD, DORKIN, 6, True)
    return ActivityTracker(nats, book, surveys, now=clock), book, clock, nats


async def run_minutes(tracker, clock, minutes):
    for _ in range(minutes):
        await tracker.tick()
        clock.value += timedelta(minutes=1)


def record(book, day=DAY):
    return book.activity.get(day_id(GUILD, day))


def voice(action_channel, user_id=DORKIN):
    return {"guild": {"id": GUILD}, "user": {"id": user_id}, "after_channel": action_channel and {"id": 5}}


async def test_voice_minutes_skip_first_two(setup):
    tracker, book, clock, nats = setup
    nats.state["status"] = "offline"
    await tracker.tick()                                   # снимок: не в войсе
    await tracker.on_voice(voice(True))
    await run_minutes(tracker, clock, 10)
    assert record(book).voice == 8                         # 10 тиков, минуты 0 и 1 захода не считаются
    await tracker.on_voice(voice(False))
    await run_minutes(tracker, clock, 5)
    assert record(book).voice == 8


async def test_voice_of_others_ignored(setup):
    tracker, book, clock, _ = setup
    await tracker.tick()
    await tracker.on_voice(voice(True, user_id=5))
    await run_minutes(tracker, clock, 5)
    assert record(book).voice == 0


async def test_snapshot_picks_up_voice_and_game(setup):
    tracker, book, clock, nats = setup
    nats.state.update(voice_channel_id=5, activities=["War Thunder"], status="dnd")
    await run_minutes(tracker, clock, 5)
    r = record(book)
    assert (r.online, r.dnd, r.game, r.presence_known) == (5, 5, 5, True)
    assert r.voice == 3
    assert nats.requests == 1                              # следующий снимок через 10 мин


async def test_presence_event_changes_state(setup):
    tracker, book, clock, nats = setup
    nats.state["status"] = "offline"
    await tracker.tick()
    await tracker.on_presence({"guild_id": GUILD, "user": {"id": DORKIN}, "status": "online",
                               "activities": ["War Thunder"]})
    await run_minutes(tracker, clock, 3)
    assert (record(book).online, record(book).game) == (3, 3)


async def test_night_minutes(setup):
    tracker, book, clock, nats = setup
    clock.value = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)          # 01:00 МСК 10.11
    nats.state["voice_channel_id"] = 5
    await run_minutes(tracker, clock, 4)
    r = record(book, date(2026, 11, 10))
    assert (r.online_night, r.voice_night) == (4, 2)


async def test_without_presence_intent_only_voice(setup):
    tracker, book, clock, nats = setup
    nats.state.update(presence_known=False, voice_channel_id=5)
    await run_minutes(tracker, clock, 4)
    r = record(book)
    assert (r.online, r.presence_known, r.voice) == (0, False, 2)


async def test_old_bot_error_is_shown(setup):
    tracker, book, clock, nats = setup
    nats.state = NatsRequestError("no responders")
    await tracker.tick()
    assert "no responders" in tracker.last_error


def test_points_caps():
    day = ActivityDay(id="x", guild_id=GUILD, day=DAY, voice=600, voice_night=60, online=700, online_night=200, dnd=240)
    assert activity_points(day) == {"voice": 10, "online": 5, "night": 5, "dnd": 2}
    assert activity_points(ActivityDay(id="x", guild_id=GUILD, day=DAY, voice=29)) == {}


def days(*games, known=True):
    start = DAY - timedelta(days=len(games) - 1)
    return {start + timedelta(days=i): ActivityDay(id=str(i), guild_id=GUILD, day=start + timedelta(days=i),
                                                   game=g, presence_known=known) for i, g in enumerate(games)}


@pytest.mark.parametrize("games, expected", [
    ((0,), (3, 1)),
    ((0, 0, 0), (9, 3)),
    ((0,) * 8, (15, 8)),
    ((0, 45, 0, 10), (6, 2)),          # сыграл 45 мин — серия заново
    ((0, 0, 30), (0, 0)),
    ((0, 0, 150), (-3, 0)),
])
def test_withdrawal(games, expected):
    assert withdrawal(days(*games), DAY) == expected


def test_no_withdrawal_without_presence():
    assert withdrawal(days(0, 0, known=False), DAY) == (0, 0)
    assert withdrawal({}, DAY) == (0, 0)


def test_activity_and_withdrawal_in_daily_index(tmp_path):
    clock = Clock()
    book = IndexBook(DataDir(tmp_path), now=clock)
    book.tests.put(TestResult(id="t", guild_id=GUILD, user_id=DORKIN, user_name="d", index=50, completed=True,
                              created_at=clock.value))
    for d in days(0, 0).values():
        d.id, d.voice = day_id(GUILD, d.day), 90 if d.day == DAY else 0
        book.activity.put(d)
    today = book.today(GUILD)
    assert (today.activity, today.activity_reasons) == (3, {"voice": 3})
    assert (today.withdrawal, today.withdrawal_streak) == (6, 2)
    assert today.daily == 59
