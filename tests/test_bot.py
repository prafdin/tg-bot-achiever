import asyncio
from types import SimpleNamespace

import pytest

from app.bot import TelegramError, handle_update, run_daily, send_pending_digests
from app.store import Store


@pytest.mark.parametrize("change, accepted", [
    ({}, True), ({"date": 10}, False), ({"reply_to_message": {}}, True),
    ({"forward_origin": {}}, True), ({"chat": {"id": 9}}, False),
    ({"sender_chat": {}}, False), ({"new_chat_members": [{"id": 4}]}, False),
    ({"quote": {"text": "quote"}}, True), ({"reply_to_story": {}}, True),
])
def test_message_filters(tmp_path, change, accepted):
    store = Store(tmp_path / "app.db")
    store.set_consent(2, True, 5)
    config = SimpleNamespace(chat_id=1, paused=False)
    message = {"chat": {"id": 1}, "date": 12, "from": {"id": 2},
               "message_id": 3, "text": "example"} | change
    if "new_chat_members" in change:
        del message["text"]
    assert handle_update({"message": message}, config, store, 10) is accepted
    assert not handle_update({"edited_message": message}, config, store, 10)
    assert not handle_update({"message_reaction": message}, config, store, 10)
    config.paused = True
    assert not handle_update({"message": message | {"message_id": 4}}, config, store, 10)


def test_daily_digest(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(chat_id=1, public_url="https://example.test")
    calls = []

    async def telegram(method, **data):
        if method == "getChatMember":
            return {"user": {"first_name": "Имя <&>"}}
        calls.append((method, data))

    asyncio.run(run_daily(config, store, telegram, "2026-11-25"))
    assert not calls
    store.set_consent(2, True, 1)
    for number in range(10):
        store.record_message(1, number, 2, 2)
    asyncio.run(run_daily(config, store, telegram, "2026-11-26", checked_at=86402))
    asyncio.run(run_daily(config, store, telegram, "2026-11-26", checked_at=86402))
    assert len(calls) == 1
    assert calls[0][0] == "sendMessage"
    payload = calls[0][1]
    assert payload["parse_mode"] == "HTML"
    assert '<a href="https://example.test/achievements/joined-the-conversation">Влился в беседу</a>' in payload["text"]
    assert '<a href="https://example.test/achievements/not-in-touch">Ща придумаю</a>' in payload["text"]
    assert "Числится живым" not in payload["text"]
    assert "Имя &lt;&amp;&gt;" in payload["text"]
    assert "reply_markup" not in payload


def test_digest_survives_failed_delivery(tmp_path):
    path = tmp_path / "app.db"
    store = Store(path)
    config = SimpleNamespace(chat_id=1, public_url="https://example.test")
    store.set_consent(2, True, 1)
    for number in range(10):
        store.record_message(1, number, 2, 2)

    async def unavailable(method, **data):
        raise TelegramError("temporary failure")

    with pytest.raises(TelegramError):
        asyncio.run(run_daily(config, store, unavailable, "2026-11-25", checked_at=2))

    store = Store(path)
    assert store.profile(2)[1] == "2026-11-25"
    day, groups = store.pending_digest()
    assert day == "2026-11-25"
    assert [(group.name, group.user_ids) for group in groups] == [("Влился в беседу", [2])]

    async def available(method, **data):
        if method == "getChatMember":
            return {"user": {"first_name": "Участник"}}

    asyncio.run(send_pending_digests(config, store, available))
    assert store.pending_digest() is None
    assert store.profile(2)[1] == "2026-11-25"


def test_reply_resets_silence_without_counting_towards_the_message_award(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(chat_id=1, paused=False)
    store.set_consent(2, True, 1)
    for number in range(1, 10):
        store.record_message(1, number, 2, 2)
    reply = {
        "chat": {"id": 1}, "date": 100, "from": {"id": 2}, "message_id": 10,
        "text": "example", "reply_to_message": {"from": {"id": 3}},
    }
    assert handle_update({"message": reply}, config, store, 10)
    assert store.award_daily("2026-11-25", checked_at=86402) == 0
    assert store.award_daily("2026-11-26", checked_at=86500) == 1
    assert store.profile(2) == (True, None)
    assert store.silence_history(2) == [(1, "2026-11-26")]
