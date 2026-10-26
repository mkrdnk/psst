import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    admin_password: str = field(repr=False)
    device_token: str = field(repr=False)
    paseto_secret_key: bytes = field(repr=False)
    database_path: Path
    public_origin: str = "http://localhost:8000"
    ping_interval_seconds: int = 30
    session_ttl_seconds: int = 43_200

    def __post_init__(self) -> None:
        if not self.admin_password or not self.device_token:
            raise ValueError("ADMIN_PASSWORD and DEVICE_TOKEN must not be empty")
        if self.admin_password == self.device_token:
            raise ValueError("ADMIN_PASSWORD and DEVICE_TOKEN must be different")
        if len(self.paseto_secret_key) != 32:
            raise ValueError("PASETO_SECRET_KEY must encode exactly 32 bytes")
        if self.ping_interval_seconds <= 0 or self.session_ttl_seconds <= 0:
            raise ValueError("Ping interval and session lifetime must be positive")
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("PUBLIC_ORIGIN must be an HTTP(S) origin without a path")
        if ":" in origin.hostname:
            raise ValueError("PUBLIC_ORIGIN must use a hostname or IPv4 address")
        host = origin.hostname.encode("idna").decode("ascii")
        default_port = 443 if origin.scheme == "https" else 80
        port = origin.port
        canonical = f"{origin.scheme}://{host}"
        if port is not None and port != default_port:
            canonical += f":{port}"
        if self.public_origin != canonical:
            raise ValueError(
                "PUBLIC_ORIGIN must use lowercase ASCII, with no default port"
            )
        if origin.scheme == "http" and origin.hostname not in {
            "localhost", "127.0.0.1"
        }:
            raise ValueError("PUBLIC_ORIGIN must use HTTPS outside localhost")

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.public_origin).scheme == "https"

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"{name} is required")
            return value

        encoded_key = required("PASETO_SECRET_KEY")
        try:
            key = base64.b64decode(
                encoded_key + "=" * (-len(encoded_key) % 4),
                altchars=b"-_",
                validate=True,
            )
        except ValueError:
            raise ValueError("PASETO_SECRET_KEY must be base64url encoded") from None

        return cls(
            admin_password=required("ADMIN_PASSWORD"),
            device_token=required("DEVICE_TOKEN"),
            paseto_secret_key=key,
            database_path=Path(
                os.environ.get(
                    "DATABASE_PATH", str(Path(__file__).parent / "data" / "psst.db")
                )
            ),
            public_origin=os.environ.get("PUBLIC_ORIGIN", "http://localhost:8000"),
            ping_interval_seconds=int(os.environ.get("PING_INTERVAL_SECONDS", "30")),
            session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "43200")),
        )
