import { useEffect, useRef, useState } from "react";
import { getRunStreamUrl } from "../api";

const MAX_BACKOFF_MS = 10000;
const INITIAL_BACKOFF_MS = 1000;

/**
 * Owns the lifecycle of the two live WebSocket streams for one active test
 * run (logs, browser screenshot frames). Mirrors useTestRun.js's cleanup-
 * on-unmount discipline: every socket and timer opened here is closed on
 * unmount or whenever runId/active changes, so a component unmount can
 * never leave a reconnect loop spinning forever.
 *
 * WS is purely additive — useTestRun's existing 1.5s poll of
 * GET /test-runs/{id} keeps running independently and stays the source of
 * truth for final status and the authoritative step/log list. If a socket
 * never connects (or the backend doesn't support it yet), the polling flow
 * from phases 0-4 works exactly as before; this hook only ever adds a
 * lower-latency view on top; it never blocks or replaces the correctness
 * path (docs/phase-05-live-streaming-websockets.md).
 */
export function useRunStream(runId, active) {
  const [liveLogLines, setLiveLogLines] = useState([]);
  const [frame, setFrame] = useState(null);
  const [connected, setConnected] = useState(false);

  const runIdRef = useRef(null);

  useEffect(() => {
    setLiveLogLines([]);
    setFrame(null);
    setConnected(false);

    if (!runId || !active) {
      return undefined;
    }

    runIdRef.current = runId;
    let cancelled = false;
    const sockets = [];
    const reconnectTimers = [];
    const logsConnectedRef = { current: false };
    const browserConnectedRef = { current: false };

    function updateConnected() {
      setConnected(logsConnectedRef.current || browserConnectedRef.current);
    }

    function connectChannel(channel, onMessage, connectedRef, backoffMs = INITIAL_BACKOFF_MS) {
      if (cancelled || runIdRef.current !== runId) return;

      const ws = new WebSocket(getRunStreamUrl(runId, channel));
      sockets.push(ws);

      ws.onopen = () => {
        if (cancelled) return;
        connectedRef.current = true;
        updateConnected();
      };

      ws.onmessage = (event) => {
        if (cancelled) return;
        try {
          const parsed = JSON.parse(event.data);
          if (parsed.type !== "connected") onMessage(parsed);
        } catch {
          // Ignore malformed frames rather than tearing down the stream —
          // the next GET poll reconciles state regardless.
        }
      };

      const scheduleReconnect = () => {
        if (cancelled || runIdRef.current !== runId) return;
        connectedRef.current = false;
        updateConnected();
        const timer = setTimeout(() => {
          connectChannel(channel, onMessage, connectedRef, Math.min(backoffMs * 2, MAX_BACKOFF_MS));
        }, backoffMs);
        reconnectTimers.push(timer);
      };

      ws.onclose = scheduleReconnect;
      ws.onerror = () => ws.close();
    }

    connectChannel(
      "logs",
      (parsed) => {
        if (parsed.type === "log") {
          setLiveLogLines((prev) => [...prev, parsed.data.message]);
        }
      },
      logsConnectedRef
    );

    connectChannel(
      "browser",
      (parsed) => {
        if (parsed.type === "screenshot") {
          setFrame(parsed.data.image);
        }
      },
      browserConnectedRef
    );

    return () => {
      cancelled = true;
      reconnectTimers.forEach(clearTimeout);
      sockets.forEach((ws) => {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onclose = null;
        ws.onerror = null;
        ws.close();
      });
    };
  }, [runId, active]);

  return { liveLogLines, frame, connected };
}
