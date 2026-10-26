import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from auth import COOKIE_NAME, COOKIE_PATH, Auth
from config import Settings
from errors import APIError
from storage import Storage

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 32_768
MAX_MESSAGE_LENGTH = 2_000


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message["headers"]) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; "
                        b"connect-src 'self'; img-src 'self'; object-src 'none'; "
                        b"base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                    ),
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise APIError(415, "Ожидается application/json.", code="unsupported_media_type")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise APIError(413, "Слишком большой запрос.", code="payload_too_large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise APIError(400, "Некорректный JSON.", code="invalid_json") from None
    if not isinstance(payload, dict):
        raise APIError(400, "Ожидается JSON-объект.", code="json_object_required")
    return payload


def text_field(payload: dict, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise APIError(400, f"Поле {name} должно быть строкой.", code="invalid_field")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise APIError(
            400, f"Поле {name} содержит некорректный Unicode.", code="invalid_unicode"
        ) from None
    return value


async def index(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html")


async def login(request: Request) -> Response:
    auth = request.app.state.auth
    auth.require_origin(request)
    auth.limit_login()
    payload = await json_body(request)
    token, csrf = auth.login(text_field(payload, "password"))
    settings = request.app.state.settings
    response = JSONResponse({"csrf_token": csrf})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_seconds,
        path=COOKIE_PATH,
        secure=settings.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return response


async def logout(request: Request) -> Response:
    state = request.app.state
    state.auth.admin(request, state.now(), mutate=True)
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        secure=state.settings.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return response


async def admin_state(request: Request) -> Response:
    state = request.app.state
    now = state.now()
    claims = state.auth.admin(request, now)
    data = await run_in_threadpool(
        state.storage.state, now, state.settings.ping_interval_seconds
    )
    return JSONResponse({**data, "csrf_token": claims["csrf"]})


async def create_message(request: Request) -> Response:
    state = request.app.state
    state.auth.admin(request, state.now(), mutate=True)
    payload = await json_body(request)
    text = text_field(payload, "text").strip()
    if not text or len(text) > MAX_MESSAGE_LENGTH:
        raise APIError(
            400, f"Сообщение должно содержать от 1 до {MAX_MESSAGE_LENGTH} символов.",
            code="invalid_message",
        )
    message = await run_in_threadpool(state.storage.create_message, text, state.now())
    return JSONResponse(message, status_code=201)


async def ping(request: Request) -> Response:
    state = request.app.state
    state.auth.device(request)
    unread = await run_in_threadpool(state.storage.ping, state.now())
    return JSONResponse({"unread": unread, "ping": "pong"})


async def message(request: Request) -> Response:
    state = request.app.state
    state.auth.device(request)
    latest = await run_in_threadpool(state.storage.latest_message)
    return JSONResponse(latest) if latest else Response(status_code=204)


async def acknowledge(request: Request) -> Response:
    state = request.app.state
    state.auth.device(request)
    payload = await json_body(request)
    message_id = payload.get("id")
    if type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
        raise HTTPException(400, "Поле id должно быть положительным 64-битным целым числом.")
    found = await run_in_threadpool(state.storage.acknowledge, message_id, state.now())
    if not found:
        raise HTTPException(404, "Сообщение не найдено.")
    return Response(status_code=204)


async def http_error(request: Request, exc: HTTPException) -> Response:
    body = {"error": exc.detail}
    if request.url.path == "/api/admin" or request.url.path.startswith("/api/admin/"):
        body["code"] = (
            exc.code if isinstance(exc, APIError)
            else {404: "not_found", 405: "method_not_allowed"}.get(
                exc.status_code, "request_failed"
            )
        )
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    storage = Storage(settings.database_path)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(storage.initialize)
        yield

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/admin/login", login, methods=["POST"]),
            Route("/api/admin/logout", logout, methods=["POST"]),
            Route("/api/admin/state", admin_state),
            Route("/api/admin/messages", create_message, methods=["POST"]),
            Route("/ping", ping, methods=["POST"]),
            Route("/message", message),
            Route("/message/ack", acknowledge, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC_DIR)),
        ],
        middleware=[
            Middleware(SecurityHeaders),
            Middleware(
                TrustedHostMiddleware,
                allowed_hosts=[urlsplit(settings.public_origin).hostname],
            ),
        ],
        exception_handlers={HTTPException: http_error},
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.storage = storage
    app.state.auth = Auth(settings)
    app.state.now = lambda: datetime.now(UTC)
    return app
