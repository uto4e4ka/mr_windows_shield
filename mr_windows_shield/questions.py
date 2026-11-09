"""Банк вопросов теста — из файла questions.toml (встроенный рядом с кодом, рабочий — data/questions.toml).

При первом запуске встроенный файл копируется в данные плагина; дальше его правят в панели (данные плагина),
после сохранения плагин перезапускается и читает его заново. Ошибка в файле — берутся встроенные вопросы,
а текст ошибки показывается в панели.

В тест идёт по одному случайному вопросу на тему (порядок тем — как в models.QUESTIONS), без повторов, пока
тема не пройдена по кругу. Ответ 1–5 идёт в вес своей темы (models.dorkin_index).
"""
import hashlib
import logging
import random
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from mr_windows_shield.models import QUESTIONS, Question

BUILTIN_FILE = Path(__file__).with_name("questions.toml")
DATA_FILE = "questions.toml"
TOPICS = [q.key for q in QUESTIONS]          # airfryer, women, warthunder, autism
ORIGINAL = {q.key: q for q in QUESTIONS}

# Хеши прошлых встроенных файлов: data/questions.toml с таким содержимым никто не правил — его можно обновить
OLD_BUILTIN_HASHES = {
    "1cac7004ecd48bf494f5d7421a6a5be1e84e3914661df2f6de6fa4df30c8ae72",    # 1.10: 100 вопросов
}

logger = logging.getLogger(__name__)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def question_id(topic: str, text: str) -> str:
    """По тексту: правка порядка в файле не путает историю, правка текста — новый вопрос."""
    return f"{topic}-{hashlib.sha1(text.strip().encode()).hexdigest()[:8]}"


def _answers(value, where: str) -> tuple[str, str, str, str, str]:
    if not isinstance(value, list) or len(value) != 5 or not all(isinstance(a, str) and a.strip() for a in value):
        raise ValueError(f"{where}: answers — ровно 5 непустых подписей")
    if any(len(a) > 80 for a in value):
        raise ValueError(f"{where}: подпись кнопки длиннее 80 символов")
    return tuple(a.strip() for a in value)


def parse_bank(text: str) -> dict[str, list[Question]]:
    data = tomllib.loads(text)
    bank = {}
    for topic in TOPICS:
        section = data.get(topic)
        if not isinstance(section, dict):
            raise ValueError(f"нет раздела [{topic}]")
        emoji = str(section.get("emoji") or ORIGINAL[topic].emoji)
        scale = _answers(section.get("answers", list(ORIGINAL[topic].answers)), f"[{topic}]")
        items = []
        for number, item in enumerate(section.get("questions") or [], start=1):
            where = f"[{topic}] вопрос {number}"
            if isinstance(item, str):
                text_, answers = item, scale
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text_, answers = item["text"], _answers(item.get("answers", list(scale)), where)
            else:
                raise ValueError(f"{where}: нужен текст в кавычках или {{ text = \"…\" }}")
            if not text_.strip():
                raise ValueError(f"{where}: пустой текст")
            items.append(Question(topic, text_.strip(), emoji, answers, id=question_id(topic, text_)))
        if not items:
            raise ValueError(f"[{topic}]: нужен хотя бы один вопрос")
        bank[topic] = list({q.id: q for q in items}.values())       # одинаковые тексты — один вопрос
    return bank


@dataclass
class QuestionBank:
    bank: dict[str, list[Question]]
    source: str = "встроенные"
    error: str = ""                                  # ошибка в data/questions.toml — для панели
    by_id: dict[str, Question] = field(init=False)

    def __post_init__(self):
        self.by_id = {q.id: q for items in self.bank.values() for q in items}

    @classmethod
    def builtin(cls) -> "QuestionBank":
        return cls(parse_bank(BUILTIN_FILE.read_text(encoding="utf-8")))

    @classmethod
    def load(cls, data_dir: Path) -> "QuestionBank":
        """data/questions.toml (создаётся из встроенного, если его нет); ошибка — встроенные + текст ошибки."""
        path = Path(data_dir) / DATA_FILE
        try:
            if not path.exists() or file_hash(path) in OLD_BUILTIN_HASHES:
                # нет файла или там прошлые встроенные вопросы без правок — кладём новые встроенные
                shutil.copyfile(BUILTIN_FILE, path)
            return cls(parse_bank(path.read_text(encoding="utf-8")), source=f"data/{DATA_FILE}")
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            logger.error(f"{DATA_FILE}: {e} — беру встроенные вопросы")
            bank = cls.builtin()
            bank.error = str(e)
            return bank

    def count(self) -> dict[str, int]:
        return {topic: len(items) for topic, items in self.bank.items()}

    def get(self, qid: str, step: int) -> Question:
        """Вопрос по id; удалённый из файла — исходный вопрос этой темы (тест не ломается)."""
        return self.by_id.get(qid) or ORIGINAL[TOPICS[step]]

    def first_ids(self) -> list[str]:
        return [self.bank[topic][0].id for topic in TOPICS]

    def pick(self, used: list[str], rng: random.Random | None = None) -> list[str]:
        """По одному непройденному вопросу на тему; тема пройдена целиком — круг заново."""
        rng = rng or random
        chosen = []
        for topic in TOPICS:
            ids = [q.id for q in self.bank[topic]]
            chosen.append(rng.choice([i for i in ids if i not in used] or ids))
        return chosen

    def mark_used(self, used: list[str], chosen: list[str]) -> list[str]:
        """Запомнить выбранные (и забыть удалённые из файла). Тема пройдена по кругу — история заново."""
        result = [i for i in used if i in self.by_id]
        for qid in chosen:
            if qid not in self.by_id:
                continue
            ids = {q.id for q in self.bank[self.by_id[qid].key]}
            if ids <= set(result):
                result = [i for i in result if i not in ids]
            result.append(qid)
        return result
