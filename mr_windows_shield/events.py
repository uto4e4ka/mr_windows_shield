"""События для анализа Индекса Доркина: свои (из панели), новости War Thunder, праздники РФ, «Текущие события» Википедии,
новости мира из RSS-лент (адреса — в настройках).

Автоматические события общие для всех серверов (guild_id=None), свои — только своего сервера.
Анализ: средний дневной индекс в день события и на следующий день против остальных дней.
"""
import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from mr_windows_shield.models import msk_day

logger = logging.getLogger(__name__)

USER_AGENT = "PotehinsonBot/1.0 (https://github.com/uto4e4ka/mr_windows_shield) httpx"
WT_NEWS_URL = "https://warthunder.com/en/news/"
WIKI_API = "https://ru.wikipedia.org/w/api.php"
WIKI_PAGE = "Портал:Текущие события/Последние события"
REFRESH_EVERY = timedelta(hours=6)
NEWS_PER_FEED_PER_DAY = 5
# Ленты, которые обычно открываются из России (Медуза и BBC там заблокированы — добавьте сами, если бот за границей)
DEFAULT_NEWS_FEEDS = ("https://rssexport.rbc.ru/rbcnews/news/30/full.rss, https://lenta.ru/rss/news/world, "
                      "https://ria.ru/export/rss2/world/index.xml, https://tass.ru/rss/v2.xml, "
                      "https://www.interfax.ru/rss.asp")
MIN_DAYS_FOR_ANALYSIS = 3

SOURCES = {
    "manual": "✍️ Своё",
    "holiday": "🎉 Праздник",
    "warthunder": "✈️ War Thunder",
    "wiki": "🌍 Википедия",
    "news": "📰 Новости мира",
}

MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
             "ноября", "декабря"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]

# Официальные праздники РФ (без переносов выходных)
HOLIDAYS = {
    (1, 1): "Новый год", (1, 2): "Новогодние каникулы", (1, 3): "Новогодние каникулы",
    (1, 4): "Новогодние каникулы", (1, 5): "Новогодние каникулы", (1, 6): "Новогодние каникулы",
    (1, 7): "Рождество Христово", (1, 8): "Новогодние каникулы", (2, 23): "День защитника Отечества",
    (3, 8): "Международный женский день", (5, 1): "Праздник Весны и Труда", (5, 9): "День Победы",
    (6, 12): "День России", (11, 4): "День народного единства", (12, 31): "Канун Нового года",
}


class Event(BaseModel):
    id: str
    guild_id: int | None = None     # None — общее (автоматическое) событие
    day: date
    title: str
    category: str                   # для анализа: «War Thunder: development», «Спорт», «Праздник»…
    source: str                     # manual, holiday, warthunder, wiki
    url: str = ""


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# --- разбор источников ------------------------------------------------------------------------

def parse_warthunder(page: str) -> list[Event]:
    """Новости со страницы warthunder.com/en/news/: заголовок, дата, раздел (development, event, shop…)."""
    events = []
    for block in page.split('class="showcase__item')[1:]:
        link = re.search(r'href="(/en/news/(\d+)-([a-z0-9\-]+?))"', block)
        title = re.search(r'widget__title">\s*(.*?)\s*</div>', block, re.S)
        when = re.search(r'widget-meta__item--right">\s*(\d{1,2}) (\w+) (\d{4})', block)
        if not (link and title and when) or when.group(2) not in MONTHS_EN:
            continue
        day = date(int(when.group(3)), MONTHS_EN.index(when.group(2)) + 1, int(when.group(1)))
        kind = link.group(3).split("-", 1)[0]
        events.append(Event(id=f"wt:{link.group(2)}", day=day, title=html.unescape(title.group(1)).strip(),
                            category=f"War Thunder: {kind}", source="warthunder",
                            url=f"https://warthunder.com{link.group(1)}"))
    return events


def _clean_wiki(text: str) -> str:
    text = re.sub(r"<ref[^>]*/>|<ref.*?</ref>", "", text, flags=re.S)
    for _ in range(3):                                         # вложенные шаблоны
        text = re.sub(r"\{\{нп\|([^|{}]*)\|[^{}]*\}\}", r"\1", text)
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    return re.sub(r"\s+", " ", text).strip(" :")


def parse_wiki(wikitext: str, today: date) -> list[Event]:
    """«=== [[7 ноября]] ===», под ним «* Категория» и «** событие». Год — ближайший прошедший к today."""
    events, day, category = [], None, "Разное"
    for line in wikitext.splitlines():
        heading = re.match(r"=+\s*\[\[(\d{1,2}) (\w+)\]\]\s*=+", line)
        if heading:
            month = heading.group(2)
            if month not in MONTHS_RU:
                day = None
                continue
            day = date(today.year, MONTHS_RU.index(month) + 1, int(heading.group(1)))
            if day > today:                                    # декабрь, увиденный в январе
                day = day.replace(year=today.year - 1)
            continue
        if day is None:
            continue
        if re.match(r"\*(?!\*)", line):
            category = _clean_wiki(line[1:]) or "Разное"
        elif line.startswith("**"):
            text = _clean_wiki(line.lstrip("*"))
            if text:
                events.append(Event(id=f"wiki:{day.isoformat()}:{_hash(text)}", day=day, title=text,
                                    category=category, source="wiki"))
    return events


def holidays_around(today: date) -> list[Event]:
    return [Event(id=f"holiday:{year}-{month:02d}-{dom:02d}", day=date(year, month, dom), title=name,
                  category="Праздник", source="holiday")
            for year in (today.year - 1, today.year, today.year + 1)
            for (month, dom), name in HOLIDAYS.items()]


def parse_rss(xml_text: str, feed_url: str) -> list[Event]:
    """RSS 2.0: заголовок, ссылка, дата (pubDate), рубрика. С каждой ленты — не больше 5 новостей за день (первые = свежие)."""
    root = ET.fromstring(xml_text)
    per_day: dict[date, int] = {}
    events = []
    for item in root.iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        published = item.findtext("pubDate")
        if not title or not published:
            continue
        try:
            day = msk_day(parsedate_to_datetime(published))
        except (TypeError, ValueError):
            continue
        if per_day.get(day, 0) >= NEWS_PER_FEED_PER_DAY:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        link = (item.findtext("link") or "").strip()
        rubric = (item.findtext("category") or "").strip()
        events.append(Event(id=f"news:{_hash(link or title)}", day=day, title=title,
                            category=f"Новости: {rubric}" if rubric else "Новости мира",
                            source="news", url=link))
    return events


def news_feeds(settings: dict) -> list[str]:
    raw = settings.get("news_feeds")
    return [url.strip() for url in str(DEFAULT_NEWS_FEEDS if raw is None else raw).split(",") if url.strip()]


def feed_fetcher(url: str) -> Callable[[httpx.AsyncClient, date], Awaitable[list[Event]]]:
    async def fetch(client: httpx.AsyncClient, today: date) -> list[Event]:
        response = await client.get(url)
        response.raise_for_status()
        return parse_rss(response.text, url)

    return fetch


async def fetch_warthunder(client: httpx.AsyncClient) -> list[Event]:
    response = await client.get(WT_NEWS_URL)
    response.raise_for_status()
    return parse_warthunder(response.text)


async def fetch_wiki(client: httpx.AsyncClient, today: date) -> list[Event]:
    response = await client.get(WIKI_API, params={"action": "parse", "page": WIKI_PAGE, "prop": "wikitext",
                                                  "format": "json", "formatversion": "2"})
    response.raise_for_status()
    return parse_wiki(response.json()["parse"]["wikitext"], today)


# --- хранилище и анализ ----------------------------------------------------------------------

@dataclass
class CategoryStat:
    category: str
    days: int
    on_day: float               # средний дневной индекс в дни события
    next_day: float | None      # на следующий день
    other: float                # в остальные дни


class EventBook:
    def __init__(self, data, settings: dict | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 fetchers: dict[str, Callable[..., Awaitable[list[Event]]]] | None = None):
        self.events = data.collection("events.json", Event, key="id")
        settings = settings or {}
        self.enabled = {source: bool(settings.get(f"events_{source}", True))
                        for source in ("warthunder", "wiki", "holiday", "news")}
        self.now = now
        if fetchers is None:
            # Ключ — «источник» или «источник адрес»: у каждой ленты новостей своя ошибка в панели
            fetchers = {"warthunder": lambda client, today: fetch_warthunder(client), "wiki": fetch_wiki}
            fetchers.update({f"news {urlparse(url).hostname}": feed_fetcher(url) for url in news_feeds(settings)})
        self.fetchers = fetchers
        self.last_refresh: datetime | None = None
        self.last_errors: dict[str, str] = {}

    def for_day(self, guild_id: int, day: date) -> list[Event]:
        return [e for e in self.events.all() if e.day == day and e.guild_id in (None, guild_id)
                and (e.source == "manual" or self.enabled.get(e.source, True))]

    def for_guild(self, guild_id: int) -> list[Event]:
        return sorted((e for e in self.events.all() if e.guild_id in (None, guild_id)
                       and (e.source == "manual" or self.enabled.get(e.source, True))),
                      key=lambda e: e.day, reverse=True)

    def add_manual(self, guild_id: int, day: date, title: str, category: str) -> Event:
        title = title.strip()
        return self.events.put(Event(id=f"manual:{guild_id}:{day.isoformat()}:{_hash(title)}", guild_id=guild_id,
                                     day=day, title=title, category=category.strip() or "Своё", source="manual"))

    def delete_manual(self, guild_id: int, event_id: str) -> bool:
        event = self.events.get(event_id)
        return event is not None and event.source == "manual" and event.guild_id == guild_id \
            and self.events.delete(event_id)

    async def refresh(self, force: bool = False) -> None:
        """Подтянуть автоматические события; не чаще раза в REFRESH_EVERY. Ошибка источника не мешает остальным."""
        if not force and self.last_refresh and self.now() - self.last_refresh < REFRESH_EVERY:
            return
        self.last_refresh = self.now()
        today = msk_day(self.now())
        found: list[Event] = holidays_around(today) if self.enabled["holiday"] else []
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            for source, fetch in self.fetchers.items():
                if not self.enabled.get(source.split()[0], True):
                    continue
                try:
                    found += await fetch(client, today)
                    self.last_errors.pop(source, None)
                except Exception as e:
                    self.last_errors[source] = f"{type(e).__name__}: {e}"
                    logger.warning(f"События {source} не загружены: {e}")
        known = {e.id for e in self.events.all()}
        fresh = [e for e in found if e.id not in known]
        if fresh:
            self.events.put_many(fresh)

    def analysis(self, guild_id: int, days) -> list[CategoryStat]:
        """По категориям: дневной индекс в день события и на следующий против остальных дней."""
        daily = {d.day: d.daily for d in days}
        if not daily:
            return []
        by_category: dict[str, set[date]] = {}
        for event in self.for_guild(guild_id):
            by_category.setdefault(event.category, set()).add(event.day)
        stats = []
        for category, event_days in by_category.items():
            on = [daily[d] for d in event_days if d in daily]
            if len(on) < MIN_DAYS_FOR_ANALYSIS:
                continue
            after = [daily[d + timedelta(days=1)] for d in event_days if d + timedelta(days=1) in daily]
            rest = [v for d, v in daily.items() if d not in event_days]
            if not rest:
                continue
            stats.append(CategoryStat(category=category, days=len(on), on_day=round(sum(on) / len(on), 1),
                                      next_day=round(sum(after) / len(after), 1) if after else None,
                                      other=round(sum(rest) / len(rest), 1)))
        return sorted(stats, key=lambda s: abs(s.on_day - s.other), reverse=True)
