from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from potehinsonnet.net import NatsRequestError
from potehinsonnet.net_models.discord_models import Channel, DiscordMessageResponse, ExecutedComponent, User
from potehinsonnet.storage import DataDir

from mr_windows_shield.index import IndexBook
from mr_windows_shield.survey import ANSWER_BUTTON, UNSUBSCRIBE_BUTTON, Surveys
from mr_windows_shield.telegram import TelegramBot, TelegramError, TgUpdate, keyboard, parse_update, to_html

GUILD, DORKIN, TG_CHAT = 1, 77, 5005


class Clock:
    value = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def telegram():
    bot = TelegramBot(token="t")
    bot.username = "dorkin_bot"
    bot.send = AsyncMock(return_value=42)
    bot.edit = AsyncMock()
    bot.reply = AsyncMock()
    bot.answer_callback = AsyncMock()
    return bot


@pytest.fixture
def discord():
    provider = AsyncMock()
    provider.send_direct_message.return_value = DiscordMessageResponse(channel_id=900, message_id=10)
    return provider


@pytest.fixture
def book(tmp_path):
    return IndexBook(DataDir(tmp_path), now=Clock())


@pytest.fixture
def surveys(tmp_path, discord, book, telegram):
    s = Surveys(DataDir(tmp_path), discord, book, now=Clock(), telegram=telegram)
    s.configure(GUILD, DORKIN, 6, True, delivery="both")
    return s


async def link(surveys):
    code = surveys.targets.get(GUILD).tg_code
    await surveys.on_telegram(TgUpdate(chat_id=TG_CHAT, user_id=TG_CHAT, user_name="@dorkin", text=f"/start {code}"))


def tg_click(data, message_id=42, chat_id=TG_CHAT):
    return TgUpdate(chat_id=chat_id, user_id=chat_id, user_name="@dorkin", callback_id="cb", data=data,
                    message_id=message_id)


def pending(surveys):
    [survey] = surveys.pending.all()
    return survey


def test_markup_and_keyboard():
    assert to_html("**Тест** <1>\n-# мелко") == "<b>Тест</b> &lt;1&gt;\n<i>мелко</i>"
    assert keyboard([[]]) == {"inline_keyboard": [[]]}


def test_parse_update():
    raw = {"update_id": 1, "callback_query": {"id": "q", "from": {"id": 5, "username": "d"}, "data": "x",
                                              "message": {"message_id": 9, "chat": {"id": 5}}}}
    assert parse_update(raw) == TgUpdate(chat_id=5, user_id=5, user_name="@d", callback_id="q", data="x", message_id=9)
    group = {"update_id": 2, "message": {"chat": {"id": -1, "type": "group"}, "text": "/start"}}
    assert parse_update(group) is None


async def test_start_with_code_links_telegram(surveys, telegram):
    await link(surveys)
    target = surveys.targets.get(GUILD)
    assert (target.tg_chat_id, target.tg_name) == (TG_CHAT, "@dorkin")
    assert "привязан" in telegram.reply.await_args.args[1]


async def test_start_with_wrong_code_does_nothing(surveys, telegram):
    await surveys.on_telegram(TgUpdate(chat_id=1, user_id=1, user_name="x", text="/start nope"))
    assert surveys.targets.get(GUILD).tg_chat_id is None


async def test_unlink_makes_old_link_useless(surveys):
    await link(surveys)
    old = surveys.targets.get(GUILD).tg_code
    surveys.unlink_telegram(GUILD)
    target = surveys.targets.get(GUILD)
    assert target.tg_chat_id is None and target.tg_code != old


async def test_both_sends_to_discord_and_telegram(surveys, discord, telegram):
    await link(surveys)
    assert await surveys.send_now(GUILD) == "Тест отправлен: Discord, Telegram"
    chat_id, text, rows = telegram.send.await_args.args
    assert chat_id == TG_CHAT and "вопрос 1/4" in text
    assert rows[0][0].data.startswith(ANSWER_BUTTON) and rows[1][0].label == "🔕 Отписаться"
    survey = pending(surveys)
    assert (survey.message_id, survey.tg_message_id) == (10, 42)


async def test_answer_in_telegram_updates_discord_too(surveys, discord, telegram, book):
    await link(surveys)
    await surveys.tick()
    sid = pending(surveys).id
    for step in range(4):
        await surveys.on_telegram(tg_click(f"{ANSWER_BUTTON}|{sid}|{step}|5"))
    assert "Тест пройден** (Telegram)" in telegram.edit.await_args.args[2]
    discord_view = discord.update_message.await_args.args[0].message.containers[0].components[0].content
    assert "Тест пройден" in discord_view
    [result] = book.tests.all()
    assert result.completed and result.index == 100


async def test_answer_in_discord_updates_telegram(surveys, telegram):
    await link(surveys)
    await surveys.tick()
    sid = pending(surveys).id
    click = ExecutedComponent(id=f"{ANSWER_BUTTON}|{sid}|0|3", user=User(id=DORKIN, name="d"),
                              channel=Channel(id=900, name="dm"), task_id="t")
    response = await surveys.answer(click)
    assert "вопрос 2/4" in response.containers[0].components[0].content
    chat_id, message_id, text, _ = telegram.edit.await_args.args
    assert (chat_id, message_id) == (TG_CHAT, 42) and "вопрос 2/4" in text


async def test_foreign_telegram_chat_cannot_answer(surveys, telegram):
    await link(surveys)
    await surveys.tick()
    sid = pending(surveys).id
    await surveys.on_telegram(tg_click(f"{ANSWER_BUTTON}|{sid}|0|5", chat_id=1))
    assert pending(surveys).answers == {}


async def test_unsubscribe_from_telegram(surveys, telegram):
    await link(surveys)
    await surveys.tick()
    await surveys.on_telegram(tg_click(f"{UNSUBSCRIBE_BUTTON}|{pending(surveys).id}"))
    assert surveys.targets.get(GUILD).unsubscribed and surveys.pending.all() == []


async def test_telegram_only_without_link_reports_error(surveys, discord):
    surveys.configure(GUILD, DORKIN, 6, True, delivery="telegram")
    with pytest.raises(RuntimeError, match="не привязан"):
        await surveys.send_now(GUILD)
    discord.send_direct_message.assert_not_awaited()


async def test_closed_dm_still_goes_to_telegram(surveys, discord, telegram):
    await link(surveys)
    discord.send_direct_message.side_effect = NatsRequestError("PermissionError: личка закрыта")
    result = await surveys.send_now(GUILD)
    assert result.startswith("Тест отправлен: Telegram") and "личка закрыта" in result
    assert pending(surveys).message_id is None


async def test_telegram_down_falls_back_to_discord(surveys, telegram):
    await link(surveys)
    telegram.send.side_effect = TelegramError("нет связи с Telegram (ConnectError)")
    assert (await surveys.send_now(GUILD)).startswith("Тест отправлен: Discord")
