from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from potehinsonnet.net import NatsRequestError
from potehinsonnet.net_models.discord_models import Channel, DiscordMessageResponse, ExecutedComponent, User
from potehinsonnet.storage import DataDir

from mr_windows_shield.index import IndexBook
from mr_windows_shield.models import QUESTIONS
from mr_windows_shield.survey import ANSWER_BUTTON, UNSUBSCRIBE_BUTTON, Surveys

GUILD, DORKIN = 1, 77


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)      # 15:00 МСК

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def discord():
    provider = AsyncMock()
    provider.send_direct_message.return_value = DiscordMessageResponse(channel_id=900, message_id=10)
    return provider


@pytest.fixture
def book(tmp_path, clock):
    return IndexBook(DataDir(tmp_path), now=clock)


@pytest.fixture
def surveys(tmp_path, discord, book, clock):
    s = Surveys(DataDir(tmp_path), discord, book, now=clock)
    s.pick = lambda used: s.bank.first_ids()          # здесь — исходные 4 вопроса; банк — в test_questions
    s.configure(GUILD, DORKIN, 6, True)
    return s


def click(custom_id, user_id=DORKIN):
    return ExecutedComponent(id=custom_id, user=User(id=user_id, name="dorkin", display_name="Доркин"),
                             channel=Channel(id=900, name="dm"), task_id="t")   # guild нет — это личка


def content(response_or_containers):
    containers = getattr(response_or_containers, "containers", response_or_containers)
    return containers[0].components[0].content


def pending_id(surveys):
    [survey] = surveys.pending.all()
    return survey.id


async def answer_all(surveys, values):
    sid = pending_id(surveys)
    response = None
    for step, value in enumerate(values):
        response = await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|{step}|{value}"))
    return response


async def test_first_question_goes_to_dm(surveys, discord):
    await surveys.tick()

    user_id, message = discord.send_direct_message.await_args.args
    assert user_id == DORKIN
    text, answers, extra = message.containers[0].components
    assert "вопрос 1/4" in text.content and "аэрогрилем" in text.content
    assert [b.label for b in answers.components] == list(QUESTIONS[0].answers)
    assert extra.components[0].custom_id.startswith(UNSUBSCRIBE_BUTTON)


async def test_questions_go_one_by_one_and_end_with_index(surveys, book):
    await surveys.tick()
    sid = pending_id(surveys)

    second = await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|0|5"))
    assert second.edit and "вопрос 2/4" in content(second) and "женщин" in content(second)
    assert "Жарю прямо сейчас" in content(second)                 # уже данные ответы видны

    final = await answer_all_rest(surveys, sid, [5, 5, 5])
    assert "Тест пройден" in content(final) and "100" in content(final) and "Критическое" in content(final)
    [result] = book.tests.all()
    assert result.completed and result.index == 100
    assert result.answers == {"airfryer": 5, "women": 5, "warthunder": 5, "autism": 5}
    assert surveys.pending.all() == []


async def answer_all_rest(surveys, sid, values):
    response = None
    for value in values:
        step = surveys.pending.get(sid).step
        response = await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|{step}|{value}"))
    return response


async def test_double_click_on_old_question_is_ignored(surveys):
    await surveys.tick()
    sid = pending_id(surveys)
    await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|0|2"))
    assert await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|0|5")) is None
    assert surveys.pending.get(sid).answers == {"airfryer": 2}


async def test_only_dorkin_can_answer(surveys, book):
    await surveys.tick()
    sid = pending_id(surveys)
    stranger = await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|0|1", user_id=5))
    assert "закрыт" in content(stranger)
    assert surveys.pending.get(sid).answers == {}


async def test_unfinished_test_in_hour_is_missed(surveys, discord, book, clock):
    await surveys.tick()
    await surveys.answer(click(f"{ANSWER_BUTTON}|{pending_id(surveys)}|0|4"))
    clock.value += timedelta(hours=1)
    await surveys.tick()

    [result] = book.tests.all()
    assert not result.completed and result.index is None and result.answers == {"airfryer": 4}
    edit = discord.update_message.await_args.args[0]
    assert (edit.message_id, edit.message.channel_id) == (10, 900)
    assert "Время на тест вышло" in content(edit.message.containers)


async def test_next_test_after_interval(surveys, discord, clock):
    await surveys.tick()
    await answer_all(surveys, [1, 1, 1, 1])
    clock.value += timedelta(hours=5, minutes=59)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 1
    clock.value += timedelta(minutes=1)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 2


async def test_quiet_hours_postpone_to_nine_msk(surveys, discord, clock):
    clock.value = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)          # 01:00 МСК
    await surveys.tick()
    assert discord.send_direct_message.await_count == 0
    assert surveys.next_send_at(surveys.targets.get(GUILD)) == datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)


async def test_unsubscribe_stops_tests_and_admin_cannot_override(surveys, discord, clock):
    await surveys.tick()
    response = await surveys.unsubscribe(click(f"{UNSUBSCRIBE_BUTTON}|{pending_id(surveys)}"))
    assert "отписались" in content(response)
    surveys.configure(GUILD, DORKIN, 1, True)
    clock.value += timedelta(days=2)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 1


async def test_closed_dm_is_shown_and_retried_after_interval(surveys, discord):
    discord.send_direct_message.side_effect = NatsRequestError("PermissionError: dorkin не принимает личные сообщения")
    await surveys.tick()
    await surveys.tick()
    assert discord.send_direct_message.await_count == 1
    assert "не принимает" in surveys.targets.get(GUILD).last_error
    assert surveys.pending.all() == []


async def test_send_now_ignores_schedule_and_night(surveys, discord, clock):
    await surveys.tick()
    await answer_all(surveys, [1, 1, 1, 1])
    clock.value = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)          # ночь, интервал не прошёл
    assert await surveys.send_now(GUILD) == "Тест отправлен: Discord"
    assert discord.send_direct_message.await_count == 2


async def test_send_now_refuses_while_waiting_unsubscribed_or_unset(surveys, discord):
    await surveys.tick()
    assert "не пройден" in await surveys.send_now(GUILD)
    await surveys.unsubscribe(click(f"{UNSUBSCRIBE_BUTTON}|{pending_id(surveys)}"))
    assert "отписался" in await surveys.send_now(GUILD)
    assert "выберите" in await surveys.send_now(2)
    assert discord.send_direct_message.await_count == 1


async def test_send_now_reports_closed_dm(surveys, discord):
    discord.send_direct_message.side_effect = NatsRequestError("PermissionError: dorkin не принимает личные сообщения")
    with pytest.raises(RuntimeError, match="не принимает"):
        await surveys.send_now(GUILD)


async def test_changing_person_cancels_previous_test(surveys, discord, book):
    await surveys.tick()                                     # тест ушёл DORKIN
    surveys.configure(GUILD, 555, 6, True)                   # админ выбрал другого
    discord.send_direct_message.return_value = DiscordMessageResponse(channel_id=901, message_id=11)

    await surveys.tick()

    assert discord.send_direct_message.await_args.args[0] == 555
    assert [p.user_id for p in surveys.pending.all()] == [555]
    assert "отменён" in content(discord.update_message.await_args.args[0].message.containers)
    assert book.tests.all() == []                            # отмена — не «уклонился»


async def test_send_now_after_changing_person(surveys, discord):
    await surveys.tick()
    surveys.configure(GUILD, 555, 6, True)
    assert await surveys.send_now(GUILD) == "Тест отправлен: Discord"
    assert discord.send_direct_message.await_args.args[0] == 555
