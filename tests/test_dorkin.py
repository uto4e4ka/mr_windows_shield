from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from potehinsonnet import PluginContext, load_manifest
from potehinsonnet.net_models.discord_models import (
    Channel, ExecutedCommand, ExecutedComponent, Guild, User,
)
from potehinsonnet.storage import DataDir

from mr_windows_shield.chart import PERIODS, period_by_name, render_chart
from mr_windows_shield.dorkin import REPORT_BUTTON, DorkinReports
from mr_windows_shield.index import IndexBook
from mr_windows_shield.models import LEVELS
from mr_windows_shield.linux_watch import find_word, parse_words
from mr_windows_shield.main import create_plugin

ROOT = Path(__file__).resolve().parents[1]
GUILD, CHANNEL, USER = Guild(id=1, name="g"), Channel(id=5, name="chat"), User(id=42, name="vasya", display_name="Вася")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reports(tmp_path, clock):
    return DorkinReports(IndexBook(DataDir(tmp_path), now=clock), AsyncMock(), now=clock)


def cmd():
    return ExecutedCommand(service="mr_windows_shield", tag="dorkin_report", user=USER, guild=GUILD, channel=CHANNEL)


def buttons(response):
    return [c for container in response.containers for row in container.components
            if hasattr(row, "components") and not hasattr(row, "content") for c in row.components or []]


def text(response):
    return " ".join(c.content for container in response.containers for c in container.components
                    if getattr(c, "content", None))


def click(level):
    return ExecutedComponent(id=f"{REPORT_BUTTON}|{level}", user=USER, guild=GUILD, channel=CHANNEL, task_id="t")


async def test_command_shows_scale_privately(reports):
    response = await reports.report(cmd())
    assert response.ephemeral and not response.buttons      # бот показывает только контейнеры
    title, row = response.containers[0].components
    assert "степень выделения" in title.content.lower()
    assert [b.custom_id for b in row.components] == [f"{REPORT_BUTTON}|{n}" for n in range(1, len(LEVELS) + 1)]


async def test_choice_saves_report_and_alerts_chat(reports):
    response = await reports.choose_level(click(4))

    assert response.edit and response.ephemeral and not response.message   # меню V2 правится только контейнером
    assert "Репорт отправлен" in text(response)
    saved = reports.for_guild(GUILD.id)
    assert [(r.user_id, r.user_name, r.level, r.channel_id) for r in saved] == [(42, "Вася", 4, 5)]

    sent = reports.discord.send_message.await_args.args[0]
    assert sent.channel_id == CHANNEL.id
    assert "Внимание!" in sent.text and "Замечен" in sent.text and LEVELS[3].alert in sent.text
    assert "09.11.2026 21:00" in sent.text          # МСК
    assert sent.placeholders["user"].value == USER.id


async def test_cooldown_30_minutes(reports, clock):
    await reports.choose_level(click(1))

    clock.value += timedelta(minutes=29)
    blocked = await reports.report(cmd())
    assert "через 1 мин" in blocked.message and not buttons(blocked)
    assert "через" in text(await reports.choose_level(click(2)))
    assert len(reports.for_guild(GUILD.id)) == 1

    clock.value += timedelta(minutes=1)
    assert buttons(await reports.report(cmd()))


async def test_cooldown_is_per_guild(reports):
    await reports.choose_level(click(1))
    other = cmd().model_copy(update={"guild": Guild(id=2, name="other")})
    assert buttons(await reports.report(other))


@pytest.mark.parametrize("bad", ["0", "99", "x"])
async def test_unknown_level_is_rejected(reports, bad):
    response = await reports.choose_level(click(bad))
    assert "Неизвестная" in text(response)
    assert reports.for_guild(GUILD.id) == []
    reports.discord.send_message.assert_not_awaited()


def test_linux_words():
    words = parse_words(" Linux, ubuntu ,,")
    assert words == ["linux", "ubuntu"]
    assert find_word("я поставил Ubuntu", words) == "ubuntu"
    assert find_word("linuxoid", words) is None


def test_plugin_builds_from_manifest(tmp_path):
    ctx = PluginContext(root=ROOT, manifest=load_manifest(ROOT), data_dir=tmp_path)
    plugin = create_plugin(ctx)
    assert plugin.service.name == "mr_windows_shield"


async def test_chart_command_sends_png(reports, clock):
    await reports.choose_level(click(3))
    response = await reports.chart(cmd())
    assert not response.ephemeral
    assert response.files[0].filename == "dorkin_chart.png"
    assert response.files[0].content().startswith(b"\x89PNG")
    gallery = response.containers[0].components[1]
    assert gallery.items[0].url == "attachment://dorkin_chart.png"


@pytest.mark.parametrize("name, key", [(None, "week"), ("сутки", "day"), ("Месяц", "month"), ("ерунда", "week")])
def test_period_by_name(name, key):
    assert period_by_name(name).key == key


def test_chart_without_data_renders(reports, clock):
    assert render_chart(reports.book, GUILD.id, PERIODS["day"], clock()).startswith(b"\x89PNG")


async def test_panel_page_chart_and_period(tmp_path):
    from potehinsonnet.net_models.panel_ui import PanelActor, PanelContext
    plugin = create_plugin(PluginContext(root=ROOT, manifest=load_manifest(ROOT), data_dir=tmp_path))
    panel = PanelContext(guild_id=1, user=PanelActor.model_construct(id=1, name="admin"))
    _, page = plugin.panel.pages["dorkin_reports"]

    blocks = (await page(panel)).blocks
    assert blocks[0].items["Общий индекс"] == "нет данных"
    assert blocks[1].fields[0].value == "week"
    assert blocks[2].url.startswith("data:image/png;base64,")
    assert [b.title for b in blocks[3:]] == ["📨 Тест в личку и наблюдение", "📊 Утренняя сводка", "📅 По дням",
                                            "🗓 События и анализ", "🧾 Репорты и тесты"]

    await plugin.panel.actions["chart_period"](panel, {"period": "day"})
    assert (await page(panel)).blocks[1].fields[0].value == "day"
