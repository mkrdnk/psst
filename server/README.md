# PSST server

A single Starlette process, SQLite, and a small HTML/JS panel with no frontend
build step. Jam issues and verifies PASETO v4.local tokens for admin login.
The server supports one device, which receives only the latest message, not a queue.

## Getting started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
cd server
uv sync --locked
cp .env.example .env
```

Fill in `.env` locally:

- `ADMIN_PASSWORD` — a long, unique password for the panel.
- `DEVICE_TOKEN` — a separate random token, also needed by the ESP.
- `PASETO_SECRET_KEY` — 32 random bytes encoded as base64url.

For the last two values, run this command **separately for each secret**:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do not reuse a secret for multiple purposes. Do not share secrets in chat or
commit a populated `.env` file. The `.env.example` file contains no secrets.

```sh
uv run --env-file .env uvicorn main:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000**, matching `PUBLIC_ORIGIN`.
Variables are loaded by `uv --env-file`; the application does not read `.env` itself.
The server will not start without the required secrets.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | Required | Password-only login, with no username or registration |
| `DEVICE_TOKEN` | Required | Bearer token for the device API only |
| `PASETO_SECRET_KEY` | Required | v4.local key: exactly 32 bytes encoded as base64url |
| `PUBLIC_ORIGIN` | `http://localhost:8000` | Exact panel origin, with no trailing `/` |
| `DATABASE_PATH` | `server/data/psst.db` | SQLite file; relative paths are resolved from the working directory |
| `PING_INTERVAL_SECONDS` | `30` | Expected ping interval used to determine device availability |
| `SESSION_TTL_SECONDS` | `43200` | Session lifetime in seconds: 12 hours |

HTTP is allowed only for `localhost` and `127.0.0.1`.
The origin must be canonical: a lowercase scheme and ASCII hostname,
without the default port (`:443` for HTTPS or `:80` for HTTP).
Use punycode for internationalized domain names. IPv6 address literals
are not supported.
For external access, set `PUBLIC_ORIGIN=https://psst.example.com` (using your domain)
and place the server behind an HTTPS reverse proxy. Preserve the original `Host`;
only the hostname from `PUBLIC_ORIGIN` is allowed. Do not expose the Uvicorn port
directly to the internet. Trust forwarded headers only from your own proxy.

Run **one worker**: the login rate limit is stored in application memory
(5 attempts per 60 seconds, including successful attempts, shared by all clients).
A restart resets this limit. There are no queues, Redis, or background tasks.

The session cookie uses `HttpOnly`, `SameSite=Strict`, and `Secure` over HTTPS.
After verifying the PASETO token, the server checks its expiry, intended use,
and admin identity. Panel POST requests require a valid `Origin` and,
after login, a CSRF token. Messages are rendered as text, not HTML;
there are no external scripts or fonts.

Logging out removes the cookie from the current browser but does not revoke
a stolen copy: it remains valid until it expires. Changing `ADMIN_PASSWORD`
or `PASETO_SECRET_KEY` and restarting the server invalidates existing sessions.
Changing `DEVICE_TOKEN` also requires updating the token on the ESP.

SQLite stores message text **without encryption**. Restrict access to the
database and its backups, and keep the data directory on persistent storage
when deploying. History is not deleted automatically; the panel shows the
latest 50 messages.

## Device API

Include this header in **every** request:

```http
Authorization: Bearer <DEVICE_TOKEN>
```

The ESP does not need the admin password or PASETO key.
In production, the ESP must verify the server's TLS certificate.

### `POST /ping`

No body is required. Updates the last-seen timestamp and returns exactly:

```json
{"unread": true, "ping": "pong"}
```

`unread` means a current message exists and has not yet been acknowledged by
the device. It is `false` if there is no message or the message has already
been acknowledged. The response contains no message text or additional fields.

`PING_INTERVAL_SECONDS` is not included in the ping response: configure the
same interval in the firmware. The device is “not connected yet” before its
first ping, then “online” if no more than `3 × N` seconds have elapsed since
the last ping, or “offline” otherwise. Fetching or acknowledging a message
does not update the ping timestamp.

### `GET /message`

Returns the current message:

```json
{"id": 42, "text": "You look lovely today"}
```

Returns **204 No Content** if no messages have been created. Fetching a message
does not acknowledge delivery. The current message is returned even if it has
already been acknowledged, so the ESP can restore its display after a restart
even when ping reports `unread: false`.

### `POST /message/ack`

After saving and displaying the text, send:

```http
Content-Type: application/json
```

```json
{"id": 42}
```

Returns **204 No Content**. Repeating an ACK is safe and preserves the first
acknowledgement timestamp. An unknown ID returns **404**. Acknowledging an
older message does not acknowledge a newer message sent in the meantime.

### ESP workflow

1. On startup, restore the last message from local storage or fetch `/message`
   to restore the display.
2. Send `/ping` every `N` seconds.
3. When `unread: true`, fetch `/message`, save its ID and text, and display it.
4. Send an ACK for that specific ID. If the same ID is received again, repeat
   the ACK; there is no need to replay the display animation.
5. Retry network failures with a delay. Do not acknowledge a message that
   could not be processed.

If A, B, and C are sent while the device is offline, it receives only C.
The panel's history is not a delivery queue. “Delivered” means the device
acknowledged receipt, not that someone read the message.

## Panel API

The panel is served at `/`, with static assets under `/static/`.
The header includes a RU / EN switch, available before login as well.
Russian is the default. The preference is saved in `localStorage` under
`psst.language`; if storage is blocked, the selection lasts until the page
is reloaded. Switching languages preserves the session, draft, and expanded
delivery details. Dates and labels change language; message text stays unchanged.
Translations are defined in `static/i18n.js`, with no additional libraries.

| Method and path | Purpose |
| --- | --- |
| `POST /api/admin/login` | JSON `{"password": "…"}` → cookie and `csrf_token` |
| `GET /api/admin/state` | Device status, latest 50 messages, and `csrf_token` |
| `POST /api/admin/messages` | JSON `{"text": "…"}` → new message, HTTP 201 |
| `POST /api/admin/logout` | Removes the cookie, HTTP 204 |

Every panel POST request requires an `Origin` matching `PUBLIC_ORIGIN`.
Authenticated POST requests also require the `X-CSRF-Token` returned by
login/state. The panel refreshes its state every 5 seconds.

Message text is trimmed and must contain 1–2000 Unicode code points.
JSON request bodies are limited to 32 KiB. Panel API errors include the
legacy `error` field and a stable `code`, for example:

```json
{"error": "...", "code": "invalid_password"}
```

The `error` field retains the existing Russian messages for compatibility.
The panel translates errors using `code`, not the text in `error`.
An unknown code is displayed as a generic error in the selected language.
Authentication and access-control codes:
`invalid_password`, `auth_required`, `invalid_session`, `invalid_origin`,
`invalid_csrf`, `rate_limited`. Validation codes: `unsupported_media_type`,
`payload_too_large`, `invalid_json`, `json_object_required`, `invalid_field`,
`invalid_unicode`, `invalid_message`. Other errors: `not_found`,
`method_not_allowed`, `request_failed`.

The device error format remains unchanged: `{"error": "…"}`. HTTP status codes:
400 — invalid data, 401 — authentication required, 403 — Origin/CSRF failure,
413 — request body too large, 415 — non-JSON content type, 429 — login rate limit
(the wait time is provided in `Retry-After`).

## Testing

Test dependencies are kept separately in `[dependency-groups].test`.

```sh
cd server
uv sync --locked --group test
uv run --locked --group test pytest
```

Tests use a temporary SQLite database, random secrets, the real Jam/PASETO
implementation, and Starlette's HTTP test client. They do not require a
production database or an `.env` file.

With Node.js installed, you can also test the panel's asynchronous response
handling and language switching without npm dependencies:

```sh
cd server
node --test tests/frontend.test.cjs
```

These tests exercise JavaScript logic with a simulated DOM, not a real browser.
