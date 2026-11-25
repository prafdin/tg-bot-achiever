import asyncio
from types import SimpleNamespace

import pytest

from app.bot import TelegramError, handle_update, run_daily, send_pending_digests
from app.store import Store


@pytest.mark.parametrize("change, accepted", [
    ({}, True), ({"date": 10}, False), ({"reply_to_message": {}}, False),
    ({"forward_origin": {}}, True), ({"chat": {"id": 9}}, False),
    ({"sender_chat": {}}, False), ({"new_chat_members": [{"id": 4}]}, False),
    ({"quote": {"text": "quote"}}, False), ({"reply_to_story": {}}, False),
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
            return {"user": {"first_name": "Участник"}}
        calls.append((method, data))

    asyncio.run(run_daily(config, store, telegram, "2026-11-25"))
    assert not calls
    store.set_consent(2, True, 1)
    for number in range(10):
        store.record_message(1, number, 2, 2)
    asyncio.run(run_daily(config, store, telegram, "2026-11-26"))
    asyncio.run(run_daily(config, store, telegram, "2026-11-26"))
    assert len(calls) == 1
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["reply_markup"]["inline_keyboard"] == [[{
        "text": "Влился в беседу",
        "url": "https://example.test/achievements/joined-the-conversation",
    }]]


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
        asyncio.run(run_daily(config, store, unavailable, "2026-11-25"))

    store = Store(path)
    assert store.profile(2)[1] == "2026-11-25"
    assert store.pending_digest() == ("2026-11-25", [2])

    async def available(method, **data):
        if method == "getChatMember":
            return {"user": {"first_name": "Участник"}}

    asyncio.run(send_pending_digests(config, store, available))
    assert store.pending_digest() is None
    assert store.profile(2)[1] == "2026-11-25"
