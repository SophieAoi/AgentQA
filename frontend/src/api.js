const API_BASE = "http://localhost:8000";
const WS_BASE = API_BASE.replace(/^http/, "ws");

// credentials: "include" on every call — the session cookie (phase 7) is
// HttpOnly and cross-port (5173 -> 8000 in local dev), so it's only ever
// sent automatically by the browser when a request explicitly opts in.
async function apiFetch(path, options = {}) {
  return fetch(`${API_BASE}${path}`, { ...options, credentials: "include" });
}

export function getRunStreamUrl(runId, channel) {
  return `${WS_BASE}/ws/test-runs/${runId}/${channel}`;
}

export async function login(username, password) {
  const res = await apiFetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Login failed");
  }
  return res.json(); // User
}

export async function logout() {
  await apiFetch("/auth/logout", { method: "POST" });
}

export async function getCurrentUser() {
  const res = await apiFetch("/auth/me");
  if (!res.ok) return null;
  return res.json(); // User
}

export async function sendChatMessage(message) {
  const res = await apiFetch("/chat/message", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error("Failed to send message");
  return res.json(); // { reply }
}

export async function getChatHistory() {
  const res = await apiFetch("/chat/history");
  if (!res.ok) throw new Error("Failed to load chat history");
  return res.json();
}

export async function startTestRun(testCaseIds) {
  const res = await apiFetch("/test-runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ test_case_ids: testCaseIds }),
  });
  if (!res.ok) throw new Error("Failed to start test run");
  return res.json(); // TestRunSummary
}

export async function getTestRun(runId) {
  const res = await apiFetch(`/test-runs/${runId}`);
  if (!res.ok) throw new Error("Failed to fetch test run");
  return res.json(); // TestRunDetail
}

export async function getTestCases() {
  const res = await apiFetch("/test-cases");
  if (!res.ok) throw new Error("Failed to fetch test cases");
  return res.json(); // TestCase[]
}

async function throwWithDetail(res, fallbackMessage) {
  const body = await res.json().catch(() => ({}));
  throw new Error(body.detail || fallbackMessage);
}

export async function createTestCase(testCase) {
  const res = await apiFetch("/test-cases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(testCase),
  });
  if (!res.ok) await throwWithDetail(res, "Failed to create test case");
  return res.json(); // TestCase
}

export async function updateTestCase(id, testCase) {
  const res = await apiFetch(`/test-cases/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(testCase),
  });
  if (!res.ok) await throwWithDetail(res, "Failed to update test case");
  return res.json(); // TestCase
}

export async function deleteTestCase(id) {
  const res = await apiFetch(`/test-cases/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) await throwWithDetail(res, "Failed to delete test case");
}

export function getReportUrl(runId) {
  return `${API_BASE}/test-runs/${runId}/report`;
}

export function getReportPdfUrl(runId) {
  return `${API_BASE}/test-runs/${runId}/report.pdf`;
}
