"""/dorkin report: автор скрыто выбирает степень выделения, в чат уходит предупреждение, репорт сохраняется."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import BaseModel
from potehinsonnet.net_models.discord_models import (
    Command, ComponentButton, ComponentButtonStyle, ExecutedCommand, ExecutedCommandResponse,
    ExecutedComponent, ExecutedComponentResponse, NatsMessage, Placeholder, PlaceholderType,
)
from potehinsonnet.discord_provider import DiscordProvider
from potehinsonnet.setup.button_registrator import ID_SEPARATOR, button
from potehinsonnet.setup.command_registrator import command
from potehinsonnet.storage import DataDir

from mr_windows_shield import config

COOLDOWN = timedelta(minutes=30)        # один репорт от человека на сервере раз в 30 минут
PERMISSION = "mr_windows_shield.dorkin.report"
REPORT_BUTTON = "dorkin_report"
MSK = timezone(timedelta(hours=3))      # время в сообщении и в панели — московское


@dataclass(frozen=True)
class Level:
    label: str                  # кнопка в скрытом сообщении
    emoji: str
    style: ComponentButtonStyle
    alert: str                  # что пишется в чат после «Внимание! Замечен…»


# Шкала выделения: порядок = степень (1 — слабее всего)
LEVELS: list[Level] = [
    Level("Лёгкое", "🟢", ComponentButtonStyle.SUCCESS, "Доркин. Лёгкое выделение, ситуация под контролем."),
    Level("Умеренное", "🟡", ComponentButtonStyle.SECONDARY, "Доркин. Умеренное выделение, сохраняйте бдительность."),
    Level("Сильное", "🟠", ComponentButtonStyle.PRIMARY, "Доркин. Сильное выделение, держитесь подальше."),
    Level("Обильное", "🔴", ComponentButtonStyle.DANGER, "Доркин. Обильное выделение, наденьте средства защиты!"),
    Level("Критическое", "☢️", ComponentButtonStyle.DANGER, "ДОРКИН. КРИТИЧЕСКОЕ ВЫДЕЛЕНИЕ. ЭВАКУАЦИЯ!"),
]


class Report(BaseModel):
    id: str
    guild_id: int
    channel_id: int
    user_id: int
    user_name: str
    level: int                  # 1..len(LEVELS)
    created_at: datetime        # UTC


def level_title(level: int) -> str:
    item = LEVELS[level - 1]
    return f"{item.emoji} {item.label}"


def format_time(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


class DorkinReports:
    def __init__(self, data: DataDir, discord: DiscordProvider,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.reports = data.collection("reports.json", Report, key="id")
        self.discord = discord
        self.now = now

    def for_guild(self, guild_id: int) -> list[Report]:
        """Репорты сервера, новые сверху."""
        return sorted(self.reports.filter(lambda r: r.guild_id == guild_id), key=lambda r: r.created_at, reverse=True)

    def wait_left(self, guild_id: int, user_id: int) -> timedelta | None:
        """Сколько ещё ждать до следующего репорта; None — можно."""
        last = next((r for r in self.for_guild(guild_id) if r.user_id == user_id), None)
        if last is None:
            return None
        left = last.created_at + COOLDOWN - self.now()
        return left if left > timedelta(0) else None

    @staticmethod
    def _cooldown_text(left: timedelta) -> str:
        minutes = max(1, -(-int(left.total_seconds()) // 60))   # вверх: «через 1 мин», а не «через 0»
        return f"⏳ Следующий репорт можно отправить через {minutes} мин."

    @command(Command(
        name="report",
        group="dorkin",
        description="Сообщить о выделении Доркина",
        tag="dorkin_report",
        permission=PERMISSION,
        ephemeral=True,
    ))
    async def report(self, cmd: ExecutedCommand) -> ExecutedCommandResponse:
        if left := self.wait_left(cmd.guild.id, cmd.user.id):
            return ExecutedCommandResponse(message=self._cooldown_text(left), ephemeral=True)
        return ExecutedCommandResponse(
            message="**Выберите степень выделения**",
            ephemeral=True,
            buttons=[
                ComponentButton(custom_id=f"{REPORT_BUTTON}{ID_SEPARATOR}{number}", label=level.label,
                                emoji=level.emoji, style=level.style)
                for number, level in enumerate(LEVELS, start=1)
            ],
        )

    @button(REPORT_BUTTON)
    async def choose_level(self, executed: ExecutedComponent) -> ExecutedComponentResponse:
        try:
            level = int(executed.id.split(ID_SEPARATOR, 1)[1])
            if not 1 <= level <= len(LEVELS):
                raise ValueError
        except (IndexError, ValueError):
            return ExecutedComponentResponse(message="Неизвестная степень выделения", ephemeral=True, edit=True)

        # Кулдаун проверяем и здесь: скрытое меню могли открыть до прошлого репорта
        if left := self.wait_left(executed.guild.id, executed.user.id):
            return ExecutedComponentResponse(message=self._cooldown_text(left), ephemeral=True, edit=True)

        name = executed.user.display_name or executed.user.name
        report = self.reports.put(Report(
            id=uuid.uuid4().hex, guild_id=executed.guild.id, channel_id=executed.channel.id,
            user_id=executed.user.id, user_name=name, level=level, created_at=self.now(),
        ))
        await self.discord.send_message(NatsMessage(
            service=config.PLUGIN_NAME,
            channel_id=executed.channel.id,
            text=f"%tag%\n⚠️ **Внимание!** Замечен {LEVELS[level - 1].alert}\n"
                 f"Степень: {level_title(level)}\n"
                 f"Репорт: %user% · {format_time(report.created_at)}",
            placeholders={
                "tag": Placeholder(type=PlaceholderType.TEXT, value=config.PLUGIN_TITLE),
                "user": Placeholder(type=PlaceholderType.MENTION, value=executed.user.id),
            },
        ))
        return ExecutedComponentResponse(message=f"✅ Репорт отправлен: {level_title(level)}", ephemeral=True, edit=True)
