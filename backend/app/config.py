"""
Centralized environment loading. Reused by chat_service.py and, from Phase 2,
the Playwright agent (agent/browser/login.py, agent/runner.py).
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Phase 8 (docs/phase-08-local-llm-migration.md): all reasoning (Planner,
# Executor, Verifier, chat) runs against a locally-hosted Ollama server by
# default — no paid third-party model API unless USE_NVIDIA_BACKEND below is
# explicitly opted into. Requires `ollama serve` running and the model
# pulled (`ollama pull qwen2.5:14b-instruct`).
# Default is the 14B build, not 32B: on 24GB-RAM machines the 20GB 32B
# model doesn't fit on GPU alongside everything else, so Ollama falls back
# to ~86% CPU compute — that's what was pinning the fan. The 9GB 14B model
# fits fully on GPU with headroom, so pick 32B only on a machine with
# enough unified/VRAM memory to keep it fully GPU-resident (verify with
# `ollama ps` — check the PROCESSOR column reads mostly/all "GPU").
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:11434")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:14b-instruct")

# How long Ollama keeps the model loaded in memory after the last request
# before unloading it. Ollama's own default is 5m — measured real cost of
# reloading from cold is a few extra seconds on the next call, which lands
# on every run if consecutive runs are more than 5 minutes apart. 30m
# trades idle RAM for skipping that tax on any reasonably-paced
# back-to-back testing session.
LOCAL_LLM_KEEP_ALIVE = os.environ.get("LOCAL_LLM_KEEP_ALIVE", "30m")

# Opt-in escape hatch from the "no paid third-party model API" directive
# (see agent/local_llm.py's module docstring) — added to A/B test speed vs.
# reliability against a cloud NIM endpoint. Off by default: real test runs
# never silently switch backends. When enabled, real staging credentials
# and live page content are sent to NVIDIA's servers — a deliberate,
# explicit tradeoff made per-run, not a default.
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")
NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
USE_NVIDIA_BACKEND = os.environ.get("USE_NVIDIA_BACKEND", "false").lower() == "true"

# Opt-in Gemini backend (off by default) — same rationale/tradeoffs as the
# NVIDIA block above. A single-call benchmark found gemini-flash-latest
# (resolves to gemini-3.7-flash) slower than local 14B on a representative
# verification prompt (4.39s vs 2.32s) due to ~100+ tokens of default
# "thinking" overhead the API doesn't skip without extra config — same
# thinking-mode tax that made qwen3:4b slower than 14B earlier in this
# project. Kept as an opt-in comparison path per explicit request to try it
# through the real UI, not because the benchmark was promising.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/models")
USE_GEMINI_BACKEND = os.environ.get("USE_GEMINI_BACKEND", "false").lower() == "true"

# Verification (agent/verifier.py) is a narrower, classification-like judgment
# call compared to the Executor's open-ended tool use, so it's a natural place
# to allow a distinct (e.g. smaller/faster) local model — defaults to the
# same model the Executor uses. Must track USE_NVIDIA_BACKEND/USE_GEMINI_BACKEND
# same as LOCAL_LLM_MODEL does: a model tag from the wrong backend 404s.
_active_model = LOCAL_LLM_MODEL
if USE_NVIDIA_BACKEND:
    _active_model = NVIDIA_MODEL
elif USE_GEMINI_BACKEND:
    _active_model = GEMINI_MODEL
VERIFIER_MODEL = os.environ.get("VERIFIER_MODEL", _active_model)

# Per-test-case wall-clock ceiling (agent/runner.py) so a runaway agent loop
# can't hang a run forever. 300s was fine when this called Claude; phase 8's
# local model is slower per call (a single fixture-page run measured ~195s
# for 5 steps) so 900s is a more realistic default against real, more
# complex pages — raise further via env if a specific test case still times
# out.
TEST_CASE_TIMEOUT_SECONDS = int(os.environ.get("TEST_CASE_TIMEOUT_SECONDS", "900"))

# Used to build absolute screenshot URLs in agent/reporter.py's HTML report —
# relative /screenshots/... paths don't resolve when Playwright loads the
# report HTML standalone (no browser address bar) to render the PDF export.
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8000")

INFLUENCE_BASE_URL = os.environ.get("INFLUENCE_BASE_URL", "https://influence-stg.movingwalls.com")
INFLUENCE_TEST_USERNAME = os.environ.get("INFLUENCE_TEST_USERNAME")
INFLUENCE_TEST_PASSWORD = os.environ.get("INFLUENCE_TEST_PASSWORD")
# The campaign new Line Items get created under during test runs — specific
# to whichever staging environment INFLUENCE_BASE_URL points at.
INFLUENCE_TEST_CAMPAIGN_ID = os.environ.get("INFLUENCE_TEST_CAMPAIGN_ID")

# False runs a real visible browser window — useful for local debugging.
PLAYWRIGHT_HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"

# Phase 7 auth (docs/phase-07-authentication.md). The seed admin account is
# created once on startup if the user store is empty — never write real
# credentials into files; set these directly in backend/.env.
INITIAL_ADMIN_USERNAME = os.environ.get("INITIAL_ADMIN_USERNAME")
INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD")

# Off by default so local http://localhost dev still works — a cookie marked
# Secure is dropped by the browser over plain HTTP. Turn this on (via env)
# for any real deployment behind TLS.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(24 * 60 * 60)))
