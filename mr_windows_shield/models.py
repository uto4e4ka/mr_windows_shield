"""Шкала выделения и записи о ней — общие для репортов, графика и (позже) опроса и авто-слежки."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from potehinsonnet.net_models.discord_models import ComponentButtonStyle

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
    source: str = "report"      # откуда запись: report — /dorkin report (дальше — auto, survey)


def level_title(level: int) -> str:
    item = LEVELS[level - 1]
    return f"{item.emoji} {item.label}"


def format_time(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
