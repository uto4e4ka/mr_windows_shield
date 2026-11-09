"""Точка входа плагина (plugin.toml → runtime.entry). Запуск для разработки: python -m potehinsonnet.run ."""
import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from potehinsonnet import Plugin, PluginContext
from potehinsonnet.net_models.panel_ui import (
    Button, ChannelPicker, Form, Image, KeyValue, MemberPicker, NumberField, Option, PanelContext, PanelPage, Section,
    Select, Table, TableRow, Text, TextField, Toggle,
)

from mr_windows_shield.chart import DEFAULT_PERIOD, PERIODS, render_chart
from mr_windows_shield.dorkin import DorkinReports
from mr_windows_shield.events import SOURCES, EventBook
from mr_windows_shield.index import IndexBook
from mr_windows_shield.linux_watch import LinuxWatch, parse_words
from mr_windows_shield.models import DayIndex, format_day, format_time, index_title, level_title
from mr_windows_shield.observe import TOPIC_TITLES, ChatObserver, build_topics
from mr_windows_shield.summary import SUMMARY_HOUR, Summaries
from mr_windows_shield.survey import DEFAULT_INTERVAL_HOURS, Surveys

DEFAULT_LINUX_WORDS = "linux"
DAYS_SHOWN = 30
EVENTS_SHOWN = 100
WORLD_EVENTS_SHOWN = 40
SCHEDULER_SECONDS = 60

logger = logging.getLogger(__name__)


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
    events = EventBook(ctx.data, ctx.settings)
    reports.events = events
    summaries = Summaries(ctx.data, plugin.discord, book, events)
    plugin.register(reports, surveys)
    plugin.add_lifespan(surveys.running())
    plugin.add_lifespan(ChatObserver(plugin.nats, book, surveys, build_topics(ctx.settings), plugin.discord).running())

    @asynccontextmanager
    async def scheduler():
        """Раз в минуту: подтянуть события (сами решают, пора ли) и отправить утреннюю сводку."""
        async def loop():
            while True:
                for job in (events.refresh, summaries.tick):
                    try:
                        await job()
                    except Exception:
                        logger.exception(f"Ошибка в расписании: {job.__qualname__}")
                await asyncio.sleep(SCHEDULER_SECONDS)

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()

    plugin.add_lifespan(scheduler())

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
        png = render_chart(book, guild_id, period, book.now(), events=events)
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
            summary_section(guild_id),
            days_section(guild_id),
            world_section(guild_id),
            events_section(guild_id),
        ])

    def summary_section(guild_id: int) -> Section:
        channel = summaries.channel(guild_id)
        return Section(title="📊 Утренняя сводка", collapsed=channel is not None, blocks=[
            Text(f"Каждый день в {SUMMARY_HOUR}:00 МСК — итоги вчерашнего дня, общий индекс, события и график за неделю."),
            Form(action="summary_channel", submit="Сохранить", fields=[
                ChannelPicker(name="channel_id", label="Канал для сводки", value=channel),
            ]),
            Button(action="summary_send_now", label="📊 Отправить сводку сейчас", disabled=channel is None),
        ])

    def world_section(guild_id: int) -> Section:
        stats = events.analysis(guild_id, [d for d in book.history(guild_id) if d.overall is not None])
        listed = events.for_guild(guild_id)[:WORLD_EVENTS_SHOWN]
        errors = {f"Ошибка: {SOURCES.get(k, k)}": v for k, v in events.last_errors.items()}
        return Section(title="🗓 События и анализ", collapsed=True, blocks=[
            Text("Средний дневной индекс в дни событий и на следующий день против остальных дней "
                 "(по закрытым дням, категория — от 3 дней)."),
            Table(columns=["Категория", "Дней", "В день события", "На следующий", "В остальные", "Разница"],
                  empty="Пока мало данных для анализа", rows=[
                      TableRow(cells=[s.category, str(s.days), f"{s.on_day:g}",
                                      f"{s.next_day:g}" if s.next_day is not None else "—",
                                      f"{s.other:g}", signed(round(s.on_day - s.other, 1))]) for s in stats]),
            *([KeyValue(items=errors)] if errors else []),
            Form(action="event_add", submit="Добавить событие", fields=[
                TextField(name="day", label="Дата", placeholder="ДД.ММ.ГГГГ", required=True),
                TextField(name="title", label="Событие", placeholder="Новый аэрогриль у Доркина", required=True),
                TextField(name="category", label="Категория", placeholder="Своё"),
            ]),
            Button(action="events_refresh", label="🔄 Обновить источники"),
            Table(columns=["Дата", "Источник", "Категория", "Событие"], empty="Событий пока нет", rows=[
                TableRow(cells=[format_day(e.day), SOURCES.get(e.source, e.source), e.category, e.title[:160]],
                         actions=[Button(action="event_delete", label="×", confirm=f"Удалить «{e.title}»?",
                                         values={"id": e.id})] if e.source == "manual" else [])
                for e in listed]),
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

    @plugin.panel_action("summary_channel")
    async def summary_channel(panel: PanelContext, values: dict) -> str:
        if not panel.guild_id:
            return "Настраивается в разделе сервера"
        channel = values.get("channel_id")
        summaries.set_channel(panel.guild_id, int(channel) if channel else None)
        return "Канал сохранён" if channel else "Сводка выключена"

    @plugin.panel_action("summary_send_now")
    async def summary_send_now(panel: PanelContext, values: dict) -> str:
        if not panel.guild_id:
            return "Настраивается в разделе сервера"
        return await summaries.send(panel.guild_id)

    @plugin.panel_action("event_add")
    async def event_add(panel: PanelContext, values: dict) -> str:
        if not panel.guild_id:
            return "Настраивается в разделе сервера"
        try:
            day = datetime.strptime(str(values.get("day") or "").strip(), "%d.%m.%Y").date()
        except ValueError:
            raise ValueError("Дата — в виде ДД.ММ.ГГГГ, например 09.11.2026") from None
        title = str(values.get("title") or "").strip()
        if not title:
            raise ValueError("Напишите, что за событие")
        events.add_manual(panel.guild_id, day, title, str(values.get("category") or ""))
        return "Событие добавлено"

    @plugin.panel_action("event_delete")
    async def event_delete(panel: PanelContext, values: dict) -> str:
        if panel.guild_id and events.delete_manual(panel.guild_id, str(values.get("id"))):
            return "Удалено"
        return "Такого события нет"

    @plugin.panel_action("events_refresh")
    async def events_refresh(panel: PanelContext, values: dict) -> str:
        await events.refresh(force=True)
        if events.last_errors:
            return "Обновлено с ошибками: " + "; ".join(
                f"{SOURCES.get(k, k)} — {v}" for k, v in events.last_errors.items())
        return "Источники обновлены"

    @plugin.panel_action("chart_period")
    async def set_chart_period(panel: PanelContext, values: dict) -> None:
        if values.get("period") in PERIODS and panel.guild_id:
            chart_period[panel.guild_id] = values["period"]

    return plugin
