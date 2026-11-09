"""Шкала выделения, тест, индексы и записи — общее для репортов, опроса, наблюдения, графика и панели."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field
from potehinsonnet.net_models.discord_models import ComponentButtonStyle

MSK = timezone(timedelta(hours=3))      # время в сообщениях, панели и границы суток — московские


@dataclass(frozen=True)
class Level:
    label: str                  # кнопка в скрытом сообщении
    emoji: str
    style: ComponentButtonStyle
    alert: str                  # что пишется в чат после «Внимание! Замечен…»


# Шкала выделения: порядок = степень (1 — слабее всего). Индекс 0–100 делится на неё по 20
LEVELS: list[Level] = [
    Level("Лёгкое", "🟢", ComponentButtonStyle.SUCCESS, "Доркин. Лёгкое выделение, ситуация под контролем."),
    Level("Умеренное", "🟡", ComponentButtonStyle.SECONDARY, "Доркин. Умеренное выделение, сохраняйте бдительность."),
    Level("Сильное", "🟠", ComponentButtonStyle.PRIMARY, "Доркин. Сильное выделение, держитесь подальше."),
    Level("Обильное", "🔴", ComponentButtonStyle.DANGER, "Доркин. Обильное выделение, наденьте средства защиты!"),
    Level("Критическое", "☢️", ComponentButtonStyle.DANGER, "ДОРКИН. КРИТИЧЕСКОЕ ВЫДЕЛЕНИЕ. ЭВАКУАЦИЯ!"),
]
INDEX_STEP = 100 / len(LEVELS)


def level_title(level: int) -> str:
    item = LEVELS[level - 1]
    return f"{item.emoji} {item.label}"


def level_for_index(index: float) -> int:
    """0–19 → 1 (лёгкое) … 80–100 → 5 (критическое)."""
    return min(len(LEVELS), int(max(0.0, index) // INDEX_STEP) + 1)


def index_title(index: float | None) -> str:
    if index is None:
        return "нет данных"
    return f"{round(index)} · {level_title(level_for_index(index))} выделение"


# --- тест в личке ------------------------------------------------------------------------

@dataclass(frozen=True)
class Question:
    key: str
    text: str
    emoji: str
    answers: tuple[str, str, str, str, str]     # подписи кнопок 1…5


QUESTIONS: list[Question] = [
    Question("airfryer", "Оцените ваше желание воспользоваться аэрогрилем", "🍗",
             ("Нет", "Слабое", "Есть", "Сильное", "Жарю прямо сейчас")),
    Question("women", "Оцените ваше желание посмотреть на женщин", "👀",
             ("Нет", "Слабое", "Есть", "Сильное", "Неудержимое")),
    Question("warthunder", "Оцените ваше желание поиграть в War Thunder", "✈️",
             ("Нет", "Слабое", "Есть", "Сильное", "Уже в ангаре")),
    Question("autism", "Оцените ваш уровень аутизма", "🧩",
             ("1", "2", "3", "4", "5")),
]
QUESTION_BY_KEY = {q.key: q for q in QUESTIONS}

# Веса желаний в «базе»; аутизм — множитель (1 → ×1, 5 → ×2)
WEIGHTS = {"airfryer": 0.30, "women": 0.25, "warthunder": 0.25}
MAX_BASE = sum(WEIGHTS.values())


def dorkin_index(answers: dict[str, int]) -> float:
    """Индекс Доркина 0–100 по ответам 1–5:  база × (1 + аутизм) / 1.6 × 100."""
    share = {key: (value - 1) / 4 for key, value in answers.items()}
    base = sum(weight * share.get(key, 0.0) for key, weight in WEIGHTS.items())
    return round(base * (1 + share.get("autism", 0.0)) / (2 * MAX_BASE) * 100, 1)


# --- дни ------------------------------------------------------------------------------------

def msk_day(moment: datetime) -> date:
    return moment.astimezone(MSK).date()


def day_start(day: date) -> datetime:
    """Начало суток по МСК (в UTC)."""
    return datetime(day.year, day.month, day.day, tzinfo=MSK).astimezone(timezone.utc)


def format_time(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def format_day(day: date) -> str:
    return day.strftime("%d.%m.%Y")


# --- записи ---------------------------------------------------------------------------------

class Report(BaseModel):
    """/dorkin report."""
    id: str
    guild_id: int
    channel_id: int
    user_id: int
    user_name: str
    level: int                  # 1..len(LEVELS)
    created_at: datetime        # UTC
    source: str = "report"


class TestResult(BaseModel):
    """Тест в личке: пройден (completed) или брошен / не начат за час (уклонился)."""
    __test__ = False            # не путать pytest: это не класс с тестами
    id: str
    guild_id: int
    user_id: int
    user_name: str
    answers: dict[str, int] = Field(default_factory=dict)
    index: float | None = None  # только у пройденного
    completed: bool
    created_at: datetime


class DayObservation(BaseModel):
    """Наблюдение за чатом за сутки: очки и за что они."""
    id: str                     # "<guild>:<YYYY-MM-DD>"
    guild_id: int
    day: date
    points: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)


class DayIndex(BaseModel):
    """Расчёт дневного индекса: из чего сложился и каким стал общий после закрытия дня."""
    id: str                     # "<guild>:<YYYY-MM-DD>"
    guild_id: int
    day: date
    base: float                 # тест за день (среднее) или общий индекс на начало дня
    base_from_test: bool
    reports: float              # вклад /dorkin report (±10)
    observation: float          # вклад наблюдения (до +15)
    missed: float               # +5 за каждое уклонение
    daily: float                # итог 0–100
    overall: float | None = None    # общий после закрытия дня; None — день ещё идёт
