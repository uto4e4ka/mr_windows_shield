"""Опрос в личку: раз в N часов бот спрашивает Доркина о степени выделения, ответ — точка на графике.

Не ответил за час — записывается «уклонился». Ночью (00:00–09:00 МСК) не беспокоим.
Кнопка «Отписаться» выключает опросы для него на этом сервере, и админ из панели их обратно не включит.
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel
from potehinsonnet.discord_provider import DiscordProvider
from potehinsonnet.net import NatsRequestError
from potehinsonnet.net_models.discord_models import (
    ActionRow, ComponentButton, ComponentButtonStyle, Container, DiscordMessageEdit, ExecutedComponent,
    ExecutedComponentResponse, NatsMessage, TextDisplay,
)
from potehinsonnet.setup.button_registrator import ID_SEPARATOR, button
from potehinsonnet.storage import DataDir

from mr_windows_shield import config
from mr_windows_shield.models import LEVELS, MISSED_LEVEL, MSK, Report, format_time, level_title

ANSWER_TIME = timedelta(hours=1)
QUIET_HOURS = range(0, 9)               # часы МСК, когда опрос не отправляется
DEFAULT_INTERVAL_HOURS = 6
TICK_SECONDS = 60
ANSWER_BUTTON = "dorkin_survey"
UNSUBSCRIBE_BUTTON = "dorkin_survey_off"

logger = logging.getLogger(__name__)


class SurveyTarget(BaseModel):
    """Кого опрашивать на сервере (один человек на сервер)."""
    guild_id: int
    user_id: int
    user_name: str = "Доркин"            # обновляется по его ответам
    interval_hours: float = DEFAULT_INTERVAL_HOURS
    enabled: bool = True
    unsubscribed: bool = False           # нажал «Отписаться» — админ включить не может
    last_sent_at: datetime | None = None
    last_error: str = ""                 # например, закрытая личка — показывается в панели


class PendingSurvey(BaseModel):
    """Отправленный опрос, на который ещё не ответили."""
    id: str
    guild_id: int
    user_id: int
    channel_id: int                      # канал лички — чтобы погасить кнопки
    message_id: int
    sent_at: datetime


def _card(text: str, *rows: ActionRow) -> list[Container]:
    return [Container(components=[TextDisplay(content=text), *rows])]


class Surveys:
    def __init__(self, data: DataDir, discord: DiscordProvider, reports,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.targets = data.collection("survey_targets.json", SurveyTarget, key="guild_id")
        self.pending = data.collection("surveys_pending.json", PendingSurvey, key="id")
        self.discord = discord
        self.reports = reports.reports       # общая коллекция репортов: ответы попадают на тот же график
        self.now = now

    # --- настройка из панели ---------------------------------------------------------------

    def configure(self, guild_id: int, user_id: int | None, interval_hours: float, enabled: bool) -> SurveyTarget | None:
        current = self.targets.get(guild_id)
        if user_id is None:
            self.targets.delete(guild_id)
            return None
        if current is None or current.user_id != user_id:
            # Новый человек: старые настройки и отписка относились к другому
            current = SurveyTarget(guild_id=guild_id, user_id=user_id)
        current.interval_hours = max(1.0, float(interval_hours or DEFAULT_INTERVAL_HOURS))
        current.enabled = enabled
        return self.targets.put(current)

    def next_send_at(self, target: SurveyTarget) -> datetime | None:
        """Когда уйдёт следующий опрос (с учётом ночи); None — опросы выключены."""
        if not target.enabled or target.unsubscribed:
            return None
        due = self.now() if target.last_sent_at is None else max(
            self.now(), target.last_sent_at + timedelta(hours=target.interval_hours))
        local = due.astimezone(MSK)
        if local.hour in QUIET_HOURS:
            local = local.replace(hour=QUIET_HOURS.stop, minute=0, second=0, microsecond=0)
        return local.astimezone(timezone.utc)

    # --- расписание ------------------------------------------------------------------------

    async def tick(self) -> None:
        await self._expire()
        for target in self.targets.all():
            when = self.next_send_at(target)
            busy = any(p.guild_id == target.guild_id for p in self.pending.all())
            if when is not None and when <= self.now() and not busy:
                await self._send(target)

    async def _expire(self) -> None:
        for survey in self.pending.all():
            if survey.sent_at + ANSWER_TIME > self.now():
                continue
            self.pending.delete(survey.id)
            target = self.targets.get(survey.guild_id)
            self._record(survey, MISSED_LEVEL, target.user_name if target else "Доркин", source="missed")
            await self._edit(survey, "⌛ Время на ответ вышло — записано как «уклонился от ответа».")

    async def _send(self, target: SurveyTarget) -> None:
        survey_id = uuid.uuid4().hex
        sent_at = self.now()
        target.last_sent_at = sent_at
        try:
            response = await self.discord.send_direct_message(target.user_id, NatsMessage(
                service=config.PLUGIN_NAME, text="", containers=self._question(survey_id)))
        except NatsRequestError as e:
            # Закрытая личка и т.п.: попробуем через интервал, админ увидит причину в панели
            target.last_error = str(e)
            self.targets.put(target)
            logger.warning(f"Опрос не отправлен [guild={target.guild_id}, user={target.user_id}]: {e}")
            return
        target.last_error = ""
        self.targets.put(target)
        self.pending.put(PendingSurvey(id=survey_id, guild_id=target.guild_id, user_id=target.user_id,
                                       channel_id=response.channel_id, message_id=response.message_id,
                                       sent_at=sent_at))

    @staticmethod
    def _question(survey_id: str) -> list[Container]:
        return _card(
            f"{config.PLUGIN_TITLE}\n**Плановый опрос.** Оцените свою текущую степень выделения.\n"
            f"-# На ответ — {int(ANSWER_TIME.total_seconds() // 3600)} ч., потом засчитается «уклонился».",
            ActionRow(components=[
                ComponentButton(custom_id=f"{ANSWER_BUTTON}{ID_SEPARATOR}{survey_id}{ID_SEPARATOR}{number}",
                                label=level.label, emoji=level.emoji, style=level.style)
                for number, level in enumerate(LEVELS, start=1)
            ]),
            ActionRow(components=[
                ComponentButton(custom_id=f"{UNSUBSCRIBE_BUTTON}{ID_SEPARATOR}{survey_id}", label="Отписаться",
                                emoji="🔕", style=ComponentButtonStyle.SECONDARY),
            ]),
        )

    async def _edit(self, survey: PendingSurvey, text: str) -> None:
        try:
            await self.discord.update_message(DiscordMessageEdit(message_id=survey.message_id, message=NatsMessage(
                service=config.PLUGIN_NAME, text="", channel_id=survey.channel_id, containers=_card(text))))
        except NatsRequestError as e:
            logger.warning(f"Не удалось погасить опрос {survey.id}: {e}")

    def _record(self, survey: PendingSurvey, level: int, user_name: str, source: str) -> Report:
        return self.reports.put(Report(
            id=uuid.uuid4().hex, guild_id=survey.guild_id, channel_id=survey.channel_id, user_id=survey.user_id,
            user_name=user_name, level=level, created_at=self.now(), source=source,
        ))

    # --- кнопки в личке ----------------------------------------------------------------------

    def _open_survey(self, executed: ExecutedComponent, survey_id: str) -> PendingSurvey | None:
        survey = self.pending.get(survey_id)
        if survey is None or survey.user_id != executed.user.id:
            return None
        return survey

    @button(ANSWER_BUTTON)
    async def answer(self, executed: ExecutedComponent) -> ExecutedComponentResponse:
        parts = executed.id.split(ID_SEPARATOR)
        try:
            survey_id, level = parts[1], int(parts[2])
            if not 1 <= level <= len(LEVELS):
                raise ValueError
        except (IndexError, ValueError):
            return ExecutedComponentResponse(containers=_card("Неизвестная степень выделения"), edit=True)

        survey = self._open_survey(executed, survey_id)
        if survey is None:
            return ExecutedComponentResponse(containers=_card("Этот опрос уже закрыт."), edit=True)

        self.pending.delete(survey.id)
        name = executed.user.display_name or executed.user.name
        if target := self.targets.get(survey.guild_id):
            target.user_name = name
            self.targets.put(target)
        report = self._record(survey, level, name, source="survey")
        return ExecutedComponentResponse(edit=True, containers=_card(
            f"✅ Записано: {level_title(level)} · {format_time(report.created_at)}\nСпасибо за честность."))

    @button(UNSUBSCRIBE_BUTTON)
    async def unsubscribe(self, executed: ExecutedComponent) -> ExecutedComponentResponse:
        survey_id = executed.id.split(ID_SEPARATOR, 1)[1] if ID_SEPARATOR in executed.id else ""
        survey = self._open_survey(executed, survey_id)
        if survey is None:
            return ExecutedComponentResponse(containers=_card("Этот опрос уже закрыт."), edit=True)
        self.pending.delete(survey.id)
        if target := self.targets.get(survey.guild_id):
            target.unsubscribed = True
            self.targets.put(target)
        return ExecutedComponentResponse(edit=True, containers=_card("🔕 Вы отписались от опросов. Больше не побеспокою."))

    # --- запуск ----------------------------------------------------------------------------

    @asynccontextmanager
    async def running(self):
        async def loop():
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Ошибка в расписании опросов")
                await asyncio.sleep(TICK_SECONDS)

        task = asyncio.create_task(loop())
        try:
            yield self
        finally:
            task.cancel()
