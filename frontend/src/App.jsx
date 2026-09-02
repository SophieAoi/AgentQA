import SiteViewer from "./components/SiteViewer";
import ChatSidebar from "./components/ChatSidebar";
import LoginScreen from "./components/LoginScreen";
import { useTestRun } from "./hooks/useTestRun";
import { useAuth } from "./hooks/useAuth";
import { useResizableSidebar } from "./hooks/useResizableSidebar";
import "./App.css";

const DEFAULT_SIDEBAR_WIDTH = 380;

export default function App() {
  // Lifted up so SiteViewer can show the same run's live log alongside
  // the pass/fail results TestRunnerPanel (inside ChatSidebar) renders.
  const { run, pollError, runTests } = useTestRun();
  const { user, checkingAuth, authError, login, logout } = useAuth();
  const { width: sidebarWidth, startDrag, handleDividerKeyDown } = useResizableSidebar(DEFAULT_SIDEBAR_WIDTH);

  if (checkingAuth) {
    return <div className="auth-checking">Loading…</div>;
  }

  if (!user) {
    return <LoginScreen login={login} authError={authError} />;
  }

  return (
    <div className="app-layout">
      <main className="app-main">
        <SiteViewer run={run} />
      </main>
      <div
        className="app-sidebar-divider"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize sidebar width"
        tabIndex={0}
        onMouseDown={startDrag}
        onKeyDown={handleDividerKeyDown}
      >
        <div className="app-sidebar-divider-handle" />
      </div>
      <ChatSidebar
        run={run}
        pollError={pollError}
        runTests={runTests}
        user={user}
        logout={logout}
        width={sidebarWidth}
      />
    </div>
  );
}
