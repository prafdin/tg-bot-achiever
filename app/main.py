import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx2 as httpx

from app.bot import Telegram, get_member_name, poll, schedule
from app.store import Store
from app.web import create_app


@dataclass(frozen=True)
class Settings:
    bot_token: str
    client_id: str
    client_secret: str
    session_secret: str
    public_url: str
    chat_id: int
    allowed_ids: frozenset[int]
    participant_names: dict[int, str]
    paused: bool
    database: Path
    timezone: ZoneInfo
    award_time: time

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Не задана переменная {name}")
            return value

        public_url = required("PUBLIC_URL").rstrip("/")
        url = urlsplit(public_url)
        if url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment or url.username:
            raise ValueError("PUBLIC_URL должен содержать HTTPS-адрес сайта без пути")
        paused = os.environ.get("COLLECTION_PAUSED", "0")
        if paused not in {"0", "1"}:
            raise ValueError("COLLECTION_PAUSED принимает 0 или 1")
        award_time = time.fromisoformat(os.environ.get("AWARD_TIME", "20:00"))
        if award_time.tzinfo is not None:
            raise ValueError("AWARD_TIME задаётся в локальном времени")
        names = json.loads(os.environ.get("PARTICIPANT_NAMES", "{}"))
        if not isinstance(names, dict) or any(
            not (key.isascii() and key.isdecimal() and isinstance(name, str) and name.strip())
            for key, name in names.items()
        ):
            raise ValueError("PARTICIPANT_NAMES должен быть JSON-объектом с числовыми ID и непустыми именами")
        return cls(
            bot_token=required("BOT_TOKEN"),
            client_id=required("TELEGRAM_CLIENT_ID"),
            client_secret=required("TELEGRAM_CLIENT_SECRET"),
            session_secret=required("SESSION_SECRET"),
            public_url=public_url,
            chat_id=int(required("TELEGRAM_CHAT_ID")),
            allowed_ids=frozenset(map(int, required("ALLOWED_TELEGRAM_IDS").split(","))),
            participant_names={int(key): name.strip() for key, name in names.items()},
            paused=paused == "1",
            database=Path(os.environ.get("DATABASE_PATH", "achiever.db")),
            timezone=ZoneInfo(os.environ.get("AWARD_TIMEZONE", "Europe/Samara")),
            award_time=award_time,
        )


def build_app(settings):
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    store = Store(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=40) as client:
            telegram = Telegram(client, settings.bot_token)

            async def resolve_member_name(identity):
                try:
                    return await asyncio.wait_for(get_member_name(telegram, settings.chat_id, identity), timeout=3)
                except asyncio.TimeoutError:
                    return None

            app.state.resolve_member_name = resolve_member_name
            tasks = [asyncio.create_task(poll(settings, store, telegram)),
                     asyncio.create_task(schedule(settings, store, telegram))]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    return create_app(settings, store, lifespan)


app = build_app(Settings.from_env())
