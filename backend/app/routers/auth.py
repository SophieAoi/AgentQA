"""
Auth endpoints + the get_current_user dependency every other router now
requires (docs/phase-07-authentication.md).
"""

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response

from app.config import SESSION_COOKIE_SECURE
from app.models.schemas import LoginRequest, RegisterRequest, User
from app.services.auth_service import AuthStoreProtocol, get_auth_store, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE_NAME = "session"


def get_current_user(
    session: str | None = Cookie(default=None),
    auth_store: AuthStoreProtocol = Depends(get_auth_store),
) -> User:
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    record = auth_store.get_session(session)
    if not record:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    user = auth_store.get_user_by_id(record.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user.to_public()


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
        path="/",
    )


@router.post("/login", response_model=User)
def login(
    request: LoginRequest,
    response: Response,
    auth_store: AuthStoreProtocol = Depends(get_auth_store),
):
    user = auth_store.get_user_by_username(request.username)
    # Same generic message whether the username doesn't exist or the
    # password is wrong — don't help an attacker enumerate valid usernames.
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = auth_store.create_session(user.id)
    _set_session_cookie(response, token)
    return user.to_public()


@router.post("/logout")
def logout(
    response: Response,
    session: str | None = Cookie(default=None),
    auth_store: AuthStoreProtocol = Depends(get_auth_store),
):
    if session:
        auth_store.delete_session(session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=User)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/register", response_model=User)
def register(
    request: RegisterRequest,
    # Requires an existing valid session — no open public signup. Any
    # logged-in user can create more accounts (no admin/regular role split
    # yet, see the phase doc's "explicitly not in scope").
    current_user: User = Depends(get_current_user),
    auth_store: AuthStoreProtocol = Depends(get_auth_store),
):
    if not request.username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    if auth_store.username_exists(request.username):
        raise HTTPException(status_code=409, detail="Username already exists")

    user = auth_store.create_user(request.username, request.password)
    return user.to_public()
