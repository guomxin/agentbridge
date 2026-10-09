import { staleResponse } from "./admin_lifecycle.mjs";

export function csrfToken(cookieText) {
  const prefix = "agentbridge_admin_csrf=";
  const item = cookieText.split(";").map(value => value.trim()).find(value => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

export function createAdminApi({ fetchImpl = fetch, cookieText, readScope, accountScope, submission = () => null }) {
  return async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { ...(options.headers || {}) };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-AgentBridge-CSRF"] = csrfToken(cookieText());
    }
    const account = accountScope.capture();
    const view = method === "GET" ? readScope.capture() : null;
    const form = method === "GET" ? null : submission();
    const signal = options.signal ?? view?.signal;
    const assertCurrent = () => {
      if (!account.current() || (view && !view.current())) throw staleResponse();
      if (form && !form.current()) throw staleResponse();
      signal?.throwIfAborted();
    };
    // Writes are submitted once. Changing pages never retries or aborts a write.
    try {
      const response = await fetchImpl(path, { ...options, signal, method, headers, credentials: "same-origin" });
      const data = await response.json().catch(() => ({}));
      assertCurrent();
      if (!response.ok) {
        const error = new Error(data.error?.message || `请求失败 (${response.status})`);
        error.code = data.error?.code || "REQUEST_FAILED";
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      assertCurrent();
      throw error;
    }
  };
}

// A late initial session lookup must not clear a newer login or restored page.
export async function restoreAdminSession({ accountScope, api, showLogin, showApp, showPassword, loadView }) {
  const ticket = accountScope.begin();
  try {
    const session = await api("/api/session");
    if (!ticket.current()) return;
    if (!session.authenticated) { showLogin(); return; }
    showApp(session.account);
    if (session.account.must_change_password) showPassword();
    else await loadView();
  } catch (error) {
    if (!ticket.current() || error.name === "AbortError") return;
    showLogin();
  }
}
