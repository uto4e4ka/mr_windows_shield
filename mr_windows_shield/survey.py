"""Тест в личку: раз в N часов бот задаёт Доркину 4 вопроса, из ответов — Индекс Доркина (models.dorkin_index).

Вопросы идут по очереди в одном сообщении. Не прошёл до конца за час — «уклонился» (+5 к дневному индексу).
Ночью (00:00–09:00 МСК) не беспокоим. «Отписаться» выключает тест для него на этом сервере,
и админ из панели его обратно не включит. В общий чат ничего не уходит.
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel, Field
from potehinsonnet.discord_provider import DiscordProvider
from potehinsonnet.net import NatsRequestError
from potehinsonnet.net_models.discord_models import (
    ActionRow, ComponentButton, ComponentButtonStyle, Container, DiscordMessageEdit, ExecutedComponent,
    ExecutedComponentResponse, NatsMessage, TextDisplay,
)
from potehinsonnet.setup.button_registrator import ID_SEPARATOR, button
from potehinsonnet.storage import DataDir

from mr_windows_shield import config
from mr_windows_shield.index import IndexBook
from mr_windows_shield.models import MSK, QUESTIONS, TestResult, dorkin_index, index_title

ANSWER_TIME = timedelta(hours=1)
QUIET_HOURS = range(0, 9)               # часы МСК, когда тест не отправляется
DEFAULT_INTERVAL_HOURS = 6
TICK_SECONDS = 60
ANSWER_BUTTON = "dorkin_survey"
UNSUBSCRIBE_BUTTON = "dorkin_survey_off"
ANSWER_STYLES = [ComponentButtonStyle.SUCCESS, ComponentButtonStyle.SECONDARY, ComponentButtonStyle.PRIMARY,
                 ComponentButtonStyle.DANGER, ComponentButtonStyle.DANGER]

logger = logging.getLogger(__name__)


class SurveyTarget(BaseModel):
    """Кого опрашивать и за кем наблюдать на сервере (один человек на сервер)."""
    guild_id: int
    user_id: int
    user_name: str = "Доркин"            # обновляется по его ответам
    interval_hours: float = DEFAULT_INTERVAL_HOURS
    enabled: bool = True
    unsubscribed: bool = False           # нажал «Отписаться» — админ включить не может
    last_sent_at: datetime | None = None
    last_error: str = ""                 # например, закрытая личка — показывается в панели


class PendingSurvey(BaseModel):
    """Отправленный тест, который ещё не пройден до конца."""
    id: str
    guild_id: int
    user_id: int
    channel_id: int                      # канал лички — чтобы менять сообщение
    message_id: int
    sent_at: datetime
    answers: dict[str, int] = Field(default_factory=dict)

    @property
    def step(self) -> int:
        return len(self.answers)


def _card(text: str, *rows: ActionRow) -> list[Container]:
    return [Container(components=[TextDisplay(content=text), *rows])]


def _answers_text(answers: dict[str, int]) -> str:
    return "\n".join(f"{q.emoji} {q.text}: **{q.answers[answers[q.key] - 1]}** ({answers[q.key]}/5)"
                     for q in QUESTIONS if q.key in answers)


class Surveys:
    def __init__(self, data: DataDir, discord: DiscordProvider, book: IndexBook,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.targets = data.collection("survey_targets.json", SurveyTarget, key="guild_id")
        self.pending = data.collection("surveys_pending.json", PendingSurvey, key="id")
        self.discord = discord
        self.book = book
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
        """Когда уйдёт следующий тест (с учётом ночи); None — выключен."""
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
            self.book.close_days(target.guild_id)
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
            self._save(survey, target.user_name if target else "Доркин", completed=False)
            done = f"\n\nУспел ответить:\n{_answers_text(survey.answers)}" if survey.answers else ""
            await self._edit(survey, f"⌛ Время на тест вышло — записано как «уклонился от ответа».{done}")

    async def _send(self, target: SurveyTarget) -> None:
        survey_id = uuid.uuid4().hex
        sent_at = self.now()
        target.last_sent_at = sent_at
        try:
            response = await self.discord.send_direct_message(target.user_id, NatsMessage(
                service=config.PLUGIN_NAME, text="", containers=self._question(survey_id, {})))
        except NatsRequestError as e:
            # Закрытая личка и т.п.: попробуем через интервал, админ увидит причину в панели
            target.last_error = str(e)
            self.targets.put(target)
            logger.warning(f"Тест не отправлен [guild={target.guild_id}, user={target.user_id}]: {e}")
            return
        target.last_error = ""
        self.targets.put(target)
        self.pending.put(PendingSurvey(id=survey_id, guild_id=target.guild_id, user_id=target.user_id,
                                       channel_id=response.channel_id, message_id=response.message_id,
                                       sent_at=sent_at))

    @staticmethod
    def _question(survey_id: str, answers: dict[str, int]) -> list[Container]:
        step = len(answers)
        question = QUESTIONS[step]
        done = f"\n\n{_answers_text(answers)}" if answers else ""
        return _card(
            f"{config.PLUGIN_TITLE} · **Плановый тест** · вопрос {step + 1}/{len(QUESTIONS)}\n"
            f"{question.emoji} **{question.text}**\n"
            f"-# На весь тест — {int(ANSWER_TIME.total_seconds() // 3600)} ч., потом засчитается «уклонился».{done}",
            ActionRow(components=[
                ComponentButton(custom_id=ID_SEPARATOR.join([ANSWER_BUTTON, survey_id, str(step), str(value)]),
                                label=label, style=ANSWER_STYLES[value - 1])
                for value, label in enumerate(question.answers, start=1)
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
            logger.warning(f"Не удалось погасить тест {survey.id}: {e}")

    def _save(self, survey: PendingSurvey, user_name: str, completed: bool) -> TestResult:
        return self.book.tests.put(TestResult(
            id=uuid.uuid4().hex, guild_id=survey.guild_id, user_id=survey.user_id, user_name=user_name,
            answers=dict(survey.answers), index=dorkin_index(survey.answers) if completed else None,
            completed=completed, created_at=self.now(),
        ))

    # --- кнопки в личке ----------------------------------------------------------------------

    def _open_survey(self, executed: ExecutedComponent, survey_id: str) -> PendingSurvey | None:
        survey = self.pending.get(survey_id)
        if survey is None or survey.user_id != executed.user.id:
            return None
        return survey

    @button(ANSWER_BUTTON)
    async def answer(self, executed: ExecutedComponent) -> ExecutedComponentResponse | None:
        parts = executed.id.split(ID_SEPARATOR)
        try:
            survey_id, step, value = parts[1], int(parts[2]), int(parts[3])
            if not (0 <= step < len(QUESTIONS) and 1 <= value <= 5):
                raise ValueError
        except (IndexError, ValueError):
            return ExecutedComponentResponse(containers=_card("Непонятный ответ"), edit=True)

        survey = self._open_survey(executed, survey_id)
        if survey is None:
            return ExecutedComponentResponse(containers=_card("Этот тест уже закрыт."), edit=True)
        if step != survey.step:
            return None                     # двойное нажатие по старому вопросу — сообщение уже другое

        survey.answers[QUESTIONS[step].key] = value
        if survey.step < len(QUESTIONS):
            self.pending.put(survey)
            return ExecutedComponentResponse(containers=self._question(survey.id, survey.answers), edit=True)

        self.pending.delete(survey.id)
        name = executed.user.display_name or executed.user.name
        if target := self.targets.get(survey.guild_id):
            target.user_name = name
            self.targets.put(target)
        result = self._save(survey, name, completed=True)
        return ExecutedComponentResponse(edit=True, containers=_card(
            f"✅ **Тест пройден.** Индекс Доркина: **{index_title(result.index)}**\n\n{_answers_text(result.answers)}"))

    @button(UNSUBSCRIBE_BUTTON)
    async def unsubscribe(self, executed: ExecutedComponent) -> ExecutedComponentResponse:
        survey_id = executed.id.split(ID_SEPARATOR, 1)[1] if ID_SEPARATOR in executed.id else ""
        survey = self._open_survey(executed, survey_id)
        if survey is None:
            return ExecutedComponentResponse(containers=_card("Этот тест уже закрыт."), edit=True)
        self.pending.delete(survey.id)
        if target := self.targets.get(survey.guild_id):
            target.unsubscribed = True
            self.targets.put(target)
        return ExecutedComponentResponse(edit=True, containers=_card("🔕 Вы отписались от тестов. Больше не побеспокою."))

    # --- запуск ----------------------------------------------------------------------------

    @asynccontextmanager
    async def running(self):
        async def loop():
            while True:
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Ошибка в расписании тестов")
                await asyncio.sleep(TICK_SECONDS)

        task = asyncio.create_task(loop())
        try:
            yield self
        finally:
            task.cancel()
