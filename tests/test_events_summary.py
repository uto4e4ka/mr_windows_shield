import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from potehinsonnet.net_models.discord_models import DiscordMessageResponse
from potehinsonnet.storage import DataDir

from mr_windows_shield.events import Event, EventBook, holidays_around, parse_warthunder, parse_wiki
from mr_windows_shield.index import IndexBook, day_id
from mr_windows_shield.models import DayIndex, DayObservation, Report, TestResult, dorkin_index
from mr_windows_shield.observe import ChatObserver, build_topics
from mr_windows_shield.summary import Summaries
from mr_windows_shield.survey import PendingSurvey, Surveys

GUILD, DORKIN = 1, 77


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


WT_PAGE = '''
<div class="showcase__item widget  ">
    <a class="widget__link" href="/en/news/10319-development-enhancements-to-munition-guidance-en"></a>
    <div class="widget__title">
        Enhancements to Munition Guidance
    </div>
    <li class="widget-meta__item widget-meta__item--right">
        9 November 2026
    </li>
</div>
<div class="showcase__item widget  ">
    <a class="widget__link" href="/en/news/10318-event-the-great-war-returns-en"></a>
    <div class="widget__title">The Great War Returns &amp; More</div>
    <li class="widget-meta__item widget-meta__item--right">8 November 2026</li>
</div>
'''

WIKI = '''<section begin="Ноябрь" />
=== [[7 ноября]] ===
* Катастрофы и происшествия
** Неизвестный [[Стрельба в Кракове|открыл огонь]] в центре [[Краков]]а<ref>https://x</ref>.

=== [[4 ноября]] ===
* Спорт
** [[Комо (футбольный клуб)|«Комо»]] разгромил «Ланс» со счётом 9:1<ref>{{cite web|title=x}}</ref>.
** {{нп|Что-то||en|Something}}: второе событие.
=== [[30 декабря]] ===
* Разное
** Событие прошлого года.
'''


def test_parse_warthunder():
    events = parse_warthunder(WT_PAGE)
    assert [(e.id, e.day, e.category, e.title) for e in events] == [
        ("wt:10319", date(2026, 11, 9), "War Thunder: development", "Enhancements to Munition Guidance"),
        ("wt:10318", date(2026, 11, 8), "War Thunder: event", "The Great War Returns & More"),
    ]
    assert events[0].url == "https://warthunder.com/en/news/10319-development-enhancements-to-munition-guidance-en"


def test_parse_wiki():
    events = parse_wiki(WIKI, today=date(2026, 11, 9))
    assert [(e.day, e.category, e.title) for e in events] == [
        (date(2026, 11, 7), "Катастрофы и происшествия", "Неизвестный открыл огонь в центре Кракова."),
        (date(2026, 11, 4), "Спорт", "«Комо» разгромил «Ланс» со счётом 9:1."),
        (date(2026, 11, 4), "Спорт", "Что-то: второе событие."),
        (date(2025, 12, 30), "Разное", "Событие прошлого года."),
    ]
    assert len({e.id for e in events}) == 4


def test_holidays():
    days = {(e.day, e.title) for e in holidays_around(date(2026, 11, 9))}
    assert (date(2026, 11, 4), "День народного единства") in days
    assert (date(2027, 1, 1), "Новый год") in days


@pytest.fixture
def book(tmp_path):
    return IndexBook(DataDir(tmp_path), now=Clock(datetime(2026, 11, 20, 12, tzinfo=timezone.utc)))


async def test_refresh_merges_sources_and_survives_errors(tmp_path):
    async def wt(client, today):
        return [Event(id="wt:1", day=today, title="Патч", category="War Thunder: update", source="warthunder")]

    async def broken(client, today):
        raise RuntimeError("нет сети")

    events = EventBook(DataDir(tmp_path), {}, now=Clock(datetime(2026, 11, 9, 12, tzinfo=timezone.utc)),
                       fetchers={"warthunder": wt, "wiki": broken})
    await events.refresh()
    assert events.events.get("wt:1") is not None
    assert events.events.get("holiday:2026-11-04") is not None
    assert "нет сети" in events.last_errors["wiki"]

    await events.refresh()                                   # не чаще раза в 6 часов
    assert len(events.events.all()) == len(holidays_around(date(2026, 11, 9))) + 1


def test_disabled_source_is_hidden(tmp_path):
    events = EventBook(DataDir(tmp_path), {"events_wiki": False})
    events.events.put(Event(id="w", day=date(2026, 11, 9), title="x", category="Спорт", source="wiki"))
    events.add_manual(GUILD, date(2026, 11, 9), "Купил аэрогриль", "")
    assert [e.source for e in events.for_day(GUILD, date(2026, 11, 9))] == ["manual"]
    assert events.for_day(2, date(2026, 11, 9)) == []          # своё — только своего сервера


def test_manual_events_delete_only_own(tmp_path):
    events = EventBook(DataDir(tmp_path), {})
    mine = events.add_manual(GUILD, date(2026, 11, 9), "Купил аэрогриль", "Покупки")
    assert not events.delete_manual(2, mine.id)
    assert events.delete_manual(GUILD, mine.id)


def test_analysis_compares_event_days(tmp_path):
    events = EventBook(DataDir(tmp_path), {})
    start = date(2026, 11, 1)
    days = [DayIndex(id=str(i), guild_id=GUILD, day=start + timedelta(days=i), base=0, base_from_test=True,
                     reports=0, observation=0, missed=0, daily=80 if i in (0, 3, 6) else 40, overall=50)
            for i in range(10)]
    for i in (0, 3, 6):
        events.events.put(Event(id=f"wt{i}", day=start + timedelta(days=i), title="патч",
                                category="War Thunder: update", source="warthunder"))
    events.events.put(Event(id="once", day=start, title="раз", category="Редкое", source="wiki"))

    [stat] = events.analysis(GUILD, days)                      # «Редкое» — меньше 3 дней, не считается
    assert (stat.category, stat.days, stat.on_day, stat.next_day, stat.other) == ("War Thunder: update", 3, 80, 40, 40)


async def test_summary_text_and_send(tmp_path, book):
    events = EventBook(DataDir(tmp_path), {})
    discord = AsyncMock()
    summaries = Summaries(DataDir(tmp_path), discord, book, events, now=book.now)
    d18, d19 = date(2026, 11, 18), date(2026, 11, 19)
    book.days.put(DayIndex(id=day_id(GUILD, d18), guild_id=GUILD, day=d18, base=50, base_from_test=True,
                           reports=0, observation=0, missed=0, daily=59, overall=50))
    book.days.put(DayIndex(id=day_id(GUILD, d19), guild_id=GUILD, day=d19, base=60, base_from_test=True,
                           reports=4, observation=7, missed=0, daily=71, overall=54.2))
    answers = {"airfryer": 5, "women": 3, "warthunder": 5, "autism": 4}
    book.tests.put(TestResult(id="t", guild_id=GUILD, user_id=DORKIN, user_name="Д", answers=answers,
                              index=dorkin_index(answers), completed=True,
                              created_at=datetime(2026, 11, 19, 10, tzinfo=timezone.utc)))
    book.observations.put(DayObservation(id=day_id(GUILD, d19), guild_id=GUILD, day=d19, points=35,
                                         reasons={"airfryer": 12, "warthunder": 6}))
    events.add_manual(GUILD, d19, "Вышло обновление WT", "WT")
    events.events.put(Event(id="w", day=d19, title="Что-то в мире " + "x" * 200, category="Спорт", source="wiki"))

    text = summaries.text(GUILD, d19)
    assert "за 19.11.2026" in text
    assert "Дневной: **71 · 🔴 Обильное выделение** (▲ +12)" in text
    assert "Общий: **54 · 🟠 Сильное выделение** (▲ +4.2)" in text
    assert "1 пройдено, 0 уклонился · Репорты: 0 · Наблюдение: 35 очк. (🍗 аэрогриль 12, ✈️ War Thunder 6)" in text
    assert "🍗 5.0" in text and "🔗 **Связанные события:**\n✍️ Вышло обновление WT\n🌍 Что-то в мире" in text
    assert "…" in text

    assert "канал" in await summaries.send(GUILD)
    summaries.set_channel(GUILD, 555)
    assert await summaries.send(GUILD) == "Сводка отправлена"
    sent = discord.send_message.await_args.args[0]
    assert sent.channel_id == 555 and sent.files[0].content().startswith(b"\x89PNG")


async def test_summary_goes_once_after_ten_msk(tmp_path):
    clock = Clock(datetime(2026, 11, 20, 6, 59, tzinfo=timezone.utc))        # 09:59 МСК
    book = IndexBook(DataDir(tmp_path), now=clock)
    discord = AsyncMock()
    summaries = Summaries(DataDir(tmp_path), discord, book, EventBook(DataDir(tmp_path), {}, now=clock), now=clock)
    summaries.set_channel(GUILD, 555)
    await summaries.tick()
    assert discord.send_message.await_count == 0
    clock.value += timedelta(minutes=1)
    await summaries.tick()
    await summaries.tick()
    assert discord.send_message.await_count == 1
    assert "за 19.11.2026" in discord.send_message.await_args.args[0].containers[0].components[0].content
    clock.value += timedelta(days=1)
    await summaries.tick()
    assert discord.send_message.await_count == 2


async def test_reminder_every_fifth_message_not_more_than_every_10_min(tmp_path):
    clock = Clock(datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
    book = IndexBook(DataDir(tmp_path), now=clock)
    surveys = Surveys(DataDir(tmp_path), AsyncMock(), book, now=clock)
    surveys.configure(GUILD, DORKIN, 6, True)
    discord = AsyncMock()
    observer = ChatObserver(None, book, surveys, build_topics({}), discord, now=clock)
    msg = {"user": {"id": DORKIN, "name": "d"}, "text": "привет", "channel_id": 5, "guild_id": GUILD}

    for _ in range(5):
        await observer.on_message(msg)
    assert discord.send_message.await_count == 0                 # теста нет — не напоминаем

    surveys.pending.put(PendingSurvey(id="s", guild_id=GUILD, user_id=DORKIN, channel_id=900, message_id=1,
                                      sent_at=clock.value - timedelta(minutes=20)))
    for _ in range(5):
        await observer.on_message(msg)
    reminder = discord.send_message.await_args.args[0]
    assert reminder.channel_id == 5 and "Осталось 40 мин" in reminder.text
    assert reminder.placeholders["user"].value == DORKIN

    clock.value += timedelta(minutes=5)
    for _ in range(10):
        await observer.on_message(msg)
    assert discord.send_message.await_count == 1                 # 10 минут ещё не прошло

    clock.value += timedelta(minutes=5)
    await observer.on_message(msg)
    assert discord.send_message.await_count == 2


RSS = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Лента</title>
""" + "".join(f"""<item><title>Новость {i} &amp; ещё</title><link>https://x.ru/{i}</link>
<category>Политика</category><pubDate>Mon, 09 Nov 2026 {10 + i}:00:00 +0300</pubDate></item>""" for i in range(7)) + """
<item><title>Ночная</title><link>https://x.ru/n</link><pubDate>Sun, 08 Nov 2026 22:30:00 +0000</pubDate></item>
<item><title>Без даты</title></item>
</channel></rss>"""


def test_parse_rss_limits_per_day_and_uses_msk():
    from mr_windows_shield.events import parse_rss
    events = parse_rss(RSS, "https://x.ru/rss")
    nov9 = [e for e in events if e.day == date(2026, 11, 9)]
    assert len(nov9) == 5 and "Ночная" not in [e.title for e in events]     # лимит 5 в день на ленту
    assert nov9[0].title == "Новость 0 & ещё" and nov9[0].category == "Новости: Политика"
    assert nov9[0].url == "https://x.ru/0" and nov9[0].source == "news"

    night = parse_rss("""<rss><channel><item><title>Ночная</title>
        <pubDate>Sun, 08 Nov 2026 22:30:00 +0000</pubDate></item></channel></rss>""", "https://x.ru/rss")
    assert [(e.day, e.category) for e in night] == [(date(2026, 11, 9), "Новости мира")]   # 01:30 МСК — уже 09.11


def test_news_feeds_from_settings():
    from mr_windows_shield.events import DEFAULT_NEWS_FEEDS, news_feeds
    assert news_feeds({}) == [u.strip() for u in DEFAULT_NEWS_FEEDS.split(",")]
    assert news_feeds({"news_feeds": " https://a.ru/rss , https://b.ru/rss"}) == ["https://a.ru/rss", "https://b.ru/rss"]
    assert news_feeds({"news_feeds": ""}) == []


async def test_each_feed_has_own_error(tmp_path):
    async def ok(client, today):
        return [Event(id="news:1", day=today, title="Новость", category="Новости мира", source="news")]

    async def down(client, today):
        raise RuntimeError("таймаут")

    events = EventBook(DataDir(tmp_path), {"events_holiday": False},
                       now=Clock(datetime(2026, 11, 9, 12, tzinfo=timezone.utc)),
                       fetchers={"news a.ru": ok, "news b.ru": down})
    await events.refresh()
    assert events.events.get("news:1") is not None
    assert list(events.last_errors) == ["news b.ru"]


def test_news_sources_are_switched_off_together(tmp_path):
    events = EventBook(DataDir(tmp_path), {"events_news": False, "news_feeds": "https://a.ru/rss"})
    assert "news a.ru" in events.fetchers and events.enabled["news"] is False


async def test_summary_lists_world_news(tmp_path, book):
    events = EventBook(DataDir(tmp_path), {})
    d19 = date(2026, 11, 19)
    for i in range(4):
        events.events.put(Event(id=f"n{i}", day=d19, title=f"Заголовок {i}", category="Новости мира", source="news"))
    text = Summaries(DataDir(tmp_path), AsyncMock(), book, events, now=book.now).text(GUILD, d19)
    assert "🔗 **Связанные события:**\n📰 Заголовок 0\n📰 Заголовок 1\n📰 Заголовок 2" in text
    assert "Заголовок 3" not in text


async def test_refresh_is_logged(tmp_path, caplog):
    import logging

    async def wt(client, today):
        return [Event(id="wt:1", day=today, title="Патч", category="War Thunder: update", source="warthunder")]

    async def down(client, today):
        raise RuntimeError("таймаут")

    events = EventBook(DataDir(tmp_path), {"events_holiday": False},
                       now=Clock(datetime(2026, 11, 9, 12, tzinfo=timezone.utc)),
                       fetchers={"warthunder": wt, "news a.ru": down})
    with caplog.at_level(logging.INFO, logger="mr_windows_shield.events"):
        await events.refresh()
    assert "События обновлены — ✈️ War Thunder: 1; не загрузились: news a.ru" in caplog.text
