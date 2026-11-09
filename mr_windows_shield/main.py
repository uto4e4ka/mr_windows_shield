"""Точка входа плагина (plugin.toml → runtime.entry). Запуск для разработки: python -m potehinsonnet.run ."""
import base64

from potehinsonnet import Plugin, PluginContext
from potehinsonnet.net_models.panel_ui import (
    Button, Form, Image, KeyValue, MemberPicker, NumberField, Option, PanelContext, PanelPage, Section, Select, Table,
    TableRow, Text, Toggle,
)

from mr_windows_shield.chart import DEFAULT_PERIOD, PERIODS, render_chart
from mr_windows_shield.dorkin import DorkinReports
from mr_windows_shield.index import IndexBook
from mr_windows_shield.linux_watch import LinuxWatch, parse_words
from mr_windows_shield.models import DayIndex, format_day, format_time, index_title, level_title
from mr_windows_shield.observe import TOPIC_TITLES, ChatObserver, build_topics
from mr_windows_shield.survey import DEFAULT_INTERVAL_HOURS, Surveys

DEFAULT_LINUX_WORDS = "linux"
DAYS_SHOWN = 30
EVENTS_SHOWN = 100


def signed(value: float) -> str:
    return f"{value:+g}" if value else "0"


def breakdown(day: DayIndex) -> str:
    base = f"{day.base:g} ({'тест' if day.base_from_test else 'общий'})"
    return (f"{base} {signed(day.reports)} репорты {signed(day.observation)} наблюдение "
            f"{signed(day.missed)} уклонения = {day.daily:g}")


def create_plugin(ctx: PluginContext) -> Plugin:
    plugin = Plugin.from_context(ctx)

    book = IndexBook(ctx.data)
    reports = DorkinReports(book, plugin.discord)
    surveys = Surveys(ctx.data, plugin.discord, book)
    plugin.register(reports, surveys)
    plugin.add_lifespan(surveys.running())
    plugin.add_lifespan(ChatObserver(plugin.nats, book, surveys.targets, build_topics(ctx.settings)).running())

    words = parse_words(str(ctx.settings.get("linux_words") or DEFAULT_LINUX_WORDS))
    plugin.add_lifespan(LinuxWatch(plugin.nats, plugin.discord, words).running())

    chart_period: dict[int, str] = {}     # выбранный в панели период графика — по серверу

    @plugin.panel_page("dorkin_reports", title="Индекс Доркина", icon="⚠️")
    async def index_page(panel: PanelContext) -> PanelPage:
        guild_id = panel.guild_id
        if not guild_id:
            return PanelPage(blocks=[Text("Индекс считается по каждому серверу — откройте страницу в разделе сервера")])
        book.close_days(guild_id)
        period = PERIODS[chart_period.get(guild_id, DEFAULT_PERIOD)]
        png = render_chart(book, guild_id, period, book.now())
        today = book.today(guild_id)
        return PanelPage(blocks=[
            KeyValue(items={
                "Общий индекс": index_title(book.state(guild_id).overall),
                "Сегодня (дневной)": index_title(today.daily if today else None),
                "Из чего сегодня": breakdown(today) if today else "ждём первого теста",
            }),
            Form(action="chart_period", auto_submit=True, submit="Показать", fields=[
                Select(name="period", label="Период графика", value=period.key,
                       options=[Option(value=p.key, label=p.title.capitalize()) for p in PERIODS.values()]),
            ]),
            Image(url="data:image/png;base64," + base64.b64encode(png).decode(), alt="График Индекса Доркина"),
            survey_section(guild_id),
            days_section(guild_id),
            events_section(guild_id),
        ])

    def survey_section(guild_id: int) -> Section:
        target = surveys.targets.get(guild_id)
        status: dict[str, str] = {}
        if target is None:
            status["Статус"] = "не настроен — выберите, за кем наблюдать"
        elif target.unsubscribed:
            status["Тест"] = f"🔕 {target.user_name} отписался (наблюдение за чатом идёт)"
        elif not target.enabled:
            status["Тест"] = "выключен (наблюдение за чатом идёт)"
        else:
            waiting = any(p.guild_id == guild_id for p in surveys.pending.all())
            status["Тест"] = "⏳ ждём прохождения" if waiting else f"следующий: {format_time(surveys.next_send_at(target))}"
        if target and target.last_sent_at:
            status["Последний тест"] = format_time(target.last_sent_at)
        if target and target.last_error:
            status["Ошибка"] = target.last_error
        return Section(title="📨 Тест в личку и наблюдение", collapsed=target is not None, blocks=[
            Text("Выбранного человека бот раз в N часов просит пройти тест в личке (ночью 00:00–09:00 МСК не пишет) "
                 "и следит за его сообщениями на сервере. Слова для наблюдения — в настройках плагина."),
            KeyValue(items=status),
            Form(action="survey_setup", submit="Сохранить", fields=[
                MemberPicker(name="user_id", label="За кем наблюдать", value=target.user_id if target else None),
                NumberField(name="interval_hours", label="Тест раз в сколько часов", min=1, step=1,
                            value=target.interval_hours if target else DEFAULT_INTERVAL_HOURS),
                Toggle(name="enabled", label="Тест включён", value=target.enabled if target else True),
            ]),
            Button(action="survey_send_now", label="📨 Отправить тест сейчас", style="primary",
                   disabled=target is None or target.unsubscribed),
        ])

    def days_section(guild_id: int) -> Section:
        days = list(reversed(book.history(guild_id)))[:DAYS_SHOWN]
        rows = []
        for day in days:
            seen = book.observations.get(f"{guild_id}:{day.day.isoformat()}")
            why = ", ".join(f"{TOPIC_TITLES.get(k, k)} {v}" for k, v in (seen.reasons if seen else {}).items())
            rows.append(TableRow(cells=[
                format_day(day.day) + ("" if day.overall is not None else " (сегодня)"),
                f"{day.daily:g}", f"{day.base:g} {'тест' if day.base_from_test else 'общий'}",
                signed(day.reports), f"{signed(day.observation)}" + (f" ({why})" if why else ""), signed(day.missed),
                f"{day.overall:g}" if day.overall is not None else "—",
            ]))
        return Section(title="📅 По дням", blocks=[Table(
            columns=["День", "Дневной", "Основа", "Репорты", "Наблюдение", "Уклонения", "Общий"],
            empty="Индекс начнётся с первого пройденного теста", rows=rows)])

    def events_section(guild_id: int) -> Section:
        events = [(r.created_at, "Репорт", r.user_name, level_title(r.level)) for r in reports.for_guild(guild_id)]
        events += [(t.created_at, "Тест", t.user_name,
                    index_title(t.index) if t.completed else "🙈 уклонился")
                   for t in book.tests.all() if t.guild_id == guild_id]
        events.sort(key=lambda e: e[0], reverse=True)
        return Section(title="🧾 Репорты и тесты", collapsed=True, blocks=[Table(
            columns=["Дата и время (МСК)", "Что", "Кто", "Результат"], empty="Пока пусто",
            rows=[TableRow(cells=[format_time(e[0]), e[1], e[2], e[3]]) for e in events[:EVENTS_SHOWN]])])

    @plugin.panel_action("survey_setup")
    async def survey_setup(panel: PanelContext, values: dict) -> str:
        if not panel.guild_id:
            return "Настраивается в разделе сервера"
        user_id = values.get("user_id")
        target = surveys.configure(panel.guild_id, int(user_id) if user_id else None,
                                   values.get("interval_hours") or DEFAULT_INTERVAL_HOURS, bool(values.get("enabled")))
        if target is None:
            return "Никто не выбран: тест и наблюдение выключены"
        if target.unsubscribed:
            return "Сохранено. Он отписался от тестов — писать ему бот не будет, наблюдение идёт"
        return "Сохранено"

    @plugin.panel_action("survey_send_now")
    async def survey_send_now(panel: PanelContext, values: dict) -> str:
        if not panel.guild_id:
            return "Настраивается в разделе сервера"
        return await surveys.send_now(panel.guild_id)

    @plugin.panel_action("chart_period")
    async def set_chart_period(panel: PanelContext, values: dict) -> None:
        if values.get("period") in PERIODS and panel.guild_id:
            chart_period[panel.guild_id] = values["period"]

    return plugin
