import functools
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_service import InMemoryAuthStore, get_auth_store
from app.services.store import InMemoryStore, get_store

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

TEST_USERNAME = "test-user"
TEST_PASSWORD = "test-password-123"


@pytest.fixture
def store():
    """A fresh InMemoryStore per test, overriding the app-wide singleton."""
    test_store = InMemoryStore()
    app.dependency_overrides[get_store] = lambda: test_store
    yield test_store
    app.dependency_overrides.pop(get_store, None)


@pytest.fixture
def auth_store():
    """A fresh InMemoryAuthStore per test, overriding the app-wide singleton —
    same reasoning as the `store` fixture: tests must not see each other's
    users/sessions."""
    test_auth_store = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: test_auth_store
    yield test_auth_store
    app.dependency_overrides.pop(get_auth_store, None)


@pytest.fixture
def unauthenticated_client(store, auth_store):
    """A TestClient with no session cookie — for tests that specifically
    exercise the "not logged in" path. Most tests want `client` instead."""
    return TestClient(app)


@pytest.fixture
def client(store, auth_store):
    """
    A TestClient that's already logged in as a fresh per-test user — nearly
    every router test predates phase 7's auth requirement and is about its
    own feature, not about auth, so authenticating here once keeps that
    whole existing suite working without touching every individual test.
    Tests that specifically need to exercise 401s use `unauthenticated_client`.
    """
    auth_store.create_user(TEST_USERNAME, TEST_PASSWORD)
    test_client = TestClient(app)
    response = test_client.post(
        "/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, f"test fixture login failed: {response.text}"
    return test_client


@pytest.fixture
def fixture_server():
    """
    Serves backend/tests/fixtures/ over real HTTP on a random localhost port.
    Real HTTP (not file://) so Playwright's wait_for_url glob patterns behave
    exactly like they do against the real staging site.
    """
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def two_origin_fixture_servers():
    """
    Two separate localhost origins, mirroring the real site's cross-domain
    OAuth hop (influence-stg.movingwalls.com -> auth-stg.movingwalls.com ->
    back). A single-origin fixture can't meaningfully test login()'s
    wait_for_url(f"{base_url}/**") success check: `**` trivially matches any
    same-origin URL, including the login page itself with zero navigation —
    only a real cross-origin redirect makes that check discriminate success
    from failure, same as production.

    Yields (app_origin, login_origin). app_origin's `/` 302-redirects to
    login_origin (the fixture login form); login_origin's form redirects
    back to app_origin/dashboard.html on the one recognized credential pair.
    """
    login_server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR)),
    )
    login_thread = threading.Thread(target=login_server.serve_forever, daemon=True)
    login_thread.start()
    login_origin = f"http://127.0.0.1:{login_server.server_port}"

    class AppOriginHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/dashboard.html":
                body = b"<html><body><h1>Dashboard</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                success_redirect = quote(f"{app_origin}/dashboard.html", safe="")
                self.send_response(302)
                self.send_header("Location", f"{login_origin}/?success_redirect={success_redirect}")
                self.end_headers()

        def log_message(self, format, *args):
            pass

    app_server = ThreadingHTTPServer(("127.0.0.1", 0), AppOriginHandler)
    app_thread = threading.Thread(target=app_server.serve_forever, daemon=True)
    app_thread.start()
    app_origin = f"http://127.0.0.1:{app_server.server_port}"

    try:
        yield app_origin, login_origin
    finally:
        login_server.shutdown()
        login_thread.join()
        app_server.shutdown()
        app_thread.join()
