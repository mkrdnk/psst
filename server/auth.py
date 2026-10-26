import hmac
import math
import secrets
import time
from collections import deque
from datetime import datetime

from jam import Jam
from jam.exceptions import JamError
from starlette.exceptions import HTTPException
from starlette.requests import Request

from config import Settings
from errors import APIError

COOKIE_NAME = "psst_admin"
COOKIE_PATH = "/api/admin"


def same_secret(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jam = Jam(config={
            "paseto": {
                "version": "v4",
                "purpose": "local",
                "secret_key": settings.paseto_secret_key,
            }
        })
        self.password_version = hmac.digest(
            settings.paseto_secret_key,
            settings.admin_password.encode("utf-8"),
            "sha256",
        ).hex()
        self.login_attempts: deque[float] = deque()

    def require_origin(self, request: Request) -> None:
        if request.headers.get("origin") != self.settings.public_origin:
            raise APIError(403, "Недопустимый источник запроса.", code="invalid_origin")

    def limit_login(self) -> None:
        now = time.monotonic()
        while self.login_attempts and self.login_attempts[0] <= now - 60:
            self.login_attempts.popleft()
        if len(self.login_attempts) >= 5:
            retry_after = max(1, math.ceil(60 - (now - self.login_attempts[0])))
            raise APIError(
                429,
                "Слишком много попыток входа. Попробуйте через минуту.",
                code="rate_limited",
                headers={"Retry-After": str(retry_after)},
            )
        self.login_attempts.append(now)

    def login(self, password: str) -> tuple[str, str]:
        if not same_secret(password, self.settings.admin_password):
            raise APIError(401, "Неверный пароль.", code="invalid_password")
        csrf = secrets.token_urlsafe(32)
        token = self.jam.issue(
            {"id": "admin"},
            via="paseto",
            exp=self.settings.session_ttl_seconds,
            iss="psst",
            aud="psst-admin",
            csrf=csrf,
            password_version=self.password_version,
        )
        return token, csrf

    def admin(self, request: Request, now: datetime, *, mutate: bool = False) -> dict:
        token = request.cookies.get(COOKIE_NAME, "")
        if not token.startswith("v4.local."):
            raise APIError(401, "Необходим вход.", code="auth_required")
        try:
            claims = self.jam.authenticate(token, via="paseto").claims
        except (JamError, ValueError, TypeError):
            raise APIError(401, "Недействительная сессия.", code="invalid_session") from None
        # Jam 4.2 verifies PASETO cryptography, but does not validate its expiry.
        if (
            claims.get("sub") != "admin"
            or claims.get("iss") != "psst"
            or claims.get("aud") != "psst-admin"
            or type(claims.get("exp")) is not int
            or claims["exp"] <= now.timestamp()
            or not isinstance(claims.get("csrf"), str)
            or not claims["csrf"]
            or claims.get("password_version") != self.password_version
        ):
            raise APIError(401, "Сессия истекла или недействительна.", code="invalid_session")
        if mutate:
            self.require_origin(request)
            if not same_secret(request.headers.get("x-csrf-token", ""), claims["csrf"]):
                raise APIError(403, "Недействительный CSRF-токен.", code="invalid_csrf")
        return claims

    def device(self, request: Request) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not same_secret(token, self.settings.device_token):
            raise HTTPException(
                401, "Недействительный токен устройства.",
                headers={"WWW-Authenticate": "Bearer"},
            )
