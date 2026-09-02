# Phase 13 — Post-navigation settle race in click()

See [BUILD-PLAN.md](BUILD-PLAN.md) for shared context. Directly
follows phase-12 (same test case, `AD_LG_04`, hit a third distinct bug
after phase-11 and phase-12's fixes both landed).

## Goal

After phase-11 and phase-12 shipped, `AD_LG_04` still failed
intermittently — but the failures no longer showed the earlier
symptoms (no invented steps, no compound-fill truncation, no
URL-path confusion). Live evidence this time: the verifier
consistently reported the page still looked like the **login page**
("shows a login page with elements such as 'Welcome Back', 'Email /
Username', and 'Password' fields") immediately after a Sign In click
that itself reported success — reproduced 3/3 on a dedicated
diagnostic run, ruling out one-off verifier non-determinism (majority
voting, phase 9, wasn't relevant here; the underlying page state being
judged was itself genuinely still mid-transition, not being
misjudged).

## Root cause

Measured directly (a standalone timing probe, not guessed): after
`agent/browser/login.py::login()`'s `page.wait_for_url()` resolves
(or, equivalently, the moment the Executor's own Sign In click lands
back on the main app's URL), the Dashboard's summary cards
("Active Line Items", "Pending Approvals", "Completed Campaigns")
take roughly **another full second** to actually render — the URL
settles before the client-side app's own async data fetch completes.

`agent/tools/playwright_tools.py::_resolve_and_act()`'s post-click
settle logic (added earlier for a different reason — see its own
existing comment) does two things after a click: wait for
`networkidle` (max 3s) and wait for the *clicked element itself* to
stop showing a disabled/loading label (max 4s). Neither one covers
this case:

- `networkidle` on a page that's mid cross-domain OAuth redirect can
  resolve on the auth-stg domain, well before the destination
  (influence-stg) page has even started its own post-redirect fetches.
- The loading-state wait targets the *same DOM element* that was
  clicked — but a real navigation (as Sign In triggers here) replaces
  the entire document, so that element handle is stale/detached by the
  time this check runs, and the wait silently no-ops rather than
  checking anything meaningful on the new page.

So the Executor's assertion step reads the DOM in the ~1s gap between
"URL is correct" and "content has actually arrived," and the read is
genuinely, correctly judged as a login page — because at that instant,
it still was one, or was a mostly-empty shell without the summary
cards. This is a real race condition affecting `click()` broadly (any
click that causes a real navigation), not something specific to
`AD_LG_04` or the login flow.

## Fix

`_resolve_and_act()` now captures `ctx.page.url` immediately before
the click. After the two existing settle waits run, if the URL has
changed (a real navigation happened, not just a same-page DOM update),
a third wait — `wait_for_load_state("networkidle", timeout=4000)` on
the **new** page — runs before the click is considered settled. This
is deliberately gated on "did the URL actually change," so ordinary
same-page clicks (the vast majority) pay no extra cost; only clicks
that cause a genuine navigation pay the extra wait, and only once.

## Verification

- Full suite (`pytest --deselect tests/test_agent_runner_integration.py
  --timeout=60`) — 172 passed, no regressions.
- Standalone timing probe (not the agent) confirmed the underlying
  gap directly: `login()` returns at ~t=3.57s while the destination
  URL still mid-transitions; dashboard content doesn't appear in the
  DOM until ~t=4.61s — about a 1-second window during which the old
  code would read stale content.
- Backend restarted, confirmed serving the fixed `click()` tool.
- Live: `AD_LG_04` re-run 3 times end-to-end against the real staging
  site after the fix — 3/3 passed cleanly, no login-page
  misjudgments.

## Files modified

- `agent/tools/playwright_tools.py` — `_resolve_and_act()`'s click
  handling: captures pre-click URL, adds a conditional post-navigation
  `networkidle` wait when the URL changed.

## Sizing

S (root cause required a dedicated timing probe to actually measure,
not just live re-runs — the fix itself is a small, targeted addition
to existing settle logic).

## Status: Done

Fixed and live-verified 3/3, with the race directly measured (not
just inferred from failures clearing up). `AD_LG_04`'s flakiness
tracker count is now at 2 (`_FLAKY_THRESHOLD`), from the two real
tie-breaks recorded across this and phase-12's live checks — so this
case now always votes on every future run rather than sampling, which
is appropriate given its demonstrated history, and requires no further
action.

This is the third distinct, real bug found and fixed on `AD_LG_04` in
close succession (phase-11: compound-step fill truncation and
URL-path over-fixation; phase-12: Planner-invented assertion steps;
phase-13: this navigation-settle race) — each one was masking the
next until fixed. `AD_LG_04` is now clean across all three
categories, and phase-13's `click()` fix benefits every other test
case whose steps involve a click-triggered navigation, not just this
one.
