"""
Phase 7 auth tests: login/logout, session cookie issuance and validation,
protected endpoints 401 without a session and work with one, and the
bootstrap admin creation logic.
"""

from unittest.mock import patch

from app.main import _bootstrap_seed_admin
from app.services.auth_service import InMemoryAuthStore, verify_password


def test_login_with_correct_credentials_issues_a_working_session(unauthenticated_client, auth_store):
    auth_store.create_user("alice", "correct-horse")

    response = unauthenticated_client.post(
        "/auth/login", json={"username": "alice", "password": "correct-horse"}
    )

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert "session" in response.cookies

    me = unauthenticated_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def test_login_with_wrong_password_rejected_with_generic_message(unauthenticated_client, auth_store):
    auth_store.create_user("alice", "correct-horse")

    response = unauthenticated_client.post(
        "/auth/login", json={"username": "alice", "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"


def test_login_with_unknown_username_gets_the_same_generic_message(unauthenticated_client, auth_store):
    """Same message as a wrong password — don't help enumerate valid usernames."""
    response = unauthenticated_client.post(
        "/auth/login", json={"username": "nobody", "password": "whatever"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"


def test_me_401s_without_a_session(unauthenticated_client):
    response = unauthenticated_client.get("/auth/me")
    assert response.status_code == 401


def test_logout_invalidates_the_session(client):
    me_before = client.get("/auth/me")
    assert me_before.status_code == 200

    logout = client.post("/auth/logout")
    assert logout.status_code == 200

    me_after = client.get("/auth/me")
    assert me_after.status_code == 401


def test_protected_endpoints_401_without_a_session_and_work_with_one(unauthenticated_client, client):
    for path in ["/test-cases", "/test-runs", "/chat/history"]:
        unauth_response = unauthenticated_client.get(path)
        assert unauth_response.status_code == 401, f"{path} should require auth"

        auth_response = client.get(path)
        assert auth_response.status_code == 200, f"{path} should work when logged in"


def test_register_requires_an_existing_session(unauthenticated_client, client):
    unauth_response = unauthenticated_client.post(
        "/auth/register", json={"username": "bob", "password": "hunter2xxxx"}
    )
    assert unauth_response.status_code == 401

    auth_response = client.post("/auth/register", json={"username": "bob", "password": "hunter2xxxx"})
    assert auth_response.status_code == 200
    assert auth_response.json()["username"] == "bob"


def test_register_rejects_a_duplicate_username(client, auth_store):
    auth_store.create_user("carol", "some-password")

    response = client.post("/auth/register", json={"username": "carol", "password": "another-password"})

    assert response.status_code == 409


def test_password_is_hashed_not_stored_in_plaintext(auth_store):
    user = auth_store.create_user("dave", "super-secret-password")

    assert user.password_hash != b"super-secret-password"
    assert verify_password("super-secret-password", user.password_hash)
    assert not verify_password("wrong-guess", user.password_hash)


def test_bootstrap_seed_admin_creates_exactly_one_user_across_repeated_startups():
    fresh_auth_store = InMemoryAuthStore()
    with patch("app.main.get_auth_store", return_value=fresh_auth_store), patch(
        "app.main.INITIAL_ADMIN_USERNAME", "seed-admin"
    ), patch("app.main.INITIAL_ADMIN_PASSWORD", "seed-password-123"):
        _bootstrap_seed_admin()
        _bootstrap_seed_admin()
        _bootstrap_seed_admin()

    assert fresh_auth_store.user_count() == 1
    assert fresh_auth_store.get_user_by_username("seed-admin") is not None


def test_bootstrap_seed_admin_does_nothing_without_configured_credentials():
    fresh_auth_store = InMemoryAuthStore()
    with patch("app.main.get_auth_store", return_value=fresh_auth_store), patch(
        "app.main.INITIAL_ADMIN_USERNAME", None
    ), patch("app.main.INITIAL_ADMIN_PASSWORD", None):
        _bootstrap_seed_admin()

    assert fresh_auth_store.user_count() == 0
