import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useTestRun } from "./useTestRun";
import * as api from "../api";

describe("useTestRun", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("clears the polling interval on unmount", async () => {
    vi.spyOn(api, "startTestRun").mockResolvedValue({
      run_id: "abc123",
      status: "running",
    });
    const getTestRunSpy = vi
      .spyOn(api, "getTestRun")
      .mockResolvedValue({ run_id: "abc123", status: "running" });

    const { result, unmount } = renderHook(() => useTestRun());

    await act(async () => {
      await result.current.runTests(["TC-001"]);
    });

    unmount();

    const callsAtUnmount = getTestRunSpy.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    // No further polling should happen once unmounted — the interval
    // must actually be cleared, not just logically ignored.
    expect(getTestRunSpy.mock.calls.length).toBe(callsAtUnmount);
  });

  it("stops polling and surfaces an error after repeated consecutive failures", async () => {
    vi.spyOn(api, "startTestRun").mockResolvedValue({
      run_id: "abc123",
      status: "running",
    });
    vi.spyOn(api, "getTestRun").mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useTestRun());

    await act(async () => {
      await result.current.runTests(["TC-001"]);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500 * 3);
    });

    await waitFor(() => {
      expect(result.current.pollError).toBeTruthy();
    });
  });

  it("stops polling once the run reaches a terminal status", async () => {
    vi.spyOn(api, "startTestRun").mockResolvedValue({
      run_id: "abc123",
      status: "running",
    });
    const getTestRunSpy = vi
      .spyOn(api, "getTestRun")
      .mockResolvedValue({ run_id: "abc123", status: "passed" });

    const { result } = renderHook(() => useTestRun());

    await act(async () => {
      await result.current.runTests(["TC-001"]);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });

    await waitFor(() => {
      expect(result.current.run.status).toBe("passed");
    });

    const callsAtTerminal = getTestRunSpy.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(getTestRunSpy.mock.calls.length).toBe(callsAtTerminal);
  });
});
