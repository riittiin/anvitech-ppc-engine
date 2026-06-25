# Two-role login (Admin / User) — Design

**Date:** 2026-06-25
**Status:** approved (pending spec review)
**Branch:** `two-role-login`

## Goal

Replace the single HTTP Basic Auth gate (browser popup, one shared
`APP_USERNAME`/`APP_PASSWORD`) with an **app-owned login page** that supports
**two roles** with different permissions:

- **Admin** — everything the app can do today (full control).
- **User** — read-only view of all tabs, plus exactly two actions: download the
  Rule 6 machine-allocation sheet, and submit Capture Actuals (Rule 7) entries.

The login logic is — and already was — entirely the app's own code; Render only
stored the password values as environment variables. This change makes that
ownership visible (a real login screen + logout) and adds the role split.

## Decisions (confirmed with the user)

1. **Login mechanism:** a real in-app login page backed by a **signed session
   cookie**, replacing the browser Basic-Auth popup. No new third-party library —
   the cookie is signed with Python's stdlib `hmac`/`hashlib`, matching this
   project's keep-dependencies-minimal style.
2. **Credentials location:** **baked into the code** (user's explicit choice), in
   one clearly-marked block, with an **optional env-var override** so a password
   can be changed in Render later without a code edit.
3. **Credentials (baked defaults):**
   | Role | Username | Password |
   |---|---|---|
   | admin | `anvitech` | `1930rail` |
   | user | `anvitech_user` | `anvitech12345678` |
4. **User sees everything read-only**, but the only things a user can *act on* are
   the Rule 6 download and the Capture Actuals form.
5. **User has no Plan button.** Instead, the user always sees **the admin's
   last-saved plan settings**, so a downloaded sheet matches what the planner set
   up. (Achieved by persisting the plan config — see below.)
6. **"Mark order complete" is admin-only** — it archives an order (a planning
   decision), so it is hidden from the user and refused server-side.

## Permission matrix

| Capability | Admin | User |
|---|---|---|
| View Orders / Rules 1–8 / Gantt | ✅ | ✅ (read-only) |
| Config bar (window, setup, overlap, priority, downtime toggle) | ✅ | ❌ hidden |
| Plan button (`POST /run`, `POST /rerun`) | ✅ | ❌ hidden; auto-loads admin's plan |
| Upload Excel (`POST /upload`) | ✅ | ❌ 403 |
| Delete selected / Delete all (`/orders/delete`, `/orders/clear`) | ✅ | ❌ 403 |
| Download Rule 6 allocation CSV / machine-wise CSV | ✅ | ✅ |
| Capture Actuals — submit production (`POST /actuals`) | ✅ | ✅ |
| "Mark order complete" (`mark_complete=true`) | ✅ | ❌ ignored/forced false |
| Read endpoints (`/orders`, `/gantt`, `/items`, `/report`, `/trace`) | ✅ | ✅ |

**Enforcement is server-side** (not just hidden in the UI). UI hiding is polish on
top so users never see controls they can't use.

## Architecture

### New: `api/auth.py`
A small, focused module — the only place that knows about accounts and sessions.

- `ACCOUNTS`: the baked-in credential block at the top of the file:
  ```python
  ACCOUNTS = {
      "anvitech":      {"password": "1930rail",        "role": "admin"},
      "anvitech_user": {"password": "anvitech12345678", "role": "user"},
  }
  ```
  Each account may be overridden by env vars if present (back-door, no code edit):
  `ADMIN_USERNAME` / `ADMIN_PASSWORD`, `USER_USERNAME` / `USER_PASSWORD`.
  (Legacy `APP_USERNAME`/`APP_PASSWORD`, if set, override the admin account too,
  so an existing Render deploy keeps working through the transition.)
- `authenticate(username, password) -> role | None` — constant-time compare
  (`secrets.compare_digest`); returns the role on success, else `None`.
- **Session secret** `_secret() -> bytes`:
  1. `SESSION_SECRET` env var if set, else
  2. a value persisted in the durable store (`anvitech:session_secret`), else
  3. generate a random one (`secrets.token_hex`), persist it, and use it.
  This keeps sessions stable across restarts with **zero required env vars** — no
  third-party dependency — while allowing an explicit override.
- `make_token(username, role) -> str` — base64 of `{u, role, iat}` + an
  HMAC-SHA256 signature, joined by `.`.
- `verify_token(token) -> {u, role} | None` — re-computes the HMAC
  (constant-time), rejects tampered/garbage tokens and tokens older than
  `MAX_AGE_DAYS` (default 30). Returns the payload or `None`.

The cookie is named `anvitech_session`, set `HttpOnly`, `SameSite=Lax`, and
`Secure` when the request is HTTPS (so it works on http://localhost in dev and is
secured on Render).

### Changed: `api/main.py`

- **Replace** the `basic_auth` middleware with a `session_auth` middleware:
  - Public paths (no session needed): `GET /login`, `POST /login`, `POST /logout`,
    and the login page's own assets (the page is self-contained, so none extra).
  - Otherwise: read + verify the session cookie.
    - Invalid/missing **and** it's a browser navigation (`GET` with
      `Accept: text/html`) → `302` redirect to `/login`.
    - Invalid/missing otherwise (API/XHR) → `401`.
    - Valid → stash `request.state.user` / `request.state.role` and continue.
- **New routes** (declared before the static mount so they take precedence):
  - `GET  /login` → serve `web/login.html`.
  - `POST /login` (form: `username`, `password`) → on success set cookie + `200`
    (the page redirects); on failure `401` with a short message.
  - `POST /logout` → clear the cookie, `200`.
  - `GET  /me` → `{ "username": ..., "role": ... }` for the signed-in user (drives
    the front-end's role-aware UI).
- **Admin-only guard:** a tiny helper `require_admin(request)` raising `403`,
  applied in `/upload`, `/orders/delete`, `/orders/clear`.
- **`/actuals`:** if the caller is not admin, force `mark_complete = False` before
  recording (so a user can log production but not archive orders).
- **`/run`, `/rerun`, `/gantt` — persisted plan config:**
  - New store key `anvitech:plan_config` (kv: JSON of the `Config`).
  - On `POST /run`: if the caller is **admin**, use the submitted config and
    **persist it**; if the caller is **user**, ignore any submitted config and use
    the persisted one (falling back to defaults if none saved yet).
  - `GET /gantt` uses the persisted config instead of a bare `Config()`.
  - Net effect: a single shared "current plan" everyone sees — the worker
    downloads exactly what the planner planned.

### New: `web/login.html`
Self-contained page (inline CSS in the app's visual style): title, username +
password fields, a Sign-in button, and an error line. Submits to `POST /login`
via `fetch`; on success redirects to `/`, on failure shows the error.

### Changed: `web/app.js` + `web/index.html`
- On load: `GET /me`. Store `currentRole`.
- Add a top **session bar**: "Signed in as `<username>` · `<role>`" + a **Logout**
  button (`POST /logout` → redirect to `/login`).
- If `currentRole === "user"`:
  - hide the entire `.controls` config block **and** the Plan button,
  - hide the `.datasource` (upload) section,
  - on the Orders tab, hide the per-row select checkboxes and the
    "Delete selected" / "Delete ALL data" buttons,
  - in the Capture Actuals form, hide the "Mark this order complete" checkbox,
  - keep the Rule 6 download buttons and the Capture Actuals form.
- **Auto-load the current plan on login** for both roles (call `/run` once on
  startup) so the schedule / Gantt / rule tabs are populated without a Plan click.
  For admin, the Plan button still re-runs with the live config.

## Data flow

```
Browser → GET /  (no cookie)
        → middleware: browser nav, not authed → 302 /login
        → GET /login → login.html
        → POST /login {username,password}
            → auth.authenticate() → role
            → Set-Cookie anvitech_session=<signed token>; 200
        → redirect to /
        → GET /me → {username, role}
        → app.js hides admin-only controls for role=user
        → GET-equivalent: POST /run (admin config persisted; user uses persisted)
            → trace + gantt + orders rendered
```

Admin-only call by a user (e.g. `POST /orders/delete`) → `require_admin` → `403`.

## Error handling

- Wrong username/password → `POST /login` returns `401` + message; page shows
  "Incorrect username or password." (No detail on which field — standard practice.)
- Tampered/expired cookie → treated as not-signed-in (redirect or `401`).
- A user hitting an admin endpoint directly → `403` (defensive; UI already hides it).
- No `SESSION_SECRET` and an empty store → a secret is generated and persisted on
  first need; never fatal.

## Testing (test-first)

New `tests/test_auth.py`:
- `authenticate` accepts each correct pair, rejects wrong password / unknown user.
- `make_token`/`verify_token` round-trip; a tampered token fails; an expired token
  fails.
- `POST /login` (admin) sets a cookie; `/me` then reports `role=admin`.
- `POST /login` (user) → `/me` reports `role=user`.
- Wrong password → `401`.
- No cookie: a JSON endpoint → `401`; `GET /` (Accept: text/html) → `302 /login`.
- **User is refused** server-side: `/upload` → `403`, `/orders/delete` → `403`,
  `/orders/clear` → `403`.
- **User is allowed**: `/actuals` (no mark-complete) → `200`; a user's
  `mark_complete=true` is ignored (order stays active).
- **User Plan config**: a user `POST /run` ignores submitted config and uses the
  admin-persisted one.
- `POST /logout` clears the cookie (subsequent protected call → `401`/redirect).

Update `tests/test_api.py`:
- Replace the Basic-Auth header with a login helper (POST `/login` as admin once;
  `TestClient` keeps the cookie). All existing assertions must still pass.
- Keep a `test_requires_login`-style check (no cookie → blocked).

Golden trace: **unchanged** — no rule logic is touched.

## Docs to update (same PR)

- `CLAUDE.md` — Commands/Login section: two roles, login page, where credentials
  live, the env back-door.
- `HANDOFF.md` — replace the Basic-Auth notes; document the two logins, the
  role split, the persisted plan config, and the local login (`anvitech` /
  `1930rail`).
- `render.yaml` — note the optional override env vars (kept `sync:false`); the app
  works with none set.
- `README` — update the login line.

## Out of scope (unchanged)

- More than two roles, per-user accounts, password reset/self-service, account
  management UI. (Two shared logins only.)
- Any change to the scheduling rules or the order-book logic.

## Risks / notes

- **Passwords in git history** (user's accepted tradeoff): use passwords not reused
  elsewhere; they can be rotated via the env back-door without a code edit.
- **Cookie `Secure` flag**: must be set only on HTTPS or local http login breaks;
  detect via request scheme / `X-Forwarded-Proto`.
- **Existing tests import `APP_USERNAME`/`APP_PASSWORD`** — update those imports
  when the constants move to `auth.py` (keep a compatible admin alias).
- **First deploy after this lands**: anyone currently logged in via the old popup
  will be sent to the new login page (expected). Tell the user to hard-refresh.
