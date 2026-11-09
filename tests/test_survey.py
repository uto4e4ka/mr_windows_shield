from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from potehinsonnet.net import NatsRequestError
from potehinsonnet.net_models.discord_models import Channel, DiscordMessageResponse, ExecutedComponent, User
from potehinsonnet.storage import DataDir

from mr_windows_shield.chart import PERIODS, render_chart
from mr_windows_shield.dorkin import DorkinReports
from mr_windows_shield.models import MISSED_LEVEL
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
def reports(tmp_path, clock):
    return DorkinReports(DataDir(tmp_path), AsyncMock(), now=clock)


@pytest.fixture
def surveys(tmp_path, discord, reports, clock):
    s = Surveys(DataDir(tmp_path), discord, reports, now=clock)
    s.configure(GUILD, DORKIN, 6, True)
    return s


def click(custom_id, user_id=DORKIN):
    return ExecutedComponent(id=custom_id, user=User(id=user_id, name="dorkin", display_name="Доркин"),
                             channel=Channel(id=900, name="dm"), task_id="t")   # guild нет — это личка


def pending_id(surveys):
    [survey] = surveys.pending.all()
    return survey.id


async def test_first_survey_goes_to_dm_with_scale_and_unsubscribe(surveys, discord):
    await surveys.tick()

    user_id, message = discord.send_direct_message.await_args.args
    assert user_id == DORKIN
    text, levels, extra = message.containers[0].components
    assert "степень выделения" in text.content
    assert len(levels.components) == 5
    assert extra.components[0].custom_id.startswith(UNSUBSCRIBE_BUTTON)
    assert surveys.pending.all()[0].channel_id == 900


async def test_answer_is_saved_as_survey_point(surveys, reports):
    await surveys.tick()
    sid = pending_id(surveys)

    response = await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|4"))

    assert response.edit and "Записано" in response.containers[0].components[0].content
    [record] = reports.for_guild(GUILD)
    assert (record.source, record.level, record.user_id, record.user_name) == ("survey", 4, DORKIN, "Доркин")
    assert surveys.pending.all() == []


async def test_only_dorkin_can_answer_and_only_once(surveys, reports):
    await surveys.tick()
    sid = pending_id(surveys)

    stranger = await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|1", user_id=5))
    assert "закрыт" in stranger.containers[0].components[0].content
    await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|2"))
    again = await surveys.answer(click(f"{ANSWER_BUTTON}|{sid}|5"))
    assert "закрыт" in again.containers[0].components[0].content
    assert [r.level for r in reports.for_guild(GUILD)] == [2]


async def test_next_survey_after_interval_not_before(surveys, discord, clock):
    await surveys.tick()
    await surveys.answer(click(f"{ANSWER_BUTTON}|{pending_id(surveys)}|1"))

    clock.value += timedelta(hours=5, minutes=59)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 1

    clock.value += timedelta(minutes=1)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 2


async def test_no_answer_in_hour_is_missed_and_buttons_go_out(surveys, discord, reports, clock):
    await surveys.tick()
    clock.value += timedelta(minutes=59)
    await surveys.tick()
    assert reports.for_guild(GUILD) == []

    clock.value += timedelta(minutes=1)
    await surveys.tick()

    [record] = reports.for_guild(GUILD)
    assert (record.source, record.level) == ("missed", MISSED_LEVEL)
    edit = discord.update_message.await_args.args[0]
    assert (edit.message_id, edit.message.channel_id) == (10, 900)
    assert "Время на ответ вышло" in edit.message.containers[0].components[0].content


async def test_quiet_hours_postpone_to_nine_msk(surveys, discord, clock):
    clock.value = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)          # 01:00 МСК
    await surveys.tick()
    assert discord.send_direct_message.await_count == 0
    target = surveys.targets.get(GUILD)
    assert surveys.next_send_at(target) == datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)   # 09:00 МСК

    clock.value = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 1


async def test_unsubscribe_stops_surveys_and_admin_cannot_override(surveys, discord, clock):
    await surveys.tick()
    response = await surveys.unsubscribe(click(f"{UNSUBSCRIBE_BUTTON}|{pending_id(surveys)}"))
    assert "отписались" in response.containers[0].components[0].content

    surveys.configure(GUILD, DORKIN, 1, True)        # админ пересохранил настройки
    clock.value += timedelta(days=2)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 1
    assert surveys.targets.get(GUILD).unsubscribed


async def test_closed_dm_is_shown_and_retried_after_interval(surveys, discord, clock):
    discord.send_direct_message.side_effect = NatsRequestError("PermissionError: dorkin не принимает личные сообщения")
    await surveys.tick()
    await surveys.tick()

    assert discord.send_direct_message.await_count == 1
    assert "не принимает" in surveys.targets.get(GUILD).last_error
    assert surveys.pending.all() == []


async def test_disabled_or_removed_target_gets_nothing(surveys, discord):
    surveys.configure(GUILD, DORKIN, 6, False)
    await surveys.tick()
    surveys.configure(GUILD, None, 6, True)
    await surveys.tick()
    assert discord.send_direct_message.await_count == 0
    assert surveys.targets.all() == []


async def test_survey_answer_does_not_trigger_report_cooldown(surveys, reports):
    await surveys.tick()
    await surveys.answer(click(f"{ANSWER_BUTTON}|{pending_id(surveys)}|3"))
    assert reports.wait_left(GUILD, DORKIN) is None


async def test_chart_with_all_sources(surveys, reports, clock):
    await surveys.tick()
    clock.value += timedelta(hours=2)
    await surveys.tick()                                   # пропуск
    clock.value += timedelta(hours=5)
    await surveys.tick()
    await surveys.answer(click(f"{ANSWER_BUTTON}|{pending_id(surveys)}|5"))
    assert {r.source for r in reports.for_guild(GUILD)} == {"missed", "survey"}
    assert render_chart(reports.for_guild(GUILD), PERIODS["day"], clock()).startswith(b"\x89PNG")
