import secrets
import time
from pathlib import Path

import httpx2 as httpx
from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from joserfc.errors import JoseError
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from app.store import AWARD_NAME


def create_app(settings, store, lifespan=None):
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    oauth = OAuth()
    telegram = oauth.register(
        "telegram", client_id=settings.client_id, client_secret=settings.client_secret,
        server_metadata_url="https://oauth.telegram.org/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile", "code_challenge_method": "S256"},
    )

    def user_id(request):
        value = request.session.get("user_id")
        return value if value in settings.allowed_ids else None

    async def profile(request):
        identity = user_id(request)
        context = {"identity": identity, "paused": getattr(settings, "paused", False)}
        if identity is not None:
            enabled, day = store.profile(identity)
            context.update(enabled=enabled, day=day, award_name=AWARD_NAME,
                           csrf=request.session.setdefault("csrf", secrets.token_urlsafe(32)))
        return templates.TemplateResponse(request, "profile.html", context,
                                          headers={"Cache-Control": "no-store"})

    async def login(request):
        request.session.clear()
        try:
            return await request.app.state.telegram.authorize_redirect(
                request, settings.public_url + "/auth/callback")
        except httpx.HTTPError:
            return PlainTextResponse("Вход временно недоступен.", status_code=502)

    async def callback(request):
        try:
            token = await request.app.state.telegram.authorize_access_token(request)
            claims = token.get("userinfo") or {}
            identity = claims.get("id")
        except (OAuthError, JoseError, ValueError):
            identity = None
        except httpx.HTTPError:
            return PlainTextResponse("Вход временно недоступен.", status_code=502)
        request.session.clear()
        if type(identity) is not int or identity not in settings.allowed_ids:
            return PlainTextResponse("Доступ к профилю закрыт.", status_code=403)
        request.session["user_id"] = identity
        return RedirectResponse("/", status_code=303)

    async def consent(request):
        identity = user_id(request)
        if identity is None:
            return PlainTextResponse("Требуется вход.", status_code=403)
        form = await request.form(max_fields=2, max_files=0)
        csrf = form.get("csrf")
        expected = request.session.get("csrf")
        if not isinstance(csrf, str) or not csrf.isascii() or not expected or not secrets.compare_digest(csrf, expected):
            return PlainTextResponse("Обновите страницу и повторите действие.", status_code=403)
        enabled = form.get("enabled")
        if enabled not in ("0", "1"):
            return PlainTextResponse("Неизвестная настройка сбора.", status_code=400)
        store.set_consent(identity, enabled == "1", time.time())
        return RedirectResponse("/", status_code=303)

    app = Starlette(lifespan=lifespan, routes=[
        Route("/", profile), Route("/login", login), Route("/auth/callback", callback),
        Route("/consent", consent, methods=["POST"]),
    ])
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       https_only=True, same_site="lax")
    app.state.telegram = telegram
    return app
