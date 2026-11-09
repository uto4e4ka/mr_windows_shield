"""Точка входа плагина (plugin.toml → runtime.entry). Запуск для разработки: python -m potehinsonnet.run ."""
from potehinsonnet import Plugin, PluginContext
from potehinsonnet.net_models.panel_ui import PanelContext, PanelPage, Table, TableRow, Text

from mr_windows_shield.dorkin import DorkinReports, format_time, level_title
from mr_windows_shield.linux_watch import LinuxWatch, parse_words

DEFAULT_LINUX_WORDS = "linux"


def create_plugin(ctx: PluginContext) -> Plugin:
    plugin = Plugin.from_context(ctx)

    reports = DorkinReports(ctx.data, plugin.discord)
    plugin.register(reports)

    words = parse_words(str(ctx.settings.get("linux_words") or DEFAULT_LINUX_WORDS))
    plugin.add_lifespan(LinuxWatch(plugin.nats, plugin.discord, words).running())

    @plugin.panel_page("dorkin_reports", title="Репорты Доркина", icon="⚠️")
    async def reports_page(panel: PanelContext) -> PanelPage:
        items = reports.for_guild(panel.guild_id) if panel.guild_id else []
        return PanelPage(blocks=[
            Text(f"Всего репортов: {len(items)}"),
            Table(columns=["Дата и время (МСК)", "Кто", "Степень"], empty="Репортов пока нет", rows=[
                TableRow(cells=[format_time(r.created_at), r.user_name, level_title(r.level)]) for r in items
            ]),
        ])

    return plugin
