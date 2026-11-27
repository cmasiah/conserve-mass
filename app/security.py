"""Token encryption and web session auth."""
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt(value: bytes) -> str:
    try:
        return _fernet().decrypt(value).decode()
    except InvalidToken as e:  # key rotated / wrong key
        raise RuntimeError("Could not decrypt stored token — TOKEN_ENCRYPTION_KEY changed?") from e


class LoginRequired(Exception):
    """Raised by require_user; handled in main.py with a redirect to /login."""


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise LoginRequired()
    return user


def require_csrf(request: Request, token: str | None) -> None:
    if not token or token != request.session.get("csrf"):
        raise HTTPException(status_code=403, detail="CSRF check failed")
