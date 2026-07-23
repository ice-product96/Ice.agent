import hashlib
import hmac
import json
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

bearer = HTTPBearer(auto_error=False)


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_token(settings: Settings) -> tuple[str, datetime]:
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.access_token_hours)
    payload = _b64(json.dumps({"exp": int(expires.timestamp()), "nonce": secrets.token_hex(8)}).encode())
    signature = _b64(hmac.new(settings.secret_key.get_secret_value().encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}", expires


def verify_token(token: str, settings: Settings) -> None:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(settings.secret_key.get_secret_value().encode(), payload.encode(), hashlib.sha256).digest())
        data = json.loads(_unb64(payload))
        if not hmac.compare_digest(signature, expected) or data["exp"] < time.time():
            raise ValueError
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    verify_token(credentials.credentials, settings)


def valid_password(provided: str, settings: Settings) -> bool:
    return hmac.compare_digest(provided.encode(), settings.admin_password.get_secret_value().encode())
