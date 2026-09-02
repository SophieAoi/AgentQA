import { useCallback, useEffect, useRef, useState } from "react";

const MIN_SECTION_PERCENT = 15;

// Shared drag-to-resize logic for a vertically-split two-pane layout
// (used by SiteViewer's stream/terminal split and ChatSidebar's
// chat/test-suite split) so the resize behavior stays identical across
// both without duplicating the mouse/keyboard handling.
export function useResizablePanes(defaultPercent) {
  const [topPercent, setTopPercent] = useState(defaultPercent);
  const containerRef = useRef(null);
  const draggingRef = useRef(false);

  const handleDragMove = useCallback((clientY) => {
    const container = containerRef.current;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const percent = ((clientY - rect.top) / rect.height) * 100;
    const clamped = Math.min(
      100 - MIN_SECTION_PERCENT,
      Math.max(MIN_SECTION_PERCENT, percent)
    );
    setTopPercent(clamped);
  }, []);

  useEffect(() => {
    function onMouseMove(e) {
      if (!draggingRef.current) return;
      handleDragMove(e.clientY);
    }
    function onMouseUp() {
      draggingRef.current = false;
      document.body.classList.remove("is-resizing-panes");
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
    document.body.classList.add("is-resizing-panes");
  }

  function handleDividerKeyDown(e) {
    const step = 5;
    if (e.key === "ArrowUp") {
      setTopPercent((p) => Math.max(MIN_SECTION_PERCENT, p - step));
    } else if (e.key === "ArrowDown") {
      setTopPercent((p) => Math.min(100 - MIN_SECTION_PERCENT, p + step));
    }
  }

  return { containerRef, topPercent, startDrag, handleDividerKeyDown };
}
