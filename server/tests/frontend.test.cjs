const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const translations = readFileSync(path.join(__dirname, "../static/i18n.js"), "utf8");
const markup = readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.children = [];
    this.listeners = {};
    this.classList = { toggle() {}, add() {} };
    this.attributes = {};
  }
  set innerHTML(_) { throw new Error("Use textContent for untrusted messages"); }
  setAttribute(name, value) {
    this.attributes[name] = value;
    if (name.startsWith("data-")) {
      this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    }
    if (name === "hidden") this.hidden = true;
  }
  getAttribute(name) { return this.attributes[name]; }
  focus() {}
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  trigger(name) { return this.listeners[name]({ preventDefault() {} }); }
}

function panel({ storedLanguage, storageThrows = false } = {}) {
  const elements = new Map();
  const staticElements = [];
  for (const [, tagName, attributes] of markup.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*?)>/gi)) {
    const element = new Element(tagName);
    for (const [, name, value = ""] of attributes.matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) {
      element.setAttribute(name, value);
    }
    staticElements.push(element);
    if (element.getAttribute("id")) elements.set(element.getAttribute("id"), element);
  }
  const requests = [];
  const timers = new Map();
  let nextTimer = 0;
  const document = {
    documentElement: staticElements.find(element => element.tagName === "html"),
    querySelectorAll(selector) {
      const nodes = new Set();
      function visit(element) {
        if (nodes.has(element)) return;
        nodes.add(element);
        for (const child of element.children || []) visit(child);
      }
      for (const element of staticElements) visit(element);
      return [...nodes].filter(element => {
        if (selector === "[data-i18n]") return element.dataset?.i18n;
        if (selector.startsWith("[")) return element.getAttribute?.(selector.slice(1, -1)) !== undefined;
        return element.tagName === selector;
      });
    },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement: (tagName) => new Element(tagName),
    createDocumentFragment: () => new Element(),
    createTextNode: (textContent) => ({ textContent }),
  };
  const context = vm.createContext({
    document, Intl, AbortController,
    localStorage: {
      getItem() {
        if (storageThrows) throw new Error("Storage blocked");
        return storedLanguage;
      },
      setItem(key, value) {
        if (storageThrows) throw new Error("Storage blocked");
        assert.equal(key, "psst.language");
        storedLanguage = value;
      },
    },
    window: {
      setTimeout(callback, delay) {
        timers.set(++nextTimer, { callback, delay });
        return nextTimer;
      },
      clearTimeout(id) { timers.delete(id); },
    },
    fetch(url, options) {
      return new Promise((resolve, reject) => {
        requests.push({
          url, options,
          reject,
          respond(data, status = 200) {
            resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
          },
        });
      });
    },
  });
  vm.runInContext(translations, context);
  vm.runInContext(script, context);
  return {
    document,
    savedLanguage: () => storedLanguage,
    element: (id) => document.getElementById(id),
    take(url) {
      const index = requests.findIndex((request) => request.url === url);
      assert.notEqual(index, -1, `Expected request ${url}`);
      return requests.splice(index, 1)[0];
    },
    pending: (url) => requests.filter((request) => request.url === url).length,
    poll() {
      const timer = [...timers].find(([, timer]) => timer.delay === 5000);
      assert.ok(timer, "Expected scheduled refresh");
      timers.delete(timer[0]);
      void timer[1].callback();
    },
  };
}

function state(messages = [], csrf = "csrf") {
  return {
    csrf_token: csrf,
    device: { status: "online", last_seen: null, ping_interval_seconds: 30 },
    messages,
  };
}

function message(text) {
  return { id: 1, text, is_current: true, created_at: "2026-01-01T00:00:00Z", received_at: null };
}

function displayedText(panel) {
  const fragment = panel.element("message-history").children[0];
  return fragment?.children[0]?.children[0]?.textContent;
}

test("sending during a poll queues an immediate non-overlapping refresh", async () => {
  const ui = panel();
  ui.take("/api/admin/state").respond(state());
  await settle();
  ui.poll();
  const stale = ui.take("/api/admin/state");
  ui.element("message").value = "Новое сообщение";
  const submitted = ui.element("message-form").trigger("submit");
  ui.take("/api/admin/messages").respond({ id: 1 }, 201);
  await settle();
  assert.equal(ui.pending("/api/admin/state"), 0, "Do not overlap state requests");
  stale.respond(state());
  await settle();
  assert.equal(ui.pending("/api/admin/state"), 1, "Refresh without waiting another 5 seconds");
  ui.take("/api/admin/state").respond(state([message("Новое сообщение")]));
  await settle();
  await submitted;
  assert.equal(displayedText(ui), "Новое сообщение");
  assert.equal(ui.element("message").value, "");
});

test("late state from the previous session is ignored after logout and login", async () => {
  const ui = panel();
  ui.take("/api/admin/state").respond(state());
  await settle();
  ui.poll();
  const stale = ui.take("/api/admin/state");
  const logout = ui.element("logout").trigger("click");
  ui.take("/api/admin/logout").respond(null, 204);
  await logout;
  assert.equal(ui.element("dashboard").hidden, true);
  ui.element("password").value = "test input";
  const login = ui.element("login-form").trigger("submit");
  ui.take("/api/admin/login").respond({ csrf_token: "new-csrf" });
  await settle();
  stale.respond(state([message("Old session")]));
  await settle();
  assert.equal(displayedText(ui), undefined);
  ui.take("/api/admin/state").respond(state([message("New session")], "new-csrf"));
  await settle();
  await login;
  assert.equal(displayedText(ui), "New session");
});

test("message markup stays text and failed sends preserve the draft", async () => {
  const ui = panel();
  const text = '<img src=x onerror="alert(1)">';
  ui.take("/api/admin/state").respond(state([message(text)]));
  await settle();
  assert.equal(displayedText(ui), text);
  ui.element("message").value = "Черновик";
  const submitted = ui.element("message-form").trigger("submit");
  ui.take("/api/admin/messages").respond({ error: "Ошибка" }, 500);
  await submitted;
  assert.equal(ui.element("message").value, "Черновик");
  assert.equal(ui.element("message-feedback").textContent, "Не удалось выполнить запрос. Попробуйте ещё раз.");
});

test("compact history keeps delivery states and marks only the current message", async () => {
  const ui = panel();
  const receivedAt = "2026-01-01T00:01:00Z";
  ui.take("/api/admin/state").respond(state([
    message("Current"),
    { ...message("Replaced"), id: 2, is_current: false },
    { ...message("Delivered"), id: 3, is_current: false, received_at: receivedAt },
  ]));
  await settle();
  const rows = ui.element("message-history").children[0].children;
  const metadata = rows.map(row => row.children[1].children);
  assert.equal(metadata[0][1].textContent, "Ждёт устройство");
  assert.equal(metadata[1][1].textContent, "Заменено");
  assert.equal(metadata[0][2].textContent, "Текущее");
  assert.equal(metadata[1].length, 2);
  assert.equal(metadata[2].length, 2);
  const delivery = metadata[2][1];
  assert.equal(delivery.children[0].textContent, "Доставлено");
  assert.equal(delivery.children[1].children[1].dateTime, receivedAt);
  assert.equal(metadata[0][0].dateTime, "2026-01-01T00:00:00Z");
  assert.equal(ui.element("sync-feedback").textContent, "");
});

test("RU is the default and stored EN translates static labels and attributes", () => {
  const ru = panel();
  assert.equal(ru.document.documentElement.lang, "ru");
  assert.equal(ru.element("compose-title").textContent, "Сообщение");
  const en = panel({ storedLanguage: "en" });
  assert.equal(en.document.documentElement.lang, "en");
  assert.equal(en.element("login-submit").textContent, "Log in");
  assert.equal(en.element("message").getAttribute("placeholder"), "Write a message…");
  assert.equal(en.element("message-history").getAttribute("aria-label"), "Message history, newest first");
  assert.equal(en.element("language-en").getAttribute("aria-pressed"), "true");
  assert.equal(panel({ storedLanguage: "fr" }).document.documentElement.lang, "ru");
});

test("switching language preserves drafts, history nodes, delivery details and the session", async () => {
  const ui = panel();
  const text = "Сообщение пользователя <b>не переводить</b>";
  ui.take("/api/admin/state").respond(state([
    { ...message(text), received_at: "2026-01-02T00:00:00Z" },
  ]));
  await settle();
  ui.element("message").value = "Черновик";
  const fragment = ui.element("message-history").children[0];
  const metadata = fragment.children[0].children[1].children;
  const delivery = metadata[1];
  delivery.open = true;
  const russianDate = metadata[0].textContent;
  await ui.element("language-en").trigger("click");
  assert.equal(ui.savedLanguage(), "en");
  assert.equal(ui.document.documentElement.lang, "en");
  assert.equal(ui.element("message").value, "Черновик");
  assert.equal(ui.element("message-history").children[0], fragment);
  assert.equal(delivery.open, true);
  assert.equal(delivery.children[0].textContent, "Delivered");
  assert.equal(delivery.children[1].children[0].textContent, "Received ");
  assert.equal(metadata[2].textContent, "Current");
  assert.notEqual(metadata[0].textContent, russianDate);
  assert.equal(displayedText(ui), text);
  assert.equal(ui.element("device-status").textContent, "Online");
  assert.equal(ui.element("device-interval").textContent, "30 sec.");
  assert.equal(ui.element("dashboard").hidden, false);
  assert.equal(ui.pending("/api/admin/state"), 0, "Switching must not refetch or restart the session");
  await ui.element("language-ru").trigger("click");
  assert.equal(delivery.children[0].textContent, "Доставлено");
  assert.equal(metadata[0].textContent, russianDate);
});

test("switching works when localStorage is blocked", async () => {
  const ui = panel({ storageThrows: true });
  assert.equal(ui.document.documentElement.lang, "ru");
  await ui.element("language-en").trigger("click");
  assert.equal(ui.element("login-submit").textContent, "Log in");
});

test("server error codes and existing feedback follow the selected language", async () => {
  const ui = panel();
  ui.take("/api/admin/state").respond({ code: "auth_required", error: "Необходим вход." }, 401);
  await settle();
  ui.element("password").value = "incorrect";
  const login = ui.element("login-form").trigger("submit");
  assert.equal(ui.element("login-submit").textContent, "Входим…");
  await ui.element("language-en").trigger("click");
  assert.equal(ui.element("login-submit").textContent, "Logging in…");
  assert.equal(ui.element("login-submit").disabled, true);
  ui.take("/api/admin/login").respond({ code: "invalid_password", error: "Неверный пароль." }, 401);
  await login;
  assert.equal(ui.element("login-feedback").textContent, "Incorrect password.");
  assert.equal(ui.element("login-submit").textContent, "Log in");
  await ui.element("language-ru").trigger("click");
  assert.equal(ui.element("login-feedback").textContent, "Неверный пароль.");
});

test("a language change during send and polling uses the new language without losing state", async () => {
  const ui = panel();
  ui.take("/api/admin/state").respond(state());
  await settle();
  ui.element("message").value = "Привет";
  const sent = ui.element("message-form").trigger("submit");
  const request = ui.take("/api/admin/messages");
  assert.equal(JSON.parse(request.options.body).text, "Привет");
  assert.equal(request.options.headers["X-CSRF-Token"], "csrf");
  await ui.element("language-en").trigger("click");
  assert.equal(ui.element("message-submit").textContent, "Sending…");
  assert.equal(ui.element("message-submit").disabled, true);
  assert.equal(ui.element("message").readOnly, true);
  request.respond({ id: 1 }, 201);
  await settle();
  ui.take("/api/admin/state").respond(state([message("Привет")]));
  await sent;
  assert.equal(ui.element("message-feedback").textContent, "Sent.");
  assert.equal(ui.element("message-submit").textContent, "Send");
  assert.equal(ui.element("message").value, "");
  assert.equal(displayedText(ui), "Привет");
  const metadata = ui.element("message-history").children[0].children[0].children[1].children;
  assert.equal(metadata[1].textContent, "Waiting for device");
});

test("validation, network errors and expiry translate without browser-native validation", async () => {
  assert.match(markup, /id="login-form"[^>]*novalidate/);
  assert.match(markup, /id="message-form"[^>]*novalidate/);
  const ui = panel({ storedLanguage: "en" });
  ui.take("/api/admin/state").respond(state());
  await settle();
  await ui.element("message-form").trigger("submit");
  assert.match(ui.element("message-feedback").textContent, /Enter a message/);
  ui.element("message").value = "Keep this draft";
  const send = ui.element("message-form").trigger("submit");
  ui.take("/api/admin/messages").reject(new Error("Disconnected"));
  await send;
  assert.match(ui.element("message-feedback").textContent, /server did not respond/);
  assert.match(ui.element("message-feedback").textContent, /check the history/);
  ui.poll();
  ui.take("/api/admin/state").respond({ code: "invalid_session" }, 401);
  await settle();
  assert.equal(ui.element("dashboard").hidden, true);
  assert.match(ui.element("login-feedback").textContent, /Your session has ended/);
  await ui.element("language-ru").trigger("click");
  assert.match(ui.element("login-feedback").textContent, /Сессия завершилась/);
  assert.equal(ui.element("message").value, "Keep this draft");
  assert.equal(ui.element("message-history").children.length, 0, "Switching after logout must not restore history");
  await ui.element("login-form").trigger("submit");
  assert.equal(ui.element("login-feedback").textContent, "Введите пароль.");
});

test("unknown error codes never display raw server text", async () => {
  for (const code of ["__proto__", "future_error", "sent", "seconds", "network_error"]) {
    const ui = panel({ storedLanguage: "en" });
    ui.take("/api/admin/state").respond(state());
    await settle();
    ui.element("message").value = "Keep this draft";
    const send = ui.element("message-form").trigger("submit");
    ui.take("/api/admin/messages").respond({ code, error: "Русская ошибка" }, 500);
    await send;
    assert.equal(ui.element("message-feedback").textContent, "The request failed. Please try again.");
    assert.equal(ui.element("message").value, "Keep this draft");
  }
});

test("custom validation marks invalid fields and clears the state after correction", async () => {
  const ui = panel();
  ui.take("/api/admin/state").respond(state());
  await settle();
  ui.element("message").value = " \n ";
  await ui.element("message-form").trigger("submit");
  assert.equal(ui.element("message").getAttribute("aria-invalid"), "true");
  await ui.element("language-en").trigger("click");
  assert.equal(ui.element("message").getAttribute("aria-invalid"), "true");
  await ui.element("message").trigger("input");
  assert.equal(ui.element("message").getAttribute("aria-invalid"), "true");
  ui.element("message").value = "Corrected";
  await ui.element("message").trigger("input");
  assert.equal(ui.element("message").getAttribute("aria-invalid"), "false");
  await ui.element("login-form").trigger("submit");
  assert.equal(ui.element("password").getAttribute("aria-invalid"), "true");
  ui.element("password").value = "password";
  await ui.element("password").trigger("input");
  assert.equal(ui.element("password").getAttribute("aria-invalid"), "false");
});

test("RU and EN have the same translation keys and cover the HTML", () => {
  const context = vm.createContext({});
  vm.runInContext(translations, context);
  const catalogs = vm.runInContext("PSST_TRANSLATIONS", context);
  assert.deepEqual(Object.keys(catalogs.ru).sort(), Object.keys(catalogs.en).sort());
  for (const [, key] of markup.matchAll(/data-i18n(?:-placeholder|-aria-label)?="([^"]+)"/g)) {
    assert.equal(typeof catalogs.ru[key], "string", key);
    assert.equal(typeof catalogs.en[key], "string", key);
  }
  for (const [key, value] of Object.entries(catalogs.ru)) {
    assert.deepEqual(value.match(/\{\w+\}/g), catalogs.en[key].match(/\{\w+\}/g), key);
  }
  for (const file of ["main.py", "auth.py"]) {
    const source = readFileSync(path.join(__dirname, "..", file), "utf8");
    for (const [, code] of source.matchAll(/code="([^"]+)"/g)) {
      assert.equal(typeof catalogs.en[code], "string", `Missing translation for ${code}`);
    }
  }
});
