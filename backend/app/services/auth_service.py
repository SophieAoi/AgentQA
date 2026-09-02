"""
Authentication (phase 7, docs/phase-07-authentication.md). Real per-user
login — username/password, bcrypt-hashed, server-side opaque sessions
delivered via an HttpOnly cookie. Deliberately not JWT: an opaque token
mapped to a session record is trivially revocable (delete the record) with
no signing-key management, the right tradeoff for a single-process
deployment with no distributed verification needs yet.

AuthStoreProtocol/InMemoryAuthStore mirror store.py's StoreProtocol/
InMemoryStore pattern exactly — same DI seam (Depends(get_auth_store)), same
future swap path to Postgres (a new class satisfying the protocol, not a
router-touching rewrite).

Data isolation is explicitly out of scope for this phase (user decision,
see the phase doc): every authenticated user still sees the same shared
chat_history/test_runs. This module only answers "is this request from
someone who's logged in," not "which user's data is this."
"""

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Protocol

import bcrypt

from app.config import SESSION_TTL_SECONDS
from app.models.schemas import User


@dataclass
class UserRecord:
    """Internal representation — carries the password hash. User (schemas.py)
    is the response-safe view that never leaves this module with a hash."""

    id: str
    username: str
    password_hash: bytes
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_public(self) -> User:
        return User(id=self.id, username=self.username, created_at=self.created_at)


@dataclass
class SessionRecord:
    token: str
    user_id: str
    expires_at: datetime


def hash_password(password: str) -> bytes:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())


def verify_password(password: str, password_hash: bytes) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash)


class AuthStoreProtocol(Protocol):
    def create_user(self, username: str, password: str) -> UserRecord: ...

    def get_user_by_username(self, username: str) -> Optional[UserRecord]: ...

    def get_user_by_id(self, user_id: str) -> Optional[UserRecord]: ...

    def username_exists(self, username: str) -> bool: ...

    def user_count(self) -> int: ...

    def create_session(self, user_id: str) -> str: ...

    def get_session(self, token: str) -> Optional[SessionRecord]: ...

    def delete_session(self, token: str) -> None: ...


class InMemoryAuthStore:
    """Dict-backed AuthStoreProtocol implementation. Resets every server
    restart — same lifetime as InMemoryStore, so the seed admin re-bootstraps
    on every restart in local dev, which is expected."""

    def __init__(self) -> None:
        self._users_by_id: dict[str, UserRecord] = {}
        self._users_by_username: dict[str, str] = {}  # username -> id
        self._sessions: dict[str, SessionRecord] = {}

    def create_user(self, username: str, password: str) -> UserRecord:
        user = UserRecord(id=uuid.uuid4().hex[:12], username=username, password_hash=hash_password(password))
        self._users_by_id[user.id] = user
        self._users_by_username[username] = user.id
        return user

    def get_user_by_username(self, username: str) -> Optional[UserRecord]:
        user_id = self._users_by_username.get(username)
        return self._users_by_id.get(user_id) if user_id else None

    def get_user_by_id(self, user_id: str) -> Optional[UserRecord]:
        return self._users_by_id.get(user_id)

    def username_exists(self, username: str) -> bool:
        return username in self._users_by_username

    def user_count(self) -> int:
        return len(self._users_by_id)

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = SessionRecord(
            token=token,
            user_id=user_id,
            expires_at=datetime.utcnow() + timedelta(seconds=SESSION_TTL_SECONDS),
        )
        return token

    def get_session(self, token: str) -> Optional[SessionRecord]:
        session = self._sessions.get(token)
        if not session:
            return None
        if session.expires_at < datetime.utcnow():
            self._sessions.pop(token, None)
            return None
        return session

    def delete_session(self, token: str) -> None:
        self._sessions.pop(token, None)


_auth_store_instance = InMemoryAuthStore()


def get_auth_store() -> AuthStoreProtocol:
    return _auth_store_instance
