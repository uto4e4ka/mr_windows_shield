"""Точка входа плагина (plugin.toml → runtime.entry). Запуск для разработки: python -m potehinsonnet.run ."""
import base64

from potehinsonnet import Plugin, PluginContext
from potehinsonnet.net_models.panel_ui import (
    Form, Image, KeyValue, MemberPicker, NumberField, Option, PanelContext, PanelPage, Section, Select, Table,
    TableRow, Text, Toggle,
)

from mr_windows_shield.chart import DEFAULT_PERIOD, PERIODS, render_chart
from mr_windows_shield.dorkin import DorkinReports
from mr_windows_shield.linux_watch import LinuxWatch, parse_words
from mr_windows_shield.models import SOURCE_TITLES, format_time, level_title
from mr_windows_shield.survey import DEFAULT_INTERVAL_HOURS, Surveys

DEFAULT_LINUX_WORDS = "linux"
TABLE_LIMIT = 100               # в таблице — последние репорты, график — за весь выбранный период


def create_plugin(ctx: PluginContext) -> Plugin:
    plugin = Plugin.from_context(ctx)

    reports = DorkinReports(ctx.data, plugin.discord)
    surveys = Surveys(ctx.data, plugin.discord, reports)
    plugin.register(reports, surveys)
    plugin.add_lifespan(surveys.running())

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
            survey_section(panel.guild_id),
            Text(f"Всего записей: {len(items)}"),
            Table(columns=["Дата и время (МСК)", "Откуда", "Кто", "Степень"], empty="Записей пока нет", rows=[
                TableRow(cells=[format_time(r.created_at), SOURCE_TITLES.get(r.source, r.source), r.user_name,
                                level_title(r.level)])
                for r in items[:TABLE_LIMIT]
            ]),
        ])

    def survey_section(guild_id: int | None) -> Section:
        target = surveys.targets.get(guild_id) if guild_id else None
        status: dict[str, str] = {}
        if target is None:
            status["Статус"] = "не настроен — выберите, кого опрашивать"
        elif target.unsubscribed:
            status["Статус"] = f"🔕 {target.user_name} отписался от опросов"
        elif not target.enabled:
            status["Статус"] = "выключен"
        else:
            when = surveys.next_send_at(target)
            waiting = any(p.guild_id == guild_id for p in surveys.pending.all())
            status["Статус"] = "⏳ ждём ответа" if waiting else f"следующий опрос: {format_time(when)}"
        if target and target.last_sent_at:
            status["Последний опрос"] = format_time(target.last_sent_at)
        if target and target.last_error:
            status["Ошибка"] = target.last_error
        return Section(title="📨 Опрос в личку", collapsed=target is None, blocks=[
            Text("Раз в N часов бот пишет человеку в личку и просит оценить степень выделения. "
                 "Ночью (00:00–09:00 МСК) не пишет; не ответил за час — «уклонился»."),
            KeyValue(items=status),
            Form(action="survey_setup", submit="Сохранить", fields=[
                MemberPicker(name="user_id", label="Кого опрашивать", value=target.user_id if target else None),
                NumberField(name="interval_hours", label="Раз в сколько часов", min=1, step=1,
                            value=target.interval_hours if target else DEFAULT_INTERVAL_HOURS),
                Toggle(name="enabled", label="Опрос включён", value=target.enabled if target else True),
            ]),
        ])

    @plugin.panel_action("survey_setup")
    async def survey_setup(panel: PanelContext, values: dict) -> str:
        if not panel.guild_id:
            return "Опрос настраивается в разделе сервера"
        user_id = values.get("user_id")
        target = surveys.configure(panel.guild_id, int(user_id) if user_id else None,
                                   values.get("interval_hours") or DEFAULT_INTERVAL_HOURS, bool(values.get("enabled")))
        if target is None:
            return "Опрос выключен: никто не выбран"
        if target.unsubscribed:
            return "Сохранено, но он отписался от опросов — бот ему писать не будет"
        return "Сохранено"

    @plugin.panel_action("chart_period")
    async def set_chart_period(panel: PanelContext, values: dict) -> None:
        if values.get("period") in PERIODS and panel.guild_id:
            chart_period[panel.guild_id] = values["period"]

    return plugin
