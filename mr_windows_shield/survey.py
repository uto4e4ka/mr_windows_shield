"""Тест в личку: раз в N часов бот задаёт Доркину 4 вопроса, из ответов — Индекс Доркина (models.dorkin_index).

Вопросы идут по очереди в одном сообщении. Не прошёл до конца за час — «уклонился» (+5 к дневному индексу).
Куда слать — Discord-личка, Telegram или оба (выбор в панели); ответить можно в любом месте,
оба сообщения показывают одно и то же. Ночью (00:00–09:00 МСК) не беспокоим. «Отписаться» выключает тест для него на этом сервере,
и админ из панели его обратно не включит. В общий чат ничего не уходит.
"""
import asyncio
import logging
import secrets
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
from mr_windows_shield.telegram import TelegramBot, TelegramError, TgButton, TgUpdate

ANSWER_TIME = timedelta(hours=1)
QUIET_HOURS = range(0, 9)               # часы МСК, когда тест не отправляется
DEFAULT_INTERVAL_HOURS = 6
TICK_SECONDS = 60
ANSWER_BUTTON = "dorkin_survey"
UNSUBSCRIBE_BUTTON = "dorkin_survey_off"
ANSWER_STYLES = [ComponentButtonStyle.SUCCESS, ComponentButtonStyle.SECONDARY, ComponentButtonStyle.PRIMARY,
                 ComponentButtonStyle.DANGER, ComponentButtonStyle.DANGER]

DELIVERY = {"discord": "Discord (личка)", "telegram": "Telegram", "both": "Discord и Telegram"}
PLATFORM = {"discord": "Discord", "telegram": "Telegram"}

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
    delivery: str = "discord"            # ключ DELIVERY
    tg_chat_id: int | None = None        # привязанный Telegram (после Start по ссылке)
    tg_name: str = ""
    tg_code: str = Field(default_factory=lambda: secrets.token_urlsafe(9))

    @property
    def platforms(self) -> list[str]:
        return ["discord", "telegram"] if self.delivery == "both" else [self.delivery]


class PendingSurvey(BaseModel):
    """Отправленный тест, который ещё не пройден до конца."""
    id: str
    guild_id: int
    user_id: int
    channel_id: int | None = None        # канал лички Discord — чтобы менять сообщение
    message_id: int | None = None
    tg_chat_id: int | None = None
    tg_message_id: int | None = None
    sent_at: datetime
    answers: dict[str, int] = Field(default_factory=dict)

    @property
    def step(self) -> int:
        return len(self.answers)


def _card(text: str, *rows: ActionRow) -> list[Container]:
    return [Container(components=[TextDisplay(content=text), *rows])]


Screen = tuple[str, list[list[ComponentButton]]]       # текст и ряды кнопок — общий вид для Discord и Telegram


def _discord(screen: Screen) -> list[Container]:
    text, rows = screen
    return _card(text, *[ActionRow(components=row) for row in rows])


def _telegram(screen: Screen) -> tuple[str, list[list[TgButton]]]:
    text, rows = screen
    return text, [[TgButton(label=f"{b.emoji} {b.label}" if b.emoji else b.label, data=b.custom_id) for b in row]
                  for row in rows]


def _answers_text(answers: dict[str, int]) -> str:
    return "\n".join(f"{q.emoji} {q.text}: **{q.answers[answers[q.key] - 1]}** ({answers[q.key]}/5)"
                     for q in QUESTIONS if q.key in answers)


class Surveys:
    def __init__(self, data: DataDir, discord: DiscordProvider, book: IndexBook,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), telegram: TelegramBot | None = None):
        self.targets = data.collection("survey_targets.json", SurveyTarget, key="guild_id")
        self.pending = data.collection("surveys_pending.json", PendingSurvey, key="id")
        self.discord = discord
        self.book = book
        self.now = now
        self.telegram = telegram
        if telegram:
            telegram.handlers.append(self.on_telegram)

    # --- настройка из панели ---------------------------------------------------------------

    def configure(self, guild_id: int, user_id: int | None, interval_hours: float, enabled: bool,
                  delivery: str | None = None) -> SurveyTarget | None:
        current = self.targets.get(guild_id)
        if user_id is None:
            self.targets.delete(guild_id)
            return None
        if current is None or current.user_id != user_id:
            # Новый человек: старые настройки и отписка относились к другому
            current = SurveyTarget(guild_id=guild_id, user_id=user_id)
        current.interval_hours = max(1.0, float(interval_hours or DEFAULT_INTERVAL_HOURS))
        current.enabled = enabled
        if delivery in DELIVERY:
            current.delivery = delivery
        return self.targets.put(current)

    def unlink_telegram(self, guild_id: int) -> None:
        if target := self.targets.get(guild_id):
            target.tg_chat_id, target.tg_name = None, ""
            target.tg_code = secrets.token_urlsafe(9)        # старая ссылка больше не привяжет
            self.targets.put(target)

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

    async def send_now(self, guild_id: int) -> str:
        """Кнопка в панели: отправить тест сразу, не дожидаясь расписания (и ночью тоже — решает админ)."""
        target = self.targets.get(guild_id)
        if target is None:
            return "Сначала выберите, кого опрашивать"
        if target.unsubscribed:
            return f"{target.user_name} отписался от тестов — бот ему писать не будет"
        await self._expire()                # тест прежнего человека или просроченный — закрыть до проверки
        if any(p.guild_id == guild_id for p in self.pending.all()):
            return "Предыдущий тест ещё не пройден — дождитесь ответа или часа на него"
        await self._send(target)
        target = self.targets.get(guild_id)
        survey = next((p for p in self.pending.all() if p.guild_id == guild_id), None)
        if survey is None:
            raise RuntimeError(f"Не отправлено: {target.last_error}")
        where = [PLATFORM[k] for k, ok in (("discord", survey.message_id), ("telegram", survey.tg_message_id)) if ok]
        sent = "Тест отправлен: " + ", ".join(where)
        return f"{sent}. Ошибка: {target.last_error}" if target.last_error else sent

    async def _expire(self) -> None:
        for survey in self.pending.all():
            target = self.targets.get(survey.guild_id)
            if target is None or target.user_id != survey.user_id:
                # В панели выбрали другого человека (или никого): его тест отменяется, «уклонился» не пишем
                self.pending.delete(survey.id)
                await self._show(survey, ("🚫 Тест отменён.", []))
                continue
            if survey.sent_at + ANSWER_TIME > self.now():
                continue
            self.pending.delete(survey.id)
            self._save(survey, target.user_name if target else "Доркин", completed=False)
            done = f"\n\nУспел ответить:\n{_answers_text(survey.answers)}" if survey.answers else ""
            await self._show(survey, (f"⌛ Время на тест вышло — записано как «уклонился от ответа».{done}", []))

    async def _send(self, target: SurveyTarget) -> None:
        survey_id = uuid.uuid4().hex
        sent_at = self.now()
        target.last_sent_at = sent_at
        survey = PendingSurvey(id=survey_id, guild_id=target.guild_id, user_id=target.user_id, sent_at=sent_at)
        screen = self._question(survey_id, {})
        errors = []
        if "discord" in target.platforms:
            try:
                response = await self.discord.send_direct_message(target.user_id, NatsMessage(
                    service=config.PLUGIN_NAME, text="", containers=_discord(screen)))
                survey.channel_id, survey.message_id = response.channel_id, response.message_id
            except NatsRequestError as e:
                errors.append(f"Discord: {e}")
        if "telegram" in target.platforms:
            if self.telegram is None:
                errors.append("Telegram: не указан токен бота в настройках плагина")
            elif target.tg_chat_id is None:
                errors.append("Telegram: не привязан — пусть нажмёт Start по ссылке из панели")
            else:
                try:
                    survey.tg_chat_id = target.tg_chat_id
                    survey.tg_message_id = await self.telegram.send(target.tg_chat_id, *_telegram(screen))
                except TelegramError as e:
                    errors.append(f"Telegram: {e}")
        # Не дошло никуда — попробуем через интервал; админ увидит причину в панели
        target.last_error = "; ".join(errors)
        self.targets.put(target)
        if errors:
            logger.warning(f"Тест [guild={target.guild_id}, user={target.user_id}]: {target.last_error}")
        if survey.message_id or survey.tg_message_id:
            self.pending.put(survey)

    @staticmethod
    def _question(survey_id: str, answers: dict[str, int]) -> Screen:
        step = len(answers)
        question = QUESTIONS[step]
        done = f"\n\n{_answers_text(answers)}" if answers else ""
        return (
            f"{config.PLUGIN_TITLE} · **Плановый тест** · вопрос {step + 1}/{len(QUESTIONS)}\n"
            f"{question.emoji} **{question.text}**\n"
            f"-# На весь тест — {int(ANSWER_TIME.total_seconds() // 3600)} ч., потом засчитается «уклонился».{done}",
            [
                [ComponentButton(custom_id=ID_SEPARATOR.join([ANSWER_BUTTON, survey_id, str(step), str(value)]),
                                 label=label, style=ANSWER_STYLES[value - 1])
                 for value, label in enumerate(question.answers, start=1)],
                [ComponentButton(custom_id=f"{UNSUBSCRIBE_BUTTON}{ID_SEPARATOR}{survey_id}", label="Отписаться",
                                 emoji="🔕", style=ComponentButtonStyle.SECONDARY)],
            ],
        )

    async def _show(self, survey: PendingSurvey, screen: Screen, skip: str = "") -> None:
        """Показать экран в обоих сообщениях теста (кроме skip — там его вернёт ответ на нажатие)."""
        if survey.message_id and skip != "discord":
            try:
                await self.discord.update_message(DiscordMessageEdit(message_id=survey.message_id, message=NatsMessage(
                    service=config.PLUGIN_NAME, text="", channel_id=survey.channel_id, containers=_discord(screen))))
            except NatsRequestError as e:
                logger.warning(f"Не удалось обновить тест {survey.id} в Discord: {e}")
        if survey.tg_message_id and skip != "telegram" and self.telegram:
            try:
                await self.telegram.edit(survey.tg_chat_id, survey.tg_message_id, *_telegram(screen))
            except TelegramError as e:
                logger.warning(f"Не удалось обновить тест {survey.id} в Telegram: {e}")

    def _save(self, survey: PendingSurvey, user_name: str, completed: bool) -> TestResult:
        return self.book.tests.put(TestResult(
            id=uuid.uuid4().hex, guild_id=survey.guild_id, user_id=survey.user_id, user_name=user_name,
            answers=dict(survey.answers), index=dorkin_index(survey.answers) if completed else None,
            completed=completed, created_at=self.now(),
        ))

    # --- кнопки (Discord-личка и Telegram) ------------------------------------------------

    async def press(self, data: str, survey: PendingSurvey | None, name: str, platform: str) -> Screen | None:
        """Нажатие кнопки теста; None — ничего не менять (двойное нажатие). Второе сообщение обновляется само."""
        if survey is None:
            return "Этот тест уже закрыт.", []
        if data.split(ID_SEPARATOR)[0] == UNSUBSCRIBE_BUTTON:
            self.pending.delete(survey.id)
            if target := self.targets.get(survey.guild_id):
                target.unsubscribed = True
                self.targets.put(target)
            screen = ("🔕 Вы отписались от тестов. Больше не побеспокою.", [])
            await self._show(survey, screen, skip=platform)
            return screen

        parts = data.split(ID_SEPARATOR)
        try:
            step, value = int(parts[2]), int(parts[3])
            if not (0 <= step < len(QUESTIONS) and 1 <= value <= 5):
                raise ValueError
        except (IndexError, ValueError):
            return "Непонятный ответ", []
        if step != survey.step:
            return None                     # двойное нажатие по старому вопросу — сообщение уже другое

        survey.answers[QUESTIONS[step].key] = value
        if survey.step < len(QUESTIONS):
            self.pending.put(survey)
            screen = self._question(survey.id, survey.answers)
        else:
            self.pending.delete(survey.id)
            target = self.targets.get(survey.guild_id)
            if target and platform == "discord":
                target.user_name = name
                self.targets.put(target)
            result = self._save(survey, target.user_name if target else name, completed=True)
            screen = (f"✅ **Тест пройден** ({PLATFORM[platform]}). Индекс Доркина: **{index_title(result.index)}**"
                      f"\n\n{_answers_text(result.answers)}", [])
        await self._show(survey, screen, skip=platform)
        return screen

    def _survey_of(self, data: str) -> PendingSurvey | None:
        parts = data.split(ID_SEPARATOR)
        return self.pending.get(parts[1]) if len(parts) > 1 else None

    async def _discord_press(self, executed: ExecutedComponent) -> ExecutedComponentResponse | None:
        survey = self._survey_of(executed.id)
        if survey is not None and survey.user_id != executed.user.id:
            survey = None
        screen = await self.press(executed.id, survey, executed.user.display_name or executed.user.name, "discord")
        return None if screen is None else ExecutedComponentResponse(containers=_discord(screen), edit=True)

    @button(ANSWER_BUTTON)
    async def answer(self, executed: ExecutedComponent) -> ExecutedComponentResponse | None:
        return await self._discord_press(executed)

    @button(UNSUBSCRIBE_BUTTON)
    async def unsubscribe(self, executed: ExecutedComponent) -> ExecutedComponentResponse | None:
        return await self._discord_press(executed)

    async def on_telegram(self, update: TgUpdate) -> None:
        if update.callback_id:
            await self.telegram.answer_callback(update.callback_id)
            survey = self._survey_of(update.data)
            if survey is not None and (survey.tg_chat_id != update.chat_id or survey.tg_message_id != update.message_id):
                survey = None
            screen = await self.press(update.data, survey, update.user_name, "telegram")
            if screen is not None:
                await self.telegram.edit(update.chat_id, update.message_id, *_telegram(screen))
            return
        if not update.text.startswith("/start"):
            return
        code = update.text.removeprefix("/start").strip()
        target = next((t for t in self.targets.all() if code and t.tg_code == code), None)
        if target is None:
            await self.telegram.reply(update.chat_id, "Привет! Чтобы получать тесты, откройте ссылку из панели бота.")
            return
        target.tg_chat_id, target.tg_name = update.chat_id, update.user_name
        self.targets.put(target)
        await self.telegram.reply(update.chat_id, f"{config.PLUGIN_TITLE}\n✅ Telegram привязан. Тесты будут приходить сюда.")

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
