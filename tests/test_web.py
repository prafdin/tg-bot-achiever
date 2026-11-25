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
        assert store.award_daily("2026-11-25") == 1
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
        assert store.award_daily("2026-11-25") == 1
        client.cookies.clear()
        assert client.get(path, follow_redirects=False).status_code == 303
        page = client.get(path)
        assert page.status_code == 200
        assert page.url.path == path
        assert "За 10 учтённых сообщений" in page.text
        assert client.get("/login?achievement=//example.org").url.path == "/"


def test_participant_directory_and_other_profiles(tmp_path):
    store = Store(tmp_path / "app.db")
    config = SimpleNamespace(allowed_ids={2, 3, 4}, public_url="https://example.test",
                             session_secret="test-secret", client_id="id", client_secret="secret")
    app = create_app(config, store)

    class Telegram:
        async def authorize_access_token(self, request):
            return {"userinfo": {"id": 2}}

    async def resolve_name(identity):
        return {2: "Аня", 3: "<Борис>"}.get(identity)

    app.state.telegram = Telegram()
    app.state.resolve_member_name = resolve_name
    store.set_consent(3, True, 1)
    for message_id in range(10):
        assert store.record_message(1, message_id, 3, 2)
    assert store.award_daily("2026-11-25") == 1

    with TestClient(app, base_url=config.public_url) as client:
        assert client.get("/participants", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/participants/3", follow_redirects=False).headers["location"] == "/login"
        assert 'href="/participants"' in client.get("/auth/callback").text
        directory = client.get("/participants")
        assert directory.status_code == 200
        assert 'href="/participants/3"' in directory.text and "&lt;Борис&gt;" in directory.text
        assert 'href="/participants/4"' in directory.text and "Участник 4" in directory.text
        profile = client.get("/participants/3")
        assert profile.status_code == 200
        assert "Влился в беседу" in profile.text and "История" in profile.text
        assert "Сбор данных" not in profile.text and 'action="/consent"' not in profile.text
        assert "Пока нет ачивок" in client.get("/participants/4").text
        assert client.get("/participants/9").status_code == 404
