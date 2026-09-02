import { useCallback, useEffect, useRef, useState } from "react";

const MIN_WIDTH = 300;
const MAX_WIDTH = 720;
const STORAGE_KEY = "agentqa-sidebar-width";

function clamp(width) {
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width));
}

function loadStoredWidth(defaultWidth) {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? Number(raw) : NaN;
    return Number.isFinite(parsed) ? clamp(parsed) : defaultWidth;
  } catch {
    return defaultWidth;
  }
}

// Drag-to-resize for the right-hand sidebar's width (the chat + test
// runner column) — same mouse/keyboard drag pattern as
// useResizablePanes, but horizontal and pixel-based rather than
// vertical and percentage-based, since a sidebar's usable width is
// better bounded by an absolute px range than a percent of whatever
// the window happens to be. Persists to localStorage so the chosen
// width survives a reload.
export function useResizableSidebar(defaultWidth) {
  const [width, setWidth] = useState(() => loadStoredWidth(defaultWidth));
  const draggingRef = useRef(false);

  const handleDragMove = useCallback((clientX) => {
    // Dragging the divider left/right of the viewport's right edge
    // maps directly to sidebar width — the sidebar sits flush against
    // the right edge, so width = distance from the pointer to that edge.
    const width = window.innerWidth - clientX;
    setWidth(clamp(width));
  }, []);

  function persistWidth(w) {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(w));
    } catch {
      // Best-effort only — a private window or blocked storage just
      // means the width resets next load, not a broken drag.
    }
  }

  useEffect(() => {
    function onMouseMove(e) {
      if (!draggingRef.current) return;
      handleDragMove(e.clientX);
    }
    function onMouseUp() {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      document.body.classList.remove("is-resizing-sidebar");
      setWidth((w) => {
        persistWidth(w);
        return w;
      });
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [handleDragMove]);

  function startDrag() {
    draggingRef.current = true;
    document.body.classList.add("is-resizing-sidebar");
  }

  function handleDividerKeyDown(e) {
    const step = 20;
    let next;
    if (e.key === "ArrowLeft") {
      next = clamp(width + step); // divider is left of the sidebar: left = wider
    } else if (e.key === "ArrowRight") {
      next = clamp(width - step);
    } else {
      return;
    }
    e.preventDefault();
    setWidth(next);
    persistWidth(next);
  }

  return { width, startDrag, handleDividerKeyDown };
}
