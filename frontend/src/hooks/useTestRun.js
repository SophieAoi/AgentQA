import { useEffect, useRef, useState, useCallback } from "react";
import { startTestRun, getTestRun } from "../api";

const POLL_INTERVAL_MS = 1500;
const MAX_CONSECUTIVE_FAILURES = 3;
const TERMINAL_STATUSES = new Set(["passed", "failed", "error"]);

/**
 * Owns the lifecycle of a test run: starting it, polling GET /test-runs/{id}
 * until it reaches a terminal status, and cleaning up the interval on
 * unmount or when a new run starts. Polling previously lived inline in
 * TestRunnerPanel with no unmount cleanup and no error handling, so a
 * component unmount or a backend hiccup mid-run left an interval spinning
 * forever (audit finding F-002) — this hook fixes both.
 */
export function useTestRun() {
  const [run, setRun] = useState(null);
  const [pollError, setPollError] = useState(null);
  const runIdRef = useRef(null);

  useEffect(() => {
    const runId = run?.run_id;
    runIdRef.current = runId;
    if (!runId || TERMINAL_STATUSES.has(run?.status)) {
      return undefined;
    }

    let cancelled = false;
    let consecutiveFailures = 0;

    const intervalId = setInterval(async () => {
      try {
        const detail = await getTestRun(runId);
        if (cancelled || runIdRef.current !== runId) return;
        consecutiveFailures = 0;
        setPollError(null);
        setRun(detail);
      } catch (err) {
        if (cancelled || runIdRef.current !== runId) return;
        consecutiveFailures += 1;
        if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
          setPollError(
            "Lost connection to the backend while this run was in progress."
          );
          clearInterval(intervalId);
        }
      }
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, [run?.run_id, run?.status]);

  const runTests = useCallback(async (testCaseIds) => {
    setPollError(null);
    const summary = await startTestRun(testCaseIds);
    setRun(summary);
    return summary;
  }, []);

  return { run, pollError, runTests };
}
