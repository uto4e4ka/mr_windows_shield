"""Журнал дней: при закрытии дня (00:00 МСК) — одна запись: дата, дневной и общий индекс, связанные события.

Хранится в data/journal.json и пишется строкой в plugin.log. Записанный день не переписывается.
"""
import logging
from datetime import date, datetime, timezone
from typing import Callable

from pydantic import BaseModel, Field

from mr_windows_shield.events import EventBook, event_line, related_events
from mr_windows_shield.index import IndexBook
from mr_windows_shield.models import format_day, index_title

logger = logging.getLogger(__name__)


class JournalDay(BaseModel):
    id: str                         # "<guild>:<YYYY-MM-DD>"
    guild_id: int
    day: date
    daily: float
    overall: float | None
    events: list[str] = Field(default_factory=list)     # «✈️ Заголовок»
    written_at: datetime


class Journal:
    def __init__(self, data, book: IndexBook, events: EventBook,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.days = data.collection("journal.json", JournalDay, key="id")
        self.book = book
        self.events = events
        self.now = now

    def for_guild(self, guild_id: int) -> list[JournalDay]:
        return sorted((d for d in self.days.all() if d.guild_id == guild_id), key=lambda d: d.day, reverse=True)

    def sync(self, guild_id: int) -> list[JournalDay]:
        """Записать закрытые дни, которых ещё нет в журнале."""
        self.book.close_days(guild_id)
        written = []
        for record in sorted((d for d in self.book.days.all() if d.guild_id == guild_id), key=lambda d: d.day):
            if self.days.get(record.id) is not None:
                continue
            entry = self.days.put(JournalDay(
                id=record.id, guild_id=guild_id, day=record.day, daily=record.daily, overall=record.overall,
                events=[event_line(e) for e in related_events(self.events.for_day(guild_id, record.day))],
                written_at=self.now(),
            ))
            logger.info(f"Журнал {format_day(entry.day)} [guild={guild_id}]: дневной {index_title(entry.daily)}, "
                        f"общий {index_title(entry.overall)}; события: {'; '.join(entry.events) or 'нет'}")
            written.append(entry)
        return written

    async def tick(self) -> None:
        for state in self.book.states.all():
            self.sync(state.guild_id)
