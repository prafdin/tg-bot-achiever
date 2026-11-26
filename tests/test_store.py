import sqlite3

import app.store as storage
from app.store import SilenceTier, Store


DAY = 86400


def test_award_and_consent(tmp_path):
    path = tmp_path / "app.db"
    store = Store(path)
    assert not store.record_message(1, 1, 2, 9)
    store.set_consent(2, True, 10)
    assert not store.record_message(1, 1, 2, 9)
    for message in range(1, 10):
        assert store.record_message(1, message, 2, 11)
    assert not store.record_message(1, 1, 2, 11)
    assert store.award_daily("2026-11-25", checked_at=11) == 0
    assert store.record_message(1, 10, 2, 12)
    store.set_consent(2, False, 13)
    assert not store.record_message(1, 11, 2, 14)
    store = Store(path)
    assert store.award_daily("2026-11-26", checked_at=14) == 0
    assert store.profile(2) == (False, None)
    assert store.pending_digest() is None
    store.set_consent(2, True, 15)
    assert store.award_daily("2026-11-26", checked_at=15) == 0
    assert store.award_daily("2026-11-27", checked_at=15) == 1
    store.set_consent(2, False, 16)
    assert store.award_daily("2026-11-28", checked_at=16) == 0
    assert store.profile(2) == (False, "2026-11-27")
    day, groups = store.pending_digest()
    assert day == "2026-11-27"
    assert [(group.name, group.user_ids) for group in groups] == [("Десяточка", [2])]
    store.mark_digest_sent("2026-11-27")
    assert store.pending_digest() is None


def test_silence_progress_survives_messages_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "SILENCE_TIERS", tuple(
        SilenceTier(days, f"Test tier {days}", f"test-{days}", "")
        for days in (1, 7, 14, 30)
    ))
    path = tmp_path / "app.db"
    store = Store(path)
    store.set_consent(2, True, 1)
    store.set_consent(3, True, 1)
    assert store.record_message(1, 1, 2, 2)
    assert store.award_daily("2026-11-25", checked_at=DAY + 1) == 0
    assert store.award_daily("2026-11-26", checked_at=DAY + 2) == 1
    assert store.silence_history(2) == [(1, "2026-11-26")]
    assert not store.silence_history(3)
    assert store.record_message(1, 2, 2, 2 * DAY, count_for_message_award=False)
    assert store.record_message(1, 3, 2, DAY, count_for_message_award=False)
    store = Store(path)
    assert store.award_daily("2026-12-03", checked_at=9 * DAY - 1) == 0
    assert store.award_daily("2026-12-04", checked_at=9 * DAY) == 1
    assert store.silence_history(2)[-1] == (2, "2026-12-04")
    assert store.award_daily("2026-12-05", checked_at=40 * DAY) == 1
    assert store.silence_history(2)[-1] == (3, "2026-12-05")
    assert store.award_daily("2026-12-05", checked_at=40 * DAY) == 0
    assert store.award_daily("2026-12-06", checked_at=41 * DAY) == 1
    assert store.record_message(1, 4, 2, 41 * DAY + 1)
    assert store.award_daily("2026-12-07", checked_at=42 * DAY) == 0
    assert [tier for tier, _ in store.silence_history(2)] == [1, 2, 3, 4]
    assert store.profile(2) == (True, None)


def test_silence_restarts_at_renewed_consent(tmp_path):
    store = Store(tmp_path / "app.db")
    store.set_consent(2, True, 1)
    assert store.record_message(1, 1, 2, 2)
    store.set_consent(2, False, 3)
    assert not store.record_message(1, 2, 2, 4, count_for_message_award=False)
    assert store.award_daily("2026-11-25", checked_at=DAY + 2) == 0
    store.set_consent(2, True, 2 * DAY)
    store.set_consent(2, True, 2 * DAY + 60)
    assert store.award_daily("2026-11-26", checked_at=3 * DAY - 1) == 0
    assert store.award_daily("2026-11-27", checked_at=3 * DAY) == 1


def test_silence_uses_last_real_message_from_existing_database(tmp_path):
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE messages (
                chat_id INTEGER, message_id INTEGER, user_id INTEGER, sent_at INTEGER,
                PRIMARY KEY (chat_id, message_id)
            );
            INSERT INTO messages VALUES (1, 1, 2, 2), (1, -1, 2, 100);
        """)
    store = Store(path)
    store.set_consent(2, True, 1)
    assert store.award_daily("2026-11-25", checked_at=DAY + 1) == 0
    assert store.award_daily("2026-11-26", checked_at=DAY + 2) == 1
    assert store.silence_history(2) == [(1, "2026-11-26")]
    assert store.record_message(1, 2, 2, 2 * DAY, count_for_message_award=False)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO messages VALUES (1, 3, 2, ?)", (3 * DAY,))
    store = Store(path)
    with store.transaction() as db:
        assert db.execute("SELECT sent_at FROM last_activity WHERE user_id=2").fetchone() == (3 * DAY,)
    assert store.award_daily("2026-12-05", checked_at=40 * DAY) == 0


def test_removed_silence_tiers_are_retained_but_not_displayed_or_sent(tmp_path):
    store = Store(tmp_path / "app.db")
    with store.transaction() as db:
        db.execute("INSERT INTO silence_awards VALUES (2, 1, '2026-11-25'), (2, 2, '2026-11-26')")
        db.execute("INSERT INTO daily VALUES ('2026-11-26', 0)")
    assert store.silence_history(2) == [(1, "2026-11-25")]
    assert store.pending_digest() == ("2026-11-26", [])
    assert not store.achievement_discovered("wanted")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM silence_awards").fetchone() == (2,)
