from datetime import date, datetime, timedelta, timezone

import pytest
from potehinsonnet.storage import DataDir

from mr_windows_shield.index import IndexBook, day_id
from mr_windows_shield.observe import ChatObserver, build_topics, score_text
from mr_windows_shield.survey import Surveys

GUILD, DORKIN = 1, 77
TOPICS = build_topics({})


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.mark.parametrize("text, reasons", [
    ("купил аэрогриль", {"airfryer": 3}),
    ("го в тундру, я в ангаре", {"warthunder": 3}),
    ("WT вечером?", {"warthunder": 3}),
    ("тянуть не будем", {}),                                    # «тян» — только целым словом
    ("смотрел на тян", {"women": 3}),
    ("ЭТО ПРОСТО НЕВЕРОЯТНО", {"caps": 2}),
    ("ОК", {}),                                                 # короткий капс не считается
    ("что???", {"shout": 1}),
    ("а" * 501, {"long": 2}),
    ("АЭРОГРИЛЬ И ТАНКИ!!!", {"airfryer": 3, "warthunder": 3, "caps": 2, "shout": 1}),
    ("обычное сообщение", {}),
])
def test_score_text(text, reasons):
    assert score_text(text, TOPICS) == reasons


def test_custom_words_from_settings():
    topics = build_topics({"watch_airfryer": "мультиварк"})
    assert score_text("моя мультиварка", topics) == {"airfryer": 3}
    assert score_text("аэрогриль", topics) == {}


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    book = IndexBook(DataDir(tmp_path), now=clock)
    surveys = Surveys(DataDir(tmp_path), None, book, now=clock)
    surveys.configure(GUILD, DORKIN, 6, True)
    return ChatObserver(None, book, surveys, TOPICS, now=clock), book, clock


def message(text, user_id=DORKIN, guild_id=GUILD):
    return {"user": {"id": user_id, "name": "d"}, "text": text, "channel_id": 5, "guild_id": guild_id}


def points(book, day=date(2026, 11, 9)):
    seen = book.observations.get(day_id(GUILD, day))
    return seen.points if seen else 0


async def test_only_target_on_his_server_is_observed(setup):
    observer, book, _ = setup
    await observer.on_message(message("аэрогриль", user_id=5))
    await observer.on_message(message("аэрогриль", guild_id=2))
    await observer.on_message(message("аэрогриль", guild_id=None))       # личка
    assert points(book) == 0
    await observer.on_message(message("аэрогриль"))
    assert points(book) == 3
    assert book.observations.get(day_id(GUILD, date(2026, 11, 9))).reasons == {"airfryer": 3}


async def test_flood_over_five_per_minute(setup):
    observer, book, clock = setup
    for _ in range(7):
        await observer.on_message(message("привет"))
    assert points(book) == 2                                    # 6-е и 7-е
    clock.value += timedelta(minutes=2)
    await observer.on_message(message("привет"))
    assert points(book) == 2


async def test_points_go_to_msk_day(setup):
    observer, book, clock = setup
    clock.value = datetime(2026, 11, 9, 21, 30, tzinfo=timezone.utc)    # 00:30 МСК 10.11
    await observer.on_message(message("аэрогриль"))
    assert points(book, date(2026, 11, 10)) == 3
