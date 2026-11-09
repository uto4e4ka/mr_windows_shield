"""/dorkin report: автор скрыто выбирает степень выделения, в чат уходит предупреждение, репорт сохраняется."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from potehinsonnet.net_models.discord_models import (
    ActionRow, Attachment, Command, CommandArgument, MediaGallery, MediaItem, ComponentButton, Container, TextDisplay, ComponentButtonStyle, ExecutedCommand, ExecutedCommandResponse,
    ExecutedComponent, ExecutedComponentResponse, NatsMessage, Placeholder, PlaceholderType,
)
from potehinsonnet.discord_provider import DiscordProvider
from potehinsonnet.setup.button_registrator import ID_SEPARATOR, button
from potehinsonnet.setup.command_registrator import command

from mr_windows_shield import config
from mr_windows_shield.chart import period_by_name, render_chart
from mr_windows_shield.index import IndexBook
from mr_windows_shield.models import LEVELS, Report, format_time, level_title

COOLDOWN = timedelta(minutes=30)        # один репорт от человека на сервере раз в 30 минут
PERMISSION = "mr_windows_shield.dorkin.report"
REPORT_BUTTON = "dorkin_report"
CHART_FILE = "dorkin_chart.png"


def card(text: str) -> list[Container]:
    """Скрытое меню — сообщение Components V2: заменить его можно только контейнером, не простым текстом."""
    return [Container(components=[TextDisplay(content=text)])]


class DorkinReports:
    def __init__(self, book: IndexBook, discord: DiscordProvider,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.book = book
        self.reports = book.reports
        self.events = None          # EventBook — подключается в main, чтобы события были и на /dorkin chart
        self.discord = discord
        self.now = now

    def for_guild(self, guild_id: int) -> list[Report]:
        """Репорты сервера, новые сверху."""
        return sorted(self.reports.filter(lambda r: r.guild_id == guild_id), key=lambda r: r.created_at, reverse=True)

    def wait_left(self, guild_id: int, user_id: int) -> timedelta | None:
        """Сколько ещё ждать до следующего репорта; None — можно."""
        last = next((r for r in self.for_guild(guild_id) if r.user_id == user_id and r.source == "report"), None)
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
        # Бот рисует кнопки только из контейнеров (Components V2), старое поле buttons он не показывает
        return ExecutedCommandResponse(ephemeral=True, containers=[Container(components=[
            TextDisplay(content="**Выберите степень выделения**"),
            ActionRow(components=[
                ComponentButton(custom_id=f"{REPORT_BUTTON}{ID_SEPARATOR}{number}", label=level.label,
                                emoji=level.emoji, style=level.style)
                for number, level in enumerate(LEVELS, start=1)
            ]),
        ])])

    @button(REPORT_BUTTON)
    async def choose_level(self, executed: ExecutedComponent) -> ExecutedComponentResponse:
        try:
            level = int(executed.id.split(ID_SEPARATOR, 1)[1])
            if not 1 <= level <= len(LEVELS):
                raise ValueError
        except (IndexError, ValueError):
            return ExecutedComponentResponse(containers=card("Неизвестная степень выделения"), ephemeral=True, edit=True)

        # Кулдаун проверяем и здесь: скрытое меню могли открыть до прошлого репорта
        if left := self.wait_left(executed.guild.id, executed.user.id):
            return ExecutedComponentResponse(containers=card(self._cooldown_text(left)), ephemeral=True, edit=True)

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
        return ExecutedComponentResponse(containers=card(f"✅ Репорт отправлен: {level_title(level)}"),
                                         ephemeral=True, edit=True)

    @command(Command(
        name="chart",
        group="dorkin",
        description="График Индекса Доркина: дневной, общий, репорты и тесты",
        tag="dorkin_chart",
        permission=PERMISSION,
        ephemeral=False,
        args=[CommandArgument(name="period", description="сутки, неделя или месяц (по умолчанию неделя)",
                              required=False)],
    ))
    async def chart(self, cmd: ExecutedCommand) -> ExecutedCommandResponse:
        period = period_by_name(next((a.value for a in cmd.args if a.name == "period"), None))
        image = render_chart(self.book, cmd.guild.id, period, self.now(), events=self.events)
        return ExecutedCommandResponse(
            ephemeral=False,
            files=[Attachment.from_bytes(image, CHART_FILE, description="График выделения")],
            containers=[Container(components=[
                TextDisplay(content=f"📈 **Индекс Доркина за {period.title}**"),
                MediaGallery(items=[MediaItem(url=f"attachment://{CHART_FILE}")]),
            ])],
        )
