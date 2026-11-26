import asyncio
import logging
import sqlite3
import time
from datetime import datetime
from html import escape

import httpx2 as httpx

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
            or not CONTENT_FIELDS.intersection(message)
            or message["date"] <= observation_started_at):
        return False
    return store.record_message(
        settings.chat_id, message["message_id"], sender["id"], message["date"],
        count_for_message_award=not REPLY_FIELDS.intersection(message),
    )


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


async def run_daily(settings, store, telegram, local_date, checked_at=None):
    store.award_daily(local_date, checked_at)
    await send_pending_digests(settings, store, telegram)


async def get_member_name(telegram, chat_id, user_id):
    try:
        member = await telegram("getChatMember", chat_id=chat_id, user_id=user_id)
    except TelegramError:
        return None
    user = member["user"] if member else {}
    return " ".join(part for key in ("first_name", "last_name") if (part := user.get(key))) or None


async def send_pending_digests(settings, store, telegram):
    while (digest := store.pending_digest()) is not None:
        day, groups = digest
        if groups:
            text = await digest_text(settings, telegram, day, groups)
            await telegram("sendMessage", chat_id=settings.chat_id, text=text,
                           parse_mode="HTML", link_preview_options={"is_disabled": True})
        store.mark_digest_sent(day)


async def digest_text(settings, telegram, day, groups):
    headings = []
    for group in groups:
        url = escape(settings.public_url + group.path)
        tier = f" · {escape(group.tier_name)}" if group.tier_name else ""
        headings.append(f'🏅 «<a href="{url}">{escape(group.name)}</a>»{tier} · {day}')

    size = sum(len(heading) + 2 for heading in headings)
    names = {}
    sections = []
    for group, heading in zip(groups, headings, strict=True):
        lines = [heading]
        for position, user_id in enumerate(group.user_ids):
            if user_id not in names:
                names[user_id] = await get_member_name(telegram, settings.chat_id, user_id) or "Участник чата"
            line = f"• {escape(names[user_id])}"
            if size + len(line) + 1 > 3900:
                lines.append(f"… и ещё {len(group.user_ids) - position}")
                break
            lines.append(line)
            size += len(line) + 1
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


async def schedule(settings, store, telegram):
    while True:
        now = datetime.now(settings.timezone)
        try:
            if now.time() >= settings.award_time:
                await run_daily(settings, store, telegram, now.date().isoformat(), now.timestamp())
            else:
                await send_pending_digests(settings, store, telegram)
        except (TelegramError, sqlite3.Error) as exc:
            LOGGER.warning("Сбой суточной проверки или отправки сводки: %s", exc)
        await asyncio.sleep(15)
