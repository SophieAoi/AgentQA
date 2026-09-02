import { useCallback, useEffect, useState } from "react";
import { getCurrentUser, login as apiLogin, logout as apiLogout } from "../api";

/**
 * Owns login/logout/current-user state (phase 7). checkAuth() runs once on
 * mount to see if a session cookie from a previous visit is still valid —
 * that's what makes the session survive a page refresh.
 */
export function useAuth() {
  const [user, setUser] = useState(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [authError, setAuthError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    getCurrentUser()
      .then((u) => {
        if (!cancelled) setUser(u);
      })
      .finally(() => {
        if (!cancelled) setCheckingAuth(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username, password) => {
    setAuthError(null);
    try {
      const u = await apiLogin(username, password);
      setUser(u);
      return u;
    } catch (err) {
      setAuthError(err.message || "Login failed");
      throw err;
    }
  }, []);

  const logout = useCallback(async () => {
    await apiLogout();
    setUser(null);
  }, []);

  return { user, checkingAuth, authError, login, logout };
}
