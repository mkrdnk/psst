"use strict";

(() => {
  const REFRESH_MS = 5000;
  const MAX_LENGTH = 2000;
  const SERVER_ERROR_CODES = new Set([
    "invalid_origin", "rate_limited", "invalid_password", "auth_required",
    "invalid_session", "invalid_csrf", "unsupported_media_type", "payload_too_large",
    "invalid_json", "json_object_required", "invalid_field", "invalid_unicode",
    "invalid_message", "not_found", "method_not_allowed", "request_failed",
  ]);
  const elements = Object.fromEntries(
    [
      "loading", "login-view", "dashboard", "logout", "login-form",
      "password", "login-feedback", "login-submit", "session-feedback",
      "message-form", "message", "message-counter", "message-feedback",
      "message-submit", "device-status", "device-last-seen", "device-interval",
      "sync-feedback", "history-empty", "message-history", "language-ru", "language-en",
    ].map((id) => [id, document.getElementById(id)]),
  );
  let language = "ru";
  try {
    const saved = localStorage.getItem("psst.language");
    if (saved === "ru" || saved === "en") language = saved;
  } catch {
    // Storage may be disabled; the panel still works with an in-memory preference.
  }
  let dateFormat;
  const feedbackMessages = new Map();

  let csrfToken = null;
  let authenticated = false;
  let sessionVersion = 0;
  let refreshTimer = null;
  let statePending = false;
  let refreshRequested = false;
  let sending = false;
  let historySnapshot = null;
  let latestState = null;
  let loggingIn = false;
  let messageValidationShown = false;

  function t(key, values = {}) {
    const catalog = PSST_TRANSLATIONS[language];
    const text = Object.hasOwn(catalog, key) ? catalog[key] : catalog.request_failed;
    return text.replace(/\{(\w+)\}/g, (_, name) => String(values[name]));
  }

  function renderFeedback(id) {
    const { keys, isError } = feedbackMessages.get(id);
    elements[id].textContent = keys.map((key) => t(key)).join(" ");
    elements[id].classList.toggle("is-error", isError);
  }

  function feedback(id, keys = [], isError = false) {
    feedbackMessages.set(id, {
      keys: (Array.isArray(keys) ? keys : [keys]).filter(Boolean),
      isError,
    });
    renderFeedback(id);
  }

  function renderButtons() {
    elements["login-submit"].textContent = t(loggingIn ? "logging_in" : "login");
    elements["message-submit"].textContent = t(sending ? "sending" : "send");
  }

  function applyLanguage() {
    document.documentElement.lang = language;
    dateFormat = new Intl.DateTimeFormat(language === "ru" ? "ru-RU" : "en-GB", {
      day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
    for (const element of document.querySelectorAll("[data-i18n]")) {
      element.textContent = t(element.dataset.i18n);
    }
    for (const attribute of ["placeholder", "aria-label"]) {
      for (const element of document.querySelectorAll(`[data-i18n-${attribute}]`)) {
        element.setAttribute(attribute, t(element.getAttribute(`data-i18n-${attribute}`)));
      }
    }
    for (const element of document.querySelectorAll("time")) {
      element.textContent = formatDate(element.dateTime);
    }
    for (const id of feedbackMessages.keys()) renderFeedback(id);
    for (const locale of ["ru", "en"]) {
      elements[`language-${locale}`].setAttribute("aria-pressed", String(language === locale));
    }
    if (latestState) renderDevice(latestState.device);
    renderButtons();
  }

  for (const locale of ["ru", "en"]) {
    elements[`language-${locale}`].addEventListener("click", () => {
      language = locale;
      try {
        localStorage.setItem("psst.language", language);
      } catch {
        // Switching must also work when the browser blocks preference storage.
      }
      applyLanguage();
    });
  }

  function translatedElement(tag, key) {
    const element = document.createElement(tag);
    element.dataset.i18n = key;
    element.textContent = t(key);
    return element;
  }

  function errorKey(error) {
    return SERVER_ERROR_CODES.has(error.code) || error.code === "network_error"
      ? error.code
      : "request_failed";
  }

  async function request(path, body) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    const options = {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      headers: { Accept: "application/json" },
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      if (path !== "/api/admin/login" && csrfToken !== null) {
        options.headers["X-CSRF-Token"] = csrfToken;
      }
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, options);
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const code = SERVER_ERROR_CODES.has(data?.code)
          ? data.code
          : response.status === 429 ? "rate_limited" : "request_failed";
        throw Object.assign(new Error(code), { code, status: response.status });
      }
      return data;
    } catch (error) {
      if (error.status) throw error;
      throw Object.assign(new Error("network_error"), { code: "network_error" });
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function showLogin(message = "", isError = false) {
    sessionVersion += 1;
    authenticated = false;
    csrfToken = null;
    window.clearTimeout(refreshTimer);
    refreshRequested = false;
    elements.loading.hidden = true;
    elements.dashboard.hidden = true;
    elements.logout.hidden = true;
    elements["login-view"].hidden = false;
    elements["message-history"].replaceChildren();
    historySnapshot = null;
    latestState = null;
    feedback("login-feedback", message, isError);
    elements.password.focus();
  }

  function showDashboard() {
    const entering = !authenticated;
    authenticated = true;
    elements.loading.hidden = true;
    elements["login-view"].hidden = true;
    elements.dashboard.hidden = false;
    elements.logout.hidden = false;
    if (entering) {
      elements.password.value = "";
      elements.password.setAttribute("aria-invalid", "false");
      feedback("session-feedback", "");
      feedback("message-feedback", "");
      elements.message.focus();
    }
  }

  function expired(error) {
    if (error.status !== 401) return false;
    showLogin("session_expired");
    return true;
  }

  function formatDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? t("unknown_date") : dateFormat.format(date);
  }

  function timestamp(value) {
    const time = document.createElement("time");
    time.className = "message-date";
    time.dateTime = value;
    time.textContent = formatDate(value);
    return time;
  }

  function renderDevice(device) {
    const labels = {
      never_seen: "device_never_seen",
      online: "device_online",
      offline: "device_offline",
    };
    elements["device-status"].textContent = t(labels[device.status] || "status_unknown");
    elements["device-status"].dataset.status = device.status;
    elements["device-last-seen"].textContent = device.last_seen
      ? formatDate(device.last_seen)
      : t("never_connected");
    elements["device-interval"].textContent = t("seconds", { count: device.ping_interval_seconds });
  }

  function renderState(state) {
    latestState = state;
    renderDevice(state.device);
    // Keep unchanged history nodes in place during polling.
    const snapshot = JSON.stringify(state.messages);
    if (snapshot === historySnapshot) return;
    const fragment = document.createDocumentFragment();
    for (const message of state.messages) {
      const item = document.createElement("li");
      item.className = "message-card";
      item.classList.toggle("is-current", message.is_current);
      const meta = document.createElement("div");
      meta.className = "message-meta";
      const status = translatedElement(
        message.received_at ? "summary" : "span",
        message.received_at ? "delivered" : message.is_current ? "waiting" : "replaced",
      );
      status.className = "badge";
      if (message.received_at) {
        status.classList.add("badge-received");
      } else if (message.is_current) {
        status.classList.add("badge-waiting");
      }
      meta.append(timestamp(message.created_at));
      if (message.received_at) {
        const delivery = document.createElement("details");
        delivery.className = "delivery-details";
        const received = document.createElement("p");
        received.append(translatedElement("span", "received_prefix"), timestamp(message.received_at));
        delivery.append(status, received);
        meta.append(delivery);
      } else {
        meta.append(status);
      }
      if (message.is_current) {
        const current = translatedElement("span", "current");
        current.className = "message-label";
        meta.append(current);
      }
      const text = document.createElement("p");
      text.className = "message-text";
      text.textContent = message.text;
      item.append(text, meta);
      fragment.append(item);
    }
    elements["message-history"].replaceChildren(fragment);
    elements["history-empty"].hidden = state.messages.length !== 0;
    historySnapshot = snapshot;
  }

  async function refreshState() {
    if (statePending) {
      refreshRequested = true;
      return;
    }
    window.clearTimeout(refreshTimer);
    statePending = true;
    refreshRequested = false;
    const version = sessionVersion;
    try {
      const state = await request("/api/admin/state");
      if (version !== sessionVersion) return;
      csrfToken = state.csrf_token;
      renderState(state);
      showDashboard();
      feedback("sync-feedback", "");
    } catch (error) {
      if (version !== sessionVersion) return;
      if (error.status === 401 && !authenticated) {
        showLogin();
      } else if (!expired(error)) {
        if (authenticated) {
          feedback("sync-feedback", [errorKey(error), "stale_data"], true);
        } else {
          showLogin(errorKey(error), true);
        }
      }
    } finally {
      statePending = false;
      if (authenticated) {
        if (refreshRequested) {
          void refreshState();
        } else {
          refreshTimer = window.setTimeout(refreshState, REFRESH_MS);
        }
      }
    }
  }

  function updateCounter() {
    const length = Array.from(elements.message.value).length;
    const tooLong = length > MAX_LENGTH;
    const empty = !elements.message.value.trim();
    if (!empty && !tooLong) messageValidationShown = false;
    elements["message-counter"].textContent = `${length} / ${MAX_LENGTH}`;
    elements["message-counter"].classList.toggle("is-error", tooLong);
    elements.message.setAttribute("aria-invalid", String(tooLong || (messageValidationShown && empty)));
    elements["message-submit"].disabled = sending || tooLong;
  }

  elements.message.addEventListener("input", updateCounter);
  elements.password.addEventListener("input", () => {
    if (elements.password.value) elements.password.setAttribute("aria-invalid", "false");
  });

  elements["login-form"].addEventListener("submit", async (event) => {
    event.preventDefault();
    if (elements["login-submit"].disabled) return;
    if (!elements.password.value) {
      elements.password.setAttribute("aria-invalid", "true");
      feedback("login-feedback", "password_required", true);
      elements.password.focus();
      return;
    }
    loggingIn = true;
    elements["login-submit"].disabled = true;
    renderButtons();
    feedback("login-feedback", "");
    try {
      const result = await request("/api/admin/login", { password: elements.password.value });
      sessionVersion += 1;
      csrfToken = result.csrf_token;
      showDashboard();
      await refreshState();
    } catch (error) {
      elements.password.setAttribute("aria-invalid", String(errorKey(error) === "invalid_password"));
      feedback("login-feedback", errorKey(error), true);
      elements.password.focus();
    } finally {
      loggingIn = false;
      elements["login-submit"].disabled = false;
      renderButtons();
    }
  });

  elements["message-form"].addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending || !authenticated) return;
    const text = elements.message.value;
    if (!text.trim() || Array.from(text).length > MAX_LENGTH) {
      messageValidationShown = true;
      updateCounter();
      feedback("message-feedback", "invalid_message", true);
      elements.message.focus();
      return;
    }
    const version = sessionVersion;
    sending = true;
    elements.message.readOnly = true;
    renderButtons();
    updateCounter();
    feedback("message-feedback", "");
    try {
      await request("/api/admin/messages", { text });
      if (version !== sessionVersion) return;
      elements.message.value = "";
      feedback("message-feedback", "sent");
      await refreshState();
    } catch (error) {
      if (version !== sessionVersion) return;
      if (!expired(error)) {
        const hint = error.status ? "" : "retry_send_hint";
        feedback("message-feedback", [errorKey(error), hint], true);
      }
    } finally {
      sending = false;
      elements.message.readOnly = false;
      renderButtons();
      updateCounter();
    }
  });

  elements.logout.addEventListener("click", async () => {
    if (elements.logout.disabled) return;
    const version = sessionVersion;
    elements.logout.disabled = true;
    feedback("session-feedback", "");
    try {
      await request("/api/admin/logout", {});
      if (version === sessionVersion) showLogin("logged_out");
    } catch (error) {
      if (version === sessionVersion && !expired(error)) {
        feedback("session-feedback", errorKey(error), true);
      }
    } finally {
      elements.logout.disabled = false;
    }
  });

  applyLanguage();
  updateCounter();
  void refreshState();
})();
