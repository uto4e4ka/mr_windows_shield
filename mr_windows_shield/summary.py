"""Утренняя сводка: в 10:00 МСК в выбранный канал — итоги вчерашнего дня, общий индекс, события и график за неделю."""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel
from potehinsonnet.discord_provider import DiscordProvider
from potehinsonnet.net_models.discord_models import (
    Attachment, Container, MediaGallery, MediaItem, NatsMessage, TextDisplay,
)

from mr_windows_shield import config
from mr_windows_shield.chart import PERIODS, render_chart
from mr_windows_shield.events import SOURCES, EventBook
from mr_windows_shield.index import IndexBook, day_id
from mr_windows_shield.models import (
    MSK, QUESTIONS, day_start, format_day, index_title, msk_day,
)
from mr_windows_shield.observe import TOPIC_TITLES

SUMMARY_HOUR = 10               # МСК
SUMMARY_CHART = "dorkin_summary.png"
WORLD_SHOWN, WORLD_LENGTH = 2, 140
NEWS_SHOWN = 3
WT_SHOWN = 3                   # новостей War Thunder в сводке

logger = logging.getLogger(__name__)


class SummaryConfig(BaseModel):
    guild_id: int
    channel_id: int | None = None
    last_sent_day: date | None = None   # день (МСК), когда сводка уже ушла


def short(title: str) -> str:
    return title if len(title) <= WORLD_LENGTH else title[:WORLD_LENGTH - 1] + "…"


def delta(now: float | None, before: float | None) -> str:
    if now is None or before is None:
        return ""
    change = round(now - before, 1)
    arrow = "▲" if change > 0 else "▼" if change < 0 else "="
    return f" ({arrow} {change:+g})" if change else " (= без изменений)"


class Summaries:
    def __init__(self, data, discord: DiscordProvider, book: IndexBook, events: EventBook,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.configs = data.collection("summary_config.json", SummaryConfig, key="guild_id")
        self.discord = discord
        self.book = book
        self.events = events
        self.now = now

    def channel(self, guild_id: int) -> int | None:
        cfg = self.configs.get(guild_id)
        return cfg.channel_id if cfg else None

    def set_channel(self, guild_id: int, channel_id: int | None) -> None:
        cfg = self.configs.get(guild_id) or SummaryConfig(guild_id=guild_id)
        cfg.channel_id = channel_id
        self.configs.put(cfg)

    def text(self, guild_id: int, day: date) -> str:
        book = self.book
        record = book.days.get(day_id(guild_id, day))
        before = book.days.get(day_id(guild_id, day - timedelta(days=1)))
        lines = [f"📊 **Сводка Индекса Доркина за {format_day(day)}**"]
        if record:
            lines.append(f"Дневной: **{index_title(record.daily)}**{delta(record.daily, before.daily if before else None)}")
            lines.append(f"Общий: **{index_title(record.overall)}**"
                         f"{delta(record.overall, before.overall if before else None)}")
        else:
            lines.append("Дневной: нет данных")
            lines.append(f"Общий: **{index_title(book.state(guild_id).overall)}**")

        start, end = day_start(day), day_start(day + timedelta(days=1))
        tests = [t for t in book.tests.all() if t.guild_id == guild_id and start <= t.created_at < end]
        done = [t for t in tests if t.completed]
        reports = [r for r in book.reports.all() if r.guild_id == guild_id and start <= r.created_at < end]
        seen = book.observations.get(day_id(guild_id, day))
        why = ", ".join(f"{TOPIC_TITLES.get(k, k)} {v}" for k, v in sorted(
            (seen.reasons if seen else {}).items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"Тесты: {len(done)} пройдено, {len(tests) - len(done)} уклонился · Репорты: {len(reports)}"
                     f" · Наблюдение: {seen.points if seen else 0} очк." + (f" ({why})" if why else ""))
        if done:
            averages = " · ".join(
                f"{q.emoji} {sum(t.answers.get(q.key, 0) for t in done) / len(done):.1f}" for q in QUESTIONS)
            lines.append(f"Ответы (среднее): {averages}")

        # Связанные события одним списком: свои и праздники, War Thunder, новости мира, Википедия
        day_events = self.events.for_day(guild_id, day)
        related = [e for e in day_events if e.source in ("manual", "holiday")]
        related += [e for e in day_events if e.source == "warthunder"][:WT_SHOWN]
        related += [e for e in day_events if e.source == "news"][:NEWS_SHOWN]
        related += [e for e in day_events if e.source == "wiki"][:WORLD_SHOWN]
        if related:
            lines.append("🔗 **Связанные события:**")
            lines += [f"{SOURCES[e.source].split()[0]} {short(e.title)}" for e in related]
        return "\n".join(lines)

    async def send(self, guild_id: int, day: date | None = None) -> str:
        channel_id = self.channel(guild_id)
        if not channel_id:
            return "Сначала выберите канал для сводки"
        self.book.close_days(guild_id)
        day = day or msk_day(self.now()) - timedelta(days=1)
        png = render_chart(self.book, guild_id, PERIODS["week"], self.now(), events=self.events)
        await self.discord.send_message(NatsMessage(
            service=config.PLUGIN_NAME, text="", channel_id=channel_id,
            files=[Attachment.from_bytes(png, SUMMARY_CHART, description="Индекс Доркина за неделю")],
            containers=[Container(components=[
                TextDisplay(content=self.text(guild_id, day)),
                MediaGallery(items=[MediaItem(url=f"attachment://{SUMMARY_CHART}")]),
            ])],
        ))
        return "Сводка отправлена"

    async def tick(self) -> None:
        now = self.now().astimezone(MSK)
        if now.hour < SUMMARY_HOUR:
            return
        for cfg in self.configs.all():
            if not cfg.channel_id or cfg.last_sent_day == now.date():
                continue
            cfg.last_sent_day = now.date()      # даже при ошибке — не долбить канал каждую минуту
            self.configs.put(cfg)
            try:
                await self.send(cfg.guild_id)
            except Exception:
                logger.exception(f"Сводка не отправлена [guild={cfg.guild_id}]")
