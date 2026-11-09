"""График Индекса Доркина: дневной — столбики, общий — линия, репорты и тесты — точки. PNG для панели и Discord."""
import io
from dataclasses import dataclass
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")       # без окна: рисуем только в файл

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from mr_windows_shield.models import INDEX_STEP, LEVELS, MSK, day_start  # noqa: E402


@dataclass(frozen=True)
class Period:
    key: str
    title: str
    span: timedelta
    date_format: str


PERIODS = {
    "day": Period("day", "сутки", timedelta(days=1), "%H:%M"),
    "week": Period("week", "неделю", timedelta(days=7), "%d.%m"),
    "month": Period("month", "месяц", timedelta(days=30), "%d.%m"),
}
DEFAULT_PERIOD = "week"

LEVEL_COLORS = ["#2f9e44", "#f0c419", "#f08c00", "#e03131", "#9c36b5"]
TEXT, GRID, BACK, PANEL = "#dbdee1", "#4e5058", "#1e1f22", "#2b2d31"


def period_by_name(name: str | None) -> Period:
    """Период по ключу или по-русски («сутки», «неделя», «месяц»); непонятное — неделя."""
    aliases = {"сутки": "day", "день": "day", "неделя": "week", "месяц": "month"}
    key = (name or "").strip().lower()
    return PERIODS.get(aliases.get(key, key), PERIODS[DEFAULT_PERIOD])


def _level_color(value: float) -> str:
    return LEVEL_COLORS[min(len(LEVELS), int(max(0.0, value) // INDEX_STEP) + 1) - 1]


def render_chart(book, guild_id: int, period: Period, now: datetime, events=None) -> bytes:
    """events — EventBook: свои события и праздники — пунктиром с подписью, новости War Thunder — метками сверху."""
    start = now - period.span
    local = lambda moment: moment.astimezone(MSK)  # noqa: E731

    days = [d for d in book.history(guild_id) if day_start(d.day) + timedelta(days=1) > start]
    tests = [t for t in book.tests.all() if t.guild_id == guild_id and t.created_at >= start]
    reports = [r for r in book.reports.all() if r.guild_id == guild_id and r.created_at >= start]

    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=110)
    try:
        fig.patch.set_facecolor(BACK)
        ax.set_facecolor(PANEL)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.tick_params(colors=TEXT)

        # Полосы степеней на фоне: 0–19 лёгкое … 80–100 критическое
        for number, color in enumerate(LEVEL_COLORS):
            ax.axhspan(number * INDEX_STEP, (number + 1) * INDEX_STEP, color=color, alpha=0.08, linewidth=0)

        if days:
            starts = [local(day_start(d.day)) for d in days]
            ax.bar(starts, [d.daily for d in days], width=0.9, align="edge", alpha=0.3,
                   color=[_level_color(d.daily) for d in days], label="Дневной индекс")
            closed = [(local(day_start(d.day) + timedelta(days=1)), d.overall) for d in days if d.overall is not None]
            if closed:
                ax.plot([x for x, _ in closed], [y for _, y in closed], color="#f2f3f5", linewidth=2,
                        marker="o", markersize=4, label="Общий индекс")

        done = [t for t in tests if t.completed]
        if done:
            ax.scatter([local(t.created_at) for t in done], [t.index for t in done], marker="D", s=55, zorder=4,
                       color=[_level_color(t.index) for t in done], edgecolors="#4dabf7", label="Тест")
        missed = [t for t in tests if not t.completed]
        if missed:
            ax.scatter([local(t.created_at) for t in missed], [3] * len(missed), marker="X", s=60, zorder=4,
                       color="#868e96", label="Уклонился")
        if reports:
            # Репорт — точка в середине своей полосы
            ax.scatter([local(r.created_at) for r in reports], [(r.level - 0.5) * INDEX_STEP for r in reports],
                       marker="o", s=40, zorder=4, color=[LEVEL_COLORS[r.level - 1] for r in reports],
                       edgecolors="#e8590c", label="Репорт")

        if events is not None:
            shown = [e for e in events.for_guild(guild_id) if day_start(e.day) + timedelta(days=1) > start
                     and day_start(e.day) <= now]
            marked = [e for e in shown if e.source in ("manual", "holiday")]
            for event in marked:
                middle = local(day_start(event.day) + timedelta(hours=12))
                ax.axvline(middle, color="#74c0fc" if event.source == "manual" else "#ffd43b",
                           linestyle=":", linewidth=1.2, zorder=2)
                ax.text(middle, 97, event.title[:28], rotation=90, va="top", ha="right", fontsize=7,
                        color="#c1c2c5", zorder=5)
            game = sorted({e.day for e in shown if e.source == "warthunder"})
            if game:
                ax.scatter([local(day_start(d) + timedelta(hours=12)) for d in game], [99] * len(game),
                           marker="v", s=30, color="#ffa94d", zorder=5, clip_on=False, label="Новости WT")

        ax.set_ylim(0, 100)
        ax.set_yticks([(n + 0.5) * INDEX_STEP for n in range(len(LEVELS))], [lv.label for lv in LEVELS])
        numbers = ax.twinx()                    # справа — сам индекс 0–100
        numbers.set_ylim(0, 100)
        numbers.set_yticks(range(0, 101, 20))
        numbers.tick_params(colors="#949ba4", labelsize=8)
        for spine in numbers.spines.values():
            spine.set_visible(False)
        ax.set_xlim(local(start), local(now))
        ax.xaxis.set_major_formatter(mdates.DateFormatter(period.date_format, tz=MSK))
        ax.grid(axis="x", color=GRID, alpha=0.5, linewidth=0.6)
        ax.set_title(f"Индекс Доркина за {period.title}", color="#f2f3f5", fontsize=12)

        if ax.get_legend_handles_labels()[0]:
            legend = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), facecolor=BACK, edgecolor=GRID,
                               fontsize=8, ncol=5)
            for text in legend.get_texts():
                text.set_color(TEXT)
        else:
            ax.text(0.5, 0.5, "Данных пока нет: индекс начнётся с первого теста", transform=ax.transAxes,
                    ha="center", va="center", color="#949ba4", fontsize=11)

        fig.tight_layout()
        out = io.BytesIO()
        fig.savefig(out, format="png", facecolor=fig.get_facecolor())
        return out.getvalue()
    finally:
        plt.close(fig)
