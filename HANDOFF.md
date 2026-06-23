# HANDOFF — Anvitech PPC Engine

Orientation for a **new Claude Code session** taking over this project. Read
[`CLAUDE.md`](CLAUDE.md) first (design principles, data flow, code map); this doc is
the current-state snapshot and operational notes.

---

## TL;DR

A **Production Planning & Control (PPC) engine** for Anvitech, a precision-machining
job shop. **Built, tested, and deployed live.**

- **Live app:** https://anvitech-ppc.onrender.com (login-gated).
- **Host:** Render (free web service). **Database:** MongoDB Atlas (free M0).
- **Repo:** GitHub `riittiin/anvitech-ppc-engine` (private). Push to `main` →
  Render auto-redeploys.
- **65 tests pass** (`pytest`). FastAPI backend + vanilla HTML/JS frontend, plain
  Python engine.

## What it is now (architecture)

The 9 business rules (see [`RULES.md`](RULES.md)) turn sales orders into a
machine-by-machine schedule + Gantt, re-planning as actual production comes in. The
engine is a **persistent, shared Order Book** (keyed by unique SO number) sitting
**above the unchanged pure Rules 1–7**:

- **Upload** an Excel → orders **merge** into the book (new SO# → Pending; a
  known/completed/intra-file-duplicate SO# → flagged, never double-counted).
- **Plan** (one button; unifies the old "Run" + "Rerun MRP") → schedules every
  *active* order at its **remaining** qty (ordered − good produced) through Rules 1–7.
- **Rule 8** daily entry records production; the first actual flips an order
  Pending → Running; ticking **"mark complete"** on an entry → Complete (archived).
- **Delete** (single / multiple / all) permanently removes from the database.
- **Login:** HTTP Basic Auth gates the whole app. **Persistence:**
  `engine/storage.py` picks **MongoDB > Upstash > local file** by env var.

## Run / test / deploy

```bash
pip install -r requirements.txt
pytest                                   # 65 tests
REGEN_GOLDEN=1 pytest -k golden          # only after an intentional logic change
uvicorn api.main:app --reload            # http://127.0.0.1:8000
```
Locally, with no store env vars set, data goes to `data/store/` (gitignored).
Default login is `anvitech` / `ppc2025`. **Deploy:** push to `main`.

## Live deployment specifics (no secrets in this file)

- **Render** service `anvitech-ppc` — free web service; **sleeps after ~15 min idle**
  (first hit after that takes ~30–60s to wake; data is unaffected).
- **MongoDB Atlas** free **M0 (512 MB)** — database `anvitech`, collections `hash`
  (orders), `list` (actuals), `kv` (masters). **Atlas Network Access includes
  `0.0.0.0/0`** (required because Render's free IPs are dynamic).
- **Env vars set on Render:** `APP_USERNAME`, `APP_PASSWORD`, `MONGODB_URI`. (Upstash
  vars may also be present but are **ignored** — Mongo takes priority.)

## Domain rules to honor (confirmed with the user)

- **SO number is the unique order key** (one SO# = one order line). Repeats are
  flagged, never double-counted; an order is **never auto-deleted** for being absent
  from an upload.
- **Status is derived:** Pending (no actuals) → Running (≥1 actual) → Complete
  (**explicit — only via the Rule 8 "mark complete" tick; the engine NEVER
  auto-completes**, not even at remaining ≤ 0).
- **Rule 3 priority = least slack** = (working-time-until-due) − (work-needed); no
  window by default; on equal dates it reduces to "more process time first." Metric
  (`slack`/`critical_ratio`/`process_time`) and window are configurable.
- **Rule 3 "total process time" = sum of per-process _cycle_ times** (data-confirmed
  against the SO-Remarks oracle).
- **Rule 6 = non-delay scheduler** — a machine never idles while an operation is
  ready for it.
- **Masters** are latest-wins on upload, but kept if a file omits them.

## Done vs deferred

**Done:** order book + lifecycle, upload-merge + dedup, completion via Rule 8,
unified Plan, hour-resolution Gantt with status labels, login, Render + MongoDB
deploy, permanent delete (single/multi/all), append-safe storage.

**Deferred (explicitly, per the user):**
- Applying **revisions** to existing orders (changed qty/date) — currently **flagged
  only**, original kept.
- Explicit **cancel** action (orders leave only via complete or delete).
- Optional **export-then-purge** of completed orders to keep the DB lean — not needed
  at current scale (512 MB ≈ decades for one shop).

## Gotchas / operational notes

- **MongoDB Atlas free is 512 MB** (the 5 GB "Flex" tier is *paid*). At ~10–20 MB/year
  for one shop, this is decades of room — not a concern.
- **The bundled `Test2.xlsx` is test-only** and intentionally reuses SO# `24-25SO121A`
  across two dates (an old consolidation-test artifact) — that's why uploading it
  shows "1 flagged." Real production data has unique SO#s, so nothing flags.
- **`requirements.txt` includes `pymongo[srv]`** (needed for the `mongodb+srv://` URI).
- Local shell is **zsh**: when scripting `curl` with credentials, put
  `-u user:pass` **literally on each curl** — an unquoted `$VAR` won't word-split.
- Static assets are served with `Cache-Control: no-cache`; after a deploy a normal
  refresh picks up new JS/CSS.

## Where to look

- [`CLAUDE.md`](CLAUDE.md) — design principles, data flow, **code map**, commands. **Read first.**
- [`RULES.md`](RULES.md) — the 9 business rules (source of truth for rule logic).
- [`docs/superpowers/specs/2026-06-22-order-book-design.md`](docs/superpowers/specs/2026-06-22-order-book-design.md) — **current** architecture design.
- `docs/superpowers/specs/2026-06-19-…` — original (pre-order-book) design; historical reference.

## Making changes safely

- For substantive logic changes, update `RULES.md` / the relevant spec **first**, then code.
- Keep the rules **pure**; the order book (`orderbook.py` / `book_store.py`) is the only stateful layer.
- Run `pytest`; regenerate the golden trace only for **intentional** logic changes.
- **Commit/push to `main` only when the user asks** — it auto-deploys to the live site.
