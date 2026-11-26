import logging
import secrets
import time
from pathlib import Path

import httpx2 as httpx
from authlib.integrations.base_client import OAuthError
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.starlette_client import OAuth
from joserfc.errors import JoseError
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from app.store import (
    AWARD_MESSAGE_COUNT, AWARD_NAME, AWARD_PATH, AWARD_SLUG,
    SILENCE_BY_SLUG, SILENCE_TIERS,
)


LOGGER = logging.getLogger(__name__)
OAUTH_ERRORS = {"access_denied", "invalid_client", "invalid_grant", "invalid_request", "unauthorized_client"}
LEGACY_SILENCE_SLUG = "listed-as-alive"
ACHIEVEMENT_PATHS = {
    AWARD_SLUG: AWARD_PATH,
    LEGACY_SILENCE_SLUG: SILENCE_TIERS[0].path,
    **{tier.slug: tier.path for tier in SILENCE_TIERS},
}


def create_app(settings, store, lifespan=None):
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    templates.env.globals.update(
        app_version=getattr(settings, "app_version", None),
        build_run_id=getattr(settings, "build_run_id", ""),
    )
    allowed_ids_by_text = {str(identity): identity for identity in settings.allowed_ids}
    participant_names = getattr(settings, "participant_names", {})
    oauth = OAuth()
    telegram = oauth.register(
        "telegram", client_id=settings.client_id, client_secret=settings.client_secret,
        server_metadata_url="https://oauth.telegram.org/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile", "code_challenge_method": "S256"},
    )

    def user_id(request):
        value = request.session.get("user_id")
        return value if value in settings.allowed_ids else None

    def participant_name(identity):
        return participant_names.get(identity, f"Участник {identity}")

    def render_profile(request, identity, viewer, name=None):
        own_profile = identity is not None and identity == viewer
        context = {"identity": identity, "own_profile": own_profile, "participant_name": name}
        if identity is not None:
            enabled, day = store.profile(identity)
            current = []
            history = []
            if day:
                award = {"name": AWARD_NAME, "path": AWARD_PATH, "day": day}
                current.append(award)
                history.append(award)
            silence_history = [
                {"name": SILENCE_TIERS[tier - 1].name, "path": SILENCE_TIERS[tier - 1].path, "day": awarded_day}
                for tier, awarded_day in store.silence_history(identity)
            ]
            if silence_history:
                current.append(silence_history[-1])
                history.extend(silence_history)
            context.update(current=current, history=sorted(history, key=lambda item: item["day"], reverse=True),
                           enabled=enabled, paused=getattr(settings, "paused", False))
        if own_profile:
            context["csrf"] = request.session.setdefault("csrf", secrets.token_urlsafe(32))
        return templates.TemplateResponse(request, "profile.html", context,
                                          headers={"Cache-Control": "no-store"})

    async def profile(request):
        identity = user_id(request)
        return render_profile(request, identity, identity)

    async def data_policy(request):
        return templates.TemplateResponse(request, "data_policy.html")

    async def favicon(request):
        return FileResponse(Path(__file__).with_name("static") / "favicon.png", media_type="image/png")

    async def participants(request):
        viewer = user_id(request)
        headers = {"Cache-Control": "no-store"}
        if viewer is None:
            return RedirectResponse("/login", status_code=303, headers=headers)
        members = sorted(
            ((identity, participant_name(identity)) for identity in settings.allowed_ids),
            key=lambda member: (member[1].casefold(), member[0]),
        )
        return templates.TemplateResponse(request, "participants.html", {
            "members": members, "viewer": viewer,
        }, headers=headers)

    async def participant_profile(request):
        viewer = user_id(request)
        headers = {"Cache-Control": "no-store"}
        if viewer is None:
            return RedirectResponse("/login", status_code=303, headers=headers)
        identity = request.path_params["identity"]
        if identity not in settings.allowed_ids:
            return PlainTextResponse("Участник не найден.", status_code=404, headers=headers)
        if identity == viewer:
            return RedirectResponse("/", status_code=303, headers=headers)
        return render_profile(request, identity, viewer, participant_name(identity))

    async def achievement(request):
        headers = {"Cache-Control": "no-store"}
        slug = request.path_params["slug"]
        if slug not in ACHIEVEMENT_PATHS:
            return PlainTextResponse("Ачивка не найдена.", status_code=404, headers=headers)
        if slug == LEGACY_SILENCE_SLUG:
            return RedirectResponse(ACHIEVEMENT_PATHS[slug], status_code=303, headers=headers)
        if user_id(request) is None:
            return RedirectResponse(f"/login?achievement={slug}", status_code=303, headers=headers)
        if not store.achievement_discovered(slug):
            return PlainTextResponse("Ачивка не найдена.", status_code=404, headers=headers)
        if slug in SILENCE_BY_SLUG:
            _, tier = SILENCE_BY_SLUG[slug]
            unit = "сутки" if tier.days == 1 else "суток"
            detail = {"award_name": tier.name, "description": tier.description,
                      "criterion": f"За {tier.days} {unit} тишины после последнего замеченного сообщения"}
        else:
            detail = {"award_name": AWARD_NAME, "description": "Зашёл на минутку, а теперь без тебя уже не договорят.",
                      "criterion": f"За {AWARD_MESSAGE_COUNT} учтённых сообщений"}
        return templates.TemplateResponse(request, "achievement.html", detail, headers=headers)

    async def login(request):
        request.session.clear()
        slug = request.query_params.get("achievement")
        if slug in ACHIEVEMENT_PATHS:
            request.session["achievement"] = slug
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
        except MismatchingStateError:
            LOGGER.warning("Telegram login: OAuth state did not match; session cookie present: %s",
                           "session" in request.cookies)
            return PlainTextResponse(
                "Сессия входа не найдена. Откройте сайт и начните вход заново в том же браузере.",
                status_code=400,
            )
        except OAuthError as exc:
            error = exc.error if exc.error in OAUTH_ERRORS else "other"
            LOGGER.warning("Telegram login: OAuth error %s", error)
            return PlainTextResponse("Telegram не завершил вход. Начните вход заново.", status_code=400)
        except (JoseError, ValueError) as exc:
            LOGGER.warning("Telegram login: token validation error %s", type(exc).__name__)
            return PlainTextResponse("Не удалось проверить ответ Telegram.", status_code=400)
        except httpx.HTTPError:
            return PlainTextResponse("Вход временно недоступен.", status_code=502)
        destination = ACHIEVEMENT_PATHS.get(request.session.get("achievement"), "/")
        request.session.clear()
        if type(identity) is str:
            identity = allowed_ids_by_text.get(identity)
        if type(identity) is not int or identity not in settings.allowed_ids:
            LOGGER.warning("Telegram login: missing or disallowed Telegram ID")
            return PlainTextResponse("Доступ к профилю закрыт.", status_code=403)
        request.session["user_id"] = identity
        return RedirectResponse(destination, status_code=303)

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
        Route("/", profile), Route("/achievements/{slug}", achievement), Route("/login", login),
        Route("/data-policy", data_policy), Route("/favicon.png", favicon),
        Route("/participants", participants), Route("/participants/{identity:int}", participant_profile),
        Route("/auth/callback", callback),
        Route("/consent", consent, methods=["POST"]),
    ])
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       https_only=True, same_site="lax")
    app.state.telegram = telegram
    return app
