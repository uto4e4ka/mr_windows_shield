import random
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from potehinsonnet.net_models.discord_models import DiscordMessageResponse
from potehinsonnet.storage import DataDir

from mr_windows_shield.index import IndexBook
from mr_windows_shield.questions import BANK, QUESTION_BY_ID, TOPICS, mark_used, pick_questions
from mr_windows_shield.survey import ANSWER_BUTTON, Surveys
from tests.test_survey import click


def test_bank_has_100_unique_questions():
    assert {t: len(q) for t, q in BANK.items()} == {t: 25 for t in TOPICS}
    assert len(QUESTION_BY_ID) == 100
    assert len({q.text for q in QUESTION_BY_ID.values()}) == 100
    assert all(len(q.answers) == 5 and all(0 < len(a) <= 80 for a in q.answers) for q in QUESTION_BY_ID.values())


def test_one_per_topic_in_order():
    chosen = pick_questions([], random.Random(1))
    assert [QUESTION_BY_ID[q].key for q in chosen] == TOPICS


def test_no_repeats_until_topic_exhausted():
    rng, used, seen = random.Random(2), [], []
    for _ in range(25):
        chosen = pick_questions(used, rng)
        used = mark_used(used, chosen)
        seen += chosen
    assert len(set(seen)) == 100                    # 25 тестов — все 100 вопросов ровно по разу
    chosen = pick_questions(used, rng)              # новый круг
    used = mark_used(used, chosen)
    assert used == chosen


async def test_random_questions_in_test_and_result(tmp_path):
    clock = lambda: datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    discord = AsyncMock()
    discord.send_direct_message.return_value = DiscordMessageResponse(channel_id=900, message_id=10)
    book = IndexBook(DataDir(tmp_path), now=clock)
    surveys = Surveys(DataDir(tmp_path), discord, book, now=clock)
    surveys.pick = lambda used: ["airfryer05", "women24", "warthunder03", "autism24"]
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
    assert result.index == 100 and result.questions == ["airfryer05", "women24", "warthunder03", "autism24"]
    assert surveys.targets.get(1).used_questions == result.questions
