import re
import time
from types import SimpleNamespace

import pytest
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
        assert client.post("/consent", data={"enabled": "1", "csrf": csrf}).status_code == 200
        assert store.profile(2)[0]
        assert not store.profile(9)[0]
        for message_id in range(10):
            assert store.record_message(1, message_id, 2, int(time.time()) + 1)
        assert store.award_daily("2026-11-25") == 1
        page = client.get("/")
        assert "Влился в беседу" in page.text
        assert '<time datetime="2026-11-25">' in page.text
