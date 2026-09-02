"""
INFLUENCE QA — Backend Entry Point
====================================
Run with:
  uvicorn app.main:app --reload --port 8000

Interactive API docs (auto-generated, handy for testing without the
frontend) will be at http://localhost:8000/docs
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.routers import auth, chat, reports, test_cases, test_runs, websockets
from app.services.auth_service import get_auth_store

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)


def _bootstrap_seed_admin() -> None:
    """
    Creates the first account from env-configured credentials, once, if no
    users exist yet — otherwise there's no way to log in at all on a fresh
    deployment (no open public signup by design, see
    docs/phase-07-authentication.md). Never write real credentials into
    files — set INITIAL_ADMIN_USERNAME/PASSWORD directly in backend/.env.
    """
    auth_store = get_auth_store()
    if auth_store.user_count() > 0:
        return
    if not INITIAL_ADMIN_USERNAME or not INITIAL_ADMIN_PASSWORD:
        return
    auth_store.create_user(INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _bootstrap_seed_admin()
    yield


app = FastAPI(title="INFLUENCE QA API", lifespan=_lifespan)

# Allow the React dev server (typically localhost:5173 for Vite, or
# localhost:3000 for Create React App) to call this API.
# TODO: restrict this to your actual frontend URL once deployed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(test_runs.router)
app.include_router(test_cases.router)
app.include_router(websockets.router)
app.include_router(reports.router)

app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")


@app.get("/")
def root():
    return {"status": "ok", "service": "INFLUENCE QA API"}
