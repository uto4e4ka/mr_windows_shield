import random
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from potehinsonnet.net_models.discord_models import DiscordMessageResponse
from potehinsonnet.storage import DataDir

from mr_windows_shield.index import IndexBook
from mr_windows_shield.questions import BUILTIN_FILE, TOPICS, QuestionBank, parse_bank, question_id
from mr_windows_shield.survey import ANSWER_BUTTON, Surveys
from tests.test_survey import click

BANK = QuestionBank.builtin()


def test_builtin_file_has_100_unique_questions():
    assert BANK.count() == {t: 25 for t in TOPICS}
    assert len(BANK.by_id) == 100
    assert [BANK.by_id[i].text for i in BANK.first_ids()][0] == "Оцените ваше желание воспользоваться аэрогрилем"


def test_own_scale_and_default_scale():
    women = BANK.by_id[question_id("women", "Оцените вашу тягу к женщинам по шкале от монаха до пса")]
    assert women.answers[-1] == "Пёс"
    assert BANK.bank["warthunder"][1].answers[-1] == "Уже в ангаре"


@pytest.mark.parametrize("text, error", [
    ("[airfryer]\nquestions = ['a']", "нет раздела [women]"),
    ("[airfryer]\nanswers = ['1']\nquestions = ['a']", "ровно 5"),
    ("[airfryer]\nquestions = [1]", "вопрос 1"),
    ("[airfryer]\nquestions = []", "хотя бы один"),
])
def test_bad_file_errors(text, error):
    with pytest.raises(ValueError, match=error.replace("[", r"\[").replace("]", r"\]")):
        parse_bank(text)


def test_load_copies_builtin_then_reads_edits(tmp_path):
    bank = QuestionBank.load(tmp_path)
    assert (tmp_path / "questions.toml").read_text(encoding="utf-8") == BUILTIN_FILE.read_text(encoding="utf-8")
    assert bank.source == "data/questions.toml" and not bank.error

    edited = "\n".join(f'[{t}]\nquestions = ["Свой вопрос {t}"]' for t in TOPICS)
    (tmp_path / "questions.toml").write_text(edited, encoding="utf-8")
    bank = QuestionBank.load(tmp_path)
    assert bank.count() == {t: 1 for t in TOPICS}
    assert bank.bank["women"][0].answers[-1] == "Неудержимое"          # шкала темы по умолчанию


def test_broken_file_falls_back_to_builtin(tmp_path):
    (tmp_path / "questions.toml").write_text("[airfryer\n", encoding="utf-8")
    bank = QuestionBank.load(tmp_path)
    assert bank.error and bank.source == "встроенные" and len(bank.by_id) == 100


def test_one_per_topic_and_no_repeats_until_exhausted():
    rng, used, seen = random.Random(2), [], []
    for _ in range(25):
        chosen = BANK.pick(used, rng)
        assert [BANK.by_id[q].key for q in chosen] == TOPICS
        used = BANK.mark_used(used, chosen)
        seen += chosen
    assert len(set(seen)) == 100
    chosen = BANK.pick(used, rng)
    assert BANK.mark_used(used, chosen) == chosen                      # новый круг


def test_removed_question_does_not_break():
    assert BANK.get("airfryer-deadbeef", 0).text == "Оцените ваше желание воспользоваться аэрогрилем"
    assert BANK.mark_used(["gone-1"], []) == []


async def test_random_questions_in_test_and_result(tmp_path):
    clock = lambda: datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    discord = AsyncMock()
    discord.send_direct_message.return_value = DiscordMessageResponse(channel_id=900, message_id=10)
    book = IndexBook(DataDir(tmp_path), now=clock)
    surveys = Surveys(DataDir(tmp_path), discord, book, now=clock, bank=BANK)
    chosen = [BANK.bank["airfryer"][5].id, question_id("women", "Оцените вашу тягу к женщинам по шкале от монаха до пса"),
              BANK.bank["warthunder"][3].id, BANK.bank["autism"][-1].id]
    surveys.pick = lambda used: chosen
    surveys.configure(1, 77, 6, True)
    await surveys.tick()

    text = discord.send_direct_message.await_args.args[1].containers[0].components[0].content
    assert "жениться на аэрогриле" in text
    [survey] = surveys.pending.all()
    for step in range(4):
        response = await surveys.answer(click(f"{ANSWER_BUTTON}|{survey.id}|{step}|5"))
        if step == 0:
            assert "от монаха до пса" in response.containers[0].components[0].content
    final = response.containers[0].components[0].content
    assert "**Пёс**" in final and "**Легенда**" in final
    [result] = book.tests.all()
    assert result.index == 100 and result.questions == chosen
    assert surveys.targets.get(1).used_questions == chosen
