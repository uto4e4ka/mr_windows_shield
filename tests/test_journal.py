from datetime import date, datetime, timezone

from potehinsonnet.storage import DataDir

from mr_windows_shield.events import EventBook
from mr_windows_shield.index import IndexBook
from mr_windows_shield.journal import Journal
from mr_windows_shield.models import TestResult

GUILD = 1


class Clock:
    value = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def test_closed_day_written_once_with_index_and_events(tmp_path, caplog):
    clock = Clock()
    data = DataDir(tmp_path)
    book = IndexBook(data, now=clock)
    events = EventBook(data, {}, now=clock, fetchers={})
    journal = Journal(data, book, events, now=clock)
    book.tests.put(TestResult(id="t", guild_id=GUILD, user_id=7, user_name="d", index=60, completed=True,
                              created_at=clock.value))
    events.add_manual(GUILD, date(2026, 11, 9), "Доркин купил аэрогриль", "Своё")

    assert journal.sync(GUILD) == []                      # день ещё идёт
    clock.value = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
    with caplog.at_level("INFO"):
        [entry] = journal.sync(GUILD)
    assert (entry.day, entry.daily, entry.overall) == (date(2026, 11, 9), 60, 60)
    assert "✍️ Доркин купил аэрогриль" in entry.events
    assert "Журнал 09.11.2026" in caplog.text and "Доркин купил аэрогриль" in caplog.text

    events.add_manual(GUILD, date(2026, 11, 9), "Поздно добавленное", "Своё")
    assert journal.sync(GUILD) == []                      # записанный день не переписывается
    assert [d.day for d in journal.for_guild(GUILD)] == [date(2026, 11, 9)]
