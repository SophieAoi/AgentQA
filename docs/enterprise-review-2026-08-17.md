# Targeted Investigation — Live Preview Pipeline & Model-Switch Regression

**Date:** 2026-08-17
**Scope:** Two specific technical questions raised mid-session, investigated directly with real, repeated measurements rather than a full multi-dimensional audit (the orchestrator's usual scope didn't fit a two-question root-cause task — see note at end).

---

## Question 1 — Is the live-preview "WebEngine" pipeline slow or broken?

**Finding: No. Confidence: High (reason: measured directly, not inferred).**

Real timing of `page.screenshot(type="jpeg", quality=50)` against the live staging site, 10 consecutive captures:

```
[0.057, 0.044, 0.032, 0.034, 0.033, 0.034, 0.033, 0.033, 0.033, 0.034] seconds
avg: 0.037s
```

Screenshot capture is consistently ~35ms — not a bottleneck. The perceived slowness of the live preview is not the browser/WebEngine/screenshot layer. It's the gap between actions while the LLM is generating its next tool call or judgment — the preview has nothing new to show during that gap, which reads as the preview itself being slow. This matches every timing measurement taken earlier in this session (individual model calls ranging from ~2s to 80+s on the same hardware, same task).

**No code change needed for this question** — the pipeline is working as designed.

---

## Question 2 — Is `qwen2.5-coder:14b` worse than `qwen2.5:14b-instruct` at Verifier judgment?

**Finding: No — on the specific failure observed, it's the opposite. Confidence: High for the specific test case measured (reason: 5-trial repeated real comparison, not a single run); Medium for generalizing beyond it (reason: only one DOM state / one expected-outcome phrasing tested in the controlled comparison).**

### What triggered the question

Two live runs of `AD_LG_01` today on `qwen2.5-coder:14b` each failed on a different Verifier judgment:
- Run 1: false negative — said no error message was visible when "Invalid credentials" was genuinely on the page.
- Run 2: false judgment on the final step — treated the (expected, documented-as-normal) OAuth redirect URL as evidence the user "was not blocked from the app."

### Controlled comparison

Same real DOM snapshot (captured live from `auth-stg.movingwalls.com` after a real failed login), same `VERIFIER_SYSTEM_PROMPT`, same expected-outcome text (`"Check that the user is not allowed into the application"` — the exact phrasing that failed in Run 2), 5 repeated trials per model, `think=False` on both:

| Model | Correct | Detail |
|---|---|---|
| `qwen2.5-coder:14b` | **4/5** | One wrong (attempt 2), otherwise consistently correct |
| `qwen2.5:14b-instruct` | **0/5** | Wrong every single time, full confidence (1.00) on each |

`qwen2.5:14b-instruct` has a **systematic blind spot** on this exact phrasing — it isn't random variance, it failed identically 5/5 times. `qwen2.5-coder:14b` handles the same case correctly most of the time.

### Interpretation

The two live failures observed on the coder model are consistent with the per-call non-determinism already documented extensively earlier in this session (repeated actions, occasional slow/wrong calls on both `qwen2.5:14b-instruct` and other models tested) — not evidence the coder model is categorically worse at this task. The one head-to-head measurement taken points the other way.

**This does not mean "coder is definitively better"** — one DOM state and one phrasing is a narrow sample. It does mean the original hypothesis (coder model regressed Verifier quality) is not supported by the evidence gathered, and the reverse claim has at least as much real support.

### Recommendation

**Confidence: Medium** (reason: based on the one controlled comparison above plus this session's broader pattern of per-call variance across every model tried; not a large-sample statistical claim).

Keep `qwen2.5-coder:14b` for now — there's no real evidence it's worse, and one controlled test favors it. If more live runs surface a *repeated, specific* wrong judgment (like the instruct model's 5/5 failure above), that's the actionable signal to act on — a single wrong verdict on a single run is expected background noise at this model size, not proof either model is broken.

No paid API was considered per the project's standing no-paid-API directive.

---

## Note on process

This was run as a direct, scoped investigation rather than a full `/enterprise-orchestrator` pass — the orchestrator's activation matrix (codebase-audit + perf-engineer + clean-architecture + security-audit across the whole project) doesn't fit a two-question root-cause task and would have spent significant time/tokens auditing unrelated code. Real measurements (screenshot timing, 5-trial model comparison) were taken directly against the live codebase and live staging site instead of guessed at.

**Overall: PASS** (both questions answered with real evidence; no code changes required as a result of this investigation).
