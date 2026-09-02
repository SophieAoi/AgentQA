import { useEffect, useMemo, useRef, useState } from "react";
import { useRunStream } from "../hooks/useRunStream";
import { useResizablePanes } from "../hooks/useResizablePanes";

// TODO: set this to your actual Influence staging URL
const DEFAULT_INFLUENCE_URL = "https://influence-stg.movingwalls.com";
const TERMINAL_STATUSES = new Set(["passed", "failed", "error"]);

const DEFAULT_STREAM_PERCENT = 45;

export default function SiteViewer({ run }) {
  const [url, setUrl] = useState(DEFAULT_INFLUENCE_URL);
  const [inputValue, setInputValue] = useState(DEFAULT_INFLUENCE_URL);
  const logEndRef = useRef(null);
  const {
    containerRef: panesRef,
    topPercent: streamPercent,
    startDrag,
    handleDividerKeyDown,
  } = useResizablePanes(DEFAULT_STREAM_PERCENT);

  const isActive = Boolean(run?.run_id) && !TERMINAL_STATUSES.has(run?.status);
  const { liveLogLines, frame, connected } = useRunStream(run?.run_id, isActive);

  // Prefer the WS stream while it's live — lower latency than the 1.5s
  // poll — falling back to the polled (authoritative) run.logs once the
  // run finishes or the stream isn't connected. WS never blocks the
  // correctness path: run.logs is always complete regardless of streaming.
  const logs = useMemo(() => {
    if (isActive && connected && liveLogLines.length > 0) return liveLogLines;
    return run?.logs ?? [];
  }, [isActive, connected, liveLogLines, run?.logs]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs.length]);

  function handleNavigate() {
    setUrl(inputValue);
  }

  return (
    <div className="site-viewer">
      <div className="site-viewer-toolbar">
        <input
          className="site-viewer-url"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleNavigate()}
        />
        <button onClick={handleNavigate}>Go</button>
        <a href={url} target="_blank" rel="noreferrer" className="open-new-tab-link">
          Open in new tab ↗
        </a>
        {isActive && (
          <span className={`stream-indicator stream-indicator--${connected ? "live" : "connecting"}`}>
            {connected ? "● Live" : "○ Connecting…"}
          </span>
        )}
      </div>

      {/*
        Influence's login is an OAuth redirect to auth-stg.movingwalls.com,
        and modern browsers block third-party cookies inside iframes — that
        hangs the OAuth flow indefinitely with no console errors. So instead
        of embedding the site here, this panel is split into two resizable
        sections: a live stream (periodic screenshot frames over WebSocket,
        phase 5 — not a true live embed, just a recent snapshot of what the
        agent's browser is looking at) and a terminal-style scrolling log of
        what the agent is actually doing. Use "Open in new tab" above to
        view the real site directly.
      */}
      <div className="site-viewer-panes" ref={panesRef}>
        <div className="site-viewer-pane site-viewer-stream-pane" style={{ height: `${streamPercent}%` }}>
          <div className="site-viewer-pane-label">Live Stream</div>
          <div className="site-viewer-pane-body site-viewer-stream-body">
            {frame ? (
              <img src={frame} alt="Live agent browser frame" className="site-viewer-frame" />
            ) : isActive ? (
              // A run is genuinely active but no frame has arrived yet — the
              // first frame previously only landed after the Executor's
              // first tool call, so this window (browser launch + login +
              // Planner thinking) could silently show the same "no run in
              // progress" placeholder for 15-25+s while a run was very much
              // in progress. Now the backend publishes an early frame right
              // after launch, but a spinner here covers whatever gap
              // remains (e.g. before that first frame's WS message arrives).
              <div className="site-viewer-stream-loading">
                <span className="site-viewer-spinner" aria-hidden="true" />
                Waiting for the first frame…
              </div>
            ) : (
              <div className="site-viewer-stream-empty">No test run in progress yet</div>
            )}
          </div>
        </div>

        <div
          className="site-viewer-divider"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize live stream and terminal output sections"
          tabIndex={0}
          onMouseDown={startDrag}
          onKeyDown={handleDividerKeyDown}
        >
          <div className="site-viewer-divider-handle" />
        </div>

        <div
          className="site-viewer-pane site-viewer-terminal-pane"
          style={{ height: `${100 - streamPercent}%` }}
        >
          <div className="site-viewer-pane-label">Terminal Output</div>
          <div className="site-viewer-pane-body site-viewer-log-panel">
            {logs.length === 0 ? (
              <div className="site-viewer-log-empty">
                No test run in progress. Start a run from the panel on the right
                to see live agent activity here.
              </div>
            ) : (
              <ul className="site-viewer-log-list">
                {logs.map((line, i) => (
                  <li key={i} className="site-viewer-log-line">
                    {line}
                  </li>
                ))}
                <li ref={logEndRef} />
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
