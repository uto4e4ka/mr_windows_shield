"""Наблюдение за чатом: сообщения Доркина на сервере приносят очки, очки за сутки двигают дневной индекс."""
import re
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from potehinsonnet.net import NatsClient
from potehinsonnet.net_models.discord_models import DiscordMessage

from mr_windows_shield.index import IndexBook, day_id
from mr_windows_shield.models import DayObservation, msk_day

TOPIC_POINTS = 3
CAPS_POINTS, CAPS_MIN_LETTERS = 2, 10
SHOUT_POINTS = 1
LONG_POINTS, LONG_LENGTH = 2, 500
BURST_WINDOW, BURST_FREE = timedelta(minutes=1), 5      # сверх 5 сообщений в минуту — +1 за каждое

DEFAULT_TOPICS = {
    "airfryer": "аэрогрил, аэрофритюр, airfryer, air fryer",
    "warthunder": "вартандер, вар тандер, war thunder, warthunder, тундр, wt, танк, ангар",
    "women": "женщин, девушк, девочк, тян, тянк, тяночк, баба, бабы, бабу, бабе, бабам, телк",
}
TOPIC_TITLES = {"airfryer": "🍗 аэрогриль", "warthunder": "✈️ War Thunder", "women": "👀 женщины",
                "caps": "КАПС", "shout": "!!! / ???", "long": "простыня", "burst": "флуд"}


@dataclass(frozen=True)
class Topic:
    key: str
    pattern: re.Pattern


def topic_pattern(words: str) -> re.Pattern:
    """Слова через запятую. Короткие (до 3 букв: wt, тян) — только целым словом, длинные — как начало слова."""
    parts = []
    for word in (w.strip().lower() for w in words.split(",")):
        if word:
            escaped = re.escape(word)
            parts.append(rf"\b{escaped}\b" if len(word) <= 3 else rf"\b{escaped}")
    return re.compile("|".join(parts) or r"(?!)", re.IGNORECASE)


def build_topics(settings: dict) -> list[Topic]:
    return [Topic(key, topic_pattern(str(settings.get(f"watch_{key}") or default)))
            for key, default in DEFAULT_TOPICS.items()]


def score_text(text: str, topics: list[Topic]) -> dict[str, int]:
    """Очки за одно сообщение по признакам (без флуда — он считается по времени)."""
    reasons: dict[str, int] = {}
    for topic in topics:
        if topic.pattern.search(text):
            reasons[topic.key] = TOPIC_POINTS
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= CAPS_MIN_LETTERS and sum(c.isupper() for c in letters) > len(letters) / 2:
        reasons["caps"] = CAPS_POINTS
    if "!!!" in text or "???" in text:
        reasons["shout"] = SHOUT_POINTS
    if len(text) > LONG_LENGTH:
        reasons["long"] = LONG_POINTS
    return reasons


class ChatObserver:
    def __init__(self, nats: NatsClient, book: IndexBook, targets, topics: list[Topic],
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.nats = nats
        self.book = book
        self.targets = targets              # кого наблюдаем — тот же, кого опрашиваем (Surveys.targets)
        self.topics = topics
        self.now = now
        self._recent: dict[tuple[int, int], deque[datetime]] = defaultdict(deque)

    def _burst(self, guild_id: int, user_id: int) -> int:
        now = self.now()
        recent = self._recent[(guild_id, user_id)]
        recent.append(now)
        while recent and now - recent[0] > BURST_WINDOW:
            recent.popleft()
        return 1 if len(recent) > BURST_FREE else 0

    async def on_message(self, body: dict) -> None:
        msg = DiscordMessage.model_validate(body)
        if msg.guild_id is None:
            return                          # личка не в счёт
        target = self.targets.get(msg.guild_id)
        if target is None or target.user_id != msg.user.id:
            return

        reasons = score_text(msg.text, self.topics)
        if burst := self._burst(msg.guild_id, msg.user.id):
            reasons["burst"] = burst
        if not reasons:
            return

        day = msk_day(self.now())
        key = day_id(msg.guild_id, day)
        record = self.book.observations.get(key) or DayObservation(id=key, guild_id=msg.guild_id, day=day)
        record.points += sum(reasons.values())
        for reason, points in reasons.items():
            record.reasons[reason] = record.reasons.get(reason, 0) + points
        self.book.observations.put(record)

    @asynccontextmanager
    async def running(self):
        sub = await self.nats.subscribe("discord.message.get", self.on_message)
        try:
            yield self
        finally:
            await sub.unsubscribe()
