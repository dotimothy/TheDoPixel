from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status

from .config import Settings
from .database import Database, utcnow

SESSION_COOKIE = "pixel_relay_session"
_hasher = PasswordHasher()


class LoginLimiter:
    def __init__(self) -> None:
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        attempts = self.attempts[key]
        while attempts and attempts[0] < now - 300:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(status_code=429, detail="Too many login attempts")
        attempts.append(now)

    def clear(self, key: str) -> None:
        self.attempts.pop(key, None)


login_limiter = LoginLimiter()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def has_admin(self) -> bool:
        return self.db.fetchone("SELECT id FROM users LIMIT 1") is not None

    def create_admin(self, username: str, password: str) -> int:
        if not password:
            raise ValueError("Password cannot be empty")
        if self.has_admin():
            raise ValueError("An administrator already exists")
        return self.db.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, _hasher.hash(password), utcnow()),
        )

    def authenticate(self, username: str, password: str) -> dict | None:
        user = self.db.fetchone("SELECT * FROM users WHERE username = ?", (username,))
        if not user:
            _hasher.hash(password)
            return None
        try:
            valid = _hasher.verify(user["password_hash"], password)
        except VerifyMismatchError:
            return None
        if valid and _hasher.check_needs_rehash(user["password_hash"]):
            self.db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (_hasher.hash(password), user["id"]),
            )
        return user if valid else None

    def create_session(self, user_id: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=self.settings.session_hours)
        self.db.execute(
            """
            INSERT INTO sessions(token_hash, user_id, csrf_token, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash(token), user_id, csrf, expires.isoformat(), utcnow()),
        )
        return token, csrf

    def get_session(self, token: str) -> dict | None:
        session = self.db.fetchone(
            """
            SELECT sessions.*, users.username
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE token_hash = ?
            """,
            (token_hash(token),),
        )
        if not session:
            return None
        if datetime.fromisoformat(session["expires_at"]) <= datetime.now(UTC):
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
            return None
        return session

    def revoke(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def require_user(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    session = request.app.state.auth.get_session(session_token)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return session


def require_csrf(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict:
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not secrets.compare_digest(
        csrf_token or "", user["csrf_token"]
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return user


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        # OAuth providers return through a cross-site top-level GET. Lax keeps
        # the session available for that callback without sending it on
        # cross-site subrequests or unsafe methods.
        samesite="lax",
        max_age=settings.session_hours * 3600,
        path="/",
    )
