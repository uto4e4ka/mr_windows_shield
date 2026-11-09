"""Точка входа плагина (plugin.toml → runtime.entry). Запуск для разработки: python -m potehinsonnet.run ."""
import base64

from potehinsonnet import Plugin, PluginContext
from potehinsonnet.net_models.panel_ui import (
    Form, Image, Option, PanelContext, PanelPage, Select, Table, TableRow, Text,
)

from mr_windows_shield.chart import DEFAULT_PERIOD, PERIODS, render_chart
from mr_windows_shield.dorkin import DorkinReports
from mr_windows_shield.linux_watch import LinuxWatch, parse_words
from mr_windows_shield.models import format_time, level_title

DEFAULT_LINUX_WORDS = "linux"
TABLE_LIMIT = 100               # в таблице — последние репорты, график — за весь выбранный период


def create_plugin(ctx: PluginContext) -> Plugin:
    plugin = Plugin.from_context(ctx)

    reports = DorkinReports(ctx.data, plugin.discord)
    plugin.register(reports)

    words = parse_words(str(ctx.settings.get("linux_words") or DEFAULT_LINUX_WORDS))
    plugin.add_lifespan(LinuxWatch(plugin.nats, plugin.discord, words).running())

    chart_period: dict[int, str] = {}     # выбранный в панели период графика — по серверу

    @plugin.panel_page("dorkin_reports", title="Репорты Доркина", icon="⚠️")
    async def reports_page(panel: PanelContext) -> PanelPage:
        items = reports.for_guild(panel.guild_id) if panel.guild_id else []
        period = PERIODS[chart_period.get(panel.guild_id or 0, DEFAULT_PERIOD)]
        png = render_chart(items, period, reports.now())
        return PanelPage(blocks=[
            Form(action="chart_period", auto_submit=True, submit="Показать", fields=[
                Select(name="period", label="Период графика", value=period.key,
                       options=[Option(value=p.key, label=p.title.capitalize()) for p in PERIODS.values()]),
            ]),
            Image(url="data:image/png;base64," + base64.b64encode(png).decode(), alt="График степени выделения"),
            Text(f"Всего репортов: {len(items)}"),
            Table(columns=["Дата и время (МСК)", "Кто", "Степень"], empty="Репортов пока нет", rows=[
                TableRow(cells=[format_time(r.created_at), r.user_name, level_title(r.level)])
                for r in items[:TABLE_LIMIT]
            ]),
        ])

    @plugin.panel_action("chart_period")
    async def set_chart_period(panel: PanelContext, values: dict) -> None:
        if values.get("period") in PERIODS and panel.guild_id:
            chart_period[panel.guild_id] = values["period"]

    return plugin
