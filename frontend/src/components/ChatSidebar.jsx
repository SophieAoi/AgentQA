import { useState, useEffect, useRef } from "react";
import { sendChatMessage, getChatHistory } from "../api";
import TestRunnerPanel from "./TestRunnerPanel";
import { useResizablePanes } from "../hooks/useResizablePanes";

const DEFAULT_CHAT_PERCENT = 55;

export default function ChatSidebar({ run, pollError, runTests, user, logout, width }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef(null);
  const {
    containerRef: panesRef,
    topPercent: chatPercent,
    startDrag,
    handleDividerKeyDown,
  } = useResizablePanes(DEFAULT_CHAT_PERCENT);

  useEffect(() => {
    getChatHistory().then(setMessages).catch(() => {});
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleSend() {
    const text = input.trim();
    if (!text || sending) return;

    setInput("");
    setSending(true);

    // Show the user's message immediately, don't wait for the round-trip
    setMessages((prev) => [...prev, { role: "user", content: text }]);

    try {
      const { reply } = await sendChatMessage(text);
      setMessages((prev) => [...prev, { role: "agent", content: reply }]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "agent", content: "(Error reaching the agent backend — is it running?)" },
      ]);
    } finally {
      setSending(false);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <aside className="chat-sidebar" style={width ? { width: `${width}px` } : undefined}>
      <div className="chat-header">
        <h2>INFLUENCE QA Agent</h2>
        {user && (
          <div className="chat-header-user">
            <span>{user.username}</span>
            <button onClick={logout} className="logout-button">
              Sign out
            </button>
          </div>
        )}
      </div>

      <div className="chat-sidebar-panes" ref={panesRef}>
        <div className="chat-sidebar-pane chat-sidebar-chat-pane" style={{ height: `${chatPercent}%` }}>
          <div className="chat-messages">
            {messages.length === 0 && (
              <div className="chat-empty">
                Ask the agent to run tests, or use the panel below to trigger a run directly.
              </div>
            )}
            {messages.map((msg, i) => (
              <div key={i} className={`chat-message chat-message--${msg.role}`}>
                <div className="chat-message-role">{msg.role === "user" ? "You" : "Agent"}</div>
                <div className="chat-message-content">{msg.content}</div>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div className="chat-input-row">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask the agent something..."
              rows={2}
            />
            <button onClick={handleSend} disabled={sending || !input.trim()}>
              Send
            </button>
          </div>
        </div>

        <div
          className="chat-sidebar-divider"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize chat and test suite sections"
          tabIndex={0}
          onMouseDown={startDrag}
          onKeyDown={handleDividerKeyDown}
        >
          <div className="chat-sidebar-divider-handle" />
        </div>

        <div
          className="chat-sidebar-pane chat-sidebar-tests-pane"
          style={{ height: `${100 - chatPercent}%` }}
        >
          <TestRunnerPanel run={run} pollError={pollError} runTests={runTests} />
        </div>
      </div>
    </aside>
  );
}
