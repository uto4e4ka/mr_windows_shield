"""Активность Доркина в сети и в войсе → дневной индекс.

Раз в минуту смотрим его текущее состояние (из событий бота + снимок раз в 10 минут) и добавляем минуту
в счётчики дня (МСК). Очки (в единицах индекса, вместе с наблюдением за чатом — не больше +25 за день):
  🎙 войс           +1 за каждые 30 мин (первые 2 мин захода не считаются), ночью 00–06 МСК — вдвое; до +10
  🟢 онлайн         +1 за каждые 2 ч в сети; до +5
  🌙 ночью в сети    +1 за каждые 30 мин в 00–06 МСК; до +5
  ⛔ «не беспокоить» +2, если 4 ч и больше за день
Ломка по War Thunder (отдельно от потолка): день без игры (меньше 30 мин) — +3 × номер дня подряд, до +15;
сыграл 30 мин — 0 и серия заново; 2 ч и больше — −3. Считается только когда бот видит статусы (интент Presence).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel
from potehinsonnet.net import NatsClient, NatsRequestError

from mr_windows_shield.models import MSK, msk_day

VOICE_EVENT = "discord.interaction.channel.voice.event.user.connection"
PRESENCE_EVENT = "discord.member.presence"
STATE_REQUEST = "discord.member.state.get"

TICK_SECONDS = 60
SNAPSHOT_EVERY = timedelta(minutes=10)
NIGHT_HOURS = range(0, 6)
VOICE_GRACE = timedelta(minutes=2)

VOICE_STEP, VOICE_CAP = 30, 10
ONLINE_STEP, ONLINE_CAP = 120, 5
NIGHT_STEP, NIGHT_CAP = 30, 5
DND_MINUTES, DND_POINTS = 240, 2
GAME_MIN, GAME_RELIEF, GAME_RELIEF_POINTS = 30, 120, -3
WITHDRAWAL_STEP, WITHDRAWAL_CAP = 3, 15
DEFAULT_GAMES = "war thunder, warthunder"

ACTIVITY_TITLES = {"voice": "🎙 войс", "online": "🟢 онлайн", "night": "🌙 ночью в сети", "dnd": "⛔ не беспокоить"}

logger = logging.getLogger(__name__)


class ActivityDay(BaseModel):
    """Минуты за сутки (МСК)."""
    id: str                         # "<guild>:<YYYY-MM-DD>"
    guild_id: int
    day: date
    voice: int = 0
    voice_night: int = 0
    online: int = 0
    online_night: int = 0
    dnd: int = 0
    game: int = 0                   # War Thunder (или что в настройке)
    presence_known: bool = False    # бот видел статусы в этот день — без этого ломку не считаем


def activity_points(day: ActivityDay | None) -> dict[str, int]:
    if day is None:
        return {}
    points = {
        "voice": min(VOICE_CAP, day.voice // VOICE_STEP + day.voice_night // VOICE_STEP),
        "online": min(ONLINE_CAP, day.online // ONLINE_STEP),
        "night": min(NIGHT_CAP, day.online_night // NIGHT_STEP),
        "dnd": DND_POINTS if day.dnd >= DND_MINUTES else 0,
    }
    return {k: v for k, v in points.items() if v}


def withdrawal(days: dict[date, ActivityDay], day: date) -> tuple[int, int]:
    """(очки ломки, какой день подряд без игры). 0 — играл или статусов не видно."""
    record = days.get(day)
    if record is None or not record.presence_known:
        return 0, 0
    if record.game >= GAME_RELIEF:
        return GAME_RELIEF_POINTS, 0
    if record.game >= GAME_MIN:
        return 0, 0
    streak = 0
    while (r := days.get(day - timedelta(days=streak))) is not None and r.presence_known and r.game < GAME_MIN:
        streak += 1
    return min(WITHDRAWAL_CAP, WITHDRAWAL_STEP * streak), streak


def format_minutes(minutes: int) -> str:
    return f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{minutes} мин"


def describe(day: ActivityDay | None, game_title: str = "War Thunder") -> str:
    if day is None:
        return ""
    parts = [f"🎙 войс {format_minutes(day.voice)}", f"🟢 онлайн {format_minutes(day.online)}"]
    if day.online_night:
        parts.append(f"🌙 ночью {format_minutes(day.online_night)}")
    if day.presence_known:
        parts.append(f"✈️ {game_title} {format_minutes(day.game)}")
    return " · ".join(parts)


@dataclass
class Live:
    """Что сейчас у наблюдаемого (в памяти)."""
    user_id: int
    status: str | None = None
    activities: list[str] = field(default_factory=list)
    voice_since: datetime | None = None
    presence_known: bool = False
    snapshot_at: datetime | None = None


class ActivityTracker:
    def __init__(self, nats: NatsClient, book, surveys, games: str = DEFAULT_GAMES,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.nats = nats
        self.book = book
        self.targets = surveys.targets
        self.games = [g.strip().lower() for g in games.split(",") if g.strip()]
        self.now = now
        self.live: dict[int, Live] = {}             # guild_id → состояние
        self.last_error = ""

    def _live(self, guild_id: int) -> Live | None:
        target = self.targets.get(guild_id)
        if target is None:
            self.live.pop(guild_id, None)
            return None
        live = self.live.get(guild_id)
        if live is None or live.user_id != target.user_id:
            live = self.live[guild_id] = Live(user_id=target.user_id)
        return live

    def playing(self, live: Live) -> bool:
        return any(game in name.lower() for name in live.activities for game in self.games)

    # --- события бота -----------------------------------------------------------------------

    async def on_voice(self, body: dict) -> None:
        guild_id = (body.get("guild") or {}).get("id")
        live = self._live(guild_id) if guild_id else None
        if live is None or (body.get("user") or {}).get("id") != live.user_id:
            return
        if body.get("after_channel"):
            live.voice_since = live.voice_since or self.now()       # переход между каналами — тот же заход
        else:
            live.voice_since = None

    async def on_presence(self, body: dict) -> None:
        live = self._live(body.get("guild_id") or 0)
        if live is None or (body.get("user") or {}).get("id") != live.user_id:
            return
        live.status = body.get("status")
        live.activities = list(body.get("activities") or [])
        live.presence_known = True

    async def snapshot(self, guild_id: int, live: Live) -> None:
        try:
            state = await self.nats.request(STATE_REQUEST, {"guild_id": guild_id, "user_id": live.user_id})
        except (NatsRequestError, asyncio.TimeoutError, Exception) as e:     # бот старый / недоступен
            if self.last_error != str(e):
                logger.warning(f"Снимок активности не получен: {e!r}")
            self.last_error = str(e) or type(e).__name__
            live.snapshot_at = self.now()
            return
        self.last_error = ""
        live.snapshot_at = self.now()
        live.presence_known = bool(state.get("presence_known"))
        if live.presence_known:
            live.status = state.get("status")
            live.activities = list(state.get("activities") or [])
        if state.get("voice_channel_id"):
            live.voice_since = live.voice_since or self.now()
        else:
            live.voice_since = None

    # --- поминутный учёт ----------------------------------------------------------------------

    def record(self, guild_id: int, day: date) -> ActivityDay:
        key = f"{guild_id}:{day.isoformat()}"
        return self.book.activity.get(key) or ActivityDay(id=key, guild_id=guild_id, day=day)

    def count_minute(self, guild_id: int, live: Live) -> None:
        now = self.now()
        record = self.record(guild_id, msk_day(now))
        night = now.astimezone(MSK).hour in NIGHT_HOURS
        if live.voice_since is not None and now - live.voice_since >= VOICE_GRACE:
            record.voice += 1
            record.voice_night += night
        if live.presence_known:
            record.presence_known = True
            if live.status and live.status != "offline":
                record.online += 1
                record.online_night += night
                record.dnd += live.status == "dnd"
            if self.playing(live):
                record.game += 1
        self.book.activity.put(record)

    async def tick(self) -> None:
        for target in self.targets.all():
            live = self._live(target.guild_id)
            if live.snapshot_at is None or self.now() - live.snapshot_at >= SNAPSHOT_EVERY:
                await self.snapshot(target.guild_id, live)
            self.count_minute(target.guild_id, live)

    @asynccontextmanager
    async def running(self):
        subs = [await self.nats.subscribe(VOICE_EVENT, self.on_voice),
                await self.nats.subscribe(PRESENCE_EVENT, self.on_presence)]

        async def loop():
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Ошибка учёта активности")
                await asyncio.sleep(TICK_SECONDS)

        task = asyncio.create_task(loop())
        try:
            yield self
        finally:
            task.cancel()
            for sub in subs:
                await sub.unsubscribe()
