import { useState, useEffect, useMemo } from "react";
import { getTestCases, getReportUrl, getReportPdfUrl, deleteTestCase } from "../api";
import TestCaseEditor from "./TestCaseEditor";

const TERMINAL_STATUSES = new Set(["passed", "failed", "error"]);
const OTHER_SUITE = "Other";

// A case is "gapped" (can't actually run) when one of its preconditions is
// a "GAP: ..." entry — see backend/agent/test_cases/*.yaml. Surfaced in the
// UI as a badge instead of just silently failing when run, and excluded
// from "Select all runnable" so a one-click full run doesn't queue 135+
// cases that are guaranteed to fail by design (missing fixtures, physical
// hardware, unconfigured external systems — not bugs to catch).
function isGapped(testCase) {
  return (testCase.preconditions || []).some((p) => p.startsWith("GAP:"));
}

function gapReason(testCase) {
  const gap = (testCase.preconditions || []).find((p) => p.startsWith("GAP:"));
  return gap ? gap.replace(/^GAP:\s*/, "") : null;
}

// Explicit display order — without this, suites render in whatever order
// their first test case happens to fall alphabetically by filename, which
// isn't a deliberate order at all. Any suite not listed here (e.g. a new
// one just added) falls back to appearing after these, in the order it's
// first encountered; "Other" (untagged cases) always renders last.
const SUITE_ORDER = [
  "Login",
  "Campaign Creation",
  "Line Item",
  "Deal Management",
  "Inventory",
  "Content Hub",
  "Delivery Reports",
  "Player Testing",
  "Planner",
  "Creative Types",
  "Day Parting",
  "Content Hub Extended",
];

export default function TestRunnerPanel({ run, pollError, runTests }) {
  const [testCases, setTestCases] = useState([]);
  const [testCasesError, setTestCasesError] = useState(null);
  const [selectedIds, setSelectedIds] = useState([]);
  const [expandedSuites, setExpandedSuites] = useState({});
  const [editorState, setEditorState] = useState(null); // null | "new" | TestCase (editing)
  const [deletingId, setDeletingId] = useState(null);
  const [deleteError, setDeleteError] = useState(null);
  const isRunning = run?.status === "running";

  function refetchTestCases() {
    return getTestCases()
      .then((cases) => {
        setTestCases(cases);
        setTestCasesError(null);
        // New suites default to expanded so a first-time suite is visible
        // without an extra click; suites the user already collapsed keep
        // that state across refetches.
        setExpandedSuites((prev) => {
          const next = { ...prev };
          for (const tc of cases) {
            const suite = tc.suite || OTHER_SUITE;
            if (!(suite in next)) next[suite] = true;
          }
          return next;
        });
      })
      .catch(() => setTestCasesError("Couldn't load test cases — is the backend running?"));
  }

  useEffect(() => {
    refetchTestCases();
  }, []);

  async function handleDelete(id) {
    if (!window.confirm(`Delete ${id}? This removes agent/test_cases/${id}.yaml and can't be undone.`)) {
      return;
    }
    setDeletingId(id);
    setDeleteError(null);
    try {
      await deleteTestCase(id);
      setSelectedIds((prev) => prev.filter((x) => x !== id));
      await refetchTestCases();
    } catch (err) {
      setDeleteError(`Couldn't delete ${id}: ${err.message}`);
    } finally {
      setDeletingId(null);
    }
  }

  function handleEditorSaved() {
    setEditorState(null);
    refetchTestCases();
  }

  const suites = useMemo(() => {
    const groups = new Map();
    for (const tc of testCases) {
      const suite = tc.suite || OTHER_SUITE;
      if (!groups.has(suite)) groups.set(suite, []);
      groups.get(suite).push(tc);
    }

    function rank(name) {
      const i = SUITE_ORDER.indexOf(name);
      if (i !== -1) return i;
      // A real suite not yet added to SUITE_ORDER renders after the known
      // ones but still before "Other" (the untagged-case fallback, which
      // always renders last).
      return name === OTHER_SUITE ? SUITE_ORDER.length + 1 : SUITE_ORDER.length;
    }

    return Array.from(groups.entries())
      .map(([name, cases]) => ({ name, cases }))
      .sort((a, b) => rank(a.name) - rank(b.name));
  }, [testCases]);

  // Real suite names in use, for the editor's suite datalist — lets a
  // new/edited case reuse an existing suite name via autocomplete
  // without hardcoding SUITE_ORDER as the only allowed set.
  const knownSuites = useMemo(
    () => Array.from(new Set(testCases.map((tc) => tc.suite).filter(Boolean))).sort(),
    [testCases]
  );

  const runnableCases = useMemo(() => testCases.filter((tc) => !isGapped(tc)), [testCases]);
  const gappedCount = testCases.length - runnableCases.length;
  // Hand-picked, load-bearing cases per suite (see backend/app/models/schemas.py's
  // TestCase.essential docstring) — a fast smoke-test subset, not a fabricated
  // priority ranking. Always runnable by construction (never gapped), but
  // filtered defensively anyway in case that ever changes.
  const essentialCases = useMemo(
    () => testCases.filter((tc) => tc.essential && !isGapped(tc)),
    [testCases]
  );

  // O(1) status lookup per case while a run is active, instead of each
  // test-case row scanning run.case_results itself on every render.
  const caseStatusById = useMemo(() => {
    const map = new Map();
    for (const r of run?.case_results || []) map.set(r.test_case_id, r.status);
    return map;
  }, [run?.case_results]);

  function toggleTestCase(id) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function toggleSuiteExpanded(name) {
    setExpandedSuites((prev) => ({ ...prev, [name]: !prev[name] }));
  }

  function toggleSuiteSelection(cases) {
    const selectableIds = cases.filter((tc) => !isGapped(tc)).map((tc) => tc.id);
    const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selectedIds.includes(id));
    setSelectedIds((prev) =>
      allSelected
        ? prev.filter((id) => !selectableIds.includes(id))
        : Array.from(new Set([...prev, ...selectableIds]))
    );
  }

  function selectAllRunnable() {
    setSelectedIds(runnableCases.map((tc) => tc.id));
  }

  function selectEssential() {
    setSelectedIds(essentialCases.map((tc) => tc.id));
  }

  function clearSelection() {
    setSelectedIds([]);
  }

  async function handleRunTests() {
    if (selectedIds.length === 0) return;
    await runTests(selectedIds);
  }

  return (
    <div className="test-runner-panel">
      <div className="test-runner-header">
        <h3>Run Test Cases</h3>
        {testCases.length > 0 && (
          <div className="test-runner-summary">
            {runnableCases.length} runnable
            {gappedCount > 0 && <span className="test-runner-summary-gapped"> · {gappedCount} blocked</span>}
          </div>
        )}
      </div>

      {testCasesError && <div className="run-result run-result--error">{testCasesError}</div>}

      {testCases.length > 0 && (
        <div className="test-runner-bulk-actions">
          <button
            type="button"
            className="bulk-action-button bulk-action-button--essential"
            onClick={selectEssential}
            disabled={isRunning || essentialCases.length === 0}
            title="A hand-picked core case or two per suite — the fastest smoke check that something is fundamentally broken, not a full run."
          >
            ★ Select essential ({essentialCases.length})
          </button>
          <button
            type="button"
            className="bulk-action-button"
            onClick={selectAllRunnable}
            disabled={isRunning || runnableCases.length === 0}
            title={gappedCount > 0 ? `Skips ${gappedCount} blocked case(s) — see their GAP badges below` : undefined}
          >
            Select all runnable ({runnableCases.length})
          </button>
          {selectedIds.length > 0 && (
            <button type="button" className="bulk-action-button bulk-action-button--ghost" onClick={clearSelection} disabled={isRunning}>
              Clear selection
            </button>
          )}
          <button
            type="button"
            className="bulk-action-button bulk-action-button--primary test-runner-new-case-button"
            onClick={() => setEditorState("new")}
            disabled={isRunning}
          >
            + New Test Case
          </button>
        </div>
      )}

      {deleteError && <div className="run-result run-result--error">{deleteError}</div>}

      <div className="test-suite-list">
        {suites.map(({ name, cases }) => {
          const expanded = expandedSuites[name] ?? true;
          const selectable = cases.filter((tc) => !isGapped(tc));
          const allSelected = selectable.length > 0 && selectable.every((tc) => selectedIds.includes(tc.id));
          const someSelected = !allSelected && selectable.some((tc) => selectedIds.includes(tc.id));
          const suiteGappedCount = cases.length - selectable.length;

          return (
            <div key={name} className="test-suite-group">
              <div className="test-suite-header">
                <button
                  type="button"
                  className="test-suite-toggle"
                  onClick={() => toggleSuiteExpanded(name)}
                  aria-expanded={expanded}
                >
                  <span className={`test-suite-chevron ${expanded ? "test-suite-chevron--open" : ""}`}>
                    ▶
                  </span>
                  <span className="test-suite-name">{name}</span>
                  <span className="test-suite-count">{cases.length}</span>
                  {suiteGappedCount > 0 && (
                    <span className="test-suite-gap-count" title={`${suiteGappedCount} case(s) blocked — missing fixtures, hardware, or external system access`}>
                      {suiteGappedCount} blocked
                    </span>
                  )}
                </button>
                <label className="test-suite-select-all">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    disabled={selectable.length === 0 || isRunning}
                    ref={(el) => el && (el.indeterminate = someSelected)}
                    onChange={() => toggleSuiteSelection(cases)}
                  />
                  Select all
                </label>
              </div>

              {expanded && (
                <div className="test-case-list">
                  {cases.map((tc) => {
                    const gapped = isGapped(tc);
                    const liveStatus = caseStatusById.get(tc.id);
                    return (
                      <div
                        key={tc.id}
                        className={`test-case-item ${gapped ? "test-case-item--gapped" : ""} ${liveStatus ? `test-case-item--${liveStatus}` : ""}`}
                        title={gapped ? gapReason(tc) : undefined}
                      >
                        <label className="test-case-item-main">
                          <input
                            type="checkbox"
                            checked={selectedIds.includes(tc.id)}
                            disabled={gapped || isRunning}
                            onChange={() => toggleTestCase(tc.id)}
                          />
                          <span className="test-case-id">{tc.id}</span>
                          <span className="test-case-label">{tc.title}</span>
                          {tc.essential && (
                            <span className="test-case-essential-badge" title="Essential — core happy-path or foundational check for this suite">
                              ★
                            </span>
                          )}
                          {gapped && <span className="test-case-gap-badge">GAP</span>}
                          {liveStatus === "running" && <span className="test-case-live-badge test-case-live-badge--running">Running…</span>}
                          {liveStatus === "passed" && <span className="test-case-live-badge test-case-live-badge--passed">✓</span>}
                          {liveStatus === "failed" && <span className="test-case-live-badge test-case-live-badge--failed">✗</span>}
                        </label>
                        <div className="test-case-item-actions">
                          <button
                            type="button"
                            className="test-case-action-button"
                            onClick={() => setEditorState(tc)}
                            disabled={isRunning}
                            title={`Edit ${tc.id}`}
                            aria-label={`Edit ${tc.id}`}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            className="test-case-action-button test-case-action-button--danger"
                            onClick={() => handleDelete(tc.id)}
                            disabled={isRunning || deletingId === tc.id}
                            title={`Delete ${tc.id}`}
                            aria-label={`Delete ${tc.id}`}
                          >
                            {deletingId === tc.id ? "Deleting..." : "Delete"}
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <button
        className="run-button"
        onClick={handleRunTests}
        disabled={selectedIds.length === 0 || isRunning}
      >
        {isRunning ? "Running..." : `Run ${selectedIds.length || ""} Test(s)`}
      </button>

      {pollError && <div className="run-result run-result--error">{pollError}</div>}

      {run && (
        <div className={`run-result run-result--${run.status}`}>
          <div className="run-result-header">
            Run {run.run_id} — <strong>{run.status.toUpperCase()}</strong>
            {run.total_count > 0 && (
              <span> ({run.passed_count}/{run.total_count} passed)</span>
            )}
            {TERMINAL_STATUSES.has(run.status) && (
              <span className="run-report-links">
                <a href={getReportUrl(run.run_id)} target="_blank" rel="noreferrer">
                  View Report
                </a>
                {" · "}
                <a href={getReportPdfUrl(run.run_id)} target="_blank" rel="noreferrer">
                  Download PDF
                </a>
              </span>
            )}
          </div>

          {isRunning && run.current_test_case_id && (
            <div className="run-current-case">
              Running {run.current_test_case_index} of {run.total_count}: <strong>{run.current_test_case_id}</strong>
            </div>
          )}

          <ul className="run-steps">
            {run.steps?.map((step, i) => (
              <li key={i} className={`run-step run-step--${step.status.toLowerCase()}`}>
                <span className="step-status">{step.status === "OK" ? "✓" : "✗"}</span>
                <span className="step-description">{step.step_description}</span>
                {step.detail && <div className="step-detail">{step.detail}</div>}
                {step.screenshot_url && (
                  <div className="step-screenshot">
                    <a href={`http://localhost:8000${step.screenshot_url}`} target="_blank" rel="noreferrer">
                      View screenshot
                    </a>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {editorState && (
        <TestCaseEditor
          existing={editorState === "new" ? null : editorState}
          suites={knownSuites}
          onClose={() => setEditorState(null)}
          onSaved={handleEditorSaved}
        />
      )}
    </div>
  );
}
