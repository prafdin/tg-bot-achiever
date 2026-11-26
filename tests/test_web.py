import re
import time
from types import SimpleNamespace

import pytest
from starlette.responses import RedirectResponse
from starlette.testclient import TestClient

from app.store import Store
from app.web import create_app


def test_callback_without_login_session(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret")
    with TestClient(create_app(config, store), base_url=config.public_url) as client:
        response = client.get("/auth/callback?code=unused&state=unknown")
        assert response.status_code == 400
        assert "том же браузере" in response.text


@pytest.mark.parametrize("telegram_id", [2, "2"])
def test_profile_and_consent(tmp_path, telegram_id):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret")
    app = create_app(config, store)

    class Telegram:
        user = {}

        async def authorize_access_token(self, request):
            return {"userinfo": self.user}

    telegram = Telegram()
    app.state.telegram = telegram
    with TestClient(app, base_url=config.public_url) as client:
        assert "Войти через Telegram" in client.get("/").text
        assert client.post("/consent").status_code == 403
        assert client.get("/auth/callback").status_code == 403
        telegram.user = {"id": 9}
        assert client.get("/auth/callback").status_code == 403
        telegram.user = {"id": "9"}
        assert client.get("/auth/callback").status_code == 403
        telegram.user = {"id": telegram_id}
        page = client.get("/auth/callback")
        assert "История" in page.text
        assert 'href="/data-policy"' in page.text
        assert client.post("/consent", data={"enabled": "1"}).status_code == 403
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        assert 'class="status status-off"' in page.text
        enabled = client.post("/consent", data={"enabled": "1", "csrf": csrf})
        assert enabled.status_code == 200
        assert 'class="status status-on"' in enabled.text
        assert store.profile(2)[0]
        assert not store.profile(9)[0]
        for message_id in range(10):
            assert store.record_message(1, message_id, 2, int(time.time()) + 1)
        assert store.award_daily("2026-11-25", checked_at=time.time()) == 1
        page = client.get("/")
        assert "Влился в беседу" in page.text
        assert page.text.count('href="/achievements/joined-the-conversation"') == 2
        assert '<time datetime="2026-11-25">' in page.text


def test_achievement_deep_link_after_first_award(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2, 3}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret")
    app = create_app(config, store)

    class Telegram:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse("/auth/callback")

        async def authorize_access_token(self, request):
            return {"userinfo": {"id": "3"}}

    app.state.telegram = Telegram()
    path = "/achievements/joined-the-conversation"
    with TestClient(app, base_url=config.public_url) as client:
        assert client.get("/auth/callback").status_code == 200
        assert client.get(path).status_code == 404
        store.set_consent(2, True, 1)
        for message_id in range(10):
            assert store.record_message(1, message_id, 2, 2)
        assert store.award_daily("2026-11-25", checked_at=2) == 1
        client.cookies.clear()
        assert client.get(path, follow_redirects=False).status_code == 303
        page = client.get(path)
        assert page.status_code == 200
        assert page.url.path == path
        assert "За 10 учтённых сообщений" in page.text
        assert 'src="https://storage.yandexcloud.net/achievements.prafdin.space/joined-the-conversation-cdn.webp"' in page.text
        assert 'referrerpolicy="no-referrer"' in page.text
        assert client.get("/login?achievement=//example.org").url.path == "/"


def test_participant_directory_and_other_profiles(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2, 3, 4, 5}, public_url="https://example.test",
                             participant_names={2: "Аня", 3: "<Борис>", 4: "Вера", 9: "Нет доступа"},
                             session_secret="test-secret", client_id="id", client_secret="secret")
    app = create_app(config, store)

    class Telegram:
        async def authorize_access_token(self, request):
            return {"userinfo": {"id": 2}}

    async def resolve_name(identity):
        return {3: "Имя из Telegram", 5: "Другое имя из Telegram"}.get(identity)

    app.state.telegram = Telegram()
    app.state.resolve_member_name = resolve_name
    store.set_consent(3, True, 1)
    for message_id in range(10):
        assert store.record_message(1, message_id, 3, 2)
    assert store.award_daily("2026-11-25", checked_at=2) == 1

    with TestClient(app, base_url=config.public_url) as client:
        assert client.get("/participants", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/participants/3", follow_redirects=False).headers["location"] == "/login"
        assert 'href="/participants"' in client.get("/auth/callback").text
        directory = client.get("/participants")
        assert directory.status_code == 200
        assert 'href="/participants/3"' in directory.text and "&lt;Борис&gt;" in directory.text
        assert 'href="/participants/4"' in directory.text and "Вера" in directory.text
        assert "Участник 5" in directory.text
        assert "Имя из Telegram" not in directory.text and "Нет доступа" not in directory.text
        profile = client.get("/participants/3")
        assert profile.status_code == 200
        assert "Влился в беседу" in profile.text and "История" in profile.text
        assert 'class="status status-on">Сбор включён' in profile.text
        assert 'action="/consent"' not in profile.text
        other = client.get("/participants/4")
        assert "<h1>Вера</h1>" in other.text
        assert "Пока нет ачивок" in other.text
        assert 'class="status status-off">Сбор выключен' in other.text
        assert client.get("/participants/9").status_code == 404


def test_data_collection_policy_is_readable_without_login(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret")
    with TestClient(create_app(config, store), base_url=config.public_url) as client:
        policy = client.get("/data-policy", follow_redirects=False)
        assert policy.status_code == 200
        assert "Политика сбора данных" in policy.text
        assert "идентификаторы чата, сообщения и отправителя" in policy.text
        assert "ранее учтённые сообщения и выданные ачивки сохраняются" in policy.text


def test_deployed_version_and_build_are_visible(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret",
                             app_version="0.2.0", build_run_id="45804489232")
    with TestClient(create_app(config, store), base_url=config.public_url) as client:
        for path in ("/", "/data-policy"):
            page = client.get(path)
            assert "Версия 0.2.0" in page.text
            assert "Сборка 45804489232" in page.text


def test_site_favicon(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret")
    with TestClient(create_app(config, store), base_url=config.public_url) as client:
        for route in ("/", "/data-policy"):
            assert 'rel="icon" type="image/png" sizes="64x64" href="/favicon.png"' in client.get(route).text
        icon = client.get("/favicon.png")
        assert icon.status_code == 200
        assert icon.headers["content-type"] == "image/png"
        assert icon.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_first_silence_achievement_replaces_the_previous_drafts(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret")
    app = create_app(config, store)

    class Telegram:
        async def authorize_redirect(self, request, redirect_uri):
            return RedirectResponse("/auth/callback")

        async def authorize_access_token(self, request):
            return {"userinfo": {"id": 2}}

    app.state.telegram = Telegram()
    first = "/achievements/not-in-touch"
    second = "/achievements/wanted"
    with TestClient(app, base_url=config.public_url) as client:
        assert client.get("/auth/callback").status_code == 200
        assert client.get(first).status_code == 404
        store.set_consent(2, True, 1)
        assert store.record_message(1, 1, 2, 2)
        assert store.award_daily("2026-11-25", checked_at=86402) == 1
        with store.transaction() as db:
            db.execute("INSERT INTO silence_awards VALUES (2, 2, '2026-12-01')")
        client.cookies.clear()
        page = client.get(first)
        assert page.status_code == 200 and page.url.path == first
        assert "<h1>Ща придумаю</h1>" in page.text
        assert "Сутки думал, что написать в чат. Не придумал." in page.text
        assert "За 1 сутки тишины" in page.text
        assert "Ступени серии" not in page.text
        assert "Числится живым" not in page.text
        assert client.get("/achievements/listed-as-alive").url.path == first
        for removed in (second, "/achievements/search-suspended", "/achievements/case-archived"):
            assert client.get(removed).status_code == 404
        profile = client.get("/").text
        assert profile.count(f'href="{first}">Ща придумаю</a>') == 2
        assert "Объявлен в розыск" not in profile
        assert "Числится живым" not in profile
