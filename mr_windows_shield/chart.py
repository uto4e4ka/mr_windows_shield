"""График степени выделения по времени: PNG для панели и для Discord."""
import io
from dataclasses import dataclass
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")       # без окна: рисуем только в файл

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from mr_windows_shield.models import LEVELS, MISSED_LEVEL, MSK, Report  # noqa: E402


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

# Откуда точка: ручной репорт, позже — авто-обнаружение и самооценка из опроса
SOURCES = {
    "report": ("Репорты", "#e8590c", "o"),
    "survey": ("Самооценка (опрос)", "#4dabf7", "D"),
    "missed": ("Уклонился от ответа", "#868e96", "X"),
}
LEVEL_COLORS = ["#2f9e44", "#f0c419", "#f08c00", "#e03131", "#9c36b5"]


def period_by_name(name: str | None) -> Period:
    """Период по ключу или по-русски («сутки», «неделя», «месяц»); непонятное — неделя."""
    aliases = {"сутки": "day", "день": "day", "неделя": "week", "месяц": "month"}
    key = (name or "").strip().lower()
    return PERIODS.get(aliases.get(key, key), PERIODS[DEFAULT_PERIOD])


def render_chart(reports: list[Report], period: Period, now: datetime) -> bytes:
    """PNG: по горизонтали время (МСК), по вертикали степень; пустой период — подпись «нет репортов»."""
    start = now - period.span
    points = sorted((r for r in reports if r.created_at >= start), key=lambda r: r.created_at)

    fig, ax = plt.subplots(figsize=(9, 4), dpi=110)
    try:
        fig.patch.set_facecolor("#1e1f22")
        ax.set_facecolor("#2b2d31")
        for spine in ax.spines.values():
            spine.set_color("#4e5058")
        ax.tick_params(colors="#dbdee1")

        # Полосы уровней на фоне — сразу видно, в какой зоне точка
        for number, color in enumerate(LEVEL_COLORS, start=1):
            ax.axhspan(number - 0.5, number + 0.5, color=color, alpha=0.08, linewidth=0)

        for source, (label, color, marker) in SOURCES.items():
            own = [r for r in points if getattr(r, "source", "report") == source]
            if not own:
                continue
            times = [r.created_at.astimezone(MSK) for r in own]
            levels = [r.level for r in own]
            if source == "missed":      # пропуски — отдельным рядом снизу, без линии
                ax.scatter(times, levels, color=color, marker=marker, s=60, zorder=3, label=label)
                continue
            ax.plot(times, levels, color=color, alpha=0.35, linewidth=1.5)
            ax.scatter(times, levels, color=[LEVEL_COLORS[lv - 1] for lv in levels], edgecolors=color,
                       marker=marker, s=60, zorder=3, label=label)

        ticks = list(range(1, len(LEVELS) + 1))
        labels = [level.label for level in LEVELS]
        if any(r.level == MISSED_LEVEL for r in points):
            ticks.insert(0, MISSED_LEVEL)
            labels.insert(0, "Уклонился")
        ax.set_ylim(ticks[0] - 0.5, len(LEVELS) + 0.5)
        ax.set_yticks(ticks, labels)
        ax.set_xlim(start.astimezone(MSK), now.astimezone(MSK))
        ax.xaxis.set_major_formatter(mdates.DateFormatter(period.date_format, tz=MSK))
        ax.grid(axis="x", color="#4e5058", alpha=0.5, linewidth=0.6)
        ax.set_title(f"Степень выделения Доркина за {period.title}", color="#f2f3f5", fontsize=12)

        if points:
            legend = ax.legend(loc="upper left", facecolor="#1e1f22", edgecolor="#4e5058", fontsize=9)
            for text in legend.get_texts():
                text.set_color("#dbdee1")
        else:
            ax.text(0.5, 0.5, "За этот период записей нет", transform=ax.transAxes, ha="center",
                    va="center", color="#949ba4", fontsize=12)

        fig.tight_layout()
        out = io.BytesIO()
        fig.savefig(out, format="png", facecolor=fig.get_facecolor())
        return out.getvalue()
    finally:
        plt.close(fig)
