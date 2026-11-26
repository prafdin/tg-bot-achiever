from app.store import Store


def test_award_and_consent(tmp_path):
    path = tmp_path / "app.db"
    store = Store(path)
    assert not store.record_message(1, 1, 2, 9)
    store.set_consent(2, True, 10)
    assert not store.record_message(1, 1, 2, 9)
    for message in range(1, 10):
        assert store.record_message(1, message, 2, 11)
    assert not store.record_message(1, 1, 2, 11)
    assert store.award_daily("2026-11-25") == 0
    assert store.record_message(1, 10, 2, 12)
    store.set_consent(2, False, 13)
    assert not store.record_message(1, 11, 2, 14)
    store = Store(path)
    assert store.award_daily("2026-11-26") == 0
    assert store.profile(2) == (False, None)
    assert store.pending_digest() is None
    store.set_consent(2, True, 15)
    assert store.award_daily("2026-11-26") == 0
    assert store.award_daily("2026-11-27") == 1
    store.set_consent(2, False, 16)
    assert store.award_daily("2026-11-28") == 0
    assert store.profile(2) == (False, "2026-11-27")
    assert store.pending_digest() == ("2026-11-27", [2])
    store.mark_digest_sent("2026-11-27")
    assert store.pending_digest() is None
