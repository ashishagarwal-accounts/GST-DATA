"""Authentication: scrypt password hashes, server-side sessions in an HttpOnly cookie, login throttling.

Every /api route except /api/auth/* and /api/health depends on `require_user`; admin-only routes on
`require_admin`. The first user is created through /api/auth/setup, which only works while there are no
users at all.
"""
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import User, UserSession, utcnow

COOKIE = "alcove_session"
SESSION_HOURS = 12
MIN_PASSWORD = 8

# scrypt cost: ~50 ms per hash on a laptop; stored with the hash so it can be raised later.
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A hash to verify against when the email is unknown, so response time does not reveal which emails exist.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if password.isdigit() or password.isalpha():
        return "password must mix letters with numbers or symbols"
    return None


# ---------------------------------------------------------------- throttling (per email and per client IP)

_FAILURES: dict[str, deque] = defaultdict(deque)
MAX_FAILURES, WINDOW_SECONDS = 5, 15 * 60


def _recent(key: str) -> deque:
    q, now = _FAILURES[key], time.monotonic()
    while q and now - q[0] > WINDOW_SECONDS:
        q.popleft()
    return q


def check_throttle(*keys: str) -> None:
    if any(len(_recent(k)) >= MAX_FAILURES for k in keys):
        raise HTTPException(429, "too many failed sign-in attempts; try again in 15 minutes")


def record_failure(*keys: str) -> None:
    for k in keys:
        _recent(k).append(time.monotonic())


def clear_failures(*keys: str) -> None:
    for k in keys:
        _FAILURES.pop(k, None)


# ---------------------------------------------------------------- sessions

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(session: Session, user: User, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.execute(delete(UserSession).where(UserSession.expires_at < now))  # tidy expired sessions
    session.add(UserSession(user_id=user.id, token_hash=_token_hash(token), expires_at=now + timedelta(hours=SESSION_HOURS),
                            user_agent=(user_agent or "")[:255]))
    user.last_login_at = now
    session.commit()
    return token


def end_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
        session.commit()


def end_user_sessions(session: Session, user_id: int, keep_token: str | None = None) -> None:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_token:
        stmt = stmt.where(UserSession.token_hash != _token_hash(keep_token))
    session.execute(stmt)


def set_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")


def require_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "sign in required")
    row = session.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
    if not row or row.expires_at < utcnow() or not row.user.active:
        raise HTTPException(401, "session expired; sign in again")
    return row.user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "only an administrator can do this")
    return user
