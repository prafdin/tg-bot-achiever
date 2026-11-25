import asyncio
import logging
import sqlite3
import time
from datetime import datetime

import httpx2 as httpx

from app.store import AWARD_NAME, AWARD_PATH

LOGGER = logging.getLogger(__name__)
CONTENT_FIELDS = {
    "text", "animation", "audio", "document", "paid_media", "photo", "sticker",
    "story", "video", "video_note", "voice", "contact", "dice", "game", "poll",
    "venue", "location", "checklist", "live_photo",
}
REPLY_FIELDS = {
    "reply_to_message", "external_reply", "quote", "reply_to_story",
    "reply_to_checklist_task_id", "reply_to_poll_option_id",
}


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, client, token):
        self.client = client
        self.url = f"https://api.telegram.org/bot{token}"

    async def __call__(self, method, **data):
        try:
            response = await self.client.post(f"{self.url}/{method}", json=data)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTP errors can contain the request URL and bot token.
            raise TelegramError(f"{method}: ошибка соединения или ответа") from None
        if not response.is_success or not payload.get("ok"):
            raise TelegramError(f"{method}: Telegram вернул код {payload.get('error_code', response.status_code)}")
        return payload["result"]


def handle_update(update, settings, store, observation_started_at):
    message = update.get("message")
    if settings.paused or not message or message["chat"]["id"] != settings.chat_id:
        return False
    sender = message.get("from")
    if (not sender or sender.get("is_bot") or "sender_chat" in message
            or REPLY_FIELDS.intersection(message)
            or not CONTENT_FIELDS.intersection(message)
            or message["date"] <= observation_started_at):
        return False
    return store.record_message(settings.chat_id, message["message_id"], sender["id"], message["date"])


async def poll(settings, store, telegram):
    offset = 0
    observation_started_at = None
    while True:
        try:
            timeout = 0 if observation_started_at is None else 30
            updates = await telegram("getUpdates", offset=offset,
                                     timeout=timeout, allowed_updates=["message"])
            if observation_started_at is None:
                # Open the observation window after the first successful API response.
                observation_started_at = time.time()
            for update in updates:
                handle_update(update, settings, store, observation_started_at)
                offset = update["update_id"] + 1
            if not updates:
                offset = 0
        except (TelegramError, sqlite3.Error) as exc:
            LOGGER.warning("Сбой наблюдения: %s", exc)
            observation_started_at = None
            offset = 0
            await asyncio.sleep(5)


async def run_daily(settings, store, telegram, local_date):
    store.award_daily(local_date)
    await send_pending_digests(settings, store, telegram)


async def send_pending_digests(settings, store, telegram):
    while (digest := store.pending_digest()) is not None:
        day, users = digest
        labels = []
        for user_id in users:
            try:
                member = await telegram("getChatMember", chat_id=settings.chat_id, user_id=user_id)
                user = member["user"] if member else {}
            except TelegramError:
                user = {}
            name = " ".join(part for key in ("first_name", "last_name") if (part := user.get(key)))
            labels.append(name or "Участник чата")

        heading = f"🏅 «{AWARD_NAME}» · {day}\n"
        lines = []
        size = len(heading)
        for label in labels:
            line = f"• {label}\n"
            if size + len(line) > 3900:
                lines.append(f"… и ещё {len(labels) - len(lines)}")
                break
            lines.append(line)
            size += len(line)
        await telegram("sendMessage", chat_id=settings.chat_id,
                       text=heading + "".join(lines).rstrip(),
                       reply_markup={"inline_keyboard": [[{
                           "text": AWARD_NAME,
                           "url": settings.public_url + AWARD_PATH,
                       }]]})
        store.mark_digest_sent(day)


async def schedule(settings, store, telegram):
    while True:
        now = datetime.now(settings.timezone)
        try:
            if now.time() >= settings.award_time:
                await run_daily(settings, store, telegram, now.date().isoformat())
            else:
                await send_pending_digests(settings, store, telegram)
        except (TelegramError, sqlite3.Error) as exc:
            LOGGER.warning("Сбой суточной проверки или отправки сводки: %s", exc)
        await asyncio.sleep(15)
